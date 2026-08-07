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

**Power takes no part in any decision.** The meter is whole-house and shared with
every other room, so this room's step is a minority of the signal; in the 2.2 h
after v4 went live, all four escalations the power veto triggered were wrong, and
one of them cooled the room 1.7 °C into a guard trip. One threshold could not serve
both "engaged" and "unresponsive" either, so fixing a false positive mechanically
created a false negative. A PI controller needs no engagement detector: persistent
error *is* the evidence that a command did not take, and the integrator answers it
in proportion instead of through a binary veto that can outrank the model. Power
survives as a number on the card.

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
    MODE_MANAGED_OFF,
)
from .pi import PiController
from .split import SplitRange

# How far above the setpoint ceiling the demand must sit, and for how long, before
# the AC is switched off outright. The loop has run out of warm actuator: it is
# asking for a setpoint the unit does not have. Deliberately patient — cutting power
# is the most disruptive thing this controller can do, and the room is cold already.
MANAGED_OFF_MARGIN = 0.5
MANAGED_OFF_AFTER_MIN = 10.0
# How much of the band the loop actively holds. Correcting only as far as the band
# edge is the classic dead-band failure — the load pushes the room straight back out
# and the loop cycles on the boundary — so the zone it returns to sits half-way in.
INNER_ZONE_FRAC = 0.5


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
    managed_off_max_min: float
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
        self._last_tick_at: datetime | None = None
        self._last_ff: float | None = None
        self._managed_off_since: datetime | None = None
        self._over_ceiling_since: datetime | None = None

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
        """Distance to the nearest edge, and exactly zero inside.

        Sign matches the point-tracking convention: positive means the room needs
        to be warmer, so the setpoint rises.
        """
        if settled > hi:
            return hi - settled
        if settled < lo:
            return lo - settled
        return 0.0

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

        # --- feedback: the residual, on the dead-time-free error ---------------
        lo, hi = self.split.deliverable(p)
        frozen = s.guard_active or not s.ac_on or self._managed_off_since is not None
        out = self.pi.step(error=error, u_ff=ff, dt_min=dt_min,
                           lo=lo, hi=hi, frozen=frozen)

        # --- output stage ------------------------------------------------------
        sp = self.split.resolve(out.u_raw, s.now, s.setpoint, s.blower_idx, p, s.fan_on)

        cmd = Command()
        cmd.trace = {
            "y": y, "settled": settled, "slope": s.slope or 0.0, "target": target,
            "zone_lo": zone_lo, "zone_hi": zone_hi,
            "in_zone": zone_lo <= settled <= zone_hi,
            "error": out.error, "u_ff": out.u_ff, "u_fb": out.u_fb,
            "u": out.u, "u_raw": out.u_raw, "integral": out.integral,
            "saturated": out.saturated, "frozen": out.frozen,
            "sp": sp.setpoint, "trim": sp.trim, "blower": sp.blower_idx,
            "sp_dwell_left": sp.sp_dwell_left,
            "sp_blocked_by_dwell": sp.sp_blocked_by_dwell,
            "hi": p.target + p.band_high, "lo": p.target - p.band_low,
            "outdoor": s.outdoor,
            "deliverable": [lo, hi],
            "gain": m.gain_per_step, "dead_time": m.dead_time_min, "tau": m.tau_min,
            "kc": m.kc, "ti": m.ti_min, "blower_gain": p.blower_gain,
            "sp_observed": s.setpoint,
        }

        # === managed-off: the loop has run out of warm actuator ================
        if self._managed_off_since is not None:
            return self._managed_off(cmd, s, p, y, zone_lo)
        if self._wants_managed_off(s, p, out.u_raw, y, zone_lo):
            self._managed_off_since = s.now
            self.pi.reset()
            cmd.set_ac_power = False
            cmd.set_fan = False
            cmd.mode = MODE_MANAGED_OFF
            cmd.branch = "managed_off.enter"
            cmd.reason = (f"cold ({y:.2f}) and asking for setpoint {out.u_raw:.1f}, "
                          f"above the ceiling {p.setpoint_max} → AC off (auto-returns)")
            return cmd

        if not s.ac_on:
            cmd.set_ac_power = True
            cmd.set_setpoint = sp.setpoint
            cmd.mode = MODE_COOLING
            cmd.branch = "ac.on"
            cmd.reason = f"AC off → power on at setpoint {sp.setpoint}"
            self._apply_fine(cmd, s, p, sp)
            return cmd

        # === normal regulation =================================================
        if s.setpoint is None or sp.setpoint != s.setpoint:
            cmd.set_setpoint = sp.setpoint
        self._apply_fine(cmd, s, p, sp)

        cmd.mode = (MODE_COOLING if settled > zone_hi
                    else MODE_EASING if settled < zone_lo else MODE_IDLE)
        if out.saturated:
            cmd.branch = "pi.saturated_cold" if out.u_raw < lo else "pi.saturated_warm"
            short = abs(out.u_raw - out.u)
            cmd.reason = (f"asking for {out.u_raw:.1f}, {short:.1f}°C past what the unit "
                          f"can deliver → setpoint {sp.setpoint}, blower and fan take the rest")
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
    def _wants_managed_off(self, s: Signals, p: ZoneParams, u_raw: float,
                           y: float, target: float) -> bool:
        """Has the demand sat above the setpoint ceiling long enough to cut power?"""
        if not s.ac_on or y >= target:
            self._over_ceiling_since = None
            return False
        if u_raw <= p.setpoint_max + MANAGED_OFF_MARGIN:
            self._over_ceiling_since = None
            return False
        if self._over_ceiling_since is None:
            self._over_ceiling_since = s.now
            return False
        return (s.now - self._over_ceiling_since).total_seconds() / 60.0 >= MANAGED_OFF_AFTER_MIN

    def _managed_off(self, cmd: Command, s: Signals, p: ZoneParams,
                     y: float, target: float) -> Command:
        off_for = (s.now - self._managed_off_since).total_seconds() / 60.0
        if y >= target or off_for >= p.managed_off_max_min:
            self._managed_off_since = None
            self._over_ceiling_since = None
            cmd.set_ac_power = True
            cmd.set_setpoint = int(max(p.setpoint_min, min(p.setpoint_max, round(p.target))))
            cmd.mode = MODE_COOLING
            cmd.branch = "managed_off.return"
            cmd.reason = (f"managed-off return: comfort {y:.2f} ≥ target {target:.2f}"
                          if y >= target else f"managed-off watchdog after {off_for:.0f}m")
        else:
            cmd.mode = MODE_MANAGED_OFF
            cmd.branch = "managed_off.wait"
            cmd.reason = f"AC off, waiting to warm to {target:.2f} (now {y:.2f})"
        return cmd

    def _apply_fine(self, cmd: Command, s: Signals, p: ZoneParams, sp) -> None:
        """Emit the blower and fan commands, only where they differ from the device."""
        if p.blower_levels and sp.blower_idx != (s.blower_idx if s.blower_idx is not None else 0):
            cmd.set_blower_idx = sp.blower_idx
        if sp.fan_on != s.fan_on:
            cmd.set_fan = sp.fan_on
        if sp.fan_on and (s.fan_level is None or abs(s.fan_level - sp.fan_level) >= 3):
            cmd.set_fan_level = sp.fan_level
