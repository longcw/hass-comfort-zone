"""Scenario tests for the pure control core (no Home Assistant needed).

Run directly:  python tests/test_controller.py
or with pytest: pytest tests/
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components"))

from comfort_zone import reset  # noqa: E402
from comfort_zone.controller import (  # noqa: E402
    Controller,
    Signals,
    ZoneParams,
    regular_blower_top,
)
from comfort_zone.model import FopdtPredictor, ModelParams  # noqa: E402
from comfort_zone.pi import PiController  # noqa: E402
from comfort_zone.safety import SafetyGuard, SafetyParams, effective_rails  # noqa: E402
from comfort_zone.split import SplitRange  # noqa: E402
from comfort_zone import const  # noqa: E402

BLOWERS = ["low", "mid", "high"]   # opaque device labels; the core only uses indices
T0 = datetime(2026, 7, 24, 12, 0, 0)
TICK = timedelta(seconds=45)


def params(target=26.0, band_low=0.4, band_high=0.4, fan_max_level=40,
           fan_max_guard=None, blower_max_idx=None, setpoint_min=24,
           setpoint_max=27, blower_gain=0.0, fan_assist_enabled=True,
           no_fan_offset=0.2):
    return ZoneParams(
        target=target,
        band_low=band_low,
        band_high=band_high,
        no_fan_offset=no_fan_offset,
        setpoint_min=setpoint_min,
        setpoint_max=setpoint_max,
        blower_levels=BLOWERS,
        fan_min_level=10,
        fan_max_level=fan_max_level,
        fan_max_guard=fan_max_guard,
        blower_max_idx=blower_max_idx,
        managed_off_max_min=30,
        blower_gain=blower_gain,
        fan_assist_enabled=fan_assist_enabled,
    )


def model(**kw):
    """Model params with the shipped defaults unless a test needs otherwise."""
    return ModelParams(**kw)


def fresh(m=None):
    return Controller(FopdtPredictor(m or model()))


# 26 °C outdoor puts the reset curve at setpoint 25.0 with a target of 26 — mid
# envelope, so a test exercises the loop rather than the clamp. See
# test_a_hot_day_saturates_the_configured_envelope for the other end.
def sig(t=T0, comfort=26.0, slope=0.0, outdoor=26.0, forecast=None, power=800.0,
        ac_on=True, setpoint=26, blower_idx=0, fan_on=False, fan_level=None,
        guard_active=False):
    return Signals(now=t, comfort=comfort, slope=slope, outdoor=outdoor,
                   forecast=forecast, power=power, ac_on=ac_on, setpoint=setpoint,
                   blower_idx=blower_idx, fan_on=fan_on, fan_level=fan_level,
                   guard_active=guard_active)


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def run(c, p, minutes, *, comfort, t0=T0, setpoint=26, **kw):
    """Tick the controller for ``minutes``, echoing its own setpoint back.

    ``comfort`` may be a constant or a callable of elapsed minutes, so a test can
    describe a room that moves without simulating a plant.
    """
    sp, last = setpoint, None
    t, end = t0, t0 + timedelta(minutes=minutes)
    while t <= end:
        e = (t - t0).total_seconds() / 60.0
        y = comfort(e) if callable(comfort) else comfort
        last = c.tick(sig(t=t, comfort=y, setpoint=sp, **kw), p)
        if last.set_setpoint is not None:
            sp = last.set_setpoint
        t += TICK
    return last, sp


# --- the feedforward -------------------------------------------------------
def test_reset_curve_asks_for_a_colder_setpoint_when_it_is_hotter_outside():
    m = model()
    mild = reset.curve(26.0, 25.0, m)
    hot = reset.curve(26.0, 33.0, m)
    check(hot < mild, f"a hotter day must ask for a lower setpoint, got {mild} → {hot}")
    check(1.0 < mild - hot < 4.0,
          f"8 °C of outdoor swing should move the setpoint ~1.7 °C, got {mild - hot:.2f}")


def test_reset_curve_tracks_the_target_at_one_over_the_gain():
    m = model()
    lo = reset.curve(25.0, 30.0, m)
    hi = reset.curve(26.0, 30.0, m)
    check(abs((hi - lo) - 1.0 / m.gain_per_step) < 1e-6,
          f"1 °C of target must move the setpoint 1/K = {1 / m.gain_per_step}, got {hi - lo}")


def test_forecast_bias_is_bounded():
    m = model()
    # a forecast that runs away: the bias must still be capped
    fc = [(T0 + timedelta(hours=h), 25.0 + 20 * h) for h in range(6)]
    ff = reset.feedforward(T0, 26.0, 25.0, fc, m)
    base = reset.curve(26.0, 25.0, m)
    check(abs(ff - base) <= reset.FORECAST_CAP + 1e-9,
          f"forecast bias {ff - base:.2f} exceeded the {reset.FORECAST_CAP} cap")
    check(ff < base, "a forecast of a hotter afternoon must pre-cool, not warm")


def test_no_outdoor_reading_falls_back_without_a_jump():
    c, p = fresh(), params()
    warm = c.tick(sig(comfort=26.6, outdoor=30.0), p)
    blind = c.tick(sig(t=T0 + TICK, comfort=26.6, outdoor=None), p)
    check(abs(blind.trace["u_ff"] - warm.trace["u_ff"]) < 1e-9,
          "a missing outdoor reading must hold the last curve, not jump")


def test_never_having_had_an_outdoor_reading_yields_a_real_setpoint():
    """Live regression: the first fallback produced 13.1 °C, not a setpoint at all."""
    c, p = fresh(), params()
    out = c.tick(sig(comfort=26.4, outdoor=None, forecast=None), p)
    ff = out.trace["u_ff"]
    check(p.setpoint_min <= ff <= p.setpoint_max,
          f"the cold-start feedforward must be inside {p.setpoint_min}–{p.setpoint_max}, got {ff}")
    check(abs(out.trace["trim"]) < 1.5,
          f"and must not invent a huge shortfall for the fan, got trim {out.trace['trim']:.2f}")


# --- the feedback loop -----------------------------------------------------
def test_a_warm_room_is_cooled_and_a_cold_room_is_eased():
    c, p = fresh(), params()
    warm, _ = run(c, p, 30, comfort=26.9)
    check(warm.trace["u"] < 26.0, f"a warm room must drive u down, got {warm.trace['u']:.2f}")
    c2 = fresh()
    cold, _ = run(c2, p, 30, comfort=25.1)
    check(cold.trace["u"] > warm.trace["u"],
          "a cold room must ask for a warmer setpoint than a warm one")


def test_the_integral_removes_a_standing_offset():
    """A room held off target must keep driving u, not settle at the P term."""
    c, p = fresh(), params()
    first = c.tick(sig(comfort=26.5), p)
    later, _ = run(c, p, 40, comfort=26.5, t0=T0 + TICK)
    check(later.trace["u"] < first.trace["u"] - 0.3,
          f"the integral must keep working on a standing error: "
          f"{first.trace['u']:.2f} → {later.trace['u']:.2f}")


def test_error_is_measured_on_the_settled_prediction_not_the_reading():
    """Cooling already on the way must shrink the error before the room moves."""
    c, p = fresh(), params()
    c.tick(sig(comfort=26.8), p)
    naive = c.tick(sig(t=T0 + TICK, comfort=26.8), p).trace["error"]
    c.predictor.record_setpoint_change(T0 + TICK, -2.0)
    aware = c.tick(sig(t=T0 + 2 * TICK, comfort=26.8), p).trace["error"]
    check(aware > naive + 0.5,
          f"a 2 °C cooling step in flight must shrink the error, {naive:.2f} → {aware:.2f}")


def test_derivative_free_loop_ignores_slope():
    c, p = fresh(), params()
    a = c.tick(sig(comfort=26.5, slope=+0.20), p)
    c2 = fresh()
    b = c2.tick(sig(comfort=26.5, slope=-0.20), p)
    check(abs(a.trace["u"] - b.trace["u"]) < 1e-9,
          "slope must not enter the control law — it is a noisy BLE thermometer")


# --- anti-windup -----------------------------------------------------------
def test_the_integral_does_not_wind_up_against_the_setpoint_floor():
    c, p = fresh(), params()
    # an hour of a room far too hot to answer: the output is pinned at the floor
    hot, _ = run(c, p, 60, comfort=28.5)
    check(hot.trace["saturated"], "28.5 °C against a 24 floor must saturate")
    span = (p.setpoint_max - p.setpoint_min) + 2.0
    check(abs(hot.trace["integral"]) <= span + 1e-6,
          f"the integral ran to {hot.trace['integral']:.2f}, past the ±{span} clamp")
    # The v3/v4 failure this exists to prevent: an hour at the floor must not buy
    # the loop an hour of stubbornness afterwards. Once the room swings cold, the
    # answer has to come in minutes, not after the integral has unwound an hour of
    # error it was never able to act on.
    cold, _ = run(c, p, 15, comfort=25.2, t0=T0 + timedelta(minutes=61),
                  setpoint=hot.trace["sp"])
    check(cold.trace["u"] > 25.0,
          f"15 min after going cold the loop must have released, u={cold.trace['u']:.2f}")


def test_windup_protection_beats_an_unprotected_integrator():
    """The regression v3/v4 suffered: overshoot from an integral nobody unwound."""
    c, p = fresh(), params()
    hot, _ = run(c, p, 90, comfort=28.5)
    protected = hot.trace["integral"]
    # what the same 90 minutes would have accumulated with no back-calculation
    m = model()
    naive, e = 0.0, 26.0 - 28.5
    for _ in range(int(90 / 0.75)):
        naive += (m.kc / m.ti_min) * e * 0.75
    check(abs(protected) < abs(naive) / 3,
          f"back-calculation must hold the integral far below the naive "
          f"{naive:.1f}, got {protected:.1f}")


def test_the_integrator_freezes_when_the_actuator_is_not_ours():
    for label, kw in (("guard", {"guard_active": True}), ("ac off", {"ac_on": False})):
        c, p = fresh(), params()
        c.tick(sig(comfort=27.0, **kw), p)
        before = c.pi.integral
        run(c, p, 30, comfort=27.0, t0=T0 + TICK, **kw)
        check(abs(c.pi.integral - before) < 1e-9,
              f"the integral must not move while {label} has the room")


# --- the output stage ------------------------------------------------------
def test_setpoint_moves_only_past_the_hysteresis_margin():
    m = model()
    s, p = SplitRange(m), params()
    h = s.sp_threshold
    held = s.resolve(26 - h + 0.02, T0, 26, 0, p)
    check(held.setpoint == 26, f"just inside the threshold must hold 26, got {held.setpoint}")
    moved = SplitRange(m).resolve(26 - h - 0.02, T0, 26, 0, p)
    check(moved.setpoint == 25, f"just past it must move to 25, got {moved.setpoint}")


def test_the_setpoint_threshold_clears_the_anti_hunting_floor():
    """The live 24→25→24 limit cycle: a step must land closer than it started.

    Stepping by 1 moves the predicted settled point by K, which the loop answers
    with Kc·K against the step, so a threshold below (1 + Kc·K)/2 hunts forever.
    """
    for m in (model(), model(gain_per_step=1.2), model(tau_c_mult=0.5)):
        h = SplitRange(m).sp_threshold
        floor = (1.0 + m.kc * m.gain_per_step) / 2.0
        check(h > floor, f"threshold {h:.3f} must clear the floor {floor:.3f} "
                         f"(K {m.gain_per_step}, Kc {m.kc:.2f})")
        # the concrete property the floor encodes: after a step from just past the
        # threshold, the demand sits comfortably inside it rather than at the edge
        after = (1.0 - h) + m.kc * m.gain_per_step
        check(after < h, f"a step would land {after:.3f} away against a {h:.3f} "
                         f"threshold — that is the limit cycle")


def test_setpoint_is_paced_by_the_compressor_dwell():
    s, p = SplitRange(), params()
    first = s.resolve(25.0, T0, 26, 0, p)
    check(first.setpoint == 25, "the first move should take")
    soon = s.resolve(24.0, T0 + timedelta(minutes=2), 25, 0, p)
    check(soon.setpoint == 25 and soon.sp_blocked_by_dwell,
          f"a second move 2 min later must be blocked, got {soon.setpoint}")
    later = s.resolve(24.0, T0 + timedelta(minutes=7), 25, 0, p)
    check(later.setpoint == 24, f"after the dwell it must move, got {later.setpoint}")


def test_split_range_mid_ranges_the_blower_when_its_gain_is_known():
    p = params(blower_gain=0.25)
    out = SplitRange().resolve(24.75, T0, 26, 0, p)
    # u_delivered = sp - blower*g must land on the demand
    delivered = out.setpoint - out.blower_idx * p.blower_gain
    check(abs(delivered - 24.75) < 0.3,
          f"the pair should deliver ~24.75, got {delivered:.2f} "
          f"(sp {out.setpoint}, blower {out.blower_idx})")
    check(out.blower_idx > 0, "the blower must carry the fraction the setpoint cannot")


def test_a_zero_blower_gain_collapses_to_setpoint_plus_fan():
    p = params(blower_gain=0.0)
    out = SplitRange().resolve(24.75, T0, 26, 0, p)
    check(out.setpoint == 25, f"with no blower gain the setpoint alone rounds, got {out.setpoint}")
    check(out.trim > 0, "the unservable residual must be positive and reach the fan")
    check(out.fan_on, "the fan must take a residual the setpoint could not deliver")


def test_an_unidentified_blower_still_steps_on_the_residual():
    p = params(blower_gain=0.0)
    s = SplitRange()
    out = s.resolve(24.6, T0, 25, 0, p)     # residual 0.4, past the step-up threshold
    check(out.blower_idx == 1, f"the blower must step up on a warm residual, got {out.blower_idx}")
    back = s.resolve(25.0, T0 + timedelta(minutes=4), 25, 1, p)
    check(back.blower_idx == 0, f"and step back down when the residual clears, got {back.blower_idx}")


def test_saturation_reaches_the_fine_actuators():
    """At the setpoint floor the clamped demand says nothing; the raw demand must."""
    p = params(blower_gain=0.0)
    out = SplitRange().resolve(22.0, T0, 24, 0, p)   # asking well below the floor
    check(out.setpoint == 24, "the setpoint clamps at the floor")
    check(out.fan_on and out.fan_level == p.fan_max_level,
          f"a 2 °C shortfall must run the fan at its cap, got {out.fan_level}")


def test_the_blower_comes_down_to_a_cap_regardless_of_dwell():
    p = params(blower_max_idx=0)
    out = SplitRange().resolve(24.0, T0, 26, 2, p)   # guard left it at index 2
    check(out.blower_idx == 0, f"a quiet cap must bind immediately, got {out.blower_idx}")


def test_deliverable_range_widens_when_the_blower_has_authority():
    lo_none, hi_none = SplitRange().deliverable(params(blower_gain=0.0))
    lo_g, hi_g = SplitRange().deliverable(params(blower_gain=0.25))
    check(hi_none == hi_g == 27, "the warm end is the setpoint ceiling either way")
    check(lo_g < lo_none, f"a blower with authority must widen the cold end, {lo_none} → {lo_g}")


def test_no_fan_available_means_no_fan_command():
    p = params(fan_assist_enabled=False)
    out = SplitRange().resolve(22.0, T0, 24, 0, p)
    check(not out.fan_on and out.fan_level == 0, "a disabled fan must never be commanded")


def test_the_fan_has_real_hysteresis():
    """A residual between the two thresholds must not flip a running fan off."""
    p = params()
    mid = 0.05     # between FAN_OFF_TRIM (0.0) and FAN_ON_TRIM (0.10)
    off = SplitRange().resolve(25.0 - mid, T0, 25, 0, p, cur_fan_on=False)
    check(not off.fan_on, f"an idle fan must not start at a {mid} residual")
    on = SplitRange().resolve(25.0 - mid, T0, 25, 0, p, cur_fan_on=True)
    check(on.fan_on, f"a running fan must keep running at a {mid} residual")


# --- the controller as a whole ---------------------------------------------
def test_setpoint_writes_are_absolute_and_self_correcting():
    """The cloud proxy re-reports its own value; the next tick simply rewrites ours."""
    c, p = fresh(), params()
    last, sp = run(c, p, 20, comfort=26.8)
    check(sp < 26, f"20 min at 26.8 should have cooled below 26, got {sp}")
    drifted = c.tick(sig(t=T0 + timedelta(minutes=21), comfort=26.8, setpoint=27), p)
    check(drifted.set_setpoint is not None and drifted.set_setpoint < 27,
          f"a setpoint the unit invented must be overwritten, got {drifted.set_setpoint}")


def test_no_command_when_the_device_already_holds_the_right_setpoint():
    c, p = fresh(), params()
    out = c.tick(sig(comfort=26.0, setpoint=25), p)
    check(out.set_setpoint is None,
          f"on target at the setpoint the load calls for, but it wrote {out.set_setpoint}")


def test_a_hot_day_saturates_the_configured_envelope():
    """A finding, not a preference: 24 is not a low enough floor for a hot day here.

    The reset curve is fitted from what the old controller itself needed, and at
    33 °C outdoor with a target of 26 it asks for setpoint 23.5 — below the
    configured minimum. The loop then has no coarse actuator left and the blower
    and fan carry everything. Widening the envelope is a comfort decision for the
    room's occupant, so the core only has to behave sanely while it is pinned.
    """
    c, p = fresh(), params()
    out, sp = run(c, p, 30, comfort=26.0, outdoor=33.0, setpoint=24)
    check(out.trace["u_ff"] < p.setpoint_min,
          f"33 °C outdoor should ask below the floor, got {out.trace['u_ff']:.2f}")
    check(sp == p.setpoint_min, f"and the setpoint must sit at the floor, got {sp}")
    check(out.trace["saturated"], "the trace must say so, so the card can show it")


def test_power_takes_no_part_in_any_decision():
    for power in (0.0, 400.0, 5000.0, None):
        c, p = fresh(), params()
        out, _ = run(c, p, 20, comfort=26.7, power=power)
        check(abs(out.trace["u"] - _baseline_u()) < 1e-9,
              f"power={power} changed the decision — it must not")


def _baseline_u():
    c, p = fresh(), params()
    out, _ = run(c, p, 20, comfort=26.7, power=800.0)
    return out.trace["u"]


def test_the_zone_is_centred_for_fit_and_widened_for_calm():
    """Centre and width answer different questions and must not be conflated."""
    c = fresh()
    lo, hi = c.zone(params(band_low=0.4, band_high=0.7))
    # centre sits mid-band (fit); half-width is a fraction of the band (calm)
    check(abs((lo + hi) / 2 - 26.15) < 1e-9,
          f"centre must sit mid-band at 26.15, got {(lo + hi) / 2}")
    check(abs((hi - lo) / 2 - 0.275) < 1e-9,
          f"half-width must be half the mean band, got {(hi - lo) / 2}")
    # the centre must NOT move when only the laziness changes
    import comfort_zone.controller as cc
    was = cc.INNER_ZONE_FRAC
    try:
        for frac in (0.0, 0.25, 0.5, 0.9):
            cc.INNER_ZONE_FRAC = frac
            a, b = c.zone(params(band_low=0.4, band_high=0.7))
            check(abs((a + b) / 2 - 26.15) < 1e-9,
                  f"centre drifted to {(a + b) / 2} at frac {frac} — that is the "
                  f"conflation that cost 3.4 points of band fit")
    finally:
        cc.INNER_ZONE_FRAC = was
    # and it must sit strictly inside the band, or it parks on the boundary
    check(26.0 - 0.4 < lo and hi < 26.0 + 0.7,
          "the zone must sit strictly inside the band it is cut from")


def test_no_error_inside_the_zone_and_distance_to_the_edge_outside():
    c = fresh()
    lo, hi = c.zone(params(band_low=0.4, band_high=0.7))
    check(c.zone_error(26.1, lo, hi) == 0.0, "inside the zone is no error")
    check(c.zone_error(lo, lo, hi) == 0.0 and c.zone_error(hi, lo, hi) == 0.0,
          "the edges themselves are still comfortable")
    check(abs(c.zone_error(hi + 0.3, lo, hi) + 0.3) < 1e-9,
          "too warm must read negative, by the distance to the edge")
    check(abs(c.zone_error(lo - 0.3, lo, hi) - 0.3) < 1e-9,
          "too cold must read positive, by the distance to the edge")


def test_a_wider_band_means_less_actuator_motion():
    """The knob the user actually reasons about: band width trades fit for calm."""
    def moves(band):
        c, p = fresh(), params(band_low=band, band_high=band)
        sp, n = 26, 0
        t = T0
        # a slow warming load the loop has to chase
        for i in range(240):
            y = 25.6 + 0.004 * i
            cmd = c.tick(sig(t=t, comfort=y, setpoint=sp), p)
            if cmd.set_setpoint is not None and cmd.set_setpoint != sp:
                sp, n = cmd.set_setpoint, n + 1
            t += TICK
        return n
    tight, wide = moves(0.3), moves(0.9)
    check(wide <= tight,
          f"a wider band must not cost MORE setpoint moves: 0.3 → {tight}, 0.9 → {wide}")


def test_no_fan_available_shifts_the_zone_down_without_widening_it():
    c = fresh()
    a_lo, a_hi = c.zone(params())
    b_lo, b_hi = c.zone(params(fan_assist_enabled=False))
    check(abs((a_lo - b_lo) - 0.2) < 1e-9 and abs((a_hi - b_hi) - 0.2) < 1e-9,
          f"the whole zone must drop 0.2, got [{b_lo}, {b_hi}] from [{a_lo}, {a_hi}]")
    check(abs((a_hi - a_lo) - (b_hi - b_lo)) < 1e-9,
          "and must not widen — a band that grows is one the controller sits inside")


def test_ac_off_is_switched_back_on():
    c, p = fresh(), params()
    out = c.tick(sig(comfort=26.8, ac_on=False), p)
    check(out.set_ac_power is True, "a warm room with the AC off must power it on")
    check(out.set_setpoint is not None, "and give it a setpoint to run at")


def test_managed_off_needs_a_sustained_ask_above_the_ceiling():
    c, p = fresh(), params()
    cold, _ = run(c, p, 5, comfort=25.0, setpoint=27)
    check(cold.set_ac_power is not False,
          "five minutes of cold must not cut the compressor")
    out, _ = run(c, p, 40, comfort=24.6, setpoint=27, t0=T0 + timedelta(minutes=6))
    check(out.mode == const.MODE_MANAGED_OFF,
          f"a sustained ask above the ceiling must cut power, got {out.branch}")


def test_managed_off_returns_on_the_reading():
    c, p = fresh(), params()
    run(c, p, 40, comfort=24.6, setpoint=27)
    check(c._managed_off_since is not None, "should be in managed-off")
    back = c.tick(sig(t=T0 + timedelta(minutes=45), comfort=26.1, ac_on=False, setpoint=27), p)
    check(back.set_ac_power is True and back.branch == "managed_off.return",
          f"reaching target must hand the room back, got {back.branch}")


def test_failsafe_without_a_reading():
    c, p = fresh(), params()
    out = c.tick(sig(comfort=None), p)
    check(out.mode == const.MODE_FAILSAFE and out.set_setpoint is None,
          "no reading must actuate nothing and say so")


# --- safety, unchanged -----------------------------------------------------
def test_guard_still_overrides_and_releases_on_the_reading():
    c, p = fresh(), params()
    g = SafetyGuard()
    sp = SafetyParams(hard_min=24.0, hard_max=28.0, cooldown_min=12)
    hot = sig(comfort=28.4)
    out = g.evaluate(hot, p, sp, c.tick(hot, p))
    check(out.mode == const.MODE_SAFETY_OVERHEAT, "past the hot rail the guard takes the room")
    check(out.set_setpoint < p.setpoint_min,
          "the guard is not bound by the optimizer's floor")
    cool = sig(t=T0 + TICK, comfort=27.5)
    out2 = g.evaluate(cool, p, sp, c.tick(cool, p))
    check(out2.mode != const.MODE_SAFETY_OVERHEAT,
          "back inside the rail, the guard must hand the room straight back")


def test_rails_are_pushed_clear_of_the_band():
    lo, hi = effective_rails(26.0, 0.5, 0.8, hard_min=25.7, hard_max=26.9)
    check(lo <= 26.0 - 0.5 - const.RAIL_BAND_CLEARANCE + 1e-9,
          f"a rail inside the band must be pushed out, got {lo}")
    check(hi >= 26.0 + 0.8 + const.RAIL_BAND_CLEARANCE - 1e-9,
          f"same on the warm side, got {hi}")


def test_blower_ladder_reserves_its_top_for_the_guard():
    check(regular_blower_top(["low", "mid", "high"]) == 1, "3 levels → optimizer may use 0..1")
    check(regular_blower_top(["low", "high"]) == 0, "2 levels → optimizer may use 0 only")
    check(regular_blower_top([]) == 0, "no ladder is not an error")


# --- tuning ----------------------------------------------------------------
def test_simc_constants_are_derived_from_the_plant():
    m = model()
    tau_c = m.tau_c_mult * m.dead_time_min
    check(abs(m.kc - m.tau_min / (m.gain_per_step * (tau_c + m.dead_time_min))) < 1e-9,
          "Kc must follow SIMC")
    check(abs(m.ti_min - min(m.tau_min, 4 * (tau_c + m.dead_time_min))) < 1e-9,
          "Ti must follow SIMC")


def test_a_slower_tau_c_gives_a_gentler_loop():
    fast, slow = model(tau_c_mult=1.0), model(tau_c_mult=3.0)
    check(slow.kc < fast.kc, f"a larger tau_c must reduce Kc, {fast.kc:.2f} → {slow.kc:.2f}")


def test_a_pre_v5_model_store_is_discarded_not_merged():
    """Its keys are exactly the ones v5 treats as reviewed priors."""
    old = {"dead_time_min": 19.1, "tau_min": 8.0, "gain_per_step": 0.3,
           "power_lead_min": 6.0, "engage_watts": 400.0, "lead_min": 3.0}
    m = ModelParams.from_dict(old)
    check(m.dead_time_min == const.MODEL_DEFAULTS[const.MK_DEAD_TIME],
          f"a ratcheted dead time must not survive, got {m.dead_time_min}")
    check(m.gain_per_step == const.MODEL_DEFAULTS[const.MK_GAIN],
          f"nor a learned gain, got {m.gain_per_step}")
    fresh_store = ModelParams(gain_per_step=0.42).to_dict()
    check(ModelParams.from_dict(fresh_store).gain_per_step == 0.42,
          "but a v5 store must still load")


def test_model_params_clamp_on_load():
    m = ModelParams.from_dict({const.MK_FF_INTERCEPT: -21.5,
                               "gain_per_step": 0.01, "dead_time_min": 99.0,
                               "tau_c_mult": 0.0, "blower_gain": -1.0})
    check(m.gain_per_step == const.GAIN_MIN, "a tiny gain must clamp — it divides into Kc")
    check(m.dead_time_min == const.DEAD_MAX, "the dead time must clamp")
    check(m.tau_c_mult == const.TAU_C_MULT_MIN, "the robustness knob must clamp")
    check(m.blower_gain == 0.0, "a negative blower gain is not a cooling lever")


def test_pi_output_reports_its_own_split():
    pi = PiController(model())
    out = pi.step(error=-0.5, u_ff=25.0, dt_min=0.75, lo=24.0, hi=27.0)
    check(abs((out.u_ff + out.u_fb) - out.u_raw) < 1e-9, "u_raw must be ff + fb")
    check(out.u_fb < 0, "a warm room (negative error) must push the setpoint down")


ALL = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    passed = 0
    for t in ALL:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(ALL)} passed")
    sys.exit(0 if passed == len(ALL) else 1)
