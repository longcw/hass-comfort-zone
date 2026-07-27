"""Strategy selector for a comfort zone."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, OPT_STRATEGY, STRATEGIES
from .entity import ComfortZoneEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([_StrategySelect(coordinator)])


class _StrategySelect(ComfortZoneEntity, SelectEntity):
    _attr_options = STRATEGIES

    def __init__(self, coordinator):
        super().__init__(coordinator, "strategy")
        self._attr_name = "Strategy"
        self._attr_icon = "mdi:tune-variant"

    @property
    def current_option(self) -> str:
        return self.coordinator.options()[OPT_STRATEGY]

    async def async_select_option(self, option: str) -> None:
        options = {**(self.coordinator.entry.options or {}), OPT_STRATEGY: option}
        self.hass.config_entries.async_update_entry(self.coordinator.entry, options=options)
        self.async_write_ha_state()  # reflect instantly, don't wait for the tick
