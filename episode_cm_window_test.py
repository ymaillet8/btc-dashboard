#!/usr/bin/env python3
"""REPORT ONLY: MVRV adaptive-threshold window/percentile sensitivity on Coin Metrics-derived Z.
Walk-forward (each day's threshold uses only data up to that day). Changes nothing."""
import sys
from datetime import date
import update_dashboard as ud
import episode_cm_report as e

D = e.load(); Z = D["Z"]; P = D["price"]
ds = [d for d in sorted(Z) if d >= e.RANK_START]
zs = [Z[d] for d in ds]
LOWS = [("Dec 2018", e.low_day(P, date(2018, 11, 15), date(2018, 12, 31))),
        ("Nov 2022", e.low_day(P, date(2022, 11, 1), date(2022, 11, 30))),
        ("Jul 2026", e.low_day(P, date(2026, 6, 25), date(2026, 7, 8)))]
idx = {d: i for i, d in enumerate(ds)}


def thr_at(i, win, pct):
    lo = 0 if win is None else max(0, i + 1 - win)
    vals = zs[lo:i + 1]
    return ud.compute_percentile(vals, pct) if len(vals) >= 365 else None


print("Z at lows:", {l: round(Z[d], 3) for l, d in LOWS}, "| latest", D["last"], round(Z[D["last"]], 3))
print(f"{'window':>7} {'pct':>4} {'thr today':>10} {'fires today':>11} | " + " | ".join(f"{l} thr/fire" for l, _ in LOWS) + " | false-fire")
for win in (730, 1460, None):
    for pct in (5, 7.5, 10):
        today = thr_at(len(ds) - 1, win, pct)
        row = []
        for l, d in LOWS:
            t = thr_at(idx[d], win, pct)
            row.append(f"{t:+.3f}/{'Y' if Z[d] <= t else 'n'}")
        fire = {}
        for i, d in enumerate(ds):
            t = thr_at(i, win, pct)
            fire[d] = (t is not None) and zs[i] <= t
        a, n = e.false_fire(P, fire)
        print(f"{str(win or 'full'):>7} {pct:>4} {today:>+10.3f} {'YES' if zs[-1] <= today else 'no':>11} | " + " | ".join(row) + f" | {a}/{n} ({100*a/n:.1f}%)")
