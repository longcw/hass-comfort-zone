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
        self._data.setdefault("schedule", [DEFAULT_TARGET] * SCHEDULE_POINTS)
        self._data.setdefault("log", [])

    async def _save(self) -> None:
        await self._store.async_save(self._data)

    # -- model --------------------------------------------------------------
    @property
    def model(self) -> dict:
        return dict(self._data.get("model", MODEL_DEFAULTS))

    async def set_model(self, model: dict) -> None:
        self._data["model"] = dict(model)
        await self._save()

    # -- schedule (48 × 30-min points) --------------------------------------
    @property
    def schedule(self) -> list[float]:
        sched = self._data.get("schedule") or [DEFAULT_TARGET] * SCHEDULE_POINTS
        if len(sched) != SCHEDULE_POINTS:
            sched = (sched + [DEFAULT_TARGET] * SCHEDULE_POINTS)[:SCHEDULE_POINTS]
        return sched

    async def set_schedule(self, schedule: list[float]) -> None:
        self._data["schedule"] = list(schedule)[:SCHEDULE_POINTS]
        await self._save()

    def target_at(self, hour: int, minute: int) -> float:
        idx = (hour * 60 + minute) // 30
        idx = max(0, min(SCHEDULE_POINTS - 1, idx))
        return float(self.schedule[idx])

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
