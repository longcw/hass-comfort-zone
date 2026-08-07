"""Per-zone control loop.

A ``DataUpdateCoordinator`` whose update *is* the control tick: every
``TICK_SECONDS`` it reads the bound entities, runs the pure controller + safety
guard, applies the resulting command, logs the decision, and publishes a data
snapshot the entities render.
"""
from __future__ import annotations

import logging
from collections import deque
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .actuators import Bindings, apply, resolve_blower_ladder
from .comfort import comfort_temp
from .const import (
    CONF_AC_CLIMATE,
    CONF_AC_POWER_SENSOR,
    CONF_AC_POWER_SWITCH,
    CONF_COMFORT_SENSOR,
    CONF_COMFORT_SOURCE,
    CONF_FAN,
    CONF_FAN_SPEED_NUMBER,
    CONF_HUMIDITY_SENSOR,
    CONF_NAME,
    CONF_TEMP_SENSOR,
    CONF_WEATHER,
    DOMAIN,
    MODE_IDLE,
    OPT_BAND_HIGH,
    OPT_BAND_LOW,
    OPT_NO_FAN_OFFSET,
    OPT_BLOWER_MAX_DAY,
    OPT_BLOWER_MAX_NIGHT,
    OPT_COMFORT_K,
    OPT_COMFORT_RH_REF,
    OPT_FAN_MAX_DAY,
    OPT_FAN_MAX_NIGHT,
    OPT_FAN_MIN_LEVEL,
    OPT_HARD_MAX,
    OPT_HARD_MIN,
    OPT_NIGHT_END,
    OPT_NIGHT_START,
    OPT_SAFETY_COOLDOWN_MIN,
    OPT_SETPOINT_MAX,
    OPT_SETPOINT_MIN,
    OPT_STRATEGY,
    OPTION_DEFAULTS,
    TICK_SECONDS,
    UNAVAILABLE_STATES,
)
from .controller import Command, Controller, Signals, ZoneParams
from .model import FopdtPredictor, ModelParams, recent_power_change
from .options import MODE_KEYS, resolve
from .safety import FAILSAFE_SETPOINT, SafetyGuard, SafetyParams, rails, warn_if_inside_band
from .store import ZoneStore

_LOGGER = logging.getLogger(__name__)

SLOPE_WINDOW_MIN = 5.0
# How long without a *fresh report* before we stop trusting the reading. Wide
# enough to tolerate normal BLE gaps (the crib thermometer can go quiet ~10 min);
# a value present but merely quiet only freezes control, it does not disrupt.
STALE_AFTER_S = 1200
# The card's power arrow looks back this far. Short, because it annotates a live
# reading and has to agree with it. This is the DISPLAY window; the control-side
# leading indicator keeps its own, much slower, baseline (see power.PowerLead).
POWER_DISPLAY_WINDOW_MIN = 3.0
# How far ahead the hourly forecast is carried. The reset curve only ever reads one
# plant horizon out (~18 min), but the interpolator needs points either side of that.
FORECAST_HOURS = 6
# A no-action row is logged when the deciding branch changes, no faster than this.
HOLD_LOG_MIN_INTERVAL_MIN = 3.0
# While the room sits OUTSIDE its comfort zone, log at least this often even if
# nothing is actuated and the branch never changes. A drift with no actions is the
# state most worth reviewing afterwards and was previously the only one invisible.
DRIFT_LOG_INTERVAL_MIN = 2.0
# Largest setpoint jump we will believe from the unit in one tick. Bigger than any
# command this controller issues (MAX step is one level at a time) and bigger than
# the guard's blast, but small enough that a garbage reading cannot poison the
# dead-time model for the next hour.
MAX_CREDIBLE_SP_STEP = 5
# Of the decision trace, what is worth persisting on every log row: enough to
# re-judge the decision later without replaying the whole tick.
LOG_TRACE_KEYS = ("y", "settled", "target", "error", "urgency", "power_bias", "u_ff", "u_fb", "u", "u_raw",
                  "integral", "saturated", "frozen", "sp", "trim", "blower",
                  "hi", "lo", "outdoor", "sp_dwell_left", "sp_observed",
                  # the zone, and whether the room was inside it — without these a
                  # row of "error 0, no action" cannot be told from a stalled loop
                  "zone_lo", "zone_hi", "in_zone")


def _slim_trace(trace: dict) -> dict:
    out = {}
    for k in LOG_TRACE_KEYS:
        v = trace.get(k)
        out[k] = round(v, 3) if isinstance(v, float) else v
    return out


def _report_age(state, now) -> float:
    """Seconds since the entity last *reported* (any write), not just changed."""
    ts = getattr(state, "last_reported", None) or state.last_updated
    return (now - ts).total_seconds()


def _fnum(state) -> float | None:
    if state is None or state.state in UNAVAILABLE_STATES:
        return None
    try:
        return float(state.state)
    except (ValueError, TypeError):
        return None


class ComfortZoneCoordinator(DataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.zone_name = entry.data.get(CONF_NAME, "Comfort Zone")
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}:{self.zone_name}",
            update_interval=timedelta(seconds=TICK_SECONDS),
        )
        self.store = ZoneStore(hass, entry.entry_id)
        self.enabled = True
        self.fan_assist = True
        self._predictor: FopdtPredictor | None = None
        self._controller: Controller | None = None
        self._safety = SafetyGuard()
        self._comfort_hist: deque = deque(maxlen=64)
        self._power_hist: deque = deque(maxlen=256)
        # The setpoint the unit last reported. The predictor is driven from changes
        # in THIS, not from our own commands: the room responds to what the unit is
        # actually running at, and this VRF's cloud proxy re-reports its own
        # remembered value after a power cycle (7 of 30 transitions on 08-06).
        self._observed_sp: int | None = None
        # When the unit's setpoint last actually changed. Reported rather than the
        # controller's own dwell clock: what a reader wants to know is how long the
        # compressor has been left alone, and a command we issued but the unit never
        # applied did not leave it alone any less.
        self._observed_sp_since = None
        self._forecast: list[tuple] = []
        self._forecast_at = None
        self._last_log_key: tuple | None = None
        self._last_log_at = None
        self._last_branch: str = ""
        self._last_hold_log_at = None
        self._last_drift_log_at = None
        self._last_rail_warning: str | None = None

    # -- lifecycle ----------------------------------------------------------
    async def async_prepare(self) -> None:
        await self.store.load()
        self.enabled = self.store.flag("enabled")
        self.fan_assist = self.store.flag("fan_assist")
        self.reload_model()

    async def async_start(self) -> None:
        await self.async_config_entry_first_refresh()

    async def async_stop(self) -> None:
        self.update_interval = None

    def reload_model(self) -> None:
        self._predictor = FopdtPredictor(ModelParams.from_dict(self.store.model))
        self._controller = Controller(self._predictor)

    async def async_set_enabled(self, value: bool) -> None:
        """Switching off means stop deciding — it must not mean freeze an override.

        The disabled path returns before the guard runs and before anything is
        applied, so whatever was last commanded simply stays on the hardware. Turn
        the zone off during an overheat blast and the unit sits at setpoint 22 with
        the blower maxed indefinitely; turn it off during an overcool hold and the
        AC stays off. Neither is what a person means by "off". So an override in
        force is released once, on the way out.
        """
        if not value and self._safety.state != "normal":
            releasing = self._safety.state
            self._safety = SafetyGuard()
            try:
                d = self.entry.data
                await apply(self.hass, Bindings(
                    ac_climate=d[CONF_AC_CLIMATE],
                    ac_power_switch=d.get(CONF_AC_POWER_SWITCH),
                    fan=d.get(CONF_FAN),
                    fan_speed_number=d.get(CONF_FAN_SPEED_NUMBER),
                    blower_levels=self.blower_levels(),
                ), Command(set_setpoint=FAILSAFE_SETPOINT, set_fan=False))
                _LOGGER.info("%s: disabled during %s — released the override to "
                             "setpoint %s", self.zone_name, releasing, FAILSAFE_SETPOINT)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("%s: could not release the %s override on disable: %s",
                              self.zone_name, releasing, err)
        self.enabled = value
        await self.store.set_flag("enabled", value)

    async def async_set_fan_assist(self, value: bool) -> None:
        self.fan_assist = value
        await self.store.set_flag("fan_assist", value)

    # -- option resolution --------------------------------------------------
    def options(self) -> dict:
        stored = self.entry.options or {}
        strategy = stored.get(OPT_STRATEGY, OPTION_DEFAULTS[OPT_STRATEGY])
        return resolve(stored, self.store.knobs(strategy))

    async def async_set_option(self, key: str, value) -> None:
        """Store a tuned value where it belongs.

        A mode's own knob is saved under that mode, so it is still there when the
        mode comes back; anything else belongs to the zone and lives on the entry.
        """
        if key in MODE_KEYS:
            await self.store.set_knob(self.options()[OPT_STRATEGY], key, value)
            self.async_update_listeners()
            await self.async_request_refresh()
        else:
            options = {**(self.entry.options or {}), key: value}
            self.hass.config_entries.async_update_entry(self.entry, options=options)

    def window_caps(self, opts: dict) -> tuple[bool, int, int]:
        """Whether the night window is in force, and the quiet caps it imposes.

        A fan cap of 0 means no fan at all in that window; the blower cap is a
        ladder index (0 = low).
        """
        now_t = dt_util.now().strftime("%H:%M")
        start, end = opts[OPT_NIGHT_START], opts[OPT_NIGHT_END]
        is_night = (start <= now_t < end) if start <= end else (now_t >= start or now_t < end)
        return (
            is_night,
            int(opts[OPT_FAN_MAX_NIGHT] if is_night else opts[OPT_FAN_MAX_DAY]),
            int(opts[OPT_BLOWER_MAX_NIGHT] if is_night else opts[OPT_BLOWER_MAX_DAY]),
        )

    def bands(self, opts: dict) -> tuple[float, float]:
        """The comfort band, ``(low, high)``.

        Fixed, and no longer conditional on the fan: warmth being less tolerable
        without a breeze shifts the comfort ZONE down (see ``Controller.zone``),
        which is what it physically means. As a band it silently widened the warm
        gate instead, which is how the room parked at 27.25 on 07-26.
        """
        return float(opts[OPT_BAND_LOW]), float(opts[OPT_BAND_HIGH])

    def fan_available(self, opts: dict) -> bool:
        """Whether any fan can run right now — switched on, and not capped to zero."""
        _, fan_max, _ = self.window_caps(opts)
        return self.fan_assist and fan_max > 0

    def blower_levels(self) -> list[str]:
        """The device's own labels for our low→high blower ladder (see actuators)."""
        st = self.hass.states.get(self.entry.data[CONF_AC_CLIMATE])
        return resolve_blower_ladder(st.attributes.get("fan_modes") if st else None)

    # -- signal reading -----------------------------------------------------
    def _read_comfort(self, opts: dict, now) -> tuple[float | None, bool]:
        source = self.entry.data.get(CONF_COMFORT_SOURCE, "compute")
        if source == "sensor":
            st = self.hass.states.get(self.entry.data[CONF_COMFORT_SENSOR])
            val = _fnum(st)
            if val is None:
                return None, True   # unavailable → park (handled in safety)
            return val, _report_age(st, now) > STALE_AFTER_S
        t_st = self.hass.states.get(self.entry.data[CONF_TEMP_SENSOR])
        h_st = self.hass.states.get(self.entry.data[CONF_HUMIDITY_SENSOR])
        t, rh = _fnum(t_st), _fnum(h_st)
        if t is None or rh is None:
            return None, True
        stale = _report_age(t_st, now) > STALE_AFTER_S or _report_age(h_st, now) > STALE_AFTER_S
        return comfort_temp(t, rh, opts[OPT_COMFORT_K], opts[OPT_COMFORT_RH_REF]), stale

    def _slope(self, comfort: float | None, now) -> float | None:
        if comfort is None:
            return None
        self._comfort_hist.append((now, comfort))
        cutoff = now - timedelta(minutes=SLOPE_WINDOW_MIN)
        window = [(t, v) for (t, v) in self._comfort_hist if t >= cutoff]
        if len(window) < 2:
            return 0.0
        (t0, v0), (t1, v1) = window[0], window[-1]
        dt = (t1 - t0).total_seconds() / 60.0
        return (v1 - v0) / dt if dt > 0 else 0.0

    def _outdoor(self) -> float | None:
        """Current outdoor temperature, from the bound weather entity."""
        eid = self.entry.data.get(CONF_WEATHER)
        st = self.hass.states.get(eid) if eid else None
        if st is None or st.state in UNAVAILABLE_STATES:
            return None
        try:
            return float(st.attributes["temperature"])
        except (KeyError, TypeError, ValueError):
            return None

    async def _async_forecast(self, now) -> list[tuple]:
        """Hourly outdoor forecast, refreshed well inside its own resolution.

        Cached because ``weather.get_forecasts`` is a service round trip and the
        underlying model only updates hourly — calling it every 45 s would cost far
        more than it tells us.
        """
        eid = self.entry.data.get(CONF_WEATHER)
        if not eid:
            return []
        if self._forecast_at is not None and now - self._forecast_at < timedelta(minutes=20):
            return self._forecast
        self._forecast_at = now
        try:
            res = await self.hass.services.async_call(
                "weather", "get_forecasts", {"entity_id": eid, "type": "hourly"},
                blocking=True, return_response=True)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("%s: no hourly forecast: %s", self.zone_name, err)
            return self._forecast
        out = []
        for item in ((res or {}).get(eid, {}) or {}).get("forecast", [])[:FORECAST_HOURS]:
            try:
                out.append((dt_util.parse_datetime(item["datetime"]),
                            float(item["temperature"])))
            except (KeyError, TypeError, ValueError):
                continue
        self._forecast = [(t, v) for t, v in out if t is not None]
        return self._forecast

    # -- the tick -----------------------------------------------------------
    async def _async_update_data(self) -> dict[str, Any]:
        opts = self.options()
        now = dt_util.utcnow()
        d = self.entry.data
        model = self._predictor.params

        comfort, stale = self._read_comfort(opts, now)
        slope = self._slope(comfort, now)

        power = _fnum(self.hass.states.get(d[CONF_AC_POWER_SENSOR])) if d.get(CONF_AC_POWER_SENSOR) else None
        if power is not None:
            self._power_hist.append((now, power))
        power_recent = recent_power_change(self._power_hist, now, POWER_DISPLAY_WINDOW_MIN)

        blower_levels = self.blower_levels()
        climate_st = self.hass.states.get(d[CONF_AC_CLIMATE])
        ac_on = climate_st is not None and climate_st.state not in ("off", *UNAVAILABLE_STATES)
        setpoint = None
        blower_idx = None
        if climate_st:
            sp_attr = climate_st.attributes.get("temperature")
            if sp_attr is not None:
                setpoint = int(round(float(sp_attr)))
            fm = climate_st.attributes.get("fan_mode")
            if fm in blower_levels:
                blower_idx = blower_levels.index(fm)

        device_min = 16
        if climate_st and climate_st.attributes.get("min_temp") is not None:
            try:
                device_min = int(round(float(climate_st.attributes["min_temp"])))
            except (TypeError, ValueError):
                pass

        fan_on = False
        fan_level = None
        if d.get(CONF_FAN):
            fan_st = self.hass.states.get(d[CONF_FAN])
            fan_on = fan_st is not None and fan_st.state == "on"
        if d.get(CONF_FAN_SPEED_NUMBER):
            lv = _fnum(self.hass.states.get(d[CONF_FAN_SPEED_NUMBER]))
            fan_level = int(lv) if lv is not None else None

        local = dt_util.now()
        target = self.store.target_at(local.hour, local.minute, opts[OPT_STRATEGY])
        is_night, fan_max, blower_max = self.window_caps(opts)
        band_low, band_high = self.bands(opts)
        # Resolved once, so the guard and the band the fit is measured against cannot
        # disagree about where the rails are.
        hard_min, hard_max = rails(
            target, band_low, band_high,
            float(opts[OPT_HARD_MIN]), float(opts[OPT_HARD_MAX]))
        warn = warn_if_inside_band(target, band_low, band_high, hard_min, hard_max)
        if warn and warn != self._last_rail_warning:
            self._last_rail_warning = warn
            _LOGGER.warning("%s: %s", self.zone_name, warn)

        # The plant responds to what the unit is actually running at, so a setpoint
        # the cloud proxy changed on its own is as real a step as one we commanded.
        if setpoint is not None and setpoint != self._observed_sp:
            if self._observed_sp is not None:
                # Bound the step before it reaches the predictor. A cloud proxy that
                # glitches to 5 or 50 would otherwise inject a ±25 °C step, and
                # `remaining_effect` would carry ±12 °C of imaginary in-flight
                # cooling for the next fifty minutes — pinning the real setpoint at
                # its floor whatever the room is doing.
                step = max(-MAX_CREDIBLE_SP_STEP,
                           min(MAX_CREDIBLE_SP_STEP, setpoint - self._observed_sp))
                if step != setpoint - self._observed_sp:
                    _LOGGER.warning("%s: unit reported setpoint %s from %s — "
                                    "clamping the modelled step to %s",
                                    self.zone_name, setpoint, self._observed_sp, step)
                self._predictor.record_setpoint_change(now, step)
                self._controller.note_setpoint_change(now)
            self._observed_sp = setpoint
            self._observed_sp_since = now

        params = ZoneParams(
            target=target,
            band_low=band_low,
            band_high=band_high,
            no_fan_offset=float(opts[OPT_NO_FAN_OFFSET]),
            setpoint_min=int(opts[OPT_SETPOINT_MIN]),
            setpoint_max=int(opts[OPT_SETPOINT_MAX]),
            blower_levels=blower_levels,
            fan_min_level=int(opts[OPT_FAN_MIN_LEVEL]),
            fan_max_level=fan_max,
            # The guard is not held back by the window in force, only by the most the
            # zone ever sanctions for its fan.
            fan_max_guard=int(max(opts[OPT_FAN_MAX_DAY], opts[OPT_FAN_MAX_NIGHT])),
            blower_max_idx=blower_max,
            blower_gain=model.blower_gain,
            fan_assist_enabled=self.fan_assist,
            hard_min=hard_min,
            hard_max=hard_max,
            setpoint_device_min=device_min,
        )
        signals = Signals(
            now=now, comfort=comfort, slope=slope,
            outdoor=self._outdoor(), forecast=await self._async_forecast(now),
            power=power, ac_on=ac_on, setpoint=setpoint, blower_idx=blower_idx,
            fan_on=fan_on, fan_level=fan_level,
            guard_active=self._safety.state != "normal",
        )
        predicted = self._predictor.predict_settled(now, comfort) if comfort is not None else None

        dev = {
            "ac_on": ac_on,
            "ac_state": climate_st.state if climate_st else None,
            "ac_blower": (climate_st.attributes.get("fan_mode") if climate_st else None),
            "fan_on": fan_on,
            "power_recent": power_recent,
            "entities": {
                "status": f"sensor.{self.entry.entry_id}",  # replaced by real id in sensor.py
                "ac": d[CONF_AC_CLIMATE],
                "ac_power_switch": d.get(CONF_AC_POWER_SWITCH),
                "power": d.get(CONF_AC_POWER_SENSOR),
                # the card opens this for the native hourly forecast dialog
                "weather": d.get(CONF_WEATHER),
                "fan": d.get(CONF_FAN),
                "fan_speed": d.get(CONF_FAN_SPEED_NUMBER),
                "temp": d.get(CONF_TEMP_SENSOR),
                "humidity": d.get(CONF_HUMIDITY_SENSOR),
                "comfort": d.get(CONF_COMFORT_SENSOR),
            },
        }

        if not self.enabled:
            return self._snapshot(opts, params, signals, target, predicted, MODE_IDLE,
                                  "disabled — not actuating", is_night, [], dev,
                                  branch="disabled", trace={})

        opt_cmd = self._controller.tick(signals, params)
        sp = SafetyParams(
            hard_min=hard_min,
            hard_max=hard_max,
            cooldown_min=float(opts[OPT_SAFETY_COOLDOWN_MIN]),
        )
        cmd = self._safety.evaluate(signals, params, sp, opt_cmd, stale=stale)
        # The guard returns a Command of its own when it takes the room, which would
        # drop the controller's telemetry. Carry it, and record which branch was
        # overridden — "what the controller wanted" is the useful thing to see here.
        if cmd is not opt_cmd:
            cmd.trace = dict(opt_cmd.trace)
            cmd.trace["overridden"] = opt_cmd.branch or None
            cmd.branch = cmd.branch or f"safety.{self._safety.state}"

        bindings = Bindings(
            ac_climate=d[CONF_AC_CLIMATE],
            ac_power_switch=d.get(CONF_AC_POWER_SWITCH),
            fan=d.get(CONF_FAN),
            fan_speed_number=d.get(CONF_FAN_SPEED_NUMBER),
            blower_levels=blower_levels,
        )
        actions: list[str] = []
        try:
            actions = await apply(self.hass, bindings, cmd)
            # Start the compressor and blower dwells only for commands that were
            # actually issued — see SplitRange.commit.
            self._controller.split.commit(
                now, cmd.set_setpoint is not None, cmd.set_blower_idx is not None)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("%s: failed to apply command: %s", self.zone_name, err)

        # A held override re-asserts the same command every tick (deliberately —
        # the VRF is not trusted to keep it). Logging each re-assert made the
        # decision log read as actuator churn that never happened, so collapse
        # identical consecutive commands into one row (with a 5-min heartbeat).
        #
        # A change of deciding ARM is logged too, even when nothing was actuated:
        # the holds are the decisions most worth reviewing afterwards, and an
        # actions-only log left them visible on the live card and nowhere else.
        key = (cmd.branch, cmd.mode, tuple(actions))
        repeat = key == self._last_log_key and \
            self._last_log_at is not None and (now - self._last_log_at) < timedelta(minutes=5)
        # A room drifting outside its zone must leave a trail even when nothing is
        # actuated. Logging only on action or branch change hid the 08-07 failure
        # completely: fourteen minutes, one unchanged branch, no rows, while the
        # room fell 0.64 °C below its zone. Silence read exactly like "fine".
        drifting = bool((cmd.trace or {}).get("urgency", 0.0) > 0.0)
        heartbeat = drifting and (
            self._last_drift_log_at is None
            or (now - self._last_drift_log_at) >= timedelta(minutes=DRIFT_LOG_INTERVAL_MIN))
        new_arm = cmd.branch != self._last_branch and (
            self._last_hold_log_at is None
            or (now - self._last_hold_log_at) >= timedelta(minutes=HOLD_LOG_MIN_INTERVAL_MIN))
        if (actions or new_arm or heartbeat) and not repeat:
            if heartbeat:
                self._last_drift_log_at = now
            self._last_log_key = key
            self._last_log_at = now
            if not actions:
                self._last_hold_log_at = now
            self._last_branch = cmd.branch
            await self.store.append_log({
                "t": now.isoformat(),
                "mode": cmd.mode,
                "branch": cmd.branch,
                "reason": cmd.reason,
                "actions": actions,
                "comfort": round(comfort, 2) if comfort is not None else None,
                "target": round(target, 2),
                "power": round(power) if power is not None else None,
                "d": _slim_trace(cmd.trace),
            })
            _LOGGER.info("%s: %s — %s", self.zone_name, cmd.reason,
                         ", ".join(actions) or "no action")

        return self._snapshot(opts, params, signals, target, predicted, cmd.mode, cmd.reason,
                              is_night, actions, dev,
                              branch=cmd.branch, trace=cmd.trace)

    def _snapshot(self, opts, params, signals, target, predicted, mode, reason, is_night, actions, dev,
                  *, branch="", trace=None):
        return {
            "name": self.zone_name,
            "enabled": self.enabled,
            "comfort": signals.comfort,
            "slope": signals.slope,
            "target": target,
            # The inner zone the loop actually holds, so the card can draw what is
            # being regulated rather than a point the controller no longer tracks.
            "zone_lo": self._controller.zone(params)[0],
            "zone_hi": self._controller.zone(params)[1],
            "predicted": predicted,
            "band_low": params.band_low,
            "band_high": params.band_high,
            # The rails actually in force, which are pushed out when a configured one
            # The configured rails, enforced exactly as set.
            "hard_min": params.hard_min,
            "hard_max": params.hard_max,
            "outdoor": signals.outdoor,
            # Minutes the unit has held this setpoint. None until we have seen it
            # change once — we cannot know how long it sat there before we started.
            "sp_held_min": (None if self._observed_sp_since is None else
                            (dt_util.utcnow() - self._observed_sp_since).total_seconds() / 60.0),
            "power": signals.power,
            # The live reading, for the card's chip and arrow.
            "power_recent": dev["power_recent"],
            "setpoint": signals.setpoint,
            "fan_level": signals.fan_level,
            "fan_on": dev["fan_on"],
            "fan_assist_enabled": self.fan_assist,
            # The quiet limits in force right now, so the card can say which window
            # it is showing rather than making the reader work out the time.
            "fan_max_level": params.fan_max_level,
            "blower_max": (params.blower_levels[params.regular_blower_max]
                           if params.blower_levels else None),
            "ac_on": dev["ac_on"],
            "ac_state": dev["ac_state"],
            "ac_blower": dev["ac_blower"],
            "mode": mode,
            "reason": reason,
            "branch": branch,
            "decision": dict(trace or {}),
            "strategy": opts[OPT_STRATEGY],
            "safety_state": self._safety.state,
            "is_night": is_night,
            "last_actions": actions,
            "entities": dev["entities"],
        }
