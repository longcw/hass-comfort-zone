"""PI feedback with dead-time compensation and back-calculation anti-windup.

The residual the feedforward missed, and nothing else. Three deliberate choices:

**PI, not PID.** The regulated signal is a BLE thermometer that reports every few
minutes and can go quiet for ten. Derivative action on it amplifies noise and buys
nothing against a ten-minute dead time.

**The error is the Smith-predictor error**, ``target − predict_settled``, not
``target − comfort``. Feeding back a signal that will not move for a dead time is
how every previous version stacked commands into the lag; feeding back where the
room is *going to settle* removes the dead time from the loop, which is what makes
a textbook tuning rule apply at all.

**Anti-windup is not optional here.** The output saturates at the setpoint limits
for hours on a hot afternoon. Without protection the integral keeps growing while
the actuator cannot answer, and then holds the setpoint at the floor long after
the room has come back — which is what most of the v3/v4 overshoot looks like in
hindsight: an integrator nobody had written down, and therefore nobody had
protected. :meth:`PiController.step` uses back-calculation, unwinding the integral
in proportion to how far the request exceeded what the actuator can deliver.

Tuning is SIMC (Skogestad), computed from the fitted plant rather than stored, so
the constants cannot drift out of step with the model they came from — see
:class:`model.ModelParams`.
"""
from __future__ import annotations

from dataclasses import dataclass

# Integral clamp, in °C of setpoint beyond the actuator's own span. Back-calculation
# already holds the integral at the saturation boundary; this is the backstop for the
# one case it cannot see — a tick gap so long (a restart, a stalled sensor) that a
# single integration step overshoots before the next correction arrives.
INTEGRAL_MARGIN = 2.0
# Longest tick a single integration step may account for. Ticks are 45 s; anything
# far beyond that is a gap in service, not a slow loop, and integrating across it
# would credit the controller for minutes it was not actually regulating.
MAX_DT_MIN = 2.0


@dataclass
class PiOutput:
    u: float                # what the actuator can actually deliver, clamped
    u_raw: float            # what the controller asked for, before the clamp
    u_ff: float             # the feedforward's share
    u_fb: float             # the feedback's share (proportional + integral)
    integral: float
    error: float
    saturated: bool
    frozen: bool


class PiController:
    def __init__(self, params) -> None:
        self.params = params
        self.integral = 0.0

    def reset(self) -> None:
        self.integral = 0.0

    def step(self, *, error: float, u_ff: float, dt_min: float,
             lo: float, hi: float, frozen: bool = False) -> PiOutput:
        """Advance one tick and return the requested and deliverable outputs.

        ``lo``/``hi`` are what the *output stage* can deliver, not the setpoint
        limits: the blower extends the cold end below the setpoint floor, and
        anti-windup has to unwind against the real boundary or it will hold the
        integral against a limit the actuator had already passed.

        ``frozen`` suspends integration for a tick — used whenever the actuator is
        not the controller's to move (the safety guard has the room, the AC is off,
        or a managed-off is in progress). The proportional term still responds, so
        the loop resumes from the right place rather than from a stale integral.
        """
        m = self.params
        kc, ti = m.kc, m.ti_min
        u_fb = kc * error + self.integral
        u_raw = u_ff + u_fb
        u = max(lo, min(hi, u_raw))

        if not frozen:
            dt = max(0.0, min(dt_min, MAX_DT_MIN))
            # back-calculation: integrate the error, then unwind whatever the
            # actuator could not deliver, with a tracking time of Ti
            self.integral += (kc / max(ti, 1e-6)) * error * dt
            self.integral += (u - u_raw) * dt / max(ti, 1e-6)
            span = (hi - lo) + INTEGRAL_MARGIN
            self.integral = max(-span, min(span, self.integral))

        return PiOutput(
            u=u, u_raw=u_raw, u_ff=u_ff, u_fb=u_fb,
            integral=self.integral, error=error,
            saturated=abs(u - u_raw) > 1e-6, frozen=frozen,
        )
