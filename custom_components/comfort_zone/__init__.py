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
    from .const import OPT_STRATEGY, OPTION_DEFAULTS
    from .coordinator import ComfortZoneCoordinator
    from .options import split_modal

    coordinator = ComfortZoneCoordinator(hass, entry)
    await coordinator.async_prepare()

    # Entries written before modes were profiles keep every knob on the entry,
    # where one tuned value shadowed all four modes. Hand them to the mode that
    # was active — the values it was running become the values it now owns.
    zone, knobs = split_modal(entry.options or {})
    if knobs:
        strategy = zone.get(OPT_STRATEGY, OPTION_DEFAULTS[OPT_STRATEGY])
        await coordinator.store.set_knobs(strategy, {**coordinator.store.knobs(strategy), **knobs})
        hass.config_entries.async_update_entry(entry, options=zone)

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
    from .const import OPT_STRATEGY

    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is None:  # a concurrent reload already replaced the zone
        return
    coordinator.async_update_listeners()  # options-flow edits show up now, not after the tick
    # A knob nudge rides the debouncer — the card's steppers fire in bursts and each
    # tick costs a round trip to the AC's cloud API. A mode switch changes the target
    # curve and every band at once, so it rebuilds the snapshot immediately instead
    # of leaving the old numbers up for as long as a tick.
    if (coordinator.data or {}).get("strategy") != coordinator.options()[OPT_STRATEGY]:
        await coordinator.async_refresh()
    else:
        await coordinator.async_request_refresh()


async def _async_register_services(hass: "HomeAssistant") -> None:
    """Register domain services once."""
    if hass.services.has_service(DOMAIN, "set_schedule"):
        return

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

    hass.services.async_register(DOMAIN, "set_schedule", _set_schedule)
    # The model is fitted offline by tools/fit.py and reviewed by a human before it
    # reaches const.MODEL_DEFAULTS. There is no service to refit it in place: a
    # constant that changes without review is how a broken engagement rule ratcheted
    # the dead time to 19 minutes and then paced every dwell off it.
