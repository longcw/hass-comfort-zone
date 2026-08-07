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
* power is **cut** with the reliable power switch, never with an HVAC-mode of "off"
  — this VRF acknowledges that mode without acting on it. Putting the unit **back**
  is not one command but a state the guard stays in until the unit reports itself
  running (``STATE_RESTORING``): it asserts power, mode and a safe setpoint, and
  asserts them again every tick until the unit agrees. One attempt is not a restore
  on this proxy — on 08-07 10:48 a power-on issued with an immediate setpoint write
  left the unit reporting off, with the setpoint applied, for the seven minutes until
  a person started it by hand. **Nothing else can finish that job**, because the
  controller may never switch an AC on, so the guard may not hand back until it has;
* a **stale/unavailable sensor** stops optimization and parks the AC at a safe
  fixed setpoint — we never act on stale data — and releases any override in force
  rather than leaving it latched on hardware nothing can then revise.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .const import (
    MODE_FAILSAFE,
    MODE_SAFETY_OVERCOOL,
    MODE_SAFETY_OVERHEAT,
    MODE_STALE_HOLD,
    RAIL_RIPPLE,
)
from .controller import Command, Signals, ZoneParams

# A rail catches an extreme temperature. This catches an extreme *duration*: the
# room sitting below the band for this long means the controller is not working,
# whatever the reading is, and the guard exists precisely for when the controller is
# the thing that is wrong. Measured 08-07 — the room spent fourteen minutes falling
# from 25.85 to 25.04, never came near the 23.0 rail, and nothing intervened.
#
# COLD SIDE ONLY, deliberately. The cold side is where a guard can act decisively
# (stop cooling) and where the danger lies for a sleeping child. Sitting warm for
# ten minutes is ordinary on a hot afternoon with the setpoint already at its floor,
# and the guard has no move there that the controller is not already making.
SUSTAINED_COLD_MIN = 10.0
# …and only once the room is this far below the band, not a hair past it. Duration
# alone reproduces the 08-06 compressor chatter: an
# ordinary excursion easily spends ten minutes a hundredth of a degree low, and
# cutting the compressor for that is the 08-06 chatter with a timer bolted on.
SUSTAINED_COLD_DEPTH = 0.25
RELEASE_HYST = 0.3        # °C back inside the rail before releasing
OVERHEAT_UNDERSHOOT = 2   # °C the guard goes BELOW the optimizer's setpoint floor
MIN_OFF_MIN = 3.0         # min minutes the overcool guard keeps power off (compressor)
RETRIP_MARGIN = 0.3       # °C past hard_min needed to re-trip overcool during the cooldown
FAILSAFE_SETPOINT = 26    # parked setpoint when the sensor is truly gone
# The mode to put a unit back into when nothing better is remembered. This is a
# cooling controller; a room whose mode was never observed still needs one.
DEFAULT_HVAC_MODE = "cool"
# How long the guard keeps re-asserting a restore before it gives up and says so.
# Long enough for a cloud proxy to answer (a write is dropped often enough here that
# one attempt is not a restore), short enough that it cannot argue for long with
# somebody switching the unit off by hand while it is trying.
RESTORE_TIMEOUT_MIN = 3.0

# How long a merely-quiet sensor may hold the room before we stop trusting the
# last reading altogether and park. Holding is right for a BLE gap; holding
# indefinitely is an open-loop AC with no rails, on the commonest hardware failure
# this system has — a dead thermometer battery, which most integrations report as
# a stale value rather than as unavailable.
STALE_PARK_AFTER_MIN = 30.0

STATE_NORMAL = "normal"
STATE_OVERHEAT = "overheat"
STATE_OVERCOOL = "overcool"
STATE_STALE = "stale"
STATE_FAILSAFE = "failsafe"
# Power is back on but the unit has not started yet. A separate state because the
# room is not the controller's again until the unit is actually running, and because
# handing back to a controller that is forbidden to power the AC on means nothing
# will ever finish the job — which is exactly what happened on 08-07 10:48.
STATE_RESTORING = "restoring"


def rails(target: float, band_low: float, band_high: float,
          hard_min: float, hard_max: float) -> tuple[float, float]:
    """The configured limits, VERBATIM. They are the user's, not ours to adjust.

    This used to widen a rail that sat close to the band, on the grounds that a rail
    inside the band's own tracking ripple stops being a backstop and becomes a
    second, cruder controller — measured 08-06, a cold rail 0.2 °C under the band
    floor cut AC power 12 times in 4.5 h.

    That reasoning was sound and the remedy was not. A hard limit that the software
    silently relaxes is not a hard limit, and it relaxed it in the *dangerous*
    direction: a configured 25.2 was being enforced at 25.0. If a rail is set
    somewhere that makes the guard trip often, that is a fact the owner needs told,
    not corrected behind their back — see ``warn_if_inside_band``.
    """
    return float(hard_min), float(hard_max)


def warn_if_inside_band(target: float, band_low: float, band_high: float,
                        hard_min: float, hard_max: float) -> str | None:
    """Say so when a rail sits close enough to the band to trip on normal ripple."""
    floor, ceil = target - band_low, target + band_high
    if hard_min > floor - RAIL_RIPPLE:
        return (f"cold rail {hard_min:.1f} is within {RAIL_RIPPLE} °C of the band floor "
                f"{floor:.1f} — ordinary tracking ripple will trip it and cut the AC")
    if hard_max < ceil + RAIL_RIPPLE:
        return (f"hot rail {hard_max:.1f} is within {RAIL_RIPPLE} °C of the band top "
                f"{ceil:.1f} — ordinary tracking ripple will trip it")
    return None


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
        self._below_band_since: datetime | None = None
        # Why the current overcool trip happened, because it decides the release:
        # a rail trip releases at the rail, a "controller is not recovering it" trip
        # must hold until the room is genuinely back in the band, or it releases
        # into the same stall it just interrupted and trips again a minute later.
        self._tripped_stuck_cold = False
        # The running mode the unit was in when this guard cut its power, so the
        # restore puts back the owner's mode rather than one this code chose.
        self._mode_before_cut: str | None = None

    def _enter(self, state: str, now: datetime) -> None:
        if state != self.state:
            self.state = state
            self._since = now

    def _mins_in_state(self, now: datetime) -> float:
        if self._since is None:
            return 1e9
        return (now - self._since).total_seconds() / 60.0

    def _restore_cmd(self, p: ZoneParams, why: str) -> Command:
        """Put the unit back the way it was: power, mode, and a safe setpoint.

        All three, every time, and repeated until the unit agrees. The switch usually
        starts it on its own; the mode covers the case where it comes back in standby;
        the setpoint keeps it from resuming the cold setting that caused the trip.
        """
        return Command(
            mode=MODE_SAFETY_OVERCOOL,
            reason=why,
            set_ac_power=True,
            set_hvac_mode=self._mode_before_cut or DEFAULT_HVAC_MODE,
            set_setpoint=p.setpoint_max,
        )

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
            cut_power = self.state == STATE_OVERCOOL
            self._enter(STATE_FAILSAFE, now)
            # A SETPOINT, not a room temperature. floor(target − band_low) is a
            # room figure; what setpoint holds the room there depends on the load,
            # so parking 25 on a mild night sits the room near 24.6 — below the
            # cold rail, open loop, with this branch returning before the rails are
            # even checked. FAILSAFE_SETPOINT is the neutral, correctly-dimensioned
            # value and was defined for this and never used.
            park = max(p.setpoint_min, min(p.setpoint_max, FAILSAFE_SETPOINT))
            return Command(
                mode=MODE_FAILSAFE,
                reason=(f"sensor unavailable → park AC at {park}, fan off"
                        + (", power back on (this guard cut it)" if cut_power else "")),
                set_setpoint=park,
                # Power is left as it was — with one exception. An overcool trip cuts
                # it, and a guard that cannot see the room can never put it back, so
                # a thermometer that dies just after a cold trip would leave the AC
                # off indefinitely with nothing watching. That is the one failure
                # this hardware actually has, and it is the worst way to fail.
                set_ac_power=True if cut_power else None,
                set_hvac_mode=(self._mode_before_cut or DEFAULT_HVAC_MODE) if cut_power else None,
                set_fan=False,
            )

        # --- has a value but stale: freeze, change nothing -----------------
        if stale:
            overriding = self.state if self.state in (STATE_OVERHEAT, STATE_OVERCOOL) else None
            self._enter(STATE_STALE, now)
            held = self._mins_in_state(now)
            # The RAILS STILL APPLY. The reading is old, not wrong, and a room
            # already past a rail when the sensor went quiet is the case that most
            # needs the guard — returning here unconditionally switched every
            # protection off for as long as the sensor stayed quiet.
            if s.comfort > sp.hard_max:
                self._enter(STATE_OVERHEAT, now)
                return self._overheat_cmd(s, p, s.comfort, sp)
            if s.comfort < sp.hard_min:
                self._enter(STATE_OVERCOOL, now)
                return self._overcool_cmd(s, s.comfort, sp, 0.0)
            park = max(p.setpoint_min, min(p.setpoint_max, FAILSAFE_SETPOINT))
            # An override in force is RELEASED on the way into a hold, never held.
            # "Hold" means actuate nothing, so whatever the guard last wrote stayed
            # latched on the hardware with nothing able to revise it: an overheat
            # blast kept blasting, and an overcool trip left the AC off for as long
            # as the sensor stayed quiet — a dead thermometer battery after a cold
            # trip, which is this system's commonest hardware failure meeting its
            # most dangerous state. The rails above still had their say first.
            if overriding is not None:
                self._below_band_since = None
                self._tripped_stuck_cold = False
                self._overcool_released_at = now
                return Command(
                    mode=MODE_STALE_HOLD,
                    reason=(f"no fresh reading (last {s.comfort:.2f}) and it is inside "
                            f"the rails → releasing the {overriding} override, "
                            f"parking at {park}, fan off"),
                    set_setpoint=park,
                    set_ac_power=True if overriding == STATE_OVERCOOL else None,
                    set_hvac_mode=(self._mode_before_cut or DEFAULT_HVAC_MODE)
                    if overriding == STATE_OVERCOOL else None,
                    set_fan=False)
            if held >= STALE_PARK_AFTER_MIN:
                return Command(
                    mode=MODE_STALE_HOLD,
                    reason=(f"no fresh reading for {held:.0f} min (last {s.comfort:.2f}) "
                            f"→ parking at {park}, fan off"),
                    set_setpoint=park, set_fan=False)
            return Command(
                mode=MODE_STALE_HOLD,
                reason=f"no fresh reading for {held:.0f} min (last {s.comfort:.2f}) → hold",
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
            release_at = (p.target - p.band_low
                          - (0.0 if p.fan_available else p.no_fan_offset) + RELEASE_HYST
                          if self._tripped_stuck_cold else sp.hard_min + RELEASE_HYST)
            # the one time gate: we cut the power, so don't restart the
            # compressor seconds later (and don't flap on sensor noise).
            if y >= release_at and off_for >= MIN_OFF_MIN:
                self._overcool_released_at = now
                # Start the sustained-cold clock afresh, or the trip that just
                # ended is still on the books and re-fires on the next tick.
                self._below_band_since = None
                self._tripped_stuck_cold = False
                # Hand back with the unit RUNNING AGAIN and with a setpoint that
                # cannot resume the overcool. Restoring power alone put the unit back
                # on the cold setpoint it was holding when power was cut, and the
                # compressor dwell then blocked the correction for up to six minutes.
                # Restoring power and setpoint alone left it powered but in STANDBY,
                # and a controller that may not switch an AC on cannot finish that —
                # so the guard is not released until the unit reports it is running.
                if not s.ac_on:
                    self._enter(STATE_RESTORING, now)
                    return self._restore_cmd(
                        p, f"OVERCOOL guard: comfort {y:.2f} back above "
                           f"{release_at:.1f} → restoring the unit")
                self._enter(STATE_NORMAL, now)
                if opt_cmd.set_setpoint is None:
                    opt_cmd.set_setpoint = p.setpoint_max
            else:
                return self._overcool_cmd(s, y, sp, off_for,
                                          stuck_cold=self._tripped_stuck_cold,
                                          band_floor=p.target - p.band_low)
        elif self.state == STATE_RESTORING:
            if s.ac_on:
                self._enter(STATE_NORMAL, now)   # it is running — the room is theirs
            elif self._mins_in_state(now) < RESTORE_TIMEOUT_MIN:
                # Re-assert rather than assume. A single write to this unit is dropped
                # often enough that one attempt is not a restore.
                return self._restore_cmd(
                    p, f"OVERCOOL guard: the unit has not started yet "
                       f"({self._mins_in_state(now):.1f} min) → asserting power, "
                       f"mode and setpoint again")
            else:
                # Give up rather than argue indefinitely with a person who may be
                # switching it off deliberately. The room is left with a unit that
                # is off and a log row saying so, which is the honest outcome — and
                # the rails still watch it.
                self._enter(STATE_NORMAL, now)
                return Command(
                    mode=MODE_SAFETY_OVERCOOL,
                    branch="safety.restore_failed",
                    reason=(f"OVERCOOL guard: gave up restoring the unit after "
                            f"{RESTORE_TIMEOUT_MIN:.0f} min — it still reports off. "
                            f"The room is at {y:.2f} and NOTHING is regulating it"))
        elif self.state in (STATE_FAILSAFE, STATE_STALE):
            self._enter(STATE_NORMAL, now)  # sensor came back / reports again

        # --- the controller is not working -------------------------------
        # Tracked on the band, not the rail, and on the reading, not a prediction.
        # Anchored to what the controller is actually holding, not to the raw
        # target. With no fan the zone shifts down by ``no_fan_offset`` while a
        # target-anchored floor stays put, leaving 0.025 °C between the loop's own
        # deadband and the guard's trip line — the guard would fire on the loop
        # working normally.
        band_floor = p.target - p.band_low - (0.0 if p.fan_available else p.no_fan_offset)
        if y < band_floor - SUSTAINED_COLD_DEPTH:
            if self._below_band_since is None:
                self._below_band_since = now
        else:
            self._below_band_since = None
        stuck_cold = (self._below_band_since is not None
                      and (now - self._below_band_since).total_seconds() / 60.0
                      >= SUSTAINED_COLD_MIN)

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
        if y < floor or stuck_cold:
            self._tripped_stuck_cold = stuck_cold and y >= floor
            # Remember what the unit was running before we take its power away, so
            # the restore puts the owner's mode back rather than one we picked.
            if s.ac_on and s.hvac_mode:
                self._mode_before_cut = s.hvac_mode
            self._enter(STATE_OVERCOOL, now)
            return self._overcool_cmd(s, y, sp, 0.0, stuck_cold=stuck_cold,
                                      band_floor=band_floor)

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
            # …and the running mode with it, re-asserted every tick like the rest of
            # this command. A unit left in standby at the hot rail is the worst
            # outcome available: it looks like a full-cool blast in every log and does
            # nothing to the room.
            set_hvac_mode=s.hvac_mode or self._mode_before_cut or DEFAULT_HVAC_MODE,
            set_setpoint=blast,
            set_blower_idx=top_blower,
            set_fan=True if guard_fan > 0 else None,
            set_fan_level=guard_fan if guard_fan > 0 else None,
        )

    def _overcool_cmd(self, s: Signals, y: float, sp: SafetyParams,
                      off_for: float, *, stuck_cold: bool = False,
                      band_floor: float | None = None) -> Command:
        release = sp.hard_min + RELEASE_HYST
        if stuck_cold:
            reason = (f"OVERCOOL guard: comfort {y:.2f} has been below the band "
                      f"({band_floor:.2f}) for {SUSTAINED_COLD_MIN:.0f} min — the "
                      f"controller is not recovering it → AC off")
        elif y < sp.hard_min:
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
