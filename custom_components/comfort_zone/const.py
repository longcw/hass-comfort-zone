"""Constants and defaults for the Comfort Zone integration."""
from __future__ import annotations

from typing import Final

DOMAIN: Final = "comfort_zone"
PLATFORMS: Final = ["sensor", "select", "number", "switch"]

# How often the control loop runs.
TICK_SECONDS: Final = 45

# ---------------------------------------------------------------------------
# Config-entry keys (set once at setup; the entity bindings)
# ---------------------------------------------------------------------------
CONF_NAME: Final = "name"

# Regulated signal: either bind an existing sensor, or compute from T + RH.
CONF_COMFORT_SOURCE: Final = "comfort_source"      # "sensor" | "compute"
CONF_COMFORT_SENSOR: Final = "comfort_sensor"      # source == "sensor"
CONF_TEMP_SENSOR: Final = "temp_sensor"            # source == "compute"
CONF_HUMIDITY_SENSOR: Final = "humidity_sensor"    # source == "compute"

# Actuators / signals.
CONF_AC_CLIMATE: Final = "ac_climate"              # the climate entity we drive
CONF_AC_POWER_SWITCH: Final = "ac_power_switch"    # reliable on/off switch (NOT hvac mode)
CONF_AC_POWER_SENSOR: Final = "ac_power_sensor"    # whole-system power (soft feedforward)
CONF_FAN: Final = "fan"                            # circulation fan (optional)
CONF_FAN_SPEED_NUMBER: Final = "fan_speed_number"  # non-lossy speed_level number (optional)
# Outdoor temperature and hourly forecast, for the reset curve. Optional: without it
# the loop runs on feedback alone, which is slower but converges to the same place.
CONF_WEATHER: Final = "weather"

# ---------------------------------------------------------------------------
# Options (tunable at runtime via options flow / number entities / card)
# ---------------------------------------------------------------------------
OPT_STRATEGY: Final = "strategy"
OPT_SCHEDULE: Final = "schedule"                   # list[float] len 48 (30-min points), °C

# Comfort-signal params (compute mode).
OPT_COMFORT_K: Final = "comfort_k"                 # humidity weight, 0..1
OPT_COMFORT_RH_REF: Final = "comfort_rh_ref"       # anchor RH (signal == raw temp here)

# The comfort band, asymmetric: [ target - band_low , target + band_high ].
#
# The band is NOT a control input. A PI controller tracks the target; there is no arm
# that waits for an edge to be crossed. The band's two remaining jobs are to define
# the fit metric the whole design is optimised for, and to keep the safety rails clear
# of ordinary tracking ripple (see RAIL_RIPPLE). Widening it is still the
# user's knob for trading fit against compressor motion — it just acts by changing
# what counts as a miss, rather than by opening a deadband the controller sits inside.
OPT_BAND_LOW: Final = "band_low"                   # °C below target
OPT_BAND_HIGH: Final = "band_high"                 # °C above target
# Without air movement, warmth is less tolerable — which under a PI is a shift of the
# TARGET, not a change of band. (As a band it silently widened the warm gate, which is
# how the room parked at 27.25 on 07-26.) Lower the target by this much whenever no
# fan can run, whether switched off or capped to zero for the window.
OPT_NO_FAN_OFFSET: Final = "no_fan_offset"
OPT_SETPOINT_MIN: Final = "setpoint_min"
OPT_SETPOINT_MAX: Final = "setpoint_max"

# Fan comfort layer. A cap of 0 means "no fan in this window" — the same as
# switching fan-assist off, warm-side band included.
OPT_FAN_MIN_LEVEL: Final = "fan_min_level"         # speed_level floor when on
OPT_FAN_MAX_DAY: Final = "fan_max_day"
OPT_FAN_MAX_NIGHT: Final = "fan_max_night"

# Highest AC blower the optimizer may use, as a ladder index: 0 = low, 1 = mid.
# The ladder's top level stays reserved for the safety guard, which no cap binds.
OPT_BLOWER_MAX_DAY: Final = "blower_max_day"
OPT_BLOWER_MAX_NIGHT: Final = "blower_max_night"

# Night window (the quiet caps above + quieter behavior).
OPT_NIGHT_START: Final = "night_start"             # "HH:MM"
OPT_NIGHT_END: Final = "night_end"

# Safety (always-on guard).
OPT_HARD_MIN: Final = "hard_min"                   # absolute comfort_temp floor, °C
OPT_HARD_MAX: Final = "hard_max"                   # absolute comfort_temp ceiling, °C
OPT_SAFETY_COOLDOWN_MIN: Final = "safety_cooldown_min"

# ---------------------------------------------------------------------------
# Fitted model constants — written by tools/fit.py and reviewed by a human.
# There is no online learning: it was not required, was not observed to help, and
# its one measurable effect was a positive feedback loop through a learned dead time.
# ---------------------------------------------------------------------------
MK_DEAD_TIME: Final = "dead_time_min"              # θ: command -> comfort starts moving
MK_TAU: Final = "tau_min"                          # τ: first-order time constant
MK_GAIN: Final = "gain_per_step"                   # K: °C settled change per 1°C setpoint
# Outdoor-reset curve: setpoint = intercept + per_outdoor·T_out + per_target·target
MK_FF_INTERCEPT: Final = "ff_intercept"
MK_FF_PER_OUTDOOR: Final = "ff_per_outdoor"
MK_FF_PER_TARGET: Final = "ff_per_target"
# °C of equivalent setpoint one AC blower level is worth. Zero means "not identified
# as a temperature lever", and the output stage then drives it from saturation alone.
MK_BLOWER_GAIN: Final = "blower_gain"
# SIMC closed-loop time constant, as a multiple of the dead time. The single tuning
# knob: larger is slower and more robust. Kc and Ti are derived from it (model.py),
# never stored, so they cannot drift out of step with the plant they came from.
MK_TAU_C_MULT: Final = "tau_c_mult"

# How close a rail may sit to the band before the ripple of ordinary tracking starts
# tripping it. Used ONLY to warn: the configured rails are enforced exactly as set.
# Measured 08-06 — a cold rail 0.2 °C under the band floor cut AC power 12 times in
# 4.5 h on dips of 0.1–0.45 °C, and each cut cost a full setpoint walk back down.
RAIL_RIPPLE: Final = 0.3
# Bounds enforced when loading from storage, so a value written under older rules can
# never persist out of range. GAIN_MIN is deliberately not tiny: it divides into Kc,
# so a gain set too low yields an over-aggressive loop rather than an obviously broken
# one, and a 1 °C setpoint step that settles under 0.3 °C is not physically credible.
GAIN_MIN: Final = 0.3
GAIN_MAX: Final = 2.0
DEAD_MIN: Final = 2.0
DEAD_MAX: Final = 25.0
TAU_C_MULT_MIN: Final = 0.5
TAU_C_MULT_MAX: Final = 5.0

# Fitted 2026-08-07 over 7 days (tools/fit.py). Read that module's docstring before
# changing any of these — closed-loop history identifies some of them and not others.
MODEL_DEFAULTS: Final = {
    # NOT identified by the fit, and not identifiable from closed-loop history: the
    # controller moves the setpoint because the room moved, so the regression recovers
    # the controller's inverse (measured gain −0.04 °C/°C, and a blower that appeared
    # to WARM the room). These three are the priors the old online adapter reported,
    # with the gain taken at the TOP of its plausible range on purpose — it divides
    # into Kc, so guessing high gives a gentler loop and guessing low an unstable one.
    MK_GAIN: 0.5,
    MK_DEAD_TIME: 10.0,
    MK_TAU: 8.0,
    # Identified: regressing the old controller's own output on outdoor temperature,
    # which is exogenous, is safe where regressing the room on that output is not.
    # −0.21 °C of setpoint per outdoor °C, over a 24.6–33.2 °C span.
    MK_FF_INTERCEPT: -21.52,
    MK_FF_PER_OUTDOOR: -0.2107,
    # Pinned by physics rather than fitted: steady state is y = c0 + c_out·T_out + K·sp,
    # so holding the room 1 °C warmer takes exactly 1/K of setpoint. Fitting it gave
    # 0.26, read off a target that never moved more than 0.5 °C in the whole week.
    MK_FF_PER_TARGET: 2.0,
    # Not identified — see above. The output stage falls back to driving the blower
    # from saturation, which needs no gain.
    MK_BLOWER_GAIN: 0.0,
    MK_TAU_C_MULT: 1.5,
}

# ---------------------------------------------------------------------------
# Strategy presets — bundles of options applied when the strategy is chosen.
# Only the emphasis knobs; entity bindings and fitted model are untouched.
# ---------------------------------------------------------------------------
STRATEGY_BABY: Final = "baby"
STRATEGY_ECO: Final = "eco"
STRATEGY_COMFORT: Final = "comfort"
STRATEGY_CUSTOM: Final = "custom"

STRATEGIES: Final = [STRATEGY_BABY, STRATEGY_ECO, STRATEGY_COMFORT, STRATEGY_CUSTOM]

STRATEGY_PRESETS: Final = {
    STRATEGY_BABY: {
        OPT_BAND_LOW: 0.4,
        OPT_BAND_HIGH: 0.5,
        OPT_NO_FAN_OFFSET: 0.2,
        OPT_FAN_MAX_DAY: 40,
        OPT_FAN_MAX_NIGHT: 25,
        OPT_BLOWER_MAX_DAY: 1,
        OPT_BLOWER_MAX_NIGHT: 1,
        OPT_SAFETY_COOLDOWN_MIN: 12,
    },
    STRATEGY_ECO: {
        OPT_BAND_LOW: 0.5,
        OPT_BAND_HIGH: 0.9,
        OPT_NO_FAN_OFFSET: 0.3,
        OPT_FAN_MAX_DAY: 70,
        OPT_FAN_MAX_NIGHT: 40,
        OPT_BLOWER_MAX_DAY: 1,
        OPT_BLOWER_MAX_NIGHT: 1,
        OPT_SAFETY_COOLDOWN_MIN: 15,
    },
    STRATEGY_COMFORT: {
        OPT_BAND_LOW: 0.35,
        OPT_BAND_HIGH: 0.45,
        OPT_NO_FAN_OFFSET: 0.15,
        OPT_FAN_MAX_DAY: 60,
        OPT_FAN_MAX_NIGHT: 35,
        OPT_BLOWER_MAX_DAY: 1,
        OPT_BLOWER_MAX_NIGHT: 1,
        OPT_SAFETY_COOLDOWN_MIN: 10,
    },
    # custom: no preset, user-tuned values are kept as-is.
}

# General option defaults (used when not provided by config/strategy).
OPTION_DEFAULTS: Final = {
    OPT_STRATEGY: STRATEGY_BABY,
    OPT_COMFORT_K: 0.35,
    OPT_COMFORT_RH_REF: 55.0,
    OPT_BAND_LOW: 0.4,
    OPT_BAND_HIGH: 0.5,
    OPT_NO_FAN_OFFSET: 0.2,
    OPT_SETPOINT_MIN: 24,
    OPT_SETPOINT_MAX: 27,
    OPT_FAN_MIN_LEVEL: 10,
    OPT_FAN_MAX_DAY: 40,
    OPT_FAN_MAX_NIGHT: 25,
    OPT_BLOWER_MAX_DAY: 1,
    OPT_BLOWER_MAX_NIGHT: 1,
    OPT_NIGHT_START: "22:00",
    OPT_NIGHT_END: "07:00",
    OPT_HARD_MIN: 23.0,
    OPT_HARD_MAX: 29.0,
    OPT_SAFETY_COOLDOWN_MIN: 12,
}

# Default flat schedule if the user hasn't drawn one yet (48 × 30-min points).
DEFAULT_TARGET: Final = 26.0
SCHEDULE_POINTS: Final = 48  # 30-minute resolution

# Comfort-zone controller "modes" (what the supervisor is doing right now).
MODE_IDLE: Final = "idle"
MODE_COOLING: Final = "cooling"
MODE_EASING: Final = "easing"
MODE_FAN_ASSIST: Final = "fan_assist"
MODE_SAFETY_OVERHEAT: Final = "safety_overheat"
MODE_SAFETY_OVERCOOL: Final = "safety_overcool"
MODE_STALE_HOLD: Final = "stale_hold"
MODE_FAILSAFE: Final = "failsafe"

# Signals that mean an entity is not usable this tick.
UNAVAILABLE_STATES: Final = ("unavailable", "unknown", "none", "")
