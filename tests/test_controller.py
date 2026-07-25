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
    """If the AC ignores the command (power flat, slope flat), escalate fast."""
    c = fresh()
    p = params()
    cmd = c.tick(sig(t=T0, comfort=27.2, setpoint=26, power=800.0), p)
    check(cmd.set_setpoint == 25, "first step to 25")
    # +5 min, power unchanged, slope flat → not engaged past window(4m) → escalate
    t2 = T0 + timedelta(minutes=5)
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
    check(out.set_setpoint == 24, "overheat cools at setpoint floor")
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


def test_midzone_uses_blower_not_setpoint():
    # warm into the mid-zone (above comfort band 26.4 but below setpoint gate 26.65):
    # the blower does the work, the compressor setpoint stays put
    c = fresh()
    cmd = c.tick(sig(comfort=26.5, slope=0.0, setpoint=26, blower_idx=0), params())
    check(cmd.set_setpoint is None, f"mid-zone must NOT move the setpoint, got {cmd.set_setpoint}")
    check(cmd.set_blower_idx == 1, f"mid-zone should raise the blower to 中, got {cmd.set_blower_idx}")


def test_adapter_learns_sp_margin_from_cycling():
    from comfort_zone.adapt import OnlineAdapter
    from comfort_zone.model import ModelParams
    m = ModelParams()
    a = OnlineAdapter(m)
    kw = dict(target=26.0, band_low=0.4, band_high=0.4)  # band top 26.4
    before = m.sp_margin
    # a small (within-tolerance) excursion then recovery → widen the deadband (fewer setpoint moves)
    a.observe(T0, 26.5, 0.0, **kw)                       # 0.1 over → within tol
    a.observe(T0 + timedelta(minutes=2), 26.0, 0.0, **kw)
    check(m.sp_margin > before, f"calm excursion should widen sp_margin, {before}→{m.sp_margin}")
    # a big overshoot then recovery → tighten it back
    mid = m.sp_margin
    a.observe(T0 + timedelta(minutes=4), 27.2, 0.0, **kw)  # 0.8 over → over tol
    a.observe(T0 + timedelta(minutes=6), 26.0, 0.0, **kw)
    check(m.sp_margin < mid, f"overshoot should tighten sp_margin, {mid}→{m.sp_margin}")


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
    # a tiny overshoot (0.1 < tolerance) then recovers → lead relaxes (anti-cycle)
    mid = m.lead_min
    a.observe(T0 + timedelta(minutes=4), 26.5, 0.0, **kw)
    a.observe(T0 + timedelta(minutes=6), 26.0, 0.0, **kw)
    check(m.lead_min < mid, f"within-tolerance excursion should relax lead, {mid}→{m.lead_min}")


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
