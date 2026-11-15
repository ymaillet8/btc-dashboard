#!/usr/bin/env python3
"""
episode_matrix.py -- part 2 of the REPORT-ONLY episode analysis (see episode_analysis.py
for the data layer and Step 1). Assembles the day-by-day matrix from zero-quota sources and
reuses the real indicator functions via backtest_indicators' builders; reimplements no
indicator logic. Zero BGeometrics API calls; no BGeometrics chart/data files fetched.
"""
import bisect
import csv
import html as _html
import os
import re
import subprocess
import time
from datetime import date, timedelta

import update_dashboard as ud
import backtest_indicators as bt
import episode_analysis as ea

HERE, OUT, CACHE = ea.HERE, ea.OUT, ea.CACHE

# ---------------------------------------------------------------------------
# Indicator catalogue (Table 1 / Cycle Bottom): 17 weighted + Realized Price + Drawdown
# ---------------------------------------------------------------------------
BG_TOKENS = ("MVRV_Z", "PUELL", "RESERVE_RISK", "LTH_SOPR", "THERMOCAP", "NRPL",
             "ASOPR_EST", "SUPPLY_LOSS", "REALIZED_PRICE")      # the nine BGeometrics-API-sourced series
BG_CHART_FILE_TOKENS = ("ACTIVE_ADDR_DEV",)                      # BGeometrics chart file: NOT fetched here
NONBG_TOKENS = ("NVT_GC", "PROD_COST", "MINER_CAP", "MAYER", "RSI", "FNG", "BOLLINGER", "MACD", "DRAWDOWN")
ALL_TOKENS = ("MVRV_Z", "PUELL", "RESERVE_RISK", "LTH_SOPR", "THERMOCAP", "NRPL", "ASOPR_EST", "SUPPLY_LOSS",
              "REALIZED_PRICE", "ACTIVE_ADDR_DEV", "FNG", "RSI", "MACD", "BOLLINGER", "MAYER", "DRAWDOWN",
              "NVT_GC", "MINER_CAP", "PROD_COST")
WEIGHT_JOIN = {"THERMOCAP": 1.5, "NRPL": 1.5, "ACTIVE_ADDR_DEV": 1.5}   # runtime-gated joins
NAME = dict(bt.TABLE1_COLUMNS)
DIRECTION = {"NVT_GC": "low", "PROD_COST": "low", "MAYER": "low", "RSI": "low", "FNG": "low",
             "BOLLINGER": "low", "MACD": "high", "DRAWDOWN": "low"}
FAV = ("STRONG BUY", "BUY", "BORDERLINE")


def weight_of(tok):
    return ud.WEIGHT_MAP.get(tok, WEIGHT_JOIN.get(tok))


def tier_of(label, css):
    return bt.normalize_tier(label, css)


# ---------------------------------------------------------------------------
# Fast trailing window: same semantics as bt._trailing_window, bisect instead of a full scan
# (glue only; the indicator maths is untouched).
# ---------------------------------------------------------------------------
_WC = {}


def _fast_window(series, end_date, window_days):
    k = id(series)
    if k not in _WC:
        _WC[k] = ([d for d, _ in series], [v for _, v in series])
    ds, vs = _WC[k]
    lo = bisect.bisect_left(ds, end_date - timedelta(days=window_days - 1))
    hi = bisect.bisect_right(ds, end_date)
    return vs[lo:hi]


# ---------------------------------------------------------------------------
# Fresh walk-forward for the zero-quota indicators. A recorder wrapped around the REAL
# ud.status_pill captures threshold + sigma; the real function still classifies every day.
# ---------------------------------------------------------------------------
def compute_fresh(price, hashrate, txvol, fng):
    import pickle
    pk = f"{CACHE}/fresh.pkl"
    if os.path.exists(pk):
        return pickle.load(open(pk, "rb"))
    bt._trailing_window = _fast_window
    orig_pill = ud.status_pill
    REC = []

    def rec_pill(value, direction, threshold, cache=None, token=None):
        label, css = orig_pill(value, direction, threshold, cache=cache, token=token)
        sigma = None
        if cache is not None and token is not None and threshold is not None:
            try:
                sigma, _ = ud.get_effective_sigma(cache, token, threshold)
            except Exception:
                sigma = None
        REC.append((token, value, direction, threshold, sigma, label, css))
        return label, css

    ud.status_pill = rec_pill
    out = {}
    builds = {
        "FNG": lambda: bt.build_fng(fng),
        "RSI": lambda: bt.build_price_derived("Weekly RSI", "RSI", price, "low", 30.0, ud.compute_weekly_rsi),
        "MACD": lambda: bt.build_price_derived("MACD (weekly)", "MACD", price, "high", 0.0,
                                               lambda w: ud.compute_weekly_macd(w)[0]),
        "BOLLINGER": lambda: bt.build_price_derived("Bollinger %B", "BOLLINGER", price, "low", 0.2, ud.compute_bollinger),
        "MAYER": lambda: bt.build_price_derived("Mayer Multiple", "MAYER", price, "low", 1.0,
                                                lambda w: ud.compute_mayer_and_drawdown(w)[0]),
        "DRAWDOWN": lambda: bt.build_price_derived("Drawdown Magnitude", "DRAWDOWN", price, "low", -77.0,
                                                   lambda w: ud.compute_mayer_and_drawdown(w)[1]),
        "NVT_GC": lambda: bt.build_nvt_gc(price, txvol),
        "MINER_CAP": lambda: bt.build_miner_capitulation(hashrate, price),
        "PROD_COST": lambda: bt.build_production_cost(hashrate, price),
    }
    try:
        for tok, fn in builds.items():
            t0 = time.time()
            REC.clear()
            built = fn()
            recs = list(REC)
            ptr, days = 0, {}
            for d, v, label, css in built["statuses"]:
                r = None
                if ptr < len(recs) and recs[ptr][1] == v and recs[ptr][5] == label:
                    r = recs[ptr]
                    ptr += 1
                days[d] = dict(v=v, label=label, css=css, tier=tier_of(label, css),
                               thr=(r[3] if r else None), sigma=(r[4] if r else None),
                               direction=(r[2] if r else DIRECTION.get(tok)))
            out[tok] = days
            print(f"  fresh {tok}: {len(days)} days, {ptr}/{len(recs)} recorder rows aligned, {time.time()-t0:.1f}s")
    finally:
        ud.status_pill = orig_pill
    pickle.dump(out, open(pk, "wb"))
    return out


# ---------------------------------------------------------------------------
# Existing daily-matrix CSV (Jun-Jul): tiers only
# ---------------------------------------------------------------------------
CSV_PATH = os.path.join(OUT, "daily_indicator_matrix_2026-06-01_to_2026-08-18.csv")
CSV_MONTHLY = [os.path.join(OUT, "daily_indicator_matrix_2026-06-01_to_2026-06-30.csv"),
               os.path.join(OUT, "daily_indicator_matrix_2026-07-01_to_2026-07-31.csv")]


def _read_matrix_csv(path):
    rows = list(csv.reader(open(path, encoding="utf-8", newline="")))
    hdr = rows[1]
    idx = {h: i for i, h in enumerate(hdr)}
    name_to_tok = {n: k for k, n in bt.TABLE1_COLUMNS}
    out = {}
    for r in rows[2:]:
        if not r or not r[0] or r[0].startswith("TOTAL"):
            continue
        d = date.fromisoformat(r[0])
        tiers = {tok: r[idx[n]] for n, tok in name_to_tok.items() if n in idx}
        pct = dict(CB=r[idx["Cycle Bottom Weighted %"]], MS=r[idx["Momentum Shift Weighted %"]],
                   CT=r[idx["Cycle Top Weighted %"]])
        out[d] = dict(tiers=tiers, pct=pct)
    return out


def load_csv_tiers():
    """The combined 06-01..08-18 file had four BGeometrics fetches fail with HTTP 429 (LTH-SOPR, NRPL,
    aSOPR/SOPR, Supply-in-Loss -> N/A on every day), so the separate, complete June and July files take
    precedence for Jun 1 - Jul 31; the combined file supplies Aug 1+ (with those four as a data gap)."""
    out = _read_matrix_csv(CSV_PATH)
    for p in CSV_MONTHLY:
        out.update(_read_matrix_csv(p))
    return out


# ---------------------------------------------------------------------------
# Committed dashboard.html history
# ---------------------------------------------------------------------------
HTML_NAME2TOK = {
    "MVRV Z-Score": "MVRV_Z", "Puell Multiple": "PUELL", "Reserve Risk": "RESERVE_RISK",
    "Production Cost": "PROD_COST", "LTH-SOPR": "LTH_SOPR", "NVT Golden Cross": "NVT_GC",
    "Thermocap Multiple": "THERMOCAP", "NRPL (Net Realized P&L)": "NRPL",
    "Active Addresses Power-Law Deviation": "ACTIVE_ADDR_DEV", "aSOPR (modeled)": "ASOPR_EST",
    "Miner Capitulation": "MINER_CAP", "MACD (weekly)": "MACD", "% Supply in Loss": "SUPPLY_LOSS",
    "Mayer Multiple": "MAYER", "Weekly RSI": "RSI", "Fear & Greed": "FNG", "Bollinger %B": "BOLLINGER",
    "Price vs. Realized Price": "REALIZED_PRICE", "Drawdown Magnitude": "DRAWDOWN",
}


def _git(*args):
    return subprocess.run(["git", "-C", HERE] + list(args), capture_output=True, text=True,
                          encoding="utf-8").stdout


def html_commits():
    """date (from the 'Auto-update dashboard YYYY-MM-DD' subject) -> latest commit for that date."""
    out = {}
    for line in _git("log", "--format=%H|%s", "--", "dashboard.html").splitlines():
        h, subj = line.split("|", 1)
        m = re.search(r"(\d{4}-\d{2}-\d{2})", subj)
        if m:
            out.setdefault(date.fromisoformat(m.group(1)), h)     # newest-first -> first seen = latest
    return out


def _num(s):
    s = s.replace("−", "-").replace(",", "").replace("$", "")
    m = re.search(r"-?\d+(?:\.\d+)?(?:e-?\d+)?", s)
    return float(m.group(0)) if m else None


def parse_dashboard_html(txt):
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", txt, flags=re.S)
    res, seen = {}, set()
    for r in rows:
        if "ind-name" not in r:
            continue
        nm = re.search(r'<td class="ind-name">(.*?)(?:<span class="tip-wrap"|<span class="caveat"|<span class="anchor|</td>)',
                       r, flags=re.S)
        if not nm:
            continue
        name = _html.unescape(re.sub(r"<[^>]+>", "", nm.group(1))).strip()
        tok = HTML_NAME2TOK.get(name)
        if tok is None or tok in seen:
            continue
        cells = [_html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
                 for _, c in re.findall(r'<td class="(reading|target)">(.*?)</td>', r, flags=re.S)]
        pill = re.search(r'<span class="status-pill ([a-z-]+)">(.*?)</span>', r, flags=re.S)
        if len(cells) < 3 or not pill:
            continue
        seen.add(tok)
        css, label = pill.group(1), _html.unescape(pill.group(2))
        tgt = cells[2]
        res[tok] = dict(reading_txt=cells[0], reading=_num(cells[0]), weight_txt=cells[1], target_txt=tgt,
                        thr=(_num(tgt.split("(")[0]) if tgt and not tgt.startswith("N/A") else None),
                        label=label, css=css, tier=tier_of(label, css))
    t = re.sub(r"<(script|style).*?</\1>", "", txt, flags=re.S)
    t = _html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "|", t)))
    pct = {}
    m = re.search(r"(\d+)/(\d+) of today's scoreable indicators are buy-favorable \(([\d.]+)% of scored weight\)", t)
    pct["CB"] = float(m.group(3)) if m else None
    m = re.search(r"of today's scoreable momentum indicators[^(]*\(([\d.]+)% of scored weight\)", t)
    pct["MS"] = float(m.group(1)) if m else None
    m = re.search(r"of today's scoreable top-side indicators[^(]*\(([\d.]+)% of scored weight\)", t)
    pct["CT"] = float(m.group(1)) if m else None
    m = re.search(r"(\d+)/(\d+) fired", t)
    return dict(rows=res, pct=pct, fired=(int(m.group(1)), int(m.group(2))) if m else None)


def load_html_day(commit):
    return parse_dashboard_html(_git("show", f"{commit}:dashboard.html"))
