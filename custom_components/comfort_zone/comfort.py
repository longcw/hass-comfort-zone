"""Comfort-temperature computation.

`comfort_temp` is the single regulated signal. It is raw temperature nudged by a
*damped* humidity term, anchored so that at the reference RH the signal equals
raw temperature (so temperature-tuned targets carry over unchanged):

    comfort = T + k · 0.33 · (e − e_ref)
            = T + k · 0.33 · ((RH − RH_ref)/100) · es(T)

where es(T) is the saturation vapour pressure (hPa) and 0.33 °C/hPa is the
Steadman humidity coefficient. k in [0,1] bounds how much mugginess the
controller is allowed to chase with cold air (k=0 ⇒ pure temperature).
"""
from __future__ import annotations

import math


def saturation_vapour_pressure(temp_c: float) -> float:
    """Saturation vapour pressure in hPa (Magnus formula)."""
    return 6.105 * math.exp(17.27 * temp_c / (237.7 + temp_c))


def comfort_temp(temp_c: float, rh_pct: float, k: float, rh_ref: float) -> float:
    """Humidity-damped comfort temperature in °C."""
    es = saturation_vapour_pressure(temp_c)
    return temp_c + k * 0.33 * ((rh_pct - rh_ref) / 100.0) * es
