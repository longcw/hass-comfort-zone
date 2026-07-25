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

Episodes confounded by a warming step, the AC being cut, a load swing that warmed
the room straight through the episode, or a step that was already sitting on the
setpoint **floor** (no cooling left to give — saturation, not a small gain) teach
nothing about the gain, so we only learn it from clean cause→effect.

The two balance knobs are learned from *different* signals, because they trade
against each other: the anticipation ``lead`` from band excursions, and the
setpoint deadband ``sp_margin`` from the observed rate of compressor moves. Driven
from the same signal they have no interior fixed point and both run to a limit.

Pure and unit-testable; it mutates the :class:`ModelParams` it is given.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

from .const import LEAD_CAP, SP_MARGIN_CAP
from .model import ModelParams

ALPHA = 0.3                 # EMA weight on the newest observation (fast)
GAIN_MIN, GAIN_MAX = 0.1, 2.0
DEAD_MIN, DEAD_MAX = 2.0, 25.0
SLOPE_EPS = 0.02

# Anticipation-lead learning: keep the worst band excursion within tolerance.
# The tolerance is DERIVED PER SIDE from the clearance between the band edge and
# the hard rail on that side, because that is what an excursion actually costs: a
# rail 0.2 °C under the band must be defended far harder than one 1 °C over it.
TOL_FRACTION = 0.75        # of the band→rail clearance
TOL_MIN, TOL_MAX = 0.10, 0.35
TOL_DEFAULT = 0.15         # when the rails aren't supplied
LEAD_UP = 0.6              # minutes added per over-tolerance excursion (×severity)
LEAD_DOWN = 0.5            # minutes relaxed per comfortably-inside excursion
RELAX_FRACTION = 0.5       # "comfortably inside" = peak below this × tolerance;
#                            in between, hold. Without a hold zone the lead has no
#                            fixed point — it random-walks to a limit either way.
DEEP_FACTOR = 2.0          # peak beyond this × tolerance is a comfort failure
MIN_EXC_MIN = 2.0          # an excursion shorter than this is a band-edge wobble, not
#                            an excursion. Scoring those let sensor noise (which is
#                            almost always shallow) relax the lead away to zero.

# Setpoint deadband — the cycling knob, so it is closed on the thing it controls:
# the observed rate of compressor moves. Driving it off overshoot (which also
# drives the lead) coupled the two learners and left the pair with no interior
# fixed point — they ran to opposite limits (lead → cap, margin → 0).
SP_MARGIN_STEP = 0.05      # °C per adjustment
CYCLE_WINDOW_MIN = 60.0    # rate measured over this trailing window
CYCLE_REVIEW_MIN = 15.0    # …and adjusted at most this often
CYCLE_HIGH_PER_H = 3.0     # above this many setpoint moves/h → widen the deadband
CYCLE_LOW_PER_H = 1.0      # below this → tighten it and buy comfort back


@dataclass
class _Episode:
    t_cmd: datetime
    comfort_at_cmd: float
    steps: float            # total °C of setpoint reduction (positive number)
    turn_at: datetime | None = None
    at_floor: bool = False  # the step landed on the setpoint floor (saturated)


class OnlineAdapter:
    def __init__(self, params: ModelParams) -> None:
        self.params = params
        self._ep: _Episode | None = None
        self._exc_side: str | None = None   # current band excursion: 'warm'/'cold'
        self._exc_peak: float = 0.0          # worst overshoot (°C) in this excursion
        self._exc_saturated: bool = False    # AC was at its floor during it
        self._exc_since: datetime | None = None   # when this excursion started
        self._cmds: deque[datetime] = deque(maxlen=128)   # setpoint moves, for the rate
        self._observing_since: datetime | None = None
        self._margin_at: datetime | None = None

    def on_setpoint_command(self, now: datetime, delta_c: float, comfort: float | None,
                            *, at_floor: bool = False) -> None:
        if delta_c != 0:
            self._cmds.append(now)   # every compressor move counts toward the rate
        if comfort is None:
            return
        if delta_c < 0:  # cooling step
            if self._ep is None:
                self._ep = _Episode(t_cmd=now, comfort_at_cmd=comfort,
                                    steps=abs(delta_c), at_floor=at_floor)
            else:
                # escalation within the same episode → accumulate steps
                self._ep.steps += abs(delta_c)
                self._ep.at_floor = self._ep.at_floor or at_floor
        elif delta_c > 0:  # warming step confounds a cooling episode
            self._ep = None

    def cancel(self) -> None:
        """AC cut / mode change / managed-off — the episode is no longer clean."""
        self._ep = None

    def observe(self, now, comfort, slope, target=None, band_low=None, band_high=None,
                hard_min=None, hard_max=None, saturated=False) -> bool:
        """Advance learning one tick. Returns True if any model param changed.

        ``saturated`` means the compressor was already on its setpoint floor: the
        room is out of band because the AC has nothing left to give, so no amount
        of anticipation could have prevented it and the excursion must not be
        scored (that is what ratcheted the lead to its cap on a hot evening).
        """
        if self._observing_since is None:
            self._observing_since = now
        changed = False
        if comfort is not None and target is not None and band_low is not None and band_high is not None:
            changed = self._track_excursion(
                now, comfort, target, band_low, band_high, hard_min, hard_max, saturated) or changed
        changed = self._track_cycling(now) or changed
        return self._advance_episode(now, comfort, slope) or changed

    @staticmethod
    def _tolerance(edge: float, rail: float | None) -> float:
        """How far past a band edge we tolerate, given the rail behind it."""
        if rail is None:
            return TOL_DEFAULT
        return max(TOL_MIN, min(TOL_MAX, abs(edge - rail) * TOL_FRACTION))

    def _track_excursion(self, now, comfort, target, band_low, band_high,
                         hard_min=None, hard_max=None, saturated=False) -> bool:
        """Learn the anticipation lead from how far comfort leaves the band.

        Each excursion is scored against that side's tolerance: beyond it →
        anticipate earlier (lead up, ∝ severity); within it → relax (lead down),
        so we never anticipate more than needed (which only adds cycling). With a
        tolerance set where the excursion actually starts to cost something, both
        branches fire and the lead has a fixed point instead of ratcheting to its
        cap. A *deep* excursion is a comfort failure, so it also tightens the
        deadband — the one place the two knobs are allowed to interact.
        """
        hi, lo = target + band_high, target - band_low
        if comfort > hi:
            if self._exc_side != "warm":
                self._exc_side, self._exc_peak, self._exc_saturated = "warm", 0.0, False
                self._exc_since = now
            self._exc_peak = max(self._exc_peak, comfort - hi)
            self._exc_saturated = self._exc_saturated or saturated
            return False
        if comfort < lo:
            if self._exc_side != "cold":
                self._exc_side, self._exc_peak, self._exc_saturated = "cold", 0.0, False
                self._exc_since = now
            self._exc_peak = max(self._exc_peak, lo - comfort)
            self._exc_saturated = self._exc_saturated or saturated
            return False
        # back in band → close out the excursion and adjust the lead
        if self._exc_side is None:
            return False
        side, peak = self._exc_side, self._exc_peak
        was_saturated, since = self._exc_saturated, self._exc_since
        self._exc_side, self._exc_peak, self._exc_saturated = None, 0.0, False
        self._exc_since = None
        if was_saturated:
            return False   # unpreventable: the compressor had nothing left to give
        if since is not None and (now - since).total_seconds() / 60.0 < MIN_EXC_MIN:
            return False   # a wobble across the band edge, not an excursion
        tol = (self._tolerance(lo, hard_min) if side == "cold"
               else self._tolerance(hi, hard_max))
        old_lead, old_spm = self.params.lead_min, self.params.sp_margin
        if peak > tol:
            self.params.lead_min = min(LEAD_CAP, old_lead + LEAD_UP * min(2.0, peak / tol))
            if peak > DEEP_FACTOR * tol:
                self.params.sp_margin = max(0.0, old_spm - SP_MARGIN_STEP)
        elif peak < RELAX_FRACTION * tol:
            self.params.lead_min = max(0.0, old_lead - LEAD_DOWN)
        # else: inside tolerance but not comfortably so → hold, this is the target
        self.params.lead_min = round(self.params.lead_min, 2)
        self.params.sp_margin = round(self.params.sp_margin, 3)
        return self.params.lead_min != old_lead or self.params.sp_margin != old_spm

    def _track_cycling(self, now: datetime) -> bool:
        """Learn the setpoint deadband from the observed compressor-move rate.

        The deadband exists to cut compressor moves, so close the loop on the
        moves themselves: too many → widen it; almost none → tighten it and buy
        comfort back. That is a real feedback loop (widening reduces the rate that
        caused the widening), which is what the overshoot-driven version lacked.
        """
        cutoff = now - timedelta(minutes=CYCLE_WINDOW_MIN)
        while self._cmds and self._cmds[0] < cutoff:
            self._cmds.popleft()
        if (now - self._observing_since).total_seconds() / 60.0 < CYCLE_WINDOW_MIN:
            return False   # not enough history to call a rate
        if self._margin_at is not None and \
                (now - self._margin_at).total_seconds() / 60.0 < CYCLE_REVIEW_MIN:
            return False
        self._margin_at = now
        rate = len(self._cmds) / (CYCLE_WINDOW_MIN / 60.0)
        old = self.params.sp_margin
        if rate > CYCLE_HIGH_PER_H:
            self.params.sp_margin = round(min(SP_MARGIN_CAP, old + SP_MARGIN_STEP), 3)
        elif rate < CYCLE_LOW_PER_H:
            self.params.sp_margin = round(max(0.0, old - SP_MARGIN_STEP), 3)
        return self.params.sp_margin != old

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

        # Episode matured — learn from it, but only where it can teach us the gain.
        changed = False
        realized_drop = ep.comfort_at_cmd - comfort         # >0 means it cooled
        # Two episodes say nothing about the plant gain:
        #  * the setpoint was already on the floor and nothing moved → that is
        #    ACTUATOR SATURATION, not a small gain (learning it here is what
        #    dragged gain to GAIN_MIN on a hot evening and left the controller
        #    over-escalating once the load dropped at night);
        #  * the room actually WARMED through a cooling episode → the load moved,
        #    so the response is not attributable to our step.
        saturated = ep.at_floor and realized_drop <= 0.05
        load_moved = realized_drop < -0.1
        if ep.steps > 0 and not saturated and not load_moved:
            if realized_drop > 0.05:
                realized_gain = realized_drop / ep.steps
                self.params.gain_per_step = _ema_clamp(
                    self.params.gain_per_step, realized_gain, GAIN_MIN, GAIN_MAX)
            else:
                # Cooling genuinely barely moved the room while it still had room
                # to cool: shrink gain so the controller steps harder next time.
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
