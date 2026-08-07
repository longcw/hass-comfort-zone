# Signals — every clue, where it enters, and what bounds it

Companion to `docs/DESIGN.md`. That document explains the design; this one is the
reference for **what the controller can see and what it is allowed to do with it.**

The organising rule: a signal is either a **measurement of the regulated variable**
(one of these, and it is authoritative), a **prediction of a disturbance** (several,
and each is bounded), or **telemetry** (no control authority at all). Every past
failure in this project came from a signal being promoted out of its class — most
often a prediction being allowed to act like a measurement, or telemetry being given
a veto.

---

## 1. The regulated variable

| | |
|---|---|
| **signal** | `comfort_temp` — humidity-weighted room temperature |
| **source** | BLE crib thermometer, or computed from a temp + RH pair |
| **enters at** | `controller.tick` → `zone_error` → `pi.step` |
| **authority** | **absolute.** It is the only thing that decides whether the room is comfortable, and the only thing the safety guard reads. |
| **bounds** | none applied — see the gap noted at the end |
| **failure mode** | can go quiet ~10 min (BLE). Handled as *staleness*, not as a value: hold, then park after 30 min. Rails keep applying. |

**It also holds a veto.** If the reading says the room is below its zone, no colder
setpoint may be commanded, whatever the model believes is on the way. The model can be
wrong about the future; the thermometer cannot be wrong about the present.

---

## 2. Predictions of disturbance — bounded, and allowed to be wrong

Each of these makes the loop act *before* the room moves. Each is capped, so being
wrong costs authority rather than safety.

### Outdoor temperature — the dominant term

| | |
|---|---|
| **source** | `weather.forecast_home`, `temperature` attribute |
| **enters at** | `reset.curve` → `u_ff` |
| **form** | `u_ff = −21.52 + (−0.2107 × T_out) + (2.0 × target)` |
| **bounds** | coefficients clamped on load; no cap on the term itself |
| **if missing** | holds the last curve value; if never seen, the middle of the setpoint envelope |

The one term fitted from real data (§8 of DESIGN). ~−0.21 °C of setpoint per outdoor
°C, so a 10 °C hotter day asks for ~2 °C colder setpoint.

### Hourly forecast — anticipation

| | |
|---|---|
| **source** | `weather.get_forecasts`, cached 20 min |
| **enters at** | `reset.feedforward` — the curve re-evaluated at `now + θ + τ` (~18 min) |
| **bounds** | **±0.5 °C** (`FORECAST_CAP`) |

This is the *only* anticipation in the system. Extrapolating the room's own slope was
tried and measured worthless (DESIGN §9.1): it cannot tell a disturbance from the
controller's own unanswered command. A forecast can, because it is about the weather
rather than about us.

### Whole-house power — the leading indicator

| | |
|---|---|
| **source** | shared house meter |
| **enters at** | `power.bias` → added to `u_ff` |
| **form** | deviation from a 90-min **median of running samples** |
| **bounds** | **±0.4 °C** (`CAP`), and four gates below |

| gate | value | why |
|---|---|---|
| running floor | 200 W | samples below this describe the rest of the house, not this unit |
| deadband | 600 W | measured separation: idle trend reaches ±195 W p90, real ramps +450…+650 W |
| persistence | 7 min | longer than the measured 2–5 min duty-cycle off-phase |
| quiet after command | 12 min | that power move *is* our command; the Smith predictor already has it |

**The persistence gate is what makes the low side safe at all.** Power near zero is the
*normal* state of a unit that has reached temperature. Reading it as "cool harder" is
positive feedback into cooling, once per duty cycle.

**This does not rely on the integral to correct it.** Inside the comfort zone the error
is exactly zero by design, so the integral is frozen there and a standing bias would
sit uncorrected. The cap is small enough that the bias cannot move the integer setpoint
on its own — that, not downstream correction, is the safety argument.

### In-flight setpoint effect — the Smith predictor

| | |
|---|---|
| **source** | **observed** setpoint transitions, not our commands |
| **enters at** | `model.remaining_effect` → `predict_settled` → the error |
| **bounds** | steps clamped to ±5 °C before the predictor sees them |

Driven from what the unit reports because the room responds to what the unit is
actually running at — and because this VRF's cloud proxy re-reports its own remembered
setpoint (7 of 30 transitions on 08-06 were never commanded). The clamp exists because
a glitched report of 5 or 50 would otherwise carry ±12 °C of imaginary cooling for
fifty minutes.

---

## 3. Configuration — what the owner sets

| signal | units | role |
|---|---|---|
| **target** (48-point curve) | room °C | what to hold, per half-hour |
| **band low / band high** | room °C | tolerance; sets the zone (control), the fit metric, and the aim's asymmetry |
| **hard_min / hard_max** | **room °C** | safety rails. Enforced **verbatim** |
| **setpoint_min / max** | **setpoint °C** | the envelope the controller may command |
| **fan_max day / night** | % | quiet caps; **0 means no fan**, which shifts the zone down by `no_fan_offset` |
| **blower_max day / night** | ladder index | quiet caps |
| **no_fan_offset** | room °C | how much cooler to aim when no fan can run |

**`hard_min` and `setpoint_min` are not comparable numbers** even though both read as
"about 24–25". One is a room temperature, the other a dial position, and setpoint 24
holds the room near 26. Mixing them has caused two bugs.

---

## 4. Telemetry — no control authority

| signal | why it is not used for control |
|---|---|
| **comfort slope** | derivative on a sensor that goes quiet 10 min amplifies noise; measured worthless as a lead |
| **live power reading** | the control path uses *deviation from baseline*, not the level; this is the card's chip and arrow |
| **`power_recent`** | 3-min display window, deliberately different from the control baseline |
| **`sp_held_min`** | how long the unit has held its setpoint — the "is it fussing?" number |

---

## 5. Unused clues a reviewer might consider

Ranked by expected value.

1. **Other rooms' AC state.** HA knows whether the other indoor units are running.
   Subtracting them would turn the whole-house meter into something close to this
   room's own draw, and let `power.py` act sooner and harder than its ±0.4 °C cap
   permits. **The largest unexploited signal in the system.**
2. **The AC's own return-air temperature**, if the integration exposes it. A second,
   faster thermal measurement would shorten the effective dead time.
3. **Cloud coverage / UV**, on the same weather entity. Solar gain is a real load on a
   room with windows and is currently invisible; outdoor temperature is a lagging
   proxy for it.
4. **Door or occupancy sensors.** A door opening is a step disturbance the loop
   currently discovers only through the thermometer, ten minutes late.
5. **Dew point directly.** Already folded into `comfort_temp`, but the humidity
   weighting (`comfort_k`) is a fixed guess, not a fitted number.

---

## 6. Known gaps in the current signal handling

Stated plainly so a reviewer does not have to find them.

- **The comfort reading is not bounds-checked, filtered, or rate-limited.** A single
  spurious sample above `hard_max` triggers a full cooling blast; one below `hard_min`
  cuts power for at least 3 minutes. A median-of-3 or a physical rate check would cost
  nothing and close this.
- **The weather `temperature` attribute is unbounded** on the way in. Its coefficients
  are clamped, so a garbage value is attenuated but not rejected — and the feedforward
  has no cap of its own, while the integral that would argue back is limited to ±5 °C.
- **`device_min_temp` is trusted as reported** and only ever raises the guard's blast
  setpoint, never lowers it. A bogus high value makes an overheat blast *warmer*.
- **The fan is commanded from the quantisation residual**, a rounding artefact rather
  than a statement about the room, so it can run while the room is cold.
- **Compressor dwell is not persisted** across a restart, so a setpoint can move
  seconds after the previous process moved it.
- **The overheat rail is unreachable during an overcool hold** (the release check
  returns first). Bounded to ~3 minutes in practice, structurally wrong.
