"""Persistence for a zone: fitted model, target schedule, strategy, decision log.

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
        return dict(self._data.get("model", MODEL_DEFAULTS))

    async def set_model(self, model: dict) -> None:
        self._data["model"] = dict(model)
        await self._save()

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

    def target_at(self, hour: int, minute: int, strategy: str) -> float:
        idx = (hour * 60 + minute) // 30
        idx = max(0, min(SCHEDULE_POINTS - 1, idx))
        return float(self.schedule_for(strategy)[idx])

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
