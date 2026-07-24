# Comfort Zone

An adaptive, multi-actuator climate controller for Home Assistant.

Most HA thermostat automations regulate one number against a band and end up
flipping the AC constantly. Comfort Zone instead **orchestrates all of a room's
devices** — AC setpoint, AC blower, a circulation fan, and AC power — to hold a
humidity-aware `comfort_temp` on a per-hour target curve while **minimizing AC
cycling**.

## Why it's different

- **Dead-time aware.** Rooms respond to a setpoint change ~10–15 min later.
  Comfort Zone waits out the response instead of re-commanding into the lag —
  the main cause of short-cycling.
- **Proactive.** It reads whole-system AC power as a *leading* signal (a power
  rise precedes cooling by ~6 min) to act before the temperature sensor catches
  up, with the sensor slope as the ground-truth check.
- **Engagement-gated escalation.** If the AC ignores a setpoint step (power
  doesn't respond), it escalates immediately (25→24→23) instead of waiting.
- **Fan-first.** Air movement is a cheap comfort actuator, reached for before
  touching the compressor.
- **Always-on safety guard** with hard limits, guaranteed managed-off return,
  and a stale-sensor fail-safe — independent of the optimizer.
- **Learns its own constants** from recorder history (`identify_model`
  service): interpretable system identification, not a black box.

## Status

v1 — control core + Home Assistant integration. Custom Lovelace card
(drag-a-curve schedule, decision history) in progress. See `docs/DESIGN.md`.

## Development

```bash
python tests/test_controller.py      # pure control-core scenarios, no HA needed
```

Install for testing by copying `custom_components/comfort_zone/` into your HA
config's `custom_components/` and restarting, then add the integration from
Settings → Devices & Services.
