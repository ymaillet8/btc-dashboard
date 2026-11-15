#!/usr/bin/env python3
"""
episode_analysis.py -- REPORT-ONLY analysis: how every Cycle Bottom indicator
behaved while BTC traded between $57K and $64K (2026 episodes), and whether
silence was "threshold too strict" or "indicator not extreme at this low".

NOT part of the daily pipeline. Changes no thresholds, weights or scoring.
Makes ZERO BGeometrics API calls and fetches NO BGeometrics chart/data files
(their Terms prohibit bypassing rate limits / bulk harvesting). Every
indicator computation reuses the real functions in update_dashboard.py /
the existing backtest_indicators.py builders; nothing is reimplemented here.

Sources (all non-BGeometrics): Blockchain.com daily charts (market-price,
hash-rate, estimated-transaction-volume-usd), Alternative.me Fear & Greed,
Binance BTCUSDT daily klines (intraday low only), the committed
dashboard.html history (git), and the existing daily-matrix CSVs.

Usage:  python episode_analysis.py fetch      # cache non-BG source data
        python episode_analysis.py step1      # price episodes
        python episode_analysis.py all        # everything, writes CSV/MD
"""
import csv
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

import update_dashboard as ud
import backtest_indicators as bt

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "backtest_output")
CACHE = os.path.join(OUT, "episode_cache")

RANGE_LO, RANGE_HI = 57000.0, 64000.0
SCOPE_START = date(2026, 5, 15)          # episodes listed from here on (see report note)
HIST_START = date(2011, 1, 1)


# ---------------------------------------------------------------------------
# Data layer (zero BGeometrics)
# ---------------------------------------------------------------------------
def _bc_chunked(chart, start=HIST_START, chunk_days=1900):
    """Blockchain.com daily series via start=/timespan= chunks (daily
    granularity holds up to ~2000 days per call; longer spans get
    downsampled, so never request more than chunk_days at once)."""
    out = {}
    cur = start
    today = datetime.now(timezone.utc).date()
    while cur <= today:
        url = (f"https://api.blockchain.info/charts/{chart}?start={cur.isoformat()}"
               f"&timespan={chunk_days}days&format=json&cors=true")
        data = ud._get_json(url)
        for pt in data.get("values", []):
            y = pt.get("y")
            if y is None:
                continue
            out[datetime.fromtimestamp(pt["x"], timezone.utc).date()] = float(y)
        cur += timedelta(days=chunk_days)
        time.sleep(1.0)
    return sorted(out.items())


def _binance_daily(start=date(2018, 1, 1)):
    rows, t = [], int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp() * 1000)
    while True:
        url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&startTime={t}&limit=1000"
        r = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "btc-dashboard-bot/1.0"}), timeout=20).read())
        if not r:
            break
        for k in r:
            d = datetime.fromtimestamp(k[0] / 1000, timezone.utc).date()
            rows.append((d.isoformat(), float(k[1]), float(k[2]), float(k[3]), float(k[4])))
        t = r[-1][0] + 86400000
        if len(r) < 1000:
            break
        time.sleep(0.5)
    return rows


def fetch_all():
    os.makedirs(CACHE, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    for name, chart in (("market_price", "market-price"), ("hash_rate", "hash-rate"),
                        ("tx_volume", "estimated-transaction-volume-usd")):
        s = _bc_chunked(chart)
        json.dump({"fetched_at_utc": stamp, "source": f"api.blockchain.info/charts/{chart}",
                   "series": [[d.isoformat(), v] for d, v in s]}, open(f"{CACHE}/{name}.json", "w"))
        print(f"{name}: n={len(s)} {s[0][0]}..{s[-1][0]}")
    fng = bt.fetch_fng_range()
    json.dump({"fetched_at_utc": stamp, "source": "api.alternative.me/fng limit=0",
               "series": [[d.isoformat(), v] for d, v in fng]}, open(f"{CACHE}/fng.json", "w"))
    print(f"fng: n={len(fng)} {fng[0][0]}..{fng[-1][0]}")
    kl = _binance_daily()
    json.dump({"fetched_at_utc": stamp, "source": "api.binance.com klines BTCUSDT 1d",
               "rows": kl}, open(f"{CACHE}/binance_1d.json", "w"))
    print(f"binance: n={len(kl)} {kl[0][0]}..{kl[-1][0]}")


def load_series(name):
    j = json.load(open(f"{CACHE}/{name}.json"))
    return [(date.fromisoformat(d), float(v)) for d, v in j["series"]]


def load_binance():
    j = json.load(open(f"{CACHE}/binance_1d.json"))
    return {date.fromisoformat(r[0]): dict(o=r[1], h=r[2], l=r[3], c=r[4]) for r in j["rows"]}


# ---------------------------------------------------------------------------
# STEP 1 -- price episodes
# ---------------------------------------------------------------------------
def find_episodes(price, lo=RANGE_LO, hi=RANGE_HI, since=SCOPE_START):
    eps, cur = [], None
    for d, p in price:
        if d < since:
            continue
        inr = lo <= p <= hi
        if inr:
            if cur is None:
                cur = [d, d, [(d, p)]]
            else:
                if (d - cur[1]).days == 1:
                    cur[1] = d
                    cur[2].append((d, p))
                else:
                    eps.append(cur)
                    cur = [d, d, [(d, p)]]
        else:
            if cur is not None:
                eps.append(cur)
                cur = None
    if cur is not None:
        eps.append(cur)
    out = []
    for s, e, pts in eps:
        low = min(pts, key=lambda t: t[1])
        out.append(dict(start=s, end=e, days=len(pts), low_close=low[1], low_date=low[0],
                        in_progress=(e >= price[-1][0] - timedelta(days=1))))
    return out


def step1():
    price = load_series("market_price")
    bn = load_binance()
    eps = find_episodes(price)
    last_day = price[-1][0]
    print(f"Blockchain.com market-price: {price[0][0]}..{last_day}, n={len(price)} (last day={last_day}, close incomplete)")
    print("\nSTEP 1 -- episodes with daily close within $57,000-$64,000 (scope: from", SCOPE_START, ")")
    print(f"{'#':>2} {'start':<11}{'end':<11}{'days':>5} {'lowest close':>13}  {'date':<11} note")
    for i, e in enumerate(eps, 1):
        note = "IN PROGRESS (last close incomplete)" if e["in_progress"] else ""
        print(f"{i:>2} {e['start']!s:<11}{e['end']!s:<11}{e['days']:>5} {e['low_close']:>13,.2f}  {e['low_date']!s:<11} {note}")
    overall = min(((d, p) for d, p in price if d >= SCOPE_START), key=lambda t: t[1])
    pct = (overall[1] / 57700 - 1) * 100
    print(f"\nLowest Blockchain.com close in scope: ${overall[1]:,.2f} on {overall[0]} ({pct:+.2f}% vs ~$57.7K reference)")
    jl = [(d, p) for d, p in price if date(2026, 6, 25) <= d <= date(2026, 7, 8)]
    print("1-Jul region closes:", [(d.isoformat(), round(p)) for d, p in jl if d <= date(2026, 7, 4)])
    # intraday-low-only days (Binance BTCUSDT) -- low in range, close outside
    pr = dict(price)
    rows = []
    for d in sorted(bn):
        if d < SCOPE_START or d not in pr:
            continue
        b = bn[d]
        if RANGE_LO <= b["l"] <= RANGE_HI and not (RANGE_LO <= pr[d] <= RANGE_HI):
            rows.append((d, b["l"], pr[d]))
    print(f"\nDays where Binance intraday LOW entered the range but the Blockchain.com close did not: {len(rows)}")
    for d, l, c in rows:
        print(f"  {d}  intraday low ${l:,.0f}  close ${c:,.0f}")
    lowb = min(((d, bn[d]['l']) for d in bn if d >= SCOPE_START), key=lambda t: t[1])
    print(f"Lowest Binance intraday low in scope: ${lowb[1]:,.2f} on {lowb[0]} (Coinbase ref $57,717.55, Bloomberg $57,742)")
    return eps, pr, bn, rows


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "step1"
    if cmd == "fetch":
        fetch_all()
    elif cmd == "step1":
        step1()
