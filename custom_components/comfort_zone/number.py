"""Runtime-tunable knobs for a comfort zone (also editable from the card)."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    OPT_BAND,
    OPT_BAND_NO_FAN,
    OPT_FAN_MAX_DAY,
    OPT_FAN_MAX_NIGHT,
    OPT_HARD_MAX,
    OPT_HARD_MIN,
    OPT_SETPOINT_MAX,
    OPT_SETPOINT_MIN,
)
from .entity import ComfortZoneEntity


@dataclass(frozen=True)
class Knob:
    key: str
    name: str
    minimum: float
    maximum: float
    step: float
    unit: str
    icon: str


KNOBS: tuple[Knob, ...] = (
    Knob(OPT_BAND, "Band (fan on)", 0.2, 1.5, 0.05, "°C", "mdi:arrow-expand-vertical"),
    Knob(OPT_BAND_NO_FAN, "Band (fan off)", 0.2, 1.5, 0.05, "°C", "mdi:arrow-collapse-vertical"),
    Knob(OPT_HARD_MIN, "Hard min (safety)", 20, 26, 0.5, "°C", "mdi:thermometer-low"),
    Knob(OPT_HARD_MAX, "Hard max (safety)", 27, 32, 0.5, "°C", "mdi:thermometer-high"),
    Knob(OPT_SETPOINT_MIN, "AC setpoint min", 16, 27, 1, "°C", "mdi:snowflake"),
    Knob(OPT_SETPOINT_MAX, "AC setpoint max", 16, 30, 1, "°C", "mdi:snowflake-off"),
    Knob(OPT_FAN_MAX_DAY, "Fan max (day)", 10, 100, 5, "%", "mdi:fan"),
    Knob(OPT_FAN_MAX_NIGHT, "Fan max (night)", 10, 100, 5, "%", "mdi:fan-off"),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([_KnobNumber(coordinator, k) for k in KNOBS])


class _KnobNumber(ComfortZoneEntity, NumberEntity):
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, knob: Knob):
        super().__init__(coordinator, knob.key)
        self._knob = knob
        self._attr_name = knob.name
        self._attr_native_min_value = knob.minimum
        self._attr_native_max_value = knob.maximum
        self._attr_native_step = knob.step
        self._attr_native_unit_of_measurement = knob.unit
        self._attr_icon = knob.icon

    @property
    def native_value(self) -> float:
        return float(self.coordinator.options()[self._knob.key])

    async def async_set_native_value(self, value: float) -> None:
        options = {**(self.coordinator.entry.options or {}), self._knob.key: value}
        self.hass.config_entries.async_update_entry(self.coordinator.entry, options=options)
