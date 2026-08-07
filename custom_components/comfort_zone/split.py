"""Output stage — one continuous demand, three actuators of very different grain.

The controller produces a single number: ``u``, the virtual setpoint in °C that
would hold the room on target if the unit accepted fractions. It does not. On this
VRF ``target_temp_step`` is 1 and the usable range is 24–27, so the setpoint is a
**four-position actuator**, and at a measured plant gain near 0.5 °C of comfort per
°C of setpoint, one position is worth more than half of a ±0.4 band. That is a
resolution limit, not a tuning failure, and it is why every previous version
oscillated. This module is where the design answers it.

All three actuators are expressed in **one currency** — equivalent degrees of
setpoint — so they can be compared instead of ranked:

    u_delivered(setpoint, blower) = setpoint − blower · g

``g`` is the blower's worth as a temperature lever, fitted offline. Splitting the
demand is then **mid-ranging**, the standard way to drive a coarse slow actuator
and a fine fast one from a single loop: aim the setpoint so the blower sits in the
middle of its own range, leaving it headroom in both directions.

* the **setpoint** carries the steady-state load and moves rarely — only when it is
  more than half a step plus a hysteresis margin from where it should be, and never
  faster than the compressor dwell. The hysteresis is on the *output*, which is what
  a deadband should always have been: a deadband on the input error makes the loop
  blind, a deadband on the output only makes it patient;
* the **blower** takes the residual the integer setpoint cannot express;
* the **circulation fan** takes whatever is left. It does not cool the room, so it
  cannot close the loop — it makes the residual tolerable while the slower actuators
  catch up, which is exactly what a leftover fraction of a degree deserves.

``g = 0`` — a blower that measured as worth nothing, or one that was never
identified — collapses this to setpoint-plus-fan with no special case: the
mid-range offset goes to zero, ``u_delivered`` becomes the setpoint, and the blower
simply parks at its quietest level.

The fan and the blower are sized against ``u_raw``, the demand *before* the output
clamp, so that saturation reaches them. At the setpoint floor on a hot afternoon
the clamped demand carries no information at all — every tick reads "exactly what
was asked for" — while the unclamped demand still says how far short the unit is
falling, which is precisely when the fine actuators are most worth having.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Beyond half a setpoint step, how much further the demand must drift before the
# setpoint follows it. Costs a little steady-state offset, which the blower and fan
# absorb, and buys immunity to the ripple that made v4 step on its own noise.
#
# There is a HARD FLOOR under this, and it is not a matter of taste. Stepping the
# setpoint by 1 immediately moves the predicted settled point by the plant gain K,
# and the loop answers that with Kc·K of demand *against* the step it just took. So
# a step from distance d leaves the demand (1 − d) + Kc·K from the new setpoint, and
# unless the threshold h clears
#
#     h ≥ (1 + Kc·K) / 2
#
# the step lands no closer than it started and the loop hunts. Measured live at
# h = 0.65 against a floor of 0.66 (Kc 0.64, K 0.5): setpoint 24→25→24 in six
# minutes, with the blower and fan round-tripping alongside it. The floor is
# computed from the model rather than hard-coded, so a re-fit cannot quietly
# reintroduce the limit cycle.
SP_HYSTERESIS = 0.15        # the minimum, used when the floor is lower
SP_HYSTERESIS_MARGIN = 0.15  # clearance above the floor, so it is not marginal
# Once the room is outside its comfort zone the threshold collapses toward this,
# reaching it URGENT_SPAN °C out. Half a step is the least it can ever be: below
# that the nearest integer is by definition already the right one.
SP_MIN_THRESHOLD = 0.5
URGENT_SPAN = 0.3
# Minutes between setpoint commands. Hardware, about the compressor, never relaxed.
SP_DWELL_MIN = 6.0
# Minutes between blower changes, and how far past a level boundary the demand must
# sit before it moves. The blower is cheap but audible in a bedroom.
BLOWER_DWELL_MIN = 3.0
BLOWER_HYSTERESIS = 0.35
# Used only when the blower's gain was never identified: the residual at which it
# steps up a level, and the one at which it steps back down. The gap between them
# is the hysteresis, and both sit inside the ±0.65 °C the setpoint's own rounding
# can leave, so the blower still trims ordinary quantisation error rather than
# waiting for the setpoint to run out entirely.
BLOWER_STEP_UP_TRIM = 0.25
BLOWER_STEP_DOWN_TRIM = 0.05
# Residual, in °C of setpoint the slower actuators could not deliver, at which the
# fan reaches its cap. Sized to the worst quantisation error the setpoint can leave.
FAN_SPAN = 0.6
FAN_ON_TRIM = 0.10        # residual at which the fan starts
FAN_OFF_TRIM = 0.0        # …and at which it stops, so it cannot chatter
BLOWER_EPS = 0.01         # below this the blower is not a temperature lever at all


@dataclass
class SplitOutput:
    setpoint: int
    blower_idx: int
    trim: float               # °C of demand the setpoint and blower could not deliver
    fan_on: bool
    fan_level: int
    sp_blocked_by_dwell: bool
    sp_dwell_left: float      # minutes


class SplitRange:
    """Holds only the dwell clocks; every output is recomputed from ``u`` each tick."""

    def __init__(self, model=None) -> None:
        self.model = model
        self._last_sp_at: datetime | None = None
        self._last_blower_at: datetime | None = None

    def sp_threshold(self, urgency: float = 0.0) -> float:
        """How far the demand must sit from the setpoint before it moves.

        The anti-hunting floor below only applies while there is nothing to fix.
        Hysteresis exists to stop the setpoint chasing its own ripple, and ripple
        is a problem *inside* the comfort zone; once the room has left the zone,
        refusing to act is not patience, it is the failure.

        Measured 08-07 06:08 with a fixed 0.81 threshold: the room fell from 25.85
        to 25.21 over fourteen minutes, more than half a degree below its zone,
        while the loop sat on a setpoint of 24 waiting for the demand to travel far
        enough to qualify. Nothing was logged, because nothing was actuated.

        So ``urgency`` — how far outside the zone the room is, in °C — collapses the
        threshold toward the bare half-step. The zone itself is what stops the
        limit cycle now: a step that brings the room back inside produces zero
        error, so there is no demand left to chase it back out.
        """
        base = 0.5 + SP_HYSTERESIS
        if self.model is not None:
            floor = (1.0 + self.model.kc * self.model.gain_per_step) / 2.0
            base = max(base, floor + SP_HYSTERESIS_MARGIN)
        if urgency <= 0.0:
            return base
        # The collapse target is the anti-hunting floor while the room is only
        # mildly out — and the bare half-step once it is far enough out that the
        # floor's own argument no longer applies.
        #
        # That argument is about a step landing no closer than it started, which can
        # only happen when one step is comparable to the whole error. Once the room
        # is more than a step's worth of room temperature outside its zone — one
        # setpoint level is `gain_per_step` °C of room — a full step is unambiguously
        # in the right direction and cannot be reversed by its own arrival. Holding
        # the floor there buys nothing and costs minutes, measured: three of them,
        # and 0.16 °C of extra fall, on the 08-07 trajectory.
        floor = SP_MIN_THRESHOLD
        step_worth = SP_MIN_THRESHOLD
        if self.model is not None:
            floor = max(SP_MIN_THRESHOLD,
                        (1.0 + self.model.kc * self.model.gain_per_step) / 2.0)
            step_worth = max(0.1, self.model.gain_per_step)
        target = floor if urgency < step_worth else SP_MIN_THRESHOLD
        frac = min(1.0, urgency / URGENT_SPAN)
        return base - (base - target) * frac

    def commit(self, now: datetime, setpoint_issued: bool, blower_issued: bool) -> None:
        """Start the dwell clocks, for commands the caller actually applied."""
        if setpoint_issued:
            self._last_sp_at = now
        if blower_issued:
            self._last_blower_at = now

    def _elapsed(self, at: datetime | None, now: datetime) -> float:
        return 1e9 if at is None else (now - at).total_seconds() / 60.0

    def deliverable(self, p) -> tuple[float, float]:
        """The demand range the actuators can actually reach, for anti-windup.

        Wider than the setpoint limits at the cold end, because the blower keeps
        cooling after the setpoint has run out of room.
        """
        g = max(0.0, p.blower_gain)
        return (p.setpoint_min - p.regular_blower_max * g, float(p.setpoint_max))

    def resolve(self, u_raw: float, now: datetime, cur_sp: int | None,
                cur_blower: int | None, p, cur_fan_on: bool = False,
                urgency: float = 0.0) -> SplitOutput:
        g = p.blower_gain if p.blower_gain > BLOWER_EPS else 0.0
        b_max = p.regular_blower_max
        cur_b = cur_blower if cur_blower is not None else 0

        # --- coarse: the setpoint, aimed to leave the blower mid-range ---------
        sp_ideal = u_raw + (b_max * g) / 2.0
        lo, hi = p.setpoint_min, p.setpoint_max
        dwell_left = max(0.0, SP_DWELL_MIN - self._elapsed(self._last_sp_at, now))
        want = int(round(max(lo, min(hi, sp_ideal))))
        blocked = False
        if cur_sp is None:
            sp = want
        elif abs(cur_sp - sp_ideal) <= self.sp_threshold(urgency):
            sp = cur_sp                      # near enough — let the fine actuators trim
        elif dwell_left > 0:
            sp, blocked = cur_sp, True       # pacing the compressor
        else:
            sp = want
        sp = int(max(lo, min(hi, sp)))
        # NOTE: the dwell clock is stamped by the caller once the command is really
        # issued, not here. Stamping on "my chosen setpoint differs from the
        # device's" burned the dwell on moves that never happened — during a guard
        # override, or while the unit was off — so the compressor pacing was spent
        # before the controller got the room back.

        # --- fine: the blower takes the residual ------------------------------
        # With a fitted gain the level is computed outright. Without one the
        # direction is still known — a positive residual means the setpoint we can
        # command is warmer than the one we want, and more airflow is the answer —
        # so the blower moves one level at a time on the sign of the residual. That
        # keeps the lever without inventing a number for how much it is worth.
        free = self._elapsed(self._last_blower_at, now) >= BLOWER_DWELL_MIN
        if g:
            b_ideal = (sp - u_raw) / g
            b = cur_b
            if free and abs(b_ideal - cur_b) > 0.5 + BLOWER_HYSTERESIS:
                b = int(round(b_ideal))
        else:
            residual = sp - u_raw
            b = cur_b
            if free and residual > BLOWER_STEP_UP_TRIM:
                b = cur_b + 1
            elif free and residual < BLOWER_STEP_DOWN_TRIM:
                b = cur_b - 1
        # The clamp also brings a blower found ABOVE the cap straight back down,
        # whatever the residual says: the dwell exists to stop chatter, and it may
        # not outrank a limit the room has been given (a quiet window starting, or
        # the guard handing 高 back).
        b = max(0, min(b_max, b))

        # --- finest: the fan carries what is left -----------------------------
        trim = (sp - b * g) - u_raw
        return SplitOutput(
            setpoint=sp, blower_idx=b, trim=trim,
            sp_blocked_by_dwell=blocked, sp_dwell_left=dwell_left,
            **self._fan(trim, p, cur_fan_on),
        )

    def _fan(self, trim: float, p, running: bool) -> dict:
        """Fan level from the residual, with real hysteresis around the threshold.

        A fan already running keeps running until the residual falls to
        FAN_OFF_TRIM; one that is off waits for FAN_ON_TRIM. Without the current
        state there is only one threshold, and the fan chatters across it.
        """
        if not p.fan_available:
            return {"fan_on": False, "fan_level": 0}
        if trim <= (FAN_OFF_TRIM if running else FAN_ON_TRIM):
            return {"fan_on": False, "fan_level": 0}
        frac = max(0.0, min(1.0, trim / FAN_SPAN))
        level = int(round(p.fan_min_level + frac * (p.fan_max_level - p.fan_min_level)))
        return {"fan_on": True, "fan_level": max(p.fan_min_level,
                                                 min(p.fan_max_level, level))}
