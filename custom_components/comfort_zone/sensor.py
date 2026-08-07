"""Diagnostic sensors for a comfort zone."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, OPT_STRATEGY
from .entity import ComfortZoneEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        _TempSensor(coordinator, "comfort", "Comfort temperature"),
        _TempSensor(coordinator, "target", "Target"),
        _TempSensor(coordinator, "predicted", "Predicted settled"),
        _SlopeSensor(coordinator),
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


class _SlopeSensor(ComfortZoneEntity, SensorEntity):
    """The regulated signal's rate of change (°C/min) — has its own history."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "°C/min"
    _attr_suggested_display_precision = 3
    _attr_icon = "mdi:slope-uphill"

    def __init__(self, coordinator):
        super().__init__(coordinator, "slope")
        self._attr_name = "Rate of change"

    @property
    def native_value(self):
        v = self._snap.get("slope")
        return round(v, 3) if isinstance(v, (int, float)) else None


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
        # Configuration, not tick output: read live so the card redraws on the
        # click. Taken from the snapshot, the previous mode's curve and bands
        # stayed on screen until a tick rebuilt it — as long as 45s away.
        opts = self.coordinator.options()
        strategy = opts[OPT_STRATEGY]
        band_low, band_high = self.coordinator.bands(opts)
        entities = dict(s.get("entities") or {})
        entities["status"] = self.entity_id  # the card wires clicks off this
        return {
            "reason": s.get("reason"),
            # What decided, and the quantities behind it — the card's "why" panel
            # renders these so a decision can be judged without the source.
            "branch": s.get("branch"),
            "decision": s.get("decision"),
            "strategy": strategy,
            "safety_state": s.get("safety_state"),
            "enabled": s.get("enabled"),
            "is_night": s.get("is_night"),
            "slope": round(s["slope"], 3) if isinstance(s.get("slope"), (int, float)) else None,
            "outdoor": s.get("outdoor"),
            "power": s.get("power"),
            "power_recent": round(s["power_recent"]) if isinstance(s.get("power_recent"), (int, float)) else None,
            "setpoint": s.get("setpoint"),
            # How long the unit has held it — the compressor-calm number, and the
            # one a reader checks when asking "is it fussing?"
            "sp_held_min": s.get("sp_held_min"),
            "fan_level": s.get("fan_level"),
            "fan_on": s.get("fan_on"),
            "fan_assist_enabled": s.get("fan_assist_enabled"),
            "ac_on": s.get("ac_on"),
            "ac_state": s.get("ac_state"),
            "ac_blower": s.get("ac_blower"),
            "band_low": band_low,
            "band_high": band_high,
            # The inner zone actually being held — narrower than the band, and
            # shifted down whenever no fan can run.
            "zone_lo": s.get("zone_lo"),
            "zone_hi": s.get("zone_hi"),
            # The rails actually in force. Worth surfacing because they are not always
            # the configured ones: a rail nearer the band than its clearance is pushed
            # out (see safety.effective_rails), and that has to be visible somewhere.
            "hard_min": s.get("hard_min"),
            "hard_max": s.get("hard_max"),
            # The quiet limits in force now, so the card need not work out the window.
            "fan_max_level": s.get("fan_max_level"),
            "blower_max": s.get("blower_max"),
            "last_actions": s.get("last_actions"),
            "entities": entities,
            # consumed by the custom card:
            "schedule": self.coordinator.store.schedule_for(strategy),
        }
