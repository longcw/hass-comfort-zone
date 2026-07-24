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
* **neither** power nor slope responded after the engagement window → the
  command didn't take → step down again immediately (25→24→23);
* engaged but the prediction still lands warm → step again after a
  dead-time-aware dwell.

Power is confounded (whole-system), so it only ever *shortens the wait* or
*grants patience*; the slope and the model decide the rest.

Actuator cost order: fan (cheap) → AC setpoint → AC blower → managed AC on/off.
The blower only escalates to its **middle** level in normal use; the top level
(高风) is reserved for the safety guard. When fan-assist is disabled the fan is
never used (and the caller passes a tighter band).
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
from .const import LEAD_CAP
from .model import FopdtPredictor

ANTICIP_CAP = 0.6         # °C — max shift the anticipation lead may apply
SLOPE_EPS = 0.02          # °C/min considered "flat"
FAN_HYST = 0.1            # °C hysteresis around target for fan on/off
FAN_SPAN = 2.0           # °C above target at which the fan reaches its cap
FF_NUDGE = 0.3           # °C the power feedforward shifts the effective temp
MIN_DWELL_FLOOR = 3.0    # minutes; hard floor between setpoint commands
BLOWER_DWELL = 3.0       # minutes; min interval between AC blower changes


@dataclass
class Signals:
    now: datetime
    comfort: float | None
    slope: float | None            # °C/min
    power: float | None            # W (whole-system)
    power_delta: float | None      # W change over ~power_lead window
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
        self._power_at_cmd: float | None = None
        self._last_cmd_cooling = False
        self._last_blower_at: datetime | None = None
        self._managed_off_since: datetime | None = None

    # -- helpers ------------------------------------------------------------
    def _step_dwell(self) -> float:
        return max(MIN_DWELL_FLOOR, self.predictor.params.dead_time_min * 0.5)

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
        self._power_at_cmd = s.power
        self._last_cmd_cooling = delta < 0

    def _cool_start_setpoint(self, p: ZoneParams) -> int:
        return max(p.setpoint_min, min(p.setpoint_max, round(p.target) - 1))

    def _manage_blower(self, cmd: Command, s: Signals, p: ZoneParams,
                       y: float, hi: float, falling: bool) -> None:
        """Track blower to cooling demand, one level per dwell.

        低风 (quiet, least draft) whenever comfort is at/below target; 中风 only
        when warm AND the setpoint is already floored (needs more cold air);
        left as-is in the warm-but-not-floored middle (no flapping). 高风 is
        reserved for the safety guard.
        """
        if not p.blower_levels or cmd.set_blower_idx is not None:
            return
        cur = s.blower_idx if s.blower_idx is not None else 0
        cooling_hard = y > hi and s.setpoint is not None and s.setpoint <= p.setpoint_min and not falling
        if cooling_hard:
            desired = p.regular_blower_max
        elif y <= p.target:
            desired = 0
        else:
            return  # warm but not floored → leave the blower where it is
        if desired == cur:
            return
        if self._last_blower_at is not None and \
                (s.now - self._last_blower_at).total_seconds() / 60.0 < BLOWER_DWELL:
            return
        cmd.set_blower_idx = cur + (1 if desired > cur else -1)
        self._last_blower_at = s.now
        note = f"blower→{p.blower_levels[cmd.set_blower_idx]}"
        cmd.reason = f"{cmd.reason}; {note}" if cmd.reason else note

    def _power_engaged(self, s: Signals) -> bool:
        """Has demand risen since our last cooling command? (fast, ~1–2 min)"""
        if not self._last_cmd_cooling or s.power is None or self._power_at_cmd is None:
            return False
        return (s.power - self._power_at_cmd) >= self.predictor.params.engage_watts

    # -- main tick ----------------------------------------------------------
    def tick(self, s: Signals, p: ZoneParams) -> Command:
        if s.comfort is None:
            return Command(mode=MODE_FAILSAFE, reason="no comfort reading")

        m = self.predictor.params
        hi = p.target + p.band_high
        lo = p.target - p.band_low
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
        cooling_incoming = s.power_delta is not None and s.power_delta > m.engage_watts
        warming_incoming = s.power_delta is not None and s.power_delta < -m.engage_watts
        mins = self._mins_since_cmd(s.now)
        engaged = falling or self._power_engaged(s)

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

        # === 2. WARM ========================================================
        if settled > hi or y > hi or y_ahead > hi:
            recent_cool = self._last_cmd_cooling and mins <= (m.dead_time_min + m.tau_min)
            not_engaged = recent_cool and not engaged

            if not s.ac_on:
                cmd.set_ac_power = True
                self._command_setpoint(cmd, s, self._cool_start_setpoint(p))
                cmd.mode = MODE_COOLING
                cmd.reason = f"warm & AC off → power on, setpoint {cmd.set_setpoint}"
            elif falling:
                cmd.mode = MODE_COOLING
                cmd.reason = f"warm ({y:.2f}) but falling ({slope:+.3f}) → cooling winning, hold"
            elif not_engaged and mins >= m.engage_window_min and s.setpoint and s.setpoint > p.setpoint_min:
                # Neither power nor slope responded → the command didn't take.
                self._command_setpoint(cmd, s, s.setpoint - 1)
                dp = (s.power - self._power_at_cmd) if (s.power is not None and self._power_at_cmd is not None) else 0
                cmd.mode = MODE_COOLING
                cmd.reason = f"not engaged after {mins:.0f}m (Δpower {dp:+.0f}W, flat slope) → escalate to {cmd.set_setpoint}"
            elif settled <= hi and self.predictor.has_pending_cooling(s.now):
                cmd.mode = MODE_COOLING
                cmd.reason = (f"warm ({y:.2f}) but cooling in flight "
                              f"({self.predictor.remaining_effect(s.now):+.2f}°C) → hold")
            elif engaged and mins < self._step_dwell():
                cmd.mode = MODE_COOLING
                cmd.reason = f"warm ({y:.2f}), engaged, giving it time ({mins:.0f}/{self._step_dwell():.0f}m)"
            elif s.setpoint is not None and s.setpoint > p.setpoint_min:
                self._command_setpoint(cmd, s, s.setpoint - 1)
                cmd.mode = MODE_COOLING
                cmd.reason = f"warm ({y:.2f}) & not enough cooling in flight → setpoint {cmd.set_setpoint}"
            else:
                cmd.mode = MODE_COOLING
                cmd.reason = f"warm ({y:.2f}) at setpoint floor → holding (blower/fan carry)"

        # === 3. COLD ========================================================
        elif settled < lo or y < lo or y_ahead < lo:
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

        # === 4. In band ====================================================
        else:
            # Return-to-neutral: below target and still falling on a low (cooling)
            # setpoint → ease the setpoint up now instead of sitting parked on a
            # cooling setpoint and slowly drifting into an overcool.
            if (y <= p.target and falling and s.ac_on and s.setpoint is not None
                    and s.setpoint < p.setpoint_max and mins >= self._step_dwell()):
                self._command_setpoint(cmd, s, s.setpoint + 1)
                cmd.mode = MODE_EASING
                cmd.reason = f"in-band below target & falling → setpoint {cmd.set_setpoint} (return to neutral)"
            else:
                cmd.mode = MODE_IDLE
                cmd.reason = f"on target ({y:.2f} in [{lo:.2f},{hi:.2f}])"

        # note when the trigger was the anticipation lead, not the reading itself
        if lo <= y <= hi and lo <= settled <= hi and (y_ahead > hi or y_ahead < lo) \
                and cmd.mode in (MODE_COOLING, MODE_EASING):
            cmd.reason = f"anticipating ({y_ahead:.2f}) — {cmd.reason}"

        # === 5. AC blower + fan comfort layer ==============================
        self._manage_blower(cmd, s, p, y, hi, falling)
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
