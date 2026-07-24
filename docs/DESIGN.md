# Comfort Zone — design

**Status:** approved 2026-07-24 · v1 in progress
**Goal:** hold a room's `comfort_temp` on a per-hour target curve while
minimizing AC on/off churn, by orchestrating *all* the room's devices
(AC setpoint, AC blower, circulation fan, AC power) proactively instead of
reactively — replacing a pile of Home Assistant automations that flip the AC
~164×/day.

## Why this exists (measured, from 7 days of the master bedroom)

- **Over-actuation, poor regulation.** 164 setpoint changes/day (median dwell
  7 min) yet `comfort_temp` still has sd 0.35 °C and swings 24.75→27.96.
- **Root cause: the controller acts faster than the room responds.** Setpoint
  step-response: no meaningful movement until ~15 min (Δ −0.25 °C by +15 min),
  but commands are issued every ~7 min → they stack into the dead-time and
  overshoot. Classic dead-time-blind control.
- **Whole-system AC power is a real leading signal.** corr(Δpower[t],
  comfort_slope[t+lag]) peaks at **−0.44 at lag +6 min** — a power rise predicts
  cooling ~6 min ahead. Strong even though the meter is shared across rooms.
- **Engagement is noisy → power is a *soft* signal.** After a setpoint-down,
  whole-system power rises within 5 min only 67% of the time. So power biases
  timing/decisions; `comfort_temp` slope is the ground-truth check.
- **The system is cleanly identifiable** (first-order-plus-dead-time). So a
  *small, interpretable* fitted model belongs in v1; heavy ML is not justified.

## Decisions

1. **v1 = deterministic fitted controller + custom UI.** A small offline
   system-ID (fit dead-time, time-constant, gain-per-step, power-lead, comfort
   params from recorder history) sets the controller's constants. Heavy
   ML/RL deferred; every decision + signal is logged to build that dataset.
2. **General multi-zone engine; master bedroom configured first.** One config
   entry per zone binds {regulated signal, AC climate + power switch, AC power
   sensor, circulation fan + speed number, strategy, safety limits}.
3. **`comfort_temp` is the single regulated target** (`T + k·humidity`,
   anchored). The **fan is a parallel comfort actuator**, NOT folded into the
   regulated signal. No invented "perceived temperature."
4. **Power = soft leading/feedforward signal; slope = ground truth.**
5. **Control core = dead-time-aware supervisor** (cost-ordered actuator ladder)
   with a **predictor seam** so MPC can drop in later.
6. **Rollout = direct cutover** with an always-on hard safety layer. Old HA
   automations disabled (not deleted) for rollback.
7. **Custom entities only** (sensors / select / number / switch) + a custom
   Lovelace card. No virtual `climate` entity.
8. **Dev loop:** proper HACS integration structure, but installed directly into
   the VM's HA `custom_components/` for iteration; HACS/GitHub publish later.

## Control engine

Each tick (~30–60 s) the supervisor reads `y`=comfort_temp, `slope`,
`power`/`Δpower`, actuator states, and the current scheduled `target`, computes a
short-horizon prediction `ŷ`, and picks at most one action from a cost-ordered
ladder. Anti-churn is achieved **not by a fixed freeze** but by:

- **Engagement-gated escalation.** After a setpoint-down: watch power+slope for
  ~2–5 min. If *not engaged* (power flat AND slope hasn't turned) → the command
  didn't take → escalate the setpoint immediately (25→24→23), bounded by
  `setpoint_min`. If *engaged* → hand to the predictor.
- **Smith-predictor patience.** Once engaged, the FOPDT model knows a step is
  in flight and predicts the *settled* `comfort_temp` including cooling already
  on the way. Another AC step is allowed only if the prediction still lands
  out-of-band after this one plays out. "Don't command cooling you already have
  coming." A short min-dwell (~engagement window) floors tick-to-tick thrash; the
  model response window is an upper backstop.

**Cost-ordered ladder**

- *Warm* (ŷ/y above target+band): ① raise/enable circulation fan (cheap, instant)
  → ② if sustained & AC unlocked, step setpoint down + engagement check
  → ③ raise AC blower.
- *Cold* (ŷ/y below target−band): ① fan down→off → ② setpoint up + blower down
  (低风, quiet) → ③ still overcooling → **managed AC-off** (record we did it) with
  a **guaranteed auto-return** watchdog.
- *Feedforward:* power spiking → suppress further AC-down + pre-trim fan; power
  collapsing + slope confirms → pre-raise fan ahead of the sensor.

**Predictor seam:** `predictor.predict(horizon)`; v1 = FOPDT + power-feedforward.

## Comfort signal & fan

`comfort_temp` computed internally from bound T+RH (`T + k·0.33·(e−e_ref)`,
anchored at `rh_ref` so temp-tuned bands carry over), or bound to an external
precomputed sensor. Fan driven by warmth + feedforward, bounded by day/night
caps (baby), via the non-lossy `speed_level` number.

## Schedule

48-point (30-min) daily target curve per zone; active target = interpolated at
`now()`. Manual override until the next curve point.

## Strategies

Named presets bundling tunables + emphasis: **Baby/Quiet**, **Eco**, **Comfort**,
**Custom**. Selectable per zone.

## Safety layer (always-on, independent of the optimizer)

Absolute hard `comfort_temp` min/max clamps (wide margin + cooldown, lessons from
the prior 安全阈值 oscillation baked in); managed AC-off always has a watchdog
return (max-duration or comfort-rise → power back on via the **reliable power
switch**, never HVAC-mode); stale/unavailable sensor → stop actuating and revert
AC to a safe fixed setpoint.

## Entities per zone (custom, no climate entity)

- `sensor.<zone>_comfort_temp` — regulated signal (if computed internally)
- `sensor.<zone>_target` — current scheduled/override target
- `sensor.<zone>_predicted` — ŷ short-horizon prediction
- `sensor.<zone>_decision` — last action + human-readable reason
- `select.<zone>_strategy`
- `switch.<zone>_enable`
- `number.<zone>_*` — safety + tuning knobs (also editable in the card)

## Learning — system-ID (v1)

Re-runnable service `comfort_zone.identify_model`: pulls recent recorder history,
fits FOPDT + power-lead + comfort params, writes constants into the zone's model
store. Decision log persisted for a future heavier model.

## Repo layout

```
custom_components/comfort_zone/
  __init__.py      setup, coordinator wiring, services
  const.py         config keys, defaults, domain
  config_flow.py   per-zone binding (config + options flow)
  coordinator.py   per-zone control loop
  comfort.py       comfort_temp computation
  model.py         FOPDT predictor + power feedforward + system-ID fit
  controller.py    supervisor state machine + cost-ordered ladder
  actuators.py     AC (setpoint/blower/power-switch) + fan abstraction
  safety.py        always-on hard guard
  store.py         decision log + fitted-model persistence
  sensor.py select.py number.py switch.py   entity platforms
frontend/          Lit/TS custom card (built to www/)
```
