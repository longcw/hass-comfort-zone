// comfort-zone-card.js
// A Lovelace card for the Comfort Zone integration — an instrument panel for an
// autonomous climate agent. It shows what the room feels like, the comfort zone
// being held, what the loop is asking of the unit (and why), the meter's answer
// to it, and lets you shape the day's target curve by dragging it.
//
// Dependency-free vanilla custom element (no Lit, no build step, no CDN — HA
// blocks external imports). Register as a Lovelace module resource.
//
// Three layers, and nothing crosses them:
//   viewModel(hass, config, cache)  — the only place that reads hass. Pure.
//   render*(vm)                     — pure HTML strings, one island each, written
//                                     only when the island's signature changes.
//   ScheduleEditor                  — the one stateful component: it owns its
//                                     draft, its selection and its listeners.
//
// Config:
//   type: custom:comfort-zone-card
//   zone: sensor.master_bedroom_status   # the zone's status sensor
//   title: 主卧 舒适          # optional; defaults to the zone's name
//   curve_min: 25            # optional; daily-target chart y-axis range (default 25–27)
//   curve_max: 27
//
// The title renders as a `.card-header`, so section-jump-nav (which scans the
// shadow tree for a .card-header matching the section target) can jump to it —
// set its `target` to this title.
//
// All related entities (comfort/target/predicted sensors, strategy select,
// enable switch, tuning numbers) are discovered from the status sensor's device
// where the registry is available, with entity-id substitution as a fallback.

(() => {
  const VERSION = "0.3.3";

  // --- semantic palette (mid-chroma so it reads on light AND dark surfaces) ---
  const C = {
    cool: "#3b8ff0",   // cooling / the target-goal line
    warm: "#f0913b",   // the comfort "feel" signal
    teal: "#20b2a0",   // fan / easing
    grey: "#8a8f98",   // managed-off
    amber: "#f5a623",  // overheat caution
    danger: "#e5484d", // overcool danger / fail-safe
    ok: "#35c07a",     // idle / on target
    out: "#8b7fd4",    // outdoor overlay — its own axis, so its own colour
  };

  const MODES = {
    idle: { label: "On target", color: C.ok },
    cooling: { label: "Cooling", color: C.cool },
    easing: { label: "Easing", color: C.teal },
    fan_assist: { label: "Fan assist", color: C.teal },
    safety_overheat: { label: "Overheat guard", color: C.amber },
    safety_overcool: { label: "Overcool guard", color: C.danger },
    stale_hold: { label: "Sensor stale", color: C.amber },
    failsafe: { label: "Fail-safe", color: C.danger },
    disabled: { label: "Disabled", color: C.grey },
  };

  const STRATEGIES = ["baby", "eco", "comfort", "custom"];
  const DEF_TMIN = 25, DEF_TMAX = 27; // default y-axis range (°C); override with curve_min/curve_max
  const SLOTS = 48;             // 30-min slots
  // The chart window clips what is DRAWN. Edits span the whole sensible room
  // range, so a value from outside the window can still be nudged back into it.
  const EDIT_MIN = 16, EDIT_MAX = 32;
  // Hours of meter history under the chart. Short on purpose: this strip is read
  // against the setpoint ticks drawn on it, and at six hours the changes crowded
  // together until most of their labels had to be suppressed to avoid overlapping.
  const POWER_HOURS = 3;

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const snap = (v, step) => Math.round(v / step) * step;
  const fnum = (v) => (v === null || v === undefined || v === "" || isNaN(+v) ? null : +v);

  // Every interpolated value goes through this. Reasons, branch ids, friendly
  // names and entity ids all come from outside and all land inside markup.
  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (v) => (v === null || v === undefined ? "" : String(v).replace(/[&<>"']/g, (c) => ESC[c]));

  // Readouts of the controller's own numbers. Signed where the sign carries
  // meaning (an error, a rate) and fixed-width so the panel's rows line up.
  const f1 = (v) => (fnum(v) == null ? "–" : (+v).toFixed(1));
  const f2 = (v) => (fnum(v) == null ? "–" : (+v).toFixed(2));
  const sg2 = (v) => (fnum(v) == null ? "–" : (v >= 0 ? "+" : "") + (+v).toFixed(2));
  const sg3 = (v) => (fnum(v) == null ? "–" : (v >= 0 ? "+" : "") + (+v).toFixed(3));

  // Blower level names shown in the UI. Units label the same three speeds every
  // which way (低风/弱风/Low/Quiet …); display one consistent set regardless of
  // which vendor label the device happens to report. Unknown labels pass through.
  const BLOWER_LABELS = {
    "低风": "低速", "低速": "低速", "弱风": "低速", "low": "低速", "quiet": "低速",
    "silent": "低速", "weak": "低速", "level1": "低速",
    "中风": "中速", "中速": "中速", "medium": "中速", "mid": "中速", "middle": "中速",
    "normal": "中速", "level2": "中速",
    "高风": "高速", "高速": "高速", "强风": "高速", "high": "高速", "strong": "高速",
    "turbo": "高速", "powerful": "高速", "level3": "高速",
    "自动": "自动", "auto": "自动",
  };
  const blowerLabel = (v) => {
    if (v === null || v === undefined) return v;
    const k = String(v).trim();
    return BLOWER_LABELS[k] ?? BLOWER_LABELS[k.toLowerCase()] ?? v;
  };

  // ===========================================================================
  // Entity resolution
  // ===========================================================================
  // Each key is claimed by an entity-id pattern first (authoritative — HA
  // slugifies the entity's name into its id) and only then by a friendly-name
  // pattern. Both sets are mutually exclusive within a domain, so no key depends
  // on the order the registry happens to hand entities over in.
  const RULES = {
    sensor: [
      ["comfort", /_comfort_temperature(_\d+)?$/, /comfort/],
      ["target", /_target(_\d+)?$/, /target/],
      ["predicted", /_predicted_settled(_\d+)?$/, /predict/],
      ["slope", /_rate_of_change(_\d+)?$/, /rate of change|slope|变化/],
    ],
    switch: [
      ["fanAssist", /_fan_assist(_\d+)?$/, /fan assist/],
      ["enable", /_enabled(_\d+)?$/, /enabled/],
    ],
    select: [
      ["strategy", /_strategy(_\d+)?$/, /strategy/],
    ],
    number: [
      ["bandLow", /_band_low(_\d+)?$/, /band.*low/],
      ["bandHigh", /_band_high(_\d+)?$/, /band.*high/],
      ["noFanOffset", /_no_fan_offset(_\d+)?$/, /cooler when fan off|no.?fan/],
      ["hardMin", /_hard_min(_\d+)?$/, /hard min/],
      ["hardMax", /_hard_max(_\d+)?$/, /hard max/],
      ["fanMaxNight", /_fan_max_night(_\d+)?$/, /fan max.*night/],
      ["fanMaxDay", /_fan_max_day(_\d+)?$/, /fan max.*day/],
    ],
  };

  // Memoized on the status entity's device plus how many entities that device
  // owns — the pair changes exactly when the registry has something new to say,
  // and scanning the registry on every hass tick was the card's hottest loop.
  function resolveEntities(hass, statusId, cache) {
    const reg = hass.entities || {};
    const device = reg[statusId] ? reg[statusId].device_id : null;
    const sibs = device ? Object.keys(reg).filter((id) => reg[id].device_id === device) : [];
    const key = `${statusId}|${device || "-"}|${sibs.length}`;
    if (cache.key === key) return cache.ent;

    const out = { status: statusId };
    const nameOf = (id) => String(hass.states[id]?.attributes?.friendly_name || "").toLowerCase();
    for (const byName of [false, true]) {
      for (const id of sibs) {
        if (id === statusId) continue;
        const rules = RULES[id.split(".")[0]];
        if (!rules) continue;
        const probe = byName ? nameOf(id) : id;
        for (const [k, idRe, nameRe] of rules) {
          if (out[k]) continue;
          if ((byName ? nameRe : idRe).test(probe)) { out[k] = id; break; }
        }
      }
    }

    // Fallbacks by id substitution (the registry path above is preferred).
    const slug = statusId.replace(/^sensor\./, "").replace(/_status$/, "");
    out.comfort ||= `sensor.${slug}_comfort_temperature`;
    out.target ||= `sensor.${slug}_target`;
    out.predicted ||= `sensor.${slug}_predicted_settled`;
    out.slope ||= `sensor.${slug}_rate_of_change`;
    out.enable ||= `switch.${slug}_enabled`;
    out.fanAssist ||= `switch.${slug}_fan_assist`;
    out.strategy ||= `select.${slug}_strategy`;
    out.bandLow ||= `number.${slug}_band_low`;
    out.bandHigh ||= `number.${slug}_band_high`;
    out.noFanOffset ||= `number.${slug}_no_fan_offset`;
    out.fanMaxDay ||= `number.${slug}_fan_max_day`;
    out.fanMaxNight ||= `number.${slug}_fan_max_night`;

    // Zone name for the set_schedule service = friendly name minus " Status".
    const fn = hass.states[statusId]?.attributes?.friendly_name || slug;
    out.zoneName = fn.replace(/\s*status$/i, "").trim() || slug;

    cache.key = key;
    cache.ent = out;
    return out;
  }

  // ===========================================================================
  // View model — the one place that reads hass
  // ===========================================================================
  function viewModel(hass, config, cache) {
    const zoneId = config.zone;
    const status = hass.states[zoneId];
    if (!status || status.state === "unavailable") {
      return { ok: false, missing: `Waiting for ${zoneId}…` };
    }
    const a = status.attributes || {};
    const em = a.entities || {};
    const ent = resolveEntities(hass, zoneId, cache);
    const st = (id) => (id ? hass.states[id] : undefined);
    const num = (id) => fnum(st(id)?.state);

    const enabled = a.enabled !== false;
    const mode = enabled ? status.state : "disabled";
    const meta = MODES[mode] || { label: mode, color: C.grey };

    const comfort = num(ent.comfort);
    const target = num(ent.target);
    const bandLow = fnum(a.band_low) ?? 0.4;
    const bandHigh = fnum(a.band_high) ?? bandLow;
    // The loop holds a ZONE, not a point, and makes no correction inside it. It is
    // narrower than the band, sits mid-band, and drops when no fan can run — so it
    // is not the scheduled target and the hero must not pretend otherwise.
    const zoneLo = fnum(a.zone_lo) ?? (target != null ? target - bandLow : null);
    const zoneHi = fnum(a.zone_hi) ?? (target != null ? target + bandHigh : null);
    const zoneMid = zoneLo != null && zoneHi != null ? (zoneLo + zoneHi) / 2 : target;
    const onTarget = comfort != null && zoneLo != null && zoneHi != null
      && comfort >= zoneLo && comfort <= zoneHi;

    const outdoor = fnum(a.outdoor);
    const slope = fnum(a.slope);
    const power = em.power ? (num(em.power) ?? fnum(a.power)) : fnum(a.power);
    // Short-window power trend. It annotates the live reading below it, so it
    // tracks the same window; nothing in the loop reads power at all.
    const recent = fnum(a.power_recent);

    // AC and fan chips read LIVE from the device entities, falling back to the
    // tick snapshot: the AC keeps cooling while the controller sits idle, and the
    // difference between the two is worth seeing.
    const acSt = st(em.ac);
    const acOn = acSt ? !["off", "unavailable", "unknown"].includes(acSt.state) : a.ac_on;
    const acState = acSt ? acSt.state : a.ac_state;
    const acSetpoint = acSt ? fnum(acSt.attributes.temperature) : fnum(a.setpoint);
    const acBlower = blowerLabel(acSt ? acSt.attributes.fan_mode : a.ac_blower);

    const fanSt = st(em.fan);
    const fanOn = fanSt ? fanSt.state === "on" : a.fan_on;
    const fanLevel = em.fan_speed ? num(em.fan_speed) : fnum(a.fan_level);
    const fanAssistSt = st(ent.fanAssist);
    const fanAssist = fanAssistSt ? fanAssistSt.state === "on" : (a.fan_assist_enabled !== false);

    const chip = (k, v, cls = "", entity = null) => ({ k, v, cls, entity });
    // Power carries a short-window trend arrow; nothing in the loop reads power.
    const powerChip = power == null ? null
      : chip("power", `${(power / 1000).toFixed(power >= 1000 ? 1 : 2)}kW`, "", em.power);
    if (powerChip && recent != null && Math.abs(recent) >= 200) {
      powerChip.arrow = recent > 0 ? "up" : "dn";
    }
    const acChip = acOn
      ? chip("ac", [acState && acState !== "cool" ? acState : "cool",
        acSetpoint != null ? `${acSetpoint}°` : "", acBlower || ""].filter(Boolean).join(" "), "", em.ac)
      : chip("ac", "off", "soft", em.ac);
    // How long the unit has actually sat on this setpoint — the "is it fussing?"
    // number, and the whole point of leaving the compressor alone. Measured from
    // OBSERVED transitions, so it is null until one has been seen. Past the 6 min
    // dwell the hold is real rather than incidental, and earns the calm colour.
    const held = fnum(a.sp_held_min);
    const heldChip = chip("held",
      held == null ? "—" : held >= 60
        ? `${Math.floor(held / 60)}h${String(Math.floor(held % 60)).padStart(2, "0")}m`
        : `${Math.floor(held)}m`,
      held == null ? "soft" : held >= 6 ? "calm" : "", em.ac);
    const fanChip = !fanAssist ? chip("fan", "assist off", "soft", em.fan)
      : a.fan_max_level === 0 ? chip("fan", "capped off", "soft", em.fan)
      : fanOn ? chip("fan", fanLevel != null ? String(fanLevel) : "on", "", em.fan)
      : chip("fan", "off", "soft", em.fan);

    // The night chip names the caps it imposes: they are the reason the room is
    // allowed to behave differently after lights-out.
    const caps = [
      a.fan_max_level === 0 ? "no fan" : a.fan_max_level != null ? `fan ≤${a.fan_max_level}%` : "",
      a.blower_max ? `≤${blowerLabel(a.blower_max)}` : "",
    ].filter(Boolean).join(" · ");

    const chips = [
      acChip,
      heldChip,
      fanChip,
      powerChip,
      // OUT opens the weather entity: HA's own dialog already draws the hourly
      // forecast, which is the next question anyone asks of an outdoor reading.
      outdoor != null ? chip("out", `${outdoor.toFixed(1)}°`, "", em.weather) : null,
      slope != null ? chip("slope", `${slope >= 0 ? "+" : ""}${slope.toFixed(2)}`, "", ent.slope) : null,
      a.strategy ? chip("strategy", a.strategy) : null,
      a.is_night ? chip("", `☾ night${caps ? ` · ${caps}` : ""}`, "soft") : null,
      a.safety_state && a.safety_state !== "normal" ? chip("safety", a.safety_state, "warn") : null,
    ].filter(Boolean);

    // Sensor freshness: when did the regulated source thermometer last report?
    // (the controller freezes on a stale reading, so surfacing it is useful)
    const freshEnt = em.comfort || em.temp || ent.comfort;
    let fresh = null;
    const fst = freshEnt ? hass.states[freshEnt] : null;
    if (fst) {
      const lu = (fst.last_reported && fst.last_reported > fst.last_updated)
        ? fst.last_reported : fst.last_updated;
      const d = lu ? new Date(lu) : null;
      if (d && !isNaN(d)) {
        const ageS = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000));
        const ago = ageS < 60 ? "just now"
          : ageS < 3600 ? `${Math.floor(ageS / 60)}m ago`
          : `${Math.floor(ageS / 3600)}h ${Math.floor((ageS % 3600) / 60)}m ago`;
        const hhmmss = d.toLocaleTimeString([], {
          hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
        fresh = { text: `updated ${hhmmss} · ${ago}`, stale: ageS > 20 * 60, entity: freshEnt };
      }
    }

    const knob = (id, label, signed = false) => {
      const s = st(id);
      if (!s) return null;
      return { id, label, signed, value: fnum(s.state),
        unit: s.attributes.unit_of_measurement || "" };
    };

    const sched = a.schedule;
    return {
      ok: true,
      title: config.title || ent.zoneName,
      zoneName: ent.zoneName,
      ent,
      enabled,
      mode,
      modeColor: meta.color,
      // "idle" already reads "On target"; don't double it up.
      pillLabel: mode === "idle" ? meta.label : meta.label + (onTarget ? " · on target" : ""),
      comfort, target, zoneLo, zoneHi, zoneMid, bandLow, bandHigh,
      acBlower,
      reason: a.reason || "",
      branch: a.branch || "",
      decision: a.decision && Object.keys(a.decision).length ? a.decision : null,
      chips,
      fresh,
      feelEntity: em.comfort || em.temp || ent.comfort || zoneId,
      goalEntity: em.status || zoneId,
      historyEntity: em.comfort || ent.comfort,
      weatherEntity: em.weather || null,
      powerEntity: em.power || null,
      acEntity: em.ac || null,
      strategy: st(ent.strategy)?.state || a.strategy || "baby",
      fanAssist: fanAssistSt ? { id: ent.fanAssist, on: fanAssist } : null,
      // The fan caps sit next to the offset because a cap of 0 is what makes "no
      // fan available" true, and the offset is what that then costs in degrees.
      knobs: [
        knob(ent.bandLow, "band low", true),
        knob(ent.bandHigh, "band high"),
        knob(ent.noFanOffset, "cooler w/o fan", true),
        knob(ent.fanMaxDay, "fan day"),
        knob(ent.fanMaxNight, "fan night"),
      ].filter(Boolean),
      schedule: Array.isArray(sched) && sched.length === SLOTS
        ? sched.map(Number) : new Array(SLOTS).fill(26),
    };
  }

  // ===========================================================================
  // Render — pure, one string per island
  // ===========================================================================
  function renderHeader(vm, showWhy) {
    const chips = vm.chips.map((c) => {
      const clickable = c.entity ? ` data-action="more" data-entity="${esc(c.entity)}"` : "";
      const arrow = c.arrow ? ` <span class="${c.arrow}">${c.arrow === "up" ? "▲" : "▼"}</span>` : "";
      return `<div class="chip ${esc(c.cls)} ${c.entity ? "clk" : ""}"${clickable}>
        ${c.k ? `<span class="k">${esc(c.k)}</span>` : ""}<span class="v">${esc(c.v)}${arrow}</span></div>`;
    }).join("");

    // The goal is a ZONE, and its centre is not the scheduled target: it sits
    // mid-band (an asymmetric band would otherwise make the target hug its tight
    // edge) and drops again when no fan can run. Show the schedule alongside and
    // name whichever cause applies, rather than letting the numbers silently
    // disagree.
    const offset = vm.target != null && vm.zoneMid != null ? vm.zoneMid - vm.target : 0;
    const mid = (vm.bandHigh - vm.bandLow) / 2 || 0;
    const causes = [];
    if (Math.abs(mid) > 0.005) causes.push("mid-band");
    if (Math.abs(offset - mid) > 0.005) causes.push("no fan");
    const schedNote = vm.target == null ? ""
      : `<span class="band alt">sched ${f1(vm.target)}°${
        causes.length ? ` · ${sg2(offset)} ${causes.join(" + ")}` : ""}</span>`;

    const fresh = vm.fresh
      ? `<div class="freshness clk${vm.fresh.stale ? " stale" : ""}" data-action="more"
           data-entity="${esc(vm.fresh.entity)}">${esc(vm.fresh.text)}</div>` : "";

    return `
      <div class="hd-top">
        <div class="zone card-header">${esc(vm.title)}</div>
        <button class="toggle ${vm.enabled ? "on" : ""}" data-action="toggle"
                title="${vm.enabled ? "Controller on" : "Controller off"}" role="switch"
                aria-checked="${vm.enabled}"><span class="knob"></span></button>
      </div>
      <div class="hero">
        <span class="feel clk" style="color:${C.warm}" data-action="more" data-entity="${esc(vm.feelEntity)}"
          >${vm.comfort != null ? vm.comfort.toFixed(1) : "–"}<span class="deg">°</span></span>
        <span class="arrow">→</span>
        <span class="goal zone clk" style="color:${C.cool}" data-action="more" data-entity="${esc(vm.goalEntity)}"
          >${vm.zoneLo != null ? vm.zoneLo.toFixed(1) : "–"}<span class="dash">–</span>${
          vm.zoneHi != null ? vm.zoneHi.toFixed(1) : "–"}<span class="deg">°</span></span>
        <span class="band">band −${vm.bandLow.toFixed(1)} / +${vm.bandHigh.toFixed(1)}</span>
        ${schedNote}
      </div>
      <div class="statusline">
        <span class="pill clk" style="--c:${vm.modeColor}" data-action="more"
          data-entity="${esc(vm.feelEntity)}">${esc(vm.pillLabel)}</span>
        <span class="reason">${esc(vm.reason)}</span>
        ${vm.decision ? `<button class="whybtn" data-action="why"
           aria-expanded="${showWhy}">why ${showWhy ? "▴" : "▾"}</button>` : ""}
      </div>
      ${showWhy ? renderWhy(vm) : ""}
      ${fresh}
      <div class="chips">${chips}</div>`;
  }

  // -- why: the causal chain, in the order the controller walks it --------------
  //   outdoor → u_ff ·  error → u_fb ·  u_ff + u_fb = u_raw ·  clamp → u
  //   ·  quantise → setpoint + blower + fan
  // The reason line says what it decided; this says from what, so a decision can
  // be agreed or disagreed with without reading the source.
  function renderWhy(vm) {
    const d = vm.decision || {};
    if (fnum(d.y) == null) {
      return `<div class="why"><div class="wrow"><span class="wv">No decision detail yet — it appears on the next tick.</span></div></div>`;
    }
    const row = (k, v, mark = "", cls = "") =>
      `<div class="wrow"><span class="wk">${k}</span><span class="wv">${v}</span>${
        mark ? `<span class="wm ${cls}">${mark}</span>` : ""}</div>`;

    // Saturation and freezing explain most surprising behaviour, so they lead.
    const del = Array.isArray(d.deliverable) ? d.deliverable : [null, null];
    const flags = [];
    if (d.saturated) {
      // Pinned at a limit. Whether that is a problem depends entirely on whether
      // the room is still comfortable there, so say which — "at the floor" alone
      // reads as an alarm when most of the time it is simply the end of the range.
      const floor = d.at_limit !== "ceiling";
      const edge = floor ? "floor" : "ceiling";
      flags.push(d.in_zone
        ? [C.grey, `at the ${edge}`,
           `setpoint ${d.sp} is as ${floor ? "cold" : "warm"} as this unit goes — `
           + `comfortable, but there is no headroom left`]
        : [C.amber, `at the ${edge}`,
           `setpoint ${d.sp} is as ${floor ? "cold" : "warm"} as this unit goes, `
           + `and the room is outside the zone — it cannot keep up`]);
    }
    if (d.frozen) {
      flags.push([C.grey, "frozen",
        "integration paused — the guard has the room, or the AC is off"]);
    }
    if (d.cold_veto) {
      // The reading overruling the model. Worth its own flag: the demand shown
      // below is one the loop was held to, not one it chose.
      flags.push([C.teal, "cold veto",
        `the room reads below its zone, so nothing colder than the setpoint the `
        + `unit already holds (${d.sp_observed ?? d.sp}) may be commanded, `
        + `whatever the model expects`]);
    }
    const flagHtml = flags.length
      ? `<div class="wflags">${flags.map(([c, k, why]) =>
        `<span class="wflag" style="--c:${c}">${k} <span class="wfx">${esc(why)}</span></span>`).join("")}</div>`
      : "";

    const rows = [];
    // 1. the room: where it is, where the dead-time model says it settles.
    rows.push(row("room", `${f2(d.y)} now → settles ${f2(d.settled)} · ${sg3(d.slope)} °C/min`));

    // 2. the zone being held, and the error the loop actually sees. The zone's
    //    centre is not the scheduled target: it sits mid-band, because an
    //    asymmetric band makes the target hug its tight edge, and drops again when
    //    no fan can run. Two causes, so name whichever is actually in play.
    const offset = fnum(d.target) != null && vm.target != null ? d.target - vm.target : 0;
    const mid = (vm.bandHigh - vm.bandLow) / 2 || 0;
    const why = [];
    if (Math.abs(mid) > 0.005) why.push(`${sg2(mid)} mid-band`);
    if (Math.abs(offset - mid) > 0.005) why.push(`${sg2(offset - mid)} no fan`);
    rows.push(row("zone", `${f2(d.zone_lo)} – ${f2(d.zone_hi)}`
      + (why.length ? ` (${why.join(", ")})` : ""), `band ${f1(d.lo)}–${f1(d.hi)}`));
    // A zero error is the state a reader is most likely to misread as broken, so
    // say WHY it is zero. Two very different reasons: the room is comfortable, or
    // the room is out and the model says the answer is already on its way. The
    // second used to be shown as the first.
    const err = fnum(d.error);
    const settledIn = d.settled_in_zone ?? (err === 0);
    rows.push(err === 0
      ? row("error", settledIn && d.in_zone !== false
          ? "0 — the room is inside the zone"
          : "0 — out of zone, but the model says the answer is in flight",
        settledIn && d.in_zone !== false ? "✓ nothing to correct" : "waiting",
        settledIn && d.in_zone !== false ? "ok" : "warn")
      : row("error", `${sg2(d.error)} to the zone centre`,
        err == null ? "" : err > 0 ? "too cold" : "too warm",
        err == null ? "" : err > 0 ? "cool" : "warn"));

    rows.push(`<div class="wsep"></div>`);

    // 3. feedforward: the setpoint this weather calls for, before any error.
    rows.push(row("outdoor", fnum(d.outdoor) != null
      ? `${f1(d.outdoor)}° outside → ff ${f2(d.u_ff)}`
      : `no reading → ff ${f2(d.u_ff)} held`));

    // 4. feedback: the residual the feedforward missed, and the memory of it.
    rows.push(row("feedback", `fb ${sg2(d.u_fb)} · ∫ ${sg2(d.integral)} °C`,
      d.frozen ? "held" : "", d.frozen ? "warn" : ""));

    // 5. the sum, and what survives the clamp.
    rows.push(row("demand", `ff ${f2(d.u_ff)} ${sg2(d.u_fb)} = ${f2(d.u_raw)} raw`));
    rows.push(row("deliver", `clamp ${f1(del[0])} – ${f1(del[1])} → u ${f2(d.u)}`,
      d.saturated ? (d.in_zone ? "at the limit" : "✗ short") : "✓ fits",
      d.saturated ? (d.in_zone ? "" : "warn") : "ok"));

    // 6. quantisation: one integer setpoint, one blower level, and the fraction
    //    neither can express — which is exactly what the fan is for.
    const spNote = fnum(d.sp_observed) != null && d.sp_observed !== d.sp
      ? ` (unit at ${d.sp_observed})` : "";
    const blNote = vm.acBlower ? ` ${esc(vm.acBlower)}` : "";
    rows.push(row("output", `setpoint ${d.sp ?? "–"}${spNote} · blower ${d.blower ?? "–"}${blNote}`
      + ` · fan takes ${f2(d.trim)}°`));

    // 7. compressor pacing — the one thing that can hold a correct setpoint back.
    const left = fnum(d.sp_dwell_left);
    if (d.sp_blocked_by_dwell) {
      rows.push(row("pacing", `${f1(left)} min to the next setpoint step`, "✗ held", "warn"));
    } else if (left != null && left > 0) {
      rows.push(row("pacing", `${f1(left)} min of compressor dwell left`));
    } else {
      rows.push(row("pacing", "free to step", "✓", "ok"));
    }

    if (d.overridden) rows.push(row("guard", `overrode <code>${esc(d.overridden)}</code>`, "✗", "warn"));
    if (fnum(d.gain) != null) {
      rows.push(row("model", `K ${f2(d.gain)} · L ${f1(d.dead_time)} · τ ${f1(d.tau)}`
        + ` · Kc ${f2(d.kc)} · Ti ${f1(d.ti)} · blower ${f2(d.blower_gain)}`));
    }
    const arm = vm.branch ? `<div class="wid">arm <code>${esc(vm.branch)}</code></div>` : "";
    return `<div class="why">${flagHtml}${rows.join("")}${arm}</div>`;
  }

  // -- tuning: strategy segmented + steppers -----------------------------------
  function renderTune(vm) {
    const seg = STRATEGIES.map((s) =>
      `<button class="segbtn ${s === vm.strategy ? "sel" : ""}" data-action="strategy"
         data-val="${esc(s)}">${esc(s)}</button>`).join("");

    // `signed` renders an offset below target as a negative value with the buttons
    // matching temperature direction: "+" warms the floor (toward target → smaller
    // magnitude), "–" cools it (further below → larger).
    const stepper = (k) => {
      // A cap of 0 % is not a quantity, it is a state: the actuator cannot run.
      const off = k.value === 0 && k.unit === "%";
      const disp = k.value == null ? "–" : off ? "off"
        : (k.signed ? `−${Math.abs(k.value).toFixed(2)}` : k.value) + k.unit;
      const lDir = k.signed ? 1 : -1;   // left "–"
      const rDir = k.signed ? -1 : 1;   // right "+"
      return `<div class="stepper">
          <span class="sk">${esc(k.label)}</span>
          <button class="sb" data-action="num" data-id="${esc(k.id)}" data-dir="${lDir}">–</button>
          <span class="sv">${esc(disp)}</span>
          <button class="sb" data-action="num" data-id="${esc(k.id)}" data-dir="${rDir}">+</button>
        </div>`;
    };

    const fanRow = vm.fanAssist ? `
      <div class="ctl">
        <span class="sk">fan assist</span>
        <button class="toggle sm ${vm.fanAssist.on ? "on" : ""}" data-action="fan_assist" role="switch"
                aria-checked="${vm.fanAssist.on}"
                title="${vm.fanAssist.on ? "Fan enabled" : "Fan disabled"}"><span class="knob"></span></button>
      </div>` : "";

    // Only the everyday knobs live on the card; the hard safety limits stay on the
    // device page.
    return `
      <div class="seg">${seg}</div>
      ${fanRow}
      <div class="steppers">${vm.knobs.map(stepper).join("")}</div>`;
  }

  // -- power strip: what the meter did, under what was commanded ---------------
  // Evidence for a human, never an input. Nothing in the loop reads power; the
  // strip exists so a command can be checked against the meter's answer.
  function renderPower(vm) {
    const h = vm.powerHist;
    if (!h || !h.pts.length) return "";
    const W = 320, H = 78, padL = 26, padR = 8, padT = 20, padB = 12;
    const iw = W - padL - padR, ih = H - padT - padB;
    const span = Math.max(1, h.t1 - h.t0);
    const X = (ms) => padL + clamp((ms - h.t0) / span, 0, 1) * iw;
    const top = Math.max(500, h.max * 1.12);
    const Y = (w) => padT + (1 - clamp(w, 0, top) / top) * ih;

    let line = "", area = `M${X(h.pts[0].ms).toFixed(1)} ${padT + ih} `;
    h.pts.forEach((q, i) => {
      const x = X(q.ms).toFixed(1), y = Y(q.w).toFixed(1);
      line += (i ? "L" : "M") + x + " " + y + " ";
      area += "L" + x + " " + y + " ";
    });
    area += `L${X(h.pts[h.pts.length - 1].ms).toFixed(1)} ${padT + ih} Z`;

    // Each setpoint change gets a tick where it landed, labelled with the value it
    // moved to. Labels alternate between two rows ABOVE the plot rather than
    // competing for one: a tick without its value says a change happened but not
    // what to, which is the half of the information worth having. Only a genuine
    // collision within the same row is dropped.
    const rowY = [padT - 12, padT - 3];
    const lastAt = [-99, -99];
    const ticks = h.sp.map((s) => {
      const x = X(s.ms);
      let lab = "";
      const r = lastAt[0] <= lastAt[1] ? 0 : 1;
      if (x - lastAt[r] > 16) {
        lastAt[r] = x;
        lab = `<text x="${(x + 2).toFixed(1)}" y="${rowY[r]}" class="axl sp">${esc(s.v)}</text>`;
      }
      return `<line x1="${x.toFixed(1)}" y1="${padT}" x2="${x.toFixed(1)}"
        y2="${padT + ih}" class="sptick"/>${lab}`;
    }).join("");

    // Saturation is only known for the live tick — there is no history of it — so
    // it is marked at the right-hand edge and claimed for nothing earlier.
    const sat = vm.decision && vm.decision.saturated;
    const satMark = sat ? `<rect x="${(padL + iw - 5).toFixed(1)}" y="${padT}" width="5"
      height="${ih}" fill="${C.amber}" opacity="0.3"/>` : "";

    const hrs = Math.max(1, Math.round(span / 3600000));
    return `<svg viewBox="0 0 ${W} ${H}" class="pwrsvg">
        <line x1="${padL}" y1="${padT}" x2="${W - padR}" y2="${padT}" class="grid faint"/>
        <line x1="${padL}" y1="${padT + ih}" x2="${W - padR}" y2="${padT + ih}" class="grid"/>
        <text x="${padL - 5}" y="${padT + 3}" class="axl" text-anchor="end">${(top / 1000).toFixed(1)}</text>
        <text x="${padL - 5}" y="${padT + ih}" class="axl" text-anchor="end">0</text>
        <path d="${area}" fill="${C.grey}" fill-opacity="0.2"/>
        <path d="${line.trim()}" fill="none" stroke="${C.grey}" stroke-width="1.2"
          stroke-linejoin="round"/>
        ${ticks}${satMark}
        <text x="${padL}" y="${H - 2}" class="axl">−${hrs}h</text>
        <text x="${W - padR}" y="${H - 2}" class="axl" text-anchor="end">now</text>
      </svg>
      <div class="hint">Meter, last ${hrs} h · kW
        <span class="lg"><i class="sw" style="background:${C.cool};width:2px;height:9px"></i>setpoint change</span>
        ${sat ? `<span class="lg"><i class="sw" style="background:${C.amber};width:5px;height:9px"></i>saturated now</span>` : ""}</div>`;
  }

  // ===========================================================================
  // Schedule editor — the only stateful component
  // ===========================================================================
  // Owns the draft, the selection and its own listeners. A full re-render happens
  // only when the data behind the chart changes; every interaction (tap, drag,
  // hover, stepper) patches the attributes that moved, so the node under the
  // pointer capture survives the whole gesture.
  class ScheduleEditor {
    constructor(root, { onSave, onDirty }) {
      this.root = root;
      this.onSave = onSave;
      this.onDirty = onDirty;
      this.draft = null;        // local edits (null = follow the entity)
      this.sel = null;          // selected slot (tap-to-select + stepper editing)
      this.dragging = false;
      this.active = null;       // {i, t} being dragged/hovered — drives the badge
      this.hoverI = null;
      this.lastI = null;        // last painted slot, for fast-drag interpolation
      this.pressX = 0; this.pressY = 0;
      this.vm = null;
      this.sig = null;
      this.el = null;
      root.addEventListener("click", (e) => this._onClick(e));
    }

    get data() { return this.draft || (this.vm ? this.vm.schedule : new Array(SLOTS).fill(26)); }

    update(vm) {
      // Schedules are per-strategy: when the strategy changes, drop any draft so
      // the new strategy's curve renders instead of the old one's edits.
      if (this.vm && vm.strategy !== this.vm.strategy) {
        this.draft = null;
        this.dragging = false;
        this.active = null;
        this.onDirty(false);
      }
      this.vm = vm;
      if (this.dragging) return;              // never clobber a live gesture
      if (this.sig !== null && this.sig === this._sig()) return;
      this._renderFull();
    }

    revert() {
      this.draft = null;
      this.dragging = false;
      this.active = null;
      this.onDirty(false);
      if (this.vm) this._renderFull();
    }

    save() {
      if (!this.draft) return;
      const out = this.draft.map((v) => +(+v).toFixed(1));
      this.draft = null;
      this.onDirty(false);
      this.onSave(out);
      this._renderFull();
    }

    // -- geometry -----------------------------------------------------------
    _geo() {
      const W = 320, H = 160, padL = 26, padR = 8, padT = 10, padB = 20;
      const iw = W - padL - padR, ih = H - padT - padB;
      const tmin = this.vm.tmin, tmax = this.vm.tmax;
      return {
        W, H, padL, padR, padT, padB, iw, ih, tmin, tmax,
        X: (i) => padL + (i / (SLOTS - 1)) * iw,
        Y: (t) => padT + ((tmax - clamp(t, tmin, tmax)) / (tmax - tmin)) * ih,
        XA: (hf) => padL + (clamp(hf, 0, 24) / 24) * iw,
      };
    }

    // Everything the chart is drawn FROM. A patch keeps the DOM in step with it,
    // so comparing it is what tells a real data change from an echo of our own.
    _sig() {
      const v = this.vm;
      return [this.data.join(","), this.sel, this.active ? `${this.active.i}:${this.active.t}` : "",
        v.comfort, v.histStamp, v.tmin, v.tmax].join("|");
    }

    _paths(g) {
      const d = this.data;
      let line = "", area = `M${g.X(0)} ${g.padT + g.ih} `, dots = [];
      d.forEach((t, i) => {
        const x = g.X(i).toFixed(1), y = g.Y(t).toFixed(1);
        line += (i ? "L" : "M") + x + " " + y + " ";
        area += "L" + x + " " + y + " ";
        dots.push(y);
      });
      area += `L${g.X(SLOTS - 1)} ${g.padT + g.ih} Z`;
      return { line: line.trim(), area, dots };
    }

    _badgeAt(i, t, g) {
      const bx = g.X(i), by = g.Y(t), bw = 70;
      return {
        bx, by, bw,
        tx: clamp(bx, g.padL + bw / 2, g.W - g.padR - bw / 2),
        ty: clamp(by - 12, g.padT + 15, g.padT + g.ih),
        label: `${String(Math.floor(i / 2)).padStart(2, "0")}:${i % 2 ? "30" : "00"} · ${(+t).toFixed(1)}°`,
      };
    }

    // -- full render --------------------------------------------------------
    _renderFull() {
      const g = this._geo();
      const now = new Date();
      const nowFrac = (now.getHours() * 60 + now.getMinutes()) / 1440;
      const nowX = g.padL + nowFrac * g.iw;
      const nowHf = nowFrac * 24;
      if (this.sel == null) {   // open on the current time
        this.sel = clamp(Math.floor((now.getHours() * 60 + now.getMinutes()) / 30), 0, SLOTS - 1);
      }

      const p = this._paths(g);
      const grid = [0, 6, 12, 18, 24].map((h) => {
        const gx = g.padL + (h / 24) * g.iw;
        return `<line x1="${gx}" y1="${g.padT}" x2="${gx}" y2="${g.padT + g.ih}" class="grid"/>
                <text x="${gx}" y="${g.H - 5}" class="axl" text-anchor="middle">${h}</text>`;
      }).join("");

      // Y ticks that fit the (configurable) range: whole degrees inside it, or
      // min/mid/max if the range is narrower than ~2°.
      const ticks = [];
      for (let t = Math.ceil(g.tmin); t <= Math.floor(g.tmax); t++) ticks.push(t);
      if (ticks.length < 2) {
        ticks.length = 0;
        ticks.push(g.tmin, Math.round((g.tmin + g.tmax) * 5) / 10, g.tmax);
      }
      const yticks = ticks.map((t) =>
        `<text x="${g.padL - 5}" y="${(g.Y(t) + 3).toFixed(1)}" class="axl" text-anchor="end"
           >${Number.isInteger(t) ? t : t.toFixed(1)}</text>
         <line x1="${g.padL}" y1="${g.Y(t).toFixed(1)}" x2="${g.W - g.padR}" y2="${g.Y(t).toFixed(1)}"
           class="grid faint"/>`).join("");

      const dots = p.dots.map((y, i) =>
        `<circle cx="${g.X(i).toFixed(1)}" cy="${y}" r="2" class="dot"/>`).join("");

      const nowDot = this.vm.comfort != null
        ? `<circle cx="${nowX.toFixed(1)}" cy="${g.Y(this.vm.comfort).toFixed(1)}" r="4"
             fill="${C.warm}" stroke="var(--card-background-color)" stroke-width="1.5"/>` : "";

      // Actual comfort on the same axes as the target, mapped by hour-of-day.
      // Today = SOLID (00:00 → now). Yesterday = a THIN line drawn only over the
      // part of the day that has not happened yet, so the two read as one
      // continuous rolling trace with no overlap.
      const act = this.vm.actual || { today: [], yesterday: [] };
      const trace = (pts) => {
        if (!pts || !pts.length) return "";
        let d = "";
        pts.forEach((q, i) => { d += (i ? "L" : "M") + g.XA(q.hf).toFixed(1) + " " + g.Y(q.t).toFixed(1) + " "; });
        return d.trim();
      };
      const yPath = trace((act.yesterday || []).filter((q) => q.hf >= nowHf));
      const tPath = trace(act.today);
      const actual =
        (yPath ? `<path d="${yPath}" fill="none" stroke="${C.warm}" stroke-width="1"
           stroke-opacity="0.5" stroke-linejoin="round" stroke-linecap="round"/>` : "")
        + (tPath ? `<path d="${tPath}" fill="none" stroke="${C.warm}" stroke-width="1.5"
           stroke-opacity="0.9" stroke-linejoin="round" stroke-linecap="round"/>` : "");

      // Outdoor temperature, today so far. It answers "why did the setpoint go
      // there", so it belongs on this chart — but it runs 25–33° against a 25–27°
      // window and would leave the frame, so it gets its OWN vertical scale,
      // stretched over the plot and named with its range in the legend. No shared
      // axis means no reading a crossing as if the two lines met.
      const od = this.vm.outdoorTrace || [];
      let odPath = "", odRange = "";
      if (od.length) {
        let olo = Math.min(...od.map((q) => q.t)), ohi = Math.max(...od.map((q) => q.t));
        const pad = Math.max(0.5, (ohi - olo) * 0.12);
        olo -= pad; ohi += pad;
        const OY = (t) => g.padT + ((ohi - clamp(t, olo, ohi)) / (ohi - olo)) * g.ih;
        od.forEach((q, i) => {
          odPath += (i ? "L" : "M") + g.XA(q.hf).toFixed(1) + " " + OY(q.t).toFixed(1) + " ";
        });
        odPath = `<path d="${odPath.trim()}" fill="none" stroke="${C.out}" stroke-width="1.2"
          stroke-dasharray="4 3" stroke-opacity="0.75" stroke-linejoin="round"/>`;
        odRange = `${Math.round(olo)}–${Math.round(ohi)}°`;
      }

      this.root.innerHTML = `
        <svg id="cz-svg" viewBox="0 0 ${g.W} ${g.H}" class="schedsvg">
          <defs>
            <linearGradient id="czgrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="${C.warm}" stop-opacity="0.28"/>
              <stop offset="100%" stop-color="${C.cool}" stop-opacity="0.10"/>
            </linearGradient>
          </defs>
          ${yticks}${grid}
          <path id="cz-area" d="${p.area}" fill="url(#czgrad)"/>
          ${odPath}
          ${actual}
          <path id="cz-line" d="${p.line}" fill="none" stroke="var(--primary-color)"
            stroke-width="2" stroke-linejoin="round"/>
          <g id="cz-dots">${dots}</g>
          <line x1="${nowX.toFixed(1)}" y1="${g.padT}" x2="${nowX.toFixed(1)}"
            y2="${g.padT + g.ih}" class="now"/>
          ${nowDot}
          <circle id="cz-sel" r="6" fill="var(--card-background-color)"
            stroke="var(--primary-color)" stroke-width="2.5"/>
          <g id="cz-badge" text-anchor="middle" style="pointer-events:none">
            <rect width="70" height="16" rx="4" fill="var(--primary-color)"/>
            <text font-size="10.5" font-weight="700" fill="var(--text-primary-color, #fff)"></text>
            <circle r="4" fill="var(--primary-color)" stroke="var(--card-background-color)" stroke-width="1.5"/>
          </g>
        </svg>
        <div class="sched-edit">
          <div class="se-grp">
            <button class="se-btn" data-action="sel-move" data-d="-1" aria-label="earlier">‹</button>
            <span class="se-lab" id="cz-selt"></span>
            <button class="se-btn" data-action="sel-move" data-d="1" aria-label="later">›</button>
          </div>
          <div class="se-grp">
            <button class="se-btn" data-action="sel-adj" data-d="-1" aria-label="cooler">−</button>
            <span class="se-lab val" id="cz-selv"></span>
            <button class="se-btn" data-action="sel-adj" data-d="1" aria-label="warmer">+</button>
          </div>
        </div>
        <div class="hint">Tap a point to select · drag to sketch · nudge with the buttons
          <span class="lg"><i class="sw" style="background:var(--primary-color)"></i>target</span>
          <span class="lg"><i class="sw" style="background:${C.warm}"></i>actual today</span>
          <span class="lg"><i class="sw" style="background:${C.warm};opacity:.5;height:1px"></i>yesterday (ahead)</span>
          ${odRange ? `<span class="lg"><i class="sw" style="background:${C.out}"></i
            >outdoor ${odRange} (own scale)</span>` : ""}</div>`;

      const q = (id) => this.root.querySelector(id);
      const badge = q("#cz-badge");
      this.el = {
        svg: q("#cz-svg"), line: q("#cz-line"), area: q("#cz-area"),
        dots: Array.from(q("#cz-dots").children),
        sel: q("#cz-sel"),
        badge, bRect: badge.querySelector("rect"), bText: badge.querySelector("text"),
        bDot: badge.querySelector("circle"),
        selT: q("#cz-selt"), selV: q("#cz-selv"),
      };
      this._bind(this.el.svg);
      this._patch();
    }

    // -- patch: the only thing an interaction touches -----------------------
    _patch() {
      if (!this.el) return;
      const g = this._geo();
      const data = this.data;
      const p = this._paths(g);
      this.el.line.setAttribute("d", p.line);
      this.el.area.setAttribute("d", p.area);
      for (let i = 0; i < this.el.dots.length; i++) this.el.dots[i].setAttribute("cy", p.dots[i]);

      if (this.sel != null && data[this.sel] != null) {
        this.el.sel.setAttribute("cx", g.X(this.sel).toFixed(1));
        this.el.sel.setAttribute("cy", g.Y(data[this.sel]).toFixed(1));
        this.el.sel.removeAttribute("display");
      } else {
        this.el.sel.setAttribute("display", "none");
      }

      // Badge "HH:MM · 26.4°" — the dragged/hovered point if there is one, else
      // the selected point, so the exact target is always readable.
      const bi = this.active ? this.active.i : this.sel;
      const bt = this.active ? this.active.t : (this.sel != null ? data[this.sel] : null);
      if (bi != null && bt != null) {
        const b = this._badgeAt(bi, bt, g);
        this.el.bRect.setAttribute("x", (b.tx - b.bw / 2).toFixed(1));
        this.el.bRect.setAttribute("y", (b.ty - 13).toFixed(1));
        this.el.bText.setAttribute("x", b.tx.toFixed(1));
        this.el.bText.setAttribute("y", (b.ty - 1).toFixed(1));
        this.el.bText.textContent = b.label;
        this.el.bDot.setAttribute("cx", b.bx.toFixed(1));
        this.el.bDot.setAttribute("cy", b.by.toFixed(1));
        this.el.badge.removeAttribute("display");
      } else {
        this.el.badge.setAttribute("display", "none");
      }

      const s = this.sel ?? 0;
      this.el.selT.textContent = `${String(Math.floor(s / 2)).padStart(2, "0")}:${s % 2 ? "30" : "00"}`;
      this.el.selV.textContent = `${(+(data[s] ?? 26)).toFixed(1)}°`;
      this.sig = this._sig();
    }

    // -- pointer ------------------------------------------------------------
    _bind(svg) {
      const MOVE_THRESH = 5;   // svg units of movement before a press becomes a sketch
      const toSvg = (ev) => {
        const r = svg.getBoundingClientRect();
        const g = this._geo();
        return { x: ((ev.clientX - r.left) / r.width) * g.W, y: ((ev.clientY - r.top) / r.height) * g.H, g };
      };
      const toVal = (ev) => {
        const { x, y, g } = toSvg(ev);
        return {
          i: clamp(Math.round(((x - g.padL) / g.iw) * (SLOTS - 1)), 0, SLOTS - 1),
          t: clamp(snap(g.tmax - ((y - g.padT) / g.ih) * (g.tmax - g.tmin), 0.1), g.tmin, g.tmax),
        };
      };

      svg.addEventListener("pointerdown", (ev) => {
        ev.preventDefault();
        const { x, y } = toSvg(ev);
        this.sel = toVal(ev).i;        // TAP = select (non-destructive)
        this.dragging = false;
        this.lastI = null;
        this.hoverI = null;
        this.pressX = x; this.pressY = y;
        // The node keeps the capture for the whole gesture, which is why nothing
        // below here re-renders the svg. A capture can still be refused (a
        // detached node, a synthetic event) and that must not cost the tap.
        try { svg.setPointerCapture(ev.pointerId); } catch (err) { /* tap still works */ }
        this._patch();                 // show the selection without replacing the node
      });

      svg.addEventListener("pointermove", (ev) => {
        if (ev.buttons) {              // pressed → sketch once it moves past threshold
          if (!this.dragging) {
            const { x, y } = toSvg(ev);
            if (Math.abs(x - this.pressX) < MOVE_THRESH && Math.abs(y - this.pressY) < MOVE_THRESH) return;
            this.dragging = true;
          }
          const { i, t } = toVal(ev);
          const d = this.draft || this.data.slice();
          if (this.lastI != null && Math.abs(i - this.lastI) > 1) {
            // interpolate across a fast drag, so no slot is skipped
            const a = Math.min(i, this.lastI), b = Math.max(i, this.lastI);
            const va = d[this.lastI];
            for (let k = a; k <= b; k++) d[k] = va + (t - va) * ((k - this.lastI) / (i - this.lastI));
          } else {
            d[i] = t;
          }
          this.lastI = i;
          this.draft = d;
          this.sel = i;                // selection follows the sketch
          this.active = { i, t };
          this.onDirty(true);
          this._patch();
          return;
        }
        // Hover (mouse only, not pressed): read that point's value, destructively
        // of nothing.
        const { i } = toVal(ev);
        if (i === this.hoverI) return;
        this.hoverI = i;
        this.active = { i, t: this.data[i] };
        this._patch();
      });

      const end = (ev) => {
        if (ev && ev.pointerId != null && svg.hasPointerCapture(ev.pointerId)) {
          svg.releasePointerCapture(ev.pointerId);
        }
        this.dragging = false;
        this.lastI = null;
        this.active = null;
        this.hoverI = null;
        this._patch();                 // keep the selection; clear the transient badge
      };
      svg.addEventListener("pointerup", end);
      svg.addEventListener("pointercancel", end);
      svg.addEventListener("pointerleave", () => {
        if (this.dragging || this.active == null) return;
        this.hoverI = null;
        this.active = null;
        this._patch();
      });
    }

    _onClick(e) {
      const el = e.target.closest("[data-action]");
      if (!el || !this.vm) return;
      if (el.dataset.action === "sel-move") {
        this.sel = clamp((this.sel ?? 0) + +el.dataset.d, 0, SLOTS - 1);
        this._patch();
      } else if (el.dataset.action === "sel-adj") {
        if (this.sel == null) return;
        const d = this.draft || this.data.slice();
        // Edits use the full room range, not the chart window: a point outside the
        // window has to stay reachable, or it can never be brought back inside.
        d[this.sel] = clamp(snap((+d[this.sel] || 26) + +el.dataset.d * 0.1, 0.1), EDIT_MIN, EDIT_MAX);
        this.draft = d;
        this.onDirty(true);
        this._patch();
      }
    }
  }

  // ===========================================================================
  // The card
  // ===========================================================================
  class ComfortZoneCard extends HTMLElement {
    setConfig(config) {
      if (!config || !config.zone) {
        throw new Error("comfort-zone-card: set `zone:` to the zone's status sensor entity_id");
      }
      this._config = config;
      // Y-axis range for the daily-target chart. Tight by default (25–27) so the
      // curve fills the height and 0.1° moves are visible; override per card with
      // `curve_min:` / `curve_max:`.
      let tmin = fnum(config.curve_min), tmax = fnum(config.curve_max);
      if (tmin == null) tmin = DEF_TMIN;
      if (tmax == null) tmax = DEF_TMAX;
      if (!(tmax - tmin >= 1)) { tmin = DEF_TMIN; tmax = DEF_TMAX; }  // sane guard
      this._tmin = clamp(tmin, EDIT_MIN, EDIT_MAX - 1);
      this._tmax = clamp(tmax, this._tmin + 1, EDIT_MAX);
      this._why = false;             // the why panel, and nothing else
      this._entCache = {};
      this._sig = {};
      this._actual = null;           // cached actual-comfort trace [{hf, t}]
      this._outdoorTrace = null;     // today's outdoor temperature [{hf, t}]
      this._powerHist = null;        // {pts, sp, max, t0, t1} for the power strip
      this._histStamp = 0;
      this._histAt = 0;
      this._histKey = "";
      if (!this.shadowRoot) this.attachShadow({ mode: "open" });
      this._buildShell();
    }

    set hass(hass) {
      this._hass = hass;
      this._update();
    }

    // Public so a host (the preview page, a dashboard action) can open the panel
    // without reaching into private state.
    get showWhy() { return this._why; }
    set showWhy(v) {
      this._why = !!v;
      this._update();
    }

    getCardSize() { return 10; }

    _buildShell() {
      this.shadowRoot.innerHTML = `
        <style>${STYLE}</style>
        <ha-card>
          <div class="cz-missing" id="cz-missing" hidden></div>
          <div id="cz-body">
            <section class="hd" id="hd"></section>
            <section class="sec">
              <div class="eyebrow"><span>Daily target</span>
                <span class="tools">
                  <button class="btn ghost" data-action="revert" id="btn-revert" disabled>Revert</button>
                  <button class="btn" data-action="save" id="btn-save" disabled>Save</button>
                </span>
              </div>
              <div id="sched"></div>
              <div id="pwr" class="pwr"></div>
            </section>
            <section class="sec">
              <div class="eyebrow"><span>Tuning</span></div>
              <div id="tune"></div>
            </section>
          </div>
        </ha-card>`;

      this._editor = new ScheduleEditor(this.shadowRoot.getElementById("sched"), {
        onSave: (schedule) => this._hass.callService("comfort_zone", "set_schedule", {
          name: this._vm.zoneName, schedule,
        }),
        onDirty: (dirty) => {
          const save = this.shadowRoot.getElementById("btn-save");
          const rev = this.shadowRoot.getElementById("btn-revert");
          if (save) save.disabled = !dirty;
          if (rev) rev.disabled = !dirty;
        },
      });
      // event delegation — survives section re-renders
      this.shadowRoot.addEventListener("click", (e) => this._onClick(e));
    }

    // -- update loop ---------------------------------------------------------
    _update() {
      if (!this._hass || !this.shadowRoot) return;
      const vm = viewModel(this._hass, this._config, this._entCache);
      const miss = this.shadowRoot.getElementById("cz-missing");
      const body = this.shadowRoot.getElementById("cz-body");
      if (!vm.ok) {
        miss.hidden = false;
        body.hidden = true;
        miss.textContent = vm.missing;
        return;
      }
      miss.hidden = true;
      body.hidden = false;
      this._vm = vm;

      // One innerHTML write per island, and only when that island's markup would
      // differ: an unrelated HA state change no longer rebuilds the DOM under the
      // pointer, so hover, scroll and focus survive it.
      const full = Object.assign({
        tmin: this._tmin, tmax: this._tmax, histStamp: this._histStamp,
        actual: this._actual, outdoorTrace: this._outdoorTrace, powerHist: this._powerHist,
      }, vm);
      this._write("hd", renderHeader(vm, this._why));
      this._write("tune", renderTune(vm));
      this._write("pwr", renderPower(full));
      this._editor.update(full);
      this._maybeFetchHistory(vm);
    }

    _write(id, html) {
      if (this._sig[id] === html) return;
      this._sig[id] = html;
      this.shadowRoot.getElementById(id).innerHTML = html;
    }

    // -- history: the three traces the card draws from the recorder ------------
    // Two windows, because they are two different pictures: a 48 h one for the
    // daily chart (comfort bucketed by calendar day, plus today's outdoor) and a
    // 6 h one for the power strip. Splitting on the window, not on the payload,
    // keeps 6 h of a fast-sampling power meter out of the 48 h request. Both are
    // throttled to ≤ once / 3 min; everything else redraws from cache.
    _maybeFetchHistory(vm) {
      if (!this._hass.callWS) return;
      const nowMs = Date.now();
      const key = [vm.historyEntity, vm.weatherEntity, vm.powerEntity, vm.acEntity].join("|");
      if (this._histKey === key && nowMs - this._histAt < 180000) return;
      this._histKey = key;
      this._histAt = nowMs;   // set first so overlapping updates don't refetch
      const ask = (hours, ids) => this._hass.callWS({
        type: "history/history_during_period",
        start_time: new Date(nowMs - hours * 3600 * 1000).toISOString(),
        end_time: new Date(nowMs).toISOString(),
        entity_ids: ids,
        // The weather entity keeps its temperature in an ATTRIBUTE, and so does the
        // climate entity's setpoint, so attributes have to come along. HA repeats
        // them only when they change, which is what makes one call affordable.
        no_attributes: false,
      });
      // A recorder row carries `a` only when the attributes changed, so a reader of
      // one attribute has to carry the last value forward itself.
      const attrs = (arr, key) => {
        const out = [];
        let last = null;
        for (const p of arr || []) {
          const a = p.a || p.attributes;
          if (a && a[key] != null) last = fnum(a[key]);
          const lu = p.lu != null ? p.lu : p.last_updated;
          if (last == null || lu == null) continue;
          out.push({ ms: lu * 1000, v: last });
        }
        return out.sort((x, y) => x.ms - y.ms);
      };

      const mid = new Date(); mid.setHours(0, 0, 0, 0);
      const midToday = mid.getTime();
      const midYest = midToday - 24 * 3600 * 1000;
      const hourFrac = (ms) => {
        const d = new Date(ms);
        return d.getHours() + d.getMinutes() / 60 + d.getSeconds() / 3600;
      };

      // Prefer the backend's real SOURCE comfort sensor (e.g. 婴儿床 舒适温度) — it
      // has history from before this integration was installed.
      const long = [vm.historyEntity, vm.weatherEntity].filter(Boolean);
      if (long.length) ask(48, long).then((res) => {
        const today = [], yesterday = [];
        for (const p of (res && res[vm.historyEntity]) || []) {
          const v = fnum(p.s);
          const lu = p.lu != null ? p.lu : p.last_updated;
          if (v == null || lu == null) continue;
          const ms = lu * 1000;
          const pt = { ms, hf: hourFrac(ms), t: v };
          if (ms >= midToday) today.push(pt);
          else if (ms >= midYest) yesterday.push(pt);
        }
        today.sort((a, b) => a.ms - b.ms);
        yesterday.sort((a, b) => a.ms - b.ms);
        this._actual = { today, yesterday };
        this._outdoorTrace = attrs(res && res[vm.weatherEntity], "temperature")
          .filter((q) => q.ms >= midToday).map((q) => ({ hf: hourFrac(q.ms), t: q.v }));
        this._histStamp++;
        this._update();
      }).catch(() => { /* no history → just skip the overlays */ });

      const short = [vm.powerEntity, vm.acEntity].filter(Boolean);
      if (short.length) ask(POWER_HOURS, short).then((res) => {
        const raw = [];
        for (const p of (res && res[vm.powerEntity]) || []) {
          const w = fnum(p.s);
          const lu = p.lu != null ? p.lu : p.last_updated;
          if (w != null && lu != null) raw.push({ ms: lu * 1000, w });
        }
        raw.sort((a, b) => a.ms - b.ms);
        // The meter can sample every few seconds; the strip is 300 px wide. Bucket
        // to the PEAK of each minute-ish bucket, so a short spike still shows.
        const t0 = nowMs - POWER_HOURS * 3600 * 1000, bucket = 60 * 1000;
        const pts = [];
        let max = 0;
        for (const q of raw) {
          if (q.ms < t0) continue;   // the recorder may return more than we asked for
          const b = Math.floor((q.ms - t0) / bucket);
          const prev = pts[pts.length - 1];
          if (prev && prev.b === b) prev.w = Math.max(prev.w, q.w);
          else pts.push({ b, ms: t0 + b * bucket, w: q.w });
          if (q.w > max) max = q.w;
        }
        // Setpoint changes are the commands the strip is evidence against.
        const sp = [];
        for (const q of attrs(res && res[vm.acEntity], "temperature")) {
          if (!sp.length || sp[sp.length - 1].v !== q.v) sp.push({ ms: q.ms, v: q.v });
        }
        this._powerHist = pts.length
          ? { pts, sp: sp.filter((q) => q.ms >= t0), max, t0, t1: nowMs } : null;
        this._histStamp++;
        this._update();
      }).catch(() => { /* no history → no strip */ });
    }

    // -- interactions --------------------------------------------------------
    _onClick(e) {
      const el = e.target.closest("[data-action]");
      if (!el || !this._hass || !this._vm) return;
      const hass = this._hass;
      const ent = this._vm.ent;
      switch (el.dataset.action) {
        case "more": {
          const entityId = el.dataset.entity;
          if (entityId) {
            this.dispatchEvent(new CustomEvent("hass-more-info", {
              detail: { entityId }, bubbles: true, composed: true,
            }));
          }
          break;
        }
        case "why":
          this.showWhy = !this._why;
          break;
        case "toggle":
          hass.callService("switch", hass.states[ent.enable]?.state === "on" ? "turn_off" : "turn_on",
            { entity_id: ent.enable });
          break;
        case "fan_assist":
          hass.callService("switch", hass.states[ent.fanAssist]?.state === "on" ? "turn_off" : "turn_on",
            { entity_id: ent.fanAssist });
          break;
        case "strategy":
          hass.callService("select", "select_option",
            { entity_id: ent.strategy, option: el.dataset.val });
          break;
        case "num": {
          const id = el.dataset.id;
          const st = hass.states[id];
          if (!st) return;
          const step = fnum(st.attributes.step) || 0.5;
          const v = clamp((fnum(st.state) || 0) + +el.dataset.dir * step,
            fnum(st.attributes.min) ?? -Infinity, fnum(st.attributes.max) ?? Infinity);
          hass.callService("number", "set_value", { entity_id: id, value: +v.toFixed(2) });
          break;
        }
        case "save": this._editor.save(); break;
        case "revert": this._editor.revert(); break;
      }
    }
  }

  // ---------------------------------------------------------------------------
  const STYLE = `
    :host { --cz-gap: 14px; }
    ha-card { padding: 16px; display: block; }
    .cz-missing { padding: 24px; text-align: center; color: var(--secondary-text-color); }
    section { margin-bottom: var(--cz-gap); }
    section:last-child { margin-bottom: 0; }
    .eyebrow { display:flex; align-items:center; justify-content:space-between;
      font-size: 11px; letter-spacing: .12em; text-transform: uppercase;
      color: var(--secondary-text-color); margin: 6px 0 8px;
      border-top: 1px solid var(--divider-color); padding-top: 10px; }
    .tools { display:flex; gap:6px; }

    /* header */
    .hd-top { display:flex; align-items:center; justify-content:space-between; }
    .zone { font-size: 15px; font-weight: 600; color: var(--primary-text-color); letter-spacing:.01em; }
    .hero { display:flex; align-items:baseline; flex-wrap:wrap; gap:8px; margin:6px 0 2px;
      font-variant-numeric: tabular-nums; }
    .feel, .goal { font-size: 40px; font-weight: 300; line-height:1; }
    /* the goal is a range, so it needs room the single number never did */
    .goal.zone { font-size: 30px; }
    .dash { font-size: 20px; padding: 0 3px; color: var(--secondary-text-color); }
    .deg { font-size: 20px; font-weight: 400; }
    .arrow { font-size: 22px; color: var(--secondary-text-color); }
    .band { font-size: 13px; color: var(--secondary-text-color); align-self:center; }
    .band.alt { font-size:11.5px; opacity:.85; }
    /* pill + reason share their own line below the numbers, so the pill's text
       length never reflows the temperature row */
    .statusline { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin:2px 0 8px; }
    .pill { flex:none; align-self:center; font-size:12px; font-weight:600; padding:3px 10px;
      border-radius:999px; color:var(--c); background: color-mix(in srgb, var(--c) 16%, transparent);
      border:1px solid color-mix(in srgb, var(--c) 40%, transparent); white-space:nowrap; }
    .reason { flex:1; min-width:0; font-size: 12.5px; color: var(--secondary-text-color); }
    .whybtn { flex:none; font-size:11px; padding:2px 8px; border-radius:999px; cursor:pointer;
      background:var(--secondary-background-color); color:var(--secondary-text-color);
      border:1px solid var(--divider-color); letter-spacing:.04em; }
    .whybtn:hover { color:var(--primary-text-color); }

    /* why panel — the chain from the weather to the three actuators */
    .why { margin:0 0 8px; padding:8px 10px; border-radius:8px;
      background:var(--secondary-background-color); display:flex; flex-direction:column; gap:3px;
      font-variant-numeric:tabular-nums; }
    .wrow { display:grid; grid-template-columns: 62px 1fr max-content; gap:2px 8px; align-items:baseline; }
    .wk { font-size:9.5px; text-transform:uppercase; letter-spacing:.06em; line-height:1.5;
      color:var(--secondary-text-color); white-space:nowrap; }
    .wv { font-size:11.5px; line-height:1.5; color:var(--primary-text-color);
      font-family: var(--code-font-family, ui-monospace, SFMono-Regular, Menlo, monospace); }
    .wv code { background:var(--card-background-color); border-radius:4px; padding:0 4px; }
    .wm { font-size:10px; font-weight:700; white-space:nowrap; color:var(--secondary-text-color); }
    .wm.ok { color:${C.ok}; } .wm.warn { color:${C.amber}; } .wm.cool { color:${C.cool}; }
    .wsep { height:1px; background:var(--divider-color); opacity:.7; margin:4px 0 2px; }
    .wflags { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:4px; }
    .wflag { font-size:10px; font-weight:700; text-transform:uppercase; letter-spacing:.06em;
      padding:3px 8px; border-radius:6px; color:var(--c);
      background: color-mix(in srgb, var(--c) 18%, transparent);
      border:1px solid color-mix(in srgb, var(--c) 45%, transparent); }
    .wflag .wfx { font-weight:500; text-transform:none; letter-spacing:0; opacity:.95; }
    .wid { margin-top:3px; font-size:9.5px; color:var(--secondary-text-color); opacity:.75; }
    .wid code { font-family: var(--code-font-family, ui-monospace, Menlo, monospace);
      background:var(--card-background-color); border-radius:4px; padding:0 4px; }
    @media (max-width: 400px) {
      .wrow { grid-template-columns: 58px 1fr; }
      .wm { grid-column:2; grid-row:2; }
    }
    .freshness { font-size:11px; color: var(--secondary-text-color); opacity:.85; margin:0 0 4px;
      width:max-content; }
    .freshness.clk { cursor:pointer; }
    .freshness.stale { color: ${C.danger}; opacity:1; font-weight:600; }
    .chips { display:flex; flex-wrap:wrap; gap:6px; }
    .chip { display:flex; gap:5px; align-items:baseline; padding:3px 8px; border-radius:7px;
      background: var(--secondary-background-color); font-size:12px; font-variant-numeric: tabular-nums; }
    .chip .k { color: var(--secondary-text-color); text-transform:uppercase; font-size:10px; letter-spacing:.08em; }
    .chip .v { color: var(--primary-text-color); font-weight:600; }
    .chip.soft .v { color: var(--secondary-text-color); }
    .chip.warn { background: color-mix(in srgb, ${C.amber} 20%, transparent); }
    .chip.warn .v { color: ${C.amber}; }
    /* a hold past the compressor dwell is the good outcome, so it gets to say so */
    .chip.calm { background: color-mix(in srgb, ${C.ok} 18%, transparent); }
    .chip.calm .v { color: ${C.ok}; }
    .up { color: ${C.cool}; } .dn { color: ${C.warm}; }

    /* enable toggle */
    .toggle { width:42px; height:24px; border-radius:999px; border:none; cursor:pointer; position:relative;
      background: var(--divider-color); transition:.18s; padding:0; }
    .toggle.on { background: ${C.ok}; }
    .toggle .knob { position:absolute; top:3px; left:3px; width:18px; height:18px; border-radius:50%;
      background:#fff; transition:.18s; box-shadow:0 1px 2px rgba(0,0,0,.3); }
    .toggle.on .knob { left:21px; }

    /* buttons */
    .btn { font-size:12px; padding:4px 12px; border-radius:7px; border:none; cursor:pointer;
      background: var(--primary-color); color: var(--text-primary-color, #fff); font-weight:600; }
    .btn:disabled { opacity:.4; cursor:default; }
    .btn.ghost { background: var(--secondary-background-color); color: var(--primary-text-color); }

    /* schedule */
    .schedsvg { width:100%; height:auto; display:block; touch-action:none; cursor:crosshair; }
    .schedsvg .grid { stroke: var(--divider-color); stroke-width:1; }
    .schedsvg .grid.faint { stroke: var(--divider-color); opacity:.4; stroke-dasharray:2 3; }
    .schedsvg .axl { fill: var(--secondary-text-color); font-size:9px; }
    .schedsvg .dot { fill: var(--primary-color); opacity:.5; }
    .schedsvg .now { stroke: ${C.cool}; stroke-width:1.5; stroke-dasharray:3 2; opacity:.8; }
    .sched-edit { display:flex; justify-content:space-between; align-items:center; gap:10px; margin-top:8px; }
    .se-grp { display:flex; align-items:center; gap:4px; flex:1;
      background:var(--secondary-background-color); border-radius:10px; padding:3px; }
    .se-btn { flex:none; width:44px; height:40px; border:none; border-radius:8px; cursor:pointer;
      background:var(--card-background-color); color:var(--primary-text-color);
      font-size:20px; line-height:1; font-weight:600; box-shadow:0 1px 2px rgba(0,0,0,.12); }
    .se-btn:active { transform:translateY(1px); }
    .se-lab { flex:1; text-align:center; font-size:15px; font-weight:600; font-variant-numeric:tabular-nums;
      color:var(--primary-text-color); }
    .se-lab.val { color:var(--primary-color); }
    /* power strip — evidence under the chart, on its own 6 h axis */
    .pwr { margin-top:10px; }
    .pwrsvg { width:100%; height:auto; display:block; }
    .pwrsvg .grid { stroke: var(--divider-color); stroke-width:1; }
    .pwrsvg .grid.faint { stroke: var(--divider-color); opacity:.4; stroke-dasharray:2 3; }
    .pwrsvg .axl { fill: var(--secondary-text-color); font-size:8.5px; }
    .pwrsvg .axl.sp { fill: ${C.cool}; font-weight:600; }
    .pwrsvg .sptick { stroke: ${C.cool}; stroke-width:1; opacity:.6; }

    .hint { font-size:11px; color: var(--secondary-text-color); margin-top:4px; text-align:center; }
    .hint .lg { margin-left:8px; white-space:nowrap; }
    .hint .sw { display:inline-block; width:9px; height:2px; border-radius:1px; margin-right:3px; vertical-align:middle; }

    /* tuning */
    .seg { display:flex; gap:0; border:1px solid var(--divider-color); border-radius:8px; overflow:hidden; margin-bottom:10px; }
    .segbtn { flex:1; border:none; background:transparent; padding:7px 4px; font-size:12px; cursor:pointer;
      color: var(--secondary-text-color); text-transform:capitalize; border-right:1px solid var(--divider-color); }
    .segbtn:last-child { border-right:none; }
    .segbtn.sel { background: var(--primary-color); color: var(--text-primary-color,#fff); font-weight:600; }
    .steppers { display:flex; gap:8px; flex-wrap:wrap; }
    .stepper { display:flex; align-items:center; gap:6px; background:var(--secondary-background-color);
      border-radius:8px; padding:4px 6px; }
    .sk { font-size:11px; color:var(--secondary-text-color); text-transform:uppercase; letter-spacing:.06em; }
    .sv { font-size:13px; font-weight:600; font-variant-numeric:tabular-nums; min-width:38px; text-align:center; }
    .sb { width:22px; height:22px; border-radius:6px; border:none; cursor:pointer; font-size:15px; line-height:1;
      background: var(--card-background-color); color:var(--primary-text-color); border:1px solid var(--divider-color); }

    /* click-to-history affordances */
    .clk { cursor:pointer; }
    .chip.clk:hover { background: color-mix(in srgb, var(--primary-color) 12%, var(--secondary-background-color)); }
    .feel.clk:hover, .goal.clk:hover, .pill.clk:hover { opacity:.82; }

    /* fan-assist control row */
    .ctl { display:flex; align-items:center; gap:12px; margin-bottom:10px; }
    .ctl .sk { font-size:11px; color:var(--secondary-text-color); text-transform:uppercase; letter-spacing:.06em; }
    .toggle.sm { width:34px; height:20px; }
    .toggle.sm .knob { width:14px; height:14px; }
    .toggle.sm.on .knob { left:17px; }

    @media (max-width: 420px) { .feel, .goal { font-size:32px; } .goal.zone { font-size:24px; } }
    @media (prefers-reduced-motion: reduce) { .toggle, .toggle .knob { transition:none; } }
    :focus-visible { outline: 2px solid var(--primary-color); outline-offset: 1px; }
  `;

  customElements.define("comfort-zone-card", ComfortZoneCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "comfort-zone-card",
    name: "Comfort Zone Card",
    description: "Control panel for a Comfort Zone: live state, the loop's own numbers, a drag-a-curve target schedule and tuning.",
    preview: false,
  });
  console.info(`%c COMFORT-ZONE-CARD %c ${VERSION} `, "background:#3b8ff0;color:#fff;border-radius:3px 0 0 3px;padding:2px 4px", "background:#f0913b;color:#fff;border-radius:0 3px 3px 0;padding:2px 4px");
})();
