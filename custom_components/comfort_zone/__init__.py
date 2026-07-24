"""Comfort Zone integration setup.

Home Assistant imports are deferred into the functions so the pure control core
(``controller``/``model``/``safety``/``comfort``) stays importable — and unit
testable — without Home Assistant installed.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .const import DOMAIN, PLATFORMS

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


async def async_setup_entry(hass: "HomeAssistant", entry: "ConfigEntry") -> bool:
    """Set up a comfort zone from a config entry."""
    from .coordinator import ComfortZoneCoordinator

    coordinator = ComfortZoneCoordinator(hass, entry)
    await coordinator.async_prepare()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await coordinator.async_start()

    await _async_register_services(hass)
    return True


async def async_unload_entry(hass: "HomeAssistant", entry: "ConfigEntry") -> bool:
    """Tear down a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_stop()
    return unloaded


async def _async_reload(hass: "HomeAssistant", entry: "ConfigEntry") -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_register_services(hass: "HomeAssistant") -> None:
    """Register domain services once."""
    if hass.services.has_service(DOMAIN, "identify_model"):
        return

    from .system_id import async_identify_service

    async def _identify(call):
        await async_identify_service(hass, call)

    async def _set_schedule(call):
        """Persist a zone's 48-point target curve (from the card)."""
        name = call.data.get("name")
        schedule = call.data["schedule"]
        for coord in hass.data.get(DOMAIN, {}).values():
            if name in (None, coord.zone_name):
                await coord.store.set_schedule([float(x) for x in schedule])
                await coord.async_request_refresh()

    hass.services.async_register(DOMAIN, "identify_model", _identify)
    hass.services.async_register(DOMAIN, "set_schedule", _set_schedule)
