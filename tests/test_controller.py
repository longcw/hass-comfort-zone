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
from comfort_zone.safety import (  # noqa: E402
    SafetyGuard, SafetyParams, rails, warn_if_inside_band,
)
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
    h = s.sp_threshold()
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
        h = SplitRange(m).sp_threshold()
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
    s.commit(T0, True, False)                    # the caller applied it
    soon = s.resolve(24.0, T0 + timedelta(minutes=2), 25, 0, p)
    check(soon.setpoint == 25 and soon.sp_blocked_by_dwell,
          f"a second move 2 min later must be blocked, got {soon.setpoint}")
    later = s.resolve(24.0, T0 + timedelta(minutes=7), 25, 0, p)
    check(later.setpoint == 24, f"after the dwell it must move, got {later.setpoint}")


def test_the_dwell_is_not_burned_by_a_move_that_was_never_issued():
    """During a guard override resolve still runs; it must not spend the pacing."""
    s, p = SplitRange(), params()
    for i in range(8):                            # 6 min of overridden ticks
        s.resolve(24.0, T0 + timedelta(seconds=45 * i), 26, 0, p)
    out = s.resolve(24.0, T0 + timedelta(seconds=45 * 8), 26, 0, p)
    check(not out.sp_blocked_by_dwell and out.setpoint == 24,
          "with nothing ever issued the dwell must still be free")


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


def test_being_pinned_at_a_limit_is_what_gets_reported_not_the_excess():
    """Live regression: 50 min stuck at the floor reported a 0.01 °C shortfall.

    Back-calculation parks the demand ON the boundary, so the excess decays away
    while the loop is every bit as stuck. The state worth reporting is the pin.
    """
    c, p = fresh(), params()
    out, sp = run(c, p, 40, comfort=26.0, outdoor=33.0, setpoint=24)
    d = out.trace
    check(d["shortfall"] < 0.1,
          f"anti-windup should have parked the demand at the limit, got "
          f"{d['shortfall']:.3f} — if this grows, back-calculation is broken")
    check(d["saturated"] and d["at_limit"] == "floor",
          f"but it IS pinned at the floor and must say so: {d['at_limit']}")
    check(sp == p.setpoint_min, f"setpoint sits at the floor, got {sp}")
    # comfortable at the limit is "no headroom", not "cannot keep up"
    check(d["in_zone"] and "no headroom" in out.reason,
          f"in-zone at the limit must not read as failure: {out.reason}")


def test_at_the_limit_and_outside_the_zone_reads_as_failing():
    c, p = fresh(), params()
    out, _ = run(c, p, 40, comfort=29.0, outdoor=33.0, setpoint=24)
    check(out.trace["saturated"] and not out.trace["in_zone"],
          "a room far too hot at the floor is both pinned and outside the zone")
    check("cannot keep up" in out.reason, f"and must say so: {out.reason}")


def test_a_loop_with_headroom_is_not_reported_as_pinned():
    c, p = fresh(), params()
    out, _ = run(c, p, 20, comfort=26.0, outdoor=26.0)
    check(not out.trace["saturated"] and out.trace["at_limit"] is None,
          f"mid-envelope must not read as pinned: u={out.trace['u']:.2f}")


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
    import comfort_zone.controller as cc
    want = (0.4 + 0.7) / 2 * cc.INNER_ZONE_FRAC
    check(abs((hi - lo) / 2 - want) < 1e-9,
          f"half-width must be {cc.INNER_ZONE_FRAC} of the mean band "
          f"({want:.4f}), got {(hi - lo) / 2:.4f}")
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


def test_leaving_the_zone_produces_an_error_worth_acting_on():
    """To the CENTRE, not the edge — an edge error enters at ~0 and stalls."""
    c = fresh()
    lo, hi = c.zone(params(band_low=0.4, band_high=0.7))
    mid = (lo + hi) / 2
    check(c.zone_error(mid, lo, hi) == 0.0, "inside the zone is no error")
    check(c.zone_error(lo, lo, hi) == 0.0 and c.zone_error(hi, lo, hi) == 0.0,
          "the edges themselves are still comfortable")
    just_out = c.zone_error(lo - 0.01, lo, hi)
    check(just_out > (hi - lo) / 2,
          f"a hair below the zone must already ask for real correction, got {just_out:.3f}")
    check(c.zone_error(hi + 0.01, lo, hi) < -(hi - lo) / 2,
          "and the warm side must be symmetric in sign")
    check(abs(c.zone_error(lo - 0.3, lo, hi) - (mid - (lo - 0.3))) < 1e-9,
          "outside, the error is the distance to the centre")


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


# --- the 2026-08-07 overcool incident --------------------------------------
# Room fell 25.85 -> 25.04, a full degree below target, over fourteen minutes in
# which the loop actuated nothing and logged nothing, then powered the AC back on
# after it had been switched off by hand. Each defect gets a test.
def test_a_room_falling_below_its_zone_gets_a_prompt_setpoint_rise():
    """The stall: a 0.81 threshold held the setpoint at 24 while the room fell."""
    c, p = fresh(), params(band_low=0.5, band_high=0.8)
    # settle the loop at the floor the way the real one was
    run(c, p, 20, comfort=26.2, outdoor=31.0, setpoint=24)
    # now the room falls steadily out of the bottom of the zone
    t0 = T0 + timedelta(minutes=21)
    sp, raised_at = 24, None
    t = t0
    while t <= t0 + timedelta(minutes=20):
        e = (t - t0).total_seconds() / 60.0
        y = 25.85 - 0.045 * e          # the measured fall, ~0.045 °C/min
        cmd = c.tick(sig(t=t, comfort=y, outdoor=31.0, setpoint=sp), p)
        if cmd.set_setpoint is not None and cmd.set_setpoint > sp:
            sp = cmd.set_setpoint
            if raised_at is None:
                raised_at = e
        t += TICK
    check(raised_at is not None, "the setpoint must rise at all as the room falls")
    check(raised_at <= 8.0,
          f"it must rise within ~8 min of leaving the zone, took {raised_at:.1f} "
          f"(live: 14 min, room reached 0.85 °C below target)")


def test_the_threshold_collapses_once_the_room_leaves_the_zone():
    m = model()
    s = SplitRange(m)
    floor = (1.0 + m.kc * m.gain_per_step) / 2.0
    calm = s.sp_threshold(0.0)
    check(calm > 0.7, f"inside the zone it must still resist ripple, got {calm:.2f}")
    check(s.sp_threshold(0.15) < calm, "it must relax as the room leaves")
    # mildly out (fully collapsed, but still less than one step's worth of room):
    # the anti-hunt floor binds, because a step could land no closer than it started
    mild = s.sp_threshold(m.gain_per_step * 0.9)
    check(abs(mild - floor) < 1e-9, f"mildly out must hold the floor, got {mild:.3f}")
    # far out: one step is unambiguously right, so the floor's argument lapses
    far = s.sp_threshold(m.gain_per_step * 2)
    check(abs(far - 0.5) < 1e-9, f"far out must reach the bare half-step, got {far:.3f}")


def test_the_controller_never_powers_the_ac_on_at_any_temperature():
    """Live: it reversed a manual power-off within one tick. Never again.

    Not conditionally, not when the room is hot — never. Restoring power belongs
    to the safety guard alone, and only for power the guard itself cut.
    """
    c, p = fresh(), params()
    for y in (20.0, 24.0, 26.0, 28.0, 32.0, 40.0):
        out = c.tick(sig(t=T0 + TICK * int(y), comfort=y, ac_on=False), p)
        check(out.set_ac_power is None,
              f"at {y} °C it must not touch AC power, got {out.set_ac_power}")
        check(out.branch == "ac.off", f"and must say the unit is off, got {out.branch}")


def test_the_controller_never_powers_the_ac_off_either():
    """It has no way to undo that, so it is not allowed to do it."""
    c, p = fresh(), params()
    for y in (20.0, 23.0, 25.0, 26.0, 30.0):
        out, _ = run(c, p, 30, comfort=y, setpoint=27)
        check(out.set_ac_power is None,
              f"at {y} °C it must not cut AC power, got {out.set_ac_power}")


def test_the_guard_restores_only_the_power_it_cut():
    c, p = fresh(), params()
    g = SafetyGuard()
    sp = SafetyParams(hard_min=24.5, hard_max=28.0, cooldown_min=12)
    cold = sig(comfort=24.0)                       # below the rail -> guard cuts power
    out = g.evaluate(cold, p, sp, c.tick(cold, p))
    check(out.set_ac_power is False, "past the cold rail the guard cuts power")
    # warmed back and past the anti-short-cycle hold -> it must restore power itself
    warm = sig(t=T0 + timedelta(minutes=20), comfort=26.0, ac_on=False)
    out2 = g.evaluate(warm, p, sp, c.tick(warm, p))
    check(out2.set_ac_power is True,
          f"the guard must put back the power it cut, got {out2.set_ac_power}")


def test_power_says_nothing_until_it_has_a_baseline():
    from comfort_zone.power import PowerLead
    pl = PowerLead()
    b, why = pl.bias(T0, 5000.0, True)
    check(b == 0.0 and why["baseline"] is None,
          "with no history it must claim nothing, however large the reading")


def _primed(hold_w=1500.0):
    """A lead that has watched the unit hold at ``hold_w`` for long enough."""
    from comfort_zone.power import PowerLead
    pl = PowerLead()
    t = T0
    for _ in range(40):
        pl.observe(t, hold_w, True)
        t += TICK
    return pl, t


def _sustain(pl, t, w, minutes=9.0):
    """Hold a deviation past PERSIST_MIN, the way a real excursion would."""
    end = t + timedelta(minutes=minutes)
    b, why = 0.0, {}
    while t <= end:
        b, why = pl.bias(t, w, True)
        t += TICK
    return b, why


def test_a_power_surge_eases_before_the_room_moves():
    pl, t = _primed()
    b, _ = _sustain(pl, t, 5000.0)
    check(b > 0, f"a sustained surge means cold is coming → ease, got {b:+.3f}")
    check(b <= 0.4 + 1e-9, f"but it is a nudge, not a decision, got {b:+.3f}")


def test_a_duty_cycle_off_phase_is_not_a_stopped_unit():
    """The positive-feedback trap: power is near zero every cycle, normally."""
    pl, t = _primed()
    b, why = _sustain(pl, t, 0.0, minutes=4.0)   # inside the measured 2-5 min off-phase
    check(b == 0.0 and why["settling"],
          f"a 4-minute off-phase must claim nothing, got {b:+.3f}")


def test_power_falling_away_predicts_warming():
    pl, t = _primed()
    b, _ = _sustain(pl, t, 0.0, minutes=12.0)    # far longer than any off-phase
    check(b < 0, f"a unit that has really stopped → cool, got {b:+.3f}")


def test_ordinary_wobble_on_a_shared_meter_claims_nothing():
    pl, t = _primed()
    for w in (1500.0, 1700.0, 1300.0, 2000.0, 1000.0):   # inside the measured noise
        b, _ = pl.bias(t, w, True)
        check(b == 0.0, f"{w:.0f} W against a 1500 W hold must claim nothing, got {b:+.3f}")


def test_power_stays_quiet_while_our_own_command_arrives():
    """The old detector's fatal flaw: reading its own step back as new evidence."""
    pl, t = _primed()
    pl.note_setpoint_change(t)
    b, why = pl.bias(t + timedelta(minutes=3), 5000.0, True)
    check(b == 0.0 and why["quiet"],
          "for a dead time after a setpoint move the surge IS our command")
    b2, _ = _sustain(pl, t + timedelta(minutes=15), 5000.0)
    check(b2 > 0, "once that has played out it may speak again")


def test_power_is_ignored_while_the_unit_is_off():
    pl, t = _primed()
    b, _ = pl.bias(t, 5000.0, False)
    check(b == 0.0, "a reading taken while the unit is off is another room's")


# --- the sustained-cold guard ----------------------------------------------
def _guard_run(minutes, comfort, p, rails=(24.5, 27.8), t0=T0):
    """Drive controller+guard over a trajectory; return (trips, releases, off_min)."""
    c, g = fresh(), SafetyGuard()
    sp_ = SafetyParams(hard_min=rails[0], hard_max=rails[1], cooldown_min=12)
    trips, releases, off_ticks, was = 0, 0, 0, "normal"
    t, sp = t0, 25
    while t <= t0 + timedelta(minutes=minutes):
        e = (t - t0).total_seconds() / 60.0
        y = comfort(e) if callable(comfort) else comfort
        sig = sig_ = Signals(now=t, comfort=y, slope=0.0, outdoor=26.0, power=1200.0,
                             ac_on=True, setpoint=sp, blower_idx=0, fan_on=False,
                             guard_active=g.state != "normal")
        out = g.evaluate(sig_, p, sp_, c.tick(sig_, p))
        if g.state == "overcool" and was != "overcool":
            trips += 1
        if was == "overcool" and g.state == "normal":
            releases += 1
        if g.state == "overcool":
            off_ticks += 1
        was = g.state
        if out.set_setpoint is not None:
            sp = out.set_setpoint
        t += TICK
    return trips, releases, off_ticks * 0.75


def test_the_sustained_cold_guard_trips_when_the_room_stays_deep_below_band():
    p = params(band_low=0.5, band_high=0.8)
    # parked 0.5 °C below the band floor, far longer than SUSTAINED_COLD_MIN
    trips, _, off_min = _guard_run(40, 25.0, p)
    check(trips >= 1, "a room parked well below its band must eventually trip the guard")
    check(off_min > 0, "and the guard must actually cut cooling")


def test_the_sustained_cold_guard_does_not_chatter_on_an_ordinary_excursion():
    """The 08-06 failure it must not reproduce: 12 power cuts in 4.5 h."""
    p = params(band_low=0.5, band_high=0.8)
    floor = p.target - p.band_low
    # a shallow dip just under the band floor, the size actually measured in replay
    trips, _, _ = _guard_run(270, lambda e: floor - 0.08, p)
    check(trips == 0,
          f"a 0.08 °C dip must never cut the compressor, got {trips} trips in 4.5 h")


def test_the_guard_hands_back_a_setpoint_not_just_power():
    """Restoring power alone resumed the setpoint that caused the overcool."""
    c, p = fresh(), params()
    g = SafetyGuard()
    sp = SafetyParams(hard_min=24.5, hard_max=28.0, cooldown_min=12)
    cut = g.evaluate(sig(comfort=24.0, setpoint=24), p, sp, c.tick(sig(comfort=24.0), p))
    check(cut.set_ac_power is False, "the guard cuts power at the rail")
    back = sig(t=T0 + timedelta(minutes=25), comfort=26.0, ac_on=False, setpoint=24)
    out = g.evaluate(back, p, sp, c.tick(back, p))
    check(out.set_ac_power is True, "and restores it")
    check(out.set_setpoint is not None and out.set_setpoint >= p.setpoint_max,
          f"with a safe setpoint, not the cold one it was cut on, got {out.set_setpoint}")


# --- the configured rails are the configured rails -------------------------
def test_the_configured_rails_are_enforced_exactly_as_set():
    """A hard limit the software adjusts is not a hard limit.

    Live 08-07: a configured cold rail of 25.2 was being enforced at 25.0 — moved
    0.2 °C in the DANGEROUS direction, silently, by a helper trying to prevent
    compressor chatter.
    """
    for hm, hx in ((25.2, 27.5), (23.0, 29.0), (25.9, 26.1), (20.0, 32.0)):
        lo, hi = rails(26.0, 0.4, 0.7, hm, hx)
        check(lo == hm and hi == hx,
              f"configured {hm}/{hx} must be enforced verbatim, got {lo}/{hi}")


def test_a_rail_close_to_the_band_is_reported_not_corrected():
    warn = warn_if_inside_band(26.0, 0.4, 0.7, hard_min=25.5, hard_max=29.0)
    check(warn is not None and "cold rail" in warn,
          f"a rail inside the ripple must be reported, got {warn!r}")
    quiet = warn_if_inside_band(26.0, 0.4, 0.7, hard_min=25.2, hard_max=27.5)
    check(quiet is None, f"and a sensibly-placed pair must not nag, got {quiet!r}")


def test_the_guard_trips_on_the_configured_rail_and_nothing_else():
    c, p = fresh(), params(band_low=0.4, band_high=0.7)
    g = SafetyGuard()
    sp = SafetyParams(hard_min=25.2, hard_max=27.5, cooldown_min=12)
    just_above = sig(comfort=25.25)
    check(not g.evaluate(just_above, p, sp, c.tick(just_above, p)).mode.startswith("safety"),
          "25.25 is above a 25.2 rail and must not trip")
    just_below = sig(t=T0 + TICK, comfort=25.15)
    out = g.evaluate(just_below, p, sp, c.tick(just_below, p))
    check(out.mode == const.MODE_SAFETY_OVERCOOL and out.set_ac_power is False,
          f"25.15 is below it and must cut power, got {out.mode}")


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
