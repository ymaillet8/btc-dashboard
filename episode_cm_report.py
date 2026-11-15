#!/usr/bin/env python3
"""
episode_cm_report.py -- REPORT ONLY. Task 4 of the Nov-2026 review: MVRV Z, Puell and
Price-vs-Realized-Price evaluated at the Dec 2018 / Nov 2022 / Jul 2026 lows (and the current
episode) using series DERIVED FROM COIN METRICS COMMUNITY DATA (cached in
backtest_output/coinmetrics_cache/btc_1d.json). Zero BGeometrics calls; changes no thresholds,
weights or scoring. Appends a section to backtest_output/episode_report_<date>.md.

Derivations (Coin Metrics free tier has no CapRealUSD):
  realized cap = CapMrktCurUSD / CapMVRVCur ; Z = (MC - RC) / expanding population std(MC)
  realized price = RC / SplyCur ; Puell = IssTotUSD / mean(IssTotUSD over trailing 365d incl. today)
Tier approximation: "fires" = value at/below the production threshold (the near-band and
strong-buy refinements are ignored; they only matter within a few % of the threshold).
Production thresholds: Z = adaptive 7.5th percentile of own trailing 730d (min 365d);
Puell <= 0.5 fixed; Price-vs-RP: spot < realized price (MVRV ratio < 1; shown, not scored).
"""
import json
import math
import os
import sys
from collections import deque
from datetime import date, timedelta

import update_dashboard as ud

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "backtest_output")
CM = os.path.join(OUT, "coinmetrics_cache", "btc_1d.json")
MARK = "## Addendum (Task 4)"
RANK_START = date(2012, 1, 1)      # skip the 2010-11 warm-up where expanding sigma / 365d means are degenerate


def load():
    with open(CM, encoding="utf-8") as f:
        j = json.load(f)
    R = {}
    for r in j["rows"]:
        d = date.fromisoformat(r["time"][:10])
        R[d] = {k: (float(r[k]) if r.get(k) not in (None, "") else None)
                for k in ("CapMVRVCur", "CapMrktCurUSD", "IssTotUSD", "SplyCur")}
    ds = sorted(d for d in R if R[d]["CapMVRVCur"] and R[d]["CapMrktCurUSD"] and R[d]["SplyCur"])
    MC = {d: R[d]["CapMrktCurUSD"] for d in ds}
    RC = {d: MC[d] / R[d]["CapMVRVCur"] for d in ds}
    price = {d: MC[d] / R[d]["SplyCur"] for d in ds}            # implied spot (MC / supply)
    rp = {d: RC[d] / R[d]["SplyCur"] for d in ds}
    n = s = s2 = 0.0
    Z = {}
    for d in ds:
        x = MC[d]
        n += 1; s += x; s2 += x * x
        if n > 30:
            sd = math.sqrt(max((s2 - s * s / n) / n, 0.0))
            if sd:
                Z[d] = (MC[d] - RC[d]) / sd
    iss = {d: R[d]["IssTotUSD"] for d in R if R[d]["IssTotUSD"] is not None}
    isd = sorted(iss)
    puell = {}
    for i, d in enumerate(isd):
        if i >= 364 and (isd[i] - isd[i - 364]).days == 364:
            puell[d] = iss[d] / (sum(iss[x] for x in isd[i - 364:i + 1]) / 365)
    ratio = {d: price[d] / rp[d] for d in ds}                    # spot / realized price (= MVRV)
    return dict(price=price, Z=Z, PUELL=puell, RATIO=ratio, RP=rp, last=ds[-1])


def adaptive_thr(Z, d):
    """Production rule: 7.5th pct of the trailing 730 Z values (needs >=365). Walk-forward."""
    vals = [Z[x] for x in sorted(Z) if x <= d][-730:]
    if len(vals) < 365:
        return None
    return ud.compute_percentile(vals, ud.MVRV_ADAPTIVE_PERCENTILE)


def rank(series, d, v, hind=False):
    xs = [x for dd, x in series.items() if dd >= RANK_START and (hind or dd <= d)]
    return 100.0 * sum(1 for x in xs if x <= v) / len(xs), len(xs)


def klass(p):
    return "A" if p <= 10 else ("B" if p > 25 else "A/B-gap")


def low_day(price, a, b):
    return min(((d, p) for d, p in price.items() if a <= d <= b), key=lambda t: t[1])[0]


def false_fire(price, fire):   # same definition as episode_steps.false_fire_rate
    dates = sorted(price)
    dq, mn = deque(), {}
    for d in dates:
        while dq and dq[0][0] < d - timedelta(days=364):
            dq.popleft()
        while dq and dq[-1][1] >= price[d]:
            dq.pop()
        dq.append((d, price[d]))
        mn[d] = dq[0][1]
    elig = [d for d in dates if price[d] >= 1.5 * mn[d] and d >= date(2012, 1, 1) + timedelta(days=730) and d in fire]
    return sum(1 for d in elig if fire[d]), len(elig)


def main():
    D = load()
    P = D["price"]
    lows = [("Dec 2018", low_day(P, date(2018, 11, 15), date(2018, 12, 31))),
            ("Jun 2022 (same cycle as Nov 2022)", low_day(P, date(2022, 6, 1), date(2022, 7, 15))),
            ("Nov 2022", low_day(P, date(2022, 11, 1), date(2022, 11, 30))),
            ("Jul 2026", low_day(P, date(2026, 6, 25), date(2026, 7, 8)))]
    cur = [date(2026, 11, 13), date(2026, 11, 14)]
    L = []
    w = L.append
    w("\n\n---\n\n## Addendum (Task 4): MVRV Z, Puell, Price-vs-Realized-Price from Coin Metrics-derived series\n")
    w(f"Source: Coin Metrics Community API (`CapMVRVCur`, `CapMrktCurUSD`, `IssTotUSD`, `SplyCur`), cached; last row {D['last']} "
      "(1 day behind today). `CapRealUSD` is not available on the free tier, so realized cap = market cap / MVRV; "
      "Z uses the expanding population sigma of market cap (reproduces BGeometrics' 2026 values within ~0.03 Z on average, up to ~14% at worst); "
      "Puell is issuance / trailing-365d mean (incl. today). Price = Coin Metrics implied spot (market cap / supply), "
      "so dates are Coin Metrics days. Ranks use history from 2012-01-01; walk-forward = own history through the day, "
      "hindsight = full history. Tier approximated as value <= production threshold (near-band/strong-buy ignored).\n")
    w("Production thresholds replayed: **Z** = adaptive 7.5th pct of own trailing 730d (>=365d), **Puell** <= 0.5, "
      "**Price vs RP** = spot < realized price (MVRV ratio < 1; shown, not scored).\n")

    rows = []
    inds = [("MVRV Z", "Z", lambda d: adaptive_thr(D["Z"], d), 3),
            ("Puell", "PUELL", lambda d: 0.5, 3),
            ("Price/RP (MVRV ratio)", "RATIO", lambda d: 1.0, 0)]
    for label, d0 in lows + [("Current (CM day)", c) for c in cur]:
        pass

    for name, key, thr_fn, wt in inds:
        S = D[key]
        w(f"\n### {name}\n")
        w("| low / day | CM date | implied price | reading | threshold | fires? | walk-fwd rank | hindsight rank | class | required to fire (gap) |")
        w("|---|---|---|---|---|---|---|---|---|---|")
        for label, d in [(l, x) for l, x in lows] + [("Current episode", c) for c in cur]:
            v = S.get(d)
            if v is None:
                w(f"| {label} | {d} | ${P[d]:,.0f} | n/a | | | | | | |")
                continue
            thr = thr_fn(d)
            fires = (thr is not None) and v <= thr
            pw, nw = rank(S, d, v)
            ph, nh = rank(S, d, v, hind=True)
            k = "HIT" if fires else klass(pw)
            req = "" if fires or thr is None else f"threshold {thr:.3f} -> {v:.3f} ({(v - thr) / abs(thr) * 100:+.0f}% loosening)"
            w(f"| {label} | {d} | ${P[d]:,.0f} | {v:.3f} | {'n/a' if thr is None else f'{thr:.3f}'} | {'yes' if fires else 'no'} | "
              f"{pw:.1f}% (n={nw}) | {ph:.1f}% | {k} | {req} |")
    # false-fire rates: current rule vs a fixed level equal to the loosest reading at the three independent lows
    w("\n### Required threshold and false-fire rate (days >=1.5x trailing-365d min close, i.e. 'not near a bottom')\n")
    w("Fixed level = the highest reading among the Dec 2018 / Nov 2022 / Jul 2026 lows (the level that would have fired at all three).\n")
    w("| indicator | current rule: false-fire | fixed level needed | fixed-level false-fire | fires at all 3 lows under fixed level? |")
    w("|---|---|---|---|---|")
    ind3 = [d for l, d in lows if l in ("Dec 2018", "Nov 2022", "Jul 2026")]
    for name, key, thr_fn, wt in inds:
        S = D[key]
        cur_fire = {}
        for d in S:
            t = thr_fn(d)
            cur_fire[d] = (t is not None) and S[d] <= t
        a, n = false_fire(P, cur_fire)
        lvl = max(S[d] for d in ind3)
        fx = {d: S[d] <= lvl for d in S}
        a2, n2 = false_fire(P, fx)
        w(f"| {name} | {a}/{n} ({100 * a / n:.1f}%) | <= {lvl:.3f} | {a2}/{n2} ({100 * a2 / n2:.1f}%) | "
          f"{'yes' if all(S[d] <= lvl for d in ind3) else 'no'} |")
    w("\n**Independence / sample size.** Three independent lows (Dec 2018, Nov 2022, Jul 2026) -- Jun 2022 and Nov 2022 are one cycle -- "
      "plus one unresolved current episode (13-14 Nov 2026, no outcome yet). Any 'required threshold' fitted to three points is "
      "a description of those three lows, not a validated rule; every conclusion above rests on n=3 (n=1 for the current episode).\n")
    text = "\n".join(L)
    rpt = os.path.join(OUT, "episode_report_2026-11-15.md")
    if "--append" in sys.argv:       # idempotent: drop any earlier copy of this addendum first
        with open(rpt, encoding="utf-8") as f:
            cur_txt = f.read()
        cut = cur_txt.find(MARK)
        if cut != -1:
            cur_txt = cur_txt[:cut]
        with open(rpt, "w", encoding="utf-8") as f:
            f.write(cur_txt + text)
    sys.stdout.reconfigure(encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
