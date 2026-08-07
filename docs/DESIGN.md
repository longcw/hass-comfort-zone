# Comfort Zone — design (v5)

One room, one VRF indoor unit, one BLE thermometer in a cot. This document is
written for someone who has to review or change the controller without having been
present for the decisions. It states what the design does, **what evidence each
choice rests on**, and — at least as important — what has already been tried and
failed, so it does not get tried again.

Read §9 before changing anything. Several of the obvious improvements have been
attempted and measured, and two of them caused incidents.

---

## 1. The problem, and the three facts that shape everything

**The job.** Hold a humidity-weighted room temperature (`comfort_temp`) on a
48-point daily target curve, in a baby's bedroom, using an AC setpoint, the AC's
blower, a circulation fan, and AC power. Optimise for **time inside a band** — a
cold miss counts the same as a warm one.

Three physical facts drive every decision below:

1. **The setpoint is a four-position actuator.** `target_temp_step` is 1 and the
   usable range is 24–27. At a plant gain near 0.5 °C of room per °C of setpoint,
   one click moves the room ~0.5 °C — comparable to the whole band. This is a
   resolution limit, not a tuning problem, and it is why v1–v4 all oscillated.
2. **The dead time is long.** ~10 min before a command reaches the sensor, ~8 min
   more to settle. Anything that feeds back the raw reading stacks commands into
   the lag.
3. **The power meter is whole-house.** Shared with every other room. This room's
   contribution is a minority of the signal.

**Setpoint is not room temperature.** This confusion has caused real errors. With
the fitted curve, at 30 °C outdoor:

| setpoint commanded | room it holds |
|---|---|
| 24 (the floor) | ~26.0 |
| 22 | ~25.0 |
| 18 | ~23.0 |

So a *safety rail* of 25.2 and a *setpoint floor* of 24 are not comparable numbers
even though they look like it. Any quantity in this codebase is either **room °C**
or **setpoint °C**, never both, and mixing them has produced two separate bugs
(§9.2, §9.5).

---

## 2. Architecture

One signal path. No branch ladder, because every fault of v1–v4 was an interaction
*between* branches rather than a bug inside one.

```
outdoor + forecast ──► reset.py ──► u_ff ─┐
power deviation    ──►                    ├──► u ──► split.py ──► setpoint (coarse)
                                          │                   ├──► blower  (fine)
zone ──►(+)──► error ──► pi.py ──► u_fb ──┘                   └──► fan     (finest)
settled ─►(−)
                                       ▼
                                   safety.py  ← can override anything, owns AC power
```

`u` is a **continuous virtual setpoint in °C** — what the unit would be given if it
accepted fractions. Everything upstream produces it; `split.py` spends it on the
three actuators that actually exist.

| module | job |
|---|---|
| `reset.py` | outdoor-reset feedforward + bounded forecast anticipation |
| `power.py` | bounded leading indicator from the meter |
| `pi.py` | PI on the dead-time-free error, back-calculation anti-windup |
| `split.py` | quantise `u` onto setpoint + blower + fan by mid-ranging |
| `controller.py` | plumbing; owns the comfort zone; **never touches AC power** |
| `safety.py` | absolute override; **owns AC power outright** |
| `model.py` | FOPDT predictor + the constants, with SIMC tuning derived |

---

## 3. The clues available, and how each is used

This is the part worth reviewing hardest. Every signal the system can see is listed,
including the ones deliberately not used.

| clue | source | how it is used | why |
|---|---|---|---|
| **comfort temp** | BLE crib sensor (or computed from temp+RH) | the regulated variable | the thing being controlled |
| **observed AC setpoint** | climate entity `temperature` | drives the Smith predictor | the room responds to what the unit is *running*, not what we asked for; this VRF's cloud proxy re-reports its own remembered value (7 of 30 transitions on 08-06) |
| **outdoor temperature** | `weather.forecast_home` | outdoor-reset feedforward, the dominant term | computes the setpoint the load calls for instead of waiting for error |
| **hourly forecast** | `weather.get_forecasts` | evaluated one plant horizon (θ+τ ≈ 18 min) ahead, bounded to ±0.5 °C | a *physical* prediction of the disturbance |
| **whole-house power** | shared meter | bounded feedforward on **deviation from a running baseline** (§4) | the meter knows minutes before the thermometer |
| **AC on/off** | climate state | freezes the integrator; gates the power baseline | the actuator is not ours while it is off |
| **blower level** | climate `fan_mode` | fine actuator, and in the delivered-demand currency | cheapest thermal lever |
| **fan state/level** | fan entity + speed number | finest actuator, takes the residual | makes leftover fractions tolerable |
| **sensor freshness** | `last_reported` age | stale → hold, then park after 30 min; **rails still apply** | acting on stale data is worse than not acting; ignoring the rails is worse still |
| **device `min_temp`** | climate attribute | clamps the guard's blast setpoint | the unit's own limit |
| **time of day** | 48-point target curve + night quiet caps | sets the *target* and the fan/blower caps | see below — it gets no separate feedforward term |
| **comfort slope** | derived | **display only** | derivative on a BLE sensor that can go quiet 10 min amplifies noise; measured worthless as an anticipation lead (§9.1) |
| humidity / dew point | same sensor | folded into `comfort_temp` already | not used again separately |
| cloud coverage, UV, wind | weather entity | **unused** | plausible but unmeasured; see §10 |
| other rooms' AC state | available in HA | **unused** | would disambiguate the shared meter — the single most promising unexploited clue, see §10 |

**Time of day gets no feedforward term, and that was measured.** Hour-of-day
explains 11.7% of what the outdoor term does not, and its hour-to-hour profile is
nearly as rough as noise (roughness 0.18, against 0.08 for a clean daily cycle and
0.29 for none). A rough profile is the old controller behaving differently at
different hours, not a load. Day/night already enters through the target curve and
the quiet caps.

---

## 4. How each stage works

### `reset.py` — the load's own setpoint

```
u_ff = -21.52 + (-0.2107 × T_outdoor) + (2.0 × target)
```

Fitted from the **old controller's own output**, not from the plant (§8). The
forecast term re-evaluates the curve at the outdoor temperature expected θ+τ ahead
and is capped at ±0.5 °C, so a wrong forecast costs little.

With no outdoor reading it holds the last curve value; with none ever, it sits at
the middle of the setpoint envelope. It must be a *setpoint* — an earlier fallback
computed `target / ff_per_target` and produced 13.1 °C, a 10 °C phantom shortfall
that pinned the fan (§9.3).

### `power.py` — the leading indicator

Deviation from a **median baseline of running samples** over 90 min, not an absolute
level, so another room's steady load cancels. Four guards make a shared meter usable:

- **deadband 600 W** — sized from measured separation: with no command the window
  trend already reaches ±195 W (p90), while real ramps measure +450…+650 W;
- **persistence 7 min** — longer than the unit's measured 2–5 min duty-cycle
  off-phase. Without this, "power near zero" reads as "the unit stopped, cool
  harder", which is positive feedback straight into cooling, every single cycle;
- **quiet for 12 min after a setpoint change** — that power move *is* our command
  arriving and the Smith predictor already has it. Reading it again is exactly how
  the old power logic cancelled its own steps;
- **cap ±0.4 °C** of setpoint. Small enough that it cannot move the integer setpoint
  on its own. It does **not** rely on the integral to correct it — inside the zone
  the error is zero by design and the integral is frozen there.

### `pi.py` — the feedback

PI, no derivative. The error is the **Smith-predictor error**, `target − settled`,
not `target − comfort`: feeding back where the room will settle removes the dead
time from the loop and lets a textbook tuning rule apply.

**Anti-windup by back-calculation**, against what the actuators can really deliver.
The output saturates at the setpoint limits for hours on a hot afternoon; most of the
v3/v4 overshoot looks in hindsight like an integrator nobody had written down and so
nobody had protected. Integration freezes whenever the actuator is not ours — guard
active, or AC off.

Tuning is SIMC, **derived from the plant, never stored**, so the constants cannot
drift out of step with the model. `Kc` 0.640, `Ti` 8.0 min. One knob, `tau_c_mult`
(1.5), deliberately not exposed in the UI — see §5.

### The comfort zone — where the loop aims

The interface is **target + band**, and a band means "this much drift is fine". So
the loop regulates to a zone, not a point:

```
centre = target + (band_high − band_low)/2      ← accuracy: an asymmetric band's
                                                  middle is not its target
half   = (band_low + band_high)/2 × 0.5         ← calm: drift that earns no reaction

inside the zone   → error = 0
outside the zone  → error = distance to the CENTRE, not to the edge just crossed
```

Three things about this, each learned the hard way:

- **Centre and width are separate questions.** Deriving the zone as
  `target ± band·frac` ties them together and under-shoots the centre in proportion
  to how lazy the loop is asked to be: measured 3.4 points of band fit worse.
- **Measuring error to the edge is dangerous.** The loop then leaves the zone with an
  error of ~0.02 °C and must wait for the room to travel before responding. On
  2026-08-07 that was fourteen minutes and a full degree below target (§9.2).
  Measuring to the centre also removes the classic dead-band limit cycle for free.
- **The zone is what makes the band a real knob.** Under point-tracking, widening a
  symmetric band changed only which excursions got *counted* — it did nothing to
  compressor motion, quietly contradicting the whole premise of "hold band
  accurately, then widen it for calm".

### `split.py` — spending `u`

All three actuators in one currency, so they can be compared rather than ranked:

```
u_delivered(setpoint, blower) = setpoint − blower × g
```

Splitting is **mid-ranging**: aim the setpoint so the blower sits mid-range with
headroom both ways. `g = 0` (its current value — not identifiable, §8) collapses this
to setpoint-plus-fan with no special case, and the blower then steps on the *sign* of
the residual, since the direction is known even when the magnitude is not.

**The setpoint threshold has a hard floor and a deliberate collapse.** Stepping by 1
moves `settled` by `K`, which the loop answers with `Kc·K` *against* the step. So a
step from distance `d` lands `(1−d) + Kc·K` from the new setpoint, and unless the
threshold clears `(1 + Kc·K)/2 = 0.66` the step gains nothing and the loop hunts —
measured live at 0.65: setpoint 24→25→24 in six minutes. But that argument only holds
while a step is comparable to the error. Once the room is more than one step's worth
of room temperature outside its zone, a full step is unambiguously right, so the
threshold collapses to a bare half-step. Holding the floor there cost three minutes
and 0.16 °C on the 08-07 trajectory.

Dwell clocks are stamped by the **caller, on commands actually issued** — not inside
`resolve`. Stamping on intent burned the compressor pacing on moves that never
happened during a guard override (§9.4).

The fine actuators are sized against `u_raw`, the demand **before** the clamp: at the
floor the clamped demand says "exactly what was asked for" every tick, while the
unclamped demand still says how far short the unit is falling.

---

## 5. What is deliberately not a user knob

The interface is **target, band low, band high** — three numbers, all room °C, all
meaning what they say.

`tau_c_mult` is the textbook knob for loop aggressiveness and is **not exposed**.
It is a control-theory constant nobody should have to hold an opinion about, and the
band expresses the same intent in the units the room is actually described in. When
the band was inert and `tau_c_mult` was offered instead, that was the wrong trade.

---

## 6. Safety — two layers, and the rules that are absolute

`safety.py` runs after the controller and can override it. It is deliberately dumb:
it reads the raw thermometer and thresholds, and shares none of the controller's
reasoning, because **the controller is the thing that might be wrong.**

Four rules that are not negotiable:

1. **The controller never powers the AC on.** Not conditionally, not when the room is
   hot, not ever. On 2026-08-07 it reversed a deliberate manual power-off within one
   tick, in a room already a degree below its zone (§9.1). There is no code path in
   `controller.py` that sets AC power.
2. **The controller never powers the AC off either** — it has no way to undo that, so
   it is not allowed to do it. This is why `managed_off` was deleted; the
   sustained-cold guard covers what it was for, and *can* restore power because it
   cut it.
3. **The configured rails are enforced verbatim.** `hard_min`/`hard_max` are the
   owner's numbers. A helper used to widen a rail that sat close to the band, on
   sound reasoning about chatter — and it enforced a configured 25.2 at 25.0, moving
   a safety limit 0.2 °C in the *dangerous* direction, silently. If a rail is placed
   where ripple will trip it, the log says so; nothing corrects it.
4. **The rails apply even when the reading is stale.** The stale branch used to return
   before the rail checks, so a dead thermometer battery — which most integrations
   report as a stale *value*, not `unavailable` — switched every protection off
   indefinitely. After 30 min of staleness it parks at `FAILSAFE_SETPOINT` rather
   than holding forever.

Guard behaviour:

- **overheat**: trips at `hard_max`, no grace period, blasts at `setpoint_min − 2`
  clamped to the device minimum, blower top, fan at the zone's guard level;
- **overcool**: trips at `hard_min`, **or** when the room sits `0.25 °C` below the
  band for `10 min` — a guard on *duration*, which catches the controller being
  wrong rather than the temperature being extreme. Cold side only: that is where a
  guard can act decisively and where the danger lies. Cuts power, and on release
  restores power **with a safe setpoint** — restoring power alone resumed the cold
  setpoint that caused the trip (§9.4);
- **release is decided by the reading, never a timer**, except one anti-short-cycle
  hold on the cold side where power was actually cut.

**Disabling the zone releases any override in force.** It used to return before the
guard and before `apply`, leaving the last command latched on the hardware — so
"disabled" meant "frozen mid-blast", not "safe".

---

## 7. Measured results

`tools/replay.py --windows`, six windows including a hot afternoon and a mild night,
against what actually ran on the same readings. At the owner's configuration
(band 0.4/0.7, rails 25.2/27.5):

| | recorded | new core |
|---|---|---|
| mean in band | 74.4% | **82.2%** |
| mean rms error | 0.455 | **0.400** |
| setpoint moves/h | 4.8 | **3.0** |
| worst cold excursion | 0.64 | **0.50** |

At band 0.5/0.8 the same core reaches ~91% in band. The band is the knob: wider is
calmer, and rms barely moves across the range, so the room is not drifting more — the
loop is simply no longer fussing.

The closed-loop arm of replay assumes the gain that could not be identified (§8), so
treat absolute numbers as indicative and the recorded column as ground truth. The
comparison survives the assumption: sweeping `K` over 0.3–0.7 moves the numbers but
not the ordering.

---

## 8. What the data can and cannot tell us

`tools/fit.py`, run offline and reviewed by a human. **It is explicit about which
constants it trusts, and a reviewer should not treat its output as uniform.**

**Not identifiable from closed-loop history: the plant itself.** The controller moves
the setpoint *because* the room moved, so a regression of room on setpoint recovers
the controller's inverse. Run that way this data returns a gain of **−0.04 °C/°C** —
wrong sign — and a blower coefficient saying more airflow makes the room *warmer*,
which is the old controller's rule, not physics. That sign is the tell. A dynamic
regressor (the materialising FOPDT response) reduces the bias but does not remove it:
the search then drives θ and τ to the bottom of the grid, because a short dead time
is what best lines up with a controller that answers the room within one tick.

So:

| constant | value | status |
|---|---|---|
| `K` (gain) | 0.5 | **prior**, from the old online adapter |
| `θ` (dead time) | 10 min | **prior** |
| `τ` | 8 min | **prior**, never fitted |
| `g` (blower authority) | 0.0 | **not identified** |
| reset curve | −21.52, −0.2107, 2.0 | **identified** (outdoor term); target term pinned at 1/K by physics |

**`K` is used in two places that want opposite errors, and only one was reasoned
about.** It divides into `Kc` (guessing high → gentler loop) and it *multiplies* in
`remaining_effect` (guessing high → aggressive, in the cold direction). Guessing high
is safe for the first and unsafe for the second. This is mitigated, not solved: the
reading now vetoes any colder setpoint while the room is measurably below its zone.

**Getting `K`, `θ`, `τ` properly needs an open-loop step test** — controller disabled,
setpoint driven deliberately for an hour or two, in an unoccupied room. Nothing in the
design depends on having it, but it is the only way to replace these three priors with
measurements, and it would let §9.6 be closed properly.

---

## 9. Tried and failed — do not repeat

Each of these was implemented and measured. Dates are 2026.

**9.1 Power as an engagement detector** — asking "did my command take?" and answering
with a binary veto. In the 2.2 h after the v4 deploy all four escalations it triggered
were wrong; one cooled the room 1.7 °C into a guard trip. One threshold cannot serve
both "engaged" and "unresponsive", so fixing a false positive mechanically created a
false negative. **Power as a bounded feedforward (§4) is a different question and is
fine; power as a veto is not.**

**Anticipation from the room's own slope** — swept over 24 h: band fit flat from lead
0 to 5, while setpoint moves scaled 2.3 → 3.7/h and the worst cold excursion tripled.
It cannot distinguish a disturbance from the controller's own unanswered command.

**Online learning** (`adapt.py`) — not required, never observed to help, and its one
measurable effect was a positive feedback loop: a broken engagement rule ratcheted the
learned dead time to 19 min, which set an 11.5-min dwell, which made the next episode
worse.

**9.2 The 08-07 overcool incident.** Room fell 25.85 → 25.04 over fourteen minutes,
with no action and no log rows, then the controller powered the AC back on over a
manual off. Four causes: `ac.on` was unconditional; the setpoint threshold did not
relax when the room left the zone; the zone measured error to its *edge*, so the loop
left it nearly blind; and logging fired only on action or branch change, so a slow
drift was invisible. All fixed. A drift heartbeat now logs every 2 min whenever the
room is outside its zone.

**9.3 A feedforward fallback that was not a setpoint** — `target / ff_per_target`
produced 13.1 °C, a 10 °C phantom shortfall that pinned the fan at maximum.

**9.4 Silent overrides of the owner** — `effective_rails` enforcing 25.0 for a
configured 25.2; the overcool release restoring power without a setpoint; the dwell
clock burned on commands never issued; disabling the zone latching the last override.

**9.5 Two dimensional errors** — the failsafe parked `floor(target − band_low)`, a
*room* temperature, into a *setpoint* field; and a diagnosis was built on `hard_min =
23.0` read from `const.py` defaults rather than from the running configuration, which
was 25.2.

**9.6 An anti-hunt fix that overshot** — raising the setpoint threshold to 0.81 to
kill a 24→25→24 limit cycle made the loop three minutes slower to answer a falling
room. The floor is real; applying it when the room is far out of zone is not.

---

## 10. Open questions for a reviewer

Ranked by how much they would improve things.

1. **Disambiguate the shared power meter using the other rooms' AC state.** HA knows
   whether the other indoor units are running. Subtracting their contribution would
   turn a noisy whole-house signal into something close to this room's own draw, and
   would let `power.py` act sooner and harder than its current ±0.4 °C cap allows.
   This is the largest unexploited clue in the system.
2. **Run an open-loop step test** and replace the three priors in §8 with
   measurements. Everything downstream of `K` is currently reasoned about rather than
   known, and `K` has two conflicting uses.
3. **Is the blower worth anything thermally?** `g = 0` today. A step test would
   answer it, and a non-zero `g` would make the split-range design do what it was
   designed for instead of degrading to setpoint-plus-fan.
4. **Should the setpoint envelope widen below 24?** At 33 °C outdoor the fitted curve
   asks for 23.5 — below the floor — so the loop saturates and the fine actuators
   carry everything. This is a comfort decision for an infant's room and belongs to
   the owner, not the model.
5. **The fan is commanded from the quantisation residual**, which is a rounding
   artefact, not a statement about where the room is. It can therefore run while the
   room is cold. Worth restructuring so the fan reads the room directly.
6. **The zone's blindness is still a trade.** At `INNER_ZONE_FRAC = 0.5` the loop sees
   no error across 0.325 °C of drift. Centre-measured error mostly compensates, but a
   reviewer should check the cold side specifically — the worst cold excursion is the
   metric that matters, not mean band fit. Choosing 0.5 on band fit alone is what
   produced the worst cold excursion of every value tested.
7. **`hard_min`/`hard_max` are room °C sitting numerically next to setpoint °C.**
   Deriving the rails from the band would remove two numbers that can be misread —
   at the cost of removing the owner's direct control over them.

---

## 11. Validating a change

```bash
python tests/test_controller.py     # 61 scenario tests, no HA needed
python tests/test_options.py        # per-mode knob resolution
python tools/fit.py --days 7        # re-fit, and read what it says it cannot fit
python tools/replay.py --windows --summary --band-low 0.4 --band-high 0.7 \
       --hard-min 25.2 --hard-max 27.5
```

`tools/` needs `HA_URL` and `HA_TOKEN` (or `COMFORT_ZONE_ENV`); the entity ids are
constants at the top of each script.

**Replay's two arms answer different-strength questions.** Decision replay is exact —
it feeds recorded readings in and records what would have been decided, with no plant
assumption. Closed-loop simulation reconstructs the load and drives the new controller
against it, which assumes linear superposition and the un-identified gain. Judge a
change on the recorded baseline and on the **worst cold excursion**, not on mean band
fit alone.

Two harness traps worth not reintroducing: the fan must be *simulated* (pinning it on
leaves the loop reading far calmer and colder than it is), and the recorded baseline
must use the same band definition as the simulated arm.

A backend change needs a **full HA restart** — a config-entry reload does not
re-import changed modules. The card is a resource-version bump.
