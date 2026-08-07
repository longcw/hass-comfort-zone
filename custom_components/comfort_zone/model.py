"""The plant model — dead-time compensation, and the constants everything else reads.

Two jobs, kept together because they describe the same plant.

**The predictor** is the useful half of a Smith predictor. A setpoint step of Δ °C
produces a settled comfort change of ``Δ · gain`` arriving on a first-order curve
after a pure dead time ``L``:

    response_fraction(e) = 0                      for e < L
                         = 1 − exp(−(e − L)/τ)    for e ≥ L

so ``predict_settled`` answers "where will the room end up once everything already
commanded has played out". Feeding *that* back instead of the raw reading is what
takes the dead time out of the loop and lets a textbook tuning rule apply. The
predictor is driven from **observed** setpoint transitions rather than from the
controller's own commands: this VRF's cloud proxy re-reports its own remembered
setpoint after a power cycle (measured 08-06: 7 of 30 transitions were never
commanded), and the room feels what the unit is actually running at.

**The tuning constants are derived, never stored.** ``Kc`` and ``Ti`` come from the
plant by SIMC, so they cannot fall out of step with the model they were computed
from — which is how v4 ended up pacing a dwell off a dead time that had ratcheted
to 19 minutes under a rule that turned out to be broken. The single knob is
``tau_c_mult``: larger is slower and more robust.

Constants are fitted offline by :mod:`tools.fit` and reviewed by a human. There is
no online learning: it was not required, was not observed to help, and its one
measurable effect was a positive feedback loop through a learned dead time.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from .const import (
    DEAD_MAX,
    DEAD_MIN,
    GAIN_MAX,
    GAIN_MIN,
    MK_BLOWER_GAIN,
    MK_DEAD_TIME,
    MK_FF_INTERCEPT,
    MK_FF_PER_OUTDOOR,
    MK_FF_PER_TARGET,
    MK_GAIN,
    MK_TAU,
    MK_TAU_C_MULT,
    MODEL_DEFAULTS,
    TAU_C_MULT_MAX,
    TAU_C_MULT_MIN,
)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def recent_power_change(history, now: datetime, window_min: float) -> float | None:
    """Change in the live power reading over the last ``window_min``, for display.

    Display only: this is the card's arrow. The control-side use of power is a
    bounded feedforward with its own slow baseline — see :mod:`power`.
    """
    pts = [(t, v) for (t, v) in history if v is not None and t <= now]
    cut = now - timedelta(minutes=window_min)
    prior = [v for (t, v) in pts if t <= cut]
    if not pts or not prior or pts[-1][0] <= cut:
        return None
    return pts[-1][1] - prior[-1]


@dataclass
class ModelParams:
    dead_time_min: float = MODEL_DEFAULTS[MK_DEAD_TIME]
    tau_min: float = MODEL_DEFAULTS[MK_TAU]
    gain_per_step: float = MODEL_DEFAULTS[MK_GAIN]
    # Outdoor-reset curve: setpoint = intercept + per_outdoor·T_out + per_target·target
    ff_intercept: float = MODEL_DEFAULTS[MK_FF_INTERCEPT]
    ff_per_outdoor: float = MODEL_DEFAULTS[MK_FF_PER_OUTDOOR]
    ff_per_target: float = MODEL_DEFAULTS[MK_FF_PER_TARGET]
    # °C of equivalent setpoint one blower level is worth. Zero means "not a
    # temperature lever", and the output stage then drives it from saturation alone.
    blower_gain: float = MODEL_DEFAULTS[MK_BLOWER_GAIN]
    tau_c_mult: float = MODEL_DEFAULTS[MK_TAU_C_MULT]

    @property
    def tau_c(self) -> float:
        """SIMC closed-loop time constant — the one robustness knob."""
        return self.tau_c_mult * self.dead_time_min

    @property
    def kc(self) -> float:
        return self.tau_min / (max(self.gain_per_step, 1e-6) * (self.tau_c + self.dead_time_min))

    @property
    def ti_min(self) -> float:
        return min(self.tau_min, 4.0 * (self.tau_c + self.dead_time_min))

    @classmethod
    def from_dict(cls, data: dict | None) -> "ModelParams":
        d = dict(MODEL_DEFAULTS)
        # A store written before v5 is discarded whole, not merged. Its surviving
        # keys are the ones the online adapter used to write, and those are exactly
        # the constants v5 treats as reviewed priors — silently inheriting a dead
        # time some earlier rule had ratcheted up would reproduce the feedback loop
        # that adapter is deleted for. The absence of a feedforward key identifies it.
        if data and MK_FF_INTERCEPT in data:
            d.update({k: v for k, v in data.items() if k in d})
        # Clamp on load, so a value written under older rules cannot persist out of
        # range. The gain floor matters most: it divides into Kc, and a gain fitted
        # too low produces an over-aggressive loop rather than an obviously broken one.
        return cls(
            dead_time_min=_clamp(float(d[MK_DEAD_TIME]), DEAD_MIN, DEAD_MAX),
            tau_min=max(1.0, float(d[MK_TAU])),
            gain_per_step=_clamp(float(d[MK_GAIN]), GAIN_MIN, GAIN_MAX),
            # Clamped like everything else. These three are the largest lever on
            # the commanded setpoint and were the only constants loaded raw, despite
            # the promise above — a bad fit could put the feedforward anywhere, and
            # the integral has only ±5 °C of authority to argue back with.
            ff_intercept=_clamp(float(d[MK_FF_INTERCEPT]), -200.0, 200.0),
            ff_per_outdoor=_clamp(float(d[MK_FF_PER_OUTDOOR]), -2.0, 0.0),
            ff_per_target=_clamp(float(d[MK_FF_PER_TARGET]), 0.5, 5.0),
            blower_gain=max(0.0, float(d[MK_BLOWER_GAIN])),
            tau_c_mult=_clamp(float(d[MK_TAU_C_MULT]), TAU_C_MULT_MIN, TAU_C_MULT_MAX),
        )

    def to_dict(self) -> dict:
        return {
            MK_DEAD_TIME: self.dead_time_min,
            MK_TAU: self.tau_min,
            MK_GAIN: self.gain_per_step,
            MK_FF_INTERCEPT: self.ff_intercept,
            MK_FF_PER_OUTDOOR: self.ff_per_outdoor,
            MK_FF_PER_TARGET: self.ff_per_target,
            MK_BLOWER_GAIN: self.blower_gain,
            MK_TAU_C_MULT: self.tau_c_mult,
        }


@dataclass
class _Step:
    """A change in delivered cooling that may still be materialising in the room."""

    at: datetime
    delta_c: float          # negative for more cooling


class FopdtPredictor:
    """Tracks in-flight steps and answers where the room is going to settle."""

    def __init__(self, params: ModelParams) -> None:
        self.params = params
        self._steps: list[_Step] = []

    def record_setpoint_change(self, at: datetime, delta_c: float) -> None:
        if delta_c:
            self._steps.append(_Step(at=at, delta_c=float(delta_c)))

    def _prune(self, now: datetime) -> None:
        horizon = self.params.dead_time_min + 5 * self.params.tau_min
        cutoff = now - timedelta(minutes=horizon)
        self._steps = [s for s in self._steps if s.at >= cutoff]

    def response_fraction(self, elapsed_min: float) -> float:
        """Fraction of a step's settled effect visible after ``elapsed_min``."""
        L = self.params.dead_time_min
        tau = max(self.params.tau_min, 1e-6)
        if elapsed_min < L:
            return 0.0
        return 1.0 - math.exp(-(elapsed_min - L) / tau)

    def remaining_effect(self, now: datetime) -> float:
        """°C of comfort change still on the way (negative = cooling coming)."""
        self._prune(now)
        total = 0.0
        for s in self._steps:
            elapsed = (now - s.at).total_seconds() / 60.0
            total += s.delta_c * self.params.gain_per_step * (1.0 - self.response_fraction(elapsed))
        return total

    def predict_settled(self, now: datetime, y: float) -> float:
        """Where comfort settles once everything already commanded plays out."""
        return y + self.remaining_effect(now)
