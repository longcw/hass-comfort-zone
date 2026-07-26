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

from .actuators import Bindings, apply
from .adapt import OnlineAdapter
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
    DOMAIN,
    MODE_IDLE,
    OPT_BAND_HIGH,
    OPT_BAND_HIGH_NO_FAN,
    OPT_BAND_LOW,
    OPT_COMFORT_K,
    OPT_COMFORT_RH_REF,
    OPT_FAN_MAX_DAY,
    OPT_FAN_MAX_NIGHT,
    OPT_FAN_MIN_LEVEL,
    OPT_HARD_MAX,
    OPT_HARD_MIN,
    OPT_MANAGED_OFF_MAX_MIN,
    OPT_NIGHT_END,
    OPT_NIGHT_START,
    OPT_SAFETY_COOLDOWN_MIN,
    OPT_SETPOINT_MAX,
    OPT_SETPOINT_MIN,
    OPT_STRATEGY,
    OPTION_DEFAULTS,
    STRATEGY_PRESETS,
    TICK_SECONDS,
    UNAVAILABLE_STATES,
)
from .controller import Controller, Signals, ZoneParams
from .model import FopdtPredictor, ModelParams, power_trend
from .safety import SafetyGuard, SafetyParams
from .store import ZoneStore

_LOGGER = logging.getLogger(__name__)

BLOWER_ORDER = ["低风", "中风", "高风"]  # ascending cooling intensity (subset of VRF modes)
SLOPE_WINDOW_MIN = 5.0
# How long without a *fresh report* before we stop trusting the reading. Wide
# enough to tolerate normal BLE gaps (the crib thermometer can go quiet ~10 min);
# a value present but merely quiet only freezes control, it does not disrupt.
STALE_AFTER_S = 1200
# The fan's power feedforward averages over this many power-lead windows (see
# power_trend): wide enough that the unit's 2–5 min off-phases do not read as the
# AC backing off, which used to flip the fan's nudge across its threshold.
POWER_TREND_WINDOWS = 2.0


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
        self._adapter: OnlineAdapter | None = None
        self._comfort_hist: deque = deque(maxlen=64)
        self._power_hist: deque = deque(maxlen=256)
        self._last_log_key: tuple | None = None
        self._last_log_at = None

    # -- lifecycle ----------------------------------------------------------
    async def async_prepare(self) -> None:
        await self.store.load()
        self.reload_model()

    async def async_start(self) -> None:
        await self.async_config_entry_first_refresh()

    async def async_stop(self) -> None:
        self.update_interval = None

    def reload_model(self) -> None:
        self._predictor = FopdtPredictor(ModelParams.from_dict(self.store.model))
        self._controller = Controller(self._predictor)
        self._adapter = OnlineAdapter(self._predictor.params)

    def set_enabled(self, value: bool) -> None:
        self.enabled = value

    def set_fan_assist(self, value: bool) -> None:
        self.fan_assist = value

    # -- option resolution --------------------------------------------------
    def options(self) -> dict:
        opts = dict(OPTION_DEFAULTS)
        strategy = (self.entry.options or {}).get(OPT_STRATEGY, opts[OPT_STRATEGY])
        opts.update(STRATEGY_PRESETS.get(strategy, {}))
        opts.update(self.entry.options or {})  # explicit user tuning wins
        opts[OPT_STRATEGY] = strategy
        return opts

    def _is_night(self, opts: dict) -> bool:
        now_t = dt_util.now().strftime("%H:%M")
        start, end = opts[OPT_NIGHT_START], opts[OPT_NIGHT_END]
        if start <= end:
            return start <= now_t < end
        return now_t >= start or now_t < end

    def _blower_levels(self) -> list[str]:
        st = self.hass.states.get(self.entry.data[CONF_AC_CLIMATE])
        modes = (st.attributes.get("fan_modes") if st else None) or []
        return [m for m in BLOWER_ORDER if m in modes]

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

    def _power_delta(self, power: float | None, now, lead_min: float) -> float | None:
        """Lead signal for the fan layer: is delivered AC power rising or falling?

        Window-averaged (see :func:`power_trend`) — a point-to-point difference reads
        the unit's own off-phases as the AC backing off.
        """
        if power is not None:
            self._power_hist.append((now, power))
        return power_trend(self._power_hist, now, lead_min * POWER_TREND_WINDOWS)

    # -- the tick -----------------------------------------------------------
    async def _async_update_data(self) -> dict[str, Any]:
        opts = self.options()
        now = dt_util.utcnow()
        d = self.entry.data
        model = self._predictor.params

        comfort, stale = self._read_comfort(opts, now)
        slope = self._slope(comfort, now)

        power = _fnum(self.hass.states.get(d[CONF_AC_POWER_SENSOR])) if d.get(CONF_AC_POWER_SENSOR) else None
        power_delta = self._power_delta(power, now, model.power_lead_min)

        blower_levels = self._blower_levels()
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
        is_night = self._is_night(opts)

        band_low = float(opts[OPT_BAND_LOW])
        band_high = float(opts[OPT_BAND_HIGH_NO_FAN] if not self.fan_assist else opts[OPT_BAND_HIGH])
        params = ZoneParams(
            target=target,
            band_low=band_low,
            band_high=band_high,
            setpoint_min=int(opts[OPT_SETPOINT_MIN]),
            setpoint_max=int(opts[OPT_SETPOINT_MAX]),
            blower_levels=blower_levels,
            fan_min_level=int(opts[OPT_FAN_MIN_LEVEL]),
            fan_max_level=int(opts[OPT_FAN_MAX_NIGHT] if is_night else opts[OPT_FAN_MAX_DAY]),
            managed_off_max_min=float(opts[OPT_MANAGED_OFF_MAX_MIN]),
            fan_assist_enabled=self.fan_assist,
            hard_min=float(opts[OPT_HARD_MIN]),
            hard_max=float(opts[OPT_HARD_MAX]),
            setpoint_device_min=device_min,
        )
        signals = Signals(
            now=now, comfort=comfort, slope=slope, power=power, power_delta=power_delta,
            ac_on=ac_on, setpoint=setpoint, blower_idx=blower_idx,
            fan_on=fan_on, fan_level=fan_level,
        )
        predicted = self._predictor.predict_settled(now, comfort) if comfort is not None else None

        dev = {
            "ac_on": ac_on,
            "ac_state": climate_st.state if climate_st else None,
            "ac_blower": (climate_st.attributes.get("fan_mode") if climate_st else None),
            "fan_on": fan_on,
            "entities": {
                "status": f"sensor.{self.entry.entry_id}",  # replaced by real id in sensor.py
                "ac": d[CONF_AC_CLIMATE],
                "ac_power_switch": d.get(CONF_AC_POWER_SWITCH),
                "power": d.get(CONF_AC_POWER_SENSOR),
                "fan": d.get(CONF_FAN),
                "fan_speed": d.get(CONF_FAN_SPEED_NUMBER),
                "temp": d.get(CONF_TEMP_SENSOR),
                "humidity": d.get(CONF_HUMIDITY_SENSOR),
                "comfort": d.get(CONF_COMFORT_SENSOR),
            },
        }

        if not self.enabled:
            return self._snapshot(opts, signals, target, predicted, MODE_IDLE,
                                  "disabled — not actuating", is_night, [], dev)

        opt_cmd = self._controller.tick(signals, params)
        sp = SafetyParams(
            hard_min=float(opts[OPT_HARD_MIN]),
            hard_max=float(opts[OPT_HARD_MAX]),
            cooldown_min=float(opts[OPT_SAFETY_COOLDOWN_MIN]),
        )
        cmd = self._safety.evaluate(signals, params, sp, opt_cmd, stale=stale)

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
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("%s: failed to apply command: %s", self.zone_name, err)

        # A held override re-asserts the same command every tick (deliberately —
        # the VRF is not trusted to keep it). Logging each re-assert made the
        # decision log read as actuator churn that never happened, so collapse
        # identical consecutive commands into one row (with a 5-min heartbeat).
        repeat = (cmd.mode, tuple(actions)) == self._last_log_key and \
            self._last_log_at is not None and (now - self._last_log_at) < timedelta(minutes=5)
        if actions and not repeat:
            self._last_log_key = (cmd.mode, tuple(actions))
            self._last_log_at = now
            await self.store.append_log({
                "t": now.isoformat(),
                "mode": cmd.mode,
                "reason": cmd.reason,
                "actions": actions,
                "comfort": round(comfort, 2) if comfort is not None else None,
                "target": round(target, 2),
                "power": round(power) if power is not None else None,
            })
            _LOGGER.info("%s: %s — %s", self.zone_name, cmd.reason, ", ".join(actions))

        # --- online self-evolution: learn only from clean, normal-state acts ---
        if self._safety.state == "normal":
            if cmd.set_ac_power is False:
                self._adapter.cancel()
            elif cmd.set_setpoint is not None and signals.setpoint is not None:
                self._adapter.on_setpoint_command(
                    now, cmd.set_setpoint - signals.setpoint, comfort,
                    at_floor=cmd.set_setpoint <= params.setpoint_min,
                )
        else:
            self._adapter.cancel()
        if self._adapter.observe(now, comfort, slope, target, band_low, band_high,
                                 hard_min=sp.hard_min, hard_max=sp.hard_max,
                                 saturated=(signals.setpoint is not None
                                            and signals.setpoint <= params.setpoint_min)):
            await self.store.set_model(self._predictor.params.to_dict())
            _LOGGER.info("%s: adapted model → %s", self.zone_name, self._predictor.params.to_dict())

        return self._snapshot(opts, signals, target, predicted, cmd.mode, cmd.reason,
                              is_night, actions, dev)

    def _snapshot(self, opts, signals, target, predicted, mode, reason, is_night, actions, dev):
        return {
            "name": self.zone_name,
            "enabled": self.enabled,
            "comfort": signals.comfort,
            "slope": signals.slope,
            "target": target,
            "predicted": predicted,
            "band_low": float(opts[OPT_BAND_LOW]),
            "band_high": float(opts[OPT_BAND_HIGH_NO_FAN] if not self.fan_assist else opts[OPT_BAND_HIGH]),
            "power": signals.power,
            "power_delta": signals.power_delta,
            "setpoint": signals.setpoint,
            "fan_level": signals.fan_level,
            "fan_on": dev["fan_on"],
            "fan_assist_enabled": self.fan_assist,
            "ac_on": dev["ac_on"],
            "ac_state": dev["ac_state"],
            "ac_blower": dev["ac_blower"],
            "mode": mode,
            "reason": reason,
            "strategy": opts[OPT_STRATEGY],
            "safety_state": self._safety.state,
            "is_night": is_night,
            "last_actions": actions,
            "entities": dev["entities"],
        }
