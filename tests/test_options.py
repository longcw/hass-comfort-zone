"""Option-resolution tests: a mode is a saved profile, not a template.

Run directly:  python tests/test_options.py
or with pytest: pytest tests/
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components"))

from comfort_zone.const import (  # noqa: E402
    OPT_BAND_HIGH,
    OPT_BAND_LOW,
    OPT_FAN_MAX_DAY,
    OPT_HARD_MIN,
    OPT_SETPOINT_MIN,
    OPT_STRATEGY,
    OPTION_DEFAULTS,
    STRATEGIES,
    STRATEGY_BABY,
    STRATEGY_COMFORT,
    STRATEGY_CUSTOM,
    STRATEGY_ECO,
    STRATEGY_PRESETS,
)
from comfort_zone.options import MODE_KEYS, resolve, split_modal  # noqa: E402

# A zone as it was stored before modes became profiles: every knob in one flat
# bag, where one tuned value shadowed all four modes at once.
LEGACY = {
    OPT_STRATEGY: STRATEGY_CUSTOM,
    OPT_BAND_LOW: 0.35,
    OPT_BAND_HIGH: 0.5,
    "no_fan_offset": 0.25,
    OPT_FAN_MAX_DAY: 35.0,
    "fan_max_night": 15.0,
    "safety_margin": 1.3,
    "safety_cooldown_min": 10,
    OPT_HARD_MIN: 25.2,
    "hard_max": 27.5,
}


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def zone_of(strategy, **knobs):
    """A store-backed zone: what the entry holds, and one mode's saved knobs."""
    return {OPT_STRATEGY: strategy, OPT_HARD_MIN: 25.2}, dict(knobs)


def test_a_fresh_mode_starts_from_its_preset():
    for strategy, preset in STRATEGY_PRESETS.items():
        stored, knobs = zone_of(strategy)
        opts = resolve(stored, knobs)
        for key, want in preset.items():
            check(opts[key] == want, f"{strategy}: {key} is {opts[key]}, preset says {want}")


def test_a_mode_with_no_preset_starts_from_the_defaults():
    stored, knobs = zone_of(STRATEGY_CUSTOM)
    opts = resolve(stored, knobs)
    for key in MODE_KEYS:
        check(opts[key] == OPTION_DEFAULTS[key],
              f"custom: {key} is {opts[key]}, default is {OPTION_DEFAULTS[key]}")


def test_a_tuned_knob_is_the_modes_own():
    """The reported bug: setting band high must stay put, and stay in this mode."""
    stored, knobs = zone_of(STRATEGY_BABY, **{OPT_BAND_HIGH: 0.8})
    check(resolve(stored, knobs)[OPT_BAND_HIGH] == 0.8, "the tuned value did not take")
    check(resolve(stored, knobs)[OPT_STRATEGY] == STRATEGY_BABY,
          "tuning a knob changed the mode")
    # untuned knobs of the same mode still come from its preset
    check(resolve(stored, knobs)[OPT_BAND_LOW] == STRATEGY_PRESETS[STRATEGY_BABY][OPT_BAND_LOW],
          "an untouched knob stopped following the preset")


def test_editing_one_mode_leaves_the_others_alone():
    edited = {OPT_BAND_HIGH: 0.8, OPT_FAN_MAX_DAY: 90}
    for other in (STRATEGY_ECO, STRATEGY_COMFORT):
        stored, _ = zone_of(other)
        opts = resolve(stored, {})           # the other mode has its own (empty) knobs
        for key, value in edited.items():
            check(opts[key] != value or STRATEGY_PRESETS[other][key] == value,
                  f"a baby edit leaked into {other}: {key}={opts[key]}")


def test_switching_away_and_back_keeps_the_edit():
    saved = {STRATEGY_BABY: {OPT_BAND_HIGH: 0.8}, STRATEGY_ECO: {}}
    away = resolve({OPT_STRATEGY: STRATEGY_ECO}, saved[STRATEGY_ECO])
    check(away[OPT_BAND_HIGH] == STRATEGY_PRESETS[STRATEGY_ECO][OPT_BAND_HIGH],
          "eco did not show its own value")
    back = resolve({OPT_STRATEGY: STRATEGY_BABY}, saved[STRATEGY_BABY])
    check(back[OPT_BAND_HIGH] == 0.8, f"the baby edit was lost, got {back[OPT_BAND_HIGH]}")


def test_the_zones_own_settings_survive_every_mode():
    """Hard limits and setpoint bounds belong to the room, not to a mode."""
    for strategy in STRATEGIES:
        stored = {OPT_STRATEGY: strategy, OPT_HARD_MIN: 25.2, OPT_SETPOINT_MIN: 23}
        opts = resolve(stored, {})
        check(opts[OPT_HARD_MIN] == 25.2, f"{strategy} moved hard_min to {opts[OPT_HARD_MIN]}")
        check(opts[OPT_SETPOINT_MIN] == 23, f"{strategy} moved setpoint_min")


def test_a_stale_entry_copy_cannot_shadow_a_mode():
    """A mode knob left on the entry must lose to the mode, or the bug is back."""
    stored = {OPT_STRATEGY: STRATEGY_BABY, OPT_BAND_HIGH: 0.5}   # legacy leftover
    check(resolve(stored, {})[OPT_BAND_HIGH] == STRATEGY_PRESETS[STRATEGY_BABY][OPT_BAND_HIGH],
          "an entry-level copy still shadows the mode")


def test_a_legacy_entry_splits_without_changing_a_thing():
    zone, knobs = split_modal(LEGACY)
    check(zone[OPT_STRATEGY] == STRATEGY_CUSTOM, "the mode was renamed")
    check(OPT_BAND_HIGH not in zone, "a mode knob stayed on the entry")
    check(zone[OPT_HARD_MIN] == 25.2, "a zone-wide setting was taken away")
    # What the old flat-bag rule resolved to — the values actually running before
    # the upgrade. Splitting them out must not move one of them.
    before = {**OPTION_DEFAULTS, **STRATEGY_PRESETS.get(LEGACY[OPT_STRATEGY], {}), **LEGACY}
    after = resolve(zone, knobs)
    for key, value in before.items():
        check(after[key] == value, f"the split moved {key} {value} -> {after[key]}")


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
