"""Fast online self-evolution.

Rather than a nightly batch refit, this watches each *cooling episode* as it
happens and nudges the model toward what the room actually did — so the
constants track current conditions (a hot afternoon behaves very differently
from a cool night) within an episode or two.

An episode starts at a cooling setpoint step and matures once the response has
had time to play out. At maturity we compare:

* **realized gain** — how much comfort actually dropped per °C of setpoint step
  → EMA-updates ``gain_per_step`` (small gain on a hot day makes the controller
  step more; large gain on a cool night makes it step less);
* **realized dead-time** — how long until the slope first turned down
  → EMA-updates ``dead_time_min``.

Episodes confounded by a warming step or the AC being cut are discarded, so we
only learn from clean cause→effect.

Pure and unit-testable; it mutates the :class:`ModelParams` it is given.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .const import LEAD_CAP, SP_MARGIN_CAP
from .model import ModelParams

ALPHA = 0.3                 # EMA weight on the newest observation (fast)
GAIN_MIN, GAIN_MAX = 0.1, 2.0
DEAD_MIN, DEAD_MAX = 2.0, 25.0
SLOPE_EPS = 0.02

# Anticipation-lead learning: keep the worst band overshoot within tolerance.
OVERSHOOT_TOL = 0.15        # °C of overshoot beyond the band we'll tolerate
LEAD_UP = 1.0              # minutes added per over-tolerance excursion (×severity)
LEAD_DOWN = 0.4           # minutes relaxed per within-tolerance excursion (anti-cycle)
# Setpoint deadband — the cycling knob. Widen it when excursions stay small (the
# compressor moves less); tighten it (safety-biased: faster) when we overshoot.
SP_MARGIN_UP = 0.05        # °C widened per within-tolerance excursion (fewer setpoint moves)
SP_MARGIN_DOWN = 0.10     # °C tightened per over-tolerance excursion (tighter control)


@dataclass
class _Episode:
    t_cmd: datetime
    comfort_at_cmd: float
    steps: float            # total °C of setpoint reduction (positive number)
    turn_at: datetime | None = None


class OnlineAdapter:
    def __init__(self, params: ModelParams) -> None:
        self.params = params
        self._ep: _Episode | None = None
        self._exc_side: str | None = None   # current band excursion: 'warm'/'cold'
        self._exc_peak: float = 0.0          # worst overshoot (°C) in this excursion

    def on_setpoint_command(self, now: datetime, delta_c: float, comfort: float | None) -> None:
        if comfort is None:
            return
        if delta_c < 0:  # cooling step
            if self._ep is None:
                self._ep = _Episode(t_cmd=now, comfort_at_cmd=comfort, steps=abs(delta_c))
            else:
                # escalation within the same episode → accumulate steps
                self._ep.steps += abs(delta_c)
        elif delta_c > 0:  # warming step confounds a cooling episode
            self._ep = None

    def cancel(self) -> None:
        """AC cut / mode change / managed-off — the episode is no longer clean."""
        self._ep = None

    def observe(self, now, comfort, slope, target=None, band_low=None, band_high=None) -> bool:
        """Advance learning one tick. Returns True if any model param changed."""
        changed = False
        if comfort is not None and target is not None and band_low is not None and band_high is not None:
            changed = self._track_excursion(comfort, target, band_low, band_high) or changed
        return self._advance_episode(now, comfort, slope) or changed

    def _track_excursion(self, comfort, target, band_low, band_high) -> bool:
        """Learn the anticipation lead from how far comfort overshoots the band.

        Every excursion beyond the band is scored: overshoot beyond tolerance →
        anticipate earlier (lead up, ∝ severity); overshoot within tolerance →
        relax (lead down) so we don't anticipate more than needed (which would
        add cycling). Converges to the least anticipation that keeps overshoot
        in check — the overshoot⇄cycling balance, and it tracks a drifting
        dead-time because recent excursions drive it.
        """
        hi, lo = target + band_high, target - band_low
        if comfort > hi:
            if self._exc_side != "warm":
                self._exc_side, self._exc_peak = "warm", 0.0
            self._exc_peak = max(self._exc_peak, comfort - hi)
            return False
        if comfort < lo:
            if self._exc_side != "cold":
                self._exc_side, self._exc_peak = "cold", 0.0
            self._exc_peak = max(self._exc_peak, lo - comfort)
            return False
        # back in band → close out the excursion and adjust the lead
        if self._exc_side is None:
            return False
        peak, self._exc_side, self._exc_peak = self._exc_peak, None, 0.0
        old_lead, old_spm = self.params.lead_min, self.params.sp_margin
        if peak > OVERSHOOT_TOL:
            # overshot → anticipate more AND tighten the setpoint deadband
            self.params.lead_min = min(LEAD_CAP, old_lead + LEAD_UP * min(2.0, peak / OVERSHOOT_TOL))
            self.params.sp_margin = max(0.0, old_spm - SP_MARGIN_DOWN)
        else:
            # stayed in check → relax anticipation AND widen the deadband (less cycling)
            self.params.lead_min = max(0.0, old_lead - LEAD_DOWN)
            self.params.sp_margin = min(SP_MARGIN_CAP, old_spm + SP_MARGIN_UP)
        self.params.lead_min = round(self.params.lead_min, 2)
        self.params.sp_margin = round(self.params.sp_margin, 3)
        return self.params.lead_min != old_lead or self.params.sp_margin != old_spm

    def _advance_episode(self, now: datetime, comfort: float | None, slope: float | None) -> bool:
        """Advance the active cooling episode (gain / dead-time). True if changed."""
        ep = self._ep
        if ep is None or comfort is None:
            return False

        if ep.turn_at is None and slope is not None and slope <= -SLOPE_EPS:
            ep.turn_at = now

        mature_after = self.params.dead_time_min + 2 * self.params.tau_min
        if (now - ep.t_cmd).total_seconds() / 60.0 < mature_after:
            return False

        # Episode matured — learn from it.
        changed = False
        realized_drop = ep.comfort_at_cmd - comfort         # >0 means it cooled
        if ep.steps > 0 and realized_drop > 0.05:
            realized_gain = realized_drop / ep.steps
            self.params.gain_per_step = _ema_clamp(
                self.params.gain_per_step, realized_gain, GAIN_MIN, GAIN_MAX)
            changed = True
        elif ep.steps > 0 and realized_drop <= 0.05:
            # Cooling barely moved the room (hot day / heavy load): shrink gain
            # so the controller becomes more aggressive next time.
            self.params.gain_per_step = _ema_clamp(
                self.params.gain_per_step, GAIN_MIN, GAIN_MIN, GAIN_MAX)
            changed = True

        if ep.turn_at is not None:
            realized_dead = (ep.turn_at - ep.t_cmd).total_seconds() / 60.0
            self.params.dead_time_min = _ema_clamp(
                self.params.dead_time_min, realized_dead, DEAD_MIN, DEAD_MAX)
            changed = True

        self._ep = None
        return changed


def _ema_clamp(old: float, new: float, lo: float, hi: float) -> float:
    val = (1 - ALPHA) * old + ALPHA * new
    return max(lo, min(hi, round(val, 3)))
