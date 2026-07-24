// comfort-zone-card.js
// A Lovelace card for the Comfort Zone integration — an instrument panel for an
// autonomous climate agent. It shows what the room feels like and what the
// controller is doing (and why), lets you shape the day's target curve by
// dragging, and streams the controller's recent decisions.
//
// Dependency-free vanilla custom element (no Lit, no build step, no CDN — HA
// blocks external imports). Register as a Lovelace module resource.
//
// Config:
//   type: custom:comfort-zone-card
//   zone: sensor.master_bedroom_status   # the zone's status sensor
//   title: 主卧 舒适          # optional; defaults to the zone's name
//
// The title renders as a `.card-header`, so section-jump-nav (which scans the
// shadow tree for a .card-header matching the section target) can jump to it —
// set its `target` to this title.
//
// All related entities (comfort/target/predicted sensors, strategy select,
// enable switch, tuning numbers) are discovered from the status sensor's device
// where the registry is available, with entity-id substitution as a fallback.

(() => {
  const VERSION = "0.1.0";

  // --- semantic palette (mid-chroma so it reads on light AND dark surfaces) ---
  const C = {
    cool: "#3b8ff0",   // cooling / the target-goal line
    warm: "#f0913b",   // the comfort "feel" signal
    teal: "#20b2a0",   // fan / easing
    grey: "#8a8f98",   // managed-off
    amber: "#f5a623",  // overheat caution
    danger: "#e5484d", // overcool danger / fail-safe
    ok: "#35c07a",     // idle / on target
  };

  const MODES = {
    idle: { label: "On target", color: C.ok },
    cooling: { label: "Cooling", color: C.cool },
    easing: { label: "Easing", color: C.teal },
    fan_assist: { label: "Fan assist", color: C.teal },
    managed_off: { label: "AC off", color: C.grey },
    safety_overheat: { label: "Overheat guard", color: C.amber },
    safety_overcool: { label: "Overcool guard", color: C.danger },
    failsafe: { label: "Fail-safe", color: C.danger },
    disabled: { label: "Disabled", color: C.grey },
  };

  const STRATEGIES = ["baby", "eco", "comfort", "custom"];
  const T_MIN = 22, T_MAX = 30; // schedule y-axis range (°C)
  const SLOTS = 48;             // 30-min slots

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const snap = (v, step) => Math.round(v / step) * step;
  const fnum = (v) => (v === null || v === undefined || v === "" || isNaN(+v) ? null : +v);

  // ---------------------------------------------------------------------------
  class ComfortZoneCard extends HTMLElement {
    setConfig(config) {
      if (!config || !config.zone) {
        throw new Error("comfort-zone-card: set `zone:` to the zone's status sensor entity_id");
      }
      this._config = config;
      this._draft = null;       // local schedule edits (null = follow the entity)
      this._dragging = false;
      this._activeEdit = null;  // {i, t} of the point being dragged (for the value badge)
      this._lastSig = null;
      this._actual = null;      // cached actual-comfort trace for today [{hf, t}]
      this._actualFetchedAt = 0;
      if (!this.shadowRoot) this.attachShadow({ mode: "open" });
      this._buildShell();
    }

    set hass(hass) {
      this._hass = hass;
      this._update();
    }

    getCardSize() { return 13; }

    // -- entity resolution ---------------------------------------------------
    _resolve() {
      const hass = this._hass;
      const statusId = this._config.zone;
      const out = { status: statusId };
      const slugFull = statusId.replace(/^sensor\./, "").replace(/_status$/, ""); // master_bedroom

      // Prefer the device registry so we survive HA's entity-id auto-naming.
      const reg = hass.entities || {};
      const dev = reg[statusId] ? reg[statusId].device_id : null;
      const sib = dev ? Object.keys(reg).filter((id) => reg[id].device_id === dev) : [];

      const nameOf = (id) => (hass.states[id]?.attributes?.friendly_name || "").toLowerCase();
      for (const id of sib) {
        const domain = id.split(".")[0];
        const fn = nameOf(id);
        if (domain === "switch") {
          if (fn.includes("fan assist")) out.fanAssist = id;
          else out.enable = id;
        } else if (domain === "select") out.strategy = id;
        else if (domain === "sensor") {
          if (id === statusId) continue;
          if (fn.includes("comfort")) out.comfort = id;
          else if (fn.includes("predict")) out.predicted = id;
          else if (fn.includes("rate") || fn.includes("slope") || fn.includes("变化")) out.slope = id;
          else if (fn.includes("target")) out.target = id;
        } else if (domain === "number") {
          if (fn.includes("band") && fn.includes("low")) out.bandLow = id;
          else if (fn.includes("band") && fn.includes("off")) out.bandHighNoFan = id;
          else if (fn.includes("band") && fn.includes("high")) out.bandHigh = id;
          else if (fn.includes("hard min")) out.hardMin = id;
          else if (fn.includes("hard max")) out.hardMax = id;
          else if (fn.includes("fan max") && fn.includes("night")) out.fanMaxNight = id;
          else if (fn.includes("fan max")) out.fanMaxDay = id;
        }
      }
      // Fallbacks by id substitution (registry path above is preferred).
      out.comfort ||= `sensor.${slugFull}_comfort_temperature`;
      out.target ||= `sensor.${slugFull}_target`;
      out.predicted ||= `sensor.${slugFull}_predicted_settled`;
      out.slope ||= `sensor.${slugFull}_rate_of_change`;
      out.enable ||= `switch.${slugFull}_enabled`;
      out.fanAssist ||= `switch.${slugFull}_fan_assist`;
      out.strategy ||= `select.${slugFull}_strategy`;
      out.bandLow ||= `number.${slugFull}_band_low`;
      out.bandHigh ||= `number.${slugFull}_band_high`;
      out.bandHighNoFan ||= `number.${slugFull}_band_high_no_fan`;
      out.fanMaxDay ||= `number.${slugFull}_fan_max_day`;

      // Zone name for the set_schedule service = device/friendly name minus " Status".
      const fn = hass.states[statusId]?.attributes?.friendly_name || slugFull;
      out.zoneName = fn.replace(/\s*status$/i, "").trim() || slugFull;
      return out;
    }

    _st(id) { return id ? this._hass.states[id] : undefined; }

    // -- shell ---------------------------------------------------------------
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
            </section>
            <section class="sec">
              <div class="eyebrow"><span>Tuning</span></div>
              <div id="tune"></div>
            </section>
            <section class="sec">
              <div class="eyebrow"><span>Recent decisions</span></div>
              <div id="hist"></div>
            </section>
          </div>
        </ha-card>`;

      // event delegation — survives section re-renders
      this.shadowRoot.addEventListener("click", (e) => this._onClick(e));
    }

    // -- update loop ---------------------------------------------------------
    _update() {
      if (!this._hass || !this.shadowRoot) return;
      const status = this._st(this._config.zone);
      const miss = this.shadowRoot.getElementById("cz-missing");
      const body = this.shadowRoot.getElementById("cz-body");
      if (!status || status.state === "unavailable") {
        miss.hidden = false;
        body.hidden = true;
        miss.textContent = `Waiting for ${this._config.zone}…`;
        return;
      }
      miss.hidden = true;
      body.hidden = false;

      this._ent = this._resolve();

      // Schedules are per-strategy: when the strategy changes, drop any draft so
      // we render the new strategy's curve instead of the old one's edits.
      const strat = status.attributes.strategy;
      if (this._lastStrategy !== undefined && strat !== this._lastStrategy) {
        this._draft = null;
        this._dragging = false;
        this._markDirty(false);
      }
      this._lastStrategy = strat;

      this._renderHeader(status);
      this._renderTune(status);
      this._renderHistory(status);
      if (!this._dragging) this._renderSchedule(status); // don't clobber a live drag
      this._maybeFetchActual();
    }

    // -- actual comfort trace (today solid, yesterday dotted) ----------------
    // Fetch a trailing ~48h of the room's real comfort and bucket it by LOCAL
    // CALENDAR DAY: today's actual is drawn SOLID (00:00 → now), the previous
    // day's DOTTED as a full-width reference. Plotted by hour-of-day on the same
    // axes as the target so the two compare directly while shaping the curve.
    // As today advances its solid line extends over yesterday's dotted one.
    // Throttled to ≤ once / 3 min; cached and redrawn from cache otherwise.
    _maybeFetchActual() {
      // Prefer the backend's real SOURCE sensor (e.g. 婴儿床 舒适温度) — it has
      // history from before this integration was installed. Fall back to the
      // integration's own comfort sensor (compute mode).
      const em = this._st(this._config.zone)?.attributes?.entities || {};
      const id = em.comfort || (this._ent && this._ent.comfort);
      if (!id || !this._hass || !this._hass.callWS) return;
      const nowMs = Date.now();
      const fresh = this._actualId === id && this._actualFetchedAt && nowMs - this._actualFetchedAt < 180000;
      if (fresh) return;
      this._actualId = id;
      this._actualFetchedAt = nowMs; // set first so overlapping updates don't refetch
      const start = new Date(nowMs - 48 * 3600 * 1000);
      this._hass.callWS({
        type: "history/history_during_period",
        start_time: start.toISOString(),
        end_time: new Date().toISOString(),
        entity_ids: [id],
        minimal_response: true,
        no_attributes: true,
      }).then((res) => {
        const arr = (res && res[id]) || [];
        const mid = new Date(); mid.setHours(0, 0, 0, 0);
        const midToday = mid.getTime();
        const midYest = midToday - 24 * 3600 * 1000;
        const today = [], yesterday = [];
        for (const p of arr) {
          const v = fnum(p.s);
          const lu = p.lu != null ? p.lu : p.last_updated;
          if (v == null || lu == null) continue;
          const ms = lu * 1000;
          const d = new Date(ms);
          const pt = { ms, hf: d.getHours() + d.getMinutes() / 60 + d.getSeconds() / 3600, t: v };
          if (ms >= midToday) today.push(pt);
          else if (ms >= midYest) yesterday.push(pt);
        }
        today.sort((a, b) => a.ms - b.ms);
        yesterday.sort((a, b) => a.ms - b.ms);
        this._actual = { today, yesterday };
        const status = this._st(this._config.zone);
        if (status && !this._dragging) this._renderSchedule(status);
      }).catch(() => { /* no history → just skip the overlay */ });
    }

    // -- header / live state -------------------------------------------------
    _renderHeader(status) {
      const a = status.attributes;
      const em = a.entities || {};
      const mode = a.enabled === false ? "disabled" : status.state;
      const meta = MODES[mode] || { label: mode, color: C.grey };
      const comfort = fnum(this._st(this._ent.comfort)?.state) ?? a.comfort ?? null;
      const target = fnum(this._st(this._ent.target)?.state) ?? a.target ?? null;
      const bandLow = fnum(a.band_low) ?? 0.4;
      const bandHigh = fnum(a.band_high) ?? bandLow;
      const onTarget = comfort != null && target != null
        && comfort >= target - bandLow && comfort <= target + bandHigh;
      // "idle" already reads "On target"; don't double it up.
      const pillLabel = mode === "idle" ? meta.label : meta.label + (onTarget ? " · on target" : "");

      const pdelta = fnum(a.power_delta);
      const parrow = pdelta == null || Math.abs(pdelta) < 40 ? "" :
        (pdelta > 0 ? `<span class="up">▲</span>` : `<span class="dn">▼</span>`);
      const power = fnum(a.power);
      const slope = fnum(a.slope);
      const feelEnt = em.comfort || em.temp || this._ent.comfort || this._config.zone;

      // chip(): `ent` (optional) makes it open that entity's native HA history.
      const chip = (label, val, extra = "", ent = null) => {
        const clickable = ent ? `data-action="more" data-entity="${ent}"` : "";
        return `<div class="chip ${extra} ${ent ? "clk" : ""}" ${clickable}>
          <span class="k">${label}</span><span class="v">${val}</span></div>`;
      };

      // AC state chip — reflects what the AC is ACTUALLY doing, distinct from the
      // controller mode (the AC keeps cooling on its own while the mode is idle).
      let acChip;
      if (a.ac_on) {
        const st = a.ac_state && a.ac_state !== "cool" ? a.ac_state : "cool";
        const parts = [st, a.setpoint != null ? `${a.setpoint}°` : "", a.ac_blower || ""].filter(Boolean);
        acChip = chip("ac", parts.join(" "), "", em.ac);
      } else {
        acChip = chip("ac", "off", "soft", em.ac);
      }

      // Fan chip — reflects the circulation fan's real on/off + assist toggle.
      let fanChip;
      if (a.fan_assist_enabled === false) {
        fanChip = chip("fan", "assist off", "soft", em.fan);
      } else if (a.fan_on) {
        fanChip = chip("fan", `${a.fan_level != null ? a.fan_level : "on"}`, "", em.fan);
      } else {
        fanChip = chip("fan", "off", "soft", em.fan);
      }

      const enableOn = a.enabled !== false;
      const chips = [
        acChip,
        fanChip,
        power != null ? chip("power", `${(power / 1000).toFixed(power >= 1000 ? 1 : 2)}kW ${parrow}`, "", em.power) : "",
        slope != null ? chip("slope", `${slope >= 0 ? "+" : ""}${slope.toFixed(2)}`, "", this._ent.slope) : "",
        a.strategy ? chip("strategy", a.strategy) : "",
        a.is_night ? chip("", "☾ night", "soft") : "",
        a.safety_state && a.safety_state !== "normal" ? chip("safety", a.safety_state, "warn") : "",
      ].join("");

      this.shadowRoot.getElementById("hd").innerHTML = `
        <div class="hd-top">
          <div class="zone card-header">${this._config.title || this._ent.zoneName}</div>
          <button class="toggle ${enableOn ? "on" : ""}" data-action="toggle"
                  title="${enableOn ? "Controller on" : "Controller off"}" role="switch"
                  aria-checked="${enableOn}"><span class="knob"></span></button>
        </div>
        <div class="hero">
          <span class="feel clk" style="color:${C.warm}" data-action="more" data-entity="${feelEnt}"
            >${comfort != null ? comfort.toFixed(1) : "–"}<span class="deg">°</span></span>
          <span class="arrow">→</span>
          <span class="goal clk" style="color:${C.cool}" data-action="more" data-entity="${em.status || this._config.zone}"
            >${target != null ? target.toFixed(1) : "–"}<span class="deg">°</span></span>
          <span class="band">−${bandLow.toFixed(1)} / +${bandHigh.toFixed(1)}</span>
        </div>
        <div class="statusline">
          <span class="pill clk" style="--c:${meta.color}" data-action="more" data-entity="${feelEnt}">${pillLabel}</span>
          <span class="reason">${a.reason || ""}</span>
        </div>
        <div class="chips">${chips}</div>`;
    }

    // -- tuning: strategy segmented + steppers --------------------------------
    _renderTune(status) {
      const cur = this._st(this._ent.strategy)?.state || status.attributes.strategy || "baby";
      const seg = STRATEGIES.map((s) =>
        `<button class="segbtn ${s === cur ? "sel" : ""}" data-action="strategy" data-val="${s}">${s}</button>`
      ).join("");

      // `signed` renders a below-target offset as a negative value with the
      // buttons matching temperature direction: "+" warms the floor (toward
      // target → smaller magnitude), "–" cools it (further below → larger).
      const stepper = (id, label, signed = false) => {
        const st = this._st(id);
        if (!st) return "";
        const v = fnum(st.state);
        const unit = st.attributes.unit_of_measurement || "";
        const disp = v == null ? "–" : (signed ? `−${Math.abs(v).toFixed(2)}` : v);
        const lDir = signed ? 1 : -1;   // left "–"
        const rDir = signed ? -1 : 1;   // right "+"
        return `<div class="stepper">
            <span class="sk">${label}</span>
            <button class="sb" data-action="num" data-id="${id}" data-dir="${lDir}">–</button>
            <span class="sv">${disp}${unit}</span>
            <button class="sb" data-action="num" data-id="${id}" data-dir="${rDir}">+</button>
          </div>`;
      };

      const fanAssistSt = this._st(this._ent.fanAssist);
      const fanOn = fanAssistSt ? fanAssistSt.state === "on" : true;
      const fanRow = fanAssistSt ? `
        <div class="ctl">
          <span class="sk">fan assist</span>
          <button class="toggle sm ${fanOn ? "on" : ""}" data-action="fan_assist" role="switch"
                  aria-checked="${fanOn}" title="${fanOn ? "Fan enabled" : "Fan disabled"}"><span class="knob"></span></button>
        </div>` : "";

      // Only the everyday knobs live on the card; fan-max / hard limits / the
      // inactive band variant stay on the device page. "band high" targets
      // whichever high band is in effect now (fan-on vs fan-off) so it always
      // matches the −low/+high shown in the header.
      const bandHighEnt = fanOn ? this._ent.bandHigh : this._ent.bandHighNoFan;

      this.shadowRoot.getElementById("tune").innerHTML = `
        <div class="seg">${seg}</div>
        ${fanRow}
        <div class="steppers">
          ${stepper(this._ent.bandLow, "band low", true)}
          ${stepper(bandHighEnt, "band high")}
        </div>`;
    }

    // -- history: sparkline + decision stream --------------------------------
    _renderHistory(status) {
      const log = Array.isArray(status.attributes.recent_log) ? status.attributes.recent_log : [];
      const host = this.shadowRoot.getElementById("hist");
      if (!log.length) {
        host.innerHTML = `<div class="empty">No decisions yet. The controller only logs when it acts — a quiet log means it's holding steady.</div>`;
        return;
      }
      host.innerHTML = `
        ${this._sparkline(log)}
        <div class="stream">${log.slice().reverse().map((e) => this._logRow(e)).join("")}</div>`;
    }

    _logRow(e) {
      const meta = MODES[e.mode] || { label: e.mode, color: C.grey };
      // e.t is a UTC ISO timestamp; parse and render in the viewer's local time.
      const d = e.t ? new Date(e.t) : null;
      const t = d && !isNaN(d)
        ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })
        : "";
      const acts = (e.actions || []).join(" · ");
      return `<div class="row">
          <span class="t">${t}</span>
          <span class="rpill" style="--c:${meta.color}">${meta.label}</span>
          <span class="racts">${acts}</span>
          <span class="rreason">${e.reason || ""}</span>
        </div>`;
    }

    // comfort vs target sparkline (°C, one axis) + a thin power strip (W).
    _sparkline(log) {
      const cs = log.map((e) => fnum(e.comfort));
      const ts = log.map((e) => fnum(e.target));
      const ps = log.map((e) => fnum(e.power));
      const vals = [...cs, ...ts].filter((v) => v != null);
      if (vals.length < 2) return "";
      const lo = Math.min(...vals) - 0.2, hi = Math.max(...vals) + 0.2;
      const W = 300, H = 54, n = log.length;
      const x = (i) => (n <= 1 ? 0 : (i / (n - 1)) * W);
      const y = (v) => H - ((v - lo) / (hi - lo || 1)) * H;
      const path = (arr) => {
        let d = "", started = false;
        arr.forEach((v, i) => { if (v == null) return; d += (started ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1) + " "; started = true; });
        return d.trim();
      };
      const lastC = [...cs].reverse().find((v) => v != null);
      const lastT = [...ts].reverse().find((v) => v != null);

      // power strip (separate axis — never share a y-scale with °C)
      let strip = "";
      const pv = ps.filter((v) => v != null);
      if (pv.length >= 2) {
        const plo = Math.min(...pv), phi = Math.max(...pv);
        const py = (v) => 20 - ((v - plo) / (phi - plo || 1)) * 18 - 1;
        let d = "M0 20 ";
        ps.forEach((v, i) => { if (v != null) d += "L" + x(i).toFixed(1) + " " + py(v).toFixed(1) + " "; });
        d += `L${W} 20 Z`;
        strip = `<div class="striplbl">system power</div>
          <svg viewBox="0 0 ${W} 20" preserveAspectRatio="none" class="strip">
            <path d="${d}" fill="${C.teal}" fill-opacity="0.22" stroke="${C.teal}" stroke-width="1"/></svg>`;
      }

      return `<div class="spark">
        <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="sparksvg">
          <path d="${path(ts)}" fill="none" stroke="${C.cool}" stroke-width="2" stroke-linejoin="round"/>
          <path d="${path(cs)}" fill="none" stroke="${C.warm}" stroke-width="2" stroke-linejoin="round"/>
        </svg>
        <div class="sparklbl">
          <span style="color:${C.warm}">● feel ${lastC != null ? lastC.toFixed(1) + "°" : ""}</span>
          <span style="color:${C.cool}">● target ${lastT != null ? lastT.toFixed(1) + "°" : ""}</span>
        </div>
        ${strip}
      </div>`;
    }

    // -- schedule editor -----------------------------------------------------
    _schedData(status) {
      if (this._draft) return this._draft;
      const s = status.attributes.schedule;
      if (Array.isArray(s) && s.length === SLOTS) return s.map(Number);
      return new Array(SLOTS).fill(26);
    }

    _renderSchedule(status) {
      const data = this._schedData(status);
      const W = 320, H = 132, padL = 26, padR = 8, padT = 8, padB = 18;
      const iw = W - padL - padR, ih = H - padT - padB;
      const X = (i) => padL + (i / (SLOTS - 1)) * iw;
      const Y = (t) => padT + ((T_MAX - t) / (T_MAX - T_MIN)) * ih;
      const now = new Date();
      const nowFrac = (now.getHours() * 60 + now.getMinutes()) / 1440;
      const nowX = padL + nowFrac * iw;
      const nowHf = nowFrac * 24;

      const comfort = fnum(this._st(this._ent.comfort)?.state);
      let line = "", area = `M${X(0)} ${padT + ih} `;
      data.forEach((t, i) => { line += (i ? "L" : "M") + X(i).toFixed(1) + " " + Y(clamp(t, T_MIN, T_MAX)).toFixed(1) + " "; });
      data.forEach((t, i) => { area += "L" + X(i).toFixed(1) + " " + Y(clamp(t, T_MIN, T_MAX)).toFixed(1) + " "; });
      area += `L${X(SLOTS - 1)} ${padT + ih} Z`;

      const grid = [0, 6, 12, 18, 24].map((h) => {
        const gx = padL + (h / 24) * iw;
        return `<line x1="${gx}" y1="${padT}" x2="${gx}" y2="${padT + ih}" class="grid"/>
                <text x="${gx}" y="${H - 5}" class="axl" text-anchor="middle">${h}</text>`;
      }).join("");
      const yticks = [24, 26, 28].map((t) =>
        `<text x="${padL - 5}" y="${Y(t) + 3}" class="axl" text-anchor="end">${t}</text>
         <line x1="${padL}" y1="${Y(t)}" x2="${W - padR}" y2="${Y(t)}" class="grid faint"/>`).join("");

      const dots = data.map((t, i) =>
        `<circle cx="${X(i).toFixed(1)}" cy="${Y(clamp(t, T_MIN, T_MAX)).toFixed(1)}" r="2" class="dot"/>`).join("");

      const nowDot = comfort != null
        ? `<circle cx="${nowX.toFixed(1)}" cy="${Y(clamp(comfort, T_MIN, T_MAX)).toFixed(1)}" r="4" fill="${C.warm}" stroke="var(--card-background-color)" stroke-width="1.5"/>`
        : "";

      // Value badge on the point currently being dragged: "HH:MM · 26.4°" — so
      // you read the exact target instead of counting gridlines.
      let editBadge = "";
      if (this._activeEdit) {
        const { i, t } = this._activeEdit;
        const bx = X(i), by = Y(clamp(t, T_MIN, T_MAX));
        const hh = String(Math.floor(i / 2)).padStart(2, "0");
        const mm = i % 2 ? "30" : "00";
        const bw = 70;
        const tx = clamp(bx, padL + bw / 2, W - padR - bw / 2);
        const ty = clamp(by - 12, padT + 15, padT + ih);
        editBadge = `<g text-anchor="middle" style="pointer-events:none">
            <rect x="${(tx - bw / 2).toFixed(1)}" y="${(ty - 13).toFixed(1)}" width="${bw}" height="16" rx="4" fill="var(--primary-color)"/>
            <text x="${tx.toFixed(1)}" y="${(ty - 1).toFixed(1)}" font-size="10.5" font-weight="700" fill="var(--text-primary-color, #fff)">${hh}:${mm} · ${t.toFixed(1)}°</text>
            <circle cx="${bx.toFixed(1)}" cy="${by.toFixed(1)}" r="4" fill="var(--primary-color)" stroke="var(--card-background-color)" stroke-width="1.5"/>
          </g>`;
      }

      // Actual comfort on the same axes as the target, mapped by hour-of-day.
      // Each calendar-day bucket is contiguous (hour-of-day only increases within
      // a day) so each is a single clean polyline — no seam handling needed.
      // Today = SOLID (00:00 → now). Yesterday = a THIN solid line, drawn only for
      // the part of the day that hasn't happened yet (now → 24) — so together they
      // read as one continuous rolling trace with no overlap.
      const XA = (hf) => padL + (clamp(hf, 0, 24) / 24) * iw;
      const act = this._actual || { today: [], yesterday: [] };
      const buildPath = (pts) => {
        if (!pts || !pts.length) return "";
        let d = "";
        pts.forEach((p, i) => { d += (i ? "L" : "M") + XA(p.hf).toFixed(1) + " " + Y(clamp(p.t, T_MIN, T_MAX)).toFixed(1) + " "; });
        return d.trim();
      };
      const yPath = buildPath((act.yesterday || []).filter((p) => p.hf >= nowHf));
      const tPath = buildPath(act.today);
      let actualLine = "";
      if (yPath) actualLine += `<path d="${yPath}" fill="none" stroke="${C.warm}" stroke-width="1" stroke-opacity="0.5" stroke-linejoin="round" stroke-linecap="round"/>`;
      if (tPath) actualLine += `<path d="${tPath}" fill="none" stroke="${C.warm}" stroke-width="1.5" stroke-opacity="0.9" stroke-linejoin="round" stroke-linecap="round"/>`;

      this.shadowRoot.getElementById("sched").innerHTML = `
        <svg id="sched-svg" viewBox="0 0 ${W} ${H}" class="schedsvg" touch-action="none">
          <defs>
            <linearGradient id="czgrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="${C.warm}" stop-opacity="0.28"/>
              <stop offset="100%" stop-color="${C.cool}" stop-opacity="0.10"/>
            </linearGradient>
          </defs>
          ${yticks}${grid}
          <path d="${area}" fill="url(#czgrad)"/>
          ${actualLine}
          <path d="${line}" fill="none" stroke="var(--primary-color)" stroke-width="2" stroke-linejoin="round"/>
          ${dots}
          <line x1="${nowX.toFixed(1)}" y1="${padT}" x2="${nowX.toFixed(1)}" y2="${padT + ih}" class="now"/>
          ${nowDot}
          ${editBadge}
        </svg>
        <div class="hint">Drag to shape the day’s target
          <span class="lg"><i class="sw" style="background:var(--primary-color)"></i>target</span>
          <span class="lg"><i class="sw" style="background:${C.warm}"></i>actual today</span>
          <span class="lg"><i class="sw" style="background:${C.warm};opacity:.5;height:1px"></i>yesterday (ahead)</span></div>`;

      const svg = this.shadowRoot.getElementById("sched-svg");
      const geo = { W, H, padL, padR, padT, padB, iw, ih, X, Y };
      const evToVal = (ev) => {
        const r = svg.getBoundingClientRect();
        const sx = ((ev.clientX - r.left) / r.width) * W;
        const sy = ((ev.clientY - r.top) / r.height) * H;
        const i = clamp(Math.round(((sx - padL) / iw) * (SLOTS - 1)), 0, SLOTS - 1);
        const t = clamp(snap(T_MAX - ((sy - padT) / ih) * (T_MAX - T_MIN), 0.1), T_MIN, T_MAX);
        return { i, t };
      };
      let lastI = null;
      const paint = (ev) => {
        const { i, t } = evToVal(ev);
        const d = this._draft || this._schedData(status).slice();
        if (lastI != null && Math.abs(i - lastI) > 1) { // interpolate across a fast drag
          const a = Math.min(i, lastI), b = Math.max(i, lastI);
          const va = d[lastI], vb = t;
          for (let k = a; k <= b; k++) d[k] = i === lastI ? t : va + (vb - va) * ((k - lastI) / (i - lastI || 1));
        } else { d[i] = t; }
        lastI = i;
        this._draft = d;
        this._activeEdit = { i, t };   // drives the value badge
        this._markDirty(true);
        this._renderScheduleQuiet(status);
      };
      svg.addEventListener("pointerdown", (ev) => {
        ev.preventDefault(); this._dragging = true; lastI = null;
        svg.setPointerCapture(ev.pointerId); paint(ev);
      });
      svg.addEventListener("pointermove", (ev) => { if (this._dragging) paint(ev); });
      const end = () => {
        this._dragging = false; lastI = null; this._activeEdit = null;
        this._renderScheduleQuiet(status);  // clear the badge
      };
      svg.addEventListener("pointerup", end);
      svg.addEventListener("pointercancel", end);
    }

    // re-render only the SVG paths during a drag (cheap, no listener churn cost matters)
    _renderScheduleQuiet(status) {
      // Reuse the full renderer but keep dragging=true so _update won't fight it.
      this._renderSchedule(status);
    }

    _markDirty(dirty) {
      const save = this.shadowRoot.getElementById("btn-save");
      const rev = this.shadowRoot.getElementById("btn-revert");
      if (save) save.disabled = !dirty;
      if (rev) rev.disabled = !dirty;
    }

    // -- interactions --------------------------------------------------------
    _onClick(e) {
      const el = e.target.closest("[data-action]");
      if (!el || !this._hass) return;
      const action = el.dataset.action;
      const hass = this._hass;
      if (action === "more") {
        const entityId = el.dataset.entity;
        if (entityId) {
          this.dispatchEvent(new CustomEvent("hass-more-info", {
            detail: { entityId }, bubbles: true, composed: true,
          }));
        }
      } else if (action === "toggle") {
        const st = this._st(this._ent.enable);
        const on = st && st.state === "on";
        hass.callService("switch", on ? "turn_off" : "turn_on", { entity_id: this._ent.enable });
      } else if (action === "fan_assist") {
        const st = this._st(this._ent.fanAssist);
        const on = st && st.state === "on";
        hass.callService("switch", on ? "turn_off" : "turn_on", { entity_id: this._ent.fanAssist });
      } else if (action === "strategy") {
        hass.callService("select", "select_option", { entity_id: this._ent.strategy, option: el.dataset.val });
      } else if (action === "num") {
        const id = el.dataset.id, dir = +el.dataset.dir;
        const st = this._st(id); if (!st) return;
        const step = fnum(st.attributes.step) || 0.5;
        const v = clamp((fnum(st.state) || 0) + dir * step,
          fnum(st.attributes.min) ?? -Infinity, fnum(st.attributes.max) ?? Infinity);
        hass.callService("number", "set_value", { entity_id: id, value: +v.toFixed(2) });
      } else if (action === "save") {
        if (!this._draft) return;
        hass.callService("comfort_zone", "set_schedule", {
          name: this._ent.zoneName, schedule: this._draft.map((v) => +(+v).toFixed(1)),
        });
        this._draft = null; this._markDirty(false);
      } else if (action === "revert") {
        this._draft = null; this._markDirty(false);
        this._renderSchedule(this._st(this._config.zone));
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
    .deg { font-size: 20px; font-weight: 400; }
    .arrow { font-size: 22px; color: var(--secondary-text-color); }
    .band { font-size: 13px; color: var(--secondary-text-color); align-self:center; }
    /* pill + reason share their own line below the numbers, so the pill's text
       length never reflows the temperature row */
    .statusline { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin:2px 0 8px; }
    .pill { flex:none; align-self:center; font-size:12px; font-weight:600; padding:3px 10px;
      border-radius:999px; color:var(--c); background: color-mix(in srgb, var(--c) 16%, transparent);
      border:1px solid color-mix(in srgb, var(--c) 40%, transparent); white-space:nowrap; }
    .reason { flex:1; min-width:0; font-size: 12.5px; color: var(--secondary-text-color); }
    .chips { display:flex; flex-wrap:wrap; gap:6px; }
    .chip { display:flex; gap:5px; align-items:baseline; padding:3px 8px; border-radius:7px;
      background: var(--secondary-background-color); font-size:12px; font-variant-numeric: tabular-nums; }
    .chip .k { color: var(--secondary-text-color); text-transform:uppercase; font-size:10px; letter-spacing:.08em; }
    .chip .v { color: var(--primary-text-color); font-weight:600; }
    .chip.soft .v { color: var(--secondary-text-color); }
    .chip.warn { background: color-mix(in srgb, ${C.amber} 20%, transparent); }
    .chip.warn .v { color: ${C.amber}; }
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

    /* history */
    .empty { font-size:12.5px; color:var(--secondary-text-color); padding:6px 0; }
    .spark { margin-bottom:10px; }
    .sparksvg { width:100%; height:54px; display:block; }
    .strip { width:100%; height:20px; display:block; }
    .sparklbl, .striplbl { font-size:10px; display:flex; gap:12px; margin-top:2px; color:var(--secondary-text-color); }
    .striplbl { text-transform:uppercase; letter-spacing:.08em; margin-top:6px; }
    .stream { max-height: 210px; overflow-y:auto; display:flex; flex-direction:column; gap:2px;
      font-family: var(--code-font-family, ui-monospace, SFMono-Regular, Menlo, monospace); }
    .row { display:grid; grid-template-columns: max-content max-content 1fr; grid-template-rows:auto auto;
      align-items:baseline; gap:2px 8px; padding:5px 6px; border-radius:6px; }
    .row:nth-child(odd) { background: var(--secondary-background-color); }
    .t { font-size:11px; color:var(--secondary-text-color); grid-row:1; grid-column:1;
      white-space:nowrap; font-variant-numeric:tabular-nums; }
    .rpill { font-size:10px; font-weight:700; padding:1px 7px; border-radius:999px; grid-column:2; grid-row:1;
      white-space:nowrap; color:var(--c); background: color-mix(in srgb, var(--c) 16%, transparent); }
    .racts { font-size:11px; color:var(--primary-text-color); grid-column:3; grid-row:1; text-align:right;
      white-space:nowrap; overflow:hidden; text-overflow:ellipsis; min-width:0; }
    .rreason { font-size:11px; color:var(--secondary-text-color); grid-column:1 / -1; grid-row:2;
      font-family: var(--primary-font-family, sans-serif); }

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

    @media (max-width: 420px) { .feel, .goal { font-size:32px; } .racts { display:none; } }
    @media (prefers-reduced-motion: reduce) { .toggle, .toggle .knob { transition:none; } }
    :focus-visible { outline: 2px solid var(--primary-color); outline-offset: 1px; }
  `;

  customElements.define("comfort-zone-card", ComfortZoneCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "comfort-zone-card",
    name: "Comfort Zone Card",
    description: "Control panel for a Comfort Zone: live state, drag-a-curve target schedule, tuning, and the controller's decision log.",
    preview: false,
  });
  console.info(`%c COMFORT-ZONE-CARD %c ${VERSION} `, "background:#3b8ff0;color:#fff;border-radius:3px 0 0 3px;padding:2px 4px", "background:#f0913b;color:#fff;border-radius:0 3px 3px 0;padding:2px 4px");
})();
