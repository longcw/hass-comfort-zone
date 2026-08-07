"""Power as a leading indicator of what the room is about to do.

The unit sitting at its usual 1–2 kW is holding station. When it jumps to 4–5 kW,
cold is on its way; when it falls to nothing, the room is about to start warming.
Either way the meter knows minutes before the thermometer does, and acting on the
change after it arrives is exactly the lag this whole design exists to remove.

**This is not the engagement detector that was deleted, and the difference matters.**
That one asked "did my command take?" and answered with a binary veto that could
outrank the model — on a whole-house meter it was wrong four times out of four. This
asks a different question, "what is about to happen to the room?", and answers it
with a *bounded bias* on the demand, small enough that it cannot move the integer
setpoint on its own. It can be wrong without being harmful because of that cap —
NOT because anything downstream corrects it (see the note at the end).

Four things keep the shared meter from poisoning it:

* **A slow baseline.** The signal is deviation from what this unit normally draws
  while holding, not an absolute level, so another room's steady load cancels out.
* **A threshold that clears the meter's own noise.** With no command in the window
  the window-mean trend already reaches ±195 W (p90), while a real ramp measures
  +450…+650 W. Below :data:`DEADBAND_W` nothing is claimed.
* **Silence after our own commands.** For a dead time after the setpoint moves, the
  power change *is* our command arriving, and the Smith predictor has already
  accounted for it. Reading it again here would double-count it and then fight it —
  which is precisely how the old power logic cancelled its own steps.
* **A persistence requirement, and it is what makes the low side safe at all.**
  This unit duty-cycles: ~18 min on, 2–5 min off. Power near zero is therefore the
  NORMAL state of a unit that has reached temperature, and reading it as "the room
  is about to warm, cool harder" is positive feedback straight into cooling — cold
  setpoint, compressor runs, bias goes positive, setpoint rises, compressor stops,
  bias goes negative, setpoint falls. So a deviation must hold for
  :data:`PERSIST_MIN`, comfortably longer than the longest measured off-phase,
  before it is believed. A duty cycle cannot clear that bar; a unit that has
  genuinely stopped, or genuinely gone to full tilt, does.

The baseline is taken over RUNNING samples only, for the same reason: a median that
includes off-phases is dragged toward them, and then every on-phase reads as a surge.

Note what this module must NOT rely on: the integral. Inside the comfort zone the error is exactly zero by design, so the integral is
frozen there and a standing bias sits uncorrected. That is why the authority is
capped small enough (:data:`CAP`) that it cannot move the integer setpoint on its
own, rather than because something downstream would clean it up.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from statistics import median

# Deviation from the holding baseline, in watts, below which nothing is claimed.
# Sized from the measured separation on this whole-house meter, not from taste.
DEADBAND_W = 600.0
# Watts of deviation that justify a full CAP of bias. A unit that normally holds at
# ~1.5 kW and peaks near 5 kW gives roughly this much headroom above the deadband.
FULL_SCALE_W = 2500.0
# Most the signal may ever move the demand, in °C of setpoint. Small on purpose: a
# shared meter earns a nudge, never a decision.
CAP = 0.4
# Below this the unit is not running, whatever the climate entity says: the
# compressor duty-cycles independently of the on/off state. Samples taken here
# describe the rest of the house, not this room.
RUNNING_W = 200.0
# How long a deviation must hold before it is believed. Longer than the longest
# measured off-phase (2–5 min), so ordinary duty cycling can never clear it.
PERSIST_MIN = 7.0
# How long the baseline looks back, and the minimum samples before it means anything.
BASELINE_MIN = 90.0
BASELINE_MIN_SAMPLES = 20
# After a setpoint change, this many minutes of silence — the power move is our own
# command arriving, already in the predictor.
QUIET_AFTER_CMD_MIN = 12.0


class PowerLead:
    """Turns the meter into a bounded, self-cancelling bias on the demand."""

    def __init__(self) -> None:
        self._hist: deque[tuple[datetime, float]] = deque(maxlen=512)
        self._last_cmd_at: datetime | None = None
        # When the current deviation first appeared, and its sign, so a duty-cycle
        # off-phase cannot be mistaken for the unit having stopped.
        self._dev_since: datetime | None = None
        self._dev_sign = 0

    def note_setpoint_change(self, at: datetime) -> None:
        self._last_cmd_at = at

    def observe(self, now: datetime, power: float | None, ac_on: bool) -> None:
        """Only samples taken while the compressor is RUNNING describe this unit."""
        if power is not None and ac_on and power >= RUNNING_W:
            self._hist.append((now, power))

    def baseline(self, now: datetime) -> float | None:
        """The median running draw over the recent past — the holding level.

        Median rather than mean: the point of this signal is the excursions, and an
        average that includes them is pulled toward the thing being measured against.
        """
        cut = now - timedelta(minutes=BASELINE_MIN)
        vals = [w for (t, w) in self._hist if t >= cut]
        return median(vals) if len(vals) >= BASELINE_MIN_SAMPLES else None

    def bias(self, now: datetime, power: float | None, ac_on: bool) -> tuple[float, dict]:
        """Bounded °C of setpoint to add to the demand, plus why.

        Positive means "cold is coming, ease off"; negative means "the unit has
        stopped working and the room will warm".
        """
        why = {"power": power, "baseline": None, "deviation": None,
               "quiet": False, "settling": False, "held_min": 0.0}
        base = self.baseline(now)
        why["baseline"] = base
        if power is None or not ac_on or base is None:
            return 0.0, why
        if self._last_cmd_at is not None and \
                (now - self._last_cmd_at).total_seconds() / 60.0 < QUIET_AFTER_CMD_MIN:
            why["quiet"] = True
            return 0.0, why
        dev = power - base
        why["deviation"] = dev
        sign = 0 if abs(dev) <= DEADBAND_W else (1 if dev > 0 else -1)
        if sign != self._dev_sign:
            self._dev_sign, self._dev_since = sign, now
        if sign == 0:
            return 0.0, why
        held = (now - self._dev_since).total_seconds() / 60.0
        why["held_min"] = held
        # A duty-cycle off-phase looks exactly like a unit that has stopped, for two
        # to five minutes. Only a deviation that outlasts that is evidence.
        if held < PERSIST_MIN:
            why["settling"] = True
            return 0.0, why
        frac = min(1.0, (abs(dev) - DEADBAND_W) / max(FULL_SCALE_W, 1e-6))
        # More power than usual → more cooling arriving → pre-emptively ease, so the
        # setpoint rises. Less than usual → cooling has stopped → let it fall.
        return (CAP * frac * sign), why
