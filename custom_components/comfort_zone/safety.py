"""Always-on hard safety guard.

This runs *after* the optimizer every tick and can override it. It is deliberately
dumb and absolute: it does not optimize comfort, it only keeps the room out of
dangerous territory and never gets stuck.

Lessons from the prior 安全阈值 that oscillated are baked in:

* trips on **wide absolute rails** (``hard_min`` / ``hard_max``), not on the
  narrow control band, so it stays a rare rail-safety and lets the optimizer do
  the normal holding;
* a **cooldown** prevents re-tripping right after handing back;
* overheat cools at the setpoint **floor** with the blower/fan maxed (no −3
  overshoot blast);
* every override uses the **reliable power switch**, never HVAC-mode;
* a **stale/unavailable sensor** stops optimization and parks the AC at a safe
  fixed setpoint — we never act on stale data.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .const import MODE_FAILSAFE, MODE_SAFETY_OVERCOOL, MODE_SAFETY_OVERHEAT
from .controller import Command, Signals, ZoneParams

RELEASE_HYST = 0.3        # °C back inside the rail before releasing
FAILSAFE_SETPOINT = 26    # parked setpoint when the sensor is unusable

STATE_NORMAL = "normal"
STATE_OVERHEAT = "overheat"
STATE_OVERCOOL = "overcool"
STATE_FAILSAFE = "failsafe"


@dataclass
class SafetyParams:
    hard_min: float
    hard_max: float
    cooldown_min: float
    stale_after_s: float = 600.0   # sensor considered stale after this


class SafetyGuard:
    def __init__(self) -> None:
        self.state: str = STATE_NORMAL
        self._since: datetime | None = None

    def _enter(self, state: str, now: datetime) -> None:
        if state != self.state:
            self.state = state
            self._since = now

    def _cooldown_ok(self, now: datetime, cooldown_min: float) -> bool:
        if self._since is None:
            return True
        return (now - self._since).total_seconds() / 60.0 >= cooldown_min

    def evaluate(
        self,
        s: Signals,
        p: ZoneParams,
        sp: SafetyParams,
        opt_cmd: Command,
        *,
        stale: bool = False,
    ) -> Command:
        """Return the command to actually apply (opt_cmd unless overridden)."""
        now = s.now

        # --- stale / missing sensor: park safely, don't optimize -----------
        if stale or s.comfort is None:
            self._enter(STATE_FAILSAFE, now)
            return Command(
                mode=MODE_FAILSAFE,
                reason="sensor stale/unavailable → park AC, stop optimizing",
                set_setpoint=FAILSAFE_SETPOINT,
                set_ac_power=None,  # leave power as-is; just fix the setpoint
                set_fan=False,
            )

        y = s.comfort

        # --- release logic (leave a protect state) -------------------------
        if self.state == STATE_OVERHEAT:
            if y <= sp.hard_max - RELEASE_HYST and self._cooldown_ok(now, sp.cooldown_min):
                self._enter(STATE_NORMAL, now)
            else:
                return self._overheat_cmd(s, p, y, sp)
        elif self.state == STATE_OVERCOOL:
            if y >= sp.hard_min + RELEASE_HYST and self._cooldown_ok(now, sp.cooldown_min):
                self._enter(STATE_NORMAL, now)
                # hand back with the AC powered on so the optimizer can resume
                if not s.ac_on and opt_cmd.set_ac_power is None:
                    opt_cmd.set_ac_power = True
            else:
                return self._overcool_cmd(s, y, sp)
        elif self.state == STATE_FAILSAFE:
            self._enter(STATE_NORMAL, now)  # sensor came back

        # --- trip logic (enter a protect state) ----------------------------
        if y > sp.hard_max:
            self._enter(STATE_OVERHEAT, now)
            return self._overheat_cmd(s, p, y, sp)
        if y < sp.hard_min:
            self._enter(STATE_OVERCOOL, now)
            return self._overcool_cmd(s, y, sp)

        # normal — the optimizer is in charge
        return opt_cmd

    def _overheat_cmd(self, s: Signals, p: ZoneParams, y: float, sp: SafetyParams) -> Command:
        top_blower = len(p.blower_levels) - 1 if p.blower_levels else None
        return Command(
            mode=MODE_SAFETY_OVERHEAT,
            reason=f"OVERHEAT guard: comfort {y:.2f} > hard_max {sp.hard_max:.1f} → full cool",
            set_ac_power=True,
            set_setpoint=p.setpoint_min,
            set_blower_idx=top_blower,
            set_fan=True,
            set_fan_level=p.fan_max_level,
        )

    def _overcool_cmd(self, s: Signals, y: float, sp: SafetyParams) -> Command:
        return Command(
            mode=MODE_SAFETY_OVERCOOL,
            reason=f"OVERCOOL guard: comfort {y:.2f} < hard_min {sp.hard_min:.1f} → AC off",
            set_ac_power=False,
            set_fan=False,
        )
