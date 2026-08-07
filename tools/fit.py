"""Offline system identification — the constants the new core runs on.

Replaces the online adapter. Everything the controller needs to know about the
plant is fitted here, from recorder history, and reviewed by a human before it is
written into :data:`const.MODEL_DEFAULTS`.

**This history is closed loop, and that decides the method.** The controller moves
the setpoint *because* the room moved, a median of every 10.5 min, so a regression
of settled comfort on setpoint recovers the controller's own inverse rather than
the plant: run that way, this data returns a gain of −0.04 °C/°C, the wrong sign
and effectively zero. There are no clean open-loop steps to fall back on either —
over two days only ten setpoint holds reached 30 minutes.

What *is* identifiable is the dynamic response, because the plant and the
controller act on different timescales. The controller answers the room within a
tick; the plant answers the setpoint only after a dead time. So the regressor is
not the setpoint but its **materialising response**

    S(t) = Σ_k Δ_k · f(t − t_k;  θ, τ)      f = the unit-gain FOPDT step response

which lags and smooths the setpoint and is therefore no longer collinear with the
controller's reaction to the load. Three stages, each answering one question:

**A — dynamics and gains.** Grid-search (θ, τ); for each, solve for the plant gain
``K``, the blower's gain ``K_b``, and a slow load spline flexible enough to absorb
the weather but far too slow (12-hour knots) to chase a 10-minute setpoint move.

**Stage A does not survive its own diagnostics, and the tool says so.** The bias is
reduced but not removed: the search runs θ and τ to the bottom of the grid, because
a short dead time is what best lines up with a controller that answers the room
within one tick, and the blower coefficient comes out POSITIVE — more airflow
measuring as a warmer room, which is the old controller's rule and not physics.
That sign is the tell. So ``K``, ``θ`` and ``τ`` ship as reviewed priors, and the
blower's authority ships as zero rather than as a number nobody believes. Stage A
is kept because knowing an estimate is untrustworthy is worth more than not having
computed it.

**B — the feedforward, and this one IS identifiable.** Not via the plant inverse,
which needs the gain. What the feedforward has to predict is "the setpoint this
load calls for", and closed-loop history answers that directly: regressing the
controller's own *output* on outdoor temperature is safe where regressing the room
on that output is not, because outdoor temperature is exogenous to both. Censoring
is repaired rather than dropped — each sample is corrected to the setpoint that
would have held the room on target — so the hot samples, which are exactly the ones
pinned at the setpoint floor, still count. The target's coefficient is pinned by
physics at 1/K rather than fitted, because the target never moved more than 0.5 °C
in the whole window.

**C — hour of day**, as a check rather than a parameter. Reports both the size of
the residual swing and its **roughness** — the mean hour-to-hour jump against the
whole swing, which scores about 0.08 for a clean daily cycle and 0.29 for pure
noise. A large swing with a noise-like roughness is the old controller behaving
differently at different hours, not a load, and keying a feedforward on it would
bake that behaviour in.

Uncertainty on the gain is reported as the **spread across single-day refits**, not
as a regression standard error — the residuals are strongly autocorrelated, so the
textbook standard error here is optimistic by about an order of magnitude.

Needs ``HA_URL`` and ``HA_TOKEN`` (see ``ENV`` below), and the entity ids at the top
of this file are this deployment's — edit them for another.

Usage:
    python tools/fit.py --days 7
"""
from __future__ import annotations

import argparse
import math
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

# Where HA_URL / HA_TOKEN come from. Both tools talk to a live Home Assistant over
# its REST API; neither stores a credential. Override the dotenv location with
# COMFORT_ZONE_ENV, or just export the two variables and skip the file entirely.
ENV = os.environ.get("COMFORT_ZONE_ENV", "~/code/ha-homekit-sync/.env")
COMFORT = "sensor.baby_crib_comfort_temp"
CLIMATE = "climate.090615_cn_proxy_646317321_00005_ktf"
WEATHER = "weather.forecast_home"
# The target is its own entity. It is NOT an attribute of the status sensor —
# reading it from there silently yields nothing at all.
TARGET = "sensor.master_bedroom_target"
BLOWERS = ["低风", "中风", "高风"]

GRID_S = 120              # resample onto this grid; the sensor reports far slower
KNOT_H = 12.0             # hours between load-spline knots — slow enough that the
#                           spline cannot chase a setpoint move and steal the gain
WARMUP_MIN = 90.0         # skip the head of the window: steps before it are unknown
THETA_GRID = [1, 2, 4, 6, 8, 12, 16, 20, 24]
TAU_GRID = [1, 2, 3, 6, 10, 16, 24, 32]
# Stage B only learns from setpoints that were actually working: the room within
# this much of target, and the setpoint off its own limits (a censored setpoint
# means the controller wanted more than it could ask for, which biases the slope).
SP_LIMITS = (24, 27)   # the configured envelope; anything outside it is the guard
# The gain stage A cannot identify. Taken from the old online adapter, which fitted
# it per episode and reported it stable across days. Assumed at the TOP of its
# plausible range on purpose: it divides into Kc, so guessing high yields a gentler
# loop and guessing low an over-aggressive one.
PRIOR_GAIN = 0.5
PRIOR_DEAD_TIME = 10.0
PRIOR_TAU = 8.0


# --- history ----------------------------------------------------------------
def _credentials() -> tuple[str, str]:
    load_dotenv(os.path.expanduser(ENV))
    try:
        return os.environ["HA_URL"], os.environ["HA_TOKEN"]
    except KeyError as err:
        raise SystemExit(
            f"{err.args[0]} is not set. Export HA_URL and HA_TOKEN, or point "
            f"COMFORT_ZONE_ENV at a dotenv that defines them (tried {ENV}).") from None


def fetch(t0: datetime, t1: datetime, entities: list[str]) -> dict:
    """History for ``entities``, a day at a time so no single request is huge.

    ``significant_changes_only=0`` matters for the weather entity: its *state* is a
    condition word that changes a few times a day, so the default filter drops most
    of the hourly temperature updates (17 rows per 2 days against 43).
    """
    url, token = _credentials()
    out: dict[str, list] = {e: [] for e in entities}
    cur = t0
    while cur < t1:
        end = min(cur + timedelta(hours=24), t1)
        r = requests.get(
            f"{url}/api/history/period/{cur.isoformat()}",
            params={"filter_entity_id": ",".join(entities),
                    "end_time": end.isoformat(), "significant_changes_only": "0"},
            headers={"Authorization": f"Bearer {token}"}, timeout=300)
        r.raise_for_status()
        for lst in r.json():
            if lst:
                out.setdefault(lst[0]["entity_id"], []).extend(lst)
        print(f"  fetched {cur:%m-%d %H:%M} → {end:%m-%d %H:%M}", file=sys.stderr)
        cur = end
    return out


def series(rows, attr=None, labels=None):
    """(timestamp, value) pairs from a history list, skipping unusable states.

    ``labels`` maps a string attribute onto its ladder index instead of a float.
    """
    out = []
    for x in rows or []:
        raw = x["attributes"].get(attr) if attr else x["state"]
        if raw in (None, "unknown", "unavailable", ""):
            continue
        try:
            val = labels.index(str(raw)) if labels else float(raw)
        except (TypeError, ValueError):
            continue
        out.append((datetime.fromisoformat(x["last_updated"]), float(val)))
    out.sort(key=lambda p: p[0])
    return out


class Step:
    """Step-interpolated lookup, for callers that query monotonically in time."""

    def __init__(self, pairs):
        self.pairs, self.i = pairs, 0

    def at(self, t):
        while self.i + 1 < len(self.pairs) and self.pairs[self.i + 1][0] <= t:
            self.i += 1
        if not self.pairs or self.pairs[self.i][0] > t:
            return None
        return self.pairs[self.i][1]


# --- least squares ----------------------------------------------------------
def lstsq(rows, ys):
    """Solve the normal equations by Gauss-Jordan with partial pivoting.

    Small and dense — a couple of dozen regressors — so an explicit solve keeps the
    tool free of numpy, which this machine's HA venv does not carry.
    """
    n = len(rows[0])
    a = [[0.0] * (n + 1) for _ in range(n)]
    for r, y in zip(rows, ys):
        for i in range(n):
            if r[i] == 0.0:
                continue
            ai = a[i]
            for j in range(i, n):
                ai[j] += r[i] * r[j]
            ai[n] += r[i] * y
    for i in range(n):                      # mirror the symmetric half
        for j in range(i):
            a[i][j] = a[j][i]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-10:
            return None
        a[col], a[piv] = a[piv], a[col]
        for r in range(n):
            if r == col:
                continue
            f = a[r][col] / a[col][col]
            if f:
                for c in range(col, n + 1):
                    a[r][c] -= f * a[col][c]
    return [a[i][n] / a[i][i] for i in range(n)]


def score(rows, ys, coef):
    mean = sum(ys) / len(ys)
    res = sum((y - sum(c * x for c, x in zip(coef, r))) ** 2 for r, y in zip(rows, ys))
    tot = sum((y - mean) ** 2 for y in ys)
    return 1.0 - res / tot if tot > 0 else 0.0


# --- the grid ---------------------------------------------------------------
def build_grid(t0, t1, hist):
    comfort = series(hist.get(COMFORT))
    outdoor = series(hist.get(WEATHER), "temperature")
    sp_obs = series(hist.get(CLIMATE), "temperature")
    blower = series(hist.get(CLIMATE), "fan_mode", labels=BLOWERS)
    target = series(hist.get(TARGET))
    for name, s in (("comfort", comfort), ("outdoor", outdoor),
                    ("setpoint", sp_obs), ("blower", blower)):
        print(f"  {name:9s} {len(s):6d} points", file=sys.stderr)
    if not comfort or not outdoor or not sp_obs:
        return [], [], []

    cf, od, sp, bl, tg = (Step(comfort), Step(outdoor), Step(sp_obs),
                          Step(blower), Step(target))
    rows, t = [], t0
    while t <= t1:
        y, o, s = cf.at(t), od.at(t), sp.at(t)
        if y is not None and o is not None and s is not None:
            rows.append({"t": t, "y": y, "outdoor": o, "sp": round(s),
                         "blower": bl.at(t) or 0.0, "target": tg.at(t)})
        t += timedelta(seconds=GRID_S)

    def transitions(key):
        out, prev = [], None
        for r in rows:
            v = r[key]
            if prev is not None and v != prev:
                out.append((r["t"], float(v - prev)))
            prev = v
        return out

    return rows, transitions("sp"), transitions("blower")


def response(rows, steps, theta, tau, base):
    """Materialising response ``S(t)`` to a list of steps, at unit gain.

    Written as "the change already commanded, minus the part not yet arrived",
    so only steps inside the last θ+6τ minutes are ever visited — the rest have
    fully materialised and are carried by the cumulative total.
    """
    horizon = theta + 6 * tau
    out, lo = [], 0
    total, applied = 0.0, 0
    for r in rows:
        t = r["t"]
        while applied < len(steps) and steps[applied][0] <= t:
            total += steps[applied][1]
            applied += 1
        while lo < len(steps) and (t - steps[lo][0]).total_seconds() / 60.0 > horizon:
            lo += 1
        pending = 0.0
        for at, delta in steps[lo:applied]:
            e = (t - at).total_seconds() / 60.0
            frac = 0.0 if e < theta else 1.0 - math.exp(-(e - theta) / max(tau, 1e-6))
            pending += delta * (1.0 - frac)
        out.append(total - pending - base)
    return out


def spline_basis(rows):
    """Piecewise-linear hats spanning the window — a partition of unity, so no
    separate intercept (one would be exactly collinear with their sum).

    The knots are spaced to land the last one *on* the final sample. Spacing them
    at a fixed KNOT_H instead leaves a trailing knot whose support falls entirely
    past the end of the data, and that column is identically zero — which is what
    makes the normal equations singular.
    """
    t0 = rows[0]["t"]
    span = (rows[-1]["t"] - t0).total_seconds() / 3600.0
    n = max(2, math.ceil(span / KNOT_H) + 1)
    step = span / (n - 1)
    return [[max(0.0, 1.0 - abs((r["t"] - t0).total_seconds() / 3600.0 - j * step) / step)
             for j in range(n)] for r in rows]


def fit_dynamics(rows, sp_steps, bl_steps, use_blower: bool):
    """Grid-search (θ, τ); return the best fit's dynamics and gains."""
    sk = spline_basis(rows)
    ys = [r["y"] for r in rows]
    keep = [i for i, r in enumerate(rows)
            if (r["t"] - rows[0]["t"]).total_seconds() / 60.0 >= WARMUP_MIN]
    best = None
    for theta in THETA_GRID:
        for tau in TAU_GRID:
            s = response(rows, sp_steps, theta, tau, 0.0)
            b = response(rows, bl_steps, theta, tau, 0.0) if use_blower else None
            x = [sk[i] + ([s[i], b[i]] if use_blower else [s[i]]) for i in keep]
            y = [ys[i] for i in keep]
            coef = lstsq(x, y)
            if coef is None:
                continue
            r2 = score(x, y, coef)
            if best is None or r2 > best["r2"]:
                best = {"theta": float(theta), "tau": float(tau), "r2": r2,
                        "gain": coef[-2] if use_blower else coef[-1],
                        "k_blower": coef[-1] if use_blower else None,
                        "n": len(keep)}
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--tau-c-mult", type=float, default=1.5,
                    help="SIMC closed-loop time constant, as a multiple of the dead time")
    a = ap.parse_args()

    t1 = datetime.now(timezone.utc)
    t0 = t1 - timedelta(days=a.days)
    print(f"window   {t0.isoformat()} → {t1.isoformat()}", file=sys.stderr)
    hist = fetch(t0, t1, [COMFORT, CLIMATE, WEATHER, TARGET])
    rows, sp_steps, bl_steps = build_grid(t0, t1, hist)
    if not rows:
        print("not enough history — need comfort, outdoor and setpoint all present")
        return 1
    use_blower = len(bl_steps) >= 20
    print(f"\ngrid     {len(rows)} samples at {GRID_S}s over "
          f"{(t1 - t0).total_seconds() / 86400:.1f} days")
    print(f"         {len(sp_steps)} setpoint transitions, {len(bl_steps)} blower transitions"
          + ("" if use_blower else "  → too few to identify the blower"))

    best = fit_dynamics(rows, sp_steps, bl_steps, use_blower)
    if best is None:
        print("the design matrix is singular — no fit")
        return 1

    print(f"\nA. DYNAMICS + GAIN     R² = {best['r2']:.3f} over {best['n']} samples")
    print(f"   dead time θ   {best['theta']:5.1f} min")
    print(f"   time const τ  {best['tau']:5.1f} min")
    print(f"   plant gain K  {best['gain']:6.3f} °C comfort per °C setpoint")
    edge = []
    if best["theta"] in (THETA_GRID[0], THETA_GRID[-1]):
        edge.append("θ")
    if best["tau"] in (TAU_GRID[0], TAU_GRID[-1]):
        edge.append("τ")
    if edge:
        print(f"   ⚠ {'/'.join(edge)} landed on the edge of the search grid — widen it")

    g = None
    if best["k_blower"] is not None:
        print(f"   blower gain   {best['k_blower']:6.3f} °C comfort per blower level")
        if best["gain"] > 1e-6:
            g = -best["k_blower"] / best["gain"]
            print(f"   → blower authority g = {g:.3f} °C of equivalent setpoint per level")
            if g < 0:
                print("     (negative: a higher blower measured WARMER — not a cooling lever)")

    # per-day refits: an honest error bar, since the residuals are autocorrelated
    days, day_gains = [], []
    cur = t0
    while cur < t1:
        end = min(cur + timedelta(hours=24), t1)
        sub = [r for r in rows if cur <= r["t"] < end]
        if len(sub) > 200:
            ss = [(t, d) for t, d in sp_steps if cur <= t < end]
            bs = [(t, d) for t, d in bl_steps if cur <= t < end]
            f = fit_dynamics(sub, ss, bs, use_blower and len(bs) >= 10)
            if f:
                days.append((cur, f))
                day_gains.append(f["gain"])
        cur = end
    if day_gains:
        print(f"\n   per-day gain: " + "  ".join(f"{v:+.2f}" for v in sorted(day_gains)))
        print(f"   median {statistics.median(day_gains):.3f}"
              + (f", sd {statistics.stdev(day_gains):.3f}" if len(day_gains) > 1 else ""))

    if best["gain"] <= 0.05:
        print("\n   ⚠ THE GAIN IS NOT IDENTIFIED. Everything below depends on it.")
        print("     A gain at or below zero means lowering the setpoint did not measurably")
        print("     cool the room in this window. Do not ship these numbers.")

    # --- B. the feedforward, learned from the old controller's own output ---
    # Not via the plant inverse: that needs the gain, and the gain is the one thing
    # this data will not give up. What the feedforward has to predict is "the
    # setpoint this load calls for", and closed-loop history answers that directly —
    # whenever the room WAS on target, the setpoint in force is the right answer by
    # construction. Regressing the controller's own output on an exogenous input is
    # safe in a way that regressing the room on the controller's output is not.
    # Only ONE number is fitted here. The target's coefficient is pinned by physics:
    # steady state is y = c0 + c_out·T_out + K·sp, so holding the room 1 °C warmer
    # takes exactly 1/K °C of setpoint. Fitting it instead gave 0.26 — read off a
    # target that never moved more than 0.5 °C all week, and badly wrong.
    #
    # And the censoring is repaired rather than dropped. Restricting to samples that
    # were both on target and off the setpoint floor discards 44% of the on-target
    # time, all of it the hottest, which truncates exactly the range the outdoor
    # slope is measured over. Instead every sample is corrected to the setpoint that
    # WOULD have held the room on target,
    #
    #     demand = setpoint − (comfort − target)/K
    #
    # which is a valid reading of the load whether or not the setpoint was pinned —
    # a room sitting 0.8 °C warm at a floor of 24 is evidence the load wanted 22.4.
    # It costs a dependence on the assumed K, which scales the slope but cannot flip it.
    kk = PRIOR_GAIN
    work = [r for r in rows
            if r["target"] is not None and SP_LIMITS[0] <= r["sp"] <= SP_LIMITS[1]]
    print(f"\nB. FEEDFORWARD         {len(work)} samples inside the setpoint envelope"
          f"  (K assumed {kk})")
    if len(work) < 100:
        print("   too few to fit")
        return 1
    x = [[1.0, r["outdoor"]] for r in work]
    ys = [r["sp"] - (r["y"] - r["target"]) / kk - r["target"] / kk for r in work]
    coef = lstsq(x, ys)
    if coef is None:
        print("   singular")
        return 1
    print(f"   setpoint = {coef[0]:+.3f} {coef[1]:+.4f}·T_outdoor "
          f"{1 / kk:+.3f}·target     R² = {score(x, ys, coef):.3f}")
    print(f"   outdoor span {min(r['outdoor'] for r in work):.1f}–"
          f"{max(r['outdoor'] for r in work):.1f} °C")
    print(f"   → a 10 °C hotter day asks for {10 * coef[1]:+.2f} °C of setpoint")
    # Plausible range, in °C of SETPOINT: the envelope moves the room by roughly
    # 0.05–0.25 °C per outdoor °C at a fixed setpoint, and each of those costs 1/K
    # of setpoint to cancel.
    if not -5.0 <= 10 * coef[1] <= -1.0:
        print(f"   ⚠ outside the physically plausible −1.0…−5.0 °C per 10 °C "
              f"(0.05–0.25 °C of room per outdoor °C, at 1/K each)")

    # --- C. hour of day ----------------------------------------------------
    resid = [(r["t"], ys[i] - sum(c * v for c, v in zip(coef, x[i])))
             for i, r in enumerate(work)]
    by_hour: dict[int, list[float]] = {}
    for t, e in resid:
        by_hour.setdefault(t.astimezone().hour, []).append(e)
    allv = [e for _, e in resid]
    var = statistics.pvariance(allv)
    within = sum(statistics.pvariance(v) * len(v) for v in by_hour.values() if len(v) > 1)
    within /= max(sum(len(v) for v in by_hour.values() if len(v) > 1), 1)
    explained = (var - within) / var if var > 0 else 0.0
    means = [statistics.fmean(by_hour[h]) for h in sorted(by_hour)]
    swing = max(means) - min(means)
    # A real load term is SMOOTH: the sun and the envelope do not reverse between
    # one hour and the next. Roughness is the mean hour-to-hour jump against the
    # whole swing — about 1/12 for a clean daily cycle, and near 1/2 for noise.
    jumps = [abs(b - a) for a, b in zip(means, means[1:] + means[:1])]
    rough = statistics.fmean(jumps) / swing if swing > 0 else 1.0
    print(f"\nC. HOUR OF DAY         explains {100 * explained:.1f}% of the setpoint "
          f"the outdoor term did not")
    print(f"   residual sd {math.sqrt(var):.3f} °C of setpoint, "
          f"peak-to-peak swing {swing:.2f} °C")
    print(f"   roughness {rough:.2f}   (a clean daily cycle scores 0.08; "
          f"hour-means of pure noise score 0.29)")
    for h in sorted(by_hour):
        m = statistics.fmean(by_hour[h])
        print(f"     {h:02d}:00  {m:+.3f} °C  (n={len(by_hour[h]):5d})  "
              + "█" * int(abs(m) * 20))
    print("   A load term must be SMOOTH — the sun and the envelope do not reverse")
    print("   between one hour and the next. Read the two numbers above together:")
    print("   a large swing with a noise-like roughness is the old controller behaving")
    print("   differently at different hours, not a load, and keying a feedforward on")
    print("   it would bake that behaviour in.")

    # --- what to actually ship ---------------------------------------------
    theta, tau, gain = best["theta"], best["tau"], best["gain"]
    weak = gain < 0.4 or theta <= THETA_GRID[1] or (best["k_blower"] or 0) > 0
    print("\n" + "=" * 70)
    if weak:
        print("STAGE A IS NOT TRUSTWORTHY — do not ship its constants.")
        print("  Closed-loop history cannot separate the plant's response to the")
        print("  setpoint from the controller's response to the room. The blower")
        print("  coefficient measuring POSITIVE (a higher blower reading warmer) is")
        print("  the proof: that is the old controller's rule, not the plant.")
        print("  Ship the conservative priors instead. A gain assumed HIGH gives a")
        print("  SMALLER Kc, so of the plausible range the safe end is the top —")
        print("  guessing low is what produces an over-aggressive loop.")
        gain, theta, tau = PRIOR_GAIN, PRIOR_DEAD_TIME, PRIOR_TAU
    tau_c = a.tau_c_mult * theta
    print(f"\nSIMC   K {gain:.2f}  θ {theta:.0f}  τ {tau:.0f}  τ_c {tau_c:.0f} min"
          f"   →   Kc {tau / (gain * (tau_c + theta)):.3f}   "
          f"Ti {min(tau, 4.0 * (tau_c + theta)):.1f} min")
    print("\n--- for const.MODEL_DEFAULTS ---")
    print(f"    MK_GAIN: {gain:.3f},")
    print(f"    MK_DEAD_TIME: {theta:.1f},")
    print(f"    MK_TAU: {tau:.1f},")
    print(f"    MK_FF_INTERCEPT: {coef[0]:.3f},")
    print(f"    MK_FF_PER_OUTDOOR: {coef[1]:.4f},")
    print(f"    MK_FF_PER_TARGET: {1 / gain:.3f},")
    print(f"    MK_BLOWER_GAIN: 0.0,"
          + ("   # not identifiable from closed-loop history" if weak else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
