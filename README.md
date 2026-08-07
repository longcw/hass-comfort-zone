# Comfort Zone

An adaptive, multi-actuator climate controller for Home Assistant.

Most HA thermostat automations regulate one number against a band and end up
flipping the AC constantly. Comfort Zone instead **orchestrates all of a room's
devices** — AC setpoint, AC blower, a circulation fan, and AC power — to hold a
humidity-aware `comfort_temp` on a per-hour target curve, and it optimises for
**band fit**: how much of the day the room actually spends inside its band,
counting a cold overshoot as the same failure as sitting warm.

Cycling is a cost, not the objective. On a duty-cycling VRF it is not avoidable,
and chasing it directly means paying for calm with comfort. The band is the one
knob that trades the two, and it belongs to you: widen it and the compressor
calms down on its own.

## Why it's different

- **Textbook, not invented.** A PI loop with a Smith predictor for the dead time,
  outdoor-reset feedforward for the weather, and split-range mid-ranging across the
  actuators. Every piece has published tuning rules and decades of HVAC use.
- **Weather-aware.** An outdoor-reset curve computes the setpoint the current load
  calls for, rather than waiting for error to appear. The hourly forecast is read one
  plant horizon ahead, so a hot afternoon is answered before it arrives.
- **Dead-time aware.** Rooms respond to a setpoint change ~10 min later. The loop
  feeds back where the room will *settle*, not what it reads now, which is what stops
  commands stacking into the lag.
- **Honest about a coarse actuator.** The setpoint is a four-position actuator whose
  one click is worth half the band. It carries the steady-state load and moves rarely;
  the blower and circulation fan carry the fraction it cannot express.
- **Anti-windup that is actually there.** The output saturates for hours on a hot day.
  Back-calculation unwinds the integral against what the actuators can really deliver,
  so an hour at the floor does not buy an hour of stubbornness afterwards.
- **Quiet hours.** A night window (default 22:00–07:00) caps both the circulation fan
  and the AC blower — down to no fan at all, or the blower pinned to its lowest speed.
  With no air movement the loop aims a little cooler, since warmth is less tolerable
  without a breeze. The safety guard is bound by neither cap.
- **Always-on safety guard** with hard limits, guaranteed managed-off return, and a
  stale-sensor fail-safe — a pure override the controller cannot reason about.
- **Constants fitted offline and reviewed**, by `tools/fit.py`, which reports which of
  them closed-loop history can identify and which it cannot. No online learning: it was
  not observed to help, and its one measurable effect was a feedback loop through a
  learned constant.

## Status

v5 — control core, Home Assistant integration, and a custom Lovelace card with a
drag-a-curve schedule editor.

- `docs/DESIGN.md` — the design, the evidence behind each choice, what has
  already been tried and failed, and the open questions.
- `docs/SIGNALS.md` — every signal the controller can see, how each is used,
  what bounds it, and the clues that are still unexploited.
- `docs/REDESIGN.md` — the proposal v5 was built from.

## Development

```bash
python tests/test_controller.py      # pure control-core scenarios, no HA needed
python tests/test_options.py         # strategy presets vs. hand-tuned knobs

python tools/fit.py --days 7         # re-fit the model from recorder history
python tools/replay.py --windows     # replay the new core against what actually ran
```

`tools/` talks to a live Home Assistant over its REST API and needs `HA_URL` and
`HA_TOKEN`; the entity ids are constants at the top of each script.

Install for testing by copying `custom_components/comfort_zone/` into your HA
config's `custom_components/` and restarting, then add the integration from
Settings → Devices & Services.
