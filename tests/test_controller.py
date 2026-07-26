"""Scenario tests for the pure control core (no Home Assistant needed).

Run directly:  python tests/test_controller.py
or with pytest: pytest tests/
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components"))

from comfort_zone.controller import Command, Controller, Signals, ZoneParams  # noqa: E402
from comfort_zone.model import FopdtPredictor, ModelParams  # noqa: E402
from comfort_zone.safety import SafetyGuard, SafetyParams  # noqa: E402
from comfort_zone import const  # noqa: E402

BLOWERS = ["低风", "中风", "高风"]
T0 = datetime(2026, 7, 24, 12, 0, 0)


def params(target=26.0, band_low=0.4, band_high=0.4):
    return ZoneParams(
        target=target,
        band_low=band_low,
        band_high=band_high,
        setpoint_min=24,
        setpoint_max=27,
        blower_levels=BLOWERS,
        fan_min_level=10,
        fan_max_level=40,
        managed_off_max_min=30,
    )


def fresh():
    return Controller(FopdtPredictor(ModelParams()))


def sig(t=T0, comfort=26.0, slope=0.0, power=800.0, power_delta=0.0,
        ac_on=True, setpoint=26, blower_idx=0, fan_on=False, fan_level=None):
    return Signals(now=t, comfort=comfort, slope=slope, power=power,
                   power_delta=power_delta, ac_on=ac_on, setpoint=setpoint,
                   blower_idx=blower_idx, fan_on=fan_on, fan_level=fan_level)


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


# --- scenarios -------------------------------------------------------------

def test_in_band_is_idle():
    c = fresh()
    cmd = c.tick(sig(comfort=26.0, slope=0.0), params())
    check(cmd.set_setpoint is None, "should not touch setpoint in band")
    check(cmd.set_ac_power is None, "should not toggle AC in band")
    check(not cmd.set_fan, f"fan should stay off in band, got {cmd.set_fan}")
    check(cmd.mode == const.MODE_IDLE, f"expected idle, got {cmd.mode}")


def test_asymmetric_band():
    # target 25.7, low 0.4 (→25.3), high 0.7 (→26.4): 26.3 is still IN band,
    # but 25.2 is cold. Warm side tolerates more than cold side.
    c = fresh()
    p = params(target=25.7, band_low=0.4, band_high=0.7)  # comfort [25.3,26.4]; +sp_margin 0.25 → setpoint gate [25.05,26.65]
    cmd = c.tick(sig(comfort=26.3, setpoint=25, slope=0.0), p)
    check(cmd.set_setpoint is None, f"26.3 within the warm side must not cool, got {cmd.set_setpoint}")
    c2 = fresh()
    cmd2 = c2.tick(sig(comfort=24.9, setpoint=25, fan_on=False, slope=0.0), p)  # below lo_sp 25.05
    check(cmd2.mode == const.MODE_EASING, f"24.9 below the setpoint band should ease, got {cmd2.mode}")


def test_blower_steps_up_inside_the_band_before_the_compressor():
    # The blower is the cheapest AC lever (it modulates cold-air delivery without
    # touching the compressor), so it must act while comfort is still INSIDE the band —
    # ahead of the setpoint, not simultaneously with it.
    c = fresh()
    p = params(target=26.0, band_low=0.4, band_high=0.6)   # mid trigger 26.30, band top 26.60
    cmd = c.tick(sig(comfort=26.40, slope=0.02, setpoint=26, blower_idx=0), p)
    check(cmd.set_blower_idx == 1, f"inside the band above the mid trigger → 中风, got {cmd.set_blower_idx}")
    check(cmd.set_setpoint is None,
          f"the compressor must NOT move yet at 26.40 (band top 26.60), got {cmd.set_setpoint}")


def test_blower_holds_between_target_and_the_mid_trigger():
    # Hysteresis: up at target + band_high/2, down only at/below target. In between it
    # stays put, so it cannot chatter around the threshold.
    c = fresh()
    p = params(target=26.0, band_low=0.4, band_high=0.6)   # mid trigger 26.30
    cmd = c.tick(sig(comfort=26.15, slope=0.02, setpoint=26, blower_idx=0), p)
    check(cmd.set_blower_idx is None, f"below the mid trigger must not raise it, got {cmd.set_blower_idx}")
    cmd = c.tick(sig(comfort=26.15, slope=0.02, setpoint=26, blower_idx=1), p)
    check(cmd.set_blower_idx is None,
          f"…and must not drop it either until target, got {cmd.set_blower_idx}")


def test_blower_drops_to_low_at_or_below_target():
    # comfort below target, blower at 中 (idx1) → step down to 低 (idx0)
    c = fresh()
    cmd = c.tick(sig(comfort=25.8, setpoint=26, blower_idx=1, slope=0.0), params())
    check(cmd.set_blower_idx == 0, f"at/below target blower should drop to 低(0), got {cmd.set_blower_idx}")


def test_blower_rises_to_mid_when_warm_at_floor():
    # warm, setpoint already at floor, blower 低 → step up to 中 (regular max)
    c = fresh()
    cmd = c.tick(sig(comfort=27.0, setpoint=24, blower_idx=0, slope=0.0), params())
    check(cmd.set_blower_idx == 1, f"warm at floor should raise blower to 中(1), got {cmd.set_blower_idx}")


def test_mild_warm_uses_fan_first():
    # within band but above target → the cheap actuator (fan) engages, AC does not
    c = fresh()
    cmd = c.tick(sig(comfort=26.25, slope=0.0), params())
    check(cmd.set_setpoint is None, "mild warmth must not step the AC")
    check(cmd.set_fan is True, "mild warmth should turn the fan on")
    check(cmd.mode == const.MODE_FAN_ASSIST, f"expected fan_assist, got {cmd.mode}")


def test_warm_ac_off_powers_on():
    c = fresh()
    cmd = c.tick(sig(comfort=27.5, ac_on=False, setpoint=26), params())
    check(cmd.set_ac_power is True, "warm + AC off should power on")
    check(cmd.set_setpoint == 25, f"cold-start setpoint should be target-1=25, got {cmd.set_setpoint}")


def test_engaged_cooling_is_patient_no_churn():
    """The key anti-churn property: once cooling is engaged and in flight,
    the controller does NOT keep stepping the setpoint."""
    c = fresh()
    p = params()
    # tick 1: warm, AC on at 26 → step to 25, arm watch
    cmd = c.tick(sig(t=T0, comfort=27.2, setpoint=26, power=800.0), p)
    check(cmd.set_setpoint == 25, f"warm should step to 25, got {cmd.set_setpoint}")
    # tick 2 (+2 min): power jumped (engaged); still warm sensor → must HOLD
    t2 = T0 + timedelta(minutes=2)
    cmd = c.tick(sig(t=t2, comfort=27.1, setpoint=25, power=1100.0, power_delta=300.0), p)
    check(cmd.set_setpoint is None,
          f"engaged cooling in flight must not re-step, got {cmd.set_setpoint}")
    check(cmd.mode == const.MODE_COOLING, f"expected cooling/hold, got {cmd.mode}")
    # tick 3 (+5 min): still engaged, still warm → still HOLD (no stacking)
    t3 = T0 + timedelta(minutes=5)
    cmd = c.tick(sig(t=t3, comfort=26.9, setpoint=25, power=1100.0, power_delta=0.0), p)
    check(cmd.set_setpoint is None, f"still must not stack, got {cmd.set_setpoint}")


def test_not_engaged_escalates():
    """If the AC ignores the command (power flat, slope flat), escalate.

    Paced by physics, not by the 4-min power window: a duty-cycling unit shows no
    power rise for minutes at a time, so the window must outlast its off phase
    (dead_time × 0.8 — 8 min on the default model).
    """
    c = fresh()
    p = params()
    cmd = c.tick(sig(t=T0, comfort=27.2, setpoint=26, power=800.0), p)
    check(cmd.set_setpoint == 25, "first step to 25")
    # +9 min, power unchanged, slope flat → past the dead-time window → escalate
    t2 = T0 + timedelta(minutes=9)
    cmd = c.tick(sig(t=t2, comfort=27.2, setpoint=25, power=800.0, power_delta=0.0, slope=0.0), p)
    check(cmd.set_setpoint == 24, f"non-engagement should escalate 25→24, got {cmd.set_setpoint}")


def test_cold_eases_fan_first():
    c = fresh()
    cmd = c.tick(sig(comfort=25.0, setpoint=25, fan_on=True, fan_level=30), params())  # below lo_sp 25.35
    check(cmd.set_fan is False, "cold should ease the fan off first")
    check(cmd.set_setpoint is None, "cold should not raise setpoint while fan still on")
    check(cmd.mode == const.MODE_EASING, f"expected easing, got {cmd.mode}")


def test_overcool_managed_off_and_return():
    c = fresh()
    p = params()
    # deeply cold, at setpoint ceiling, fan already off → managed AC-off
    cmd = c.tick(sig(t=T0, comfort=25.0, setpoint=27, fan_on=False, slope=-0.05), p)
    check(cmd.set_ac_power is False, f"overcool at ceiling → managed off, got {cmd.set_ac_power}")
    check(cmd.mode == const.MODE_MANAGED_OFF, f"expected managed_off, got {cmd.mode}")
    # later it warms back to target → auto-return powers the AC on
    t2 = T0 + timedelta(minutes=10)
    cmd = c.tick(sig(t=t2, comfort=26.05, ac_on=False, setpoint=27, slope=0.03), p)
    check(cmd.set_ac_power is True, f"should auto-return AC on, got {cmd.set_ac_power}")


def test_safety_overheat_overrides():
    g = SafetyGuard()
    sp = SafetyParams(hard_min=23.0, hard_max=29.0, cooldown_min=12)
    opt = Command(mode=const.MODE_IDLE, reason="opt idle")
    out = g.evaluate(sig(comfort=29.5, setpoint=26), params(), sp, opt)
    check(out.mode == const.MODE_SAFETY_OVERHEAT, f"expected overheat, got {out.mode}")
    check(out.set_setpoint == 22, f"overheat blasts 2 below the setpoint floor, got {out.set_setpoint}")
    check(out.set_ac_power is True and out.set_fan is True, "overheat forces AC+fan on")


def test_safety_stale_with_value_holds():
    # a value present but no fresh report → freeze, don't disrupt
    g = SafetyGuard()
    sp = SafetyParams(hard_min=23.0, hard_max=29.0, cooldown_min=12)
    opt = Command(mode=const.MODE_COOLING, set_setpoint=24)
    out = g.evaluate(sig(comfort=26.0), params(), sp, opt, stale=True)
    check(out.mode == const.MODE_STALE_HOLD, f"expected stale_hold, got {out.mode}")
    check(out.set_setpoint is None and out.set_fan is None, "stale hold must change nothing")


def test_safety_unavailable_parks_at_floor():
    # no value at all → park at floor(target - band), fan off
    g = SafetyGuard()
    sp = SafetyParams(hard_min=23.0, hard_max=29.0, cooldown_min=12)
    opt = Command(mode=const.MODE_COOLING, set_setpoint=24)
    out = g.evaluate(sig(comfort=None), params(target=26.0, band_low=0.4), sp, opt, stale=True)
    check(out.mode == const.MODE_FAILSAFE, f"expected failsafe, got {out.mode}")
    check(out.set_setpoint == 25, f"park at floor(26-0.4)=25, got {out.set_setpoint}")
    check(out.set_fan is False, "failsafe stops the fan")


def test_pending_cooling_survives_stale_easing_steps():
    # v3 bug: remaining_effect is a NET sum over ~55 min, so the easing steps that
    # preceded a fresh cooling step cancelled it out and the controller concluded
    # "no cooling in flight" 45 s after commanding cooling.
    from comfort_zone.model import FopdtPredictor, ModelParams
    m = ModelParams(dead_time_min=15.3, tau_min=8.0, gain_per_step=0.164)
    p = FopdtPredictor(m)
    p.record_setpoint_change(T0, -1)                            # 19:45 cool
    p.record_setpoint_change(T0 + timedelta(minutes=29), +2)     # 20:14 ease
    p.record_setpoint_change(T0 + timedelta(minutes=38), +1)     # 20:23 ease
    p.record_setpoint_change(T0 + timedelta(minutes=48), -1)     # 20:33 cool
    now = T0 + timedelta(minutes=48, seconds=45)                 # 20:34, 45 s later
    check(p.has_pending_cooling(now),
          f"a cooling step 45 s old must count as in flight (net remaining "
          f"{p.remaining_effect(now):+.3f} is cancelled by stale easing steps)")


def test_power_trend_ignores_the_duty_cycle():
    # The fan feedforward asks "is more cooling arriving?". Point-sampling that on a
    # duty-cycled load made a normal 2-5 min off-phase look like the AC backing off,
    # so the fan sped up for no reason — 14 hard sign flips across one night.
    from comfort_zone.model import power_trend
    hist = []
    t = T0
    for _cycle in range(8):                    # steady output: 6 min on, 3 min off
        for _ in range(8):
            hist.append((t, 650.0))
            t += timedelta(seconds=45)
        for _ in range(4):
            hist.append((t, 2.0))
            t += timedelta(seconds=45)
    from comfort_zone.controller import FF_TRIGGER_MULT
    trigger = 150.0 * FF_TRIGGER_MULT      # engage_watts default × the fan's multiplier
    worst = 0.0
    for i in range(40, len(hist)):
        d = power_trend(hist[:i], hist[i - 1][0], 12.0)
        if d is not None:
            worst = max(worst, abs(d))
    check(worst < trigger,
          f"steady duty-cycled output must not reach the fan's feedforward trigger "
          f"({trigger:.0f} W), worst |Δ| = {worst:.0f} W")


def test_power_trend_still_sees_a_real_ramp():
    from comfort_zone.model import power_trend
    hist = []
    t = T0
    for _ in range(20):                        # baseline
        hist.append((t, 650.0))
        t += timedelta(seconds=45)
    for _ in range(20):                        # unit ramps up and stays there
        hist.append((t, 1300.0))
        t += timedelta(seconds=45)
    from comfort_zone.controller import FF_TRIGGER_MULT
    d = power_trend(hist, hist[-1][0], 12.0)
    check(d is not None and d > 150.0 * FF_TRIGGER_MULT,
          f"a sustained +650 W ramp must clear the feedforward trigger, got {d}")


def test_power_trend_unknown_without_history():
    from comfort_zone.model import power_trend
    check(power_trend([], T0, 12.0) is None, "no history → unknown, not zero")
    thin = [(T0, 700.0), (T0 + timedelta(seconds=45), 700.0)]
    check(power_trend(thin, thin[-1][0], 12.0) is None, "too little history → unknown")


def test_off_phase_must_not_read_as_a_failed_command():
    # Measured: this VRF duty-cycles ~18 min on / 2-5 min off. Four minutes after a
    # step, "no power rise" is a normal off phase — and with a 17-min dead time the
    # slope cannot have answered either. v3.1 called that "not engaged" and escalated
    # after nearly every step, which is what drove the 15-min limit cycle.
    c = fresh()
    c.predictor.params.dead_time_min = 17.0
    p = params(target=26.1, band_low=0.4, band_high=0.65)
    first = c.tick(sig(t=T0, comfort=26.9, slope=0.03, power=670.0, setpoint=26), p)
    check(first.set_setpoint == 25, f"warm room should step, got {first.set_setpoint}")
    out = c.tick(sig(t=T0 + timedelta(minutes=4, seconds=30), comfort=26.95, slope=0.03,
                     power=2.0, setpoint=25), p)   # compressor in an off phase
    check(out.set_setpoint is None,
          f"an off-phase 4.5 min in must not escalate (window is dead-time paced), "
          f"got {out.set_setpoint}: {out.reason}")


def test_flat_power_while_running_escalates_fast():
    # Power keeps its fast veto — it just has to compare RUNNING levels. Once we have
    # watched the unit actually run for a couple of minutes at an unchanged level, the
    # command demonstrably didn't take, and we escalate without waiting out the dead time.
    c = fresh()
    c.predictor.params.dead_time_min = 17.0
    p = params(target=26.1, band_low=0.4, band_high=0.65)
    first = c.tick(sig(t=T0, comfort=26.9, slope=0.03, power=670.0, setpoint=26), p)
    check(first.set_setpoint == 25, f"warm room should step, got {first.set_setpoint}")
    out = None
    for i in range(1, 7):                       # ticks every 45 s, unit running, level flat
        out = c.tick(sig(t=T0 + timedelta(seconds=45 * i), comfort=26.95, slope=0.03,
                         power=665.0, setpoint=25), p)
    check(out.set_setpoint == 24,
          f"flat running level ⇒ the step didn't take ⇒ escalate well before the "
          f"dead time (4.5 min in), got {out.set_setpoint}: {out.reason}")


def test_power_ramp_grants_patience():
    # The mirror case, which v3.1 got wrong by reading a point sample of 1 W: the unit
    # ramped hard after the command, so be patient rather than stacking another step.
    c = fresh()
    c.predictor.params.dead_time_min = 17.0
    p = params(target=26.1, band_low=0.4, band_high=0.65)
    c.tick(sig(t=T0, comfort=26.9, slope=0.03, power=640.0, setpoint=26), p)
    out = None
    for i in range(1, 9):
        out = c.tick(sig(t=T0 + timedelta(seconds=45 * i), comfort=26.95, slope=0.0,
                         power=1058.0, setpoint=25), p)
    check(out.set_setpoint is None,
          f"a +418 W ramp is engagement — must not stack another step, "
          f"got {out.set_setpoint}: {out.reason}")


def test_no_power_signal_falls_back_to_the_dead_time_window():
    # With no usable power at all, the slope is the only evidence and it cannot answer
    # before the dead time — so wait, then escalate.
    c = fresh()
    c.predictor.params.dead_time_min = 17.0
    p = params(target=26.1, band_low=0.4, band_high=0.65)
    c.tick(sig(t=T0, comfort=26.9, slope=0.03, power=None, setpoint=26), p)
    early = c.tick(sig(t=T0 + timedelta(minutes=4, seconds=30), comfort=26.95,
                       slope=0.03, power=None, setpoint=25), p)
    check(early.set_setpoint is None, f"must not escalate blind at 4.5 min, got {early.set_setpoint}")
    late = c.tick(sig(t=T0 + timedelta(minutes=14), comfort=27.0, slope=0.03,
                      power=None, setpoint=25), p)
    check(late.set_setpoint == 24,
          f"after the dead-time window a flat response should escalate, "
          f"got {late.set_setpoint}: {late.reason}")


def test_warm_side_setpoint_acts_at_the_band_edge():
    # v3.1 let the learned deadband widen the warm gate to 27.25 with the rail at
    # 27.5, so the room was allowed to sit warm. Comfort first: on the warm side the
    # setpoint acts at the band edge, whatever the learner has decided.
    c = fresh()
    c.predictor.params.sp_margin = 0.45
    p = params(target=26.1, band_low=0.4, band_high=0.65)   # band top 26.75
    cmd = c.tick(sig(t=T0, comfort=26.80, slope=0.02, power=700.0, setpoint=26), p)
    check(cmd.set_setpoint == 25,
          f"just above the band top must cool even with sp_margin 0.45, got "
          f"{cmd.set_setpoint}: {cmd.reason}")


def test_deadband_never_eats_the_rail_clearance():
    # hard_min 25.2 sits 0.2 under the band floor — less than the keep-out — so the
    # learned deadband must be fully suppressed on that side rather than inviting a trip.
    c = fresh()
    c.predictor.params.sp_margin = 0.45
    p = params(target=26.1, band_low=0.4, band_high=0.65)
    p.hard_min, p.hard_max = 25.2, 27.5
    cmd = c.tick(sig(t=T0, comfort=25.65, slope=-0.02, power=700.0, setpoint=25, fan_on=False), p)
    check(cmd.mode == const.MODE_EASING,
          f"below the band floor (25.70) must ease, not sit inside a learned deadband, "
          f"got {cmd.mode}: {cmd.reason}")


def test_learned_params_are_clamped_on_load():
    # A param learned under older/buggier rules must not stay out of range forever:
    # gain froze at 0.164 because no episode could mature, and lead was left at the
    # retired cap of 8.0.
    from comfort_zone.model import ModelParams
    from comfort_zone import const
    from comfort_zone.adapt import GAIN_MIN
    m = ModelParams.from_dict({
        "dead_time_min": 17.0, "tau_min": 8.0, "gain_per_step": 0.164,
        "power_lead_min": 6.0, "engage_watts": 150.0, "engage_window_min": 4.0,
        "lead_min": 8.0, "sp_margin": 0.35,
    })
    check(m.gain_per_step >= GAIN_MIN, f"gain must load clamped to >= {GAIN_MIN}, got {m.gain_per_step}")
    check(m.lead_min <= const.LEAD_CAP, f"lead must load clamped to <= {const.LEAD_CAP}, got {m.lead_min}")


def test_overheat_blasts_below_the_normal_floor():
    # The rail guard is not the optimizer: at the hot rail it may go colder than the
    # optimizer's own floor to pull the room back.
    g = SafetyGuard()
    sp = SafetyParams(hard_min=25.2, hard_max=27.5, cooldown_min=12)
    p = params()                      # setpoint_min 24
    out = g.evaluate(sig(comfort=27.6, setpoint=25), p, sp, Command())
    check(out.mode == const.MODE_SAFETY_OVERHEAT, f"expected overheat, got {out.mode}")
    check(out.set_setpoint == 22, f"overheat should blast to setpoint_min-2=22, got {out.set_setpoint}")
    # …but never below what the device accepts
    p.setpoint_device_min = 23
    out = g.evaluate(sig(comfort=27.9, setpoint=25), p, sp, Command())
    check(out.set_setpoint == 23, f"must respect the device floor 23, got {out.set_setpoint}")


def _live_v3_controller():
    """The controller carrying the model the live system had learned by 07-25 22:00."""
    c = fresh()
    c.predictor.params.dead_time_min = 15.3
    c.predictor.params.gain_per_step = 0.164
    c.predictor.params.lead_min = 8.0     # was pinned at the old LEAD_CAP
    c.predictor.params.sp_margin = 0.1
    return c


def test_anticipation_does_not_step_from_deep_inside_the_band():
    # the real 20:33 trigger: comfort 25.97 with target 25.8 — only +0.17 over
    # target, well inside the band — yet a saturated lead pushed the forecast
    # 0.01 °C past the setpoint gate and moved the compressor.
    c = _live_v3_controller()
    p = params(target=25.8, band_low=0.4, band_high=0.65)
    cmd = c.tick(sig(t=T0, comfort=25.97, slope=0.0738, power=736.0, setpoint=26), p)
    check(cmd.set_setpoint is None,
          f"must not move the compressor from inside the band, got {cmd.set_setpoint}: {cmd.reason}")


def test_no_setpoint_double_step_within_dwell():
    # a genuinely warm room may step — but not twice inside the dwell, which is
    # how 26→25→24 happened in 45 s and drove a 1.1 °C dive into an overcool trip
    c = _live_v3_controller()
    p = params(target=25.8, band_low=0.4, band_high=0.65)
    # stale easing steps still inside the ~55-min prediction horizon: these are
    # what cancelled the fresh cooling step in the net-sum view
    c.predictor.record_setpoint_change(T0 - timedelta(minutes=19), +2)
    c.predictor.record_setpoint_change(T0 - timedelta(minutes=10), +1)
    first = c.tick(sig(t=T0, comfort=26.70, slope=0.05, power=736.0, setpoint=26), p)
    check(first.set_setpoint == 25, f"a warm room should step once, got {first.set_setpoint}")
    second = c.tick(sig(t=T0 + timedelta(seconds=45), comfort=26.75, slope=0.05,
                        power=736.0, setpoint=25), p)
    check(second.set_setpoint is None,
          f"must not step again 45 s later (dwell {c._step_dwell():.1f}m), got "
          f"{second.set_setpoint}: {second.reason}")


def test_lead_not_ratcheted_by_an_unpreventable_excursion():
    # hot evening, compressor already on its floor: the room is out of band because
    # the AC has nothing left to give. Anticipation could not have prevented it, so
    # scoring it is what walked the lead to its cap.
    from comfort_zone.adapt import OnlineAdapter
    from comfort_zone.model import ModelParams
    kw = dict(target=25.8, band_low=0.4, band_high=0.65, hard_min=25.2, hard_max=27.5)
    a = OnlineAdapter(ModelParams())
    before = a.params.lead_min
    a.observe(T0, 27.24, 0.0, saturated=True, **kw)                    # 0.79 over band
    a.observe(T0 + timedelta(minutes=5), 26.0, 0.0, saturated=True, **kw)
    check(a.params.lead_min == before,
          f"a saturated excursion must not move the lead, {before}→{a.params.lead_min}")


def test_gain_not_learned_from_saturated_episode():
    # at the setpoint floor with the room still warm, cooling CANNOT move the room:
    # that is actuator saturation, not a small plant gain — learning from it dragged
    # gain to its floor and made the controller over-escalate later
    from comfort_zone.adapt import OnlineAdapter
    from comfort_zone.model import ModelParams
    m = ModelParams()
    a = OnlineAdapter(m)
    before = m.gain_per_step
    a.on_setpoint_command(T0, delta_c=-1, comfort=27.2, at_floor=True)
    a.observe(T0 + timedelta(minutes=40), comfort=27.25, slope=0.0)   # matured, no drop
    check(m.gain_per_step == before,
          f"a saturated episode must teach nothing about gain, {before}→{m.gain_per_step}")
    # …but an unsaturated episode that genuinely failed to cool still shrinks gain
    a.on_setpoint_command(T0 + timedelta(hours=2), delta_c=-1, comfort=27.2, at_floor=False)
    a.observe(T0 + timedelta(hours=2, minutes=40), comfort=27.22, slope=0.0)
    check(m.gain_per_step < before,
          f"a clean no-response episode should still shrink gain, got {m.gain_per_step}")


def test_lead_tolerance_is_tighter_on_the_cold_side():
    # the cold rail sits ~0.2 °C under the band, the hot rail ~1.05 °C over it, so the
    # same 0.2 °C excursion is "too deep" cold-side and fine warm-side
    from comfort_zone.adapt import OnlineAdapter
    from comfort_zone.model import ModelParams
    kw = dict(target=25.8, band_low=0.4, band_high=0.65, hard_min=25.2, hard_max=27.5)
    a = OnlineAdapter(ModelParams())
    lead0 = a.params.lead_min
    a.observe(T0, 25.20, 0.0, **kw)                              # 0.2 below band
    a.observe(T0 + timedelta(minutes=2), 25.8, 0.0, **kw)        # closed
    check(a.params.lead_min > lead0, f"cold excursion of 0.2 must raise lead, got {a.params.lead_min}")
    b = OnlineAdapter(ModelParams())
    lead0 = b.params.lead_min
    b.observe(T0, 26.65, 0.0, **kw)                              # the SAME 0.2, warm side
    b.observe(T0 + timedelta(minutes=2), 25.8, 0.0, **kw)        # closed
    check(b.params.lead_min <= lead0,
          f"the same 0.2 excursion is tolerable on the warm side (rail is 1.05 away), "
          f"must not raise lead, got {b.params.lead_min}")


def test_lead_moves_both_ways():
    # v3 pinned lead at LEAD_CAP because OVERSHOOT_TOL (0.15) was tighter than
    # anything this room achieves, so the relax branch never fired. The learner
    # must be able to walk back down, not just up.
    from comfort_zone.adapt import OnlineAdapter
    from comfort_zone.model import ModelParams
    from comfort_zone import const
    kw = dict(target=25.8, band_low=0.4, band_high=0.65, hard_min=25.2, hard_max=27.5)
    a = OnlineAdapter(ModelParams())
    a.params.lead_min = const.LEAD_CAP            # start where v3 got stuck
    t = T0
    for _ in range(4):                             # excursions inside tolerance
        a.observe(t, 26.60, 0.0, **kw)             # 0.15 over a 0.35 tolerance
        a.observe(t + timedelta(minutes=2), 25.8, 0.0, **kw)
        t += timedelta(minutes=20)
    check(a.params.lead_min < const.LEAD_CAP - 1.0,
          f"calm excursions must walk the lead back down from the cap, got {a.params.lead_min}")
    down = a.params.lead_min
    for _ in range(3):                             # now deep ones
        a.observe(t, 27.30, 0.0, **kw)             # 0.85 over → severity-scaled
        a.observe(t + timedelta(minutes=2), 25.8, 0.0, **kw)
        t += timedelta(minutes=20)
    check(a.params.lead_min > down,
          f"deep excursions must raise it again, {down}→{a.params.lead_min}")


def test_sp_margin_follows_the_cycling_rate():
    # sp_margin is the anti-cycling knob, so it must be driven by observed compressor
    # motion (its own feedback loop) — not by overshoot, which also drives lead and
    # left the pair with no stable interior point.
    from comfort_zone.adapt import OnlineAdapter
    from comfort_zone.model import ModelParams
    a = OnlineAdapter(ModelParams())
    a.observe(T0, 25.8, 0.0)                       # start observing
    before = a.params.sp_margin
    t = T0
    for i in range(8):                             # 8 setpoint moves in an hour
        t = T0 + timedelta(minutes=7 * (i + 1))
        a.on_setpoint_command(t, delta_c=-1 if i % 2 else 1, comfort=26.0)
        a.observe(t, 26.0, 0.0)
    a.observe(T0 + timedelta(minutes=75), 26.0, 0.0)
    check(a.params.sp_margin > before,
          f"heavy cycling must widen the deadband, {before}→{a.params.sp_margin}")
    # now go quiet for hours → tighten back to buy comfort
    mid = a.params.sp_margin
    for i in range(1, 12):
        a.observe(T0 + timedelta(minutes=75 + 20 * i), 26.0, 0.0)
    check(a.params.sp_margin < mid,
          f"a quiet stretch must tighten the deadband, {mid}→{a.params.sp_margin}")


def test_safety_overcool_releases_once_warm_again():
    # The reported bug: the guard tripped cold, the room warmed back well past
    # the release point, and the guard kept the AC off anyway (a hidden timer).
    g = SafetyGuard()
    sp = SafetyParams(hard_min=25.2, hard_max=29.0, cooldown_min=12)
    p = params()
    out = g.evaluate(sig(t=T0, comfort=24.9, ac_on=True), p, sp, Command())
    check(out.mode == const.MODE_SAFETY_OVERCOOL, f"expected overcool trip, got {out.mode}")
    check(out.set_ac_power is False, "overcool must cut AC power")
    # 10 min later the room is at 26.19 — way above release (25.5): hand back NOW
    t = T0 + timedelta(minutes=10)
    opt = Command(mode=const.MODE_COOLING, reason="opt cooling", set_setpoint=25)
    out = g.evaluate(sig(t=t, comfort=26.19, ac_on=False), p, sp, opt)
    check(out.mode != const.MODE_SAFETY_OVERCOOL,
          f"warm again ({26.19} ≥ 25.5) must release the guard, got {out.mode}: {out.reason}")
    check(out.set_ac_power is True, f"hand back with the AC powered on, got {out.set_ac_power}")
    check(g.state == "normal", f"guard should be back to normal, got {g.state}")


def test_safety_overcool_short_hold_is_honest():
    # A brief anti-short-cycle hold is fine, but the reason must say so instead
    # of claiming it is waiting for a temperature it has already reached.
    g = SafetyGuard()
    sp = SafetyParams(hard_min=25.2, hard_max=29.0, cooldown_min=12)
    p = params()
    g.evaluate(sig(t=T0, comfort=24.9), p, sp, Command())
    out = g.evaluate(sig(t=T0 + timedelta(seconds=45), comfort=26.19, ac_on=False), p, sp, Command())
    check(out.mode == const.MODE_SAFETY_OVERCOOL, "should still protect the compressor right after cutting power")
    check("holding until" not in out.reason,
          f"must not claim it is waiting for comfort ≥ 25.5 when comfort is 26.19: {out.reason}")
    check("compressor" in out.reason, f"the reason must name the real hold: {out.reason}")


def test_safety_overheat_releases_as_soon_as_back_under_rail():
    # Holding "full cool at setpoint floor" after the room is back under the
    # rail is what drives the overshoot into the opposite guard.
    g = SafetyGuard()
    sp = SafetyParams(hard_min=23.0, hard_max=29.0, cooldown_min=12)
    p = params()
    out = g.evaluate(sig(t=T0, comfort=29.5), p, sp, Command())
    check(out.mode == const.MODE_SAFETY_OVERHEAT, f"expected overheat trip, got {out.mode}")
    t = T0 + timedelta(minutes=3)
    opt = Command(mode=const.MODE_COOLING, reason="opt cooling")
    out = g.evaluate(sig(t=t, comfort=28.5, setpoint=24), p, sp, opt)
    check(out is opt, f"back under the rail (28.5 ≤ 28.7) must hand back at once, got {out.mode}: {out.reason}")


def test_safety_overcool_retrip_needs_a_deeper_dip():
    # After handing back, a hair below the rail must not immediately re-cut
    # power (that is the flapping the cooldown exists to prevent) — but a real
    # dip still trips.
    g = SafetyGuard()
    sp = SafetyParams(hard_min=25.2, hard_max=29.0, cooldown_min=12)
    p = params()
    g.evaluate(sig(t=T0, comfort=24.9), p, sp, Command())
    g.evaluate(sig(t=T0 + timedelta(minutes=6), comfort=25.6, ac_on=False), p, sp, Command())
    check(g.state == "normal", f"should have released, got {g.state}")
    t = T0 + timedelta(minutes=8)
    out = g.evaluate(sig(t=t, comfort=25.15), p, sp, Command(mode=const.MODE_IDLE))
    check(out.mode != const.MODE_SAFETY_OVERCOOL, f"0.05°C dip within cooldown should not re-trip, got {out.reason}")
    out = g.evaluate(sig(t=t, comfort=24.8), p, sp, Command(mode=const.MODE_IDLE))
    check(out.mode == const.MODE_SAFETY_OVERCOOL, f"a real dip must still trip, got {out.mode}")


def test_safety_overheat_always_trips_immediately():
    # The hot side gets no re-trip grace: heat is the dangerous direction.
    g = SafetyGuard()
    sp = SafetyParams(hard_min=23.0, hard_max=29.0, cooldown_min=12)
    p = params()
    g.evaluate(sig(t=T0, comfort=29.5), p, sp, Command())
    g.evaluate(sig(t=T0 + timedelta(minutes=2), comfort=28.6), p, sp, Command())
    check(g.state == "normal", f"should have released, got {g.state}")
    out = g.evaluate(sig(t=T0 + timedelta(minutes=3), comfort=29.05), p, sp, Command())
    check(out.mode == const.MODE_SAFETY_OVERHEAT, f"any excursion past hard_max must trip, got {out.mode}")


def test_fan_disabled_never_runs_fan():
    c = fresh()
    p = params()
    p.fan_assist_enabled = False
    # mildly warm — would normally turn the fan on
    cmd = c.tick(sig(comfort=26.3, fan_on=False), p)
    check(cmd.set_fan is not True, f"fan must not turn on when fan-assist disabled, got {cmd.set_fan}")
    # and if it were on, it gets turned off
    cmd = c.tick(sig(comfort=26.3, fan_on=True), p)
    check(cmd.set_fan is False, "fan-assist disabled should turn a running fan off")


def test_adapter_learns_gain():
    from comfort_zone.adapt import OnlineAdapter
    from comfort_zone.model import ModelParams
    m = ModelParams()  # gain 0.5, dead 10, tau 8
    a = OnlineAdapter(m)
    a.on_setpoint_command(T0, delta_c=-1, comfort=27.0)
    # a 1°C step that actually dropped comfort ~1.0°C → realized gain ~1.0 > 0.5
    mature = T0 + timedelta(minutes=30)
    # feed a falling slope so dead-time is captured
    a.observe(T0 + timedelta(minutes=6), comfort=26.7, slope=-0.05)
    changed = a.observe(mature, comfort=26.0, slope=-0.01)
    check(changed, "adapter should update on a matured episode")
    check(m.gain_per_step > 0.5, f"gain should rise toward realized ~1.0, got {m.gain_per_step}")


def test_safety_normal_passthrough():
    g = SafetyGuard()
    sp = SafetyParams(hard_min=23.0, hard_max=29.0, cooldown_min=12)
    opt = Command(mode=const.MODE_COOLING, set_setpoint=25, reason="opt")
    out = g.evaluate(sig(comfort=26.0), params(), sp, opt)
    check(out is opt, "normal conditions must pass the optimizer command through unchanged")


def test_anticipation_pre_cools():
    # in-band (26.2) but rising; with the default lead the forecast crosses the
    # band top → start cooling early instead of waiting for the overshoot
    c = fresh()  # in comfort band (26.3<hi 26.4) but rising hard → y_ahead crosses the wider setpoint gate 26.65
    cmd = c.tick(sig(comfort=26.3, slope=0.2, setpoint=26), params())
    check(cmd.set_setpoint == 25, f"anticipated warm should pre-cool to 25, got {cmd.set_setpoint}")


def test_in_band_return_to_neutral():
    # below target, still falling, setpoint parked low (24) → ease it back up
    # instead of sitting on a cooling setpoint and drifting into an overcool
    c = fresh()
    cmd = c.tick(sig(comfort=25.8, slope=-0.03, setpoint=24), params(target=26.0))
    check(cmd.set_setpoint == 25, f"return-to-neutral should raise setpoint to 25, got {cmd.set_setpoint}")
    check(cmd.mode == const.MODE_EASING, f"expected easing, got {cmd.mode}")


def test_warm_side_has_no_midzone_the_blower_assists():
    # There is no warm mid-zone any more: above the band the setpoint acts (comfort
    # first — tolerating warmth is what blew the sd) and the blower assists in the
    # same tick. The cost ordering survives as the dwell asymmetry: the blower may
    # step every 3 min, the compressor only every ~9.
    c = fresh()
    cmd = c.tick(sig(comfort=26.5, slope=0.0, setpoint=26, blower_idx=0), params())
    check(cmd.set_setpoint == 25, f"above the band the setpoint must act, got {cmd.set_setpoint}")
    check(cmd.set_blower_idx == 1, f"the blower should assist to 中, got {cmd.set_blower_idx}")


def test_cold_side_keeps_its_midzone():
    # The deadband survives on the cold side, where it buys fewer AC power cycles —
    # provided the rail is far enough away to afford it (here hard_min is 23.0).
    c = fresh()
    c.predictor.params.sp_margin = 0.5
    p = params(target=26.0, band_low=0.4, band_high=0.4)   # band floor 25.6
    p.hard_min, p.hard_max = 23.0, 29.0
    cmd = c.tick(sig(comfort=25.45, slope=0.0, setpoint=26, fan_on=False), p)
    check(cmd.set_setpoint is None,
          f"just below the band floor must sit in the cold deadband, got {cmd.set_setpoint}")


def test_excursion_depth_only_tightens_sp_margin():
    # sp_margin is no longer driven UP by calm excursions (that coupled it to the
    # lead learner and left the pair with no interior fixed point) — the cycling
    # rate widens it. A *deep* excursion is a comfort failure and still tightens it.
    from comfort_zone.adapt import OnlineAdapter
    from comfort_zone.model import ModelParams
    m = ModelParams()
    a = OnlineAdapter(m)
    kw = dict(target=26.0, band_low=0.4, band_high=0.4, hard_min=25.2, hard_max=27.5)
    before = m.sp_margin
    a.observe(T0, 26.5, 0.0, **kw)                        # 0.1 over → within tolerance
    a.observe(T0 + timedelta(minutes=2), 26.0, 0.0, **kw)
    check(m.sp_margin == before, f"a calm excursion must leave sp_margin alone, {before}→{m.sp_margin}")
    a.observe(T0 + timedelta(minutes=4), 27.4, 0.0, **kw)  # 1.0 over → deep
    a.observe(T0 + timedelta(minutes=6), 26.0, 0.0, **kw)
    check(m.sp_margin < before, f"a deep excursion should tighten sp_margin, {before}→{m.sp_margin}")


def test_adapter_learns_lead_from_overshoot():
    from comfort_zone.adapt import OnlineAdapter
    from comfort_zone.model import ModelParams
    m = ModelParams()
    a = OnlineAdapter(m)
    kw = dict(target=26.0, band_low=0.4, band_high=0.4)  # band top 26.4
    # a big overshoot (peaks 0.6 over the band) then recovers → lead increases
    a.observe(T0, 27.0, 0.0, **kw)
    before = m.lead_min
    a.observe(T0 + timedelta(minutes=2), 26.0, 0.0, **kw)
    check(m.lead_min > before, f"overshoot should raise lead, {before}→{m.lead_min}")
    # an overshoot inside tolerance but not comfortably so → HOLD: this is the
    # target, and without that hold zone the lead random-walks to a limit
    mid = m.lead_min
    a.observe(T0 + timedelta(minutes=8), 26.5, 0.0, **kw)   # 0.10 over a 0.15 tolerance
    a.observe(T0 + timedelta(minutes=10), 26.0, 0.0, **kw)
    check(m.lead_min == mid, f"an on-target excursion should hold the lead, {mid}→{m.lead_min}")
    # a comfortably-inside overshoot then recovers → lead relaxes (anti-cycle)
    mid = m.lead_min
    a.observe(T0 + timedelta(minutes=12), 26.43, 0.0, **kw)  # 0.03 over → well inside
    a.observe(T0 + timedelta(minutes=14), 26.0, 0.0, **kw)
    check(m.lead_min < mid, f"a comfortably-inside excursion should relax lead, {mid}→{m.lead_min}")


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
