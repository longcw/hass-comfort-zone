"""Offline system identification — fit the FOPDT model from recorder history.

This is v1's "learning": it does not train a black box, it *identifies* the few
interpretable constants the controller needs — the power→comfort lead, and the
setpoint step response (dead-time, time-constant, gain). Re-run it (service
``comfort_zone.identify_model``) after collecting more data; on any gap it keeps
the existing/default constants rather than fitting garbage.
"""
from __future__ import annotations

import logging
import math
from datetime import timedelta
from typing import TYPE_CHECKING

from .comfort import comfort_temp
from .const import (
    CONF_AC_CLIMATE,
    CONF_AC_POWER_SENSOR,
    CONF_COMFORT_SENSOR,
    CONF_COMFORT_SOURCE,
    CONF_HUMIDITY_SENSOR,
    CONF_TEMP_SENSOR,
    DOMAIN,
    MK_DEAD_TIME,
    MK_GAIN,
    MK_POWER_LEAD,
    MK_TAU,
    OPT_COMFORT_K,
    OPT_COMFORT_RH_REF,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall

_LOGGER = logging.getLogger(__name__)
FIT_DAYS = 7
LAGS = [0, 2, 4, 6, 8, 10, 12, 15]
STEP_WINDOWS = [5, 10, 15, 20, 25, 30]


def _grid(states, start, minutes, transform=float):
    """Forward-fill a list of States onto a 1-minute grid of length ``minutes``."""
    pts = []
    for s in states:
        try:
            pts.append((s.last_updated, transform(s)))
        except (ValueError, TypeError):
            continue
    pts = [(t, v) for (t, v) in pts if v is not None]
    pts.sort(key=lambda x: x[0])
    out: list[float | None] = []
    j, cur = 0, None
    for k in range(minutes):
        tk = start + timedelta(minutes=k)
        while j < len(pts) and pts[j][0] <= tk:
            cur = pts[j][1]
            j += 1
        out.append(cur)
    return out


def _slope(grid, w=5):
    out = [None] * len(grid)
    for i in range(len(grid)):
        if i - w < 0 or i + w >= len(grid):
            continue
        a, b = grid[i - w], grid[i + w]
        if a is None or b is None:
            continue
        out[i] = (b - a) / (2 * w)
    return out


def _delta(grid, w=5):
    out = [None] * len(grid)
    for i in range(len(grid)):
        if i - w >= 0 and grid[i] is not None and grid[i - w] is not None:
            out[i] = grid[i] - grid[i - w]
    return out


def _corr(x, y):
    pairs = [(a, b) for a, b in zip(x, y) if a is not None and b is not None]
    if len(pairs) < 30:
        return None
    xs = [a for a, _ in pairs]
    ys = [b for _, b in pairs]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    num = sum((a - mx) * (b - my) for a, b in pairs)
    dx = math.sqrt(sum((a - mx) ** 2 for a in xs))
    dy = math.sqrt(sum((b - my) ** 2 for b in ys))
    return num / (dx * dy) if dx and dy else None


def _median(xs):
    xs = sorted(v for v in xs if v is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def _fit_from_grids(comfort, power, setpoint, minutes) -> dict:
    """Return the subset of fitted model constants we're confident about."""
    fitted: dict = {}

    s_comfort = _slope(comfort)

    # -- power lead: lag maximising |corr(Δpower, future slope)| -------------
    if power is not None:
        d_pw = _delta(power)
        best_lag, best_c = None, 0.0
        for lag in LAGS:
            shifted = [s_comfort[i + lag] if i + lag < minutes else None for i in range(minutes)]
            c = _corr(d_pw, shifted)
            if c is not None and abs(c) > abs(best_c):
                best_lag, best_c = lag, c
        if best_lag is not None and best_c < -0.15:  # only trust a real negative lead
            fitted[MK_POWER_LEAD] = float(max(2, best_lag))

    # -- setpoint step response: dead-time, tau, gain -----------------------
    events = [
        i for i in range(1, minutes)
        if setpoint[i] is not None and setpoint[i - 1] is not None and setpoint[i] < setpoint[i - 1]
    ]
    if len(events) >= 8:
        curve = {}
        for w in STEP_WINDOWS:
            deltas = [
                comfort[i + w] - comfort[i]
                for i in events
                if i + w < minutes and comfort[i] is not None and comfort[i + w] is not None
            ]
            curve[w] = _median(deltas)
        settled = curve.get(max(STEP_WINDOWS))
        if settled is not None and settled < -0.08:
            fitted[MK_GAIN] = round(abs(settled), 3)  # per ~1°C step (events are unit steps)
            # dead-time: first window reaching 15% of settled
            L = next((w for w in STEP_WINDOWS if curve.get(w) and curve[w] <= 0.15 * settled), 8)
            # 63% point → dead-time + tau
            t63 = next((w for w in STEP_WINDOWS if curve.get(w) and curve[w] <= 0.63 * settled), L + 8)
            fitted[MK_DEAD_TIME] = float(L)
            fitted[MK_TAU] = float(max(3, t63 - L))

    return fitted


async def async_identify_service(hass: "HomeAssistant", call: "ServiceCall") -> None:
    from homeassistant.components.recorder import get_instance, history

    coordinators = list(hass.data.get(DOMAIN, {}).values())
    if not coordinators:
        return

    end = None
    from homeassistant.util import dt as dt_util

    end = dt_util.utcnow()
    start = end - timedelta(days=FIT_DAYS)
    minutes = FIT_DAYS * 24 * 60

    for coord in coordinators:
        d = coord.entry.data
        opts = coord.options()
        ent_ids = [d[CONF_AC_CLIMATE]]
        if d.get(CONF_AC_POWER_SENSOR):
            ent_ids.append(d[CONF_AC_POWER_SENSOR])
        source = d.get(CONF_COMFORT_SOURCE, "compute")
        if source == "sensor":
            ent_ids.append(d[CONF_COMFORT_SENSOR])
        else:
            ent_ids += [d[CONF_TEMP_SENSOR], d[CONF_HUMIDITY_SENSOR]]

        states = await get_instance(hass).async_add_executor_job(
            history.get_significant_states, hass, start, end, ent_ids, None, False, False
        )

        def num(entity):
            return _grid(states.get(entity, []), start, minutes,
                         lambda s: float(s.state) if s.state not in ("unknown", "unavailable") else None)

        power = num(d[CONF_AC_POWER_SENSOR]) if d.get(CONF_AC_POWER_SENSOR) else None
        setpoint = _grid(
            states.get(d[CONF_AC_CLIMATE], []), start, minutes,
            lambda s: float(s.attributes["temperature"]) if s.attributes.get("temperature") is not None else None,
        )
        if source == "sensor":
            comfort = num(d[CONF_COMFORT_SENSOR])
        else:
            temp = num(d[CONF_TEMP_SENSOR])
            hum = num(d[CONF_HUMIDITY_SENSOR])
            k, rh_ref = opts[OPT_COMFORT_K], opts[OPT_COMFORT_RH_REF]
            comfort = [
                comfort_temp(t, h, k, rh_ref) if t is not None and h is not None else None
                for t, h in zip(temp, hum)
            ]

        fitted = _fit_from_grids(comfort, power, setpoint, minutes)
        if fitted:
            model = {**coord.store.model, **fitted}
            await coord.store.set_model(model)
            coord.reload_model()
            _LOGGER.info("%s: identified model %s", coord.zone_name, fitted)
        else:
            _LOGGER.warning("%s: not enough clean history to fit; keeping current model", coord.zone_name)
