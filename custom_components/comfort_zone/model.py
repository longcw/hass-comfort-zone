"""First-order-plus-dead-time (FOPDT) predictor — the dead-time compensation.

This is the ``predictor`` seam. The supervisor never re-derives thermal dynamics
itself; it asks the model two questions:

* ``remaining_effect(now)`` — of the setpoint steps already issued, how much
  cooling/heating is *still on the way* but not yet visible in the sensor?
  This is what lets the controller be patient: "don't command cooling you
  already have coming." (The Smith-predictor idea.)
* ``predict_settled(now, y)`` — where will ``comfort_temp`` settle once the
  in-flight steps have fully played out?

A setpoint step of ``Δ`` °C is modelled as producing a settled comfort change of
``Δ · gain`` °C, arriving on a first-order curve after a pure dead-time ``L``:

    response_fraction(e) = 0                      for e < L
                         = 1 − exp(−(e − L)/τ)    for e ≥ L

The constants (L, τ, gain, power-lead, engagement) are fit offline from recorder
history by :mod:`system_id` and passed in via ``ModelParams``; defaults live in
:data:`const.MODEL_DEFAULTS`.

The model holds no Home Assistant references and is fully unit-testable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .const import (
    DEAD_MAX,
    DEAD_MIN,
    GAIN_MAX,
    GAIN_MIN,
    LEAD_CAP,
    SP_MARGIN_CAP,
    MK_DEAD_TIME,
    MK_ENGAGE_WATTS,
    MK_ENGAGE_WINDOW,
    MK_GAIN,
    MK_LEAD,
    MK_POWER_LEAD,
    MK_SP_MARGIN,
    MK_TAU,
    MODEL_DEFAULTS,
)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


@dataclass
class ModelParams:
    dead_time_min: float = MODEL_DEFAULTS[MK_DEAD_TIME]
    tau_min: float = MODEL_DEFAULTS[MK_TAU]
    gain_per_step: float = MODEL_DEFAULTS[MK_GAIN]
    power_lead_min: float = MODEL_DEFAULTS[MK_POWER_LEAD]
    engage_watts: float = MODEL_DEFAULTS[MK_ENGAGE_WATTS]
    engage_window_min: float = MODEL_DEFAULTS[MK_ENGAGE_WINDOW]
    lead_min: float = MODEL_DEFAULTS[MK_LEAD]
    sp_margin: float = MODEL_DEFAULTS[MK_SP_MARGIN]

    @classmethod
    def from_dict(cls, data: dict | None) -> "ModelParams":
        d = dict(MODEL_DEFAULTS)
        if data:
            d.update({k: v for k, v in data.items() if k in d})
        # Clamp on load: a value learned under older or buggier rules must not
        # persist out of range (gain froze at 0.164 while no episode could mature,
        # and lead was left sitting at a retired cap).
        return cls(
            dead_time_min=_clamp(float(d[MK_DEAD_TIME]), DEAD_MIN, DEAD_MAX),
            tau_min=float(d[MK_TAU]),
            gain_per_step=_clamp(float(d[MK_GAIN]), GAIN_MIN, GAIN_MAX),
            power_lead_min=float(d[MK_POWER_LEAD]),
            engage_watts=float(d[MK_ENGAGE_WATTS]),
            engage_window_min=float(d[MK_ENGAGE_WINDOW]),
            lead_min=_clamp(float(d[MK_LEAD]), 0.0, LEAD_CAP),
            sp_margin=_clamp(float(d[MK_SP_MARGIN]), 0.0, SP_MARGIN_CAP),
        )

    def to_dict(self) -> dict:
        return {
            MK_DEAD_TIME: self.dead_time_min,
            MK_TAU: self.tau_min,
            MK_GAIN: self.gain_per_step,
            MK_POWER_LEAD: self.power_lead_min,
            MK_ENGAGE_WATTS: self.engage_watts,
            MK_ENGAGE_WINDOW: self.engage_window_min,
            MK_LEAD: self.lead_min,
            MK_SP_MARGIN: self.sp_margin,
        }


@dataclass
class _Step:
    """A setpoint change that may still be materialising in the room."""

    at: datetime
    delta_c: float  # negative for a cooling step (setpoint lowered)


class FopdtPredictor:
    """Tracks in-flight setpoint steps and answers prediction queries."""

    def __init__(self, params: ModelParams) -> None:
        self.params = params
        self._steps: list[_Step] = []

    # -- bookkeeping --------------------------------------------------------
    def record_setpoint_change(self, at: datetime, delta_c: float) -> None:
        """Register a setpoint change (delta<0 = cooling step)."""
        if delta_c != 0:
            self._steps.append(_Step(at=at, delta_c=float(delta_c)))

    def _prune(self, now: datetime) -> None:
        # A step is fully materialised well after L + a few τ; drop it then.
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

    # -- queries ------------------------------------------------------------
    def remaining_effect(self, now: datetime) -> float:
        """°C of comfort change still on the way (negative = cooling coming)."""
        self._prune(now)
        total = 0.0
        for s in self._steps:
            elapsed = (now - s.at).total_seconds() / 60.0
            unmaterialised = 1.0 - self.response_fraction(elapsed)
            total += s.delta_c * self.params.gain_per_step * unmaterialised
        return total

    def in_flight_effect(self, now: datetime) -> float:
        """°C still on the way from the most recently commanded *direction*.

        ``remaining_effect`` is a net superposition over the whole horizon, which is
        what "where does it settle" needs — but it is the wrong question for "should
        I wait?". After a couple of easing steps their unmaterialised warming cancels
        a fresh cooling step, so the net reads ≈0 and the controller concludes nothing
        is in flight seconds after commanding it (that stacked 26→25→24 in 45 s).
        Only the tail of same-direction steps describes what we just asked for.
        """
        self._prune(now)
        if not self._steps:
            return 0.0
        cooling = self._steps[-1].delta_c < 0
        total = 0.0
        for s in reversed(self._steps):
            if (s.delta_c < 0) != cooling:
                break
            elapsed = (now - s.at).total_seconds() / 60.0
            total += s.delta_c * self.params.gain_per_step * (1.0 - self.response_fraction(elapsed))
        return total

    def has_pending_cooling(self, now: datetime) -> bool:
        """True if a cooling step is engaged and still materialising."""
        return self.in_flight_effect(now) < -0.03

    def predict_settled(self, now: datetime, y: float) -> float:
        """Where comfort_temp settles once in-flight steps play out."""
        return y + self.remaining_effect(now)

    def predict_trend(
        self,
        y: float,
        slope: float | None,
        horizon_min: float,
    ) -> float:
        """Near-term extrapolation from the measured slope (bounded).

        Used for the fast fan layer. Slope is only trustworthy over a short
        horizon, so it is capped to avoid wild extrapolation.
        """
        if slope is None:
            return y
        h = min(horizon_min, 8.0)  # never trust instantaneous slope beyond ~8 min
        return y + slope * h
