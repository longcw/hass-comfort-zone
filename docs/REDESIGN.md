# Comfort Zone v5 — redesign

Status: **proposal, not implemented.** Written 2026-08-07 to seed a fresh session.
Supersedes `docs/DESIGN.md` if adopted. Current live version is v4 (see `docs/EVAL.md`).

---

## 1. The request, verbatim

> since we back and forced a lot of times especially on the power engage algorithm. I think it's worth to refactor the whole thing as well as the UI bugs and UX design.
>
> 1. redesign the core: use better/commonly used algorithm instead of invent something, keep the goal to stable in band instead of reducing cycles, the assumption is when it can accurately in band, I can enlarge the band size so the cycle can be reduced as well; self-learning is not required since I didn't see it helpful past days, but you can still add it if you think it fits the new core, but I think the algorithm need a change: it should also take the time (day/night), weather forecast (outside env) into account since if env cooler or warm will cause different warming rate in the room.
> 2. the UI had a lot of bugs, now it's better but also was patched a lot. I'd like to keep the most of the UI, but remove the history action part, and refactor the code to make it stable
> 3. especailly for power engage: it's not working properly, for example, right now the power dropped because we increased setpoint, after a while the it says no cooling step to judge and set the setpoint from 26 to 27 where it's already engaged. it doesn' make sense.

---

## 2. Why a rewrite, in evidence

The v1–v4 core is a hand-built state machine: a cost-ordered ladder of branches
(`warm.step`, `warm.escalate`, `warm.dwell_engaged`, `warm.in_flight`, `cold.step`,
`band.to_neutral`, …), each with its own guard condition, plus a hand-rolled Smith
predictor, an anticipation lead, a learned setpoint deadband, and a power-based
engagement detector. Every fault of the last two weeks has been an *interaction between
branches*, not a bug inside one:

| date | fault | root cause |
|---|---|---|
| 07-25 | 26→25→24 in 45 s | net in-flight summing cancelled a fresh step |
| 07-26 | room parked at 27.25 with rail at 27.5 | learned deadband widened the warm gate |
| 08-06 | room reached 27.0, guard cut power 12× in 4.5 h | power outvoted a rising room; dwell 11.5 min; rail inside the band ripple |
| 08-07 | overcooling, 2 guard trips in 2.2 h | raising `engage_watts` widened the *unresponsive* region; escalation outranked the in-flight hold |

The last one is the clearest signal that the structure is the problem. Fixing the false
*positive* (power says engaged when it isn't) mechanically created a false *negative*
(power says dead when it isn't), because one threshold served both verdicts. That is not
a tuning mistake — it is what happens when a detector is wired into a branch that can
override the model.

Measured, same clock window, fixed band ±(0.4, 0.7):

| window | in band | cold | worst − | range | guard trips |
|---|---|---|---|---|---|
| post-v4 (08-06 22:45 → 08-07 01:00) | 52.8% | 27.7% | −0.68 | 2.10 | **2** |
| −1 day (v3.2) | 77.4% | 22.6% | −0.42 | 1.41 | 0 |
| −2 days (v3.2) | 73.5% | 19.9% | −0.28 | 1.44 | 0 |
| −3 days (v3.2) | 93.0% | 7.0% | −0.16 | 1.24 | 0 |

**v4 is worse than what it replaced.** Whatever is built next must be validated against
this table before it goes live.

---

## 3. The finding that should shape the design

**The setpoint is a 4-position actuator, and one position is worth half the band.**

- `target_temp_step = 1` on this unit — integer setpoints only, confirmed on the entity.
- Configured usable range is 24–27, i.e. **4 levels** (device allows 16–32).
- Measured plant gain ≈ **0.5 °C of comfort per 1 °C of setpoint**.

So one setpoint click moves the room ~0.5 °C, while a ±0.4 band is 0.8 °C wide. **A single
click is more than half the band.** No control algorithm can hold a tight band with an
actuator that coarse — this is a resolution limit, not a tuning failure, and it explains
why every version has oscillated.

Three consequences the new design must accept:

1. The **setpoint is the coarse, slow actuator**. It should carry the *steady-state load*
   and move rarely.
2. The **blower and circulation fan are the fine actuators**. They are near-continuous,
   fast, and cheap, and they must carry the trim. In v1–v4 they were treated as cheaper
   rungs of the same ladder; they should instead be a separate, faster inner loop.
3. Widening the band is not a workaround, it is the correct response to actuator
   resolution — which is exactly the user's stated plan.

---

## 4. Goal and non-goals

**Goal.** Maximise time inside the band. Fit is symmetric: a cold excursion counts the
same as a warm one. The band is the user's knob for trading fit against compressor
motion; widen it and cycling falls out.

**Non-goals.**
- Minimising compressor cycles directly. Measured over 24 h, a tight band costs *more*
  moves than a loose one; chasing cycles spends the thing being optimised.
- Online self-learning. Not required (user), and not observed to help. See §7.
- Cleverness. Prefer a named, textbook method with published tuning rules over anything
  invented here.

**Acceptance criteria** (must beat the −3d row above on a comparable window):
- ≥ 90% of ticks in band at ±(0.4, 0.7)
- worst excursion within ±0.35 °C of the band edge, both sides
- **zero** safety-guard trips in normal operation
- ≤ 4 setpoint moves/hour

---

## 5. Proposed core

Two standard pieces, in series. Nothing here is novel; all of it has published tuning
rules and decades of HVAC use.

```
outdoor temp ──► outdoor reset curve ──┐  (feedforward: the steady-state load)
   forecast  ──►                       │
                                       ▼
target ──►(+)──► error ──► PI + Smith predictor ──►(+)──► u  ──► split range
comfort ─►(−)              (feedback: the residual)          ├─► setpoint (coarse, slow)
                                                             └─► blower + fan (fine, fast)
```

### 5.1 Feedforward — outdoor reset (weather compensation)

The standard HVAC answer to "the load changes with the weather", and the direct answer to
the user's point about ambient. Rather than waiting for error to appear and then
correcting it, compute the setpoint the room *should* need:

```
u_ff = a - b · (T_outdoor - target)
```

A straight line, two parameters, fitted offline from history (§7). Extensions, in order
of value:

- **Time of day** enters through the existing 48-point target curve and through the
  night/day quiet caps. It does not need to enter the feedforward separately — most of
  the day/night difference *is* the outdoor temperature. Verify this on history before
  adding a term.
- **Forecast** for pre-cooling: `weather.forecast_home` provides hourly temperature
  (verified working: 28.8 → 31.2 °C over the next four hours). When the forecast implies
  the load will rise by more than the plant can answer within its dead time, bias `u_ff`
  down early. This is the *right* home for anticipation — a physical prediction of the
  disturbance, rather than extrapolating the sensor's own slope, which is what the v2–v4
  `lead` did and which measured as worthless (§7).
- **Dew point / humidity** are available on the same entity and matter because the
  regulated signal is humidity-weighted. Phase 2.

Available now, confirmed: `weather.forecast_home` → `temperature`, `humidity`,
`dew_point`, `cloud_coverage`, plus `weather.get_forecasts` (hourly).

### 5.2 Feedback — PI with a Smith predictor and anti-windup

- **PI, not PID.** Derivative on a noisy BLE thermometer with a 10-min dead time buys
  nothing and amplifies noise.
- **Smith predictor** for the dead time. The existing `model.py` already implements the
  useful half of this correctly and can be reused nearly as-is.
- **Anti-windup is mandatory.** The output saturates at 24 and 27 for hours at a time.
  Use back-calculation or a conditional integrator. *Most of the v3/v4 overshoot
  behaviour looks like integrator windup implemented accidentally and without the
  protection that normally comes with it.*
- **SIMC / lambda tuning** (Skogestad) gives the constants from the FOPDT model directly:

  ```
  Kc = τ / (K · (τ_c + θ))          Ti = min(τ, 4·(τ_c + θ))
  ```

  With measured `K = 0.5 °C/°C`, `τ = 8 min`, `θ = 10 min` and a robust `τ_c = 1.5θ`:
  `Kc ≈ 0.64`, `Ti ≈ 8 min`. These are starting points to verify in replay, not gospel.

### 5.3 Output stage — split range and quantisation

The single controller output `u` (a continuous "virtual setpoint" in °C) is split across
actuators by authority, which is the standard way to drive a coarse and a fine actuator
from one loop:

- **Fine band** — blower level and fan speed move continuously with `u` within roughly
  ±0.5 °C of the current setpoint. They answer in a minute or two.
- **Coarse band** — the integer setpoint changes only when `u` leaves the current level's
  authority by a hysteresis margin, and never faster than a compressor-protection dwell
  (~6 min). Hysteresis on the *output*, not on the input error — this is what a deadband
  should have been all along.

Because the output is an **absolute** setpoint recomputed every tick, the whole
`_commanded_sp` / drift-detection / re-assert machinery collapses to "write it if it
differs from what the device reports". The cloud proxy's habit of re-reporting its own
remembered setpoint (7 of 30 transitions on 08-06) is then self-correcting by
construction.

### 5.4 Safety

Keep `safety.py` essentially as-is — it is the one part with a clean contract. Keep
`effective_rails()` (rails pushed clear of the band, else ordinary ripple trips them) and
keep stale/unavailable sensor handling. The guard must remain a pure override that the
optimizer cannot reason about.

---

## 6. Delete: power-based engagement

**Remove it entirely from the control path.** This is the single biggest simplification
and it directly answers the user's point 3.

The evidence, from the 2.2 h after the v4 deploy — every escalation the power veto
triggered was wrong:

```
23:10:29 escalated on   +74W at y=27.19  →  within 15 min y fell to 26.20  (−0.99)
23:59:15 escalated on    +2W at y=26.87  →  within 15 min y fell to 26.04  (−0.83)
00:05:15 escalated on  +378W at y=26.91  →  within 15 min y fell to 25.19  (−1.72)  ← guard trip
00:46:32 escalated on    −3W at y=27.02  →  within 15 min y fell to 26.72  (−0.30)
```

Four for four. The command had taken every time; the meter simply could not see it.

Three independent reasons it cannot work here:

1. **The meter is whole-house.** `sensor.shuang_lu_hu_gan_ji_liang_qi_power_2` is shared
   with every other room. This room's step is a minority of the signal — at 23:59 the
   meter read 685 W before and 1046 W after, while other rooms moved by more than that on
   their own.
2. **One threshold cannot serve two verdicts.** `_power_says_unresponsive()` returns true
   when `rise < engage_watts`, so raising that constant to fix false positives
   mechanically widened the false-negative region. `+378 W` was logged as "an unchanged
   level".
3. **It is asymmetric and blind by construction — the user's example.** `_power_rise()`
   returns `None` whenever `_last_cmd_cooling` is false, so after *any* easing command
   there is no engagement signal at all. Raising the setpoint correctly makes power fall,
   and the controller then reports "no cooling step to judge" and eases again — 26 → 27 —
   with no way to notice the first easing had already worked. There is no cold-side
   engagement check to balance the warm-side one.

A PI controller does not need an engagement detector. Persistent error *is* the evidence
that a command did not take, and the integrator responds to it proportionally rather than
through a binary veto that can outrank the model.

**Keep power as:** a display value on the card, and optionally a feedforward hint for the
fan. **Never** as a gate on setpoint decisions.

---

## 7. Self-learning → offline identification

The user does not require online learning, and the data agrees.

The anticipation `lead` was swept over 24 h of history at band 0.5/0.8: band fit is
**flat** from lead 0 to 5 (88.1–89.1% in band, rms 0.474–0.486 — all inside the noise),
while setpoint moves scale 2.3 → 3.7/h and the worst cold excursion triples past lead 3
(−0.10 → −0.41 °C). It cost moves and bought nothing.

The online adapter also proved actively harmful in a subtle way: `dead_time` ratcheted to
19.1 min under a broken engagement rule, which set an 11.5-min dwell, which made the next
episode worse — a positive feedback loop through a learned constant.

**Replace with:**
- **Offline system identification**, run occasionally and reviewed by a human. `system_id.py`
  already exists for this. Fit `K`, `τ`, `θ` and the outdoor-reset coefficients `a`, `b`
  from recorder history.
- **Gain scheduling** on outdoor temperature if one fit does not cover hot and mild days.
  Deterministic, inspectable, reproducible.
- **The PI integrator** already provides the adaptation the learner was reaching for —
  it absorbs any steady-state load the feedforward missed — but bounded and with
  well-understood dynamics.

If online adaptation is added later, adapt only the **feedforward intercept** (a slow
load bias), never the feedback gains or the dead time.

---

## 8. UI / UX

Keep the card's look and its schedule editor. The specific asks:

- **Remove the history/action list.** Drop `recent_log` from the sensor attributes and
  the card. It exists for debugging and belongs in the eval tooling, not the card. This
  also removes the largest attribute payload the sensor publishes.
- **Refactor for stability.** The card has been patched repeatedly. Rebuild it around:
  - one explicit view-model built from the sensor attributes in a single place;
  - one pure render pass — no incremental DOM patching, no state stashed on elements
    (`el._sel`, `el._activeEdit` today);
  - the schedule editor isolated as the only stateful component;
  - the "why" panel rewritten against the new controller's much smaller state:
    `error`, `u_ff`, `u_fb`, `u`, saturation flag, dwell remaining. The v4 panel renders
    a dozen ladder-specific fields that will not exist.
- Keep the headless render check (`frontend/preview.html` + Chrome `--screenshot`); it
  caught real regressions. Note `node` is **not** installed on this machine, so
  `node --check` is unavailable — the render is the syntax check.

---

## 9. Validation plan

Do not deploy anything without replaying it first. `tools/replay.py` already does this and
should be extended, not rewritten:

- **Decision replay (exact).** Feed recorded ticks in, record decisions. Answers "would
  the guard have tripped", "what would it have commanded", with no plant assumptions.
- **Closed-loop simulation (model-dependent).** Reconstructs the thermal load as the
  residual after removing the identified response to recorded setpoints, then drives the
  new controller against the same load.
- **Metrics.** % ticks in band, **rms error vs target** (band-independent, so it is the
  honest tracking measure), worst excursion per side, guard trips, setpoint moves/h.

Two harness bugs found the hard way, worth carrying forward: the fan must be *simulated*
(pinning it on leaves the cold arm stuck at "ease fan first" and the loop reads far calmer
and colder than it is), and the recorded baseline must use the same band definition as the
simulated arm.

Add: replay across **several days including a hot afternoon and a mild night**, since the
whole point of the feedforward is that those differ.

---

## 10. Migration

1. Build the new core alongside the old one — it is pure and testable with no Home
   Assistant import, as `controller.py`/`model.py`/`safety.py` already are.
2. Fit `K, τ, θ, a, b` offline from ≥ 1 week of history.
3. Replay against v3.2 and v4 on the same windows. Must clear §4's acceptance criteria.
4. Deploy behind the existing `switch.master_bedroom_enabled`; watch one full day.
5. Rollback path is unchanged: `git checkout <tag> -- custom_components/comfort_zone`,
   redeploy, restart. The learned-model store should be **deleted**, not migrated.

Deploy loop, host, credentials: `~/.claude/shared-memory/topics/home-assistant.md`.
A backend change needs a **full HA restart** — a config-entry reload does not re-import
changed modules.

---

## 11. Measured constants for the new session

| quantity | value | source |
|---|---|---|
| plant gain `K` | ~0.5 °C comfort per °C setpoint | online adapter, stable across days |
| time constant `τ` | ~8 min | model default, never re-fit — **verify** |
| dead time `θ` | 10–19 min | drifted under a broken rule; **re-fit offline** |
| setpoint step | **1 °C** (integer only) | `target_temp_step` on the entity |
| setpoint range | 24–27 configured, 16–32 device | zone options / entity |
| blower levels | 自动 / 低风 / 中风 / 高风 | `fan_modes`; top level reserved for the guard |
| tick | 45 s | `TICK_SECONDS` |
| power meter | **whole-house, shared** | the reason §6 exists |
| comfort sensor | BLE crib thermometer, can go quiet ~10 min | `STALE_AFTER_S = 1200` |
| duty cycle | ~18 min on, 2–5 min off | measured |
| outdoor temp + hourly forecast | `weather.forecast_home` | verified working |
| best measured band | `band_low 0.5 / band_high 0.8` | 24 h sweep: 87–88% in band, ~2.5–3.0 moves/h |

## 12. Open questions

1. **Widen the setpoint range?** 24–27 is 4 levels. Allowing 23 (or 22) gives the loop
   more authority on hot afternoons, at the risk of deeper overshoot. Test in replay
   before changing — this is a comfort decision for a baby's room, so it is the user's.
2. **Is `τ = 8 min` real?** It has never been fitted, only defaulted. It feeds every
   tuning formula in §5.2.
3. **Does day/night need its own feedforward term**, or is it fully explained by outdoor
   temperature? Check on history before adding a parameter.
4. **Should the blower be modelled?** It is treated as a comfort lever today, but it
   plainly changes the delivered cooling and therefore the plant gain. If the split-range
   design in §5.3 is adopted, its effect needs to be in the model.
