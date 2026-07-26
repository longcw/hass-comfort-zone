"""The supervisor — a pure, testable multi-actuator control policy.

Given a snapshot of signals and resolved zone parameters, ``Controller.tick``
returns a :class:`Command` describing at most one AC action plus the desired fan
state. It holds a little state across ticks (last command, power at that command,
managed-off) but touches no Home Assistant APIs, so it is unit-testable.

**Engagement = power OR slope.** After a cooling step we ask "did it engage?"
using whole-system power *first* — it reacts in ~1–2 min, far faster than the
crib sensor's ~10-min thermal lag — and the comfort slope as ground truth:

* **falling** (slope turned down) → cooling is winning → HOLD;
* **power engaged** (demand rose since the step) and the model predicts the
  in-flight cooling lands in band → HOLD (be patient, don't stack);
* **power flat while RUNNING** → the command didn't take → step down again
  (25→24→23), fast, without waiting out the dead time;
* **power inconclusive** (the unit has not been seen running since the command —
  it duty-cycles, so an off-phase proves nothing) → wait a dead-time-aware window,
  because the slope cannot answer any sooner, then step;
* engaged but the prediction still lands warm → step again after a
  dead-time-aware dwell.

**Power is read as a RUNNING LEVEL, never as an instantaneous sample.** This unit
cycles (measured: ~18 min on, 2–5 min off), so comparing the sample at command time
with the sample now is a sampling artifact: an off-phase reads as "the command did
nothing", an on-phase as engagement. Only samples taken while it draws power count,
and a *negative* verdict additionally needs several minutes of observed running,
because the meter is whole-system and one room's step can be small.

Power is confounded (whole-system), so it never overrides the slope or the model
when they disagree; it makes the fast call when it has evidence.

Actuator cost order: circulation fan (cheapest) → AC blower → AC setpoint →
managed AC on/off — and each lever must actually get its turn *before* the next
one, which means the cheap ones work **inside** the band rather than waiting for an
out-of-band excursion. The fan is proportional; the blower is a two-level lever
(中风 above ``target + band_high × BLOWER_MID_FRAC``, 低风 at/below target), so it
engages before the compressor gate at the band top. 高风 is reserved for the safety
guard. When fan-assist is disabled the fan is never used (and the caller passes a
tighter band).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .const import (
    MODE_COOLING,
    MODE_EASING,
    MODE_FAILSAFE,
    MODE_FAN_ASSIST,
    MODE_IDLE,
    MODE_MANAGED_OFF,
)
from .const import LEAD_CAP, SP_MARGIN_CAP
from .model import FopdtPredictor

ANTICIP_CAP = 0.6         # °C — max shift the anticipation lead may apply
SLOPE_EPS = 0.02          # °C/min considered "flat"
FAN_HYST = 0.1            # °C hysteresis around target for fan on/off
FAN_SPAN = 2.0           # °C above target at which the fan reaches its cap
FF_NUDGE = 0.3           # °C the power feedforward shifts the effective temp
FF_TRIGGER_MULT = 2.0    # × engage_watts before the feedforward nudges the fan.
#                          engage_watts is calibrated for a STEP at a known moment; this
#                          signal is a 12-min mean of a duty-cycled load. Set from the
#                          measured separation, not from taste: with no command in the
#                          window |trend| sits at p90 ≈ 195 W, while real ramps measure
#                          +450…+650 W. Note the trigger does NOT damp chatter — the
#                          nudge changes direction ~18×/night at 150, 300 or 375 W alike
#                          (that is the estimator's job, see model.power_trend), so
#                          within the separating band prefer sensitivity: a missed ramp
#                          means the fan keeps blowing while cooling is arriving.
MIN_DWELL_FLOOR = 6.0    # minutes; hard floor between setpoint commands (pace the compressor)
BLOWER_DWELL = 3.0       # minutes; min interval between AC blower changes
BLOWER_MID_FRAC = 0.5    # of band_high above target → step the blower to its mid level
RAIL_KEEPOUT = 0.4       # °C of the band→rail clearance the deadband may never use
POWER_JUDGE_MIN = 3.0    # minutes of observed RUNNING before power may judge a command
#                          (the meter is whole-system, so one room's step can be small:
#                           demand more watching before trusting a NEGATIVE verdict)


def _rail_limited(margin: float, edge: float, rail: float | None) -> float:
    """Trim a deadband margin so it stops short of the safety rail behind it."""
    if rail is None:
        return margin
    return max(0.0, min(margin, abs(rail - edge) - RAIL_KEEPOUT))


@dataclass
class Signals:
    now: datetime
    comfort: float | None
    slope: float | None            # °C/min
    power: float | None            # W (whole-system)
    power_delta: float | None      # W change in window-MEAN power (see model.power_trend)
    ac_on: bool = False
    setpoint: int | None = None
    blower_idx: int | None = None  # index into blower_levels; None = auto/unknown
    fan_on: bool = False
    fan_level: int | None = None


@dataclass
class ZoneParams:
    target: float
    band_low: float                # °C below target before easing
    band_high: float               # °C above target before cooling (fan-resolved)
    setpoint_min: int
    setpoint_max: int
    blower_levels: list[str]
    fan_min_level: int
    fan_max_level: int
    managed_off_max_min: float
    fan_assist_enabled: bool = True
    hard_min: float | None = None   # safety rails, so the learned deadband
    hard_max: float | None = None   # can never widen into a guard trip
    setpoint_device_min: int = 16   # lowest setpoint the unit accepts (guard blast)

    @property
    def regular_blower_max(self) -> int:
        n = len(self.blower_levels)
        return max(0, n - 2) if n >= 2 else max(0, n - 1)


@dataclass
class Command:
    mode: str = MODE_IDLE
    reason: str = ""
    set_setpoint: int | None = None
    set_blower_idx: int | None = None
    set_ac_power: bool | None = None
    set_fan: bool | None = None
    set_fan_level: int | None = None


class Controller:
    def __init__(self, predictor: FopdtPredictor) -> None:
        self.predictor = predictor
        self._last_cmd_at: datetime | None = None
        self._last_cmd_cooling = False
        # Power bookkeeping: levels observed while the unit is RUNNING, never instants.
        self._power_run_level: float | None = None        # latest running level
        self._power_level_at_cmd: float | None = None     # running level when we commanded
        self._power_run_max: float | None = None          # best running level since then
        self._running_since_cmd = 0.0                     # minutes seen running since then
        self._last_tick_at: datetime | None = None
        self._last_blower_at: datetime | None = None
        self._managed_off_since: datetime | None = None

    # -- helpers ------------------------------------------------------------
    def _step_dwell(self) -> float:
        return max(MIN_DWELL_FLOOR, self.predictor.params.dead_time_min * 0.6)

    def _mins_since_cmd(self, now: datetime) -> float:
        if self._last_cmd_at is None:
            return 1e9
        return (now - self._last_cmd_at).total_seconds() / 60.0

    def _command_setpoint(self, cmd: Command, s: Signals, new_sp: int) -> None:
        if s.setpoint is not None and new_sp == s.setpoint:
            return
        delta = new_sp - (s.setpoint if s.setpoint is not None else new_sp)
        cmd.set_setpoint = new_sp
        self.predictor.record_setpoint_change(s.now, delta)
        self._last_cmd_at = s.now
        self._last_cmd_cooling = delta < 0
        # Baseline for engagement is the last level seen while RUNNING — not the
        # instantaneous reading, which may well be an off-phase.
        self._power_level_at_cmd = self._power_run_level
        self._power_run_max = None
        self._running_since_cmd = 0.0

    def _cool_start_setpoint(self, p: ZoneParams) -> int:
        return max(p.setpoint_min, min(p.setpoint_max, round(p.target) - 1))

    def _manage_blower(self, cmd: Command, s: Signals, p: ZoneParams,
                       y: float, falling: bool) -> None:
        """The AC blower is a TEMPERATURE LEVER, not an out-of-band reaction.

        It is the cheapest AC actuator — it modulates cold-air delivery without
        cycling the compressor — so it must act while comfort is still *inside* the
        band, ahead of the setpoint. With 高风 reserved for the safety guard there
        are only two usable grades, so it is a two-level lever with hysteresis:

        * **中风** once comfort is more than ``band_high × BLOWER_MID_FRAC`` above
          target and not already falling — reached before the setpoint gate at the
          band top, which is what keeps the cost order fan → blower → compressor real;
        * **低风** (quiet, least draft) at/below target;
        * held in between, so it cannot chatter around the threshold.
        """
        if not p.blower_levels or cmd.set_blower_idx is not None:
            return
        cur = s.blower_idx if s.blower_idx is not None else 0
        mid_on = p.target + p.band_high * BLOWER_MID_FRAC
        if y > mid_on and not falling:
            desired = p.regular_blower_max   # warming inside the band → more airflow
        elif y <= p.target:
            desired = 0
        else:
            return  # between target and the mid trigger → hold (hysteresis)
        if desired == cur:
            return
        if self._last_blower_at is not None and \
                (s.now - self._last_blower_at).total_seconds() / 60.0 < BLOWER_DWELL:
            return
        cmd.set_blower_idx = cur + (1 if desired > cur else -1)
        self._last_blower_at = s.now
        note = f"blower→{p.blower_levels[cmd.set_blower_idx]}"
        cmd.reason = f"{cmd.reason}; {note}" if cmd.reason else note

    def _observe_power(self, s: Signals) -> None:
        """Track the unit's RUNNING power level and how long it has been running.

        This VRF duty-cycles (measured: ~18 min on, 2–5 min off), which makes a
        comparison of two instantaneous samples worthless — an off-phase reads as
        "the command did nothing" and an on-phase as engagement. So only samples
        taken while the unit actually draws power count, and engagement is judged by
        comparing *running levels*. Power stays the fast clue it should be; it just
        stops being read through a sampling artifact.
        """
        dt = 0.0
        if self._last_tick_at is not None:
            dt = min((s.now - self._last_tick_at).total_seconds() / 60.0, 2.0)
        self._last_tick_at = s.now
        if s.power is None or s.power < self.predictor.params.engage_watts:
            return                      # off-phase or no signal: tells us nothing
        self._power_run_level = s.power
        if self._last_cmd_at is not None and s.now >= self._last_cmd_at:
            self._running_since_cmd += dt
            self._power_run_max = (s.power if self._power_run_max is None
                                   else max(self._power_run_max, s.power))

    def _power_rise(self) -> float | None:
        """Rise in running level since the last cooling command (None = unknown)."""
        if not self._last_cmd_cooling:
            return None
        if self._power_level_at_cmd is None or self._power_run_max is None:
            return None
        return self._power_run_max - self._power_level_at_cmd

    def _power_engaged(self) -> bool:
        """Has the unit's running level risen since our cooling command? (fast)"""
        rise = self._power_rise()
        return rise is not None and rise >= self.predictor.params.engage_watts

    def _power_says_unresponsive(self) -> bool:
        """Have we watched it RUN long enough to conclude the command did nothing?

        Requires observed running time — otherwise the verdict is *inconclusive*
        (an off-phase is not evidence), and the caller waits instead.
        """
        if self._running_since_cmd < POWER_JUDGE_MIN:
            return False
        rise = self._power_rise()
        return rise is not None and rise < self.predictor.params.engage_watts

    # -- main tick ----------------------------------------------------------
    def tick(self, s: Signals, p: ZoneParams) -> Command:
        self._observe_power(s)
        if s.comfort is None:
            return Command(mode=MODE_FAILSAFE, reason="no comfort reading")

        m = self.predictor.params
        hi = p.target + p.band_high
        lo = p.target - p.band_low
        # Cost-split: the fan works the comfort band [lo,hi]; the AC blower works
        # the mid-zone; the SETPOINT (compressor) only moves outside a WIDER
        # deadband [lo_sp, hi_sp] — sp_margin is learned. Fewer compressor moves.
        #
        # COMFORT FIRST on the warm side: the setpoint acts at the band edge, full
        # stop. Letting the learner widen this is what allowed the room to sit at
        # 27.25 with the rail at 27.5 — calm, and too warm to be worth it. The
        # deadband survives only as a COLD-side lever, where it buys something real
        # (fewer AC power cycles) and is still clamped off the rail.
        sp_margin = max(0.0, min(SP_MARGIN_CAP, m.sp_margin))
        hi_sp = hi
        lo_sp = lo - _rail_limited(sp_margin, lo, p.hard_min)
        y = s.comfort
        slope = s.slope if s.slope is not None else 0.0
        falling = slope <= -SLOPE_EPS
        rising = slope >= SLOPE_EPS

        settled = self.predictor.predict_settled(s.now, y)
        trend = self.predictor.predict_trend(y, s.slope, m.power_lead_min)
        # Anticipation: act on where the signal is HEADING, by a learned (bounded)
        # lead. y_ahead crosses the band before y does, so we start/ease earlier.
        lead = max(0.0, min(LEAD_CAP, m.lead_min))
        y_ahead = y + max(-ANTICIP_CAP, min(ANTICIP_CAP, slope * lead))
        ff_trigger = m.engage_watts * FF_TRIGGER_MULT
        cooling_incoming = s.power_delta is not None and s.power_delta > ff_trigger
        warming_incoming = s.power_delta is not None and s.power_delta < -ff_trigger
        mins = self._mins_since_cmd(s.now)
        engaged = falling or self._power_engaged()

        cmd = Command()

        # === 1. Managed-off: watch for the return ==========================
        if self._managed_off_since is not None:
            off_for = (s.now - self._managed_off_since).total_seconds() / 60.0
            if y >= p.target or off_for >= p.managed_off_max_min:
                self._managed_off_since = None
                cmd.set_ac_power = True
                self._command_setpoint(cmd, s, self._cool_start_setpoint(p))
                cmd.mode = MODE_COOLING
                cmd.reason = (f"managed-off return: comfort {y:.2f} ≥ target {p.target:.1f}"
                              if y >= p.target else f"managed-off watchdog after {off_for:.0f}m")
            else:
                cmd.mode = MODE_MANAGED_OFF
                cmd.reason = f"AC off, waiting to warm to {p.target:.1f} (now {y:.2f})"
            self._fan_layer(cmd, s, p, trend, cooling_incoming, warming_incoming)
            return cmd

        # === 2. WARM (setpoint acts only beyond the wider deadband) =========
        if settled > hi_sp or y > hi_sp or y_ahead > hi_sp:
            recent_cool = self._last_cmd_cooling and mins <= (m.dead_time_min + m.tau_min)
            not_engaged = recent_cool and not engaged
            # Power is the fast clue: once we have watched the unit RUN for a couple
            # of minutes at an unchanged level, the command demonstrably didn't take
            # and we escalate immediately. When power is inconclusive (the unit has
            # not been seen running yet — an off-phase proves nothing) we fall back to
            # a dead-time-paced window, because the slope cannot answer any sooner.
            power_dead = mins >= m.engage_window_min and self._power_says_unresponsive()
            escalate_after = max(m.engage_window_min, m.dead_time_min * 0.8)

            if not s.ac_on:
                cmd.set_ac_power = True
                self._command_setpoint(cmd, s, self._cool_start_setpoint(p))
                cmd.mode = MODE_COOLING
                cmd.reason = f"warm & AC off → power on, setpoint {cmd.set_setpoint}"
            elif falling:
                cmd.mode = MODE_COOLING
                cmd.reason = f"warm ({y:.2f}) but falling ({slope:+.3f}) → cooling winning, hold"
            elif not_engaged and (power_dead or mins >= escalate_after) \
                    and s.setpoint and s.setpoint > p.setpoint_min:
                # Neither power nor slope responded → the command didn't take.
                rise = self._power_rise()
                self._command_setpoint(cmd, s, s.setpoint - 1)
                why = (f"ran {self._running_since_cmd:.1f}m at an unchanged level "
                       f"({rise:+.0f}W)" if power_dead
                       else f"no power evidence in {mins:.0f}m, flat slope")
                cmd.mode = MODE_COOLING
                cmd.reason = f"not engaged: {why} → escalate to {cmd.set_setpoint}"
            elif settled <= hi and self.predictor.has_pending_cooling(s.now):
                cmd.mode = MODE_COOLING
                cmd.reason = (f"warm ({y:.2f}) but cooling in flight "
                              f"({self.predictor.in_flight_effect(s.now):+.2f}°C) → hold")
            elif engaged and mins < self._step_dwell():
                cmd.mode = MODE_COOLING
                cmd.reason = f"warm ({y:.2f}), engaged, giving it time ({mins:.0f}/{self._step_dwell():.0f}m)"
            elif mins < self._step_dwell():
                # MIN_DWELL_FLOOR is a floor between setpoint commands, not a
                # courtesy for engaged steps only: without this the branch below
                # could stack a step every tick while power had yet to respond.
                cmd.mode = MODE_COOLING
                cmd.reason = (f"warm ({y:.2f}) → pacing the compressor "
                              f"({mins:.1f}/{self._step_dwell():.0f}m since last step)")
            elif s.setpoint is not None and s.setpoint > p.setpoint_min:
                self._command_setpoint(cmd, s, s.setpoint - 1)
                cmd.mode = MODE_COOLING
                cmd.reason = f"warm ({y:.2f}) & not enough cooling in flight → setpoint {cmd.set_setpoint}"
            else:
                cmd.mode = MODE_COOLING
                cmd.reason = f"warm ({y:.2f}) at setpoint floor → holding (blower/fan carry)"

        # === 3. COLD (setpoint acts only beyond the wider deadband) =========
        elif settled < lo_sp or y < lo_sp or y_ahead < lo_sp:
            if rising:
                cmd.mode = MODE_EASING
                cmd.reason = f"cold ({y:.2f}) but rising ({slope:+.3f}) → warming back, hold"
            elif s.fan_on and p.fan_assist_enabled:
                cmd.mode = MODE_EASING
                cmd.reason = f"cold ({y:.2f}) → ease fan first"
            elif s.ac_on and s.setpoint is not None and s.setpoint < p.setpoint_max and mins >= self._step_dwell():
                self._command_setpoint(cmd, s, s.setpoint + 1)
                cmd.mode = MODE_EASING
                cmd.reason = f"cold ({y:.2f}) → setpoint {cmd.set_setpoint}"
            elif s.ac_on and s.setpoint is not None and s.setpoint >= p.setpoint_max and settled < lo:
                cmd.set_ac_power = False
                self._managed_off_since = s.now
                cmd.mode = MODE_MANAGED_OFF
                cmd.reason = "overcooling at setpoint ceiling → managed AC-off (will auto-return)"
            else:
                cmd.mode = MODE_EASING
                cmd.reason = f"cold ({y:.2f}) → hold"

        # === 4. In the deadband — fan/blower only, no setpoint churn ========
        else:
            # Return-to-neutral, PACED and CAPPED: if we're below target and still
            # falling on a setpoint that's below neutral (round target), ease it up
            # one step toward neutral — undoes a deep cooling escalation without
            # ping-ponging (it never raises above neutral, and it's slow-paced).
            neutral = round(p.target)
            if (y < p.target and falling and s.ac_on and s.setpoint is not None
                    and s.setpoint < neutral and mins >= self._step_dwell() * 1.5):
                self._command_setpoint(cmd, s, s.setpoint + 1)
                cmd.mode = MODE_EASING
                cmd.reason = f"below target & falling on a low setpoint → ease to {cmd.set_setpoint} (toward neutral {neutral})"
            else:
                cmd.mode = MODE_IDLE
                cmd.reason = f"in deadband ({y:.2f}, setpoint band [{lo_sp:.2f},{hi_sp:.2f}]) — fan/blower only"

        # note when the trigger was the anticipation lead, not the reading itself
        if lo_sp <= y <= hi_sp and lo_sp <= settled <= hi_sp and (y_ahead > hi_sp or y_ahead < lo_sp) \
                and cmd.mode in (MODE_COOLING, MODE_EASING):
            cmd.reason = f"anticipating ({y_ahead:.2f}) — {cmd.reason}"

        # === 5. AC blower (mid-zone lever) + fan comfort layer ==============
        self._manage_blower(cmd, s, p, y, falling)
        self._fan_layer(cmd, s, p, trend, cooling_incoming, warming_incoming)
        if cmd.mode == MODE_IDLE and (cmd.set_fan or cmd.set_fan_level is not None):
            cmd.mode = MODE_FAN_ASSIST
        return cmd

    def _fan_layer(self, cmd, s, p, trend, cooling_incoming, warming_incoming) -> None:
        # Fan-assist disabled → never run the circulation fan.
        if not p.fan_assist_enabled:
            if s.fan_on:
                cmd.set_fan = False
            return

        effective = trend
        if cooling_incoming:
            effective -= FF_NUDGE
        if warming_incoming:
            effective += FF_NUDGE

        want_on = effective > p.target + FAN_HYST
        if s.fan_on and effective < p.target - FAN_HYST:
            want_on = False

        if not want_on:
            if s.fan_on:
                cmd.set_fan = False
            return

        frac = max(0.0, min(1.0, (effective - p.target) / FAN_SPAN))
        level = int(round(p.fan_min_level + frac * (p.fan_max_level - p.fan_min_level)))
        level = max(p.fan_min_level, min(p.fan_max_level, level))
        if not s.fan_on:
            cmd.set_fan = True
        if s.fan_level is None or abs((s.fan_level or 0) - level) >= 3:
            cmd.set_fan_level = level
