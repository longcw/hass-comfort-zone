# Comfort Zone Card

A single-file, dependency-free Lovelace card for the Comfort Zone integration.
It shows the zone's live state and the controller's decision, lets you shape the
day's target temperature by dragging a curve, exposes strategy + tuning, and
streams the controller's recent decisions ("why did it do that").

![preview](preview.html) — open `preview.html` in any browser to see the card
with stub data (light + dark).

## Install (manual resource)

1. Copy the card into your Home Assistant config's `www/` folder:

   ```bash
   cp comfort-zone-card.js /path/to/homeassistant/config/www/comfort-zone-card.js
   ```

   (Or `scp` it to your HA host's `config/www/` directory.)

2. Register it as a Lovelace resource — **Settings → Dashboards → ⋮ → Resources
   → Add resource**:

   - URL: `/local/comfort-zone-card.js`
   - Type: **JavaScript Module**

   (Or in YAML mode, under `lovelace:` → `resources:`)

   ```yaml
   lovelace:
     resources:
       - url: /local/comfort-zone-card.js
         type: module
   ```

3. Hard-refresh the browser / restart the companion app so the new resource loads.

## Use

Add a card to any dashboard:

```yaml
type: custom:comfort-zone-card
zone: sensor.master_bedroom_status
```

- **`zone`** (required): the zone's *status* sensor. Every other entity
  (comfort/target/predicted sensors, strategy select, enable switch, tuning
  numbers) is discovered automatically from that sensor's device, with
  entity-id substitution as a fallback.

## Sections

- **Header** — comfort (amber, "how it feels") → target (blue, "the goal") with
  the ± band, the current mode pill and one-line reason, a telemetry chip row,
  and the enable toggle.
- **Daily target** — drag anywhere on the ribbon to shape the 48-point (30-min)
  target curve; the blue dashed line is *now*, the amber dot is the current
  comfort reading riding on the plan. **Save** writes it back (via the
  `comfort_zone.set_schedule` service); **Revert** discards edits.
- **Tuning** — strategy as a segmented control; steppers for band / hard-min /
  hard-max.
- **Recent decisions** — a comfort-vs-target sparkline + system-power strip, and
  the controller's decision stream (newest first) with the action taken and why.

## Notes

- Theme-aware: uses Home Assistant CSS variables, so it follows your light/dark
  theme. The semantic hues (blue = cool/target, amber = warm/feel, teal =
  fan/power, red/amber = safety) are chosen to stay legible on both.
- No build step, no external dependencies (Home Assistant blocks CDN imports).
- Degrades gracefully: if the status sensor is missing it shows a placeholder
  rather than erroring.
