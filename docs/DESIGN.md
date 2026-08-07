# Comfort Zone — design (v5)

Supersedes the v1–v4 ladder design. The proposal this was built from is
`docs/REDESIGN.md`; the measured outcome is at the end of this file.

## The problem, stated honestly

One room, one VRF indoor unit, one BLE thermometer in a cot. Three facts shape
everything else:

1. **The setpoint is a four-position actuator.** `target_temp_step` is 1 and the
   usable range is 24–27. At a plant gain near 0.5 °C of comfort per °C of setpoint,
   one click moves the room ~0.5 °C — more than half of a ±0.4 band. This is a
   resolution limit, not a tuning failure, and it is why every earlier version
   oscillated.
2. **The dead time is long.** A command takes ~10 min to reach the sensor and
   another ~8 min to settle. Anything that feeds back the raw reading will stack
   commands into that lag.
3. **The power meter is whole-house.** It is shared with every other room, so this
   room's step is a minority of the signal. Every version that gated a decision on
   it was wrong (§ "What was removed").

## Shape

One signal path. No branches that can trade places.

```
outdoor + forecast ──► reset.py ──► u_ff  ─┐
                                           ├──► u ──► split.py ──► setpoint (coarse)
target ──►(+)──► error ──► pi.py  ──► u_fb ─┘                  ├─► blower  (fine)
settled ─►(−)                                                  └─► fan     (finest)
```

`u` is a **continuous virtual setpoint in °C** — what the unit would be given if it
accepted fractions. Everything upstream produces it; everything downstream spends it.

### `reset.py` — feedforward

`u_ff = c0 + c_out·T_outdoor + c_tgt·target`: the setpoint this load calls for,
computed rather than waited for, so the feedback loop is left with the residual
rather than with the weather.

Fitted from the **old controller's own output**, not from the plant — see
"Identification" below. The forecast enters here and nowhere else: the curve is
evaluated at the outdoor temperature expected one plant horizon (θ+τ ≈ 18 min)
ahead, bounded by `FORECAST_CAP`. That is a physical prediction of the disturbance,
unlike the v2–v4 anticipation lead, which extrapolated the room's own slope and
could not tell a disturbance from the controller's own unanswered command.

### `pi.py` — feedback

PI, no derivative: the regulated signal is a BLE thermometer that can go quiet for
ten minutes, and derivative action on it amplifies noise against a long dead time.

The error is the **Smith-predictor error**, `target − predict_settled`, not
`target − comfort`. Feeding back where the room is going to settle removes the dead
time from the loop, which is what lets a textbook tuning rule apply.

**Anti-windup by back-calculation**, and it is not optional: the output saturates at
the setpoint limits for hours on a hot afternoon. Most of the v3/v4 overshoot looks
in hindsight like an integrator nobody had written down and therefore nobody had
protected. Integration is also frozen whenever the actuator is not the controller's
to move — the guard has the room, the AC is off, or a managed-off is in progress.

Tuning is SIMC (Skogestad), **derived from the plant rather than stored**, so the
constants cannot drift out of step with the model they came from. One knob:
`tau_c_mult`, larger being slower and more robust.

### `split.py` — output stage

All three actuators in one currency, equivalent degrees of setpoint:

```
u_delivered(setpoint, blower) = setpoint − blower · g
```

Splitting the demand is then **mid-ranging**: aim the setpoint so the blower sits in
the middle of its range, leaving headroom both ways.

- **setpoint** — carries the steady-state load, moves only past half a step plus a
  hysteresis margin, never faster than a 6-min compressor dwell. The hysteresis is on
  the *output*: a deadband on the input error makes the loop blind, a deadband on the
  output only makes it patient.

  That margin has a **hard floor, and it is not a matter of taste**. Stepping the
  setpoint by 1 immediately moves the predicted settled point by `K`, and the loop
  answers with `Kc·K` of demand *against* the step it just took. A step from distance
  `d` therefore lands `(1 − d) + Kc·K` from the new setpoint, so unless the threshold
  clears `(1 + Kc·K)/2` the step gains nothing and the loop hunts. Shipped first at
  0.65 against a floor of 0.66 and it hunted on the real room within twenty minutes —
  setpoint 24→25→24 in six, with the blower and fan round-tripping alongside. Now
  computed from the model, so a re-fit cannot quietly reintroduce it.
- **blower** — takes the residual the integer setpoint cannot express.
- **fan** — takes what is left. It does not cool the room, so it cannot close the
  loop; it makes the residual tolerable while the slower actuators catch up.

`g = 0` collapses this to setpoint-plus-fan with no special case. Since `g` was not
identifiable (below), the blower currently steps on the *sign* of the residual — the
direction is known even when the magnitude is not.

The fine actuators are sized against `u_raw`, the demand **before** the clamp. At the
setpoint floor the clamped demand carries no information; the unclamped demand still
says how far short the unit is falling, which is when they are most worth having.

### Where the loop aims — a zone, not a point

The user's interface is a **target and a band**, and a band means "this much drift is
fine". So the loop regulates to a zone: no error while the room is comfortable, an
error measured to the nearest edge once it is not.

That is what makes the band a real knob. Under a point-tracking loop the band only
changed which excursions got *counted* — widening a symmetric band did nothing at all
to compressor motion, which quietly contradicted the whole premise of "hold band
accurately, then widen it to calm the cycling".

**Centre and width are separate questions and are computed separately:**

```
centre = target + (band_high − band_low)/2      ← accuracy
half   = (band_low + band_high)/2 × 0.5         ← calm
```

The centre is about fit: an asymmetric band's middle is not its target, and aiming
anywhere else spends margin on the side that has less of it. The width is about calm:
it is how much drift earns no reaction. Deriving the zone as `target ± band·frac` ties
the two together and under-shifts the centre in proportion to how lazy the loop is
asked to be — measured at 3.4 points of band fit worse, for no gain.

The zone is a **fraction** of the band, not the band itself, because correcting back
only as far as the band edge is the classic dead-band failure: the load pushes the
room straight back out and the loop cycles on the boundary.

The fraction is 0.5, chosen at the knee of the measured trade:

| inner zone | in band | setpoint moves/h |
|---|---|---|
| 0.0 (a point) | 90.4% | 2.0 |
| **0.5** | **89.7%** | **0.8** |
| 0.75 | 84.7% | 0.4 |
| 1.0 (the whole band) | 81.4% | 0.3 |

and the band lever it restores, at that fraction:

| band | in band | rms | moves/h |
|---|---|---|---|
| ±0.3 | 51.3% | 0.400 | 1.1 |
| −0.4/+0.7 | 80.4% | 0.410 | 0.8 |
| −0.5/+0.8 | 89.7% | 0.437 | 0.8 |
| −0.7/+1.0 | 95.3% | 0.426 | 0.4 |
| −0.9/+1.2 | 98.4% | 0.463 | 0.4 |

rms barely moves across that whole range: the room is not drifting more, the loop is
simply no longer fussing. Widening the band buys calm at almost no cost in where the
room actually sits.

The zone shifts down by `no_fan_offset` when no fan can run: warmth is less tolerable
without a breeze. A shift, never a widening — a band that grows is a band the
controller can sit inside, which is how the room parked at 27.25 on 07-26.

`tau_c_mult` stays an internal robustness constant. It is the textbook knob for loop
aggressiveness, but it is not a knob a person should have to hold an opinion about;
the band expresses the same intent in the units the room is actually described in.

### `safety.py` — unchanged

A pure override the controller cannot reason about. Trips on wide absolute rails,
releases on the reading rather than a timer, and `effective_rails()` pushes a rail out
when it sits inside the band's own ripple — a rail inside the ripple is not a backstop
but a second, cruder controller whose only move is to cut the compressor.

## What was removed

| removed | why |
|---|---|
| the branch ladder | every fault of v1–v4 was an interaction *between* branches, not a bug inside one |
| power-based engagement | whole-house meter; all four escalations it triggered after the v4 deploy were wrong, one of them into a guard trip. One threshold cannot serve both "engaged" and "unresponsive", so fixing a false positive mechanically created a false negative |
| the anticipation `lead` | swept over 24 h: band fit flat from lead 0 to 5, while setpoint moves scaled 2.3 → 3.7/h and the worst cold excursion tripled |
| `adapt.py`, the online learner | not required, not observed to help, and its one measurable effect was a positive feedback loop — a broken engagement rule ratcheted the dead time to 19 min, which set an 11.5-min dwell, which made the next episode worse |
| `_commanded_sp` and re-assert | `u` is absolute and recomputed every tick, so the command is "write it if it differs from what the unit reports" — the cloud proxy re-reporting its own value is now self-correcting by construction |
| the setpoint deadband | replaced by output hysteresis, which is what it should always have been |
| `recent_log` on the sensor | debugging output, and the largest attribute payload published; it belongs in the eval tooling |

## Identification

`tools/fit.py`, offline, reviewed by a human. **A week of closed-loop history
identifies some constants and not others, and the tool says which.**

The controller moves the setpoint *because* the room moved, so regressing the room on
the setpoint recovers the controller's inverse rather than the plant. Run that way this
data returns a gain of −0.04 °C/°C — the wrong sign — and a blower coefficient saying
more airflow makes the room *warmer*, which is the old controller's rule, not physics.
A dynamic regressor (the materialising FOPDT response, which lags and smooths the
setpoint) reduces the bias but does not remove it: the search then drives θ and τ to
the bottom of the grid, because a short dead time is what best lines up with a
controller that answers the room within one tick.

So:

- **K, θ, τ are priors, not measurements** — 0.5, 10 min, 8 min, from the old online
  adapter. The gain is taken at the **top** of its plausible range on purpose: it
  divides into `Kc`, so guessing high gives a gentler loop and guessing low an
  over-aggressive one.
- **`g`, the blower's authority, is 0** — not identified.
- **The reset curve IS identified**, by regressing the old controller's own output on
  outdoor temperature, which is exogenous. −0.21 °C of setpoint per outdoor °C. The
  target coefficient is pinned by physics at 1/K rather than fitted, because the target
  never moved more than 0.5 °C in the whole week.
- **Time of day needs no term.** It explains 11.7% of what the outdoor term does not,
  and its hour-to-hour profile is nearly as rough as noise (0.18 against 0.08 for a
  clean daily cycle and 0.29 for none). Day and night already enter through the 48-point
  target curve and the quiet caps.

Getting K, θ, τ properly needs an **open-loop step test** — the controller disabled and
the setpoint driven deliberately for an hour or two. Nothing in this design depends on
having it, but it is the only way to replace those three priors with measurements.

## Measured outcome

`tools/replay.py --windows`, six windows including a hot afternoon and a mild night,
against the recorded behaviour on the same readings.

At band ±(0.5, 0.8), the best band measured in the v4 sweep:

| window | recorded | new core | rms | moves/h |
|---|---|---|---|---|
| v4, post-deploy | 63.5% | **81.2%** | 0.647 → 0.550 | 5.8 → 2.2 |
| v3.2, −1 day | 86.2% | **95.0%** | 0.376 → 0.316 | 5.3 → 1.8 |
| v3.2, −2 days | 77.9% | **94.5%** | 0.465 → 0.310 | 4.4 → 1.8 |
| v3.2, −3 days | 95.6% | **97.2%** | 0.321 → 0.440 | 4.0 → 3.1 |
| hot afternoon | 99.2% | 87.2% | 0.314 → 0.361 | 4.2 → 1.2 |
| mild night | 68.2% | **87.2%** | 0.605 → 0.451 | 5.2 → 2.0 |
| **mean** | **81.8%** | **90.4%** | 0.455 → 0.405 | 4.8 → 2.0 |

Zero guard trips on every window, and every window inside the ≤4 setpoint moves/h
criterion. The one consistent loss is the hot afternoon, where the recorded run was
already near-perfect.

The closed-loop arm assumes the gain that could not be identified, so treat the
absolute numbers as indicative and the recorded column as ground truth. The comparison
survives the assumption: sweeping K over 0.3–0.7 moves the numbers but not the ordering.
