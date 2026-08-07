"""Persistence for a zone: fitted model, target schedule, switch state, decision log.

Backed by Home Assistant's ``Store`` helper (JSON under ``.storage``). The
decision log is bounded and is what the UI's history view and (eventually) the
heavier learning model read.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .const import DEFAULT_TARGET, MODEL_DEFAULTS, SCHEDULE_POINTS

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

STORAGE_VERSION = 1
MAX_LOG = 500  # decisions kept per zone


class ZoneStore:
    def __init__(self, hass: "HomeAssistant", entry_id: str) -> None:
        from homeassistant.helpers.storage import Store

        self._store = Store(hass, STORAGE_VERSION, f"comfort_zone.{entry_id}")
        self._data: dict[str, Any] = {}

    async def load(self) -> None:
        self._data = await self._store.async_load() or {}
        self._data.setdefault("model", dict(MODEL_DEFAULTS))
        self._data.setdefault("log", [])
        # Schedules are per-strategy so the target curve binds to the preset.
        # Migrate an older single-list schedule by seeding it under every key.
        if not isinstance(self._data.get("schedules"), dict):
            legacy = self._data.get("schedule")
            base = legacy if isinstance(legacy, list) and len(legacy) == SCHEDULE_POINTS \
                else [DEFAULT_TARGET] * SCHEDULE_POINTS
            self._data["schedules"] = {"_default": base}
            self._data.pop("schedule", None)

    async def _save(self) -> None:
        await self._store.async_save(self._data)

    # -- model --------------------------------------------------------------
    @property
    def model(self) -> dict:
        """Read-only. Nothing in the integration writes this key.

        The plant constants are fitted offline by ``tools/fit.py`` and edited into
        ``const.MODEL_DEFAULTS`` by a person who has read what that fit says it
        cannot identify — the edit *is* the review, and a write path would be a way
        around it. What remains here is the pre-v5 online adapter's store, still on
        disk and still discarded on load; see ``ModelParams.from_dict``.
        """
        return dict(self._data.get("model", MODEL_DEFAULTS))

    # -- schedule (48 × 30-min points), per strategy ------------------------
    def schedule_for(self, strategy: str) -> list[float]:
        schedules = self._data.get("schedules", {})
        sched = schedules.get(strategy) or schedules.get("_default") \
            or [DEFAULT_TARGET] * SCHEDULE_POINTS
        if len(sched) != SCHEDULE_POINTS:
            sched = (list(sched) + [DEFAULT_TARGET] * SCHEDULE_POINTS)[:SCHEDULE_POINTS]
        return list(sched)

    async def set_schedule(self, schedule: list[float], strategy: str) -> None:
        self._data.setdefault("schedules", {})[strategy] = list(schedule)[:SCHEDULE_POINTS]
        await self._save()

    # -- knobs, per mode ----------------------------------------------------
    def knobs(self, strategy: str) -> dict:
        """What the user has tuned in this mode; its preset seeds the rest."""
        return dict(self._data.get("knobs", {}).get(strategy, {}))

    async def set_knob(self, strategy: str, key: str, value: float) -> None:
        self._data.setdefault("knobs", {}).setdefault(strategy, {})[key] = float(value)
        await self._save()

    async def set_knobs(self, strategy: str, knobs: dict) -> None:
        self._data.setdefault("knobs", {})[strategy] = dict(knobs)
        await self._save()

    def target_at(self, hour: int, minute: int, strategy: str) -> float:
        idx = (hour * 60 + minute) // 30
        idx = max(0, min(SCHEDULE_POINTS - 1, idx))
        return float(self.schedule_for(strategy)[idx])

    # -- the loop's earned state --------------------------------------------
    def integral(self, max_age_min: float) -> float | None:
        """The PI integral this loop had converged on, if it is still fresh.

        The one piece of loop state not recomputed each tick, and worth 0.5–0.8 °C of
        setpoint on this room: the whole difference between the reset curve's guess at
        the load and the load the room really has. Thrown away on restart, the loop
        resumes on the curve alone and walks the room toward its cold rail while it
        re-earns the difference.

        Aged out rather than trusted forever — a load estimate from before an
        eight-hour outage describes yesterday's weather, and the loop is better off
        starting from the curve than from that.
        """
        rec = self._data.get("pi") or {}
        try:
            value, at = float(rec["integral"]), rec["at"]
        except (KeyError, TypeError, ValueError):
            return None
        from homeassistant.util import dt as dt_util
        stamped = dt_util.parse_datetime(at)
        if stamped is None:
            return None
        age_min = (dt_util.utcnow() - stamped).total_seconds() / 60.0
        return value if 0 <= age_min <= max_age_min else None

    async def set_integral(self, value: float, at: str) -> None:
        self._data["pi"] = {"integral": float(value), "at": at}
        await self._save()

    # -- switch state -------------------------------------------------------
    def flag(self, key: str, default: bool = True) -> bool:
        """A switch position the user set; it outlives reloads and restarts."""
        return bool(self._data.get("flags", {}).get(key, default))

    async def set_flag(self, key: str, value: bool) -> None:
        self._data.setdefault("flags", {})[key] = bool(value)
        await self._save()

    # -- decision log -------------------------------------------------------
    @property
    def log(self) -> list[dict]:
        return list(self._data.get("log", []))

    async def append_log(self, entry: dict) -> None:
        log = self._data.setdefault("log", [])
        log.append(entry)
        if len(log) > MAX_LOG:
            del log[: len(log) - MAX_LOG]
        await self._save()
