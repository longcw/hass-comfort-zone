"""The supervisor — plumbing between a feedforward, a feedback loop and an output stage.

Given a snapshot of signals and resolved zone parameters, :meth:`Controller.tick`
returns a :class:`Command`. It touches no Home Assistant APIs, so it is unit-testable.

There is no ladder. Versions 1–4 were a hand-built state machine of cost-ordered
branches, each with its own guard condition, and every fault of the last fortnight
was an interaction *between* branches rather than a bug inside one — a net in-flight
sum cancelling a fresh step, a learned deadband widening the warm gate, a power veto
outranking a rising room. The structure was the defect, so this version has one
signal path and no arms to trade places:

    outdoor + forecast ──► reset.py  ──► u_ff  ─┐
                                                ├──► u ──► split.py ──► setpoint
    target ──►(+)──► error ──► pi.py  ──► u_fb ─┘                   ├─► blower
    settled ─►(−)                                                   └─► fan

``u`` is a continuous virtual setpoint in °C: the value the unit would be given if
it accepted fractions. :mod:`split` turns it into the three actuators it really has.

**Power is a bounded feedforward, never a veto.** The meter is whole-house, and the
v1–v4 *engagement detector* that read it as "did my command take?" was wrong four
times out of four — one threshold cannot serve both "engaged" and "unresponsive", so
fixing a false positive mechanically created a false negative. That is gone and is
not coming back: persistent error is the evidence a command did not take, and the
integrator answers it in proportion. What power is used for now is a different
question — "what is about to happen to the room?" — answered as a small capped bias
on the feedforward that no decision hangs on. See :mod:`power`.

**Setpoint writes are stateless.** ``u`` is absolute and recomputed every tick, so
the command is simply "write it if it differs from what the unit reports". The
``_commanded_sp`` bookkeeping, the drift detection and the re-assert timer all go
away, and the cloud proxy's habit of re-reporting its own remembered setpoint (7 of
30 transitions on 08-06) becomes self-correcting by construction. The predictor is
fed from *observed* setpoint transitions by the caller, for the same reason: the
room responds to what the unit is actually running at, not to what we asked for.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import reset
from .const import (
    MODE_COOLING,
    MODE_EASING,
    MODE_FAILSAFE,
    MODE_FAN_ASSIST,
    MODE_IDLE,
)
from .pi import PiController
from .power import PowerLead
from .split import SplitRange

# How much of the band the loop actively holds. Correcting only as far as the band
# edge is the classic dead-band failure — the load pushes the room straight back out
# and the loop cycles on the boundary — so the zone it returns to sits inside it.
#
# Half the band: inside it the loop does nothing, so the band stays the knob that
# trades comfort against compressor motion. What makes a wide zone safe is not
# shrinking it but what happens at its edge — see zone_error.
INNER_ZONE_FRAC = 0.5
# How close to a deliverable limit counts as sitting ON it.
#
# The reportable state is "the output is pinned and has no headroom left", NOT
# "the demand exceeds what we can deliver". Those look the same for a few minutes
# and then diverge: back-calculation exists precisely to park the demand at the
# boundary, so the excess decays to nothing while the loop is still every bit as
# stuck. Measuring the excess therefore catches only the transient — live, a loop
# that had been at the floor for 50 minutes reported a shortfall of 0.01 °C.
LIMIT_EPS = 0.02


def regular_blower_top(levels: list[str]) -> int:
    """Highest ladder index the optimizer may use — the ladder's top is the guard's.

    Also what the UI lists a cap from: a level that can never be honoured must not
    be offered as a setting.
    """
    n = len(levels)
    return max(0, n - 2) if n >= 2 else max(0, n - 1)


@dataclass
class Signals:
    now: datetime
    comfort: float | None
    slope: float | None                  # °C/min, display and telemetry only
    outdoor: float | None = None
    forecast: list[tuple[datetime, float]] | None = None
    power: float | None = None           # W, display only — never a control input
    ac_on: bool = False
    setpoint: int | None = None
    blower_idx: int | None = None
    fan_on: bool = False
    fan_level: int | None = None
    # The safety guard had the room on the previous tick, so this tick's actuator
    # position is not the controller's doing and must not feed its integrator.
    guard_active: bool = False


@dataclass
class ZoneParams:
    target: float
    band_low: float                 # the fit metric, and the rails' clearance —
    band_high: float                # not a control input; see const.OPT_BAND_LOW
    no_fan_offset: float            # °C the target drops when no fan can run
    setpoint_min: int
    setpoint_max: int
    blower_levels: list[str]
    fan_min_level: int
    fan_max_level: int              # 0 = no fan in this window
    blower_gain: float = 0.0        # °C of equivalent setpoint per blower level
    fan_assist_enabled: bool = True
    # Quiet limits, resolved for the window in force. The rail guard is deliberately
    # exempt from both — see :attr:`fan_guard_level` and ``safety._overheat_cmd``.
    fan_max_guard: int | None = None
    blower_max_idx: int | None = None
    hard_min: float | None = None
    hard_max: float | None = None
    setpoint_device_min: int = 16   # lowest setpoint the unit accepts (guard blast)

    @property
    def regular_blower_max(self) -> int:
        top = regular_blower_top(self.blower_levels)
        return top if self.blower_max_idx is None else max(0, min(top, self.blower_max_idx))

    @property
    def fan_available(self) -> bool:
        """A zero cap says "no fan", the same as switching fan-assist off."""
        return self.fan_assist_enabled and self.fan_max_level > 0

    @property
    def fan_guard_level(self) -> int:
        """Fan speed for the rail guard, which a quiet cap does not bind.

        Zero only when the zone has no fan to blast at all.
        """
        return self.fan_max_guard if self.fan_max_guard is not None else self.fan_max_level


@dataclass
class Command:
    mode: str = MODE_IDLE
    reason: str = ""
    set_setpoint: int | None = None
    set_blower_idx: int | None = None
    set_ac_power: bool | None = None
    set_fan: bool | None = None
    set_fan_level: int | None = None
    # A stable id for what decided this, comparable across ticks where the reason
    # text is not: the UI collapses repeats on it and offline eval groups by it.
    branch: str = ""
    # The quantities behind the decision, as read-only telemetry. Nothing reads it back.
    trace: dict = field(default_factory=dict)


class Controller:
    def __init__(self, predictor) -> None:
        self.predictor = predictor
        self.pi = PiController(predictor.params)
        self.split = SplitRange(predictor.params)
        self.power = PowerLead()
        self._last_tick_at: datetime | None = None
        self._last_ff: float | None = None

    def note_setpoint_change(self, at: datetime) -> None:
        """Told by the caller when the UNIT's setpoint actually changed."""
        self.power.note_setpoint_change(at)

    def zone(self, p: ZoneParams) -> tuple[float, float]:
        """The inner comfort zone the loop actively holds.

        The user's interface is a target and a band, and a band means "this much
        drift is fine". So the loop regulates to a ZONE, not to a point: no error
        while the room is comfortable, and an error measured to the nearest edge
        once it is not. That is what makes the band a real knob again — a wider
        band is a wider zone is less actuator motion — rather than something that
        only changed which excursions got counted.

        The zone is a FRACTION of the band, not the band itself. Correcting back
        only as far as the band edge is the classic dead-band failure: the load
        pushes the room straight back out and the loop cycles on the boundary.

        **Centre and width are separate questions, and conflating them costs fit.**
        The CENTRE is about accuracy — an asymmetric band's middle is not its
        target, and aiming anywhere else spends margin on the side that has less
        of it. The WIDTH is about calm — it is how much drift earns no reaction.
        Deriving the zone as ``target ± band·frac`` ties them together and
        under-shifts the centre in exact proportion to how lazy the loop is asked
        to be; measured over six windows that was 3.4 points of band fit worse
        than centring alone, for no gain.

        The zone shifts down when no air movement is available: warmth is less
        tolerable without a breeze. A shift, never a widening — a band that grows
        is a band the controller can sit inside, which is how the room parked at
        27.25 on 07-26.
        """
        centre = p.target + (p.band_high - p.band_low) / 2.0
        if not p.fan_available:
            centre -= p.no_fan_offset
        half = (p.band_low + p.band_high) / 2.0 * INNER_ZONE_FRAC
        return centre - half, centre + half

    @staticmethod
    def zone_error(settled: float, lo: float, hi: float) -> float:
        """Zero inside the zone; outside, the distance to its CENTRE.

        The step at the boundary is deliberate and is the whole point. Measuring to
        the *edge* means the loop leaves the zone with an error of nearly nothing
        and has to wait for the room to travel before it responds at all — on
        08-07 that was fourteen minutes and a full degree, because the error
        entered at 0.02 °C and the integral had to build from zero at 0.024 °C/min.

        Measuring to the centre means the moment the room is out, the loop is
        already asking to bring it properly back, not merely to the edge it just
        crossed. That also removes the classic dead-band limit cycle for free: a
        recovery that stops at the edge gets pushed straight back out, a recovery
        aimed at the centre does not.

        Sign matches the point-tracking convention: positive means the room needs
        to be warmer, so the setpoint rises.
        """
        if lo <= settled <= hi:
            return 0.0
        return (lo + hi) / 2.0 - settled

    def tick(self, s: Signals, p: ZoneParams) -> Command:
        dt_min = 0.0
        if self._last_tick_at is not None:
            dt_min = (s.now - self._last_tick_at).total_seconds() / 60.0
        self._last_tick_at = s.now

        if s.comfort is None:
            return Command(mode=MODE_FAILSAFE, branch="failsafe.no_reading",
                           reason="no comfort reading")

        m = self.predictor.params
        y = s.comfort
        zone_lo, zone_hi = self.zone(p)
        # The feedforward still needs a single point to solve its curve for; the
        # zone's centre is the load it should be sized for.
        target = (zone_lo + zone_hi) / 2.0
        settled = self.predictor.predict_settled(s.now, y)
        error = self.zone_error(settled, zone_lo, zone_hi)

        # --- feedforward: the setpoint this load calls for ---------------------
        ff = reset.feedforward(s.now, target, s.outdoor, s.forecast, m)
        if ff is None:
            # No outdoor reading. Hold the last curve rather than jumping to a
            # neutral value, which the integrator would have to unwind; failing
            # that, sit in the middle of the envelope and let feedback do all of
            # the work. The neutral must be a SETPOINT and it must be constant:
            # anything derived from the curve without its outdoor term is not one
            # (measured live: it produced 13.1 °C, a 10 °C phantom shortfall that
            # pinned the fan), and anything tracking the device would feed back on
            # itself through the very setpoint it sets.
            ff = self._last_ff if self._last_ff is not None \
                else (p.setpoint_min + p.setpoint_max) / 2.0
        self._last_ff = ff

        # --- leading indicator: what the meter says is about to happen ---------
        # Added to the FEEDFORWARD, not the feedback: it is a prediction of a
        # disturbance, in the same currency and with the same bounded authority as
        # the weather. The loop remains free to overrule it.
        self.power.observe(s.now, s.power, s.ac_on)
        p_bias, p_why = self.power.bias(s.now, s.power, s.ac_on)
        ff += p_bias

        # --- feedback: the residual, on the dead-time-free error ---------------
        lo, hi = self.split.deliverable(p)
        frozen = s.guard_active or not s.ac_on
        out = self.pi.step(error=error, u_ff=ff, dt_min=dt_min,
                           lo=lo, hi=hi, frozen=frozen)

        # NEVER ASK FOR A COLDER SETPOINT WHILE THE THERMOMETER SAYS THE ROOM IS
        # ALREADY COLD. `settled` can sit above the zone during a recovery purely
        # because the model believes warming it has already commanded is on the
        # way — and it believes that on the strength of `gain_per_step`, a PRIOR
        # set deliberately at the top of its plausible range. That constant DIVIDES
        # into Kc, where guessing high is gentle, but it MULTIPLIES in
        # remaining_effect, where guessing high is aggressive and points at the
        # cold. The two uses want opposite errors. Between a model that may be
        # wrong about what is coming and a thermometer reading where the room is
        # now, the thermometer wins.
        cold_veto = y < zone_lo and s.setpoint is not None and out.u_raw < s.setpoint
        if cold_veto:
            out.u_raw = max(out.u_raw, float(s.setpoint))
            out.u = max(out.u, min(hi, float(s.setpoint)))

        # --- output stage ------------------------------------------------------
        # How far the ROOM is outside its zone, which is what makes a setpoint
        # move urgent. Measured on the reading, not the prediction: the predictor
        # can believe help is coming while the thermometer keeps falling.
        urgency = max(0.0, zone_lo - y, y - zone_hi)
        sp = self.split.resolve(out.u_raw, s.now, s.setpoint, s.blower_idx, p,
                                s.fan_on, urgency)
        # Pinned at a limit, and by how much the demand still exceeds it. The first
        # is the state worth reporting; the second is transient detail (see LIMIT_EPS).
        at_floor = out.u <= lo + LIMIT_EPS
        at_ceiling = out.u >= hi - LIMIT_EPS
        at_limit = at_floor or at_ceiling
        shortfall = abs(out.u_raw - out.u)

        cmd = Command()
        cmd.trace = {
            "y": y, "settled": settled, "slope": s.slope or 0.0, "target": target,
            "zone_lo": zone_lo, "zone_hi": zone_hi,
            "in_zone": zone_lo <= settled <= zone_hi,
            "error": out.error, "u_ff": out.u_ff, "u_fb": out.u_fb,
            "u": out.u, "u_raw": out.u_raw, "integral": out.integral,
            # `saturated` means "pinned at a limit with no headroom", which is what
            # a reader cares about. Whether that is a PROBLEM depends on in_zone:
            # at the floor and comfortable is merely no headroom left, at the floor
            # and drifting out is the unit failing to keep up.
            "saturated": at_limit, "at_limit": "floor" if at_floor
            else "ceiling" if at_ceiling else None,
            "shortfall": shortfall, "frozen": out.frozen,
            "sp": sp.setpoint, "trim": sp.trim, "blower": sp.blower_idx,
            "urgency": urgency, "cold_veto": cold_veto,
            "sp_dwell_left": sp.sp_dwell_left,
            "sp_blocked_by_dwell": sp.sp_blocked_by_dwell,
            "hi": p.target + p.band_high, "lo": p.target - p.band_low,
            "outdoor": s.outdoor,
            "power_bias": p_bias, "power_baseline": p_why["baseline"],
            "power_deviation": p_why["deviation"], "power_quiet": p_why["quiet"],
            "deliverable": [lo, hi],
            "gain": m.gain_per_step, "dead_time": m.dead_time_min, "tau": m.tau_min,
            "kc": m.kc, "ti": m.ti_min, "blower_gain": p.blower_gain,
            "sp_observed": s.setpoint,
        }

        # === the unit is off ==================================================
        # THE CONTROLLER NEVER POWERS THE AC ON. Not when the room is warm, not
        # when it is hot, not ever. An off unit is a person's decision, and on
        # 08-07 this branch reversed one within a single tick in a room already a
        # degree below its zone. The only thing permitted to restore power is the
        # safety guard, putting back what the safety guard itself cut — see
        # safety.SafetyGuard, which owns AC power outright.
        #
        # There is no managed-off here any more either, for the same reason: a
        # controller that can switch the compressor off needs a way to switch it
        # back on, and it is not allowed one. The sustained-cold guard covers what
        # managed-off was for, and it can restore power because it cut it.
        if not s.ac_on:
            cmd.mode = MODE_IDLE
            cmd.branch = "ac.off"
            cmd.reason = (f"the unit is off — leaving it off (room {y:.2f}); "
                          f"only the safety guard may restore power")
            self._fan_only(cmd, s, p, sp)
            return cmd

        # === normal regulation =================================================
        if s.setpoint is None or sp.setpoint != s.setpoint:
            cmd.set_setpoint = sp.setpoint
        self._apply_fine(cmd, s, p, sp)

        cmd.mode = (MODE_COOLING if settled > zone_hi
                    else MODE_EASING if settled < zone_lo else MODE_IDLE)
        if at_limit:
            cmd.branch = "pi.at_floor" if at_floor else "pi.at_ceiling"
            edge = "floor" if at_floor else "ceiling"
            more = (f", still asking {shortfall:.1f}°C past it"
                    if shortfall > LIMIT_EPS else "")
            cmd.reason = (
                f"at the setpoint {edge} ({sp.setpoint}){more} — "
                + ("comfortable, but no headroom left" if error == 0 else
                   f"and {abs(error):.2f}°C outside the zone, so it cannot keep up"))
        elif sp.sp_blocked_by_dwell:
            cmd.branch = "pi.dwell"
            cmd.reason = (f"want setpoint {out.u:.1f}, pacing the compressor "
                          f"({sp.sp_dwell_left:.1f} min left) → holding {sp.setpoint}")
        else:
            cmd.branch = "pi.track" if out.error else "pi.in_zone"
            where = (f"settles {settled:.2f}, inside [{zone_lo:.2f}, {zone_hi:.2f}]"
                     if not out.error else
                     f"settles {settled:.2f}, {abs(out.error):.2f} outside "
                     f"[{zone_lo:.2f}, {zone_hi:.2f}]")
            cmd.reason = (f"{where} → u {out.u:.2f} = ff {out.u_ff:.2f} "
                          f"{out.u_fb:+.2f} fb → setpoint {sp.setpoint}")
        if cmd.mode == MODE_IDLE and (cmd.set_fan or cmd.set_fan_level is not None):
            cmd.mode = MODE_FAN_ASSIST
        return cmd

    # -- helpers ------------------------------------------------------------
    def _fan_only(self, cmd: Command, s: Signals, p: ZoneParams, sp) -> None:
        """The circulation fan is not the AC and keeps working while it is off."""
        if sp.fan_on != s.fan_on:
            cmd.set_fan = sp.fan_on
        if sp.fan_on and (s.fan_level is None or abs(s.fan_level - sp.fan_level) >= 3):
            cmd.set_fan_level = sp.fan_level

    def _apply_fine(self, cmd: Command, s: Signals, p: ZoneParams, sp) -> None:
        """Emit the blower and fan commands, only where they differ from the device."""
        if p.blower_levels and sp.blower_idx != (s.blower_idx if s.blower_idx is not None else 0):
            cmd.set_blower_idx = sp.blower_idx
        if sp.fan_on != s.fan_on:
            cmd.set_fan = sp.fan_on
        if sp.fan_on and (s.fan_level is None or abs(s.fan_level - sp.fan_level) >= 3):
            cmd.set_fan_level = sp.fan_level
