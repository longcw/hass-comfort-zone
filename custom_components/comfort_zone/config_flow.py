"""Config + options flow for a comfort zone."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

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
    OPT_COMFORT_K,
    OPT_COMFORT_RH_REF,
    OPT_NIGHT_END,
    OPT_NIGHT_START,
    OPT_STRATEGY,
    OPTION_DEFAULTS,
    STRATEGIES,
)


def _entity(domain: str | list[str]):
    return selector.EntitySelector(selector.EntitySelectorConfig(domain=domain))


class ComfortZoneConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_signal()

        schema = vol.Schema({
            vol.Required(CONF_NAME, default="Master bedroom"): str,
            vol.Required(CONF_COMFORT_SOURCE, default="compute"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": "compute", "label": "Compute from temperature + humidity"},
                        {"value": "sensor", "label": "Use an existing comfort sensor"},
                    ],
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Required(CONF_AC_CLIMATE): _entity("climate"),
            vol.Optional(CONF_AC_POWER_SWITCH): _entity("switch"),
            vol.Optional(CONF_AC_POWER_SENSOR): _entity("sensor"),
            vol.Optional(CONF_FAN): _entity("fan"),
            vol.Optional(CONF_FAN_SPEED_NUMBER): _entity("number"),
        })
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_signal(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._data.update(user_input)
            await self.async_set_unique_id(self._data[CONF_NAME])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=self._data[CONF_NAME], data=self._data)

        if self._data.get(CONF_COMFORT_SOURCE) == "sensor":
            schema = vol.Schema({vol.Required(CONF_COMFORT_SENSOR): _entity("sensor")})
        else:
            schema = vol.Schema({
                vol.Required(CONF_TEMP_SENSOR): _entity("sensor"),
                vol.Required(CONF_HUMIDITY_SENSOR): _entity("sensor"),
            })
        return self.async_show_form(step_id="signal", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return ComfortZoneOptionsFlow(entry)


class ComfortZoneOptionsFlow(OptionsFlow):
    def __init__(self, entry: ConfigEntry) -> None:
        self.entry = entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data={**self.entry.options, **user_input})

        cur = {**OPTION_DEFAULTS, **self.entry.options}
        schema = vol.Schema({
            vol.Required(OPT_STRATEGY, default=cur[OPT_STRATEGY]): selector.SelectSelector(
                selector.SelectSelectorConfig(options=STRATEGIES)
            ),
            vol.Required(OPT_NIGHT_START, default=cur[OPT_NIGHT_START]): str,
            vol.Required(OPT_NIGHT_END, default=cur[OPT_NIGHT_END]): str,
            vol.Required(OPT_COMFORT_K, default=cur[OPT_COMFORT_K]): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=1, step=0.05)
            ),
            vol.Required(OPT_COMFORT_RH_REF, default=cur[OPT_COMFORT_RH_REF]): selector.NumberSelector(
                selector.NumberSelectorConfig(min=30, max=70, step=1)
            ),
        })
        return self.async_show_form(step_id="init", data_schema=schema)
