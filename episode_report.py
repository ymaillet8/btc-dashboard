#!/usr/bin/env python3
"""
episode_report.py -- part 3: matrix assembly (Step 2), per-indicator summary (Step 3),
miss diagnosis (Step 4), cross-cycle yardstick (Step 5), report (Step 6).
REPORT ONLY. Zero BGeometrics API calls; no BGeometrics chart/data files fetched. Does not
change thresholds, weights or scoring. All indicator maths = the real update_dashboard.py /
backtest_indicators.py functions (see episode_matrix.compute_fresh).
"""
import csv
import copy
import os
import sys
import time
from datetime import date, timedelta

import update_dashboard as ud
import backtest_indicators as bt
import episode_analysis as ea
import episode_matrix as em

OUT = ea.OUT
FAV = em.FAV
BG_DIR = {t: v[1] for t, v in ud.BG_METRICS.items()}
BG_DIR.update({"ASOPR_EST": "low", "SUPPLY_LOSS": "high", "REALIZED_PRICE": "low", "THERMOCAP": "low", "NRPL": "low",
               "SUPPLY_PROFIT": "high"})

MATRIX_START, MATRIX_END = date(2026, 6, 2), date(2026, 11, 15)
HTML_FIRST_FULL = date(2026, 8, 5)       # first day whose committed dashboard.html has the full parseable table
WINDOWS = {   # name -> (label, price-low day, days)
    "W1": ("Jun-Jul 2026 (eps 1-8, Jun 5 - Aug 4)", date(2026, 7, 1)),
    "W2": ("Aug 2026 (ep 9, Aug 11-17)", date(2026, 8, 17)),
    "W3": ("Nov 2026 (Nov 13-15; intraday only)", date(2026, 11, 15)),
}


def gap_fields(direction, v, thr, sigma):
    """Positive gap = shortfall (not yet at threshold); negative = beyond it."""
    if v is None or thr is None or direction is None:
        return None, None, None
    g = (v - thr) if direction == "low" else (thr - v)
    pct = (g / abs(thr) * 100.0) if thr not in (0, 0.0) else None
    sg = (g / sigma) if sigma else None
    return g, pct, sg


def build_matrix():
    price = dict(ea.load_series("market_price"))
    fresh = em.compute_fresh(ea.load_series("market_price"), ea.load_series("hash_rate"),
                             ea.load_series("tx_volume"), ea.load_series("fng"))
    csvd = em.load_csv_tiers()
    hc = em.html_commits()
    days, htmlcache = {}, {}
    d = MATRIX_START
    while d <= MATRIX_END:
        use_html = d >= HTML_FIRST_FULL and d in hc
        in_nov = d >= date(2026, 11, 8)
        in_aug = date(2026, 8, 5) <= d <= date(2026, 8, 25)
        in_csv = d in csvd and d < HTML_FIRST_FULL
        if not (in_csv or (use_html and (in_aug or in_nov))):
            d += timedelta(days=1)
            continue
        rec = {}
        if use_html:
            h = em.load_html_day(hc[d])
            htmlcache[d] = h
            for tok in em.ALL_TOKENS:
                r = h["rows"].get(tok)
                if r is None:
                    rec[tok] = dict(tier="N/A (building history)", src="html:missing")
                    continue
                f = fresh.get(tok, {}).get(d)
                direction = (f or {}).get("direction") or BG_DIR.get(tok) or em.DIRECTION.get(tok)
                reading, thr = r["reading"], r["thr"]
                if tok in ("PROD_COST", "REALIZED_PRICE"):       # compared value is spot; target is cost / realized price
                    reading, thr = price.get(d), (r["reading"] if tok == "REALIZED_PRICE" else r["thr"])
                sigma = (f or {}).get("sigma") if tok in em.NONBG_TOKENS else None
                g, gp, gs = gap_fields(direction, reading, thr, sigma)
                rec[tok] = dict(tier=r["tier"], label=r["label"], reading=reading, thr=thr, sigma=sigma, gap=g,
                                gap_pct=gp, gap_sigma=gs, target_txt=r["target_txt"], reading_txt=r["reading_txt"],
                                src="html(live)", direction=direction)
            meta = dict(src="html", pct=h["pct"], fired=h["fired"])
        else:
            prev = csvd.get(d - timedelta(days=1))
            for tok in em.ALL_TOKENS:
                if tok in em.NONBG_TOKENS:
                    f = fresh[tok].get(d)
                    if f is None:
                        rec[tok] = dict(tier="N/A (building history)", src="fresh:nodata")
                        continue
                    v, thr, sigma = f["v"], f["thr"], f["sigma"]
                    reading = price.get(d) if tok == "PROD_COST" else v
                    g, gp, gs = gap_fields(f["direction"], reading, thr, sigma)
                    rec[tok] = dict(tier=f["tier"], label=f["label"], reading=reading, thr=thr, sigma=sigma, gap=g,
                                    gap_pct=gp, gap_sigma=gs, src="fresh(real fns)", direction=f["direction"])
                elif tok in em.BG_CHART_FILE_TOKENS:
                    rec[tok] = dict(tier=csvd[d]["tiers"][tok], src="csv(d) tier only; AA chart file not refetched")
                else:   # BGeometrics-API sourced: production value on day d = CSV row d-1
                    t = prev["tiers"][tok] if prev else "N/A (no d-1 row)"
                    rec[tok] = dict(tier=t, src="csv(d-1) approx, tier only")
            meta = dict(src="csv", pct=dict(csvd[d]["pct"]), fired=None)
        # weighted verdict recomputation with the REAL ud.build_verdict, as-of-day pool membership
        base = dict(ud.WEIGHT_MAP)
        try:
            for tok, w in em.WEIGHT_JOIN.items():
                if rec[tok]["tier"] != "N/A (building history)" and not str(rec[tok]["tier"]).startswith("N/A"):
                    ud.WEIGHT_MAP[tok] = w
            cssmap = {"STRONG BUY": "st-strong-buy", "BUY": "st-buy", "BORDERLINE": "st-near", "NOT YET": "st-no"}
            vals = {f"{t}_STATUS_CLASS": cssmap.get(rec[t]["tier"], "st-mid") for t in em.ALL_TOKENS}
            meta["cb_recomputed"] = ud.build_verdict(vals)["VERDICT_PCT"]
            pool = [t for t in ud.WEIGHT_MAP]
        finally:
            ud.WEIGHT_MAP.clear()
            ud.WEIGHT_MAP.update(base)
        weighted_tokens = [t for t in em.ALL_TOKENS if t not in ("REALIZED_PRICE", "DRAWDOWN")]
        scoreable = [t for t in weighted_tokens if not str(rec[t]["tier"]).startswith("N/A")]
        meta["firing"] = sum(1 for t in weighted_tokens if rec[t]["tier"] in FAV)
        meta["scoreable"] = len(scoreable)
        meta["close"] = price.get(d)
        meta["in_range"] = (price.get(d) is not None and ea.RANGE_LO <= price[d] <= ea.RANGE_HI)
        days[d] = dict(rec=rec, meta=meta)
        d += timedelta(days=1)
    return days, fresh, price


# ---------------------------------------------------------------------------
# Validation: how well does "CSV row d-1" approximate the live production page?
# ---------------------------------------------------------------------------
def validate_d_minus_1():
    """CSV (backtest) tier vs the LIVE committed page, Aug 5-18 (the only overlap with parseable html).
    Days where the CSV itself is N/A are excluded (no vacuous N/A==N/A agreement)."""
    csvd = em.load_csv_tiers()
    hc = em.html_commits()
    out = {}
    for tok in em.BG_TOKENS + em.BG_CHART_FILE_TOKENS:
        a = a0 = n = 0
        for d in sorted(csvd):
            if not (HTML_FIRST_FULL <= d <= date(2026, 8, 18)) or d not in hc or (d - timedelta(days=1)) not in csvd:
                continue
            c1, c0 = csvd[d - timedelta(days=1)]["tiers"][tok], csvd[d]["tiers"][tok]
            if c1.startswith("N/A") and c0.startswith("N/A"):
                continue
            live = em.load_html_day(hc[d])["rows"].get(tok)
            if not live:
                continue
            n += 1
            a += c1 == live["tier"]
            a0 += c0 == live["tier"]
        out[tok] = (a, a0, n)
    return out


def md_table(rows, header):
    s = "| " + " | ".join(header) + " |\n|" + "|".join("---" for _ in header) + "|\n"
    for r in rows:
        s += "| " + " | ".join("" if c is None else str(c) for c in r) + " |\n"
    return s
