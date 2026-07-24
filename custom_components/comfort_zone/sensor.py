"""Diagnostic sensors for a comfort zone."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import ComfortZoneEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        _TempSensor(coordinator, "comfort", "Comfort temperature"),
        _TempSensor(coordinator, "target", "Target"),
        _TempSensor(coordinator, "predicted", "Predicted settled"),
        _StatusSensor(coordinator),
    ])


class _TempSensor(ComfortZoneEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator, key, name):
        super().__init__(coordinator, key)
        self._attr_name = name

    @property
    def native_value(self):
        v = self._snap.get(self._key)
        return round(v, 2) if isinstance(v, (int, float)) else None


class _StatusSensor(ComfortZoneEntity, SensorEntity):
    """The controller's current mode, with the decision reason + telemetry."""

    def __init__(self, coordinator):
        super().__init__(coordinator, "status")
        self._attr_name = "Status"
        self._attr_icon = "mdi:brain"

    @property
    def native_value(self):
        return self._snap.get("mode")

    @property
    def extra_state_attributes(self):
        s = self._snap
        strategy = s.get("strategy", "baby")
        entities = dict(s.get("entities") or {})
        entities["status"] = self.entity_id  # the card wires clicks off this
        return {
            "reason": s.get("reason"),
            "strategy": strategy,
            "safety_state": s.get("safety_state"),
            "enabled": s.get("enabled"),
            "is_night": s.get("is_night"),
            "slope": round(s["slope"], 3) if isinstance(s.get("slope"), (int, float)) else None,
            "power": s.get("power"),
            "power_delta": round(s["power_delta"]) if isinstance(s.get("power_delta"), (int, float)) else None,
            "setpoint": s.get("setpoint"),
            "fan_level": s.get("fan_level"),
            "fan_on": s.get("fan_on"),
            "fan_assist_enabled": s.get("fan_assist_enabled"),
            "ac_on": s.get("ac_on"),
            "ac_state": s.get("ac_state"),
            "ac_blower": s.get("ac_blower"),
            "band": s.get("band"),
            "last_actions": s.get("last_actions"),
            "entities": entities,
            # consumed by the custom card:
            "schedule": self.coordinator.store.schedule_for(strategy),
            "recent_log": self.coordinator.store.log[-20:],
        }
