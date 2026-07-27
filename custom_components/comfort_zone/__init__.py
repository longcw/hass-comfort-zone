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
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
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


async def _async_options_updated(hass: "HomeAssistant", entry: "ConfigEntry") -> None:
    """Apply an option change on the next tick without rebuilding the zone.

    Options are read live from the entry, so a reload would only discard what
    the running zone has learned — power/comfort history, the safety cooldown,
    the controller's dead-time timers. Rebinding entities (the reconfigure
    step) reloads on its own.
    """
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is None:  # a concurrent reload already replaced the zone
        return
    coordinator.async_update_listeners()  # options-flow edits show up now, not after the tick
    await coordinator.async_request_refresh()


async def _async_register_services(hass: "HomeAssistant") -> None:
    """Register domain services once."""
    if hass.services.has_service(DOMAIN, "identify_model"):
        return

    from .system_id import async_identify_service

    async def _identify(call):
        await async_identify_service(hass, call)

    async def _set_schedule(call):
        """Persist a zone's 48-point target curve for its current strategy."""
        from .const import OPT_STRATEGY

        name = call.data.get("name")
        schedule = call.data["schedule"]
        strategy = call.data.get("strategy")
        for coord in hass.data.get(DOMAIN, {}).values():
            if name in (None, coord.zone_name):
                strat = strategy or coord.options()[OPT_STRATEGY]
                await coord.store.set_schedule([float(x) for x in schedule], strat)
                await coord.async_request_refresh()

    hass.services.async_register(DOMAIN, "identify_model", _identify)
    hass.services.async_register(DOMAIN, "set_schedule", _set_schedule)
    # Note: continuous self-evolution happens online in the coordinator
    # (see adapt.OnlineAdapter). `identify_model` is a manual full-history refit.
