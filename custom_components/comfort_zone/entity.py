"""Shared base for Comfort Zone entities."""
from __future__ import annotations

from homeassistant.helpers.device_info import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ComfortZoneCoordinator


class ComfortZoneEntity(CoordinatorEntity[ComfortZoneCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: ComfortZoneCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=coordinator.zone_name,
            manufacturer="Comfort Zone",
            model="Adaptive climate zone",
        )

    @property
    def _snap(self) -> dict:
        return self.coordinator.data or {}
