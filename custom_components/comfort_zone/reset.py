"""Outdoor reset — the feedforward that answers the weather before the room does.

    u_ff = c0 + c_out · T_outdoor + c_tgt · target

The setpoint the current load calls for, computed rather than waited for, so the
feedback loop is left with the residual instead of with the weather.

**Fitted from the old controller's own output, not from the plant.** The obvious
route — invert the steady-state plant for ``comfort = target`` — needs the plant
gain, and a week of closed-loop history will not give that up: the controller moves
the setpoint because the room moved, so a regression of room on setpoint recovers
the controller's inverse (measured: a gain of −0.04 °C/°C, and a blower that
appeared to *warm* the room). Regressing the controller's **output** on an
exogenous input has no such problem. Every sample where the old system actually
held the room on target is a labelled example of "this load wanted this setpoint",
which is exactly the question the feedforward asks. See :mod:`tools.fit`.

Anticipation lives here and nowhere else. Rather than extrapolating the room's own
slope — which measured as worthless over 24 h of history, and which cannot tell a
disturbance from the controller's own unanswered command — the curve is evaluated
at the outdoor temperature expected one plant horizon (dead time + time constant)
ahead. That is a physical prediction of the disturbance, and it is bounded, so a
wrong forecast costs at most :data:`FORECAST_CAP`.
"""
from __future__ import annotations

from datetime import datetime, timedelta

# °C of setpoint the forecast may shift the feedforward away from the reading. The
# forecast is a regional hourly model and this is one room: it is worth acting on
# for the direction of an afternoon, never for its magnitude.
FORECAST_CAP = 0.5


def interpolate(forecast: list[tuple[datetime, float]] | None,
                at: datetime) -> float | None:
    """Linear interpolation of an hourly forecast at ``at``.

    None outside the forecast's range rather than an extrapolation — past the last
    point there is no information, only the slope of the final segment.
    """
    if not forecast or len(forecast) < 2:
        return None
    pts = sorted(forecast, key=lambda p: p[0])
    if at <= pts[0][0] or at >= pts[-1][0]:
        return None
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        if t0 <= at <= t1:
            span = (t1 - t0).total_seconds()
            return v0 if span <= 0 else v0 + (v1 - v0) * (at - t0).total_seconds() / span
    return None


def curve(target: float, outdoor: float, m) -> float:
    """The reset curve itself, with no forecast term."""
    return m.ff_intercept + m.ff_per_outdoor * outdoor + m.ff_per_target * target


def feedforward(now: datetime, target: float, outdoor: float | None,
                forecast: list[tuple[datetime, float]] | None, m) -> float | None:
    """The load's own setpoint, biased toward the load one plant horizon ahead.

    None when there is no outdoor reading at all; the caller then holds its last
    value, and failing that runs on feedback alone — slower, but the integrator
    converges to the same place.
    """
    if outdoor is None:
        return None
    base = curve(target, outdoor, m)
    ahead = interpolate(forecast, now + timedelta(minutes=m.dead_time_min + m.tau_min))
    if ahead is None:
        return base
    shift = curve(target, ahead, m) - base
    return base + max(-FORECAST_CAP, min(FORECAST_CAP, shift))
