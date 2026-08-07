"""Always-on hard safety guard.

This runs *after* the optimizer every tick and can override it. It is deliberately
dumb and absolute: it does not optimize comfort, it only keeps the room out of
dangerous territory and never gets stuck.

Lessons from the prior 安全阈值 that oscillated are baked in:

* trips on **wide absolute rails** (``hard_min`` / ``hard_max``), not on the
  narrow control band, so it stays a rare rail-safety and lets the optimizer do
  the normal holding;
* **release is decided by the reading, never by a timer**: the moment the room is
  back inside the rail (by ``RELEASE_HYST``) the optimizer gets the room back.
  The only time gate is a short anti-short-cycle hold on the cold side, where we
  actually cut power — and it is spelled out in the reason string;
* a **cooldown** prevents re-tripping right after handing back — on the *cold*
  side only, and only for shallow dips (``RETRIP_MARGIN``). Heat is the dangerous
  direction, so overheat always trips the instant the rail is crossed;
* overheat cools at the setpoint **floor** with the blower/fan maxed (no −3
  overshoot blast);
* every override uses the **reliable power switch**, never HVAC-mode;
* a **stale/unavailable sensor** stops optimization and parks the AC at a safe
  fixed setpoint — we never act on stale data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from .const import (
    MODE_FAILSAFE,
    MODE_SAFETY_OVERCOOL,
    MODE_SAFETY_OVERHEAT,
    MODE_STALE_HOLD,
    RAIL_BAND_CLEARANCE,
)
from .controller import Command, Signals, ZoneParams

RELEASE_HYST = 0.3        # °C back inside the rail before releasing
OVERHEAT_UNDERSHOOT = 2   # °C the guard goes BELOW the optimizer's setpoint floor
MIN_OFF_MIN = 3.0         # min minutes the overcool guard keeps power off (compressor)
RETRIP_MARGIN = 0.3       # °C past hard_min needed to re-trip overcool during the cooldown
FAILSAFE_SETPOINT = 26    # parked setpoint when the sensor is truly gone

STATE_NORMAL = "normal"
STATE_OVERHEAT = "overheat"
STATE_OVERCOOL = "overcool"
STATE_STALE = "stale"
STATE_FAILSAFE = "failsafe"


def effective_rails(target: float, band_low: float, band_high: float,
                    hard_min: float, hard_max: float) -> tuple[float, float]:
    """Push the rails out until they clear the band they sit behind.

    A rail inside the band's own tracking ripple is not a backstop — it is a second
    controller, and a cruder one, because its only move is to cut the compressor.
    Measured 08-06: hard_min 25.2 against a band floor of 25.4 cut AC power 12 times
    in 4.5 h on dips of 0.1–0.45 °C, and each cut cost a full setpoint walk back down.
    So a configured rail is honoured only where it is already outside the band by
    RAIL_BAND_CLEARANCE; nearer than that, the rail moves, never the band, because
    widening the band would silently change the temperature the room is held at.

    Returns the rails to use for both the guard and the controller's deadband
    trimming, which have to agree or the deadband opens into a trip.
    """
    return (min(hard_min, target - band_low - RAIL_BAND_CLEARANCE),
            max(hard_max, target + band_high + RAIL_BAND_CLEARANCE))


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
        self._overcool_released_at: datetime | None = None

    def _enter(self, state: str, now: datetime) -> None:
        if state != self.state:
            self.state = state
            self._since = now

    def _mins_in_state(self, now: datetime) -> float:
        if self._since is None:
            return 1e9
        return (now - self._since).total_seconds() / 60.0

    def _in_overcool_cooldown(self, now: datetime, cooldown_min: float) -> bool:
        """Did we hand back from an overcool trip very recently?"""
        if self._overcool_released_at is None:
            return False
        return (now - self._overcool_released_at).total_seconds() / 60.0 < cooldown_min

    def evaluate(
        self,
        s: Signals,
        p: ZoneParams,
        sp: SafetyParams,
        opt_cmd: Command,
        *,
        stale: bool = False,
    ) -> Command:
        """Return the command to actually apply (opt_cmd unless overridden).

        Two distinct sensor problems, handled very differently:

        * **No value** (``comfort is None`` — the source is unavailable/unknown):
          the sensor is genuinely gone, so park the AC at a safe fixed setpoint.
        * **A value, but no fresh report for a while** (``stale``): the room was
          probably fine and the sensor just went quiet (common with BLE). Do NOT
          disrupt a working room — simply HOLD (actuate nothing) until it
          reports again.
        """
        now = s.now

        # --- truly gone: no value at all → park safely ---------------------
        if s.comfort is None:
            self._enter(STATE_FAILSAFE, now)
            park = int(math.floor(p.target - p.band_low))
            park = max(p.setpoint_min, min(p.setpoint_max, park))
            return Command(
                mode=MODE_FAILSAFE,
                reason=f"sensor unavailable → park AC at floor(target−band_low)={park}, fan off",
                set_setpoint=park,
                set_ac_power=None,   # leave power as-is; just fix the setpoint
                set_fan=False,
            )

        # --- has a value but stale: freeze, change nothing -----------------
        if stale:
            self._enter(STATE_STALE, now)
            return Command(
                mode=MODE_STALE_HOLD,
                reason=f"no fresh reading for a while (last {s.comfort:.2f}) → hold, no changes",
            )

        y = s.comfort

        # --- release logic (leave a protect state) -------------------------
        # The reading decides, not a timer: as soon as the room is back inside the
        # rail we hand the room back. Holding a protect state past that point is
        # how a guard "gets stuck" — and on the hot side it force-cools straight
        # through the band into an overcool.
        if self.state == STATE_OVERHEAT:
            if y <= sp.hard_max - RELEASE_HYST:
                self._enter(STATE_NORMAL, now)
            else:
                return self._overheat_cmd(s, p, y, sp)
        elif self.state == STATE_OVERCOOL:
            off_for = self._mins_in_state(now)
            # the one time gate: we cut the power, so don't restart the
            # compressor seconds later (and don't flap on sensor noise).
            if y >= sp.hard_min + RELEASE_HYST and off_for >= MIN_OFF_MIN:
                self._enter(STATE_NORMAL, now)
                self._overcool_released_at = now
                # hand back with the AC powered on so the optimizer can resume
                if not s.ac_on and opt_cmd.set_ac_power is None:
                    opt_cmd.set_ac_power = True
            else:
                return self._overcool_cmd(s, y, sp, off_for)
        elif self.state in (STATE_FAILSAFE, STATE_STALE):
            self._enter(STATE_NORMAL, now)  # sensor came back / reports again

        # --- trip logic (enter a protect state) ----------------------------
        # Heat trips on the rail, always, with no grace period.
        if y > sp.hard_max:
            self._enter(STATE_OVERHEAT, now)
            return self._overheat_cmd(s, p, y, sp)
        # Cold: right after handing back, a shallow dip is not worth cutting the
        # power again for (that flapping is what cooldown_min exists to stop);
        # anything deeper than RETRIP_MARGIN still trips normally.
        floor = sp.hard_min
        if self._in_overcool_cooldown(now, sp.cooldown_min):
            floor -= RETRIP_MARGIN
        if y < floor:
            self._enter(STATE_OVERCOOL, now)
            return self._overcool_cmd(s, y, sp, 0.0)

        # normal — the optimizer is in charge
        return opt_cmd

    def _overheat_cmd(self, s: Signals, p: ZoneParams, y: float, sp: SafetyParams) -> Command:
        top_blower = len(p.blower_levels) - 1 if p.blower_levels else None
        release = sp.hard_max - RELEASE_HYST
        # The rail guard is not the optimizer, so it is not bound by the optimizer's
        # setpoint floor: at the hot rail it goes OVERHEAT_UNDERSHOOT below it to pull
        # the room back, clamped to what the unit will accept.
        blast = max(p.setpoint_device_min, p.setpoint_min - OVERHEAT_UNDERSHOOT)
        # …and no more bound by a quiet cap than by that floor: the blower goes to the
        # top of the ladder and the fan to the level the zone keeps for the guard.
        guard_fan = p.fan_guard_level
        reason = (
            f"OVERHEAT guard: comfort {y:.2f} > hard_max {sp.hard_max:.1f} → full cool at {blast}"
            if y > sp.hard_max
            else f"OVERHEAT guard: full cool at {blast}, holding until comfort ≤ {release:.1f} (now {y:.2f})"
        )
        return Command(
            mode=MODE_SAFETY_OVERHEAT,
            reason=reason,
            set_ac_power=True,
            set_setpoint=blast,
            set_blower_idx=top_blower,
            set_fan=True if guard_fan > 0 else None,
            set_fan_level=guard_fan if guard_fan > 0 else None,
        )

    def _overcool_cmd(self, s: Signals, y: float, sp: SafetyParams,
                      off_for: float) -> Command:
        release = sp.hard_min + RELEASE_HYST
        if y < sp.hard_min:
            reason = f"OVERCOOL guard: comfort {y:.2f} < hard_min {sp.hard_min:.1f} → AC off"
        elif y >= release:
            # the reading already says release — say what we are actually waiting on
            reason = (f"OVERCOOL guard: comfort {y:.2f} back above {release:.1f}, "
                      f"AC off {off_for:.1f}/{MIN_OFF_MIN:.0f} min (compressor protection) "
                      f"→ handing back next")
        else:
            reason = f"OVERCOOL guard: AC off, holding until comfort ≥ {release:.1f} (now {y:.2f})"
        return Command(
            mode=MODE_SAFETY_OVERCOOL,
            reason=reason,
            set_ac_power=False,
            set_fan=False,
        )
