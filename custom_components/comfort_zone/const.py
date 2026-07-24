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

# ---------------------------------------------------------------------------
# Options (tunable at runtime via options flow / number entities / card)
# ---------------------------------------------------------------------------
OPT_STRATEGY: Final = "strategy"
OPT_SCHEDULE: Final = "schedule"                   # list[float] len 48 (30-min points), °C

# Comfort-signal params (compute mode).
OPT_COMFORT_K: Final = "comfort_k"                 # humidity weight, 0..1
OPT_COMFORT_RH_REF: Final = "comfort_rh_ref"       # anchor RH (signal == raw temp here)

# Band / setpoint envelope. The comfort band is ASYMMETRIC:
#   [ target - band_low , target + band_high ]
# The low (cold) side is the same regardless of the fan; the high (warm) side
# tightens when fan-assist is off (no air movement to make warmth tolerable).
OPT_BAND_LOW: Final = "band_low"                   # °C below target before easing
OPT_BAND_HIGH: Final = "band_high"                 # °C above target before cooling (fan on)
OPT_BAND_HIGH_NO_FAN: Final = "band_high_no_fan"   # warm-side tolerance when fan off (tighter)
OPT_SETPOINT_MIN: Final = "setpoint_min"
OPT_SETPOINT_MAX: Final = "setpoint_max"

# Fan comfort layer.
OPT_FAN_MIN_LEVEL: Final = "fan_min_level"         # speed_level floor when on
OPT_FAN_MAX_DAY: Final = "fan_max_day"
OPT_FAN_MAX_NIGHT: Final = "fan_max_night"

# Night window (fan caps + quieter behavior).
OPT_NIGHT_START: Final = "night_start"             # "HH:MM"
OPT_NIGHT_END: Final = "night_end"

# Safety (always-on guard).
OPT_HARD_MIN: Final = "hard_min"                   # absolute comfort_temp floor, °C
OPT_HARD_MAX: Final = "hard_max"                   # absolute comfort_temp ceiling, °C
OPT_SAFETY_MARGIN: Final = "safety_margin"         # wide margin beyond band for guard, °C
OPT_SAFETY_COOLDOWN_MIN: Final = "safety_cooldown_min"
OPT_MANAGED_OFF_MAX_MIN: Final = "managed_off_max_min"  # watchdog: force return after this

# ---------------------------------------------------------------------------
# Fitted model constants (written by system-ID; config provides seeds)
# ---------------------------------------------------------------------------
OPT_MODEL: Final = "model"                         # dict, see MODEL_DEFAULTS
MK_DEAD_TIME: Final = "dead_time_min"              # L: command -> comfort starts moving
MK_TAU: Final = "tau_min"                          # first-order time constant
MK_GAIN: Final = "gain_per_step"                   # °C settled change per 1°C setpoint step
MK_POWER_LEAD: Final = "power_lead_min"            # power leads comfort slope by this
MK_ENGAGE_WATTS: Final = "engage_watts"            # Δpower that counts as "engaged"
MK_ENGAGE_WINDOW: Final = "engage_window_min"      # how long to wait for engagement
MK_LEAD: Final = "lead_min"                        # anticipation lead (LEARNED, bounded)

LEAD_CAP: Final = 8.0          # max anticipation lead the learner may reach (min)

MODEL_DEFAULTS: Final = {
    MK_DEAD_TIME: 10.0,
    MK_TAU: 8.0,
    MK_GAIN: 0.5,            # a 1°C setpoint drop settles ~0.5°C of comfort_temp
    MK_POWER_LEAD: 6.0,
    MK_ENGAGE_WATTS: 150.0,
    MK_ENGAGE_WINDOW: 4.0,
    MK_LEAD: 3.0,           # start with a little anticipation; the adapter tunes it
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
        OPT_BAND_HIGH_NO_FAN: 0.3,
        OPT_FAN_MAX_DAY: 40,
        OPT_FAN_MAX_NIGHT: 25,
        OPT_SAFETY_MARGIN: 1.4,
        OPT_SAFETY_COOLDOWN_MIN: 12,
    },
    STRATEGY_ECO: {
        OPT_BAND_LOW: 0.5,
        OPT_BAND_HIGH: 0.9,
        OPT_BAND_HIGH_NO_FAN: 0.6,
        OPT_FAN_MAX_DAY: 70,
        OPT_FAN_MAX_NIGHT: 40,
        OPT_SAFETY_MARGIN: 1.6,
        OPT_SAFETY_COOLDOWN_MIN: 15,
    },
    STRATEGY_COMFORT: {
        OPT_BAND_LOW: 0.35,
        OPT_BAND_HIGH: 0.45,
        OPT_BAND_HIGH_NO_FAN: 0.3,
        OPT_FAN_MAX_DAY: 60,
        OPT_FAN_MAX_NIGHT: 35,
        OPT_SAFETY_MARGIN: 1.3,
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
    OPT_BAND_HIGH_NO_FAN: 0.3,
    OPT_SETPOINT_MIN: 24,
    OPT_SETPOINT_MAX: 27,
    OPT_FAN_MIN_LEVEL: 10,
    OPT_FAN_MAX_DAY: 40,
    OPT_FAN_MAX_NIGHT: 25,
    OPT_NIGHT_START: "22:00",
    OPT_NIGHT_END: "07:00",
    OPT_HARD_MIN: 23.0,
    OPT_HARD_MAX: 29.0,
    OPT_SAFETY_MARGIN: 1.4,
    OPT_SAFETY_COOLDOWN_MIN: 12,
    OPT_MANAGED_OFF_MAX_MIN: 30,
}

# Default flat schedule if the user hasn't drawn one yet (48 × 30-min points).
DEFAULT_TARGET: Final = 26.0
SCHEDULE_POINTS: Final = 48  # 30-minute resolution

# Comfort-zone controller "modes" (what the supervisor is doing right now).
MODE_IDLE: Final = "idle"
MODE_COOLING: Final = "cooling"
MODE_EASING: Final = "easing"
MODE_FAN_ASSIST: Final = "fan_assist"
MODE_MANAGED_OFF: Final = "managed_off"
MODE_SAFETY_OVERHEAT: Final = "safety_overheat"
MODE_SAFETY_OVERCOOL: Final = "safety_overcool"
MODE_STALE_HOLD: Final = "stale_hold"
MODE_FAILSAFE: Final = "failsafe"

# Signals that mean an entity is not usable this tick.
UNAVAILABLE_STATES: Final = ("unavailable", "unknown", "none", "")
