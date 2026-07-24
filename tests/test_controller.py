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
    p = params(target=25.7, band_low=0.4, band_high=0.7)
    cmd = c.tick(sig(comfort=26.3, setpoint=25, slope=0.0), p)
    check(cmd.set_setpoint is None, f"26.3 within [25.3,26.4] must not cool, got {cmd.set_setpoint}")
    c2 = fresh()
    cmd2 = c2.tick(sig(comfort=25.2, setpoint=25, fan_on=False, slope=0.0), p)
    check(cmd2.mode == const.MODE_EASING, f"25.2 below low bound should ease, got {cmd2.mode}")


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
    cmd = c.tick(sig(comfort=25.4, setpoint=25, fan_on=True, fan_level=30), params())
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
    c = fresh()
    cmd = c.tick(sig(comfort=26.2, slope=0.1, setpoint=26), params())
    check(cmd.set_setpoint == 25, f"anticipated warm should pre-cool to 25, got {cmd.set_setpoint}")


def test_in_band_return_to_neutral():
    # below target, still falling, setpoint parked low (24) → ease it back up
    # instead of sitting on a cooling setpoint and drifting into an overcool
    c = fresh()
    cmd = c.tick(sig(comfort=25.8, slope=-0.03, setpoint=24), params(target=26.0))
    check(cmd.set_setpoint == 25, f"return-to-neutral should raise setpoint to 25, got {cmd.set_setpoint}")
    check(cmd.mode == const.MODE_EASING, f"expected easing, got {cmd.mode}")


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
