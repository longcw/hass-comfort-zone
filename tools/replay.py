"""Replay recorded history through the control core, to measure BAND FIT.

Two passes, because they answer questions of different strength:

**Decision replay (exact).** Feed each recorded tick's signals into the controller
and record what it decides. The recorded comfort trajectory is the input, so
nothing here depends on a plant model: "would the guard have tripped on this
reading", "what setpoint would this state have produced", "how long would the loop
have sat saturated" are all answered exactly.

**Closed-loop simulation (model-dependent).** To get a fit metric the loop has to be
closed, which needs a plant. The thermal load is reconstructed as the residual after
removing the identified FOPDT response to the *recorded* setpoints, then the new
controller drives the same load. This assumes what the model assumes — linear
superposition, constant gain and dead time — and the gain in particular is a prior
rather than a measurement (see tools/fit.py). Treat the absolute numbers as
indicative and the recorded baseline as ground truth.

Two harness bugs, found the hard way and worth not reintroducing: the fan must be
*simulated* (pinning it on leaves the old cold arm stuck at "ease fan first" and the
loop reads far calmer and colder than it is), and the recorded baseline must use the
same band definition as the simulated arm.

Usage:
    python tools/replay.py --hours 24
    python tools/replay.py --from 2026-08-06T11:00 --to 2026-08-06T15:35
    python tools/replay.py --windows
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components"))

import requests
from dotenv import load_dotenv

from comfort_zone.controller import Controller, Signals, ZoneParams
from comfort_zone.model import FopdtPredictor, ModelParams
from comfort_zone.safety import SafetyGuard, SafetyParams, rails as configured_rails

# Where HA_URL / HA_TOKEN come from. Both tools talk to a live Home Assistant over
# its REST API; neither stores a credential. Override the dotenv location with
# COMFORT_ZONE_ENV, or just export the two variables and skip the file entirely.
ENV = os.environ.get("COMFORT_ZONE_ENV", "~/code/ha-homekit-sync/.env")
COMFORT = "sensor.baby_crib_comfort_temp"
POWER = "sensor.shuang_lu_hu_gan_ji_liang_qi_power_2"
CLIMATE = "climate.090615_cn_proxy_646317321_00005_ktf"
WEATHER = "weather.forecast_home"
# The target is its own entity — it is NOT an attribute of the status sensor, and
# reading it from there silently yields nothing and falls back to a constant.
TARGET = "sensor.master_bedroom_target"
BLOWERS = ["低风", "中风", "高风"]
TICK_S = 45
SLOPE_WINDOW_MIN = 5.0

# Named windows worth replaying, because the whole point of a weather feedforward
# is that a hot afternoon and a mild night are not the same problem.
WINDOWS = [
    ("v4, post-deploy", "2026-08-06T22:45", "2026-08-07T01:00"),
    ("v3.2, −1 day", "2026-08-05T22:45", "2026-08-06T01:00"),
    ("v3.2, −2 days", "2026-08-04T22:45", "2026-08-05T01:00"),
    ("v3.2, −3 days", "2026-08-03T22:45", "2026-08-04T01:00"),
    ("hot afternoon", "2026-08-05T04:00", "2026-08-05T09:00"),
    ("mild night", "2026-08-04T14:00", "2026-08-04T22:00"),
]


# --- history ---------------------------------------------------------------
def _credentials() -> tuple[str, str]:
    load_dotenv(os.path.expanduser(ENV))
    try:
        return os.environ["HA_URL"], os.environ["HA_TOKEN"]
    except KeyError as err:
        raise SystemExit(
            f"{err.args[0]} is not set. Export HA_URL and HA_TOKEN, or point "
            f"COMFORT_ZONE_ENV at a dotenv that defines them (tried {ENV}).") from None


def fetch(t0: datetime, t1: datetime) -> dict:
    url, token = _credentials()
    out: dict[str, list] = {}
    cur = t0
    while cur < t1:
        end = min(cur + timedelta(hours=24), t1)
        r = requests.get(
            f"{url}/api/history/period/{cur.isoformat()}",
            params={"filter_entity_id": ",".join([COMFORT, POWER, CLIMATE, WEATHER, TARGET]),
                    "end_time": end.isoformat(), "significant_changes_only": "0"},
            headers={"Authorization": f"Bearer {token}"}, timeout=300)
        r.raise_for_status()
        for lst in r.json():
            if lst:
                out.setdefault(lst[0]["entity_id"], []).extend(lst)
        cur = end
    return out


def series(rows, attr=None):
    """(timestamp, value) pairs, skipping unusable states."""
    out = []
    for x in rows or []:
        raw = x["attributes"].get(attr) if attr else x["state"]
        if raw in (None, "unknown", "unavailable", ""):
            continue
        try:
            out.append((datetime.fromisoformat(x["last_updated"]), float(raw)))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda p: p[0])
    return out


def at(pairs, t):
    """Last value at or before t (step interpolation, as the recorder stores it)."""
    val = None
    for ts, v in pairs:
        if ts > t:
            break
        val = v
    return val


def state_at(rows, t):
    cur = None
    for x in rows or []:
        if datetime.fromisoformat(x["last_updated"]) > t:
            break
        cur = x
    return cur


# --- plant -----------------------------------------------------------------
class Plant:
    """FOPDT superposition plus a reconstructed load, sharing the model's assumptions."""

    def __init__(self, params: ModelParams, load):
        self.p = params
        self.load = load          # (timestamp, °C) — comfort with setpoint effects removed
        self.steps: list[tuple[datetime, float]] = []

    def response(self, t, steps) -> float:
        total = 0.0
        for at_, delta in steps:
            e = (t - at_).total_seconds() / 60.0
            if e <= 0:
                continue
            L, tau = self.p.dead_time_min, max(self.p.tau_min, 1e-6)
            frac = 0.0 if e < L else 1.0 - math.exp(-(e - L) / tau)
            total += delta * self.p.gain_per_step * frac
        return total

    def y(self, t) -> float:
        return at(self.load, t) + self.response(t, self.steps)


def reconstruct_load(comfort, sp_steps, params: ModelParams, grid):
    """Comfort with the identified response to the RECORDED setpoints removed."""
    plant = Plant(params, [])
    plant.steps = sp_steps
    return [(t, at(comfort, t) - plant.response(t, sp_steps)) for t in grid]


# --- metrics ---------------------------------------------------------------
def fit_metrics(samples, target_of, band_low, band_high, rails):
    """Band fit: the objective. rms error vs target is the band-independent one."""
    n = len(samples)
    if not n:
        return {}
    inside = warm = cold = rail_hits = 0
    worst_warm = worst_cold = sq = bias = 0.0
    for t, y in samples:
        tgt = target_of(t)
        hi, lo = tgt + band_high, tgt - band_low
        sq += (y - tgt) ** 2
        bias += y - tgt
        if y > hi:
            warm += 1
            worst_warm = max(worst_warm, y - hi)
        elif y < lo:
            cold += 1
            worst_cold = max(worst_cold, lo - y)
        else:
            inside += 1
        if y < rails[0] or y > rails[1]:
            rail_hits += 1
    return {
        "in_band_pct": 100.0 * inside / n,
        "warm_pct": 100.0 * warm / n,
        "cold_pct": 100.0 * cold / n,
        "rms_err": (sq / n) ** 0.5,
        # Mean SIGNED error. Separates a loop that sits off target from one that is
        # centred but rippling — an asymmetric band scores those very differently.
        "bias": bias / n,
        "worst_warm": worst_warm,
        "worst_cold": worst_cold,
        "rail_ticks": rail_hits,
    }


def zone_params(tgt, a):
    return ZoneParams(
        target=tgt, band_low=a.band_low, band_high=a.band_high,
        no_fan_offset=0.2,
        setpoint_min=a.setpoint_min, setpoint_max=a.setpoint_max,
        blower_levels=BLOWERS, fan_min_level=10, fan_max_level=30,
        blower_gain=a.blower_gain,
    )


def slope_of(hist, t):
    cut = t - timedelta(minutes=SLOPE_WINDOW_MIN)
    win = [(ts, v) for ts, v in hist if ts >= cut]
    if len(win) < 2:
        return 0.0
    dt = max((win[-1][0] - win[0][0]).total_seconds() / 60.0, 1e-9)
    return (win[-1][1] - win[0][1]) / dt


def replay_window(hist, t0, t1, a, label=""):
    comfort = series(hist.get(COMFORT))
    power = series(hist.get(POWER))
    outdoor = series(hist.get(WEATHER), "temperature")
    sp_obs = series(hist.get(CLIMATE), "temperature")
    tgt_series = series(hist.get(TARGET))
    comfort = [(t, v) for t, v in comfort if t0 <= t <= t1]
    if not comfort or not sp_obs:
        return None

    def target_of(t):
        return at(tgt_series, t) or a.target

    grid = []
    t = t0
    while t <= t1:
        if at(comfort, t) is not None:
            grid.append(t)
        t += timedelta(seconds=TICK_S)
    if len(grid) < 20:
        return None

    rec_steps, prev = [], None
    for ts, v in sp_obs:
        v = int(round(v))
        if prev is not None and v != prev and t0 <= ts <= t1:
            rec_steps.append((ts, float(v - prev)))
        prev = v

    params = ModelParams(gain_per_step=a.gain, dead_time_min=a.dead_time, tau_min=a.tau,
                         blower_gain=a.blower_gain)
    if a.ff_per_outdoor is not None:
        # pivot about --ff-ref: hold the curve's value there, change only its slope
        params.ff_intercept += (params.ff_per_outdoor - a.ff_per_outdoor) * a.ff_ref
        params.ff_per_outdoor = a.ff_per_outdoor
    rails = configured_rails(target_of(grid[0]), a.band_low, a.band_high,
                            a.hard_min, a.hard_max)

    # ---- 1. recorded baseline (ground truth) ------------------------------
    rec = [(t, at(comfort, t)) for t in grid]
    base = fit_metrics(rec, target_of, a.band_low, a.band_high, rails)

    # ---- 2. decision replay, exact ---------------------------------------
    ctl = Controller(FopdtPredictor(ModelParams(**vars(params))))
    guard = SafetyGuard()
    trips = saturated = 0
    chist: list[tuple[datetime, float]] = []
    seen_sp = None
    for t in grid:
        y = at(comfort, t)
        chist.append((t, y))
        cst = state_at(hist.get(CLIMATE), t)
        sp_now = int(round(at(sp_obs, t))) if at(sp_obs, t) is not None else None
        # the predictor and the compressor dwell are both driven from OBSERVED
        # transitions, as the coordinator does
        if sp_now is not None and sp_now != seen_sp:
            if seen_sp is not None:
                ctl.predictor.record_setpoint_change(t, sp_now - seen_sp)
                ctl.note_setpoint_change(t)
            seen_sp = sp_now
        zp = zone_params(target_of(t), a)
        sig = Signals(
            now=t, comfort=y, slope=slope_of(chist, t),
            outdoor=at(outdoor, t), power=at(power, t),
            ac_on=bool(cst and cst["state"] not in ("off", "unavailable", "unknown")),
            setpoint=sp_now,
            blower_idx=(BLOWERS.index(cst["attributes"]["fan_mode"])
                        if cst and cst["attributes"].get("fan_mode") in BLOWERS else 0),
            fan_on=True, fan_level=20, guard_active=guard.state != "normal",
        )
        cmd = ctl.tick(sig, zp)
        if cmd.trace.get("saturated"):
            saturated += 1
        before = guard.state
        out = guard.evaluate(sig, zp, SafetyParams(rails[0], rails[1], 12.0), cmd)
        if out.mode.startswith("safety") and before == "normal":
            trips += 1

    # ---- 3. closed loop, model-dependent ---------------------------------
    load = reconstruct_load(comfort, rec_steps, params, grid)
    ctl2 = Controller(FopdtPredictor(ModelParams(**vars(params))))
    guard2 = SafetyGuard()
    plant = Plant(params, load)
    sim, sim_moves = [], 0
    chist2 = []
    sp_cur = int(round(sp_obs[0][1]))
    blower_cur = 0
    # The fan has to be simulated too: pinning it on made every easing decision read
    # as "ease fan first" and the loop looked far calmer, and colder, than it was.
    fan_on, fan_level = False, 0
    for t in grid:
        y = plant.y(t)
        sim.append((t, y))
        chist2.append((t, y))
        zp = zone_params(target_of(t), a)
        sig = Signals(now=t, comfort=y, slope=slope_of(chist2, t),
                      outdoor=at(outdoor, t), power=at(power, t),
                      ac_on=True, setpoint=sp_cur, blower_idx=blower_cur,
                      fan_on=fan_on, fan_level=fan_level,
                      guard_active=guard2.state != "normal")
        cmd = ctl2.tick(sig, zp)
        out = guard2.evaluate(sig, zp, SafetyParams(rails[0], rails[1], 12.0), cmd)
        if out.set_setpoint is not None and out.set_setpoint != sp_cur:
            plant.steps.append((t, float(out.set_setpoint - sp_cur)))
            ctl2.predictor.record_setpoint_change(t, out.set_setpoint - sp_cur)
            # Starts the compressor dwell, so the simulated arm is paced like the
            # real one. Without it nothing here ever started that clock and the arm
            # reported a moves/h figure the hardware would never have allowed —
            # which is how a 3.0/h claim came out of an unpaced loop.
            ctl2.note_setpoint_change(t)
            sp_cur = out.set_setpoint
            sim_moves += 1
        if out.set_blower_idx is not None:
            blower_cur = out.set_blower_idx
        if out.set_fan is not None:
            fan_on = out.set_fan
        if out.set_fan_level is not None:
            fan_level = out.set_fan_level
    new = fit_metrics(sim, target_of, a.band_low, a.band_high, rails)

    hours = max((t1 - t0).total_seconds() / 3600.0, 1e-9)
    return {
        "label": label, "ticks": len(grid), "hours": hours,
        "base": base, "new": new, "rails": rails,
        "rec_moves": len(rec_steps), "sim_moves": sim_moves,
        "trips": trips, "saturated_pct": 100.0 * saturated / len(grid),
        "outdoor": [v for _, v in outdoor if t0 <= _ <= t1] if outdoor else [],
    }


HEAD = ("  {:22s} {:>8s} {:>7s} {:>7s} {:>14s} {:>16s} {:>6s}"
        .format("", "in band", "rms", "bias", "warm/cold", "worst +/-", "rails"))


def line(name, m):
    print(f"  {name:22s} {m['in_band_pct']:7.1f}% {m['rms_err']:7.3f} {m['bias']:+7.3f}   "
          f"{m['warm_pct']:5.1f}% /{m['cold_pct']:5.1f}%   "
          f"+{m['worst_warm']:.2f} / -{m['worst_cold']:.2f}   {m['rail_ticks']:4d}")


def report(r, a):
    print(f"\n{'=' * 78}\n{r['label']}   {r['ticks']} ticks over {r['hours']:.1f} h"
          + (f"   outdoor {min(r['outdoor']):.1f}–{max(r['outdoor']):.1f} °C"
             if r["outdoor"] else "   outdoor: no history"))
    print(f"  band ±({a.band_low}, {a.band_high})   rails effective "
          f"({r['rails'][0]:.2f}, {r['rails'][1]:.2f})")
    print(HEAD)
    line("recorded (as it ran)", r["base"])
    line("simulated (new core)", r["new"])
    print(f"  setpoint moves   recorded {r['rec_moves']:3d} "
          f"({r['rec_moves'] / r['hours']:.1f}/h)   |   "
          f"simulated {r['sim_moves']:3d} ({r['sim_moves'] / r['hours']:.1f}/h)")
    print(f"  guard trips on the recorded readings: {r['trips']}")
    print(f"  new core would sit saturated {r['saturated_pct']:.0f}% of ticks "
          f"(asking for a setpoint outside {a.setpoint_min}–{a.setpoint_max})")


def verdict(r, a):
    """The §4 acceptance criteria, checked rather than eyeballed."""
    n, hours = r["new"], r["hours"]
    checks = [
        ("≥ 90% of ticks in band", n["in_band_pct"] >= 90.0, f"{n['in_band_pct']:.1f}%"),
        ("worst excursion ≤ 0.35 °C", max(n["worst_warm"], n["worst_cold"]) <= 0.35,
         f"+{n['worst_warm']:.2f} / -{n['worst_cold']:.2f}"),
        ("zero guard trips", r["trips"] == 0, str(r["trips"])),
        ("≤ 4 setpoint moves/h", r["sim_moves"] / hours <= 4.0,
         f"{r['sim_moves'] / hours:.1f}/h"),
        ("beats the recorded run", n["in_band_pct"] >= r["base"]["in_band_pct"],
         f"{r['base']['in_band_pct']:.1f}% → {n['in_band_pct']:.1f}%"),
    ]
    for name, ok, got in checks:
        print(f"    {'PASS' if ok else 'FAIL'}  {name:28s} {got}")
    return all(ok for _, ok, _ in checks)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--from", dest="t_from")
    ap.add_argument("--to", dest="t_to")
    ap.add_argument("--windows", action="store_true",
                    help="replay the named windows in WINDOWS, including a hot "
                         "afternoon and a mild night")
    ap.add_argument("--band-low", type=float, default=0.4)
    ap.add_argument("--band-high", type=float, default=0.7)
    ap.add_argument("--hard-min", type=float, default=23.0)
    ap.add_argument("--hard-max", type=float, default=29.0)
    ap.add_argument("--target", type=float, default=26.0)
    ap.add_argument("--setpoint-min", type=int, default=24)
    ap.add_argument("--setpoint-max", type=int, default=27)
    # The shipped constants. Override to check a conclusion survives a different prior.
    ap.add_argument("--gain", type=float, default=0.5)
    ap.add_argument("--dead-time", type=float, default=10.0)
    ap.add_argument("--tau", type=float, default=8.0)
    ap.add_argument("--blower-gain", type=float, default=0.0)
    # The reset curve's outdoor slope is the least certain constant in the model:
    # fitted on samples the old controller had censored it reads −0.21 °C/°C, and on
    # the uncensored ones −0.03. Override it to let replay arbitrate. The intercept
    # moves with it so the curve PIVOTS about --ff-ref rather than sliding, which
    # keeps the sweep about sensitivity to weather and not about the overall level.
    ap.add_argument("--ff-per-outdoor", type=float)
    ap.add_argument("--ff-ref", type=float, default=28.0)
    ap.add_argument("--inner-zone", type=float,
                    help="fraction of the band the loop actively holds (0 = track "
                         "the target as a point)")
    ap.add_argument("--summary", action="store_true",
                    help="one line per window plus a mean, for sweeping a constant")
    a = ap.parse_args()

    if a.inner_zone is not None:
        from comfort_zone import controller as _c
        _c.INNER_ZONE_FRAC = a.inner_zone

    if a.windows:
        spans = [(lab, datetime.fromisoformat(f).replace(tzinfo=timezone.utc),
                  datetime.fromisoformat(t).replace(tzinfo=timezone.utc))
                 for lab, f, t in WINDOWS]
    elif a.t_from:
        t0 = datetime.fromisoformat(a.t_from).replace(tzinfo=timezone.utc)
        t1 = (datetime.fromisoformat(a.t_to).replace(tzinfo=timezone.utc)
              if a.t_to else datetime.now(timezone.utc))
        spans = [("requested window", t0, t1)]
    else:
        t1 = datetime.now(timezone.utc)
        spans = [(f"last {a.hours:g} h", t1 - timedelta(hours=a.hours), t1)]

    lo = min(s[1] for s in spans)
    hi = max(s[2] for s in spans)
    print(f"fetching {lo:%m-%d %H:%M} → {hi:%m-%d %H:%M} …", file=sys.stderr)
    hist = fetch(lo, hi)

    results = []
    for label, t0, t1 in spans:
        r = replay_window(hist, t0, t1, a, label)
        if r is None:
            print(f"\n{label}: not enough history in that window")
            continue
        results.append(r)
        if not a.summary:
            report(r, a)

    if a.summary and results:
        print(f"\nband ±({a.band_low}, {a.band_high})   K {a.gain}"
              f"   setpoint {a.setpoint_min}–{a.setpoint_max}")
        print(f"  {'window':18s} {'recorded':>9s} {'new core':>9s} {'rms':>14s} "
              f"{'moves/h':>16s} {'worst cold':>11s}")
        for r in results:
            print(f"  {r['label']:18s} {r['base']['in_band_pct']:8.1f}% "
                  f"{r['new']['in_band_pct']:8.1f}% "
                  f"{r['base']['rms_err']:6.3f} → {r['new']['rms_err']:5.3f} "
                  f"{r['rec_moves'] / r['hours']:8.1f} → {r['sim_moves'] / r['hours']:5.1f} "
                  f"{r['base']['worst_cold']:5.2f} → {r['new']['worst_cold']:4.2f}")
        n = len(results)
        # The worst cold excursion is reported as the WORST across windows, not the
        # mean: it is a safety number, and averaging it away is how a single bad
        # night hides behind five good ones.
        print(f"  {'MEAN':18s} {sum(r['base']['in_band_pct'] for r in results) / n:8.1f}% "
              f"{sum(r['new']['in_band_pct'] for r in results) / n:8.1f}% "
              f"{sum(r['base']['rms_err'] for r in results) / n:6.3f} → "
              f"{sum(r['new']['rms_err'] for r in results) / n:5.3f} "
              f"{sum(r['rec_moves'] / r['hours'] for r in results) / n:8.1f} → "
              f"{sum(r['sim_moves'] / r['hours'] for r in results) / n:5.1f} "
              f"{max(r['base']['worst_cold'] for r in results):5.2f} → "
              f"{max(r['new']['worst_cold'] for r in results):4.2f}  (worst)")
        return 0

    if results:
        print(f"\n{'=' * 78}\nACCEPTANCE (REDESIGN §4), per window")
        for r in results:
            print(f"\n  {r['label']}")
            verdict(r, a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
