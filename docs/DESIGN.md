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

**On the cold side the error may never be smaller than the reading says.** `settled`
credits every in-flight step with a full `gain_per_step`, and that prior is
deliberately high — safe where it divides into `Kc`, aggressive where it multiplies
here (§8). Measured live 08-07 16:10: a 24→25 step drew the same 1 kW before and
after, so nothing was on the way at all, yet the model had already shaved 0.24 °C off
the error and the loop sat at 25 with the room 0.55 °C below its zone and still
falling. So a prediction may shrink the error that answers a **warm** room — feeding
back a reading that will not move for a dead time is how v1–v4 stacked commands into
the lag — but it may never talk the loop out of warming a room the thermometer says
is cold. Same rule as the veto below, one stage earlier, and cold side only.

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

**The setpoint threshold has a hard floor inside the zone and none outside it.**
Stepping by 1 moves `settled` by `K`, which the loop answers with `Kc·K` *against* the
step. So a step from distance `d` lands `(1−d) + Kc·K` from the new setpoint, and
unless the threshold clears `(1 + Kc·K)/2 = 0.66` the step gains nothing and the loop
hunts — measured live at 0.65: setpoint 24→25→24 in six minutes. That answer only
exists while the loop still has an error to feel, and a step that returns the room to
its zone produces exactly **zero** error. So outside the zone the argument lapses
entirely and the threshold is the bare half-step, at once — below half a step the
nearest integer is by definition already the right one, so there is nothing left to
argue about. Ramping it was measured worse than either endpoint (§9.6).

The **compressor dwell starts on the unit's own setpoint transition**, never on our
write. This VRF's cloud proxy acknowledges `set_temperature` and then keeps reporting
its remembered value; stamped on the write, a lost write spent the full six minutes
pacing a compressor that had not moved (§9.7). Absolute output is what makes a lost
write self-correcting — "write it if it differs from what the unit reports" — but only
if the dwell agrees about what *moved* means. The blower's dwell does stamp on the
write: no proxy in the way, no compressor behind it.

The fine actuators are sized against `u_raw`, the demand **before** the clamp — at the
floor the clamped demand says "exactly what was asked for" every tick, while the
unclamped demand still says how far short the unit is falling — and against the
setpoint the unit is **running**, not the one just chosen. Read off the chosen
setpoint the residual flipped sign in the same tick as any step that crossed the
demand, so one tick asked for a warmer setpoint and more airflow together (§9.7).

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
5. **An override in force is released on the way into a hold, never held.** "Hold"
   actuates nothing, so whatever the guard last wrote stayed latched on hardware that
   nothing could then revise: an overheat blast kept blasting, and an overcool trip
   left the AC **off indefinitely** — a dead thermometer battery after a cold trip,
   which is this system's commonest hardware failure meeting its most dangerous
   state. Both sensor failures (stale, and gone entirely) now give back the power
   this guard cut and park at a safe setpoint. The rails above still have their say
   first, so a stale reading past a rail keeps the guard.
6. **Restoring is a state, not a command.** The guard asserts power, the running mode
   and a safe setpoint, and keeps asserting them every tick until the unit reports
   itself running — only then is it released. One attempt is not a restore on this
   proxy (§9.8), and because the controller may never switch an AC on, a guard that
   hands back to a unit that never started leaves **nothing at all** regulating the
   room. It gives up after `RESTORE_TIMEOUT_MIN` so it cannot argue indefinitely with
   somebody switching the unit off by hand, and logs that at ERROR when it does.

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

| | recorded | v5 as first shipped | v5 + §9.7 fixes |
|---|---|---|---|
| mean in band | 74.4% | 82.6% | **85.6%** |
| mean rms error | 0.455 | 0.402 | **0.396** |
| setpoint moves/h | 4.8 | 3.0 | **2.5** |
| worst cold excursion | 0.64 | 0.50 | **0.36** |

Five of the six windows improved on band fit; the sixth (the post-v4 window, the worst
recorded at 49.2%) fell 68.5% → 63.5% with its worst cold excursion unchanged.

At band 0.5/0.8 the same core reaches ~91% in band. The band is the knob: wider is
calmer, and rms barely moves across the range, so the room is not drifting more — the
loop is simply no longer fussing.

**Both columns are measured with the compressor dwell paced.** Neither replay arm used
to start that clock, so every moves/h the harness ever printed was one an unpaced loop
could reach and the hardware could not. It happened not to matter for the code
measured first, whose threshold rarely wanted a move inside six minutes anyway — which
is exactly why the gap stayed hidden until the threshold collapsed. Unpaced, the same
fixes below score 10.0 moves/h and read as a disaster. Judge nothing on this harness
without checking that clock is running.

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
is safe for the first and unsafe for the second. This is mitigated, not solved, in two
places — both cold side only, both deferring to the thermometer over the model: the
error may not be smaller than the reading says (§4), and no colder setpoint may be
commanded while the room reads below its zone. Neither removes the need for §10.2.

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

**9.6 An anti-hunt fix that overshot, twice.** Raising the setpoint threshold to 0.81
to kill a 24→25→24 limit cycle made the loop three minutes slower to answer a falling
room. The floor is real; applying it when the room is out of zone is not. The first
remedy — collapse the threshold *toward* the floor over 0.3 °C of urgency, and only
reach the bare half-step once the room is a full step's worth out — kept the worst of
both. Live 08-07 07:49 that gave a room 0.115 °C out 0.06 °C of relaxation, and it
waited eight and a half minutes and 0.30 °C of further fall for one step. The
threshold reaches 0.5 the moment the room is out, or the ramp is doing the deadband's
job a second time.

**9.7 The 08-07 afternoon stall — four defects, all of them "measure it against what
the unit is actually doing".** The room sat 0.42 °C below its zone and below its band
floor, reported as `idle — settles 25.96, inside [25.88, 26.42]`, holding a setpoint of
25 while the meter showed the unit drawing the same 1 kW it had before the step:

- a setpoint write of 25 was **lost** — the proxy kept reporting 24 for four ticks —
  and the dwell, stamped on the write, blocked the re-assert for six minutes. Absolute
  output was supposed to make a lost write self-correcting; the dwell has to agree;
- the zone deadband was tested on `settled`, so a prediction satisfied it while the
  thermometer did not. `mode`, `branch`, `reason` and `in_zone` all reported the
  prediction's opinion of the room as the room;
- the fine actuators' residual was measured against the setpoint just *chosen*, so it
  flipped sign the instant a step crossed the demand — one tick commanded a warmer
  setpoint and more airflow together, and the blower round-tripped 低→中→低 at the
  dwell period all day;
- the cold veto was applied to the PI's answer *after* it had integrated, so
  back-calculation unwound nothing and the loop wound up against a limit it could not
  see. Every other limit in this design is enforced where anti-windup can see it.

**9.8 The 10:48 restore that never landed, and a wrong first diagnosis.** The overcool
guard cut power at 10:35, released at 10:48 with `switch.turn_on` + `set_temperature 27`
— and the unit reported **off** one second later, with the setpoint applied, and stayed
off for seven minutes until a person called `climate.set_hvac_mode` by hand. The
controller correctly refused to touch it the whole time (rule 1), and the guard had
already released, so nothing was left that was allowed to finish.

The first diagnosis was "the switch restores mains and leaves this VRF in standby, so
the restore must set the mode". **That was wrong, and a live test disproved it**: cut
with `switch.turn_off`, held two minutes, then `switch.turn_on` alone brought the unit
to `cool` in five seconds. What differed on 10:48 is that the power-on and the setpoint
write went out back-to-back in one `apply()` — the same proxy that drops a setpoint
write (§9.7) mis-applied one issued into a unit that was still coming up.

So the fix is **verification, not the mode**: a restore is re-asserted until the unit
agrees (§6.5). The mode is set too, because it is cheap, it provably works, and it is
the only remedy if the unit ever really does come back in standby — but it is not what
makes the restore work, and a future reader should not believe it is. Worth keeping in
mind when reading the rest of this document: two consecutive failures on this hardware
had "one write, unverified" as their root cause, and neither looked like it at first.

---

**9.9 Inferring the load bias from the setpoint in force — measured worse, twice.**
The integral carries the whole difference between the reset curve and this room's real
load, 0.5–0.8 °C of setpoint. Inside the zone the error is exactly zero by design, so
that bias cannot converge there; and a restart threw it away entirely, which on 08-07
left the loop demanding 24.32 while the room sat comfortably at 25.81 on a setpoint of
25. It then spent eleven minutes climbing back to parity with a setpoint it was already
running, and the room reached the cold rail first.

The obvious remedy is to read the bias off the setpoint that is demonstrably holding
the room — "comfortable at 25" is a cleaner measurement of the load than a curve fitted
from another controller's output. **Both forms of it measured worse:**

| | in band | rms | moves/h | worst cold |
|---|---|---|---|---|
| neither | **85.6%** | **0.396** | 2.5 | **0.36** |
| prime the integral from the observed setpoint at startup | 83.0% | 0.434 | 2.7 | 0.98 |
| …and track it continuously while in zone (τ 180 min) | 82.7% | 0.436 | 2.6 | 0.98 |
| …τ 60 min | 82.2% | 0.441 | 2.6 | 0.98 |
| …τ 20 min | 82.0% | 0.443 | 2.5 | 0.98 |

Monotonic in the wrong direction, and the worst cold excursion nearly triples. Both
assume the setpoint in force is the right one, and that is **circular**: inside the
zone the demand sits anywhere within ±0.5 of the integer the loop itself rounded to, so
pulling the demand toward that integer makes a cold rounding self-confirming. Priming
fails the same way for a different reason — at startup the setpoint in force is whatever
was there before, including whatever error put it there.

What is left is the part that was never circular: **persist the integral** across a
restart, aged out after two hours. It is the loop's own value, earned from real error,
and nothing else in the loop needs to survive a restart. It does not appear in the table
above because replay never restarts — which is exactly why the gap survived this long.

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
4. **The reset curve does not explain this room's load, and 08-07 says so twice.**
   At local noon, outdoor 30.5, the loop was pinned at setpoint 24 with 中风 and the
   fan at 30 while the room ran 26.66 → 27.55 into the hot rail over 34 minutes; the
   guard's blast to 22 recovered it, so the load genuinely wanted ~22. Four hours
   later, outdoor **32.2**, the same room was overcooling at setpoint 25. `u_ff` read
   24.09 and 24.25 across those two states. A −0.21 °C/outdoor-°C line cannot produce
   a 3 °C swing that runs *opposite* to outdoor temperature, so the residual is
   larger than the term itself — and the "dominant term" is mostly not the dominant
   term. Candidates: solar gain through the window (invisible today, §5.3 of SIGNALS),
   or shared-condenser capacity, since a VRF's indoor unit gets less when the other
   rooms are demanding. Re-run `fit.py` now that a runaway is in the history; its
   hour-of-day verdict (§3) is the one measured conclusion this data argues against.
5. **The setpoint envelope 24–27 is too narrow at BOTH ends, and 08-07 showed both.**
   At noon the room reached the hot rail with the floor exhausted and the guard's 22
   fixed it. That same evening the mirror image: room 25.47 and falling, 0.19 below its
   zone, with the loop's demand at 25.89 — *below* the 26 already running — and the
   cold veto the only thing stopping it going colder still. It was `pi.at_ceiling` at
   27 earlier in the same hour. **At setpoint 27, its warmest allowed value, this unit
   still overcools this room.** The climate entity's own return air reads 22–23 °C
   against a room of 25.5, which is what a unit delivering far more than the load looks
   like.

   That has a consequence worth stating plainly: when the envelope's ceiling is still
   colder than the load wants, **no setpoint-based controller can hold the cold side**,
   and the only remaining lever is to stop the unit — which rule 6.2 forbids the
   controller from touching. So the overcool guard cutting power is not the guard
   failing; it is the system running out of actuator and falling back to the one lever
   left. Raising `setpoint_max` toward the device's 32 is the cheap experiment, and it
   can only ever reduce cooling. Both ends are comfort decisions for an infant's room
   and belong to the owner, not the model, which is why nothing here has changed them.
6. **The zone's blindness is still a trade.** At `INNER_ZONE_FRAC = 0.5` the loop sees
   no error across 0.325 °C of drift. Centre-measured error mostly compensates, and on
   the cold side the reading now floors the error outright (§4), but a reviewer should
   still check the cold side specifically — the worst cold excursion is the metric that
   matters, not mean band fit. Choosing 0.5 on band fit alone is what produced the
   worst cold excursion of every value tested.
7. **`hard_min`/`hard_max` are room °C sitting numerically next to setpoint °C.**
   Deriving the rails from the band would remove two numbers that can be misread —
   at the cost of removing the owner's direct control over them.
8. **Power's sign is right for this unit and possibly wrong for this meter.** More
   draw than baseline reads as "more cooling arriving, ease off". But the persistence
   gate deliberately filters out *this* unit's duty cycle, which preferentially admits
   another room starting — and on a shared condenser another room starting means less
   capacity here, i.e. the opposite. Bounded at ±0.4 °C so it cannot do much either
   way. Unchanged, because flipping a bounded feedforward on a hypothesis is how this
   project got into trouble before; settle it with the other rooms' AC state (#1).

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

Three harness traps worth not reintroducing: the fan must be *simulated* (pinning it on
leaves the loop reading far calmer and colder than it is); the recorded baseline must
use the same band definition as the simulated arm; and **both arms must feed the
observed setpoint transition back** with `Controller.note_setpoint_change`, or the
compressor dwell never starts and every moves/h figure is fiction (§7).

A backend change needs a **full HA restart** — a config-entry reload does not
re-import changed modules. The card is a resource-version bump.
