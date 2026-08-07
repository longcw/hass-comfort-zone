"""Strategy selector and the AC blower's quiet caps."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    OPT_BLOWER_MAX_DAY,
    OPT_BLOWER_MAX_NIGHT,
    OPT_STRATEGY,
    STRATEGIES,
)
from .controller import regular_blower_top
from .entity import ComfortZoneEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        _StrategySelect(coordinator),
        _BlowerCapSelect(coordinator, OPT_BLOWER_MAX_DAY, "AC blower max (day)"),
        _BlowerCapSelect(coordinator, OPT_BLOWER_MAX_NIGHT, "AC blower max (night)"),
    ])


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
        # Only the name is stored: the mode's knobs and its curve are kept under
        # that name in the zone store, so switching loads whatever it last held.
        options = {**(self.coordinator.entry.options or {}), OPT_STRATEGY: option}
        self.hass.config_entries.async_update_entry(self.coordinator.entry, options=options)
        self.async_write_ha_state()  # reflect instantly, don't wait for the tick


class _BlowerCapSelect(ComfortZoneEntity, SelectEntity):
    """The highest AC blower speed the optimizer may use in one window.

    Offered as the device's own speed labels rather than a ladder index, and only
    for the speeds that can actually be honoured — the ladder's top belongs to the
    safety guard, so listing it would be a setting that silently does nothing.
    """

    _attr_icon = "mdi:fan-chevron-up"

    def __init__(self, coordinator, key: str, name: str):
        super().__init__(coordinator, key)
        self._attr_name = name

    @property
    def _levels(self) -> list[str]:
        levels = self.coordinator.blower_levels()
        return levels[: regular_blower_top(levels) + 1]

    @property
    def options(self) -> list[str]:
        return self._levels

    @property
    def current_option(self) -> str | None:
        levels = self._levels
        if not levels:
            return None
        idx = int(self.coordinator.options()[self._key])
        return levels[max(0, min(len(levels) - 1, idx))]

    async def async_select_option(self, option: str) -> None:
        levels = self._levels
        if option not in levels:
            return
        await self.coordinator.async_set_option(self._key, levels.index(option))
        self.async_write_ha_state()
