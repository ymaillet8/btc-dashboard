#!/usr/bin/env python3
"""
episode_steps.py -- Steps 3-6 of the REPORT-ONLY episode analysis. See episode_analysis.py.
Zero BGeometrics API calls; no BGeometrics chart/data files fetched; no threshold, weight or
scoring change. Indicator maths = the real ud.* / bt.* functions.

Run:  python episode_steps.py        (needs `python episode_analysis.py fetch` first)
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
import episode_report as er

OUT = ea.OUT
FAV = em.FAV
NOV22 = (date(2022, 11, 9), date(2022, 11, 21))
LOW_JUL = date(2026, 7, 1)
CLASS_D = {"MINER_CAP": "event-based (Hash Ribbons state machine)",
           "LTH_SOPR": "fixed break-even (1.0)", "ASOPR_EST": "fixed break-even (1.0)"}
SHORT = {"MVRV_Z": "MVRV", "PUELL": "Puell", "RESERVE_RISK": "ResRisk", "LTH_SOPR": "LTH-SOPR", "THERMOCAP": "Thermo",
         "NRPL": "NRPL", "ASOPR_EST": "aSOPR", "SUPPLY_LOSS": "SupLoss", "REALIZED_PRICE": "RealPx",
         "ACTIVE_ADDR_DEV": "ActAddr", "FNG": "F&G", "RSI": "RSI", "MACD": "MACD", "BOLLINGER": "BB%B", "MAYER": "Mayer",
         "DRAWDOWN": "Drawdn", "NVT_GC": "NVT", "MINER_CAP": "MinerCap", "PROD_COST": "ProdCost"}


def fmt(x, nd=3):
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.{nd}g}" if abs(x) < 1e5 else f"{x:,.0f}"
    return str(x)


def window_days(days, price):
    w1 = [d for d in sorted(days) if days[d]["meta"]["in_range"] and date(2026, 6, 5) <= d <= date(2026, 8, 4)]
    w2 = [d for d in sorted(days) if days[d]["meta"]["in_range"] and date(2026, 8, 11) <= d <= date(2026, 8, 17)]
    w3 = [d for d in sorted(days) if date(2026, 11, 13) <= d <= date(2026, 11, 15)]
    return {"W1": w1, "W2": w2, "W3": w3}


# ---------------------------------------------------------------------------
# percentile rank of a reading vs the indicator's own history (favourable tail), walk-forward + hindsight
# ---------------------------------------------------------------------------
def series_for_rank(tok, fresh):
    """date -> comparable scalar. PROD_COST: spot/cost ratio. MINER_CAP: none (state machine)."""
    f = fresh.get(tok)
    if not f or tok == "MINER_CAP":
        return None
    if tok == "PROD_COST":
        return {d: x["v"] / x["thr"] for d, x in f.items() if x["thr"]}
    return {d: x["v"] for d, x in f.items() if x["v"] is not None}


def pct_rank(series, d, v, direction):
    """% of the indicator's own history through d (walk-forward) that is at least as extreme in the
    favourable direction as v (low: x<=v; high: x>=v). Smaller = more extreme."""
    hist = [x for dd, x in series.items() if dd <= d]
    if not hist:
        return None, 0
    n = len(hist)
    k = sum(1 for x in hist if (x <= v if direction == "low" else x >= v))
    return 100.0 * k / n, n


def pct_rank_hind(series, v, direction):
    hist = list(series.values())
    k = sum(1 for x in hist if (x <= v if direction == "low" else x >= v))
    return 100.0 * k / len(hist), len(hist)


# ---------------------------------------------------------------------------
# STEP 3
# ---------------------------------------------------------------------------
def summarize(days, wdays, lowday, fresh):
    rows = {}
    for tok in em.ALL_TOKENS:
        recs = [(d, days[d]["rec"][tok]) for d in wdays]
        n = len(recs)
        scoreable = [(d, r) for d, r in recs if not str(r["tier"]).startswith("N/A")]
        firing = [(d, r) for d, r in scoreable if r["tier"] in FAV]
        strong = sum(1 for d, r in firing if r["tier"] == "STRONG BUY")
        buy = sum(1 for d, r in firing if r["tier"] == "BUY")
        bord = sum(1 for d, r in firing if r["tier"] == "BORDERLINE")
        with_gap = [(d, r) for d, r in recs if r.get("gap") is not None]
        best = min(with_gap, key=lambda t: t[1]["gap"]) if with_gap else None
        rows[tok] = dict(n=n, scoreable=len(scoreable), firing=len(firing), strong=strong, buy=buy, bord=bord, best=best,
                         weight=em.weight_of(tok), lowday=lowday)
    return rows


def print_step3(label, rows, lowday):
    print(f"\nSTEP 3 -- {label}   (price low: {lowday})")
    print(f"{'indicator':<9}{'wt':>5} {'days':>4} {'scor':>4} {'fire':>4} {'S/B/Bd':>7}  {'best reading (date)':<26}{'d from low':>10}  {'gap@best: units / % / sigma':<30}")
    for tok in em.ALL_TOKENS:
        r = rows[tok]
        w = r["weight"]
        wt = "-" if w is None else f"{w:g}"
        if tok in ("REALIZED_PRICE", "DRAWDOWN"):
            wt = "n/s"
        if r["best"]:
            d, b = r["best"]
            br = f"{fmt(b['reading'])} ({d.strftime('%m-%d')})"
            dl = (d - lowday).days
            gp = f"{fmt(b['gap_pct'],3)}%" if b['gap_pct'] is not None else "-"
            gs = f"{fmt(b['gap_sigma'],3)}s" if b['gap_sigma'] is not None else "-"
            g = f"{fmt(b['gap'])} / {gp} / {gs}"
        else:
            br, dl, g = "tier only (no reading)", None, "-"
        print(f"{SHORT[tok]:<9}{wt:>5} {r['n']:>4} {r['scoreable']:>4} {r['firing']:>4} {r['strong']}/{r['buy']}/{r['bord']:<3} {br:<26}{'' if dl is None else dl:>10}  {g:<30}")


# ---------------------------------------------------------------------------
# STEP 4 -- diagnose every miss (primary window W1, the Jul-2026 low)
# ---------------------------------------------------------------------------
def at_low_window(days, lowday=LOW_JUL, half=3):
    return [d for d in (lowday + timedelta(days=i) for i in range(-half, half + 1)) if d in days]


def diagnose(days, fresh, lowday=LOW_JUL):
    """Diagnose every indicator that was NOT firing on the low day itself. Evidence window: low day +/-3d."""
    win = at_low_window(days, lowday)
    rows = summarize(days, win, lowday, fresh)
    out = {}
    for tok in em.ALL_TOKENS:
        r = rows[tok]
        on_low = days[lowday]["rec"][tok]["tier"]
        fired_low = on_low in FAV
        if fired_low:
            out[tok] = dict(kind="HIT", note=f"{on_low} on the low day; {r['firing']}/{r['scoreable']} days in +/-3d window")
            continue
        base = dict(partial=(r["firing"] > 0), win_firing=r["firing"], win_n=r["n"])
        if r["scoreable"] == 0:
            out[tok] = dict(kind="C", note="N/A on every day in the window (gate/data)", **base)
            continue
        if tok in em.BG_TOKENS or tok in em.BG_CHART_FILE_TOKENS:
            if tok in CLASS_D:
                out[tok] = dict(kind="D", note=CLASS_D[tok], pending=True, **base)
            else:
                out[tok] = dict(kind="PENDING", note="needs full API history for percentile rank; tier-only in this window", pending=True, **base)
            continue
        if tok in CLASS_D:
            out[tok] = dict(kind="D", note=CLASS_D[tok], **base)
            continue
        b = r["best"]
        d, br = b
        ser = series_for_rank(tok, fresh)
        direction = br.get("direction")
        v = (br["reading"] / br["thr"]) if tok == "PROD_COST" else br["reading"]
        p_wf, n_wf = pct_rank(ser, d, v, direction)
        p_h, n_h = pct_rank_hind(ser, v, direction)
        kind = "A" if p_wf <= 10 else ("B" if p_wf > 25 else "A/B-gap (10-25%)")
        out[tok] = dict(kind=kind, p_wf=p_wf, n_wf=n_wf, p_h=p_h, n_h=n_h, best_day=d, best=br,
                        sigma_gap=br.get("gap_sigma"), **base)
    return out


# ---------------------------------------------------------------------------
# STEP 5 -- cross-cycle yardstick
# ---------------------------------------------------------------------------
def dated_inputs(tok, fresh, price):
    """(dated_values, history_dated_values, threshold_fn_factory) mirroring bt's builders for each token."""
    f = fresh[tok]
    ds = sorted(f)
    if tok == "PROD_COST":
        vals = [(d, price[d]) for d in ds if d in price]
        hist = [(d, f[d]["thr"]) for d in ds if d in price and f[d]["thr"] is not None]
        costd = dict(hist)
        return vals, hist, lambda mult: (lambda c, d, v: (costd[d] * mult if d in costd else None))
    vals = [(d, f[d]["v"]) for d in ds]
    return vals, None, lambda T: (lambda c, d, v: T)


def run_walk(tok, fresh, price, param, direction, snap_days=()):
    vals, hist, mk = dated_inputs(tok, fresh, price)
    snaps = {}
    base_tf = mk(param)

    def tf(cache, d, v):
        if d in snap_days:
            snaps[d] = (copy.deepcopy(cache), v)
        return base_tf(cache, d, v)

    st = bt.walk_forward_statuses(vals, direction, tf, tok, history_dated_values=hist)
    return {d: (v, css) for d, v, label, css in st}, snaps


def required_loosening(tok, fresh, price, direction, window_dates):
    """Minimal loosening of the current threshold giving >=1 firing day inside window_dates (real
    ud.status_pill, with each day's walk-forward history cache). Returns (param_required, param_current)."""
    f = fresh[tok]
    cur = 1.0 if tok == "PROD_COST" else (f[window_dates[0]]["thr"])
    _, snaps = run_walk(tok, fresh, price, cur, direction, snap_days=set(window_dates))
    snaps = {d: s for d, s in snaps.items() if d in window_dates}
    FAVC = ("st-strong-buy", "st-buy", "st-near")

    def T_of(d, param):
        if tok == "PROD_COST":
            return f[d]["thr"] * param
        return param

    def fires(param):
        for d, (cache, v) in snaps.items():
            T = T_of(d, param)
            if T is None:
                continue
            if ud.status_pill(v, direction, T, cache=cache, token=tok)[1] in FAVC:
                return True
        return False

    if fires(cur):
        return cur, cur
    sgn = 1.0 if direction == "low" else -1.0
    base = abs(cur) if cur not in (0, None) else 1.0
    hi = cur + sgn * base * 0.05
    for _ in range(60):                       # expand until it fires
        if fires(hi):
            break
        hi = cur + (hi - cur) * 1.6 + sgn * base * 0.01
    lo = cur
    for _ in range(50):
        mid = (lo + hi) / 2
        if fires(mid):
            hi = mid
        else:
            lo = mid
    return hi, cur


def episode_fire(st, lo, hi):
    ds = [d for d in st if lo <= d <= hi]
    f = [d for d in ds if st[d][1] in ("st-strong-buy", "st-buy", "st-near")]
    return len(f), len(ds)


def false_fire_rate(st, price):
    """Days with close >= 1.5x its trailing-365-day minimum close ('not near a bottom'): share where it fired."""
    dates = sorted(price)
    mn = {}
    from collections import deque
    dq = deque()
    for d in dates:
        while dq and dq[0][0] < d - timedelta(days=364):
            dq.popleft()
        while dq and dq[-1][1] >= price[d]:
            dq.pop()
        dq.append((d, price[d]))
        mn[d] = dq[0][1]
    elig = [d for d in dates if d in st and price[d] >= 1.5 * mn[d] and d >= dates[0] + timedelta(days=365)]
    fired = [d for d in elig if st[d][1] in ("st-strong-buy", "st-buy", "st-near")]
    return len(fired), len(elig)


def step5(diag, fresh, price):
    dec18_lo = min(((d, p) for d, p in price.items() if date(2018, 11, 15) <= d <= date(2018, 12, 31)), key=lambda t: t[1])
    DEC18 = (dec18_lo[0] - timedelta(days=3), dec18_lo[0] + timedelta(days=3))
    win_jul = [LOW_JUL]                                   # the low day itself (matches the miss test)
    win_jul3 = [LOW_JUL + timedelta(days=i) for i in range(-3, 4)]
    rows = []
    for tok in em.NONBG_TOKENS:
        k = diag[tok]["kind"]
        if tok == "MINER_CAP" or k not in ("A", "C", "A/B-gap (10-25%)"):
            continue
        direction = em.DIRECTION[tok]
        req, cur = required_loosening(tok, fresh, price, direction, win_jul)
        req3, _ = required_loosening(tok, fresh, price, direction, win_jul3)
        stc, _ = run_walk(tok, fresh, price, cur, direction)
        str_, _ = run_walk(tok, fresh, price, req, direction)
        n22c = episode_fire(stc, *NOV22)
        n22r = episode_fire(str_, *NOV22)
        n18c = episode_fire(stc, *DEC18)
        n18r = episode_fire(str_, *DEC18)
        ffc = false_fire_rate(stc, price)
        ffr = false_fire_rate(str_, price)
        loosen = (abs(req - cur) / abs(cur) * 100.0) if cur not in (0, None) and tok != "PROD_COST" else None
        rows.append(dict(tok=tok, kind=k, cur=cur, req=req, req3=req3, loosen=loosen, n22c=n22c, n22r=n22r, n18c=n18c, n18r=n18r,
                         ffc=ffc, ffr=ffr))
    return rows, DEC18, dec18_lo


# ---------------------------------------------------------------------------
# Writers + driver
# ---------------------------------------------------------------------------
ABBR = {"STRONG BUY": "SB", "BUY": "B", "BORDERLINE": "bd", "NOT YET": ".", "STALE": "ST"}


def abbr(t):
    return ABBR.get(t, "NA" if str(t).startswith("N/A") else str(t)[:3])


def write_matrix(days, path_csv, path_md):
    os.makedirs(OUT, exist_ok=True)
    hdr = ["date", "close_blockchain_com", "close_in_57_64K", "source", "firing_of_scoreable_17", "cb_pct_reported",
           "cb_pct_recomputed_real_build_verdict", "ms_pct_context", "ct_pct_context"]
    for t in em.ALL_TOKENS:
        hdr += [f"{t}__tier", f"{t}__reading", f"{t}__threshold", f"{t}__gap", f"{t}__gap_pct", f"{t}__gap_sigma", f"{t}__src"]
    with open(path_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(hdr)
        for d in sorted(days):
            m, rec = days[d]["meta"], days[d]["rec"]
            row = [d.isoformat(), None if m["close"] is None else round(m["close"], 2), int(m["in_range"]), m["src"],
                   f"{m['firing']}/{m['scoreable']}", m["pct"].get("CB"), m.get("cb_recomputed"), m["pct"].get("MS"), m["pct"].get("CT")]
            for t in em.ALL_TOKENS:
                r = rec[t]
                row += [r["tier"]] + [("" if r.get(k) is None else (round(r[k], 6) if isinstance(r[k], float) else r[k]))
                                      for k in ("reading", "thr", "gap", "gap_pct", "gap_sigma")] + [r.get("src", "")]
            w.writerow(row)
    lines = ["# Episode day-by-day matrix (Cycle Bottom indicators), 2026-06-02 .. 2026-11-15\n",
             "REPORT ONLY. Tiers: SB=STRONG BUY, B=BUY, bd=BORDERLINE, .=NOT YET, NA=N/A. `*` = close inside $57-64K. "
             "Full readings/thresholds/gaps are in the CSV.\n",
             "Sources: Jun 2 - Aug 4 = existing daily-matrix CSVs (BGeometrics columns use row d-1 as the production value: "
             "APPROXIMATE, tier only) + fresh walk-forward for the zero-quota indicators; Aug 5 - Nov 15 = committed dashboard.html "
             "(live page as published). Aug 1-4 LTH-SOPR/NRPL/aSOPR/Supply-in-Loss are a data gap in the source CSV (HTTP 429). "
             "Realized Price and Drawdown are shown but not weighted. Active Addresses tier = existing CSV/live page (BGeometrics chart "
             "file not re-fetched). CB% reported = CSV (unshifted) or live page; recomputed = real ud.build_verdict on the shown tiers.\n"]
    head = ["date", "close", "src", "fire"] + [es_short for es_short in (SHORT[t] for t in em.ALL_TOKENS)] + ["CB% rep", "CB% rc", "MS%", "CT%"]
    rows = []
    for d in sorted(days):
        m, rec = days[d]["meta"], days[d]["rec"]
        rows.append([d.isoformat() + ("*" if m["in_range"] else ""), f"{m['close']:,.0f}" if m["close"] else "-", m["src"],
                     f"{m['firing']}/{m['scoreable']}"] + [abbr(rec[t]["tier"]) for t in em.ALL_TOKENS] +
                    [m["pct"].get("CB"), m.get("cb_recomputed"), m["pct"].get("MS"), m["pct"].get("CT")])
    lines.append(er.md_table(rows, head))
    open(path_md, "w", encoding="utf-8").write("\n".join(lines))


def run_all():
    t_start = time.time()
    eps, pr, bn, intr = ea.step1()
    price = dict(ea.load_series("market_price"))
    days, fresh, _ = er.build_matrix()
    csv_path = os.path.join(OUT, "episode_matrix_2026-06-02_to_2026-11-15.csv")
    md_path = os.path.join(OUT, "episode_matrix_2026-06-02_to_2026-11-15.md")
    write_matrix(days, csv_path, md_path)
    W = window_days(days, price)
    val = er.validate_d_minus_1()
    print("\nVALIDATION of the CSV(d-1) production-day shift vs the LIVE committed page (Aug 5-18; N/A-vs-N/A excluded):")
    for k, (a, a0, n) in val.items():
        print(f"  {es_name(k):<10} d-1 matches {a}/{n};  unshifted matches {a0}/{n}")
    sums = {}
    for wn, lowday in (("W1", date(2026, 7, 1)), ("W2", date(2026, 8, 17)), ("W3", date(2026, 11, 15))):
        sums[wn] = summarize(days, W[wn], lowday, fresh)
        print_step3({"W1": "W1 Jun 5-Aug 4 (41 in-range days)", "W2": "W2 Aug 11-17 (7 in-range days)",
                     "W3": "W3 Nov 13-15 (3 days, intraday only)"}[wn], sums[wn], lowday)
    diag = diagnose(days, fresh)
    print("\nSTEP 4 -- diagnosis of every indicator NOT firing on the 1 Jul 2026 low day (evidence window +/-3d)")
    for t in em.ALL_TOKENS:
        d = diag[t]
        if d["kind"] == "HIT":
            continue
        b = d.get("best") or {}
        extra = ""
        if "p_wf" in d:
            extra = (f" best={fmt(b.get('reading'))} thr={fmt(b.get('thr'))} gap={fmt(b.get('gap_pct'))}% ({fmt(b.get('gap_sigma'))} sigma); "
                     f"walk-forward own-history rank {d['p_wf']:.1f}% (n={d['n_wf']}), hindsight {d['p_h']:.1f}%")
        print(f"  {SHORT[t]:<9} class {d['kind']:<17}{'(fired on other days in window)' if d.get('partial') else '':<32}{d.get('note','')}{extra}")
    s5, DEC18, dec18_lo = step5(diag, fresh, price)
    print(f"\nSTEP 5 -- cross-cycle (Nov 2022 window {NOV22[0]}..{NOV22[1]}; Dec 2018 low {dec18_lo[0]} ${dec18_lo[1]:,.0f}, window {DEC18[0]}..{DEC18[1]})")
    for r in s5:
        print(f"  {SHORT[r['tok']]:<9} class {r['kind']:<17} cur={fmt(r['cur'])}  required(Jul-1)={fmt(r['req'])}  required(+/-3d)={fmt(r['req3'])}"
              f"  Nov22 fire days cur {r['n22c'][0]}/{r['n22c'][1]} req {r['n22r'][0]}/{r['n22r'][1]}"
              f"  Dec18 cur {r['n18c'][0]}/{r['n18c'][1]} req {r['n18r'][0]}/{r['n18r'][1]}"
              f"  false-fire cur {r['ffc'][0]}/{r['ffc'][1]} ({100*r['ffc'][0]/max(r['ffc'][1],1):.1f}%) req {r['ffr'][0]}/{r['ffr'][1]} ({100*r['ffr'][0]/max(r['ffr'][1],1):.1f}%)")
    return dict(days=days, fresh=fresh, price=price, eps=eps, intr=intr, sums=sums, diag=diag, s5=s5, val=val,
                DEC18=DEC18, dec18_lo=dec18_lo, elapsed=time.time() - t_start, csv=csv_path, md=md_path)


def es_name(k):
    return SHORT.get(k, k)


if __name__ == "__main__":
    run_all()
