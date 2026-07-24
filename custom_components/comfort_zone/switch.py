"""Enable/disable switch for a comfort zone (gates all actuation)."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import ComfortZoneEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([_EnableSwitch(coordinator), _FanAssistSwitch(coordinator)])


class _EnableSwitch(ComfortZoneEntity, SwitchEntity):
    def __init__(self, coordinator):
        super().__init__(coordinator, "enable")
        self._attr_name = "Enabled"
        self._attr_icon = "mdi:power"

    @property
    def is_on(self) -> bool:
        return self.coordinator.enabled

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.set_enabled(True)
        self.async_write_ha_state()  # reflect instantly, don't wait for the tick
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.set_enabled(False)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()


class _FanAssistSwitch(ComfortZoneEntity, SwitchEntity):
    """Enable/disable the circulation fan. When off, the AC runs a tighter band."""

    def __init__(self, coordinator):
        super().__init__(coordinator, "fan_assist")
        self._attr_name = "Fan assist"
        self._attr_icon = "mdi:fan"

    @property
    def is_on(self) -> bool:
        return self.coordinator.fan_assist

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.set_fan_assist(True)
        self.async_write_ha_state()  # reflect instantly, don't wait for the tick
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.set_fan_assist(False)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()
