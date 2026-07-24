"""The supervisor — a pure, testable multi-actuator control policy.

Given a snapshot of signals and resolved zone parameters, ``Controller.tick``
returns a :class:`Command` describing at most one AC action plus the desired fan
state. It holds a little state across ticks (engagement watch, managed-off,
last-action time) but touches no Home Assistant APIs, so it can be driven
tick-by-tick in unit tests.

Anti-churn is structural, not a timer:

* **Engagement-gated escalation** — after a cooling step we watch power+slope
  for ``engage_window`` minutes. If the unit didn't engage, we step down again
  *immediately* (25→24→23) instead of waiting out the thermal lag. If it did,
  we hand off to the predictor.
* **Smith-predictor patience** — once cooling is engaged, ``predict_settled``
  already accounts for the cooling in flight, so we only issue another step if
  the room is predicted to *stay* out of band after the current step lands.

Actuator cost order: fan (cheap) → AC setpoint → AC blower → managed AC on/off.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .const import (
    MODE_COOLING,
    MODE_EASING,
    MODE_FAILSAFE,
    MODE_FAN_ASSIST,
    MODE_IDLE,
    MODE_MANAGED_OFF,
)
from .model import FopdtPredictor

# Hysteresis / tuning that isn't worth exposing yet.
SLOPE_EPS = 0.02          # °C/min considered "flat"
FAN_HYST = 0.1            # °C hysteresis around target for fan on/off
FAN_SPAN = 2.0           # °C above target at which the fan reaches its cap
FF_NUDGE = 0.3           # °C the power feedforward shifts the effective temp
MIN_DWELL_FLOOR = 3.0    # minutes; hard floor between setpoint commands


@dataclass
class Signals:
    """A snapshot of everything the controller reads this tick."""

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
    """Resolved (day/night applied) parameters for this tick."""

    target: float
    band: float
    setpoint_min: int
    setpoint_max: int
    blower_levels: list[str]       # ascending cooling intensity
    fan_min_level: int
    fan_max_level: int             # already resolved day vs night
    managed_off_max_min: float


@dataclass
class Command:
    """At-most-one AC action plus desired fan state. None = leave as-is."""

    mode: str = MODE_IDLE
    reason: str = ""
    set_setpoint: int | None = None
    set_blower_idx: int | None = None
    set_ac_power: bool | None = None   # managed on/off (via reliable power switch)
    set_fan: bool | None = None
    set_fan_level: int | None = None


class Controller:
    def __init__(self, predictor: FopdtPredictor) -> None:
        self.predictor = predictor
        self._last_setpoint_cmd_at: datetime | None = None
        # engagement watch after a cooling step
        self._watch_since: datetime | None = None
        self._watch_baseline_power: float | None = None
        # managed-off bookkeeping
        self._managed_off_since: datetime | None = None

    # -- helpers ------------------------------------------------------------
    def _dwell_ok(self, now: datetime, engage_window: float) -> bool:
        if self._last_setpoint_cmd_at is None:
            return True
        floor = max(MIN_DWELL_FLOOR, 0.0)
        elapsed = (now - self._last_setpoint_cmd_at).total_seconds() / 60.0
        return elapsed >= floor

    def _command_setpoint(self, cmd: Command, s: Signals, new_sp: int) -> None:
        """Emit a setpoint command and record the step for the predictor."""
        if s.setpoint is not None and new_sp == s.setpoint:
            return
        delta = new_sp - (s.setpoint if s.setpoint is not None else new_sp)
        cmd.set_setpoint = new_sp
        self.predictor.record_setpoint_change(s.now, delta)
        self._last_setpoint_cmd_at = s.now
        if delta < 0:  # a cooling step — arm engagement watch
            self._watch_since = s.now
            self._watch_baseline_power = s.power

    # -- main tick ----------------------------------------------------------
    def tick(self, s: Signals, p: ZoneParams) -> Command:
        if s.comfort is None:
            return Command(mode=MODE_FAILSAFE, reason="no comfort reading")

        m = self.predictor.params
        hi = p.target + p.band
        lo = p.target - p.band
        y = s.comfort
        slope = s.slope if s.slope is not None else 0.0

        settled = self.predictor.predict_settled(s.now, y)
        trend = self.predictor.predict_trend(y, s.slope, m.power_lead_min)

        cooling_incoming = s.power_delta is not None and s.power_delta > m.engage_watts
        warming_incoming = s.power_delta is not None and s.power_delta < -m.engage_watts

        cmd = Command()

        # === 1. Managed-off: we turned the AC off; watch for a return ======
        if self._managed_off_since is not None:
            off_for = (s.now - self._managed_off_since).total_seconds() / 60.0
            # Return if it has warmed back to/above target, or the watchdog fires.
            if y >= p.target or off_for >= p.managed_off_max_min:
                self._managed_off_since = None
                cmd.set_ac_power = True
                self._command_setpoint(cmd, s, self._cool_start_setpoint(p))
                cmd.mode = MODE_COOLING
                cmd.reason = (
                    f"managed-off return: comfort {y:.2f} ≥ target {p.target:.1f}"
                    if y >= p.target
                    else f"managed-off watchdog after {off_for:.0f}m"
                )
                self._fan_layer(cmd, s, p, trend, cooling_incoming, warming_incoming)
                return cmd
            cmd.mode = MODE_MANAGED_OFF
            cmd.reason = f"AC off, waiting to warm to {p.target:.1f} (now {y:.2f})"
            # fan may still run for comfort while the AC is off
            self._fan_layer(cmd, s, p, trend, cooling_incoming, warming_incoming)
            return cmd

        # === 2. Engagement watch after a cooling step ======================
        if self._watch_since is not None and s.ac_on:
            waited = (s.now - self._watch_since).total_seconds() / 60.0
            power_rose = (
                s.power is not None
                and self._watch_baseline_power is not None
                and (s.power - self._watch_baseline_power) >= m.engage_watts
            )
            engaged = power_rose or slope <= -SLOPE_EPS
            if engaged:
                self._watch_since = None  # hand off to Smith patience
            elif waited >= m.engage_window_min:
                # Command didn't take — escalate now, don't wait out the lag.
                if s.setpoint is not None and s.setpoint > p.setpoint_min:
                    self._command_setpoint(cmd, s, s.setpoint - 1)
                    cmd.mode = MODE_COOLING
                    cmd.reason = (
                        f"not engaged after {waited:.0f}m "
                        f"(Δpower≈{(s.power or 0) - (self._watch_baseline_power or 0):+.0f}W) "
                        f"→ escalate to {s.setpoint - 1}"
                    )
                    self._fan_layer(cmd, s, p, trend, cooling_incoming, warming_incoming)
                    return cmd
                # already at floor; stop watching, let blower/fan carry it
                self._watch_since = None

        # === 3. AC decision (at most one setpoint/blower/power action) =====
        if settled > hi:
            # Predicted to stay warm even counting cooling already in flight.
            if not s.ac_on:
                cmd.set_ac_power = True
                self._command_setpoint(cmd, s, self._cool_start_setpoint(p))
                cmd.mode = MODE_COOLING
                cmd.reason = f"warm & AC off → power on, setpoint {cmd.set_setpoint}"
            elif self.predictor.has_pending_cooling(s.now):
                # Cooling is engaged and still arriving — be patient.
                cmd.mode = MODE_COOLING
                cmd.reason = (
                    f"warm (settled {settled:.2f} > {hi:.2f}) but cooling in flight "
                    f"({self.predictor.remaining_effect(s.now):+.2f}°C) → hold"
                )
            elif self._dwell_ok(s.now, m.engage_window_min) and (
                s.setpoint is not None and s.setpoint > p.setpoint_min
            ):
                self._command_setpoint(cmd, s, s.setpoint - 1)
                cmd.mode = MODE_COOLING
                cmd.reason = f"warm (settled {settled:.2f} > {hi:.2f}) → setpoint {cmd.set_setpoint}"
            elif s.setpoint is not None and s.setpoint <= p.setpoint_min:
                # At the floor and still warm → lean on the blower.
                raised = self._raise_blower(cmd, s, p)
                cmd.mode = MODE_COOLING
                cmd.reason = (
                    f"warm at setpoint floor → blower {p.blower_levels[cmd.set_blower_idx]}"
                    if raised
                    else "warm at floor, blower maxed → fan only"
                )
        elif settled < lo:
            # Predicted to stay cold — ease off, cheapest actuator first.
            if s.fan_on:
                # handled by fan layer below (fan down/off is the first move)
                cmd.mode = MODE_EASING
                cmd.reason = f"cold (settled {settled:.2f} < {lo:.2f}) → ease fan first"
            elif s.ac_on and s.setpoint is not None and s.setpoint < p.setpoint_max:
                self._command_setpoint(cmd, s, s.setpoint + 1)
                self._lower_blower(cmd, s, p)
                cmd.mode = MODE_EASING
                cmd.reason = f"cold (settled {settled:.2f} < {lo:.2f}) → setpoint {cmd.set_setpoint}, quieter"
            elif s.ac_on and (settled < lo - 0.3 or slope < -SLOPE_EPS):
                # At the ceiling and still overcooling → managed off.
                cmd.set_ac_power = False
                self._managed_off_since = s.now
                cmd.mode = MODE_MANAGED_OFF
                cmd.reason = f"overcooling at setpoint ceiling → managed AC-off (will auto-return)"
            else:
                cmd.mode = MODE_EASING
                cmd.reason = f"cold (settled {settled:.2f} < {lo:.2f}) → hold"
        else:
            cmd.mode = MODE_IDLE
            cmd.reason = f"in band (settled {settled:.2f} in [{lo:.2f},{hi:.2f}])"

        # === 4. Fan comfort layer (always, unless we already set fan) ======
        self._fan_layer(cmd, s, p, trend, cooling_incoming, warming_incoming)
        if cmd.mode == MODE_IDLE and (cmd.set_fan or cmd.set_fan_level is not None):
            cmd.mode = MODE_FAN_ASSIST
        return cmd

    # -- sub-policies -------------------------------------------------------
    def _cool_start_setpoint(self, p: ZoneParams) -> int:
        """Cold-start / return setpoint: one below target, engagement escalates lower."""
        return max(p.setpoint_min, min(p.setpoint_max, round(p.target) - 1))

    def _raise_blower(self, cmd: Command, s: Signals, p: ZoneParams) -> bool:
        n = len(p.blower_levels)
        if n == 0:
            return False
        cur = s.blower_idx if s.blower_idx is not None else 0
        if cur >= n - 1:
            return False
        cmd.set_blower_idx = cur + 1
        return True

    def _lower_blower(self, cmd: Command, s: Signals, p: ZoneParams) -> bool:
        if not p.blower_levels:
            return False
        cur = s.blower_idx if s.blower_idx is not None else 0
        if cur <= 0:
            return False
        cmd.set_blower_idx = cur - 1
        return True

    def _fan_layer(
        self,
        cmd: Command,
        s: Signals,
        p: ZoneParams,
        trend: float,
        cooling_incoming: bool,
        warming_incoming: bool,
    ) -> None:
        """Parallel comfort actuator: air movement when warm, off when cool.

        Uses the power feedforward to preempt the lagging sensor — ease the fan
        before cold air arrives, raise it before warming shows up.
        """
        effective = trend
        if cooling_incoming:
            effective -= FF_NUDGE   # cool is coming → don't add draft
        if warming_incoming:
            effective += FF_NUDGE   # warming coming → get ahead of it

        want_on = effective > p.target + FAN_HYST
        if s.fan_on and effective < p.target - FAN_HYST:
            want_on = False

        if not want_on:
            if s.fan_on:
                cmd.set_fan = False
            return

        # scale level from floor (at target) to cap (at target + FAN_SPAN)
        frac = max(0.0, min(1.0, (effective - p.target) / FAN_SPAN))
        level = int(round(p.fan_min_level + frac * (p.fan_max_level - p.fan_min_level)))
        level = max(p.fan_min_level, min(p.fan_max_level, level))
        if not s.fan_on:
            cmd.set_fan = True
        if s.fan_level is None or abs((s.fan_level or 0) - level) >= 3:
            cmd.set_fan_level = level
