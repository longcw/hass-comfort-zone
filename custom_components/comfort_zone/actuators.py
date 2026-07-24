"""Translate a :class:`Command` into Home Assistant service calls.

All device-facing side effects live here so the control logic stays pure. Every
call is guarded: we only actuate entities that were actually bound, and AC
power uses the *reliable power switch* (never HVAC-mode, which this VRF ACKs but
doesn't reliably act on).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .controller import Command

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


@dataclass
class Bindings:
    ac_climate: str
    ac_power_switch: str | None
    fan: str | None
    fan_speed_number: str | None
    blower_levels: list[str]


async def apply(hass: "HomeAssistant", b: Bindings, cmd: Command) -> list[str]:
    """Apply a command; return a list of the actions actually taken (for logging)."""
    done: list[str] = []

    # AC power (managed on/off) — do this first so a following setpoint sticks.
    if cmd.set_ac_power is not None and b.ac_power_switch:
        service = "turn_on" if cmd.set_ac_power else "turn_off"
        await hass.services.async_call(
            "switch", service, {"entity_id": b.ac_power_switch}, blocking=True
        )
        done.append(f"ac_power={'on' if cmd.set_ac_power else 'off'}")

    if cmd.set_setpoint is not None:
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": b.ac_climate, "temperature": cmd.set_setpoint},
            blocking=True,
        )
        done.append(f"setpoint={cmd.set_setpoint}")

    if cmd.set_blower_idx is not None and b.blower_levels:
        idx = max(0, min(len(b.blower_levels) - 1, cmd.set_blower_idx))
        await hass.services.async_call(
            "climate",
            "set_fan_mode",
            {"entity_id": b.ac_climate, "fan_mode": b.blower_levels[idx]},
            blocking=True,
        )
        done.append(f"blower={b.blower_levels[idx]}")

    if cmd.set_fan is not None and b.fan:
        service = "turn_on" if cmd.set_fan else "turn_off"
        await hass.services.async_call(
            "fan", service, {"entity_id": b.fan}, blocking=True
        )
        done.append(f"fan={'on' if cmd.set_fan else 'off'}")

    # Fan speed via the non-lossy number entity, only when the fan is/stays on.
    if cmd.set_fan_level is not None and b.fan_speed_number and cmd.set_fan is not False:
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": b.fan_speed_number, "value": cmd.set_fan_level},
            blocking=True,
        )
        done.append(f"fan_level={cmd.set_fan_level}")

    return done
