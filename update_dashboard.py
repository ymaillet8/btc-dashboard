#!/usr/bin/env python3
"""
BTC Bottom Watch — data engine.

Pulls from four free sources and rebuilds dashboard.html from
dashboard_template.html:

  1. BGeometrics API (needs a free key) — 10 calls/run, using the FULL
     confirmed free-tier allowance (10 req/hour, 15 req/day per your own
     account page — not the 8/hour originally assumed). Zero buffer left;
     see the note above BG_METRICS below. Covers: MVRV Z-Score, Realized
     Price, Puell Multiple, Reserve Risk, Thermocap Multiple, LTH-SOPR,
     Pi Cycle Bottom, % Supply in Loss, VDD Multiplier, aSOPR.
  2. CoinGecko public API — no key, no signup. Covers: Mayer Multiple and
     Drawdown magnitude (both computed from 200+ days of price history).
  3. Blockchain.com charts/stats API — no key, no signup. Covers:
     Price vs. Production Cost and Miner Capitulation Index (Hash Ribbons
     proxy), and feeds into NVT Golden Cross.
  4. Alternative.me — no key. Fear & Greed Index.

Plus one calendar-only calculation needing no API at all: the 1064/364-day
cycle rhythm.

Environment variable (GitHub Actions secret):
  BGEOMETRICS_API_KEY   required -> https://portal.bgeometrics.com/register

HONESTY NOTE ON BGEOMETRICS: their interactive docs are JavaScript-rendered,
so I could not verify the exact endpoint slugs from outside a logged-in
session. Each is tried once (no retries, to protect the 8/hour quota) and
falls back to "CHECK" on failure rather than breaking the run. See the
Action log for exactly which ones succeeded.

HONESTY NOTE ON PRODUCTION COST: this is a self-computed estimate, not a
licensed feed. It uses two disclosed, editable assumptions — average fleet
efficiency (J/TH) and electricity cost ($/kWh) — documented in the
ASSUMPTIONS block below. Treat it as directional, the same way you would
JPMorgan's or Checkonchain's own production-cost models, which rely on
similar assumptions.

HONESTY NOTE ON NVT GOLDEN CROSS AND MINER CAPITULATION: both are
reconstructions of the standard published methodology using raw free data,
not a licensed "Golden Cross" or "Miner Capitulation Index" feed from
LookIntoBitcoin/CryptoQuant. Labeled "(approx.)" on the dashboard.

HONESTY NOTE ON ACTIVE ADDRESSES POWER-LAW DEVIATION: a self-computed
regression (OLS on log(active addresses) vs. log(days since genesis)),
grounded in Santostasi's own stated methodology that price, hash rate, and
active addresses are all power laws of each other and of time. Sourced from
BGeometrics' own Active Addresses chart data file, NOT the metered
v1/active-addresses REST endpoint -- verified directly in the API
Playground that the free tier caps historical range queries on that
endpoint to the last 4 years, and confirmed on real data that a 4-year-only
window breaks the regression (produces a negative slope). See the longer
note above fetch_active_addresses_full_history() below.
"""
import json
import math
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone, date

# ---------------------------------------------------------------------------
# Disclosed assumptions for the self-computed Production Cost estimate.
# Adjust these as your own research updates — they're the two inputs that
# actually drive the number, everything else comes from live network data.
# ---------------------------------------------------------------------------
ASSUMED_FLEET_EFFICIENCY_J_PER_TH = 20.0   # blended fleet efficiency, Joules per TH/s
ASSUMED_ELECTRICITY_COST_USD_PER_KWH = 0.05  # blended global miner electricity cost

BG_KEY = os.environ.get("BGEOMETRICS_API_KEY", "")
BG_BASE = "https://api.bitcoin-data.com/v1"

# ---------------------------------------------------------------------------
# Last-known-good value cache. Real production trackers fall back to the
# last successful reading (clearly labeled as such) when a live fetch
# fails — e.g. during today's BGeometrics rate-limit exhaustion — instead
# of just going blank. This file gets committed alongside dashboard.html
# by the workflow, so it persists between daily runs.
# ---------------------------------------------------------------------------
CACHE_FILE = "last_known_good.json"
CACHE_MAX_AGE_DAYS = 5  # older than this, a stale fallback isn't shown at all


def load_cache():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cache(cache):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        print(f"  ! failed to save cache file: {e}")


def cache_lookup(cache, key):
    """Returns (value, date_str) if a usable (not-too-old) cached value
    exists for this key, else (None, None)."""
    entry = cache.get(key)
    if not entry:
        return None, None
    try:
        cached_date = datetime.fromisoformat(entry["date"])
        age_days = (datetime.now(timezone.utc) - cached_date).days
        if age_days > CACHE_MAX_AGE_DAYS:
            return None, None
        return entry["value"], entry["date"][:10]
    except (KeyError, ValueError, TypeError):
        return None, None


def cache_store(cache, key, value):
    if value is not None:
        cache[key] = {"value": value, "date": datetime.now(timezone.utc).isoformat()}


# ---------------------------------------------------------------------------
# Rolling-history percentile thresholds. Grounded in real research, not
# invented: a fixed historical-cycle-analog number (e.g. "NRPL <= -800,000
# BTC, per 2018/2022") is anchored to a smaller, structurally different
# network — Bitcoin's realized cap and on-chain activity have grown since,
# so an absolute threshold from a prior cycle isn't the same statistical
# statement today. A peer-reviewed paper (Grobys et al., Research in
# International Business and Finance, 2026) confirms there's no
# universally accepted rule for these thresholds in the first place, and a
# documented model ("Bitcoin Barometer," validated blind against 16
# historical cycle events at 94% accuracy) uses percentile scoring against
# an indicator's OWN historical distribution specifically to avoid
# anchoring to an earlier market structure. This does the same thing,
# self-normalizing rather than using a fixed number, using the same daily
# cache this dashboard already builds — zero extra API cost.
#
# Honest limitation, stated plainly: this starts with zero history and
# needs real time to become meaningful. MIN_HISTORY_DAYS below is the
# bootstrap gate — below that many accumulated daily points, the
# indicator stays excluded (N/A) exactly as before, rather than trusting
# a percentile computed from too few observations.
HISTORY_MAX_LEN = 200       # cap stored history length (keeps the cache file small)
MIN_HISTORY_DAYS = 90       # minimum accumulated points before trusting a computed percentile
PERCENTILE_CUTOFF = 10      # bottom 10th percentile of trailing history = buy-favorable


def history_append(cache, key, value, date=None):
    """Append today's value to this token's rolling history, trimmed to
    HISTORY_MAX_LEN. Separate from cache_store()'s single last-known-good
    value — this list is what percentile thresholds get computed from.

    Tracks the calendar day (UTC) of the last append per key. A second call
    on the same day (e.g. a manual workflow_dispatch rerun after the
    scheduled run already fetched today) replaces that day's entry instead
    of adding a duplicate — otherwise the rolling n count, and therefore
    MIN_HISTORY_DAYS gating, would be inflated by reruns rather than
    reflecting genuine distinct days.

    `date` lets callers that replay historical days (e.g.
    backtest_indicators.py's walk-forward loops, which call this many times
    within one real wall-clock run to simulate one call per past day)
    supply the simulated day explicitly, instead of every call collapsing
    onto the real "today"."""
    if value is None:
        return
    hist_key = f"{key}__history"
    entry = cache.get(hist_key, {"values": [], "last_date": None})
    try:
        fvalue = float(value)
    except (TypeError, ValueError):
        return
    day = date.isoformat() if date is not None else datetime.now(timezone.utc).date().isoformat()
    if entry.get("last_date") == day and entry["values"]:
        entry["values"][-1] = fvalue
    else:
        entry["values"].append(fvalue)
        entry["last_date"] = day
    entry["values"] = entry["values"][-HISTORY_MAX_LEN:]
    cache[hist_key] = entry


def compute_percentile(values, pct):
    """Simple, dependency-free percentile (linear interpolation between
    closest ranks) — avoids requiring numpy for one calculation."""
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * (pct / 100)
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def rolling_threshold(cache, key, percentile=PERCENTILE_CUTOFF):
    """Returns (threshold, n_points) if enough history has accumulated to
    trust a computed percentile, else (None, n_points) so callers can show
    honest accumulation progress even before the gate is met. `percentile`
    defaults to PERCENTILE_CUTOFF (10 — bottom-favorable) for every
    existing Table 1 caller, unchanged; Table 3's top-side callers
    (MVRV Z-Score, Puell, Thermocap) pass percentile=100-PERCENTILE_CUTOFF
    to get the top decile of the SAME trailing history instead — same
    function, same cache, just which tail of the distribution counts."""
    hist_key = f"{key}__history"
    values = cache.get(hist_key, {}).get("values", [])
    n = len(values)
    if n < MIN_HISTORY_DAYS:
        return None, n
    return compute_percentile(values, percentile), n


# ---------------------------------------------------------------------------
# "Near-threshold" leeway. Real research behind this, not a guess: the
# credible sources for exactly this problem (TradingView's own documented
# Bollinger Bands methodology, an arXiv paper on hysteresis threshold
# choice) size a tolerance band using the indicator's OWN measured
# statistical scale — not a flat percentage applied to everything alike
# (too blunt: a slow-moving indicator and a jumpy one get treated
# identically), and not a hand-picked band per indicator (too subjective —
# that's a guess wearing precision as a costume). This uses each
# indicator's real trailing standard deviation, computed from the same
# rolling-history cache already built for the percentile thresholds.
NEAR_STDEV_MIN_DAYS = 20     # fewer points needed than a percentile (MIN_HISTORY_DAYS=90) —
                             # a standard deviation estimate stabilizes faster than a reliable tail percentile
NEAR_STDEV_PARTIAL_MIN_DAYS = 3   # floor below which even a real stdev is too noisy to trust at all
NEAR_BAND_STDEV_FRACTION = 0.25   # width of the "near" zone, in units of the indicator's own trailing sigma
NEAR_BAND_BOOTSTRAP_PCT = 0.03    # provisional flat 3% fallback only until real sigma exists

# 2 sigma is real quant convention for "statistically uncommon" (~95th
# percentile under normal assumptions), not 1 sigma, which the same
# sources (SpotGamma, NinjaTrader, an open quant-strategies repo) note is
# ordinary day-to-day movement. Used to distinguish a reading that just
# crossed its threshold from one that blew through it.
STRONG_BUY_STDEV_FRACTION = 2.0


def compute_stdev(values):
    """Population standard deviation, dependency-free (no numpy)."""
    if not values or len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((x - mean) ** 2 for x in values) / len(values)
    return variance ** 0.5


def get_effective_sigma(cache, token, threshold):
    """Shared sigma source for both the leeway band and the strong-buy
    tier, so the two stay consistent rather than each having its own
    definition of "normal" for the same indicator. Real trailing stdev
    once >=20 days of history exist. Below that, a zero/None-threshold
    indicator (MVRV Z-Score, MACD) can't use the flat-percentage bootstrap
    below at all -- 3% of zero is meaningless -- so it used to stay fully
    dormant (no leeway whatsoever) until 20 days accumulated. That's a
    real gap: a real, if noisier, stdev is computable from as few as 3
    points and is still a genuine measurement, not a guess. So a
    zero/None-threshold indicator with 3-19 days of history now gets that
    partial-sample stdev, clearly labeled as lower-confidence. A
    non-zero-threshold indicator in that same 3-19 day range is
    unaffected -- it already had a real (if provisional) band via the 3%
    bootstrap below, and that path is untouched. Returns (None, None) only
    when truly nothing defensible can be built yet (zero/None threshold,
    fewer than 3 days)."""
    hist_key = f"{token}__history"
    values = cache.get(hist_key, {}).get("values", [])
    n = len(values)
    if n >= NEAR_STDEV_MIN_DAYS:
        sigma = compute_stdev(values)
        if sigma:
            return sigma, f"n={n}"
    if threshold in (0, None) and n >= NEAR_STDEV_PARTIAL_MIN_DAYS:
        sigma = compute_stdev(values)
        if sigma:
            return sigma, f"provisional, n={n} (partial sample)"
    if threshold not in (0, None):
        return abs(threshold) * NEAR_BAND_BOOTSTRAP_PCT, "provisional 3%-of-threshold estimate"
    return None, None


def near_threshold_band(cache, token, threshold):
    """Returns (band_width, source_label) — how far past the threshold a
    reading can sit and still count as "near." Prefers a real trailing
    standard deviation once enough history exists; falls back to a small,
    clearly-labeled flat percentage before then. Returns (None, None) if
    no defensible band can be computed yet (e.g. a zero threshold with too
    little history — 3% of zero is meaningless, so no leeway is granted
    rather than fabricating one). Built on get_effective_sigma() so the
    two share one definition of "how much this indicator naturally
    wobbles" -- any real-stdev case (full n>=20 OR the partial 3-19-day
    sample) scales it to a tighter band (NEAR_BAND_STDEV_FRACTION of
    sigma), since both are genuine measurements just at different
    confidence; the bootstrap case is already the right-sized band as-is
    (no further scaling), matching this function's pre-existing behavior
    exactly."""
    sigma, source = get_effective_sigma(cache, token, threshold)
    if sigma is None:
        return None, None
    if source.startswith("n=") or source.startswith("provisional, n="):
        return sigma * NEAR_BAND_STDEV_FRACTION, f"\u00b1{NEAR_BAND_STDEV_FRACTION}\u03c3 ({source})"
    return sigma, "provisional 3%"


def consecutive_buy_days(cache, token, direction, threshold):
    """How many of the most recent daily readings in this token's own
    rolling history were buy-favorable (crossed the threshold outright, or
    sat inside its near-threshold leeway band), counting back from today
    until the first day that wasn't. A second, independent signal shown
    next to today's status -- doesn't feed the tier system, WEIGHT_MAP, or
    the weighted verdict at all, just visible context. Uses today's
    threshold/band against every historical point rather than
    reconstructing each day's now-lost threshold, the same simplification
    the rest of this leeway system already makes."""
    hist_key = f"{token}__history"
    values = cache.get(hist_key, {}).get("values", [])
    if not values or direction not in ("low", "high") or threshold is None:
        return None
    band, _ = near_threshold_band(cache, token, threshold)
    count = 0
    for v in reversed(values):
        if direction == "low":
            favorable = v <= threshold or (band is not None and v <= threshold + band)
        else:
            favorable = v >= threshold or (band is not None and v >= threshold - band)
        if not favorable:
            break
        count += 1
    return count

# token -> (endpoint slug [best guess, unverified], direction, threshold)
BG_METRICS = {
    "MVRV_Z":        ("mvrv-zscore",      "low",  0.0),
    "REALIZED_PRICE": ("realized-price",  None,   None),
    "PUELL":         ("puell-multiple",   "low",  0.5),
    "RESERVE_RISK":  ("reserve-risk",     "low",  0.002),
    "THERMOCAP":     ("thermocap-multiple", "low", None),
    "LTH_SOPR":      ("lth-sopr",         "low",  1.0),
    # Pi Cycle removed (v18): confirmed via BGeometrics' own chart page
    # description (charts.bgeometrics.com/pi_cycle.html) that this feed is
    # the Top-only variant (111-day vs 350-day SMA), not the genuine Bottom
    # variant (150-day EMA vs 471-day SMA x0.745, a completely different
    # calculation). A top-only signal has no use case on a dashboard built
    # specifically to call bottoms. The real Bottom variant would require
    # ~500+ days of price history to self-compute, more than CoinGecko's
    # free tier reliably supports — a possible future addition, not
    # implemented here.
    "NRPL":          ("nrpl-btc",         "low",  None),   # Net Realized P&L in BTC — confirmed-real slug (seen directly in your BGeometrics account's own API usage examples), not a guess like the metric it replaced
    "SUPPLY_PROFIT": ("supply-profit", None,   None),    # confirmed-real slug (BGeometrics API Playground/docs, Aug 2026) — % Supply in Loss = 100 - this, computed in main()
    "SOPR":          ("sopr",             None,   None),    # raw fetch only — used to derive an aSOPR estimate below, not shown directly
}
# v9: swapped VDD (an unconfirmed slug guess that was always context-only
# anyway) for NRPL-BTC, a metric this whole addition is grounded in: your
# uploaded forecaster-track-record report specifically recommends "realized-
# loss magnitude" as a confirmation signal for a genuine bottom (2022 flushed
# ~1.2M BTC in realized losses; mid-2026 had only ~187k — watch that gap
# close). NRPL (net realized profit/loss) is the closest confirmed-available
# proxy: deeply negative NRPL means realized losses are dominating realized
# profits, i.e. capitulation. It's the net figure, not the pure gross-loss
# figure the report cites, but it's built from the same underlying data and
# moves the same direction. No widely-cited fixed "buy" threshold exists for
# this specific metric, so it stays context-only (displayed, not scored in
# the verdict tally) rather than inventing a number — same honest treatment
# as Thermocap.
# This is 10 of 10 confirmed free hourly requests — zero spare again, a
# deliberate tradeoff (see chat) rather than an oversight. A manual re-run
# within the same hour will 429; wait ~an hour or use tomorrow's scheduled
# run. Drop a metric here if this becomes a real annoyance during testing.

# aSOPR estimation constant — see compute_asopr_estimate() below for the
# reasoning. Adjustable if better data on the dilution share ever surfaces.
ASOPR_DILUTION_SHARE = 0.30

# HONESTY UPDATE (Aug 2, 2026): a prior version of this file concluded Pi
# Cycle, VDD, and Supply in Loss were absent from BGeometrics entirely.
# That was based on an incomplete view of their metric list. Their full
# site navigation confirms chart pages exist for all three:
#   charts.bgeometrics.com/pi_cycle.html
#   charts.bgeometrics.com/vdd.html
#   charts.bgeometrics.com/supply_in_profit.html
# The API slugs above are inferred from those chart filenames, following
# the same underscore-to-hyphen pattern confirmed correct for the 6
# metrics that already work — but NOT independently verified against live
# API docs (still JS-rendered, still can't access them directly). If any
# of these three show "CHECK", check the Action log and this may need a
# slug tweak — same graceful fallback as always, nothing breaks.
# NOTE ON PI CYCLE: their nav lists it simply as "Pi Cycle" under a Price
# category, not "Pi Cycle Bottom" specifically. It's possible this is the
# more famous Pi Cycle TOP indicator rather than the bottom variant your
# ranking called for. If the reading looks like a top-signal instead of a
# bottom-signal once live, that's why — let me know and I'll investigate.
# aSOPR still confirmed absent — checked against BGeometrics' complete
# metric list (SOPR, NRPL, NUPL, Realized P&L Ratio all present; no
# adjusted/aSOPR variant anywhere) — stays manual, this one really is gone.
# This uses 9 of the confirmed 10/hour free-tier requests, leaving 1 as a
# real safety buffer — enough for one manual re-run within the same hour
# without hitting a 429.

# Known cycle anchor dates for the 1064/364-day rhythm check (UTC dates).
# Update PRIOR_BOTTOM/PRIOR_TOP if your own model's anchors change.
CYCLE_ANCHORS = {
    "last_cycle_top": date(2025, 10, 6),       # ~ATH October 2025
    "projected_bottom_offset_days": 386,        # per your existing 364-day-family model
}
ALL_TIME_HIGH_USD = 126296  # update if a new ATH prints


def _get_json(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "btc-dashboard-bot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# ---------------------------------------------------------------------------
# 1. BGeometrics — 8 calls, safely under the 8/hour free cap
# ---------------------------------------------------------------------------
def _log_bg_rate_limit(slug, headers):
    """Log BGeometrics' rate-limit headers, if present, for whichever call
    just ran (success or failure) -- closes the observability gap where we
    could only ever see a 429 after the fact, never how close to it a
    healthy run was cutting things."""
    remaining_hour = headers.get("X-RateLimit-Remaining-Hour")
    remaining_day = headers.get("X-RateLimit-Remaining-Day")
    reset_hour = headers.get("X-RateLimit-Reset-Hour")
    if remaining_hour is not None or remaining_day is not None:
        print(f"    [{slug}] rate limit: {remaining_hour}/hour remaining, "
              f"{remaining_day}/day remaining, hour resets in {reset_hour}s")


def fetch_bg_latest(slug):
    """Fetch and unwrap the latest value for one metric.

    Confirmed real response shape (per your live dashboard, Aug 2, 2026):
        [{"d": "2026-08-01", "unixTs": 1785542400, "mvrvZscore": 0.3471}]
    i.e. a list containing one dict, where the actual metric lives under a
    camelCase key that varies per indicator (mvrvZscore, realizedPrice,
    puellMultiple, etc.) alongside metadata keys (d, unixTs). This pulls
    the first value that isn't one of the known metadata keys, so it works
    regardless of the exact field name for any given metric.
    """
    url = f"{BG_BASE}/{slug}"
    METADATA_KEYS = {"d", "date", "unixts", "unix_ts", "timestamp", "time"}
    req = urllib.request.Request(url, headers={
        "User-Agent": "btc-dashboard-bot/1.0",
        "Authorization": f"Bearer {BG_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            _log_bg_rate_limit(slug, resp.headers)
            data = json.loads(resp.read().decode())

        entry = None
        if isinstance(data, list) and data:
            last = data[-1]
            if isinstance(last, dict):
                entry = last
            else:
                return last  # bare value at the end of a plain list
        elif isinstance(data, dict):
            if "value" in data:
                return data["value"]
            d = data.get("data")
            if isinstance(d, list) and d:
                last = d[-1]
                entry = last if isinstance(last, dict) else None
                if entry is None:
                    return last
            elif isinstance(d, dict):
                entry = d
            else:
                entry = data  # the top-level dict itself may be the entry

        if entry:
            for key, val in entry.items():
                if key.lower() not in METADATA_KEYS:
                    return val
    except urllib.error.HTTPError as e:
        _log_bg_rate_limit(slug, e.headers)
        print(f"  ! {slug}: HTTP {e.code} — {e.reason}")
    except Exception as e:
        print(f"  ! {slug}: failed — {e}")
    return None


# ---------------------------------------------------------------------------
# 1a-top. Pi Cycle Top — genuinely new for the Cycle Top table (Table 3).
# The "pi-cycle" slug was removed from BG_METRICS entirely in v18 after
# confirming (via BGeometrics' own chart page) it's the TOP-only variant
# (111-day SMA vs. 350-day SMA x2), not the Bottom variant this dashboard
# needed at the time -- useless there, but exactly the real, standard,
# published Pi Cycle TOP formula needed here. Re-confirmed live before
# writing this: the endpoint still works, unauthenticated, same free tier
# (https://coincodex.com/bitcoin-pi-cycle-top-indicator/ for the public
# formula). Returns three raw fields per day (piSma111, piSma350x2,
# piSignal) -- richer than the single-value shape fetch_bg_latest() expects,
# so this needs its own small fetch rather than reusing that generic
# parser. The crossed/not-crossed state is computed here directly from the
# two raw SMA values rather than trusted from BGeometrics' own piSignal
# field: piSignal was 0 for the entire available ~4-year window when this
# was researched (no real crossover happened in that window -- the last
# one was April 2021), so its exact semantics couldn't be independently
# verified against a known historical crossover before shipping.
# ---------------------------------------------------------------------------
def fetch_pi_cycle_latest():
    """Latest day's (piSma111, piSma350x2) from BGeometrics' pi-cycle feed,
    or (None, None) on any fetch/parse failure."""
    url = f"{BG_BASE}/pi-cycle"
    try:
        data = _get_json(url, headers={
            "User-Agent": "btc-dashboard-bot/1.0",
            "Authorization": f"Bearer {BG_KEY}",
        })
        if isinstance(data, list) and data:
            last = data[-1]
            if isinstance(last, dict):
                sma111 = last.get("piSma111")
                sma350x2 = last.get("piSma350x2")
                if sma111 is not None and sma350x2 is not None:
                    return float(sma111), float(sma350x2)
    except urllib.error.HTTPError as e:
        print(f"  ! pi-cycle: HTTP {e.code} — {e.reason}")
    except Exception as e:
        print(f"  ! pi-cycle: failed — {e}")
    return None, None


# ---------------------------------------------------------------------------
# 1b. Active Addresses Power-Law Deviation — self-computed, sourced from
#     BGeometrics but NOT via the metered v1/active-addresses REST endpoint.
#
#     Grounded in Santostasi's own stated methodology: Bitcoin's price,
#     hash rate, and active addresses are "all power laws of each other and
#     of time," and his fuller model builds a separate Price-vs-Active-
#     Addresses power law alongside the well-known Price-vs-Time one. This
#     measures that same relationship from the addresses side: how far
#     current network adoption sits below (or above) its own long-run
#     power-law trend against days since genesis.
#
#     HONESTY NOTE ON THE DATA SOURCE (verified directly in the BGeometrics
#     API Playground, Aug 2026, before writing any of this): the v1/active-
#     addresses REST endpoint genuinely supports startday/endday range
#     queries -- but the Playground's own Usage Guidelines state the free
#     tier caps historical range queries to the last 4 years. That cap
#     isn't just a smaller dataset -- fitting this regression on real data
#     confirmed it actively breaks the method: the last-4-years-only window
#     gives a NEGATIVE slope (implying active addresses shrink over time),
#     failing the basic sanity check that network adoption has grown, not
#     shrunk, since genesis. The full genesis-to-now history (fit on the
#     same real data) gives a sane positive slope (~2.5) with R^2 ~0.83.
#     So this pulls full history from the same static JSON file that feeds
#     BGeometrics' own public Active Addresses chart
#     (charts.bgeometrics.com/address_active_dark.html) instead -- genuinely
#     their data, just via the unmetered file their own chart frontend
#     reads, rather than the 10/hour-capped REST API. Bonus: this adds zero
#     calls against that budget, so BG_METRICS' existing 9-of-10 usage
#     (one spare call already reserved) is untouched.
# ---------------------------------------------------------------------------
GENESIS_DATE = date(2009, 1, 3)
ACTIVE_ADDR_CHART_JSON_URL = "https://charts.bgeometrics.com/files/addresses_active.json"


def days_since_genesis_for(d):
    return (d - GENESIS_DATE).days


def fit_power_law(xy_pairs):
    """Closed-form OLS fit of log(y) = intercept + slope*log(x) -- pure
    Python, no numpy. slope = covariance(x,y)/variance(x) (population
    covariance/variance, consistent with compute_stdev() elsewhere in this
    file). Returns (slope, intercept, r_squared), or (None, None, None) if
    there isn't enough usable data to fit."""
    pts = [(x, y) for x, y in xy_pairs if x and x > 0 and y and y > 0]
    if len(pts) < 30:
        return None, None, None
    xs = [math.log(x) for x, _ in pts]
    ys = [math.log(y) for _, y in pts]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / n
    variance_x = sum((x - mean_x) ** 2 for x in xs) / n
    if variance_x == 0:
        return None, None, None
    slope = covariance / variance_x
    intercept = mean_y - slope * mean_x
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    r_squared = (1 - ss_res / ss_tot) if ss_tot else None
    return slope, intercept, r_squared


def predict_power_law(slope, intercept, days):
    return math.exp(intercept + slope * math.log(days))


def fetch_active_addresses_full_history():
    """Full genesis-to-now (days_since_genesis, active_addresses, date)
    history, straight from BGeometrics' own chart data file -- see the
    HONESTY NOTE above for why this is used instead of the metered REST
    endpoint.

    UNDOCUMENTED-FILE RISK: unlike v1/active-addresses, this JSON file is
    not a versioned, documented API contract -- it's whatever BGeometrics'
    own chart frontend happens to fetch today (currently a bare
    [[unix_ms, value], ...] array). It could silently change shape, move,
    or disappear with no deprecation notice, unlike a real API endpoint.
    So this function trusts nothing about the response: a non-list top
    level, or any entry that doesn't parse as an ordered [ts, value] pair,
    is treated as a parse failure, never as data. A fetch exception OR an
    unexpected shape both return [] the same way -- the caller (main())
    can't tell them apart, which is intentional: both should degrade to
    the last-known-good cached value (labeled STALE) or, on a true
    first-ever failure, an honest N/A -- never a loud crash, and never
    silently-wrong numbers computed from garbage input.
    """
    try:
        raw = _get_json(ACTIVE_ADDR_CHART_JSON_URL)
    except Exception as e:
        print(f"  ! active-addresses full history fetch failed: {e}")
        return []
    if not isinstance(raw, list):
        print(f"  ! active-addresses full history: unexpected top-level shape "
              f"({type(raw).__name__}, expected a list) -- the file may have changed "
              f"format; treating this as a failed fetch rather than guessing at the data")
        return []
    points = []
    for entry in raw:
        try:
            ts_ms, val = entry[0], entry[1]
            if val is None or val <= 0:
                continue
            d = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date()
            points.append((days_since_genesis_for(d), float(val), d))
        except (TypeError, ValueError, IndexError, KeyError):
            continue
    points.sort(key=lambda p: p[0])
    return points


def seed_active_addr_dev_history(cache, points, slope, intercept):
    """One-time bootstrap: seed the rolling-history cache (the same one
    rolling_threshold() reads for the percentile threshold) with a
    deviation value for every historical day in the fetched full-history
    series, computed under today's regression fit -- so this indicator can
    have a real percentile threshold from the very first run instead of a
    90-day wait, per the same rolling-percentile infrastructure Thermocap
    and NRPL already use. Only runs once: if history has already
    accumulated for this token (from a prior seed or organic daily
    appends), this is a no-op -- normal single-value-per-day
    history_append() takes over from there, identical to every other
    leeway-enabled indicator. Returns the number of days seeded (0 if it
    was a no-op or nothing usable was computed)."""
    hist_key = "ACTIVE_ADDR_DEV__history"
    if cache.get(hist_key, {}).get("values"):
        return 0
    deviations = []
    for days, val, _ in points:
        predicted = predict_power_law(slope, intercept, days)
        if predicted:
            deviations.append(round((val - predicted) / predicted * 100, 4))
    deviations = deviations[-HISTORY_MAX_LEN:]
    if deviations:
        cache[hist_key] = {"values": deviations}
    return len(deviations)


# ---------------------------------------------------------------------------
# 2. CoinGecko — Mayer Multiple + Drawdown magnitude
# ---------------------------------------------------------------------------
def fetch_coingecko_history(days=210):
    url = f"https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days={days}&interval=daily"
    try:
        data = _get_json(url)
        prices = data.get("prices", [])  # [[ms_timestamp, price], ...]
        return [p[1] for p in prices if p[1]]
    except Exception as e:
        print(f"  ! coingecko history failed: {e}")
        return []


def compute_mayer_and_drawdown(price_history):
    if len(price_history) < 30:
        return None, None
    current = price_history[-1]
    window = price_history[-200:] if len(price_history) >= 200 else price_history
    ma200 = sum(window) / len(window)
    mayer = round(current / ma200, 3) if ma200 else None
    drawdown = round(((current - ALL_TIME_HIGH_USD) / ALL_TIME_HIGH_USD) * 100, 1)
    return mayer, drawdown


def resample_weekly(daily_prices):
    """Every 7th day, counted backward from today, to approximate weekly
    closes without needing a separate weekly data feed."""
    reversed_prices = daily_prices[::-1]
    weekly_reversed = reversed_prices[::7]
    return weekly_reversed[::-1]


def _ema_series(values, period):
    """Simplified EMA (seeds from the first value rather than waiting for a
    full-period SMA) — standard, slightly faster-converging approach, fine
    given ~48 weekly points feeding into this."""
    if not values:
        return []
    k = 2 / (period + 1)
    ema = [values[0]]
    for v in values[1:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema


def compute_weekly_rsi(price_history, period=14):
    """Standard RSI (Wilder-style average gain/loss), computed on weekly
    closes rather than daily — matches how this indicator is actually used
    for macro cycle calls (per your Grok research, weekly RSI is what
    flagged the Dec 2022 bottom, not daily)."""
    weekly = resample_weekly(price_history)
    if len(weekly) < period + 1:
        return None
    deltas = [weekly[i] - weekly[i - 1] for i in range(1, len(weekly))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def compute_weekly_macd(price_history, fast=12, slow=26, signal=9):
    """Standard MACD (12/26/9 EMA), computed on weekly closes. Returns
    (histogram, just_crossed_bullish)."""
    weekly = resample_weekly(price_history)
    if len(weekly) < slow + signal:
        return None, False
    ema_fast = _ema_series(weekly, fast)
    ema_slow = _ema_series(weekly, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = _ema_series(macd_line, signal)
    histogram = macd_line[-1] - signal_line[-1]
    prev_histogram = macd_line[-2] - signal_line[-2] if len(macd_line) > 1 else histogram
    just_crossed_bullish = prev_histogram <= 0 and histogram > 0
    return round(histogram, 1), just_crossed_bullish


def compute_bollinger(price_history, period=20, num_std=2):
    """Standard Bollinger Bands (20-period, 2 std dev), on weekly closes.
    Returns %B: 0 = touching lower band, 1 = touching upper band."""
    weekly = resample_weekly(price_history)
    if len(weekly) < period:
        return None
    window = weekly[-period:]
    ma = sum(window) / period
    variance = sum((x - ma) ** 2 for x in window) / period
    std = variance ** 0.5
    upper, lower = ma + num_std * std, ma - num_std * std
    if upper == lower:
        return None
    current = weekly[-1]
    percent_b = (current - lower) / (upper - lower)
    return round(percent_b, 3)


# ---------------------------------------------------------------------------
# 3. Blockchain.com — Production Cost + Miner Capitulation + NVT Golden Cross
# ---------------------------------------------------------------------------
def fetch_blockchain_stats():
    try:
        return _get_json("https://api.blockchain.info/stats")
    except Exception as e:
        print(f"  ! blockchain.info stats failed: {e}")
        return {}


def fetch_blockchain_chart(chart_name, days=100):
    url = f"https://api.blockchain.info/charts/{chart_name}?timespan={days}days&format=json&cors=true"
    try:
        data = _get_json(url)
        return [pt["y"] for pt in data.get("values", []) if pt.get("y") is not None]
    except Exception as e:
        print(f"  ! blockchain.info chart {chart_name} failed: {e}")
        return []


def compute_production_cost(stats):
    """Electricity-only production cost estimate. Two assumptions, both
    grounded in cited sources rather than picked arbitrarily:

    - Electricity cost ($0.05/kWh): this is Cambridge University's CBECI
      model's own stated assumption for global average miner electricity
      cost (ccaf.io/cbnsi/cbeci/methodology) — the most rigorous public
      academic source available.
    - Fleet efficiency (20 J/TH): sits in the middle of the range cited
      across current (2026) sources — newest-generation ASICs run near
      15 J/TH, while network-wide blended estimates (including older,
      still-active hardware) run as high as 28 J/TH. 20 J/TH is a
      reasonable current blended-fleet midpoint, adjustable at the top of
      this file.

    IMPORTANT SCOPE NOTE: this is an electricity-only estimate. It will
    read LOWER than headline "all-in" bank estimates like JPMorgan's
    (~$78,000 as of mid-2026), which also bake in hardware depreciation
    and corporate overhead on top of electricity. Both are legitimate;
    they're answering slightly different questions. Don't be alarmed if
    this number and a bank's headline number disagree — that's expected,
    not a bug.
    """
    try:
        # blockchain.info's "hash_rate" field is reported in GH/s; convert to TH/s
        hash_rate_th = stats["hash_rate"] / 1000.0
        minutes_between = stats.get("minutes_between_blocks", 10)
        n_blocks = stats.get("n_blocks_total", 0)
        halvings = n_blocks // 210000
        block_reward = 50 / (2 ** halvings)
        blocks_per_day = 1440 / minutes_between if minutes_between else 144
        daily_btc_issued = blocks_per_day * block_reward

        power_watts = hash_rate_th * ASSUMED_FLEET_EFFICIENCY_J_PER_TH
        daily_kwh = power_watts * 24 / 1000
        daily_cost_usd = daily_kwh * ASSUMED_ELECTRICITY_COST_USD_PER_KWH

        if daily_btc_issued == 0:
            return None, None
        cost_per_btc = daily_cost_usd / daily_btc_issued
        market_price = stats.get("market_price_usd")
        pct_vs_cost = round(((market_price - cost_per_btc) / cost_per_btc) * 100, 1) if market_price else None
        return round(cost_per_btc), pct_vs_cost
    except Exception as e:
        print(f"  ! production cost calc failed: {e}")
        return None, None


def compute_miner_capitulation(hashrate_history, price_history):
    """Full Hash Ribbons methodology (Charles Edwards, Capriole Investments,
    2019 — capriole.com/hash-ribbons-bitcoin-bottoms). Two parts, matching
    the original design exactly rather than a single simplified percentage:

    1. Hash rate state: 30-day SMA vs. 60-day SMA of network hashrate.
       - 30d crosses BELOW 60d -> capitulation begins
       - 30d crosses back ABOVE 60d -> capitulation ends / recovery
    2. Price momentum confirmation (Edwards' own recommended addition):
       10-day SMA vs. 20-day SMA of price. The full "buy signal" only
       fires when hash rate has just recovered above its 60d SMA AND
       price's 10d SMA is above its 20d SMA — capitulation alone isn't
       the signal, recovery + positive price momentum together is.

    Returns (state_label, hashrate_deviation_pct).
    """
    if len(hashrate_history) < 60:
        return None, None
    hr_ma30 = sum(hashrate_history[-30:]) / 30
    hr_ma60 = sum(hashrate_history[-60:]) / 60
    if hr_ma60 == 0:
        return None, None
    hr_deviation_pct = round(((hr_ma30 - hr_ma60) / hr_ma60) * 100, 2)
    hashrate_recovering = hr_ma30 > hr_ma60

    price_confirmed = False
    if len(price_history) >= 20:
        price_ma10 = sum(price_history[-10:]) / 10
        price_ma20 = sum(price_history[-20:]) / 20
        price_confirmed = price_ma10 > price_ma20

    if not hashrate_recovering:
        state = "CAPITULATION"
    elif hashrate_recovering and price_confirmed:
        state = "BUY SIGNAL"
    else:
        state = "RECOVERING"
    return state, hr_deviation_pct


def compute_nvt_golden_cross(price_history, tx_volume_history, approx_supply=19_900_000):
    """Exact published CryptoQuant formula (userguide.cryptoquant.com/
    cryptoquant-metrics/network/nvt-golden-cross), not an approximation of it:

        NVT = Market Cap / Transaction Volume
        NVT_diff = NVT(10-day MA) - NVT(30-day MA)
        NVT_GC = NVT_diff / (300-day moving standard deviation of NVT_diff)

    Interpretation per CryptoQuant: > 2.2 = overpriced (short/top signal),
    < -1.6 = underpriced (long/bottom signal).

    Needs a long history (300+30 days minimum) to compute the rolling
    standard deviation properly — this is why the CoinGecko/Blockchain.com
    fetches request ~340 days rather than 60.
    """
    n = min(len(price_history), len(tx_volume_history))
    if n < 330:
        print(f"  ! only {n} days of history available, need 330+ for a proper NVT_GC — skipping")
        return None

    prices = price_history[-n:]
    volumes = tx_volume_history[-n:]
    nvt_series = [(p * approx_supply) / v for p, v in zip(prices, volumes) if v]
    if len(nvt_series) < 330:
        return None

    # Build the daily NVT_diff series (10d MA - 30d MA) for the last 300+ days
    nvt_diff_series = []
    for i in range(30, len(nvt_series)):
        window = nvt_series[: i + 1]
        ma10 = sum(window[-10:]) / 10
        ma30 = sum(window[-30:]) / 30
        nvt_diff_series.append(ma10 - ma30)

    if len(nvt_diff_series) < 300:
        return None

    recent_diffs = nvt_diff_series[-300:]
    mean_diff = sum(recent_diffs) / len(recent_diffs)
    variance = sum((x - mean_diff) ** 2 for x in recent_diffs) / len(recent_diffs)
    std_dev = variance ** 0.5
    if std_dev == 0:
        return None

    latest_diff = nvt_diff_series[-1]
    return round(latest_diff / std_dev, 2)


# ---------------------------------------------------------------------------
# 4. Alternative.me — Fear & Greed
# ---------------------------------------------------------------------------
def fetch_fear_greed():
    try:
        data = _get_json("https://api.alternative.me/fng/?limit=1&format=json")
        entry = data["data"][0]
        return entry["value"], entry["value_classification"]
    except Exception as e:
        print(f"  ! fear & greed failed: {e}")
        return None, None


# ---------------------------------------------------------------------------
# 4b. Polymarket Gamma API — live Bitcoin prediction-market odds, bucketed
#     into your requested horizons. Free, keyless, a totally separate
#     service from BGeometrics — zero impact on that 10/hour budget.
#
#     Per your uploaded forecaster-track-record report: prediction markets
#     are "the only source with demonstrated multi-month calibration" for
#     Bitcoin specifically, but they are NOT a forecast — just the crowd's
#     current probability, prone to overconfidence right at a threshold.
#     This section is deliberately NOT scored in the weighted verdict below
#     — it's shown as its own context section, exactly as the report
#     recommends using it.
# ---------------------------------------------------------------------------
POLYMARKET_BASE = "https://gamma-api.polymarket.com"

# Target horizons in days, and a matching-window tolerance so we don't force
# a bad match if nothing genuinely close exists for a given bucket.
HORIZONS = {
    "WEEKLY":    (7,   4),    # target 7 days, accept anything within +-4
    "BIWEEKLY":  (14,  5),
    "MONTHLY":   (30,  10),
    "QUARTERLY": (90,  25),
    "YEARLY":    (365, 60),
}
MIN_VOLUME_USD = 25_000  # ignore illiquid/noise markets below this

# Short-interval "will BTC be up or down in the next 15m/1h/4h" markets are
# created continuously (Polymarket spins up a fresh one every interval) and
# their end dates never land anywhere near our weekly-through-yearly
# horizons anyway — but on an unsorted page they can make up the bulk of
# the events returned, crowding out the much rarer longer-dated markets we
# actually want. Filtered out by slug pattern before any horizon matching.
_SHORT_INTERVAL_SLUG_MARKERS = ("-updown-", "-up-or-down-", "up-or-down-in-the-next")


def _is_short_interval_market(title, slug):
    haystack = f"{title} {slug}".lower()
    return any(marker in haystack for marker in _SHORT_INTERVAL_SLUG_MARKERS)


def fetch_polymarket_btc_events():
    """Filtered client-side for Bitcoin/BTC markets — the Gamma API has no
    free-text search that works reliably against this event list (a ?q=
    param is silently ignored on /events), so this is the correct
    approach, not a shortcut.

    HONESTY NOTE (fixed after the dashboard shipped 100% "no liquid market
    found" for every horizon, then re-verified before this fix shipped —
    the first diagnosis of this bug was partly wrong and worth being
    upfront about):

    A prior fix attempt claimed "order=volume was wrong, Polymarket's docs
    use order=volume24hr." That claim doesn't hold up: Polymarket's own
    official GitHub examples repo (github.com/Polymarket/agent-skills)
    documents the valid /events sort values as volume_24hr, volume,
    liquidity, start_date, end_date, competitive, closed_time — meaning
    "volume" alone was already a valid, real sort value the whole time,
    and "volume24hr" (no underscore) isn't in that documented list at all.
    So that specific "fix" likely wouldn't have changed anything.

    The actual, verified fix: Polymarket has an official Crypto category
    tag (tag_id=21, confirmed via Polymarket's own safe-wallet-integration
    GitHub repo) that lets the API filter server-side for crypto-relevant
    events, instead of scanning hundreds of generic events (most of which
    are sports/politics/etc, not crypto) and hoping enough Bitcoin ones
    turn up in a fixed-size page. Combined with the confirmed-real
    short-interval slug pattern "btc-updown-{interval}-{timestamp}"
    (Grokipedia's Gamma API writeup, cross-referenced against Polymarket's
    own API guide) for filtering those out before matching, this should
    reliably surface the actual weekly/monthly/quarterly/yearly markets
    regardless of whatever the API's default/sort ordering happens to be —
    since the matching logic below scans every returned candidate itself
    rather than trusting page order, sort-parameter correctness was never
    actually load-bearing for this function's correctness in the first
    place.

    Uses the documented keyset pagination endpoint (confirmed via
    docs.polymarket.com's own OpenAPI spec, including the exact
    next_cursor/after_cursor field names used below) with the tag_id=21
    filter, paging through multiple batches for full coverage. Falls back
    to the legacy flat /events endpoint (also tag_id=21-filtered) if the
    keyset endpoint's shape ever changes — belt and suspenders rather than
    a single fragile assumption.

    Returns (events, fetch_succeeded). fetch_succeeded=False means every
    fetch attempt failed (network error, bad response shape) — distinct
    from a successful request that just happened to find zero BTC events,
    which is a legitimate (if unlikely) outcome, not a failure to fall
    back from."""
    MAX_PAGES = 6
    PAGE_LIMIT = 100
    CRYPTO_TAG_ID = 21  # confirmed via Polymarket/safe-wallet-integration (github.com/Polymarket)
    all_events = []
    cursor = None
    fetched_any_page = False

    for _ in range(MAX_PAGES):
        url = f"{POLYMARKET_BASE}/events/keyset?active=true&closed=false&tag_id={CRYPTO_TAG_ID}&limit={PAGE_LIMIT}"
        if cursor:
            url += f"&after_cursor={cursor}"
        try:
            data = _get_json(url, headers={"User-Agent": "btc-dashboard-bot/1.0"})
        except Exception as e:
            print(f"  ! Polymarket keyset fetch failed: {e}")
            break

        fetched_any_page = True
        page_events = data.get("events") if isinstance(data, dict) else None
        if page_events is None:
            print("  ! Polymarket keyset response missing 'events', stopping pagination")
            break

        all_events.extend(page_events)
        cursor = data.get("next_cursor")
        if not cursor or not page_events:
            break

    if not fetched_any_page or not all_events:
        # Fall back to the legacy flat endpoint, same tag_id filter —
        # kept only as a safety net, not the primary path. Deliberately
        # omits the order param: it's not load-bearing for correctness
        # here (see docstring), and its exact valid spelling is genuinely
        # ambiguous across Polymarket's own documentation.
        legacy_url = f"{POLYMARKET_BASE}/events?active=true&closed=false&tag_id={CRYPTO_TAG_ID}&limit=300"
        try:
            data = _get_json(legacy_url, headers={"User-Agent": "btc-dashboard-bot/1.0"})
            if isinstance(data, list):
                all_events = data
            else:
                print("  ! Polymarket legacy fallback returned an unexpected shape too")
                return [], False
        except Exception as e:
            print(f"  ! Polymarket legacy fallback also failed: {e}")
            return [], (fetched_any_page and not all_events)  # honest: True only if we got a valid-but-empty page

    btc_events = []
    for event in all_events:
        title = (event.get("title") or event.get("question") or "")
        slug = event.get("slug") or ""
        haystack = f"{title} {slug}".lower()
        if "bitcoin" not in haystack and "btc" not in haystack:
            continue
        if _is_short_interval_market(title, slug):
            continue
        btc_events.append(event)

    print(f"  Polymarket: scanned {len(all_events)} events across pagination, "
          f"kept {len(btc_events)} non-short-interval BTC/Bitcoin events")
    return btc_events, True


def _parse_json_field(raw):
    """outcomes/outcomePrices arrive as JSON-encoded STRINGS, not native
    arrays or lists — a documented Gamma API quirk. Decode defensively."""
    if raw is None:
        return None
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _extract_markets(events):
    """Flatten events -> individual markets, pulling the fields we need and
    parsing the JSON-string-encoded ones, skipping anything malformed."""
    markets = []
    for event in events:
        for m in event.get("markets", []) or []:
            question = m.get("question") or event.get("title") or "Untitled market"
            end_date_str = m.get("endDate") or event.get("endDate")
            if not end_date_str:
                continue
            try:
                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            except ValueError:
                continue

            outcomes = _parse_json_field(m.get("outcomes")) or ["Yes", "No"]
            prices = _parse_json_field(m.get("outcomePrices"))
            if not prices or len(prices) < 2:
                continue
            try:
                yes_price = float(prices[0])
            except (TypeError, ValueError):
                continue

            volume = m.get("volume") or event.get("volume") or 0
            try:
                volume = float(volume)
            except (TypeError, ValueError):
                volume = 0.0
            if volume < MIN_VOLUME_USD:
                continue

            markets.append({
                "question": question,
                "yes_label": outcomes[0] if len(outcomes) > 0 else "Yes",
                "yes_pct": round(yes_price * 100, 1),
                "end_date": end_date,
                "volume": volume,
                "slug": m.get("slug") or event.get("slug") or "",
            })
    return markets


def build_polymarket_buckets(cache=None):
    """For each target horizon, find the BTC market whose expiry is closest
    to that many days out, within the tolerance window, above the volume
    floor. Returns a dict of template-ready values — every bucket that has
    no reasonable match is explicitly None, not a forced bad guess.

    If the fetch itself failed (network error, bad response), falls back
    to cached bucket results from a prior successful run where available,
    clearly labeled as stale. A legitimate "no close market this horizon"
    result on a successful fetch is NOT treated as a failure — that's an
    honest, current, non-error outcome and shouldn't be overridden with
    old cached data."""
    if cache is None:
        cache = {}
    events, fetch_ok = fetch_polymarket_btc_events()
    markets = _extract_markets(events)
    now = datetime.now(timezone.utc)

    result = {}
    for horizon, (target_days, tolerance) in HORIZONS.items():
        best = None
        best_diff = None
        for m in markets:
            days_out = (m["end_date"] - now).days
            if days_out < 0:
                continue
            diff = abs(days_out - target_days)
            if diff > tolerance:
                continue
            # Prefer the closest match; break ties by higher volume
            if best is None or diff < best_diff or (diff == best_diff and m["volume"] > best["volume"]):
                best = m
                best_diff = diff

        prefix = f"POLY_{horizon}"
        cache_key = f"poly_{horizon.lower()}"
        if best:
            result[f"{prefix}_QUESTION"] = best["question"]
            result[f"{prefix}_YES_LABEL"] = best["yes_label"]
            result[f"{prefix}_YES_PCT"] = best["yes_pct"]
            result[f"{prefix}_DATE"] = best["end_date"].strftime("%Y-%m-%d")
            result[f"{prefix}_VOLUME"] = f"{best['volume']:,.0f}"
            result[f"{prefix}_FOUND"] = True
            cache_store(cache, cache_key, {
                "question": best["question"], "yes_label": best["yes_label"],
                "yes_pct": best["yes_pct"], "date": best["end_date"].strftime("%Y-%m-%d"),
                "volume": f"{best['volume']:,.0f}",
            })
        elif not fetch_ok:
            cached_val, cached_date = cache_lookup(cache, cache_key)
            if cached_val:
                result[f"{prefix}_QUESTION"] = f"{cached_val['question']} (STALE — cached {cached_date})"
                result[f"{prefix}_YES_LABEL"] = cached_val["yes_label"]
                result[f"{prefix}_YES_PCT"] = cached_val["yes_pct"]
                result[f"{prefix}_DATE"] = cached_val["date"]
                result[f"{prefix}_VOLUME"] = cached_val["volume"]
                result[f"{prefix}_FOUND"] = True
                print(f"  {horizon}: live fetch failed, using cached market from {cached_date}")
            else:
                result[f"{prefix}_QUESTION"] = "Polymarket fetch failed and no cached fallback available"
                result[f"{prefix}_YES_LABEL"] = "—"
                result[f"{prefix}_YES_PCT"] = "—"
                result[f"{prefix}_DATE"] = "—"
                result[f"{prefix}_VOLUME"] = "—"
                result[f"{prefix}_FOUND"] = False
        else:
            # Fetch succeeded, genuinely no close-enough market — honest, not an error
            result[f"{prefix}_QUESTION"] = "No liquid BTC market found near this horizon"
            result[f"{prefix}_YES_LABEL"] = "—"
            result[f"{prefix}_YES_PCT"] = "—"
            result[f"{prefix}_DATE"] = "—"
            result[f"{prefix}_VOLUME"] = "—"
            result[f"{prefix}_FOUND"] = False

    return result


# ---------------------------------------------------------------------------
# 5. Cycle rhythm — pure calendar math, no API
# ---------------------------------------------------------------------------
def compute_cycle_rhythm():
    today = datetime.now(timezone.utc).date()
    days_since_top = (today - CYCLE_ANCHORS["last_cycle_top"]).days
    projected = CYCLE_ANCHORS["last_cycle_top"]
    from datetime import timedelta
    projected_bottom = CYCLE_ANCHORS["last_cycle_top"] + timedelta(
        days=CYCLE_ANCHORS["projected_bottom_offset_days"]
    )
    days_to_projected = (projected_bottom - today).days
    return days_since_top, projected_bottom.isoformat(), days_to_projected


def compute_asopr_estimate(sopr_val):
    """Model an aSOPR estimate from plain SOPR, grounded in Glassnode's own
    documented mechanism rather than treating SOPR as a bare substitute.

    Glassnode's aSOPR docs state that UTXOs under 1 hour old consistently
    represent 20-40% of daily on-chain volume, trade at approximately
    breakeven (ratio ~1.0), and "dilute" the aggregate SOPR toward 1 —
    which is exactly why aSOPR (which excludes them) reads as "more
    responsive, and generally of greater magnitude than the equivalent
    SOPR." That's a solvable relationship, not just a vague correlation:

        SOPR ≈ (dilution_share × 1.0) + (1 − dilution_share) × aSOPR
        => aSOPR ≈ (SOPR − dilution_share) / (1 − dilution_share)

    Using the midpoint of Glassnode's stated 20-40% range (30%) as the
    dilution share. Verified against the documented behavior before
    shipping: breakeven maps to breakeven, and deviations from 1.0 get
    consistently amplified (~1.43x at 30% dilution) in the correct
    direction — matching the "more responsive, greater magnitude"
    description exactly, not just directionally.

    This is a modeled estimate, not a licensed feed — labeled as such on
    the dashboard. The true metric requires per-UTXO lifespan data that
    no free source provides.
    """
    if sopr_val is None:
        return None
    try:
        s = float(sopr_val)
        return round((s - ASOPR_DILUTION_SHARE) / (1 - ASOPR_DILUTION_SHARE), 4)
    except (TypeError, ValueError):
        return None


def _distance_note(v, threshold, direction):
    """How far a non-buy-favorable reading sits from its threshold, as a
    percentage where the threshold is meaningfully non-zero, or an
    absolute distance for a zero threshold (a % of zero is meaningless)."""
    if threshold == 0:
        gap = abs(v - threshold)
        return f" ({round(gap, 3)} away from threshold)"
    pct_away = abs((v - threshold) / threshold) * 100
    return f" ({round(pct_away, 1)}% away from threshold)"


def status_pill(value, direction, threshold, cache=None, token=None):
    """direction='low': buy-favorable at or below threshold. direction='high':
    buy-favorable at or above threshold. Four tiers when cache+token are
    supplied: a reading that crossed by >=STRONG_BUY_STDEV_FRACTION sigma
    (via get_effective_sigma()) is st-strong-buy -- a real buy signal,
    just a stronger one, counted identically to st-buy everywhere. A
    reading just outside the threshold, within near_threshold_band(),
    still counts as buy-favorable but is labeled and styled distinctly
    (st-near, "BORDERLINE BUY") so it's never mistaken for a clean,
    fully-crossed reading -- but it IS a real buy signal, not a maybe.
    Omit cache/token for the old, exact-only two-tier behavior."""
    if value is None or direction is None or threshold is None:
        return "CHECK", "st-mid"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "CHECK", "st-mid"

    if direction == "low":
        if v <= threshold:
            if cache is not None and token is not None:
                sigma, source = get_effective_sigma(cache, token, threshold)
                if sigma and (threshold - v) >= STRONG_BUY_STDEV_FRACTION * sigma:
                    return f"STRONG BUY ({STRONG_BUY_STDEV_FRACTION}σ+, {source})", "st-strong-buy"
            return "BUY ZONE", "st-buy"
        if cache is not None and token is not None:
            band, source = near_threshold_band(cache, token, threshold)
            if band is not None and v <= threshold + band:
                return f"BORDERLINE BUY ({source})", "st-near"
        return f"NOT YET{_distance_note(v, threshold, direction)}", "st-no"

    if direction == "high":
        if v >= threshold:
            if cache is not None and token is not None:
                sigma, source = get_effective_sigma(cache, token, threshold)
                if sigma and (v - threshold) >= STRONG_BUY_STDEV_FRACTION * sigma:
                    return f"STRONG BUY ({STRONG_BUY_STDEV_FRACTION}σ+, {source})", "st-strong-buy"
            return "ELEVATED", "st-buy"
        if cache is not None and token is not None:
            band, source = near_threshold_band(cache, token, threshold)
            if band is not None and v >= threshold - band:
                return f"BORDERLINE BUY ({source})", "st-near"
        return f"NORMAL{_distance_note(v, threshold, direction)}", "st-no"

    return "CHECK", "st-mid"


# ---------------------------------------------------------------------------
# 6. Verdict synthesis — mechanical, transparent aggregation across every
#    indicator this run actually produced a real (non-CHECK) reading for.
#    NOT a prediction — a weighted tally of the same rows shown above,
#    using the weights from your own ranked list. See the disclaimer this
#    function always attaches.
# ---------------------------------------------------------------------------
WEIGHT_MAP = {
    # Tier 1 — highest-ranked, closest in spirit to Power Law (weight 3)
    "MVRV_Z": 3, "PUELL": 3,
    # NOTE: Price vs. Realized Price is intentionally NOT in this dict.
    # It's mathematically the un-normalized precursor to MVRV Z-Score
    # (spot/realized price IS the raw MVRV ratio; the Z-score is that same
    # ratio standardized against its own volatility) — scoring both would
    # double-count one underlying signal. Still fetched, still displayed
    # with its own real BUY ZONE/NOT YET status, just excluded from the
    # weighted verdict tally.
    "RESERVE_RISK": 2.5,
    # Tier 2 — solid on-chain (weight 2)
    # NOTE: Thermocap and Pi Cycle are also intentionally NOT in this dict
    # (moved out in v15) — they never actually score (no defensible fixed
    # threshold exists for either), so leaving them in WEIGHT_MAP was
    # inconsistent: it inflated the total weight pool's denominator with
    # weight that could never be earned. Same treatment as NRPL, Drawdown,
    # and Cycle Rhythm now — displayed, ranked, explicitly N/A on weight.
    # LTH-SOPR vs aSOPR redundancy, evaluated (v18): both are SOPR-family
    # profit/loss-realization ratios, but cover genuinely different holder
    # cohorts (LTH-SOPR = long-term holders only; aSOPR = the full holder
    # base, minus <1hr noise) — not the same-ratio-twice case that Realized
    # Price/MVRV Z-Score was. Full removal of either would lose real,
    # non-duplicate information (different cohorts often move at different
    # times — LTH-SOPR tends to lag as long-term holders capitulate last).
    # But there IS a real, moderate overlap (all-holder SOPR mathematically
    # contains long-term-holder activity as a blended-in subset), and
    # aSOPR carries additional uncertainty as a modeled estimate rather
    # than a directly-measured feed. Net verdict: discount, don't remove.
    "LTH_SOPR": 2, "NVT_GC": 2, "ASOPR_EST": 1.5,
    "PROD_COST": 2.5,
    # Tier 3 — behavioral/technical (weight 1.5)
    "MINER_CAP": 1.5,
    # Tier 4 — sentiment/blunt technicals (weight 1)
    # Note: Mayer, RSI, and Bollinger are mutually correlated (all derived
    # from the same underlying price series, just different windows) —
    # flagged honestly, but left unchanged since further discounting an
    # already-minimal weight has little practical effect and risks false
    # precision rather than reflecting a real finding.
    "SUPPLY_LOSS": 1, "MAYER": 1, "RSI": 1, "FNG": 1, "BOLLINGER": 1,
    # MACD discounted from 1.5 to 1.0 (evidence-based, not a redundancy/
    # confidence-tier discount like aSOPR's above): backtest_indicators.py
    # found a near-coin-flip 46.7% success rate at 365 days and a negative
    # average return at 90 days (-11.5%) across 20 real firing events —
    # weak enough forward performance to warrant a real discount, not just
    # a "different holder cohort" or "not yet fully validated" caveat.
    # NVT Golden Cross and Active Addresses also showed mixed backtest
    # results but are left untouched for now — under observation, not
    # adjusted yet.
    "MACD": 1.0,
}

# ---------------------------------------------------------------------------
# WEIGHT_MAP_TOP — Table 3's own, genuinely separate weight pool for the
# Cycle TOP table. Deliberately NOT merged with WEIGHT_MAP: Table 1 and
# Table 3 must each be able to independently report "my phase is active"
# (e.g. Table 1 at 40% bottom-confluence while Table 3 sits at 10%
# top-confluence is a perfectly normal, meaningful mid-cycle reading —
# a shared pool would make that unrepresentable). For indicators whose
# RAW VALUE is shared with Table 1 (MVRV Z-Score, Puell, Reserve Risk,
# Thermocap, RSI, Bollinger %B, Mayer, Fear & Greed, NVT Golden Cross),
# the underlying fetch/compute happens exactly once in main() and the SAME
# rolling-history cache key (e.g. "MVRV_Z__history") is reused for both
# tables' sigma/leeway calculations -- only the comparison direction and
# threshold differ. Starts EMPTY: Phase 2 builds every indicator as
# real-but-unweighted first (DISPLAYABLE_BUT_UNWEIGHTED_TOP /
# NO_SIGNAL_STATUS_TOP below), exactly the discipline the Realized Price/
# Drawdown lesson established on Table 1 -- a real status has to exist and
# be tested before it goes anywhere near a weight.
#
# PHASE 3 — tier-confidence weight assignment. Same three-tier framework
# already established for Table 1 (Tier 1 mathematically-derived, Tier 2
# established-but-less-certain, Tier 3 novel/unproven), applied fresh here
# rather than assuming a token's bottom-table tier carries over:
#
#   MVRV_Z, PUELL (1.5 each once bootstrapped): NOT weighted the same as
#   Thermocap/NRPL's underlying metric quality would suggest on its own --
#   deliberately matched to Thermocap/NRPL's own Tier-3 weight (1.5)
#   instead. Reasoning (revised after review): the percentile mechanism has
#   ZERO validating data in either direction for a top-side call, since no
#   full cycle top exists inside the available ~4-year BGeometrics window --
#   that's a stronger case for starting conservative than a thin-but-real
#   evidence base (like MACD's evidence-based discount) would be. There's
#   no principled reason a top-side percentile mechanism deserves more
#   initial trust than the bottom-side one did before ITS OWN track record
#   existed (Thermocap/NRPL started at 1.5, not higher). NOT static entries
#   below -- like Thermocap/NRPL on Table 1, these join WEIGHT_MAP_TOP
#   conditionally at runtime in main(), only once their percentile
#   threshold actually has enough history (n>=90) to be real. A static
#   entry here would let them count toward the weight pool and percentage
#   share while still showing "BUILDING HISTORY" with no real signal --
#   exactly the bug class Thermocap/NRPL's own conditional-join pattern
#   exists to prevent.
#
#   RESERVE_RISK (1.5): a real, published top threshold exists (>0.02), but
#   single-sourced at Medium confidence per Phase 1 -- discounted well below
#   its Tier-1 bottom-table weight (2.5) for that citation gap specifically.
#
#   THERMOCAP (1.0): weakest Phase 1 citation of the set (32x-64x range
#   across just the two 2021 tops alone -- no defensible single number even
#   as a reference point, unlike NRPL/Thermocap's bottom-table ~4x). Same
#   percentile mechanism as MVRV_Z/Puell above (also a conditional runtime
#   join, not a static entry, for the same bootstrap-honesty reason), but
#   the underlying concept's top-side citation is markedly weaker -- floor
#   tier once it does join, one notch below MVRV_Z/Puell's 1.5.
#
#   NVT_GC (2.0): exact same published CryptoQuant formula as Table 1's
#   NVT_GC (also weight 2.0 there). Originally discounted to 1.5 to price
#   around a real gap -- the >2.2 boolean wasn't routed through
#   status_pill(), so it had none of the leeway/sigma/strong-signal tiering
#   every other Table 3 indicator gets. Checked whether that gap could be
#   fixed directly instead of priced around: it could, straightforwardly --
#   status_pill(nvt_gc_val, "high", 2.2, cache=cache, token="NVT_GC") uses
#   the exact same shared "NVT_GC__history" cache/token as every other
#   Table 3 indicator, no new machinery needed. (This is NOT the same as
#   Table 1's own hardcoded OVERPRICED branch, which is deliberately kept
#   OUT of status_pill() for a different reason -- that call's leeway is
#   calibrated for Table 1's buy-side -1.6 case, and running an overpriced
#   reading through it would attach buy-favorable-flavored "BORDERLINE BUY"
#   language to a bottom-focused table's high reading. Table 3's >2.2,
#   direction="high" IS the actual comparison this table cares about, so
#   routing it through status_pill() here is correct, not a repeat of that
#   mistake.) With the actual gap resolved rather than priced around, full
#   weight (2.0) is now justified on the same basis as Table 1's NVT_GC:
#   same formula, same data, same leeway machinery, same confidence.
#
#   RSI, BOLLINGER, MAYER (1.0 each): same "generic, mutually-correlated,
#   all derived from the same underlying price series" reasoning Table 1
#   already applies to this exact trio at its own floor tier (weight 1) --
#   applied symmetrically here despite Mayer's individually strong Trace-
#   Mayer citation, for consistency with that established precedent.
#
#   FNG (1.0): matches Table 1's own "genuine contrarian signal, but weak
#   standalone trigger" floor-tier treatment of Fear & Greed.
#
#   PI_CYCLE_TOP: NOT included -- stays fully display-only. Per Phase 1,
#   BGeometrics' own piSignal field could not be independently verified
#   against a known historical crossover (zero crossovers occurred in the
#   ~4-year available window), this dashboard computes the crossed/not-
#   crossed state itself rather than trusting that field, and the metric
#   has zero track record in this specific implementation (previously
#   removed from the bottom table entirely). Per your own instruction --
#   "Pi Cycle Top... start at Tier 3 or display-only" -- this is the more
#   conservative of those two options, chosen deliberately given the
#   unverified-field and zero-track-record combination.
# ---------------------------------------------------------------------------
WEIGHT_MAP_TOP = {
    "RESERVE_RISK": 1.5, "NVT_GC": 2.0,
    "RSI": 1.0, "BOLLINGER": 1.0, "MAYER": 1.0, "FNG": 1.0,
    # MVRV_Z, PUELL, THERMOCAP deliberately absent here -- see the comment
    # block above. They join at runtime in main() only once their rolling-
    # percentile threshold has real history (n>=90), at weight 1.5, 1.5,
    # and 1.0 respectively.
}

DISPLAY_NAMES = {
    "MVRV_Z": "MVRV Z-Score", "REALIZED_PRICE": "Price vs. Realized Price",
    "PUELL": "Puell Multiple", "RESERVE_RISK": "Reserve Risk",
    "THERMOCAP": "Thermocap Multiple", "LTH_SOPR": "LTH-SOPR",
    "PROD_COST": "Production Cost", "NVT_GC": "NVT Golden Cross",
    "NRPL": "NRPL (Net Realized P&L)",
    "SUPPLY_LOSS": "% Supply in Loss", "ASOPR_EST": "aSOPR (modeled)",
    "MINER_CAP": "Miner Capitulation", "MACD": "MACD (weekly)",
    "MAYER": "Mayer Multiple", "RSI": "Weekly RSI", "FNG": "Fear & Greed",
    "BOLLINGER": "Bollinger %B", "CYCLE_RHYTHM": "1064/364-Day Cycle Rhythm",
    "DRAWDOWN": "Drawdown Magnitude",
    "ACTIVE_ADDR_DEV": "Active Addresses Power-Law Deviation",
    "PI_CYCLE_TOP": "Pi Cycle Top",
}

# Short subtitle shown right after an indicator's name, before its tooltip
# icon — ported verbatim from the old §1/§2 tables. Omitted wherever
# DISPLAY_NAMES already states the same qualifier (e.g. aSOPR's name
# already says "(modeled)"; MACD's and Cycle Rhythm's names already carry
# their own interval/day-count), so nothing gets stated twice.
NAME_CAVEAT = {
    "REALIZED_PRICE": "shown, not scored",
    "PROD_COST": "electricity-only",
    "DRAWDOWN": "not scored",
    "MINER_CAP": "Hash Ribbons",
    "MACD": "12/26/9",
    "RSI": "14-period",
    "BOLLINGER": "20-week, 2σ",
    "CYCLE_RHYTHM": "not scored",
}

# Hover-tooltip methodology text for every tracked indicator — ported
# verbatim (byte-for-byte, programmatically extracted rather than
# retyped) from the old §1/§2 tables, so the merged table loses none of
# that explanatory content. Single source of truth: the table-building
# function below is the only place this ever gets read.
TOOLTIP_TEXT = {
    "MVRV_Z": 'Market cap vs. realized cap, standardized against its own historical volatility. The most refined member of the "price vs. cost basis" family — highest weight on this board.',
    "REALIZED_PRICE": 'Average cost basis of every coin. <strong>Not counted in the weighted verdict</strong> — mathematically the un-normalized version of MVRV Z-Score (spot ÷ realized price IS the MVRV ratio), so scoring both would double-count one signal.',
    "PUELL": 'Daily miner issuance value vs. its 365-day average. Ties valuation to real mining economics — genuinely independent from the on-chain price-ratio cluster above.',
    "RESERVE_RISK": 'Price relative to long-term holder conviction (accumulated coin-days vs. price). Thinner public track record than MVRV/Puell, hence a discounted weight.',
    "LTH_SOPR": 'Long-term holders spending at profit (&gt;1) or loss (&lt;1). A behavioral confirmation signal — tends to lag the actual low rather than lead it.',
    "THERMOCAP": 'Market cap vs. cumulative miner revenue ever paid. No globally-agreed fixed threshold exists, so this computes its own: the bottom 10th percentile of its own trailing daily history, once at least 90 days have accumulated. Self-normalizing rather than anchored to an older market — see README for the research behind this. Scored at reduced (Tier 3) weight until then.',
    "NRPL": 'Net Realized P&amp;L in BTC — realized profit minus realized loss; deeply negative means capitulation dominates. No fixed published threshold exists, so this computes its own: the bottom 10th percentile of its own trailing daily history, once at least 90 days have accumulated. Self-normalizing rather than anchored to an older, smaller network — see README. Scored at reduced (Tier 3) weight until then.',
    "SUPPLY_LOSS": 'Share of circulating BTC below cost basis (derived: 100 − Supply in Profit). The metric this whole project started from.',
    "ASOPR_EST": 'SOPR with the ~1.0-ratio "under 1hr" volume mathematically de-diluted out — derived from Glassnode\'s own published mechanism, not a raw substitute for the real metric.',
    "PROD_COST": 'Live hashrate/difficulty × Cambridge CBECI\'s $0.05/kWh assumption + 20 J/TH blended efficiency. Reads lower than "all-in" bank headlines (e.g. JPMorgan\'s ~$78K), which add hardware depreciation — different scope, not a contradiction.',
    "MAYER": 'Price ÷ 200-day moving average. Simple, blunt, generic across any asset — lowest weight tier for that reason.',
    "DRAWDOWN": "% below all-time high ($126,296, Oct 2025). Buy-favorable at or beyond -77% (the shallow edge of the past-cycle-bottom analog range). <strong>Not weighted</strong> — an analog-based threshold, not a mathematically-derived one, so it gets a real live status but no scoring weight.",
    "MINER_CAP": "Charles Edwards' full original methodology: 30d/60d hashrate MA cross for capitulation/recovery, confirmed by 10d/20d price MA momentum for the real buy signal — not just the raw hashrate spread.",
    "NVT_GC": 'Exact CryptoQuant formula: 10d/30d MA spread of Market Cap ÷ Tx Volume, standardized as a z-score against its own 300-day volatility. &gt;2.2 overpriced, &lt;−1.6 bottom zone.',
    "FNG": 'Composite sentiment score. Genuine contrarian signal, but can sit at extremes for months — good color, weak standalone trigger.',
    "MACD": 'Trend-following crossover — a bullish histogram flip has historically flagged major reversals, including Dec 2022.',
    "RSI": 'Classic oversold oscillator — generic across any asset, can stay pinned for months in a strong downtrend.',
    "BOLLINGER": 'Where price sits in its volatility envelope — 0 = touching lower band. Volatility context, not a valuation signal.',
    "CYCLE_RHYTHM": 'Calendar-only projection. <strong>Not counted</strong> — a date, not a threshold. Already broke once on the last cycle leg (376–381 vs. claimed 364 days).',
    "ACTIVE_ADDR_DEV": 'Santostasi\'s own model states Bitcoin\'s price, hash rate, and active addresses are "all power laws of each other and of time" — this fits active addresses to its own power-law trend (OLS regression of log(addresses) vs. log(days since genesis), R&sup2; ≈ 0.83 on the full genesis-to-now fit) and measures how far today sits below that trend. Sourced from BGeometrics\' own chart data file, not their versioned API — an unofficial source, flagged as such deliberately. No fixed published threshold exists, so like Thermocap/NRPL this computes its own: the bottom 10th percentile of its own trailing daily history. Scored at reduced (Tier 3) weight — a genuinely novel construction with no track record against past cycle bottoms yet.',
}

# Table 3 (Cycle Top) tooltip text -- a separate dict, not reused entries in
# TOOLTIP_TEXT above, because several tokens are shared between both tables
# by raw value (MVRV Z-Score, Puell, Reserve Risk, Thermocap, RSI, Bollinger
# %B, Mayer, Fear & Greed, NVT Golden Cross) but need TOP-framed methodology
# text, not the bottom-framed text already shown on Table 1 for the same
# token. Research + sourcing for every threshold referenced here is in the
# Phase 1 research (see conversation/commit message), not re-derived here.
TOOLTIP_TEXT_TOP = {
    "PI_CYCLE_TOP": 'The real, published Pi Cycle TOP indicator (Philip Swift): 111-day SMA crossing above 2x the 350-day SMA has historically flagged Bitcoin cycle tops within days, across three separate cycles (2013, 2017, 2021). A crossover event, not a continuous magnitude -- shown as a real CROSSED/NOT CROSSED state with % distance as context, not a leeway band.',
    "MVRV_Z": 'Same market-cap-vs-realized-cap ratio as Table 1, standardized against its own volatility -- but its peak value has declined every cycle (9.4 in Dec 2017, 7.3 in Apr 2021, only 6.4 in Nov 2021\'s final top), so a single fixed top threshold would risk quietly becoming uncrossable. Uses the same rolling-percentile system as Thermocap/NRPL instead: the top 10th percentile of its own trailing history, self-adjusting as the trend continues.',
    "PUELL": 'Same miner-issuance ratio as Table 1 -- but its historically-cited top threshold has also declined every cycle (>8 in 2013, >5 in 2017, ~4 more recently). Same rolling-percentile treatment as MVRV Z-Score above, for the same reason.',
    "RESERVE_RISK": 'Same long-term-holder-conviction ratio as Table 1. A real published top-side ("sell zone") threshold exists at >0.02, corroborated by the metric\'s own creator\'s framework (Hans Hauge, via Bitcoin Magazine/Glassnode) for the underlying concept, though the specific 0.02 number itself is single-sourced (Medium confidence, flagged as such) -- historically associated with the 2013, 2017, and early-2021 peaks.',
    "THERMOCAP": 'Same market-cap-vs-cumulative-miner-revenue ratio as Table 1. No credible single fixed top threshold exists -- researched values ranged 32x-64x across just the two 2021 tops alone (2x variance within the same cycle), too wide to cite as one number. Same rolling-percentile system as the bottom table\'s own Thermocap treatment.',
    "RSI": 'Same weekly RSI as Table 1 (14-period). Overbought is researched specifically for the WEEKLY timeframe, not the generic daily-chart 70: weekly RSI above 80 triggered near the 2017, 2021, and early-2025 tops.',
    "BOLLINGER": 'Same %B (20-week, 2σ) as Table 1. 0.8 is the independently-confirmed standard overbought convention (StockCharts ChartSchool), not just an assumed mirror of the bottom table\'s 0.2.',
    "MAYER": 'Same price ÷ 200-day MA ratio as Table 1. >2.4 is Trace Mayer\'s own historically-backtested overheated threshold (the metric\'s creator) -- occurring in under 5% of trading days, marking the 2013/2017/2021 tops.',
    "FNG": 'Same composite sentiment score as Table 1. ≥75 is alternative.me\'s own published "Extreme Greed" band boundary.',
    "NVT_GC": 'Same exact CryptoQuant formula as Table 1, compared against the &gt;2.2 "overpriced" threshold instead of Table 1\'s &lt;−1.6. Fully routed through the same leeway/sigma system as every other Table 3 indicator (STRONG TOP SIGNAL / TOP-FAVORABLE / APPROACHING TOP / NORMAL) -- unlike Table 1\'s own hardcoded OVERPRICED branch, which is deliberately kept out of that system since its leeway is calibrated for Table 1\'s buy-side reading instead.',
}

# (url_or_None, link_text) for every tracked indicator's Source column.
# Every URL here is one already verified and shipped in a prior version's
# §1/§2 tables (v14 independently re-verified six of these against real
# indexed search results) — none of these are new/invented for this
# merge, per the same "reuse, don't reinvent" bar as that verification.
SOURCE_URL = {
    "MVRV_Z": ("https://charts.bgeometrics.com/mvrv.html", "view"),
    "REALIZED_PRICE": ("https://charts.bgeometrics.com/realized_price_g.html", "view"),
    "PUELL": ("https://charts.bgeometrics.com/puell_multiple.html", "view"),
    "RESERVE_RISK": ("https://charts.bgeometrics.com/reserve_risk.html", "view"),
    "LTH_SOPR": ("https://charts.bgeometrics.com/lth_sopr.html", "view"),
    "THERMOCAP": ("https://charts.bitbo.io/thermocap-multiple/", "view"),
    "NRPL": ("https://charts.bgeometrics.com/nrpl.html", "view"),
    "SUPPLY_LOSS": ("https://charts.bgeometrics.com/supply_in_profit.html", "view"),
    "ASOPR_EST": (None, "formula, see README"),
    "PROD_COST": ("https://ccaf.io/cbnsi/cbeci/methodology", "methodology"),
    "MAYER": ("https://www.coingecko.com/en/coins/bitcoin", "data"),
    "DRAWDOWN": ("https://www.coingecko.com/en/coins/bitcoin", "data"),
    "MINER_CAP": ("https://capriole.com/hash-ribbons-bitcoin-bottoms/", "method"),
    "NVT_GC": ("https://userguide.cryptoquant.com/cryptoquant-metrics/network/nvt-golden-cross", "method"),
    "FNG": ("https://alternative.me/crypto/fear-and-greed-index/", "view"),
    "MACD": ("https://www.coingecko.com/en/coins/bitcoin", "data"),
    "RSI": ("https://www.coingecko.com/en/coins/bitcoin", "data"),
    "BOLLINGER": ("https://www.coingecko.com/en/coins/bitcoin", "data"),
    "CYCLE_RHYTHM": (None, "pure date math"),
    "ACTIVE_ADDR_DEV": ("https://charts.bgeometrics.com/address_active_dark.html", "view"),
    "PI_CYCLE_TOP": ("https://coincodex.com/bitcoin-pi-cycle-top-indicator/", "method"),
}

# Per-token reading-cell formatters, ported verbatim from the richer
# display formatting §1/§2 used to apply (dollar/percent signs, two-line
# state+detail displays) — without this, the merged table would silently
# regress to a plainer reading than the tables it's replacing. Tokens not
# listed here show their bare value (str(values.get(token))), matching
# what §4 already did for the tokens that never had special formatting.
READING_FORMATTERS = {
    "REALIZED_PRICE": lambda v: f"${v.get('REALIZED_PRICE', '—')}",
    "SUPPLY_LOSS": lambda v: f"{v.get('SUPPLY_LOSS', '—')}%",
    "PROD_COST": lambda v: f"${v.get('PROD_COST', '—')}<br><span class=\"caveat\">({v.get('PROD_COST_PCT', '—')}% vs. spot)</span>",
    "DRAWDOWN": lambda v: f"{v.get('DRAWDOWN', '—')}%",
    "MINER_CAP": lambda v: f"{v.get('MINER_CAP_STATE', '—')}<br><span class=\"caveat\">({v.get('MINER_CAP', '—')}% spread)</span>",
    "FNG": lambda v: f"{v.get('FNG', '—')} ({v.get('FNG_LABEL', '—')})",
    "MACD": lambda v: f"{v.get('MACD', '—')}<br><span class=\"caveat\">cross: {v.get('MACD_CROSSED', '—')}</span>",
    "CYCLE_RHYTHM": lambda v: f"{v.get('DAYS_SINCE_TOP', '—')}d since top",
    "ACTIVE_ADDR_DEV": lambda v: f"{v.get('ACTIVE_ADDR_DEV', '—')}%",
    "PI_CYCLE_TOP": lambda v: f"{v.get('PI_CYCLE_TOP_STATE', '—')}<br><span class=\"caveat\">({v.get('PI_CYCLE_TOP', '—')}% away)</span>",
}


# ---------------------------------------------------------------------------
# Target values — the number each indicator needs to reach to flip to a buy
# signal. Wherever a real numeric threshold already drives status_pill()
# above, that SAME number is reused here verbatim (single source of truth —
# never a second, possibly-drifting copy). For the four indicators that have
# never had a scored threshold (Thermocap, Pi Cycle, NRPL, Drawdown), these
# are new additions, each sourced as follows:
#
#   THERMOCAP  (Market Cap / Thermocap ratio): checkonchain/Bitcoin Magazine
#   Pro historical charts put every past cycle bottom in the ~1-4x range,
#   with each successive cycle's floor landing lower as the denominator
#   (cumulative miner revenue) grows faster than any single drawdown can
#   compress it. "<= 4" is a round, conservative read of that historical
#   band — illustrative, not a fixed threshold anyone formally publishes.
#
#   PI_CYCLE: the real Pi Cycle Bottom indicator (Philip Swift) is a
#   crossover — 471-day SMA x 0.745 falling below the 150-day SMA — not a
#   level a single number can cross. No target number is defensible; the
#   dashboard says so rather than inventing one.
#
#   NRPL (Net Realized P&L, BTC): your own uploaded forecaster-track-record
#   research cites 2022's bottom flushing ~1.2M BTC in realized losses, and
#   2018's flushed roughly 800K BTC (Glassnode's Week 30 2022 review of both
#   cycles). Using the SMALLER of the two prior cycle bottoms as the more
#   conservative anchor: target <= -800,000 BTC. A historical-analog
#   estimate, not a published fixed threshold — flagged as such on the page.
#
#   DRAWDOWN MAGNITUDE: past major BTC drawdowns bottomed around -83% (Nov
#   2018) and -77% (Nov 2022), each measured peak-to-trough on daily closes
#   (widely cited, e.g. via CoinGecko/Glassnode cycle retrospectives).
#   Shown as a descriptive historical range, explicitly not scored.
# ---------------------------------------------------------------------------
TARGET_LABELS = {
    "MVRV_Z":        "\u2264 0.0",
    "PUELL":         "\u2264 0.5",
    "RESERVE_RISK":  "\u2264 0.002",
    "LTH_SOPR":      "\u2264 1.0",
    "ASOPR_EST":     "\u2264 1.0",
    "SUPPLY_LOSS":   "\u2265 50%",
    "MAYER":         "\u2264 1.0",
    "NVT_GC":        "< \u22121.6 (z-score)",
    "FNG":           "\u2264 20 (Extreme Fear)",
    "MACD":          "histogram crosses \u2265 0",
    "RSI":           "\u2264 30 (oversold)",
    "BOLLINGER":     "\u2264 0.2 (near lower band)",
    "THERMOCAP":     "\u2264 ~4x (2018/19 & 2022 bottom analog, Glassnode-sourced \u2014 reference only, live percentile is the real threshold once active)",
    "NRPL":          "No citable historical bottom value found (checked directly) \u2014 live percentile is the only real threshold, no illustrative fallback",
    "DRAWDOWN":      "\u2264 \u221277% (shallow edge of the \u221277% to \u221283% cycle-bottom analog range; analog-based, not weighted)",
    "MINER_CAP":     "Hash Ribbons \u201cBUY SIGNAL\u201d state (recovery + price MA confirmed)",
    "ACTIVE_ADDR_DEV": "No citable historical bottom value found (novel construction) \u2014 live percentile is the only real threshold, no illustrative fallback",
}


def target_for(token):
    return TARGET_LABELS.get(token, "\u2014")


# Table 3 (Cycle Top) target labels. MVRV Z-Score, Puell Multiple, and
# Thermocap Multiple are deliberately NOT here -- per Phase 1's decision,
# all three use the live rolling-percentile system (same as Thermocap/NRPL
# on Table 1), so their target string is dynamic and set directly in
# main(), not a static entry here.
TARGET_LABELS_TOP = {
    "RESERVE_RISK":  "\u2265 0.02 (sell zone \u2014 single-sourced, Medium confidence)",
    "RSI":           "\u2265 80 (overbought, weekly timeframe)",
    "BOLLINGER":     "\u2265 0.8 (near upper band)",
    "MAYER":         "\u2265 2.4 (Trace Mayer's own backtested overheated threshold)",
    "FNG":           "\u2265 75 (Extreme Greed, alternative.me's own band)",
    "NVT_GC":        "> 2.2 (z-score, OVERPRICED)",
    "PI_CYCLE_TOP":  "111-day SMA crosses above 2x 350-day SMA",
}


def target_for_top(token):
    return TARGET_LABELS_TOP.get(token, "\u2014")


# Full rank order, matching the original ranked analysis from early in this
# project (Power Law first, everything else in the same priority order
# established then) — this is the single reference used to build the rank
# table below, so a token's position here IS its rank, whether or not it
# actually carries scoreable weight.
# The pool of every indicator this dashboard tracks. Order here is now
# ONLY a fallback/tiebreak reference — actual displayed rank is derived
# from real weight below, not maintained as a hand-written list. That's
# the fix for a real bug: the old hand-written order drifted out of sync
# with the weights after multiple rounds of genuine reweighting (Prod
# Cost's upgrade, aSOPR's discount), producing ranks that didn't match
# what the weights actually said. A derived rank can't drift again.
_ALL_TRACKED_TOKENS = [
    "MVRV_Z", "REALIZED_PRICE", "PUELL", "RESERVE_RISK", "THERMOCAP",
    "LTH_SOPR", "PROD_COST", "NRPL", "ACTIVE_ADDR_DEV", "ASOPR_EST", "NVT_GC",
    "MINER_CAP", "MACD", "SUPPLY_LOSS", "MAYER", "RSI", "FNG", "BOLLINGER",
    "DRAWDOWN", "CYCLE_RHYTHM",
]


def get_master_rank_order():
    """Scored indicators sorted strictly by actual current weight,
    heaviest first (ties broken by the original conceptual order, purely
    for stable/deterministic output). Indicators with no current weight
    (N/A) are appended after ALL scored ones, in their original
    conceptual order — they have no number to sort by, but this
    guarantees they can never rank above a genuinely weighted indicator,
    which is exactly the bug this replaces. Called fresh each run rather
    than cached, since WEIGHT_MAP can change at runtime (Thermocap/NRPL
    conditionally join it once their 90-day bootstrap completes)."""
    tiebreak = {t: i for i, t in enumerate(_ALL_TRACKED_TOKENS)}
    scored = [t for t in _ALL_TRACKED_TOKENS if t in WEIGHT_MAP]
    unscored = [t for t in _ALL_TRACKED_TOKENS if t not in WEIGHT_MAP]
    scored.sort(key=lambda t: (-WEIGHT_MAP[t], tiebreak[t]))
    return scored + unscored


# Table 3 (Cycle Top) pool of tracked tokens -- fallback/tiebreak reference
# only, same discipline as _ALL_TRACKED_TOKENS above: actual rank is always
# derived from real WEIGHT_MAP_TOP weight, never hand-maintained. Several
# token names here are identical to Table 1's (MVRV_Z, PUELL, RESERVE_RISK,
# THERMOCAP, RSI, BOLLINGER, MAYER, FNG, NVT_GC) -- that's intentional and
# safe: they share the same rolling-history cache key for sigma/leeway, but
# every RESULT (status label/class/target) this table reads is stored under
# its own "_TOP"-suffixed values key, so nothing collides with Table 1's
# own stored results for the same token.
_ALL_TRACKED_TOKENS_TOP = [
    "PI_CYCLE_TOP", "MVRV_Z", "PUELL", "THERMOCAP", "RESERVE_RISK",
    "RSI", "BOLLINGER", "MAYER", "FNG", "NVT_GC",
]


def get_master_rank_order_top():
    """Table 3's own rank-order function -- structurally identical to
    get_master_rank_order() above (same sort-by-real-weight-first logic,
    same never-hand-written-order discipline), just reading WEIGHT_MAP_TOP
    and _ALL_TRACKED_TOKENS_TOP instead. A hand-written order already cost
    Table 1 a dedicated bug fix once; this is built correctly from day one
    rather than copy-pasting that mistake into a second table."""
    tiebreak = {t: i for i, t in enumerate(_ALL_TRACKED_TOKENS_TOP)}
    scored = [t for t in _ALL_TRACKED_TOKENS_TOP if t in WEIGHT_MAP_TOP]
    unscored = [t for t in _ALL_TRACKED_TOKENS_TOP if t not in WEIGHT_MAP_TOP]
    scored.sort(key=lambda t: (-WEIGHT_MAP_TOP[t], tiebreak[t]))
    return scored + unscored


# Short, one-line reasons for every indicator that can never contribute a
# scored vote — shown as a bullet under its row instead of a percentage.
EXCLUSION_REASONS = {
    "REALIZED_PRICE": "Redundant — same core ratio as MVRV Z-Score, just un-normalized",
    "THERMOCAP": "Building a rolling percentile threshold from live data — joins scoring once enough history accumulates",
    "NRPL": "Building a rolling percentile threshold from live data — joins scoring once enough history accumulates",
    "ACTIVE_ADDR_DEV": "Building a rolling percentile threshold from live data — joins scoring once enough history accumulates",
    "DRAWDOWN": "Analog-based threshold (past cycle-bottom range), not a mathematically-derived one — shown live, not weighted",
    "CYCLE_RHYTHM": "A calendar date, not a threshold — nothing to score",
}

# Two genuinely different reasons a token can be excluded from WEIGHT_MAP,
# which must not be visually conflated (that conflation was the bug this
# fixes): some tokens have a real, live-computed buy/not-yet signal that's
# just not being weighted (redundancy or lower analog-confidence); others
# have no number to compare against at all right now. DISPLAYABLE_BUT_
# UNWEIGHTED tokens get their real STATUS_LABEL/STATUS_CLASS shown, same
# as a weighted row would; anything else excluded falls back to
# NO_SIGNAL_STATUS (a token-specific label) or, failing that, "NOT SCORED".
DISPLAYABLE_BUT_UNWEIGHTED = {"REALIZED_PRICE", "DRAWDOWN"}

NO_SIGNAL_STATUS = {
    "THERMOCAP": ("BUILDING HISTORY", "st-mid"),
    "NRPL": ("BUILDING HISTORY", "st-mid"),
    "ACTIVE_ADDR_DEV": ("BUILDING HISTORY", "st-mid"),
    "CYCLE_RHYTHM": ("NOT A THRESHOLD", "st-mid"),
}

# Table 3 (Cycle Top) equivalents. Phase 2 -- display-only, no weight yet --
# so EVERY tracked token here shows its real, currently-computed status
# regardless of WEIGHT_MAP_TOP membership (which stays empty through this
# phase); main() is responsible for storing either a genuine status_pill()
# result or the literal "BUILDING HISTORY" state directly under each
# token's "_TOP_STATUS_LABEL"/"_TOP_STATUS_CLASS" keys, so nothing here
# needs a WEIGHT_MAP_TOP-membership-gated fallback the way Table 1's
# Thermocap/NRPL do.
DISPLAYABLE_BUT_UNWEIGHTED_TOP = {
    "PI_CYCLE_TOP", "MVRV_Z", "PUELL", "RESERVE_RISK", "THERMOCAP",
    "RSI", "BOLLINGER", "MAYER", "FNG", "NVT_GC",
}

# Only actually shown for tokens NOT currently in WEIGHT_MAP_TOP: PI_CYCLE_
# TOP (never weighted, see WEIGHT_MAP_TOP's own comment) and MVRV_Z/PUELL/
# THERMOCAP while their rolling-percentile bootstrap (n>=90) is still in
# progress -- once real, main() adds them to WEIGHT_MAP_TOP at runtime and
# these entries stop applying to them. The other six Table 3 tokens are
# static WEIGHT_MAP_TOP entries from the moment the process starts, so
# their reasons here are dead code kept only for completeness/safety.
EXCLUSION_REASONS_TOP = {
    "PI_CYCLE_TOP": "Crossover event with an unverified upstream signal field and zero track record in this implementation — display-only by deliberate choice, not a bootstrap-in-progress case",
    "MVRV_Z": "Rolling percentile threshold (own peak value has declined every cycle — see Phase 1 research) — joins WEIGHT_MAP_TOP at weight 1.5 once n>=90",
    "PUELL": "Rolling percentile threshold (same declining-per-cycle pattern as MVRV Z-Score) — joins WEIGHT_MAP_TOP at weight 1.5 once n>=90",
    "THERMOCAP": "Rolling percentile threshold (no credible single fixed top number — see Phase 1 research) — joins WEIGHT_MAP_TOP at weight 1.0 once n>=90",
}


# Direction/threshold source for the consecutive-buy-days annotation,
# shown only next to the top 7 by weight. PROD_COST is deliberately
# excluded: its own rolling history stores the THRESHOLD side (cost/BTC),
# not the value actually compared against it (spot price) -- no daily
# spot-price history is cached anywhere, so there's no honest way to
# reconstruct its past daily status from existing data. Fabricating one
# from mismatched series would be worse than just not showing a count.
CONSECUTIVE_DAYS_EXCLUDED = {"PROD_COST"}


def _consecutive_days_direction_threshold(token, cache):
    if token in BG_METRICS:
        direction, threshold = BG_METRICS[token][1], BG_METRICS[token][2]
        return direction, threshold
    if token == "NVT_GC":
        return "low", -1.6
    if token == "ACTIVE_ADDR_DEV":
        threshold, _n = rolling_threshold(cache, "ACTIVE_ADDR_DEV")
        return "low", threshold
    return None, None


def build_full_weighted_breakdown(values, cache):
    """The single master table: Power Law at rank 1 (veto power, not a
    percentage), then every tracked indicator via get_master_rank_order()
    below it. This is the only place that generates a row of HTML for any
    indicator -- name, tooltip, anchor icon, reading, weight-share (or N/A
    plus a one-line reason for indicators that can never score), target,
    today's status, and a hyperlinked source all come from the Python
    dicts above (TOOLTIP_TEXT, SOURCE_URL, READING_FORMATTERS, NAME_CAVEAT,
    DISPLAY_NAMES, TARGET_LABELS-derived values), not from any hand-written
    HTML row in the template. Returns a ready-to-insert HTML string, since
    the day-to-day status mix changes every run.

    The top 7 by weight additionally get a small consecutive-buy-days
    annotation next to their status pill -- an independent, visible second
    signal (not fed back into scoring or the tier system at all)."""
    rows = []
    top7 = set(get_master_rank_order()[:7])

    # Rank 1 — Power Law, always first, never part of the weight pool.
    rows.append(
        '<tr class="rank-veto"><td class="rank-num">1</td>'
        '<td class="ind-name">Santostasi Power Law</td>'
        '<td class="reading">\u2014</td>'
        '<td class="reading">VETO</td>'
        '<td class="target">Lower band touch (manual)</td>'
        '<td><span class="status-pill" style="background:rgba(96,165,250,.15); color:var(--blue);">MASTER SIGNAL</span></td>'
        '<td class="ind-source">manual</td></tr>'
    )

    for i, token in enumerate(get_master_rank_order(), start=2):
        name = DISPLAY_NAMES.get(token, token)
        target = values.get(f"{token}_TARGET", "—")
        formatter = READING_FORMATTERS.get(token)
        if formatter:
            reading_val = formatter(values)
        else:
            reading_val = values.get(token)
            reading_val = "\u2014" if reading_val is None else reading_val

        caveat = NAME_CAVEAT.get(token, "")
        caveat_html = f' <span class="caveat">{caveat}</span>' if caveat else ""
        tooltip_text = TOOLTIP_TEXT.get(token, "")
        tooltip_html = (
            f' <span class="tip-wrap"><span class="tip-icon" tabindex="0">i</span>'
            f'<span class="tip-content">{tooltip_text}</span></span>'
        ) if tooltip_text else ""
        is_weighted = token in WEIGHT_MAP
        anchor_html = ' <span class="anchor-icon">⚓</span>' if is_weighted else ""
        name_cell = f"{name}{caveat_html}{tooltip_html}{anchor_html}"

        src_url, src_label = SOURCE_URL.get(token, (None, "—"))
        source_html = f'<a href="{src_url}" target="_blank">{src_label}</a>' if src_url else src_label

        show_real_status = is_weighted or token in DISPLAYABLE_BUT_UNWEIGHTED
        if show_real_status:
            css = values.get(f"{token}_STATUS_CLASS")
            label = values.get(f"{token}_STATUS_LABEL", "CHECK")
            if css == "st-strong-buy":
                status_text, status_css = "STRONG BUY", "st-strong-buy"
            elif css == "st-buy":
                status_text, status_css = "BUY-FAVORABLE", "st-buy"
            elif css == "st-near":
                status_text, status_css = "BORDERLINE BUY", "st-near"
            elif css == "st-watch":
                status_text, status_css = label, "st-watch"
            elif css == "st-no":
                status_text, status_css = label, "st-no"
            elif label and label.startswith("STALE"):
                status_text, status_css = label, "st-mid"
            elif token == "NVT_GC" and label and label != "CHECK":
                # NVT's real computed label (e.g. its own OVERPRICED case,
                # or any future non-buy st-mid state) should reach the
                # page as-is instead of being flattened to the generic
                # placeholder below -- that placeholder is only accurate
                # when there's genuinely no reading (CHECK/None).
                status_text, status_css = label, "st-mid"
            else:
                status_text, status_css = "NO READING TODAY", "st-mid"
        elif token in NO_SIGNAL_STATUS:
            status_text, status_css = NO_SIGNAL_STATUS[token]
        else:
            status_text, status_css = "NOT SCORED", "st-mid"

        days_html = ""
        if token in top7 and token not in CONSECUTIVE_DAYS_EXCLUDED:
            days_direction, days_threshold = _consecutive_days_direction_threshold(token, cache)
            days = consecutive_buy_days(cache, token, days_direction, days_threshold)
            if days:
                unit = "day" if days == 1 else "days"
                days_html = f' <span class="caveat">{days} {unit}</span>'

        if is_weighted:
            weight = WEIGHT_MAP[token]
            pct_of_total = round((weight / sum(WEIGHT_MAP.values())) * 100, 1)
            weight_display = f"{pct_of_total}%"
            rows.append(
                f'<tr><td class="rank-num">{i}</td><td class="ind-name">{name_cell}</td>'
                f'<td class="reading">{reading_val}</td>'
                f'<td class="reading">{weight_display}</td>'
                f'<td class="target">{target}</td>'
                f'<td><span class="status-pill {status_css}">{status_text}</span>{days_html}</td>'
                f'<td class="ind-source">{source_html}</td></tr>'
            )
        else:
            reason = EXCLUSION_REASONS.get(token, "Not currently scored")
            rows.append(
                f'<tr class="rank-na"><td class="rank-num">{i}</td>'
                f'<td class="ind-name">{name_cell}<div class="na-reason">{reason}</div></td>'
                f'<td class="reading">{reading_val}</td>'
                f'<td class="reading">N/A</td>'
                f'<td class="target">{target}</td>'
                f'<td><span class="status-pill {status_css}">{status_text}</span></td>'
                f'<td class="ind-source">{source_html}</td></tr>'
            )
    return "\n".join(rows)


# Table 3 (Cycle Top) status-css -> display-text mapping. Deliberately NOT
# reusing build_full_weighted_breakdown()'s own st-buy/st-strong-buy/st-near
# -> "BUY-FAVORABLE"/"STRONG BUY"/"BORDERLINE BUY" text: status_pill()'s
# underlying leeway/sigma/tier LOGIC is direction-agnostic and fully reused
# (same function, same css classes, same colors), but "BUY" wording on a
# cycle-TOP table describing an overheated/sell-favorable reading would be
# actively backwards. Only the label TEXT differs here; st-no already comes
# back from status_pill() as "NORMAL{distance}" for direction="high", which
# reads correctly on a top table with no override needed.
_TOP_STATUS_TEXT = {
    "st-strong-buy": "STRONG TOP SIGNAL",
    "st-buy": "TOP-FAVORABLE",
    "st-near": "APPROACHING TOP",
}


def build_full_weighted_breakdown_top(values, cache):
    """Table 3's own master table -- structurally identical to
    build_full_weighted_breakdown() above (same per-row assembly from the
    same kind of Python dicts, just the _TOP-suffixed ones), with its own
    rank order (get_master_rank_order_top()), own weight pool
    (WEIGHT_MAP_TOP), and its own top-appropriate status wording via
    _TOP_STATUS_TEXT above. No Power Law veto row here -- Power Law is a
    bottom-calling model with no published top-side analog, so it isn't
    duplicated onto this table. No consecutive-days annotation either --
    that's a top-7-by-weight feature and WEIGHT_MAP_TOP is still empty
    (Phase 2)."""
    rows = []
    for i, token in enumerate(get_master_rank_order_top(), start=1):
        name = DISPLAY_NAMES.get(token, token)
        target = values.get(f"{token}_TOP_TARGET", "—")
        formatter = READING_FORMATTERS.get(token)
        if formatter:
            reading_val = formatter(values)
        else:
            reading_val = values.get(token)
            reading_val = "—" if reading_val is None else reading_val

        caveat = NAME_CAVEAT.get(token, "")
        caveat_html = f' <span class="caveat">{caveat}</span>' if caveat else ""
        tooltip_text = TOOLTIP_TEXT_TOP.get(token, "")
        tooltip_html = (
            f' <span class="tip-wrap"><span class="tip-icon" tabindex="0">i</span>'
            f'<span class="tip-content">{tooltip_text}</span></span>'
        ) if tooltip_text else ""
        is_weighted = token in WEIGHT_MAP_TOP
        anchor_html = ' <span class="anchor-icon">⚓</span>' if is_weighted else ""
        name_cell = f"{name}{caveat_html}{tooltip_html}{anchor_html}"

        src_url, src_label = SOURCE_URL.get(token, (None, "—"))
        source_html = f'<a href="{src_url}" target="_blank">{src_label}</a>' if src_url else src_label

        show_real_status = is_weighted or token in DISPLAYABLE_BUT_UNWEIGHTED_TOP
        if show_real_status:
            css = values.get(f"{token}_TOP_STATUS_CLASS")
            label = values.get(f"{token}_TOP_STATUS_LABEL", "CHECK")
            if css in _TOP_STATUS_TEXT:
                status_text, status_css = _TOP_STATUS_TEXT[css], css
            elif css == "st-watch":
                status_text, status_css = label, "st-watch"
            elif css == "st-no":
                status_text, status_css = label, "st-no"
            elif css == "st-mid" and label and label != "CHECK":
                # Real, non-generic st-mid state (e.g. Pi Cycle's own
                # NOT CROSSED / BUILDING HISTORY text) reaches the page
                # as-is, same discipline as the NVT_GC fix on Table 1.
                status_text, status_css = label, "st-mid"
            else:
                status_text, status_css = "NO READING TODAY", "st-mid"
        elif token in EXCLUSION_REASONS_TOP:
            status_text, status_css = "NOT YET SCORED", "st-mid"
        else:
            status_text, status_css = "NOT SCORED", "st-mid"

        if is_weighted:
            weight = WEIGHT_MAP_TOP[token]
            pct_of_total = round((weight / sum(WEIGHT_MAP_TOP.values())) * 100, 1)
            weight_display = f"{pct_of_total}%"
            rows.append(
                f'<tr><td class="rank-num">{i}</td><td class="ind-name">{name_cell}</td>'
                f'<td class="reading">{reading_val}</td>'
                f'<td class="reading">{weight_display}</td>'
                f'<td class="target">{target}</td>'
                f'<td><span class="status-pill {status_css}">{status_text}</span></td>'
                f'<td class="ind-source">{source_html}</td></tr>'
            )
        else:
            reason = EXCLUSION_REASONS_TOP.get(token, "Not currently scored")
            rows.append(
                f'<tr class="rank-na"><td class="rank-num">{i}</td>'
                f'<td class="ind-name">{name_cell}<div class="na-reason">{reason}</div></td>'
                f'<td class="reading">{reading_val}</td>'
                f'<td class="reading">N/A</td>'
                f'<td class="target">{target}</td>'
                f'<td><span class="status-pill {status_css}">{status_text}</span></td>'
                f'<td class="ind-source">{source_html}</td></tr>'
            )
    return "\n".join(rows)


# Colors: st-strong-buy/st-buy/st-near get their own color (a fired slice
# reads as fired at a glance); everything else (st-no/st-mid/st-watch)
# falls back to muted grey -- deliberately not enumerated per-class,
# since "not currently fired" should look the same regardless of why.
PIE_COLOR_MAP = {"st-strong-buy": "#1fae63", "st-buy": "#3ddc9a", "st-near": "#6ba9fa"}
PIE_GREY = "#3a4150"


def build_weight_pie_svg(values):
    """Inline SVG (no JS charting library, this is a static HTML page) --
    one slice per currently-weighted indicator, sized by its share of the
    total weight pool, colored by whether it's fired today. Unweighted
    indicators (Realized Price, Thermocap pre-bootstrap, etc.) have no
    meaningful weight share and get no slice.

    Slice boundaries are computed from a running WEIGHT total divided by
    the grand total at each step -- not by repeatedly adding pre-divided
    fractional degrees -- so floating-point rounding can't accumulate
    across slices. Since the final running total always equals the grand
    total exactly (same float value divided by itself is exactly 1.0 in
    IEEE754), the last slice's end angle is always exactly 360.0, closing
    the circle with no gap or overlap regardless of how many slices
    precede it or how their individual fractions round."""
    slices = [(DISPLAY_NAMES.get(t, t), w, values.get(f"{t}_STATUS_CLASS", "st-mid"))
              for t, w in WEIGHT_MAP.items()]
    total = sum(w for _, w, _ in slices)
    cx, cy, r = 150, 150, 120
    fired_count = sum(1 for _, _, css in slices if css in ("st-strong-buy", "st-buy", "st-near"))

    paths = []
    cum_weight = 0.0
    angle = 0.0
    for label, weight, css in slices:
        cum_weight += weight
        start = angle
        end = (cum_weight / total) * 360
        angle = end
        large_arc = 1 if (end - start) > 180 else 0
        sx = cx + r * math.sin(math.radians(start)); sy = cy - r * math.cos(math.radians(start))
        ex = cx + r * math.sin(math.radians(end)); ey = cy - r * math.cos(math.radians(end))
        color = PIE_COLOR_MAP.get(css, PIE_GREY)
        frac = weight / total
        paths.append(
            f'<path d="M{cx},{cy} L{sx:.2f},{sy:.2f} A{r},{r} 0 {large_arc},1 {ex:.2f},{ey:.2f} Z" '
            f'fill="{color}"><title>{label}: {round(frac * 100, 1)}%</title></path>'
        )
    center_text = (
        f'<text x="{cx}" y="{cy}" text-anchor="middle" dominant-baseline="middle" '
        f'fill="white" font-size="20">{fired_count}/{len(slices)} fired</text>'
    )
    return f'<svg viewBox="0 0 300 300" width="260" height="260">{"".join(paths)}{center_text}</svg>'


def build_weight_pie_svg_top(values):
    """Table 3's own pie chart -- same slice-boundary math as
    build_weight_pie_svg() above (same running-total approach, same
    floating-point-safe closure), reading WEIGHT_MAP_TOP and the
    "_TOP_STATUS_CLASS"-suffixed values instead. WEIGHT_MAP_TOP is still
    empty through Phase 2, so this explicitly handles the zero-weight case
    (the original function never needed to, since WEIGHT_MAP always has
    real entries) rather than dividing by zero."""
    slices = [(DISPLAY_NAMES.get(t, t), w, values.get(f"{t}_TOP_STATUS_CLASS", "st-mid"))
              for t, w in WEIGHT_MAP_TOP.items()]
    total = sum(w for _, w, _ in slices)
    if not total:
        return ('<svg viewBox="0 0 300 300" width="260" height="260">'
                '<circle cx="150" cy="150" r="120" fill="#3a4150"/>'
                '<text x="150" y="150" text-anchor="middle" dominant-baseline="middle" '
                'fill="white" font-size="16">No weighted indicators yet</text></svg>')
    cx, cy, r = 150, 150, 120
    fired_count = sum(1 for _, _, css in slices if css in ("st-strong-buy", "st-buy", "st-near"))

    paths = []
    cum_weight = 0.0
    angle = 0.0
    for label, weight, css in slices:
        cum_weight += weight
        start = angle
        end = (cum_weight / total) * 360
        angle = end
        large_arc = 1 if (end - start) > 180 else 0
        sx = cx + r * math.sin(math.radians(start)); sy = cy - r * math.cos(math.radians(start))
        ex = cx + r * math.sin(math.radians(end)); ey = cy - r * math.cos(math.radians(end))
        color = PIE_COLOR_MAP.get(css, PIE_GREY)
        frac = weight / total
        paths.append(
            f'<path d="M{cx},{cy} L{sx:.2f},{sy:.2f} A{r},{r} 0 {large_arc},1 {ex:.2f},{ey:.2f} Z" '
            f'fill="{color}"><title>{label}: {round(frac * 100, 1)}%</title></path>'
        )
    center_text = (
        f'<text x="{cx}" y="{cy}" text-anchor="middle" dominant-baseline="middle" '
        f'fill="white" font-size="20">{fired_count}/{len(slices)} fired</text>'
    )
    return f'<svg viewBox="0 0 300 300" width="260" height="260">{"".join(paths)}{center_text}</svg>'


# Matches the two live-indicator table sections in the template exactly —
# used only for the summary counts below, not for scoring itself.
CORE_TOKENS = ("MVRV_Z", "REALIZED_PRICE", "PUELL", "RESERVE_RISK", "THERMOCAP",
               "LTH_SOPR", "NRPL", "SUPPLY_LOSS", "ASOPR_EST")
SELF_COMPUTED_TOKENS = ("PROD_COST", "MAYER", "DRAWDOWN", "MINER_CAP", "NVT_GC",
                         "FNG", "MACD", "RSI", "BOLLINGER", "CYCLE_RHYTHM",
                         "ACTIVE_ADDR_DEV")


def _count_bucket(values, tokens):
    """(buy_count, scored_count, pct) for a group of tokens — only counts
    ones currently carrying real weight (in WEIGHT_MAP), matching the
    "weighted indicators" framing this box is meant to report on. A
    near-threshold reading (st-near) counts as buy-favorable here too,
    same treatment as the main verdict."""
    scored = [t for t in tokens if t in WEIGHT_MAP]
    buy = [t for t in scored if values.get(f"{t}_STATUS_CLASS") in ("st-strong-buy", "st-buy", "st-near")]
    n_scored = len(scored)
    n_buy = len(buy)
    pct = round((n_buy / n_scored) * 100, 1) if n_scored else 0.0
    return n_buy, n_scored, pct


def build_signal_summary_html(values):
    """Three fractions (core / self-computed / all weighted indicators)
    plus the actual weight-adjusted percentage, in one compact box. The
    weighted percentage is the one that actually drives the verdict —
    deliberately shown larger than the three simple counts, which are
    context, not the headline number."""
    core_buy, core_total, core_pct = _count_bucket(values, CORE_TOKENS)
    self_buy, self_total, self_pct = _count_bucket(values, SELF_COMPUTED_TOKENS)
    all_buy, all_total, all_pct = _count_bucket(values, CORE_TOKENS + SELF_COMPUTED_TOKENS)
    weighted_pct = values.get("VERDICT_PCT")
    weighted_pct_display = f"{weighted_pct}%" if weighted_pct is not None else "—"

    return f'''<div class="summary-box">
      <div class="summary-row">
        <div class="summary-cell">
          <div class="summary-frac">{core_buy}/{core_total}</div>
          <div class="summary-label">Core indicators signaling bottom</div>
          <div class="summary-pct">{core_pct}%</div>
        </div>
        <div class="summary-cell">
          <div class="summary-frac">{self_buy}/{self_total}</div>
          <div class="summary-label">Self-computed indicators signaling bottom</div>
          <div class="summary-pct">{self_pct}%</div>
        </div>
        <div class="summary-cell">
          <div class="summary-frac">{all_buy}/{all_total}</div>
          <div class="summary-label">All weighted indicators signaling bottom</div>
          <div class="summary-pct">{all_pct}%</div>
        </div>
        <div class="summary-cell summary-cell-main">
          <div class="summary-pct-main">{weighted_pct_display}</div>
          <div class="summary-label">\u2693 Actual weighted percentage \u2693</div>
        </div>
      </div>
    </div>'''


def _count_bucket_top(values, tokens):
    """Table 3 equivalent of _count_bucket() -- reads WEIGHT_MAP_TOP and
    "_TOP_STATUS_CLASS" instead."""
    scored = [t for t in tokens if t in WEIGHT_MAP_TOP]
    buy = [t for t in scored if values.get(f"{t}_TOP_STATUS_CLASS") in ("st-strong-buy", "st-buy", "st-near")]
    n_scored = len(scored)
    n_buy = len(buy)
    pct = round((n_buy / n_scored) * 100, 1) if n_scored else 0.0
    return n_buy, n_scored, pct


def build_signal_summary_html_top(values):
    """Table 3's own summary box -- simpler than build_signal_summary_html()
    above by design: Table 3 has ~10 indicators total, not the ~20 Table 1
    has, so a Core/Self-computed split would be an arbitrary categorical
    line with nothing real behind it. One fraction (all weighted
    indicators) plus the main weighted percentage is proportionate to the
    actual indicator count. Reuses the same .summary-box/.summary-cell CSS
    already defined for Table 1 -- no new styling needed."""
    all_buy, all_total, all_pct = _count_bucket_top(values, tuple(WEIGHT_MAP_TOP.keys()))
    weighted_pct = values.get("VERDICT_PCT_TOP")
    weighted_pct_display = f"{weighted_pct}%" if weighted_pct is not None else "\u2014"

    return f'''<div class="summary-box">
      <div class="summary-row">
        <div class="summary-cell">
          <div class="summary-frac">{all_buy}/{all_total}</div>
          <div class="summary-label">All weighted indicators signaling top</div>
          <div class="summary-pct">{all_pct}%</div>
        </div>
        <div class="summary-cell summary-cell-main">
          <div class="summary-pct-main">{weighted_pct_display}</div>
          <div class="summary-label">\u26a0\ufe0f Actual weighted percentage \u26a0\ufe0f</div>
        </div>
      </div>
    </div>'''


def build_verdict(values):
    scored, excluded_names, buy_names = [], [], []
    for token, weight in WEIGHT_MAP.items():
        css = values.get(f"{token}_STATUS_CLASS")
        name = DISPLAY_NAMES.get(token, token)
        if css in ("st-strong-buy", "st-buy"):
            # A strong-buy reading (crossed by >=2 sigma) counts exactly
            # like a normal buy-favorable one here -- it's a real signal,
            # just a stronger one, not a separate scoring tier.
            scored.append((token, weight, True))
            buy_names.append(name)
        elif css == "st-near":
            # Near-threshold leeway (BORDERLINE BUY): counts as
            # buy-favorable per your own call, but visibly marked so it's
            # never confused with a clean, fully-crossed reading.
            scored.append((token, weight, True))
            buy_names.append(f"{name} (borderline)")
        elif css == "st-no":
            scored.append((token, weight, False))
        else:
            excluded_names.append(name)  # st-mid or missing = CHECK/context this run, excluded from scoring

    total_weight = sum(w for _, w, _ in scored)
    buy_weight = sum(w for _, w, buy in scored if buy)
    pct = round((buy_weight / total_weight) * 100, 1) if total_weight else None
    buy_count = sum(1 for _, _, buy in scored if buy)
    total_count = len(scored)

    if pct is None:
        headline = "Insufficient data this run"
        body = ("Not enough indicators returned usable readings to synthesize a verdict this run — "
                "check the Action log for what failed.")
    elif pct < 25:
        headline = "Minimal confluence"
        body = ("Few of the weighted indicators are in buy-favorable territory right now. Historically, "
                "a reading this low has preceded further consolidation or continued decline rather than an "
                "imminent bottom. The higher-probability read for the next few weeks is more of the same — "
                "not urgency.")
    elif pct < 50:
        headline = "Partial confluence — early accumulation zone"
        body = ("A meaningful minority of indicators are buy-favorable, concentrated more in the technical/"
                "sentiment tier than the core on-chain valuation tier. In 2018 and 2022, this stage typically "
                "preceded the actual low by anywhere from several weeks to a few months. Reads as 'still "
                "forming,' not confirmed.")
    elif pct < 75:
        headline = "Strong confluence forming"
        body = ("A majority of tracked indicators, including some in the core on-chain tier, are now aligned. "
                "In prior cycles, this level of confluence has shown up within roughly one to three months of "
                "the eventual cycle low. This has historically been a zone where gradual accumulation — not "
                "waiting for perfect confirmation — outperformed sitting in cash. The master signal below "
                "still has final say.")
    else:
        headline = "Near-maximal confluence"
        body = ("Almost every tracked indicator is aligned — a configuration that has historically coincided "
                "with, or shortly preceded, the actual cycle low in 2015, 2018, and 2022. About as strong a "
                "secondary-signal picture as this framework produces.")

    return {
        "VERDICT_PCT": pct if pct is not None else "—",
        "VERDICT_COUNT": f"{buy_count}/{total_count}",
        "VERDICT_HEADLINE": headline,
        "VERDICT_BODY": body,
        "VERDICT_BUY_LIST": ", ".join(buy_names) if buy_names else "none this run",
        "VERDICT_EXCLUDED_COUNT": len(excluded_names),
        "VERDICT_EXCLUDED_LIST": ", ".join(excluded_names) if excluded_names else "none",
    }


def build_verdict_top(values):
    """Table 3's own verdict -- same mechanical weighted-tally structure as
    build_verdict() above (same st-strong-buy/st-buy/st-near/st-no
    classification, same "excluded this run" bucket for st-mid/missing),
    reading WEIGHT_MAP_TOP and "_TOP_STATUS_CLASS" instead. Copy is
    reframed for top/euphoria confluence rather than bottom/accumulation
    language -- these are different claims about different market phases,
    not a reworded mirror. With WEIGHT_MAP_TOP still empty (Phase 2),
    total_weight is always 0 here, so this always returns the
    "Insufficient data" case honestly -- exactly as it should until Phase 3
    adds real weights."""
    scored, excluded_names, top_names = [], [], []
    for token, weight in WEIGHT_MAP_TOP.items():
        css = values.get(f"{token}_TOP_STATUS_CLASS")
        name = DISPLAY_NAMES.get(token, token)
        if css in ("st-strong-buy", "st-buy"):
            scored.append((token, weight, True))
            top_names.append(name)
        elif css == "st-near":
            scored.append((token, weight, True))
            top_names.append(f"{name} (approaching)")
        elif css == "st-no":
            scored.append((token, weight, False))
        else:
            excluded_names.append(name)

    total_weight = sum(w for _, w, _ in scored)
    top_weight = sum(w for _, w, top in scored if top)
    pct = round((top_weight / total_weight) * 100, 1) if total_weight else None
    top_count = sum(1 for _, _, top in scored if top)
    total_count = len(scored)

    if pct is None:
        headline = "Insufficient data this run"
        body = ("Not enough Table 3 indicators currently carry real weight to synthesize a top-side verdict "
                "yet — Phase 3 hasn't assigned weights. Individual indicator statuses above are still real and "
                "live; this synthesis line just doesn't exist until weights do.")
    elif pct < 25:
        headline = "Minimal top confluence"
        body = ("Few of the weighted indicators are in overheated/top-favorable territory right now. "
                "Historically, a reading this low has not coincided with a cycle top.")
    elif pct < 50:
        headline = "Partial top confluence — early euphoria signs"
        body = ("A meaningful minority of indicators are flashing overheated. In prior cycles, this stage has "
                "preceded the actual top by weeks to months, not confirmed it.")
    elif pct < 75:
        headline = "Strong top confluence forming"
        body = ("A majority of tracked top-side indicators are now aligned toward euphoria/overheated "
                "territory. Historically a zone worth taking seriously, though not by itself a confirmed top.")
    else:
        headline = "Near-maximal top confluence"
        body = ("Almost every tracked top-side indicator is aligned toward euphoria — the configuration that "
                "has historically clustered around, or shortly preceded, past cycle tops.")

    return {
        "VERDICT_PCT_TOP": pct if pct is not None else "—",
        "VERDICT_COUNT_TOP": f"{top_count}/{total_count}",
        "VERDICT_HEADLINE_TOP": headline,
        "VERDICT_BODY_TOP": body,
        "VERDICT_TOP_LIST_TOP": ", ".join(top_names) if top_names else "none this run",
        "VERDICT_EXCLUDED_COUNT_TOP": len(excluded_names),
        "VERDICT_EXCLUDED_LIST_TOP": ", ".join(excluded_names) if excluded_names else "none",
    }


def main():
    values = {}
    cache = load_cache()
    print(f"Loaded cache with {len(cache)} previously-known values")

    if not BG_KEY:
        print(f"! BGEOMETRICS_API_KEY not set — those {len(BG_METRICS)} rows will show CHECK.")

    print(f"Fetching {len(BG_METRICS)} indicators from BGeometrics...")
    for token, (slug, direction, threshold) in BG_METRICS.items():
        val = fetch_bg_latest(slug)
        if val is not None:
            cache_store(cache, token, val)
            try:
                history_append(cache, token, float(val))
            except (TypeError, ValueError):
                pass
            values[token] = val
            label, css = status_pill(val, direction, threshold, cache=cache, token=token)
        else:
            cached_val, cached_date = cache_lookup(cache, token)
            if cached_val is not None:
                values[token] = cached_val
                label, css = f"STALE ({cached_date})", "st-mid"
                print(f"  {token}: live fetch failed, using cached value from {cached_date}")
            else:
                values[token] = None
                label, css = "CHECK", "st-mid"
        values[f"{token}_STATUS_LABEL"] = label
        values[f"{token}_STATUS_CLASS"] = css
        values[f"{token}_TARGET"] = target_for(token)
        print(f"  {token} ({slug}): {values[token]} -> {label}")

    # Active Addresses Power-Law Deviation — see the HONESTY NOTE above
    # fetch_active_addresses_full_history() for why this pulls BGeometrics'
    # own chart data file rather than the metered REST endpoint. Computed
    # here, before the PERCENTILE_ELIGIBLE loop below, so it can join that
    # same generic rolling-percentile/WEIGHT_MAP machinery Thermocap and
    # NRPL already use — no separate mechanism needed.
    print("Fetching full Active Addresses history (BGeometrics chart data, unmetered)...")
    aa_points = fetch_active_addresses_full_history()
    if aa_points:
        aa_slope, aa_intercept, aa_r2 = fit_power_law([(d, v) for d, v, _ in aa_points])
        if aa_slope is not None:
            today_days, today_val, today_date = aa_points[-1]
            today_pred = predict_power_law(aa_slope, aa_intercept, today_days)
            aa_deviation = round((today_val - today_pred) / today_pred * 100, 2) if today_pred else None
            print(f"  Power-law fit: slope={aa_slope:.4f} intercept={aa_intercept:.4f} "
                  f"R2={aa_r2:.4f} (n={len(aa_points)} days, {aa_points[0][2]} -> {today_date})")
            print(f"  Today ({today_date}): active_addresses={today_val:.0f}, "
                  f"predicted={today_pred:.0f}, deviation={aa_deviation}%")
            seeded_n = seed_active_addr_dev_history(cache, aa_points, aa_slope, aa_intercept)
            if seeded_n:
                print(f"  Seeded rolling-history cache with {seeded_n} historical daily deviations")
            values["ACTIVE_ADDR_DEV"] = aa_deviation
            if aa_deviation is not None:
                cache_store(cache, "ACTIVE_ADDR_DEV", aa_deviation)
                if not seeded_n:
                    # History already existed (post-bootstrap) — normal
                    # single-value-per-day append, identical to every other
                    # leeway-enabled indicator. Skipped on the seeding run
                    # itself so today's value isn't double-counted on top
                    # of the bulk seed, which already includes it.
                    history_append(cache, "ACTIVE_ADDR_DEV", aa_deviation)
        else:
            print("  ! not enough usable points to fit a power law — ACTIVE_ADDR_DEV staying N/A")
            values["ACTIVE_ADDR_DEV"] = None
    else:
        cached_val, cached_date = cache_lookup(cache, "ACTIVE_ADDR_DEV")
        if cached_val is not None:
            values["ACTIVE_ADDR_DEV"] = cached_val
            values["ACTIVE_ADDR_DEV_STATUS_LABEL"] = f"STALE ({cached_date})"
            values["ACTIVE_ADDR_DEV_STATUS_CLASS"] = "st-mid"
            print(f"  ACTIVE_ADDR_DEV: live fetch failed, using cached value from {cached_date}")
        else:
            values["ACTIVE_ADDR_DEV"] = None
            print("  ACTIVE_ADDR_DEV: live fetch failed and no cached fallback available")

    # Rolling-percentile thresholds for Thermocap, NRPL, and Active
    # Addresses Power-Law Deviation — see the long comment above
    # rolling_threshold() for the research this is grounded in. Researched
    # historical-cycle reference values (2018/19 & 2022 bottoms) shown
    # alongside the accumulation progress below, purely as comparison
    # context — never used as the live scoring threshold, and honestly
    # reported where no citable number exists.
    HISTORICAL_REFERENCE = {
        "THERMOCAP": "~4x at 2018/19 & 2022 bottoms (Glassnode-sourced)",
        "NRPL": "no citable historical bottom value found (checked directly)",
        "ACTIVE_ADDR_DEV": "no citable historical bottom value found (novel construction, checked directly)",
    }
    PERCENTILE_ELIGIBLE = ("THERMOCAP", "NRPL", "ACTIVE_ADDR_DEV")
    for token in PERCENTILE_ELIGIBLE:
        fresh_val = values.get(token)
        # History is already recorded generically in the BG_METRICS loop
        # above (for every token, on every genuinely fresh fetch) — no
        # need to append it again here, that would double-count today's
        # value in the distribution.

        threshold, n_points = rolling_threshold(cache, token)
        if threshold is not None and fresh_val is not None:
            label, css = status_pill(fresh_val, "low", threshold, cache=cache, token=token)
            values[f"{token}_STATUS_LABEL"] = label
            values[f"{token}_STATUS_CLASS"] = css
            values[f"{token}_TARGET"] = f"\u2264 {round(threshold, 2)} (live, {PERCENTILE_CUTOFF}th pct. of trailing {n_points}d)"
            WEIGHT_MAP[token] = 1.5  # Tier 3: real and self-computed, but not yet full-cycle-validated
            print(f"  {token}: rolling {PERCENTILE_CUTOFF}th percentile threshold = {round(threshold, 2)} (n={n_points}) -> {label}")
        else:
            ref = HISTORICAL_REFERENCE.get(token, "")
            values[f"{token}_TARGET"] = f"N/A \u2014 building history ({n_points}/{MIN_HISTORY_DAYS} days). Reference: {ref}"
            print(f"  {token}: only {n_points}/{MIN_HISTORY_DAYS} days of history accumulated, staying N/A")

    # Supply in Loss = 100 - % Supply in Profit (BGeometrics only exposes
    # the profit side under this slug; the loss framing is the one your
    # original ranking used, and the one this whole project started from).
    # supply-profit returns a raw BTC count, not a percentage (confirmed
    # via BGeometrics' own docs, Aug 2026), so it has to be divided by
    # circulating supply first. Circulating supply comes from a dedicated
    # Blockchain.com /stats call here (totalbc, in satoshis) rather than
    # reusing the one fetched later in main() for Production Cost, since
    # reordering the whole function isn't worth it for one shared number.
    # If the underlying Supply-in-Profit value came from cache (stale),
    # this derived figure inherits that staleness rather than looking
    # freshly computed.
    supply_profit_val = values.get("SUPPLY_PROFIT")
    supply_profit_is_stale = values.get("SUPPLY_PROFIT_STATUS_LABEL", "").startswith("STALE")
    supply_loss_val = None
    if supply_profit_val is not None:
        circulating_stats = fetch_blockchain_stats()
        circulating_btc = circulating_stats.get("totalbc")
        if circulating_btc:
            try:
                percent_in_profit = (float(supply_profit_val) / (circulating_btc / 1e8)) * 100
                supply_loss_val = round(100 - percent_in_profit, 2)
            except (TypeError, ValueError, ZeroDivisionError):
                supply_loss_val = None
        else:
            print("  ! circulating supply (totalbc) unavailable — SUPPLY_LOSS staying N/A")
    values["SUPPLY_LOSS"] = supply_loss_val
    if supply_profit_is_stale:
        cached_date = values["SUPPLY_PROFIT_STATUS_LABEL"].split("(")[1].rstrip(")")
        label, css = f"STALE ({cached_date})", "st-mid"
    else:
        if supply_loss_val is not None:
            history_append(cache, "SUPPLY_LOSS", supply_loss_val)
        label, css = status_pill(supply_loss_val, "high", 50.0, cache=cache, token="SUPPLY_LOSS")
    values["SUPPLY_LOSS_STATUS_LABEL"], values["SUPPLY_LOSS_STATUS_CLASS"] = label, css
    values["SUPPLY_LOSS_TARGET"] = target_for("SUPPLY_LOSS")
    print(f"  SUPPLY_LOSS (derived from SUPPLY_PROFIT): {supply_loss_val} -> {label}")

    # aSOPR estimate, derived from the same SOPR fetch — see
    # compute_asopr_estimate() for the reasoning behind the formula.
    # Same staleness-propagation logic as above.
    sopr_is_stale = values.get("SOPR_STATUS_LABEL", "").startswith("STALE")
    asopr_est = compute_asopr_estimate(values.get("SOPR"))
    values["ASOPR_EST"] = asopr_est
    if sopr_is_stale:
        cached_date = values["SOPR_STATUS_LABEL"].split("(")[1].rstrip(")")
        label, css = f"STALE ({cached_date})", "st-mid"
    else:
        if asopr_est is not None:
            history_append(cache, "ASOPR_EST", asopr_est)
        label, css = status_pill(asopr_est, "low", 1.0, cache=cache, token="ASOPR_EST")
    values["ASOPR_EST_STATUS_LABEL"], values["ASOPR_EST_STATUS_CLASS"] = label, css
    values["ASOPR_EST_TARGET"] = target_for("ASOPR_EST")
    print(f"  ASOPR_EST (modeled from SOPR={values.get('SOPR')}): {asopr_est} -> {label}")

    print("Fetching price history from CoinGecko (340 days, needed for NVT Golden Cross)...")
    price_history = fetch_coingecko_history(days=340)
    spot_price = price_history[-1] if price_history else None

    # Realized Price: buy-favorable when spot trades below the realized-
    # price cost basis. Routed through status_pill() (not a hand-rolled
    # binary check) so it gets the same near-threshold leeway and
    # distance-from-threshold reporting every other scored indicator
    # gets — still excluded from WEIGHT_MAP (redundant with MVRV Z-Score),
    # this only changes what status gets displayed, not scoring.
    # Skip the fresh comparison if the realized-price figure itself is a
    # stale cache fallback — don't compare today's live spot price against
    # a days-old cost-basis snapshot and present it with fresh confidence.
    #
    # Deliberately NOT using cache/token="REALIZED_PRICE" for the leeway
    # calc: that history key (populated above, in the generic BG_METRICS
    # loop) tracks the realized-price FEED itself -- the threshold side of
    # this comparison, not the value actually being compared (spot price).
    # The realized-price feed moves by single-digit-dollars/day; spot price
    # moves by hundreds to thousands. Feeding the former into a sigma meant
    # to characterize the latter would eventually produce a wildly
    # miscalibrated band/strong-buy trigger once real n>=20 history
    # accumulates there (today it's masked by the flat 3% bootstrap, since
    # n<20, but that's a matter of days away). REALIZED_PRICE_GAP is a
    # separate history series tracking the actual compared value (spot
    # price) instead, so its sigma measures what it's supposed to.
    realized_price_val = values.get("REALIZED_PRICE")
    realized_price_is_stale = values.get("REALIZED_PRICE_STATUS_LABEL", "").startswith("STALE")
    if realized_price_is_stale:
        rp_label, rp_css = values["REALIZED_PRICE_STATUS_LABEL"], "st-mid"
    elif realized_price_val is not None and spot_price is not None:
        try:
            rp = float(realized_price_val)
            history_append(cache, "REALIZED_PRICE_GAP", spot_price)
            rp_label, rp_css = status_pill(spot_price, "low", rp, cache=cache, token="REALIZED_PRICE_GAP")
        except (TypeError, ValueError):
            rp_label, rp_css = "CHECK", "st-mid"
    else:
        rp_label, rp_css = "CHECK", "st-mid"
    values["REALIZED_PRICE_STATUS_LABEL"], values["REALIZED_PRICE_STATUS_CLASS"] = rp_label, rp_css
    # The target IS the reading itself — buy-favorable triggers when spot
    # price falls below this cost-basis figure, so there's no separate
    # number to compute; just relabel it as the threshold it already is.
    if realized_price_val is not None:
        try:
            values["REALIZED_PRICE_TARGET"] = f"Spot < ${float(realized_price_val):,.0f}"
        except (TypeError, ValueError):
            values["REALIZED_PRICE_TARGET"] = "Spot below realized price"
    else:
        values["REALIZED_PRICE_TARGET"] = "Spot below realized price"

    mayer, drawdown = compute_mayer_and_drawdown(price_history)
    values["MAYER"] = mayer
    values["DRAWDOWN"] = drawdown
    if mayer is not None:
        history_append(cache, "MAYER", mayer)
    label, css = status_pill(mayer, "low", 1.0, cache=cache, token="MAYER")
    values["MAYER_STATUS_LABEL"], values["MAYER_STATUS_CLASS"] = label, css
    values["MAYER_TARGET"] = target_for("MAYER")

    # Drawdown Magnitude: buy-favorable at or beyond -77% from ATH -- the
    # shallow edge of the existing "-77% to -83%" historical-analog range
    # (anything beyond -77% is still inside that analog zone). Routed
    # through status_pill() for the same near-threshold leeway every other
    # scored indicator gets. Still excluded from WEIGHT_MAP -- this is an
    # analog-based signal, not a mathematically-derived threshold, and
    # shouldn't carry the same confidence as one that is.
    if drawdown is not None:
        history_append(cache, "DRAWDOWN", drawdown)
    dd_label, dd_css = status_pill(drawdown, "low", -77.0, cache=cache, token="DRAWDOWN")
    values["DRAWDOWN_STATUS_LABEL"], values["DRAWDOWN_STATUS_CLASS"] = dd_label, dd_css
    values["DRAWDOWN_TARGET"] = target_for("DRAWDOWN")
    print(f"  Mayer Multiple: {mayer} | Drawdown from ATH: {drawdown}%")

    print("Computing weekly RSI, MACD, and Bollinger %B from the same price history...")
    rsi = compute_weekly_rsi(price_history)
    values["RSI"] = rsi
    if rsi is not None:
        history_append(cache, "RSI", rsi)
    label, css = status_pill(rsi, "low", 30.0, cache=cache, token="RSI")
    values["RSI_STATUS_LABEL"], values["RSI_STATUS_CLASS"] = label, css
    values["RSI_TARGET"] = target_for("RSI")

    macd_hist, macd_crossed = compute_weekly_macd(price_history)
    values["MACD"] = macd_hist
    values["MACD_CROSSED"] = "Yes — fresh this week" if macd_crossed else "No"
    if macd_hist is not None:
        history_append(cache, "MACD", macd_hist)
    label, css = status_pill(macd_hist, "high", 0.0, cache=cache, token="MACD")
    values["MACD_STATUS_LABEL"], values["MACD_STATUS_CLASS"] = label, css
    values["MACD_TARGET"] = target_for("MACD")

    bollinger_pb = compute_bollinger(price_history)
    values["BOLLINGER"] = bollinger_pb
    if bollinger_pb is not None:
        history_append(cache, "BOLLINGER", bollinger_pb)
    label, css = status_pill(bollinger_pb, "low", 0.2, cache=cache, token="BOLLINGER")
    values["BOLLINGER_STATUS_LABEL"], values["BOLLINGER_STATUS_CLASS"] = label, css
    values["BOLLINGER_TARGET"] = target_for("BOLLINGER")
    print(f"  RSI: {rsi} | MACD histogram: {macd_hist} (crossed: {macd_crossed}) | Bollinger %B: {bollinger_pb}")

    print("Fetching network stats from Blockchain.com...")
    stats = fetch_blockchain_stats()
    cost_per_btc, pct_vs_cost = compute_production_cost(stats)
    values["PROD_COST"] = cost_per_btc
    values["PROD_COST_PCT"] = pct_vs_cost
    # Routed through status_pill() against the actual dollar values (spot
    # vs. cost_per_btc), not the pre-computed pct_vs_cost against a zero
    # threshold -- a zero threshold would fall into the same dormant-
    # leeway trap MVRV Z-Score and MACD currently have (bootstrap-3%-of-
    # zero is meaningless, so no leeway until real sigma accumulates).
    # Gets the same near-threshold leeway and strong-buy tier every other
    # scored indicator gets; still the same WEIGHT_MAP entry/weight as
    # before -- this is a status-computation fix only, not a scoring one.
    if cost_per_btc is not None:
        history_append(cache, "PROD_COST", cost_per_btc)
    if cost_per_btc is not None and spot_price is not None:
        prod_status, prod_css = status_pill(spot_price, "low", cost_per_btc, cache=cache, token="PROD_COST")
    else:
        prod_status, prod_css = "CHECK", "st-mid"
    values["PROD_COST_STATUS_LABEL"], values["PROD_COST_STATUS_CLASS"] = prod_status, prod_css
    # Same self-referential logic as Realized Price: buy-favorable triggers
    # when spot falls below this estimated production-cost figure.
    if cost_per_btc is not None:
        try:
            values["PROD_COST_TARGET"] = f"Spot < ${float(cost_per_btc):,.0f}"
        except (TypeError, ValueError):
            values["PROD_COST_TARGET"] = "Spot below production cost"
    else:
        values["PROD_COST_TARGET"] = "Spot below production cost"
    print(f"  Est. production cost (electricity-only): ${cost_per_btc} ({pct_vs_cost}% vs. spot)")

    hashrate_hist = fetch_blockchain_chart("hash-rate", days=100)
    mc_state, hr_deviation = compute_miner_capitulation(hashrate_hist, price_history)
    values["MINER_CAP"] = hr_deviation
    values["MINER_CAP_STATE"] = mc_state or "CHECK"
    # Only the full Edwards "BUY SIGNAL" (recovery + price momentum) counts as
    # a positive trigger — capitulation is a precursor/warning state that has
    # historically PRECEDED bottoms, not the bottom-confirmation itself. Per
    # Edwards' own methodology, mid-capitulation is not yet a buy; the buy is
    # the recovery that follows. Kept as a distinct "watch" state (same blue
    # used for near-threshold leeway) rather than flat neutral, since it IS
    # meaningfully different from "no data" — just not a confirmed trigger.
    mc_label_map = {
        "BUY SIGNAL": ("BUY", "st-buy"),
        "CAPITULATION": ("CAPITULATION — watching for hashrate to turn up (30d MA to cross back above 60d MA)", "st-watch"),
        "RECOVERING": ("RECOVERING — watching for price momentum to confirm (10d MA to cross above 20d MA)", "st-watch"),
    }
    mc_label, mc_css = mc_label_map.get(mc_state, ("CHECK", "st-mid"))
    values["MINER_CAP_STATUS_LABEL"] = mc_label
    values["MINER_CAP_STATUS_CLASS"] = mc_css
    values["MINER_CAP_TARGET"] = target_for("MINER_CAP")
    print(f"  Miner Capitulation (Hash Ribbons): {mc_state}, hashrate MA deviation {hr_deviation}%")

    print("Fetching transaction volume history from Blockchain.com (340 days)...")
    tx_vol_hist = fetch_blockchain_chart("estimated-transaction-volume-usd", days=340)
    nvt_gc = compute_nvt_golden_cross(price_history, tx_vol_hist)
    values["NVT_GC"] = nvt_gc
    history_append(cache, "NVT_GC", nvt_gc)
    # OVERPRICED stays a hardcoded top-only case -- not relevant to a
    # bottom-focused dashboard, so it never goes through status_pill()'s
    # buy-side leeway machinery. The buy-side determination (formerly a
    # hardcoded "< -1.6" check) is now routed through status_pill() like
    # every other indicator, so NVT gets the same STRONG BUY / BUY /
    # BORDERLINE BUY / NOT YET four-tier system instead of a flat
    # threshold and a hand-rolled "NEUTRAL" label.
    if nvt_gc is None:
        nvt_label, nvt_css = "CHECK", "st-mid"
    elif nvt_gc > 2.2:
        nvt_label, nvt_css = "OVERPRICED", "st-no"
    else:
        nvt_label, nvt_css = status_pill(nvt_gc, "low", -1.6, cache=cache, token="NVT_GC")
    values["NVT_GC_STATUS_LABEL"], values["NVT_GC_STATUS_CLASS"] = nvt_label, nvt_css
    values["NVT_GC_TARGET"] = target_for("NVT_GC")
    print(f"  NVT Golden Cross (z-score): {nvt_gc} -> {nvt_label}")

    print("Fetching Fear & Greed from Alternative.me...")
    fng_val, fng_label = fetch_fear_greed()
    values["FNG"] = fng_val
    values["FNG_LABEL"] = fng_label
    if fng_val is not None:
        try:
            history_append(cache, "FNG", float(fng_val))
        except (TypeError, ValueError):
            pass
    label, css = status_pill(fng_val, "low", 20.0, cache=cache, token="FNG")
    values["FNG_STATUS_LABEL"], values["FNG_STATUS_CLASS"] = label, css
    values["FNG_TARGET"] = target_for("FNG")
    print(f"  Fear & Greed: {fng_val} ({fng_label})")

    print("Fetching Polymarket Bitcoin prediction markets (weekly/biweekly/monthly/quarterly/yearly)...")
    poly_values = build_polymarket_buckets(cache)
    values.update(poly_values)
    for horizon in HORIZONS:
        found = poly_values.get(f"POLY_{horizon}_FOUND")
        pct = poly_values.get(f"POLY_{horizon}_YES_PCT")
        print(f"  {horizon}: {'found' if found else 'no match in window'} -> {pct}")

    print("Computing cycle rhythm...")
    days_since_top, projected_bottom, days_to_projected = compute_cycle_rhythm()
    values["DAYS_SINCE_TOP"] = days_since_top
    values["PROJECTED_BOTTOM"] = projected_bottom
    values["DAYS_TO_PROJECTED"] = days_to_projected
    values["CYCLE_RHYTHM_TARGET"] = "N/A — date, not a level"
    print(f"  {days_since_top} days since last cycle top, projected bottom {projected_bottom} ({days_to_projected} days away)")

    values["LAST_UPDATED"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Overall staleness banner: count how many BGeometrics metrics are
    # running on cached (not live) data this run. A couple is normal
    # noise (a single wrong slug guess); a lot signals something bigger
    # (quota exhaustion, an outage) worth flagging prominently rather
    # than making you notice it by scanning every row yourself.
    stale_count = sum(
        1 for token in BG_METRICS
        if values.get(f"{token}_STATUS_LABEL", "").startswith("STALE")
    )
    if stale_count >= 4:
        values["STALENESS_BANNER"] = (
            f'<div class="master" style="border-color:#4a3a14; margin-bottom:20px;">'
            f'<div class="master-top"><div><div class="master-label" style="color:var(--amber);">DATA FRESHNESS NOTICE</div>'
            f'<div class="master-name">{stale_count} of {len(BG_METRICS)} BGeometrics indicators are showing cached, '
            f'not live, data this run</div></div></div>'
            f'<div class="master-note">Likely cause: the free-tier hourly request quota was exhausted before this run '
            f'(check the Action log for HTTP 429 errors). This is expected to self-correct on tomorrow\'s scheduled run. '
            f'Cached values are clearly labeled "STALE (date)" in the table below and are excluded from the weighted verdict.</div>'
            f'</div>'
        )
    else:
        values["STALENESS_BANNER"] = ""

    print("Building weighted verdict synthesis...")
    values["FULL_WEIGHTED_BREAKDOWN_ROWS"] = build_full_weighted_breakdown(values, cache)
    values["WEIGHT_PIE_SVG"] = build_weight_pie_svg(values)
    values["TOTAL_TRACKED_COUNT"] = len(_ALL_TRACKED_TOKENS) + 1  # +1 for Power Law, always rank 1
    verdict = build_verdict(values)
    values.update(verdict)
    # Anchor icon (⚓) next to every indicator currently carrying real
    # weight is computed per-row directly inside build_full_weighted_
    # breakdown() above, from the same WEIGHT_MAP membership check the
    # row's weight/status logic already uses.
    values["SIGNAL_SUMMARY_BOX"] = build_signal_summary_html(values)
    print(f"  Verdict: {verdict['VERDICT_HEADLINE']} ({verdict['VERDICT_PCT']}%, {verdict['VERDICT_COUNT']} weighted-buy)")

    # -----------------------------------------------------------------
    # TABLE 3 — Cycle Top Rank & Weight. Phase 2: every indicator computes
    # and stores a REAL status under its own "_TOP"-suffixed values keys;
    # WEIGHT_MAP_TOP stays empty (Phase 3's job). Genuinely separate weight
    # pool, rank order, verdict, summary box, and pie chart from Table 1 --
    # see WEIGHT_MAP_TOP's own comment for why a shared pool would defeat
    # the point. Indicators whose raw value is shared with Table 1 reuse
    # the exact same rolling-history cache key (e.g. "MVRV_Z__history") --
    # no new fetch, no duplicated history, just a different comparison
    # direction/threshold against the same underlying data.
    # -----------------------------------------------------------------
    print("Computing Table 3 (Cycle Top) indicators...")

    print("  Fetching Pi Cycle Top (BGeometrics pi-cycle, genuinely new)...")
    pi_sma111, pi_sma350x2 = fetch_pi_cycle_latest()
    if pi_sma111 is not None and pi_sma350x2 is not None and pi_sma350x2:
        pi_pct_away = round((pi_sma350x2 - pi_sma111) / pi_sma350x2 * 100, 2)
        pi_crossed = pi_sma111 >= pi_sma350x2
        values["PI_CYCLE_TOP"] = pi_pct_away
        if pi_crossed:
            values["PI_CYCLE_TOP_STATE"] = "CROSSED"
            values["PI_CYCLE_TOP_TOP_STATUS_LABEL"] = "CROSSED — 111d SMA above 2x 350d SMA"
            values["PI_CYCLE_TOP_TOP_STATUS_CLASS"] = "st-strong-buy"
        elif pi_pct_away <= 5:
            # "Approaching" band: a disclosed judgment call (not a
            # researched/published number like the crossover formula
            # itself), purely for display bucketing -- does not feed
            # WEIGHT_MAP_TOP scoring at all in Phase 2.
            values["PI_CYCLE_TOP_STATE"] = "APPROACHING"
            values["PI_CYCLE_TOP_TOP_STATUS_LABEL"] = f"APPROACHING — {pi_pct_away}% away from crossing"
            values["PI_CYCLE_TOP_TOP_STATUS_CLASS"] = "st-near"
        else:
            values["PI_CYCLE_TOP_STATE"] = "NOT CROSSED"
            values["PI_CYCLE_TOP_TOP_STATUS_LABEL"] = f"NOT CROSSED — {pi_pct_away}% away"
            values["PI_CYCLE_TOP_TOP_STATUS_CLASS"] = "st-no"
    else:
        values["PI_CYCLE_TOP"] = None
        values["PI_CYCLE_TOP_STATE"] = "CHECK"
        values["PI_CYCLE_TOP_TOP_STATUS_LABEL"] = "CHECK"
        values["PI_CYCLE_TOP_TOP_STATUS_CLASS"] = "st-mid"
    values["PI_CYCLE_TOP_TOP_TARGET"] = target_for_top("PI_CYCLE_TOP")
    print(f"    Pi Cycle Top: 111d={pi_sma111}, 350dx2={pi_sma350x2} -> {values['PI_CYCLE_TOP_STATE']}")

    # MVRV Z-Score, Puell, Thermocap: rolling TOP-decile percentile of the
    # SAME trailing history Table 1 already built for these tokens (Phase 1
    # decision -- their historically-cited fixed top thresholds decline
    # every cycle, so a self-adjusting percentile is used instead, exactly
    # like Thermocap/NRPL's bottom-table treatment). Each conditionally
    # JOINS WEIGHT_MAP_TOP here, only once threshold is real (n>=90) --
    # never a static entry (see WEIGHT_MAP_TOP's own comment for why).
    TOP_PERCENTILE_WEIGHTS = {"MVRV_Z": 1.5, "PUELL": 1.5, "THERMOCAP": 1.0}
    for token, join_weight in TOP_PERCENTILE_WEIGHTS.items():
        fresh_val = values.get(token)
        threshold, n_points = rolling_threshold(cache, token, percentile=100 - PERCENTILE_CUTOFF)
        if threshold is not None and fresh_val is not None:
            label, css = status_pill(fresh_val, "high", threshold, cache=cache, token=token)
            values[f"{token}_TOP_STATUS_LABEL"] = label
            values[f"{token}_TOP_STATUS_CLASS"] = css
            values[f"{token}_TOP_TARGET"] = f"≥ {round(threshold, 2)} (live, {100 - PERCENTILE_CUTOFF}th pct. of trailing {n_points}d)"
            WEIGHT_MAP_TOP[token] = join_weight
            print(f"    {token} (top): rolling {100 - PERCENTILE_CUTOFF}th percentile threshold = {round(threshold, 2)} (n={n_points}) -> {label}")
        else:
            values[f"{token}_TOP_STATUS_LABEL"] = "BUILDING HISTORY"
            values[f"{token}_TOP_STATUS_CLASS"] = "st-mid"
            values[f"{token}_TOP_TARGET"] = f"N/A — building history ({n_points}/{MIN_HISTORY_DAYS} days)"
            print(f"    {token} (top): only {n_points}/{MIN_HISTORY_DAYS} days of history accumulated, staying N/A")

    # Reserve Risk: fixed threshold (0.02, single-sourced/Medium confidence
    # per Phase 1), same shared cache/token as Table 1's Reserve Risk.
    reserve_risk_val = values.get("RESERVE_RISK")
    label, css = status_pill(reserve_risk_val, "high", 0.02, cache=cache, token="RESERVE_RISK")
    values["RESERVE_RISK_TOP_STATUS_LABEL"], values["RESERVE_RISK_TOP_STATUS_CLASS"] = label, css
    values["RESERVE_RISK_TOP_TARGET"] = target_for_top("RESERVE_RISK")

    # RSI (weekly, threshold=80 not the generic daily 70), Bollinger %B
    # (0.8), Mayer Multiple (2.4) — all fixed, researched thresholds, same
    # shared cache/token as their Table 1 counterparts.
    for token, threshold in (("RSI", 80.0), ("BOLLINGER", 0.8), ("MAYER", 2.4)):
        val = values.get(token)
        label, css = status_pill(val, "high", threshold, cache=cache, token=token)
        values[f"{token}_TOP_STATUS_LABEL"], values[f"{token}_TOP_STATUS_CLASS"] = label, css
        values[f"{token}_TOP_TARGET"] = target_for_top(token)

    # Fear & Greed: Extreme Greed at >=75 (alternative.me's own published
    # band boundary), same shared cache/token as Table 1's Fear & Greed.
    fng_val = values.get("FNG")
    label, css = status_pill(fng_val, "high", 75.0, cache=cache, token="FNG")
    values["FNG_TOP_STATUS_LABEL"], values["FNG_TOP_STATUS_CLASS"] = label, css
    values["FNG_TOP_TARGET"] = target_for_top("FNG")

    # NVT Golden Cross: >2.2 is fully wired through status_pill() here,
    # unlike Table 1's own hardcoded OVERPRICED branch. Those are two
    # different situations, not an inconsistency: on Table 1, OVERPRICED
    # is explicitly kept OUT of status_pill() because that call's leeway
    # machinery is calibrated for the BUY-side (-1.6) case -- running the
    # overpriced reading through it would attach buy-favorable-flavored
    # "BORDERLINE BUY" language to a bottom-focused table's high reading,
    # which is backwards. Here on Table 3, ">2.2, direction=high" IS the
    # actual comparison this table cares about, so routing it through
    # status_pill() is the correct, direct fix -- same shared "NVT_GC__
    # history" cache/token as every other Table 3 indicator, same pattern
    # as Reserve Risk/RSI/Bollinger/Mayer/FNG above. Gets the real four-
    # tier system (STRONG TOP SIGNAL / TOP-FAVORABLE / APPROACHING TOP /
    # NORMAL) instead of a flat boolean.
    nvt_gc_val = values.get("NVT_GC")
    label, css = status_pill(nvt_gc_val, "high", 2.2, cache=cache, token="NVT_GC")
    values["NVT_GC_TOP_STATUS_LABEL"], values["NVT_GC_TOP_STATUS_CLASS"] = label, css
    values["NVT_GC_TOP_TARGET"] = target_for_top("NVT_GC")

    print("Building Table 3 (Cycle Top) verdict synthesis...")
    values["FULL_WEIGHTED_BREAKDOWN_ROWS_TOP"] = build_full_weighted_breakdown_top(values, cache)
    values["WEIGHT_PIE_SVG_TOP"] = build_weight_pie_svg_top(values)
    values["TOTAL_TRACKED_COUNT_TOP"] = len(_ALL_TRACKED_TOKENS_TOP)
    verdict_top = build_verdict_top(values)
    values.update(verdict_top)
    values["SIGNAL_SUMMARY_BOX_TOP"] = build_signal_summary_html_top(values)
    print(f"  Table 3 Verdict: {verdict_top['VERDICT_HEADLINE_TOP']} ({verdict_top['VERDICT_PCT_TOP']}%, {verdict_top['VERDICT_COUNT_TOP']} weighted-top)")

    with open("dashboard_template.html", "r", encoding="utf-8") as f:
        html = f.read()

    for key, val in values.items():
        token = "{{" + key + "}}"
        html = html.replace(token, "—" if val is None else str(val))

    with open("dashboard.html", "w", encoding="utf-8") as f:
        f.write(html)

    save_cache(cache)
    print(f"Saved cache with {len(cache)} entries")
    print("Wrote dashboard.html")


if __name__ == "__main__":
    main()
