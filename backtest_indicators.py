#!/usr/bin/env python3
"""
backtest_indicators.py — standalone, on-demand backtest of every indicator's
selectivity (firing frequency) and forward-looking success rate.

NOT part of the daily automated pipeline. Does not modify WEIGHT_MAP or any
live scoring logic. Read-only analysis report.

CRITICAL DESIGN CONSTRAINT — every status computation below calls the real
functions from update_dashboard.py (status_pill, get_effective_sigma,
near_threshold_band, rolling_threshold, compute_asopr_estimate,
compute_mayer_and_drawdown, compute_weekly_rsi, compute_weekly_macd,
compute_bollinger, compute_production_cost, compute_miner_capitulation,
compute_nvt_golden_cross, fit_power_law, predict_power_law) rather than
reimplementing any of that math here. See the imports below.

WALK-FORWARD, NO LOOK-AHEAD — for every day D being tested, only data up to
and including D is fed into the live functions:
  - Rolling-history caches (for sigma/leeway/percentile thresholds) are built
    incrementally day by day, exactly mirroring how last_known_good.json
    actually accumulates in production (append today's value, THEN classify
    today's status against that same, now-updated, history).
  - Price/volume-derived indicators (RSI, Mayer, MACD, Bollinger, Drawdown,
    NVT Golden Cross, Miner Capitulation) use a trailing 340-day window
    ending at D, matching main()'s literal fetch_coingecko_history(days=340)
    / fetch_blockchain_chart(..., days=340) calls exactly -- not a longer,
    ever-growing window, which would let later (more historical) data creep
    into an earlier day's indicator value and silently diverge from what
    production actually computed on that day.
  - Active Addresses Power-Law Deviation refits fit_power_law() fresh for
    each day D using only the addresses data through D -- using the single
    fit derived from the FULL file (through today) to evaluate an old day
    would itself be look-ahead bias baked into the backtest, even though the
    live system's daily refit is legitimate (it only ever sees data through
    "today" when it runs).

KNOWN LIMITATION -- READ BEFORE TRUSTING THE ON-CHAIN RESULTS: every
BGeometrics-sourced metric (MVRV Z-Score, Puell, Reserve Risk, LTH-SOPR,
aSOPR/SOPR, Supply-in-Loss, Thermocap, NRPL, Realized Price) is capped at
~4 years of history (~1,460 days, starting 2022-08-06) by BGeometrics' own
free-tier window -- confirmed directly against their API, not assumed. That
window does not contain a full prior having/bottom cycle, so several of
these indicators fire only a handful of times in this backtest (some as few
as 2-3 events, flagged inline as [LOW-N] in the report). A high success rate
built on 2-3 events is not a statistically meaningful claim -- it is one or
two historical coincidences dressed as a percentage. This ~4-year window
grows by one real day every time the daily dashboard workflow runs (it
accumulates in last_known_good.json), so re-running this script periodically
as more history naturally builds up is the intended way to get a more
trustworthy read over time -- there is no way to backfill it faster than
that without a paid BGeometrics tier. Price-derived and unmetered-source
indicators (RSI, Mayer, MACD, Bollinger, Drawdown, Active Addresses, Fear &
Greed) are NOT subject to this cap and already span much deeper history.
"""
import csv
import json as json_module
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import update_dashboard as ud

BG_KEY_MISSING_NOTE = (
    "BGeometrics historical-range queries below are unauthenticated (no "
    "BGEOMETRICS_API_KEY needed for the startday/endday range endpoint, "
    "confirmed in Step 1) -- but still bound by the documented 10 req/hour "
    "cap. This script makes exactly 9 BGeometrics calls, one per on-chain "
    "metric, with a short delay between them."
)

# ---------------------------------------------------------------------------
# Fetch layer -- each function returns [(date, value), ...] sorted ascending.
# Reuses ud._get_json (the real HTTP primitive) and ud.BG_BASE / ud.BG_METRICS
# so slugs/thresholds/directions never drift from what update_dashboard.py
# actually uses live.
# ---------------------------------------------------------------------------
_METADATA_KEYS = {"d", "date", "unixts", "unix_ts", "timestamp", "time"}


def fetch_bg_range(slug, start="2009-01-01", end=None, retries=1, max_wait_seconds=3600):
    """Real HTTP call (not routed through ud._get_json) so a 429 response's
    X-RateLimit-Reset-Hour header can be read and waited out precisely,
    rather than guessing at a fixed backoff -- the documented cap is a
    10/hour HOURLY window (confirmed in Step 1/follow-ups), so a short fixed
    retry would almost always fail again pointlessly."""
    import urllib.request
    import urllib.error
    end = end or date.today().isoformat()
    url = f"{ud.BG_BASE}/{slug}?startday={start}&endday={end}"
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": "btc-dashboard-bot/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json_module.loads(resp.read().decode())
            out = []
            for row in data:
                if not isinstance(row, dict):
                    continue
                d = row.get("d")
                val = None
                for k, v in row.items():
                    if k.lower() not in _METADATA_KEYS:
                        val = v
                        break
                if d and val is not None:
                    out.append((date.fromisoformat(d), float(val)))
            out.sort(key=lambda t: t[0])
            return out, None
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                reset_hdr = e.headers.get("X-RateLimit-Reset-Hour")
                wait_s = 65
                if reset_hdr:
                    wait_s = max(5, min(max_wait_seconds, int(reset_hdr) - int(time.time()) + 5))
                print(f"    ! {slug}: rate-limited (429), waiting {wait_s}s for the hourly window to reset...")
                time.sleep(wait_s)
                continue
            return [], f"HTTP {e.code}: {e.reason}"
        except Exception as e:
            return [], str(e)
    return [], "exhausted retries"


def fetch_pi_cycle_range(start="2009-01-01", end=None, retries=1, max_wait_seconds=3600):
    """Day-by-day (piSma111, piSma350x2) from BGeometrics' pi-cycle feed,
    via the same unauthenticated startday/endday range endpoint as
    fetch_bg_range() (same 10/hour cap, same 429/hourly-reset retry
    handling) -- but pi-cycle returns TWO fields per day (piSma111,
    piSma350x2), not one, so it needs its own small parser rather than
    fetch_bg_range()'s single-value extraction. Used only by the daily
    firing-matrix mode (--daily-matrix) for Table 3's Pi Cycle Top column;
    the standard main() backtest run doesn't touch this metric."""
    import urllib.request
    import urllib.error
    end = end or date.today().isoformat()
    url = f"{ud.BG_BASE}/pi-cycle?startday={start}&endday={end}"
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": "btc-dashboard-bot/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json_module.loads(resp.read().decode())
            out = []
            for row in data:
                if not isinstance(row, dict):
                    continue
                d = row.get("d")
                sma111 = row.get("piSma111")
                sma350x2 = row.get("piSma350x2")
                if d and sma111 is not None and sma350x2 is not None:
                    out.append((date.fromisoformat(d), float(sma111), float(sma350x2)))
            out.sort(key=lambda t: t[0])
            return out, None
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                reset_hdr = e.headers.get("X-RateLimit-Reset-Hour")
                wait_s = 65
                if reset_hdr:
                    wait_s = max(5, min(max_wait_seconds, int(reset_hdr) - int(time.time()) + 5))
                print(f"    ! pi-cycle: rate-limited (429), waiting {wait_s}s for the hourly window to reset...")
                time.sleep(wait_s)
                continue
            return [], f"HTTP {e.code}: {e.reason}"
        except Exception as e:
            return [], str(e)
    return [], "exhausted retries"


def fetch_blockchain_chart_range(chart_name, days):
    url = f"https://api.blockchain.info/charts/{chart_name}?timespan={days}days&format=json&cors=true"
    data = ud._get_json(url)
    out = []
    for pt in data.get("values", []):
        y = pt.get("y")
        if y is None:
            continue
        d = datetime.fromtimestamp(pt["x"], timezone.utc).date()
        out.append((d, float(y)))
    out.sort(key=lambda t: t[0])
    return out


def fetch_fng_range():
    url = "https://api.alternative.me/fng/?limit=0&format=json"
    data = ud._get_json(url)
    out = []
    for e in data.get("data", []):
        d = datetime.fromtimestamp(int(e["timestamp"]), timezone.utc).date()
        out.append((d, float(e["value"])))
    out.sort(key=lambda t: t[0])
    return out


def fetch_active_addresses_range():
    """Reuses ud.fetch_active_addresses_full_history() directly -- same
    unmetered chart-data source, same (days_since_genesis, value, date_str)
    shape the live code already builds its power-law fit from."""
    points = ud.fetch_active_addresses_full_history()
    return points or []


# ---------------------------------------------------------------------------
# Small date-alignment helpers
# ---------------------------------------------------------------------------
def to_dict(series):
    return {d: v for d, v in series}


def nearest_value(series_dict, target_date, tolerance_days=2):
    """Exact date if present, else nearest available date within
    tolerance_days (real data sources occasionally have a day's gap across
    different providers' UTC cutoffs). Returns None if nothing close enough
    exists -- never fabricates a value."""
    if target_date in series_dict:
        return series_dict[target_date]
    for delta in range(1, tolerance_days + 1):
        for cand in (target_date - timedelta(days=delta), target_date + timedelta(days=delta)):
            if cand in series_dict:
                return series_dict[cand]
    return None


# ---------------------------------------------------------------------------
# Walk-forward status classification -- the core no-look-ahead machinery.
# Builds the rolling-history cache incrementally, one day at a time, calling
# ud.status_pill()/ud.get_effective_sigma()/ud.near_threshold_band() exactly
# as production does. `history_series` is what accumulates under `token`
# (normally == the value series itself; PROD_COST is the one live exception
# where the token's history tracks the threshold side -- see PROD_COST
# handling below, which passes a different series for value vs history).
# ---------------------------------------------------------------------------
def walk_forward_statuses(dated_values, direction, threshold_fn, token,
                           history_dated_values=None):
    """dated_values: [(date, value), ...] ascending -- the value status_pill
    is evaluated against each day.
    threshold_fn(cache, date, value) -> threshold for that day (or None to
    mark CHECK).
    history_dated_values: if given, THIS series (not dated_values) is what
    gets appended to the token's rolling-history cache each day -- replicates
    PROD_COST's live quirk where cache/token track cost_per_btc while the
    compared value is spot price.
    Returns [(date, value, label, css), ...]."""
    history_series = history_dated_values if history_dated_values is not None else dated_values
    hist_dict = to_dict(history_series)
    cache = {}
    out = []
    for d, v in dated_values:
        hv = hist_dict.get(d)
        if hv is not None:
            ud.history_append(cache, token, float(hv), date=d)
        if v is None:
            out.append((d, v, "CHECK", "st-mid"))
            continue
        threshold = threshold_fn(cache, d, v)
        if threshold is None:
            out.append((d, v, "CHECK", "st-mid"))
            continue
        label, css = ud.status_pill(v, direction, threshold, cache=cache, token=token)
        out.append((d, v, label, css))
    return out


BUY_FAVORABLE_CSS = ("st-buy", "st-strong-buy", "st-near")


# ---------------------------------------------------------------------------
# Firing-event detection + forward-return testing
# ---------------------------------------------------------------------------
HORIZONS = (90, 180, 365)


def detect_firing_events(statuses):
    """statuses: [(date, value, label, css), ...]. A firing event = the
    FIRST day of a continuous streak where css is buy-favorable. Returns
    list of entry dates."""
    events = []
    in_streak = False
    for d, v, label, css in statuses:
        favorable = css in BUY_FAVORABLE_CSS
        if favorable and not in_streak:
            events.append(d)
            in_streak = True
        elif not favorable:
            in_streak = False
    return events


def test_forward_returns(entry_dates, price_dict, last_available_price_date):
    """For each entry date, look up entry price and forward prices at each
    horizon (skipping any horizon that would extend past the last available
    price date -- never fabricated). Returns per-horizon (successes, total,
    pct_changes)."""
    results = {h: {"successes": 0, "total": 0, "pct_changes": []} for h in HORIZONS}
    skipped_no_entry_price = 0
    for entry_date in entry_dates:
        entry_price = nearest_value(price_dict, entry_date)
        if entry_price is None:
            skipped_no_entry_price += 1
            continue
        for h in HORIZONS:
            forward_date = entry_date + timedelta(days=h)
            if forward_date > last_available_price_date:
                continue  # would require fabricating a future price -- skip
            forward_price = nearest_value(price_dict, forward_date)
            if forward_price is None:
                continue
            pct_change = (forward_price - entry_price) / entry_price * 100
            results[h]["total"] += 1
            results[h]["pct_changes"].append(pct_change)
            if forward_price > entry_price:
                results[h]["successes"] += 1
    return results, skipped_no_entry_price


def format_horizon(results, h):
    r = results[h]
    if r["total"] == 0:
        return "n/a (no complete window)"
    pct = round(r["successes"] / r["total"] * 100, 1)
    flag = "  [LOW-N, not reliable]" if r["total"] < 5 else ""
    return f"{r['successes']}/{r['total']} ({pct}%){flag}"


def format_avg_change(results, h):
    r = results[h]
    if not r["pct_changes"]:
        return "n/a"
    avg = sum(r["pct_changes"]) / len(r["pct_changes"])
    return f"{avg:+.1f}%"


# ---------------------------------------------------------------------------
# Per-indicator reconstruction. Each builder returns a dict:
#   {name, source, category, statuses: [(date, value, label, css), ...]}
# "category" is "price" (price-derived, mean-reversion test) or "onchain"
# (independent-source test), matching the two-way split the report needs.
# ---------------------------------------------------------------------------
PRICE_WINDOW_DAYS = 340  # exactly matches fetch_coingecko_history(days=340) /
                          # fetch_blockchain_chart(..., days=340) in main() --
                          # using anything longer would let a day's indicator
                          # value see more trailing history than production
                          # actually gave it, which is its own kind of drift.


def _trailing_window(dated_series, end_date, window_days):
    """Values only (ascending, oldest first) from the trailing window_days
    ending at end_date inclusive -- mirrors the fixed-depth fetches main()
    actually makes every day."""
    start = end_date - timedelta(days=window_days - 1)
    return [v for d, v in dated_series if start <= d <= end_date]


def _trailing_window_dated(dated_series, end_date, window_days):
    """Same trailing window as _trailing_window(), but keeps the (date,
    value) tuples -- needed by ud.detect_rsi_divergence()/
    detect_mvrv_price_divergence(), which match points by date, not just
    by value."""
    start = end_date - timedelta(days=window_days - 1)
    return [(d, v) for d, v in dated_series if start <= d <= end_date]


def build_simple_bg_indicator(name, token, series, direction, threshold,
                               source="BGeometrics (startday/endday range)"):
    return {
        "name": name, "source": source,
        "category": "onchain",
        "statuses": walk_forward_statuses(series, direction, lambda c, d, v: threshold, token),
    }


def build_percentile_indicator(name, token, series, source_label, direction="low", percentile=None):
    """Thermocap / NRPL / Active Addresses pattern: dynamic threshold =
    ud.rolling_threshold()'s live percentile of the token's own
    walk-forward-built trailing history (n>=90 gate, identical to
    production's MIN_HISTORY_DAYS bootstrap). direction="low" (Table 1,
    bottom-decile, the original/default use of this function) reproduces
    Thermocap/NRPL exactly as before. direction="high" (Table 3's own
    MVRV Z-Score/Puell/Thermocap top-decile percentile mechanism, main()
    ~4046-4068) reads the SAME shared rolling-history cache key -- passing
    the identical `series` a Table 1 caller also uses for this `token`
    reproduces production's cache-sharing exactly, since the classification
    direction/threshold never affects what gets appended to history, only
    what gets compared against it. `percentile` defaults to
    ud.PERCENTILE_CUTOFF (bottom-favorable) for direction="low" and
    100-ud.PERCENTILE_CUTOFF (top-favorable) for direction="high" -- exact
    mirror of main()'s own two call sites."""
    if percentile is None:
        percentile = ud.PERCENTILE_CUTOFF if direction == "low" else 100 - ud.PERCENTILE_CUTOFF

    def threshold_fn(cache, d, v):
        threshold, n_points = ud.rolling_threshold(cache, token, percentile=percentile)
        return threshold
    return {
        "name": name, "source": source_label, "category": "onchain",
        "statuses": walk_forward_statuses(series, direction, threshold_fn, token),
    }


def build_active_addr_dev(aa_points):
    """aa_points: [(days_since_genesis, value, date_str), ...] ascending,
    the exact shape ud.fetch_active_addresses_full_history() returns.
    Refits ud.fit_power_law() fresh for each day using only points through
    that day -- see module docstring for why this is necessary even though
    the live daily refit itself has no look-ahead problem."""
    dated_values = []
    n = len(aa_points)
    print(f"    Refitting power law day-by-day across {n} days (this is the slow step)...")
    t0 = time.time()
    for i in range(n):
        if i + 1 < 30:
            continue
        window = aa_points[: i + 1]
        slope, intercept, r2 = ud.fit_power_law([(d, v) for d, v, _ in window])
        if slope is None:
            continue
        today_days, today_val, today_date = window[-1]
        predicted = ud.predict_power_law(slope, intercept, today_days)
        if not predicted:
            continue
        deviation = round((today_val - predicted) / predicted * 100, 2)
        dated_values.append((today_date, deviation))
    print(f"    Done in {time.time()-t0:.1f}s ({len(dated_values)} testable days)")
    return build_percentile_indicator(
        "Active Addresses Power-Law Deviation", "ACTIVE_ADDR_DEV", dated_values,
        "BGeometrics chart file (addresses_active.json, unmetered)",
    )


def build_realized_price(realized_price_series, market_price_series):
    """Live quirk (from the round-2 fix): value=spot price, threshold=
    today's realized-price feed reading, but the leeway cache/token is
    REALIZED_PRICE_GAP, whose history tracks spot price (the value side) --
    not the realized-price feed itself."""
    rp_dict = to_dict(realized_price_series)
    mp_dict = to_dict(market_price_series)
    common_dates = sorted(set(rp_dict) & set(mp_dict))
    dated_values = [(d, mp_dict[d]) for d in common_dates]
    threshold_lookup = {d: rp_dict[d] for d in common_dates}

    def threshold_fn(cache, d, v):
        return threshold_lookup.get(d)

    return {
        "name": "Price vs. Realized Price", "source": "BGeometrics (realized-price) + Blockchain.com market-price",
        "category": "onchain",
        "statuses": walk_forward_statuses(dated_values, "low", threshold_fn, "REALIZED_PRICE_GAP"),
    }


def build_supply_loss(supply_profit_series, total_bitcoins_series):
    sp_dict = to_dict(supply_profit_series)
    tb_dict = to_dict(total_bitcoins_series)
    dated_values = []
    for d, supply_profit_val in supply_profit_series:
        circulating_btc_sats = nearest_value(tb_dict, d, tolerance_days=3)
        if circulating_btc_sats is None:
            continue
        # Exact mirror of the two-line inline formula at update_dashboard.py
        # ~1969-1970 (not a separate function in the live code -- this is
        # the one indicator where no importable function exists to call).
        try:
            percent_in_profit = (supply_profit_val / circulating_btc_sats) * 100
            supply_loss_val = round(100 - percent_in_profit, 2)
        except ZeroDivisionError:
            continue
        dated_values.append((d, supply_loss_val))
    return {
        "name": "% Supply in Loss", "source": "BGeometrics (supply-profit) + Blockchain.com total-bitcoins",
        "category": "onchain",
        "statuses": walk_forward_statuses(dated_values, "high", lambda c, d, v: 50.0, "SUPPLY_LOSS"),
    }


def build_asopr_est(sopr_series):
    dated_values = [(d, ud.compute_asopr_estimate(v)) for d, v in sopr_series]
    dated_values = [(d, v) for d, v in dated_values if v is not None]
    return {
        "name": "aSOPR (modeled)", "source": "BGeometrics (sopr) via ud.compute_asopr_estimate()",
        "category": "onchain",
        "statuses": walk_forward_statuses(dated_values, "low", lambda c, d, v: 1.0, "ASOPR_EST"),
    }


def build_price_derived(name, token, market_price_series, direction, threshold, extractor):
    """Generic builder for RSI / MACD / Bollinger / Mayer / Drawdown: all
    share the same 340-day trailing price window and the same walk-forward
    leeway mechanism. `extractor(price_window)` returns this indicator's
    value (or None) from ud's real compute_* function."""
    dated_values = []
    for d, _ in market_price_series:
        window = _trailing_window(market_price_series, d, PRICE_WINDOW_DAYS)
        v = extractor(window)
        if v is not None:
            dated_values.append((d, v))
    return {
        "name": name, "source": "Blockchain.com market-price (340-day trailing window)",
        "category": "price",
        "statuses": walk_forward_statuses(dated_values, direction, lambda c, d, v: threshold, token),
    }


def _nvt_gc_dated_raw(market_price_series, tx_volume_series):
    """Shared by build_nvt_gc() (Table 1) and build_nvt_gc_top() (Table 3)
    -- the single place that calls the real ud.compute_nvt_golden_cross()
    day by day over the PRICE_WINDOW_DAYS trailing window, so both tables
    build their status off the exact same raw NVT Golden Cross values
    (and, in turn, the exact same "NVT_GC__history" cache content once
    each table's own walk_forward_statuses() call appends it)."""
    dated_raw = []
    for d, _ in market_price_series:
        price_window = _trailing_window(market_price_series, d, PRICE_WINDOW_DAYS)
        vol_window = _trailing_window(tx_volume_series, d, PRICE_WINDOW_DAYS)
        if len(vol_window) < 330:
            continue
        nvt_gc = ud.compute_nvt_golden_cross(price_window, vol_window)
        if nvt_gc is not None:
            dated_raw.append((d, nvt_gc))
    return dated_raw


def build_nvt_gc(market_price_series, tx_volume_series):
    dated_raw = _nvt_gc_dated_raw(market_price_series, tx_volume_series)

    cache = {}
    statuses = []
    for d, v in dated_raw:
        ud.history_append(cache, "NVT_GC", float(v), date=d)
        if v > 2.2:
            statuses.append((d, v, "OVERPRICED", "st-no"))
        else:
            label, css = ud.status_pill(v, "low", -1.6, cache=cache, token="NVT_GC")
            statuses.append((d, v, label, css))
    return {
        "name": "NVT Golden Cross", "source": "Blockchain.com market-price + estimated-transaction-volume-usd",
        "category": "onchain", "statuses": statuses,
    }


def build_nvt_gc_top(market_price_series, tx_volume_series):
    """Table 3's own NVT Golden Cross: direction="high" @ 2.2, fully routed
    through status_pill() (unlike Table 1's hardcoded OVERPRICED branch --
    see main()'s own ~4093-4110 comment for why that's the correct,
    deliberate difference, not an inconsistency)."""
    dated_raw = _nvt_gc_dated_raw(market_price_series, tx_volume_series)
    return {
        "name": "NVT Golden Cross (Top)", "source": "Blockchain.com market-price + estimated-transaction-volume-usd",
        "category": "onchain",
        "statuses": walk_forward_statuses(dated_raw, "high", lambda c, d, v: 2.2, "NVT_GC"),
    }


def _build_miner_capitulation_dated_raw(hashrate_series, market_price_series):
    """Shared by build_miner_capitulation() and the RECOVERING-vs-BUY-SIGNAL
    variant analysis below -- the single place that calls the real
    ud.compute_miner_capitulation() day by day over a trailing window,
    exactly matching live's fetch_blockchain_chart(..., days=100) hash-rate
    depth and the PRICE_WINDOW_DAYS=340 price depth. Returns
    [(date, hr_dev, mc_state), ...] ascending."""
    dated_raw = []
    for d, _ in hashrate_series:
        hr_window = _trailing_window(hashrate_series, d, 100)  # matches live fetch_blockchain_chart(..., days=100)
        price_window = _trailing_window(market_price_series, d, PRICE_WINDOW_DAYS)
        if len(hr_window) < 60 or len(price_window) < 20:
            continue
        mc_state, hr_dev = ud.compute_miner_capitulation(hr_window, price_window)
        dated_raw.append((d, hr_dev, mc_state))
    return dated_raw


def build_miner_capitulation(hashrate_series, market_price_series):
    dated_raw = _build_miner_capitulation_dated_raw(hashrate_series, market_price_series)

    statuses = []
    for d, hr_dev, mc_state in dated_raw:
        if mc_state == "BUY SIGNAL":
            statuses.append((d, hr_dev, "BUY", "st-buy"))
        elif mc_state in ("CAPITULATION", "RECOVERING"):
            statuses.append((d, hr_dev, mc_state, "st-watch"))
        else:
            statuses.append((d, hr_dev, "CHECK", "st-mid"))
    return {
        "name": "Miner Capitulation (Hash Ribbons)", "source": "Blockchain.com hash-rate + market-price",
        "category": "onchain", "statuses": statuses,
    }


# ---------------------------------------------------------------------------
# Miner Capitulation RECOVERING-vs-BUY-SIGNAL variant analysis.
#
# Reuses _build_miner_capitulation_dated_raw() (i.e. ud.compute_miner_capitulation()
# itself) as the single source of truth for state on each day -- everything
# below is pure bookkeeping over that already-computed state sequence, not a
# reimplementation of the Hash Ribbons math.
#
#   Variant 1 (STRICT)              -- existing baseline: build_miner_capitulation()
#                                       + detect_firing_events(), unchanged.
#   Variant 2 (RECOVERING-inclusive) -- extract_combined_streaks() below.
#   Variant 3 (RECOVERING-only,
#              isolated false starts) -- classify_streaks()'s "isolated" bucket.
# ---------------------------------------------------------------------------
def extract_combined_streaks(dated_raw):
    """Maximal streaks of consecutive days where mc_state in (RECOVERING,
    BUY SIGNAL) -- Variant 2's favorable set. A streak that goes
    RECOVERING -> BUY SIGNAL is ONE streak (no CAPITULATION day breaks it),
    matching the "continuous streak = one event" rule detect_firing_events()
    already uses for every other indicator in this file.

    Returns a list of streak dicts:
      {"entry_date": <first day of the streak>,
       "states": [(date, state), ...] for every day in the streak,
       "exit_state": "CAPITULATION" if the streak was closed by a
                      CAPITULATION day, or None if the streak is still open
                      at the end of the dataset (unresolved -- not yet a
                      confirmed conversion OR a confirmed false start)."""
    streaks = []
    current = None
    for d, hr_dev, state in dated_raw:
        favorable = state in ("RECOVERING", "BUY SIGNAL")
        if favorable:
            if current is None:
                current = {"entry_date": d, "states": []}
            current["states"].append((d, state))
        else:
            if current is not None:
                current["exit_state"] = "CAPITULATION"
                streaks.append(current)
                current = None
    if current is not None:
        current["exit_state"] = None  # still open at end of dataset
        streaks.append(current)
    return streaks


def classify_streaks(streaks):
    """Splits the combined streaks (Variant 2's population) into the pieces
    Variant 3 and the conversion-rate/timing metrics need. Pure bookkeeping
    over what extract_combined_streaks() already found -- no new state
    computation happens here.

    Returns dict with:
      variant2_events        -- entry date of every combined streak.
      recovering_streaks_total -- count of streaks that STARTED as RECOVERING
                                   (i.e. had a genuine RECOVERING precursor day).
      direct_buy_streaks     -- count of streaks that started directly as
                                 BUY SIGNAL with no RECOVERING precursor.
      converted               -- RECOVERING-started streaks that reached a
                                  BUY SIGNAL day: [{entry_date, first_buy_date,
                                  days_to_confirm}, ...].
      isolated                -- RECOVERING-started streaks that reverted to
                                  CAPITULATION WITHOUT ever reaching BUY
                                  SIGNAL (genuine false starts, = Variant 3):
                                  [{entry_date, exit_date}, ...].
      ongoing                 -- RECOVERING-started streaks still open at the
                                  end of the dataset -- outcome unresolved,
                                  excluded from conversion-rate denominators.
      variant3_events         -- entry dates of the "isolated" bucket."""
    variant2_events = [s["entry_date"] for s in streaks]

    recovering_streaks = [s for s in streaks if s["states"][0][1] == "RECOVERING"]
    direct_buy_streaks = [s for s in streaks if s["states"][0][1] == "BUY SIGNAL"]

    converted, isolated, ongoing = [], [], []
    for s in recovering_streaks:
        buy_dates = [d for d, st in s["states"] if st == "BUY SIGNAL"]
        if buy_dates:
            first_buy = min(buy_dates)
            days_to_confirm = (first_buy - s["entry_date"]).days
            converted.append({"entry_date": s["entry_date"], "first_buy_date": first_buy,
                               "days_to_confirm": days_to_confirm})
        elif s["exit_state"] == "CAPITULATION":
            isolated.append({"entry_date": s["entry_date"], "exit_date": s["states"][-1][0]})
        else:
            ongoing.append({"entry_date": s["entry_date"]})

    return {
        "variant2_events": variant2_events,
        "recovering_streaks_total": len(recovering_streaks),
        "direct_buy_streaks": len(direct_buy_streaks),
        "converted": converted, "isolated": isolated, "ongoing": ongoing,
        "variant3_events": [e["entry_date"] for e in isolated],
    }


def median(values):
    s = sorted(values)
    n = len(s)
    if n == 0:
        return None
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


def _test_streak_detection():
    """[TEST 3] Synthetic streak-detection correctness checks -- confirms a
    continuous RECOVERING-then-BUY-SIGNAL run is treated as ONE Variant 2/3
    event (not double-counted), while still yielding a separate, correctly
    later-dated Variant 1 event at the BUY SIGNAL transition, and that two
    RECOVERING runs split by a CAPITULATION day are correctly counted as two
    distinct events, not merged into one."""
    print(f"\n{'-'*100}\n[TEST 3] Streak-detection correctness (synthetic cases)\n{'-'*100}")
    base = date(2024, 1, 1)

    def mkday(offset, state):
        return (base + timedelta(days=offset), 0.0, state)

    # Case A: RECOVERING(3d) -> BUY SIGNAL(2d) -> CAPITULATION.
    # One converted combined streak; Variant 1 must see exactly ONE event,
    # landing on the BUY SIGNAL transition day (day 4), not day 1.
    case_a = [mkday(0, "CAPITULATION"), mkday(1, "RECOVERING"), mkday(2, "RECOVERING"),
              mkday(3, "RECOVERING"), mkday(4, "BUY SIGNAL"), mkday(5, "BUY SIGNAL"),
              mkday(6, "CAPITULATION")]
    streaks_a = extract_combined_streaks(case_a)
    cls_a = classify_streaks(streaks_a)
    assert len(streaks_a) == 1, f"expected 1 combined streak, got {len(streaks_a)}"
    assert cls_a["variant2_events"] == [base + timedelta(days=1)], cls_a["variant2_events"]
    assert len(cls_a["converted"]) == 1 and cls_a["converted"][0]["days_to_confirm"] == 3, cls_a["converted"]
    assert len(cls_a["isolated"]) == 0

    statuses_a = [(d, v, ("BUY" if s == "BUY SIGNAL" else s), ("st-buy" if s == "BUY SIGNAL" else "st-watch"))
                  for d, v, s in case_a]
    v1_events_a = detect_firing_events(statuses_a)
    assert v1_events_a == [base + timedelta(days=4)], f"variant1 mis-detected: {v1_events_a}"
    print("  Case A (RECOVERING(3d) -> BUY SIGNAL(2d), one continuous run): PASS -- "
          "1 combined streak, Variant 2 entry on day 1 (first RECOVERING day), "
          "Variant 1 entry correctly lands on day 4 (the BUY SIGNAL transition, NOT "
          "double-counted as a separate day-1 event), days_to_confirm=3.")

    # Case B: RECOVERING(4d) -> CAPITULATION. False start, never converts.
    case_b = [mkday(0, "CAPITULATION"), mkday(1, "RECOVERING"), mkday(2, "RECOVERING"),
              mkday(3, "RECOVERING"), mkday(4, "RECOVERING"), mkday(5, "CAPITULATION")]
    cls_b = classify_streaks(extract_combined_streaks(case_b))
    assert len(cls_b["isolated"]) == 1 and len(cls_b["converted"]) == 0
    assert cls_b["variant3_events"] == [base + timedelta(days=1)], cls_b["variant3_events"]
    print("  Case B (RECOVERING never confirms, reverts straight to CAPITULATION): PASS -- "
          "correctly isolated as a Variant 3 false start, 0 conversions.")

    # Case C: two SEPARATE RECOVERING->BUY SIGNAL runs split by a CAPITULATION
    # day -- must be counted as 2 events, never merged into 1.
    case_c = [mkday(0, "CAPITULATION"), mkday(1, "RECOVERING"), mkday(2, "BUY SIGNAL"),
              mkday(3, "CAPITULATION"), mkday(4, "RECOVERING"), mkday(5, "BUY SIGNAL")]
    streaks_c = extract_combined_streaks(case_c)
    assert len(streaks_c) == 2, f"expected 2 separate streaks, got {len(streaks_c)}"
    cls_c = classify_streaks(streaks_c)
    assert cls_c["variant2_events"] == [base + timedelta(days=1), base + timedelta(days=4)]
    assert len(cls_c["converted"]) == 2
    print("  Case C (two separate RECOVERING->BUY SIGNAL runs split by a CAPITULATION day): "
          "PASS -- correctly counted as 2 distinct events, not merged.")

    print("  All synthetic streak-detection checks passed.")


def _demonstrate_no_look_ahead(hashrate_series, market_price_series, dated_raw):
    """[TEST 2] Concrete, printed proof that a given day D's Miner Capitulation
    state is computed from data ending at D only -- same walk-forward
    discipline as every other backtest in this file. Picks one real day D,
    shows the exact hash-rate/price windows fed into
    ud.compute_miner_capitulation() for D never contain a date after D, then
    independently re-derives D's state from a fresh <=D slice of the raw
    series and confirms it matches what the walk-forward loop produced --
    proving the trailing-window slicing is what enforces this, not an
    accident of iteration order. Finally shows future data for D genuinely
    exists in the fetched dataset but was excluded from D's window by
    construction (not just "didn't exist yet")."""
    print(f"\n{'-'*100}\n[TEST 2] Walk-forward / no-look-ahead concrete proof\n{'-'*100}")
    sample_date, sample_hr_dev, sample_state = dated_raw[len(dated_raw) // 2]

    hr_window_dates = [d for d, v in hashrate_series if sample_date - timedelta(days=99) <= d <= sample_date]
    price_window_dates = [d for d, v in market_price_series
                           if sample_date - timedelta(days=PRICE_WINDOW_DAYS - 1) <= d <= sample_date]
    print(f"  Sample day D = {sample_date}  (state computed during walk-forward: {sample_state}, "
          f"hr_dev={sample_hr_dev})")
    print(f"  hash-rate window fed to compute_miner_capitulation(): {len(hr_window_dates)} days, "
          f"{hr_window_dates[0]} .. {hr_window_dates[-1]}")
    print(f"  price window fed to compute_miner_capitulation():     {len(price_window_dates)} days, "
          f"{price_window_dates[0]} .. {price_window_dates[-1]}")
    assert hr_window_dates[-1] == sample_date, "hash-rate window leaks past D"
    assert price_window_dates[-1] == sample_date, "price window leaks past D"
    print(f"  -> max date in either window is D itself; no day after D was ever passed into "
          f"compute_miner_capitulation() for D's classification.")

    hr_hist = [v for d, v in hashrate_series if d <= sample_date][-100:]
    price_hist = [v for d, v in market_price_series if d <= sample_date][-PRICE_WINDOW_DAYS:]
    recheck_state, recheck_dev = ud.compute_miner_capitulation(hr_hist, price_hist)
    match = (recheck_state, recheck_dev) == (sample_state, sample_hr_dev)
    print(f"  Independent re-derivation from a fresh <=D slice: state={recheck_state}, hr_dev={recheck_dev} "
          f"-> {'MATCHES' if match else 'MISMATCH'} the walk-forward loop's result for D.")
    assert match, "independent re-derivation diverged from the walk-forward result -- look-ahead bug"

    future_hr_days = [d for d, v in hashrate_series if d > sample_date]
    print(f"  {len(future_hr_days)} more days of hash-rate data exist AFTER D in the fetched dataset "
          f"but were excluded from D's window by construction (proves exclusion is deliberate, not "
          f"just an absence of future data).")


def build_production_cost(hashrate_series, market_price_series):
    """Live quirk preserved deliberately: compared value is spot price,
    threshold is cost_per_btc, but the leeway cache/token (PROD_COST) tracks
    cost_per_btc -- exactly as update_dashboard.py does it today (this was
    investigated and NOT changed in an earlier round; the backtest tests
    the live system as it actually behaves, not a hypothetical fixed one).
    n_blocks_total is approximated via well-documented halving dates (block
    reward only takes 5 known values across Bitcoin's whole history --
    supplying any n_blocks_total within the correct halving epoch produces
    an IDENTICAL result from compute_production_cost(), since it only ever
    uses n_blocks // 210000). minutes_between_blocks is omitted, so the
    real function's own built-in default (10) applies, matching main()'s
    behavior whenever that field isn't supplied."""
    HALVING_DATES = [
        (date(2009, 1, 3), 0), (date(2012, 11, 28), 210_000),
        (date(2016, 7, 9), 420_000), (date(2020, 5, 11), 630_000),
        (date(2024, 4, 20), 840_000),
    ]

    def n_blocks_for(d):
        n = HALVING_DATES[0][1]
        for halving_date, block_height in HALVING_DATES:
            if d >= halving_date:
                n = block_height
        return n

    hr_dict = to_dict(hashrate_series)
    mp_dict = to_dict(market_price_series)
    value_series = []   # spot price (the compared value)
    history_series = []  # cost_per_btc (what PROD_COST's cache actually tracks live)
    for d, hash_rate_th in hashrate_series:
        spot = nearest_value(mp_dict, d)
        if spot is None:
            continue
        # Chart hash-rate is already TH/s (confirmed via the chart's own
        # "unit" field in Step 1); compute_production_cost expects GH/s and
        # divides by 1000 internally, so scale back up to match its contract.
        stats = {"hash_rate": hash_rate_th * 1000.0, "n_blocks_total": n_blocks_for(d), "market_price_usd": spot}
        cost_per_btc, pct_vs_cost = ud.compute_production_cost(stats)
        if cost_per_btc is None:
            continue
        value_series.append((d, spot))
        history_series.append((d, cost_per_btc))

    def threshold_fn(cache, d, v):
        return to_dict(history_series).get(d)

    return {
        "name": "Production Cost (electricity-only)", "source": "Blockchain.com hash-rate + market-price",
        "category": "onchain",
        "statuses": walk_forward_statuses(value_series, "low", threshold_fn, "PROD_COST",
                                           history_dated_values=history_series),
    }


def build_fng(fng_series):
    return build_simple_bg_indicator("Fear & Greed", "FNG", fng_series, "low", 20.0,
                                      source="Alternative.me (full history, limit=0)")


# ---------------------------------------------------------------------------
# Table 3 (Cycle Top) — Pi Cycle Top. Genuinely new metric (no Table 1
# equivalent to share a cache with), no leeway/sigma machinery in
# production either -- direct CROSSED/APPROACHING/NOT CROSSED comparison,
# exact mirror of main()'s own PI_CYCLE_TOP block (~4017-4044).
# ---------------------------------------------------------------------------
def build_pi_cycle_top(pi_series):
    """pi_series: [(date, sma111, sma350x2), ...] ascending, from
    fetch_pi_cycle_range()."""
    statuses = []
    for d, sma111, sma350x2 in pi_series:
        if not sma350x2:
            statuses.append((d, None, "CHECK", "st-mid"))
            continue
        pct_away = round((sma350x2 - sma111) / sma350x2 * 100, 2)
        crossed = sma111 >= sma350x2
        if crossed:
            statuses.append((d, pct_away, "CROSSED", "st-strong-buy"))
        elif pct_away <= 5:
            statuses.append((d, pct_away, "APPROACHING", "st-near"))
        else:
            statuses.append((d, pct_away, "NOT CROSSED", "st-no"))
    return {
        "name": "Pi Cycle Top", "source": "BGeometrics (pi-cycle, startday/endday range)",
        "category": "onchain", "statuses": statuses,
    }


# ---------------------------------------------------------------------------
# Table 2 (Momentum Shift) — RSI Divergence, MACD Histogram Slope, MVRV
# Momentum. All three reuse ud's real detection functions
# (detect_rsi_divergence, detect_macd_histogram_fade, get_mvrv_momentum_ma)
# exactly as main() calls them (~3934-3992) -- see that block's own
# comments for why none of these double-count an existing Table 1/3 claim.
# ---------------------------------------------------------------------------
def build_rsi_divergence_daily(market_price_series, target_dates):
    """Walk-forward RSI Divergence: builds an "RSI__history" cache
    incrementally, day by day (ud.detect_rsi_divergence() reads dated RSI
    history directly out of the cache, not through status_pill(), so this
    can't reuse the generic walk_forward_statuses() helper the way
    threshold-comparison indicators do). For each date in target_dates,
    calls ud.detect_rsi_divergence() against a trailing PRICE_WINDOW_DAYS
    price window ending that date -- same depth main() itself uses for
    price_history_dated, and the function's own internal lookback_days=180
    slice narrows further from there, exactly as production does."""
    cache = {}
    target_set = set(target_dates)
    statuses = []
    for d, _ in market_price_series:
        rsi_window = _trailing_window(market_price_series, d, PRICE_WINDOW_DAYS)
        rsi_val = ud.compute_weekly_rsi(rsi_window)
        if rsi_val is not None:
            ud.history_append(cache, "RSI", float(rsi_val), date=d)
        if d not in target_set:
            continue
        price_window_dated = _trailing_window_dated(market_price_series, d, PRICE_WINDOW_DAYS)
        result = ud.detect_rsi_divergence(cache, price_window_dated)
        if result["available"]:
            p1, p2 = result["point1"], result["point2"]
            desc = f"{p1[0]} ${p1[1]:,.0f}/RSI{p1[2]:.1f} -> {p2[0]} ${p2[1]:,.0f}/RSI{p2[2]:.1f}"
            if result["detected"]:
                statuses.append((d, desc, "BEARISH DIVERGENCE", "st-buy"))
            else:
                statuses.append((d, desc, "NO DIVERGENCE", "st-no"))
        else:
            statuses.append((d, result["reason"], "BUILDING HISTORY", "st-mid"))
    return {
        "name": "RSI Divergence (bearish)", "source": "Blockchain.com market-price (340-day trailing window)",
        "category": "price", "statuses": statuses,
    }


def build_macd_slope_daily(market_price_series, target_dates):
    """MACD Histogram Slope ("Two-Bar Fade") is fully stateless -- no
    cache/history dependency, ud.detect_macd_histogram_fade() recomputes
    the weekly histogram fresh from whatever price window it's given each
    time -- so this only needs to evaluate target_dates, unlike the RSI
    Divergence walk above which must visit every day to build its cache."""
    target_set = set(target_dates)
    statuses = []
    for d, _ in market_price_series:
        if d not in target_set:
            continue
        window = _trailing_window(market_price_series, d, PRICE_WINDOW_DAYS)
        result = ud.detect_macd_histogram_fade(window)
        if result["available"]:
            desc = " -> ".join(f"{h:.2f}" for h in result["series"])
            if result["detected"]:
                statuses.append((d, desc, "TWO-BAR FADE", "st-buy"))
            else:
                statuses.append((d, desc, "NO FADE", "st-no"))
        else:
            statuses.append((d, result["reason"], "BUILDING HISTORY", "st-mid"))
    return {
        "name": "MACD Histogram Slope", "source": "Blockchain.com market-price (340-day trailing window)",
        "category": "price", "statuses": statuses,
    }


def build_mvrv_momentum_daily(mvrv_series):
    """MVRV Momentum: current MVRV Z-Score vs. its own trailing 365-day
    moving average (ud.get_mvrv_momentum_ma()), reading the same
    "MVRV_Z__history" cache shape Table 1/3's own MVRV_Z walk-forward
    builds -- fed the identical mvrv_series here, so the cache content is
    numerically identical to theirs (see build_percentile_indicator()'s own
    docstring for why a separate cache instance doesn't change the
    result). Below the n>=365 bootstrap gate, reports BUILDING HISTORY --
    same honest-accumulation-progress discipline as everywhere else."""
    cache = {}
    statuses = []
    for d, v in mvrv_series:
        ud.history_append(cache, "MVRV_Z", float(v), date=d)
        ma, n = ud.get_mvrv_momentum_ma(cache)
        if ma is not None:
            label, css = ud.status_pill(v, "low", ma, cache=cache, token="MVRV_Z")
            statuses.append((d, v, label, css))
        else:
            statuses.append((d, v, "BUILDING HISTORY", "st-mid"))
    return {
        "name": "MVRV Momentum (vs. 365d MA)", "source": "BGeometrics (mvrv-zscore) via ud.get_mvrv_momentum_ma()",
        "category": "onchain", "statuses": statuses,
    }


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------
def render_row(indicator, price_dict, last_price_date):
    statuses = indicator["statuses"]
    total_days = len(statuses)
    events = detect_firing_events(statuses)
    results, skipped = test_forward_returns(events, price_dict, last_price_date)
    return {
        "name": indicator["name"], "source": indicator["source"],
        "total_days": total_days, "n_events": len(events),
        "results": results, "skipped_no_entry_price": skipped,
    }


def print_table(rows, title):
    print(f"\n{'='*100}\n{title}\n{'='*100}")
    header = f"{'Indicator':<38}{'Days':>7}{'Events':>8}   {'90d':<26}{'180d':<26}{'365d':<26}"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(f"{r['name']:<38}{r['total_days']:>7}{r['n_events']:>8}   "
              f"{format_horizon(r['results'],90):<26}{format_horizon(r['results'],180):<26}{format_horizon(r['results'],365):<26}")
    print()
    print(f"{'Indicator':<38}{'Avg % chg 90d':>16}{'Avg % chg 180d':>17}{'Avg % chg 365d':>17}")
    print("-" * 88)
    for r in rows:
        print(f"{r['name']:<38}{format_avg_change(r['results'],90):>16}"
              f"{format_avg_change(r['results'],180):>17}{format_avg_change(r['results'],365):>17}")
    print(f"\nData source(s) per indicator:")
    for r in rows:
        print(f"  {r['name']}: {r['source']}")


def print_variant_row(label, events, price_dict, last_price_date):
    results, skipped = test_forward_returns(events, price_dict, last_price_date)
    print(f"\n  {label}")
    n_events = len(events)
    flag = "  [LOW-N, not reliable]" if n_events < 5 else ""
    skip_note = f"  (skipped {skipped}, no entry price found)" if skipped else ""
    print(f"    Events: {n_events}{flag}{skip_note}")
    for h in HORIZONS:
        print(f"    {h}d: {format_horizon(results, h):<32} avg {format_avg_change(results, h)}")
    return results, skipped


DAILY_MATRIX_OUT_DIR = "backtest_output"

TABLE1_COLUMNS = [
    ("MVRV_Z", "MVRV Z-Score"), ("PUELL", "Puell Multiple"), ("RESERVE_RISK", "Reserve Risk"),
    ("LTH_SOPR", "LTH-SOPR"), ("THERMOCAP", "Thermocap Multiple"), ("NRPL", "NRPL (Net Realized P&L)"),
    ("ASOPR_EST", "aSOPR (modeled)"), ("SUPPLY_LOSS", "% Supply in Loss"), ("REALIZED_PRICE", "Price vs. Realized Price"),
    ("ACTIVE_ADDR_DEV", "Active Addresses Power-Law Dev"), ("FNG", "Fear & Greed"), ("RSI", "Weekly RSI"),
    ("MACD", "MACD (weekly)"), ("BOLLINGER", "Bollinger %B"), ("MAYER", "Mayer Multiple"),
    ("DRAWDOWN", "Drawdown Magnitude"), ("NVT_GC", "NVT Golden Cross"), ("MINER_CAP", "Miner Capitulation"),
    ("PROD_COST", "Production Cost"),
]
TABLE2_COLUMNS = [
    ("RSI_DIVERGENCE", "RSI Divergence (bearish)"), ("MVRV_MOMENTUM", "MVRV Momentum (vs. 365d MA)"),
    ("MACD_SLOPE", "MACD Histogram Slope"),
]
TABLE3_COLUMNS = [
    ("PI_CYCLE_TOP", "Pi Cycle Top"), ("MVRV_Z_TOP", "MVRV Z-Score (Top)"), ("PUELL_TOP", "Puell Multiple (Top)"),
    ("THERMOCAP_TOP", "Thermocap Multiple (Top)"), ("RESERVE_RISK_TOP", "Reserve Risk (Top)"),
    ("RSI_TOP", "Weekly RSI (Top)"), ("BOLLINGER_TOP", "Bollinger %B (Top)"), ("MAYER_TOP", "Mayer Multiple (Top)"),
    ("FNG_TOP", "Fear & Greed (Top)"), ("NVT_GC_TOP", "NVT Golden Cross (Top)"),
]
# CYCLE_RHYTHM is deliberately excluded from every table below: it's pure
# calendar arithmetic against fixed anchor dates (see ud.CYCLE_ANCHORS),
# never routed through status_pill(), always displays "NOT A THRESHOLD"
# (ud.NO_SIGNAL_STATUS's own entry for it) and is explicitly excluded from
# WEIGHT_MAP everywhere in production -- it has no STRONG BUY/BUY/
# BORDERLINE/NOT YET tier to report because it structurally can't fire.
# Forcing it into this matrix's six-tier vocabulary would fabricate a
# status production itself never generates.

CANONICAL_TIERS = ("STRONG BUY", "BUY", "BORDERLINE", "NOT YET", "N/A (building history)", "STALE")
BUY_FAVORABLE_TIERS = {"STRONG BUY", "BUY", "BORDERLINE"}


def normalize_tier(label, css):
    """Collapses every indicator's own status_pill()/detector label+css
    pair down to the closed six-value vocabulary every cell in the daily
    matrix reports. CSS class is the ground truth for the tier -- it's the
    exact same st-strong-buy/st-buy/st-near/st-no grouping production's own
    buy-favorable bucket counting already keys off (BUY_FAVORABLE_CSS
    above); label text only ever matters to catch a STALE-prefixed label
    (STALE has no dedicated CSS class in production -- it's st-mid with a
    "STALE (date)"-prefixed label). STALE never actually fires in this
    backtest (see module docstring: this reconstructs each day from real
    historical API data, so there's no "today's live fetch failed, fall
    back to yesterday's cache" case to reproduce) -- kept here so the
    mapping stays complete/honest rather than silently impossible.
    css="st-watch" (Miner Capitulation's CAPITULATION/RECOVERING states)
    collapses to NOT YET: neither state is buy-favorable, and the closed
    six-tier vocabulary requested for this report has no room for a
    separate "watching" tier."""
    if isinstance(label, str) and label.startswith("STALE"):
        return "STALE"
    if css == "st-strong-buy":
        return "STRONG BUY"
    if css == "st-buy":
        return "BUY"
    if css == "st-near":
        return "BORDERLINE"
    if css in ("st-no", "st-watch"):
        return "NOT YET"
    return "N/A (building history)"


def build_daily_matrix(start_date, end_date, out_dir=DAILY_MATRIX_OUT_DIR):
    """Orchestrates the whole --daily-matrix mode: fetch every source once,
    build every indicator's walk-forward status series across all three
    tables (reusing the SAME builder functions the main backtest above
    uses -- Table 1's are literally the same calls; Table 3/Table 2 reuse
    the generalized/new builders added alongside this function), then
    render one row per day in [start_date, end_date] with one column per
    indicator, grouped by table. Writes both a CSV and a Markdown table to
    out_dir. Returns a summary dict for the caller to report on."""
    t_start = time.time()
    gaps_notes = []
    print(f"DAILY INDICATOR FIRING MATRIX -- {start_date.isoformat()} .. {end_date.isoformat()}")
    print(BG_KEY_MISSING_NOTE)
    print()

    # --- Fetch layer: BGeometrics (10 calls: the 9 BG_METRICS + pi-cycle) ---
    print("Fetching BGeometrics historical ranges (10 calls, ~4s apart -- 9 BG_METRICS + pi-cycle)...")
    bg_series = {}
    bg_tokens = list(ud.BG_METRICS.items())
    for i, (token, (slug, direction, threshold)) in enumerate(bg_tokens):
        series, err = fetch_bg_range(slug)
        bg_series[token] = series
        status = f"FAILED: {err}" if err else f"OK, n={len(series)}"
        print(f"  [{i+1}/10] {token} ({slug}): {status}")
        if err:
            gaps_notes.append(f"{token} ({slug}): {err}")
        time.sleep(4)
    pi_series, pi_err = fetch_pi_cycle_range()
    print(f"  [10/10] PI_CYCLE_TOP (pi-cycle): {'FAILED: ' + pi_err if pi_err else f'OK, n={len(pi_series)}'}")
    if pi_err:
        gaps_notes.append(f"PI_CYCLE_TOP (pi-cycle): {pi_err}")

    print("\nFetching Blockchain.com deep daily series (market-price, hash-rate, tx-volume, total-bitcoins)...")
    market_price_series = fetch_blockchain_chart_range("market-price", 2200)
    hashrate_series = fetch_blockchain_chart_range("hash-rate", 2200)
    tx_volume_series = fetch_blockchain_chart_range("estimated-transaction-volume-usd", 2200)
    total_bitcoins_series = fetch_blockchain_chart_range("total-bitcoins", 1600)
    print(f"  market-price: n={len(market_price_series)}, hash-rate: n={len(hashrate_series)}, "
          f"tx-volume: n={len(tx_volume_series)}, total-bitcoins: n={len(total_bitcoins_series)}")

    print("\nFetching Active Addresses full history + Fear & Greed full history...")
    aa_points = fetch_active_addresses_range()
    fng_series = fetch_fng_range()
    print(f"  addresses_active.json: n={len(aa_points)}, Alternative.me: n={len(fng_series)}")

    fetch_elapsed = time.time() - t_start
    print(f"\nAll fetches complete in {fetch_elapsed:.1f}s")

    target_dates = [start_date + timedelta(days=i) for i in range((end_date - start_date).days + 1)]

    # --- Build every indicator's walk-forward status series, register as a column ---
    columns = {}  # key -> {"name": str, "table": "CB"/"MS"/"CT", "lookup": {date: (label, css)}}

    def register(key, name, table, built):
        columns[key] = {
            "name": name, "table": table,
            "lookup": {d: (label, css) for d, v, label, css in built["statuses"]},
        }

    print("\nBuilding Table 1 (Cycle Bottom) indicator columns...")
    for token, name in (("MVRV_Z", "MVRV Z-Score"), ("PUELL", "Puell Multiple"),
                         ("RESERVE_RISK", "Reserve Risk"), ("LTH_SOPR", "LTH-SOPR")):
        slug, direction, threshold = ud.BG_METRICS[token]
        if bg_series.get(token):
            register(token, name, "CB", build_simple_bg_indicator(name, token, bg_series[token], direction, threshold))
        else:
            gaps_notes.append(f"{name}: no BGeometrics data this run -- column will show N/A for every day")
    if bg_series.get("THERMOCAP"):
        register("THERMOCAP", "Thermocap Multiple", "CB",
                  build_percentile_indicator("Thermocap Multiple", "THERMOCAP", bg_series["THERMOCAP"],
                                              "BGeometrics (thermocap-multiple)"))
    else:
        gaps_notes.append("Thermocap Multiple: no BGeometrics data this run -- column will show N/A for every day")
    if bg_series.get("NRPL"):
        register("NRPL", "NRPL (Net Realized P&L)", "CB",
                  build_percentile_indicator("NRPL (Net Realized P&L)", "NRPL", bg_series["NRPL"],
                                              "BGeometrics (nrpl-btc)"))
    else:
        gaps_notes.append("NRPL: no BGeometrics data this run -- column will show N/A for every day")
    if bg_series.get("SOPR"):
        register("ASOPR_EST", "aSOPR (modeled)", "CB", build_asopr_est(bg_series["SOPR"]))
    else:
        gaps_notes.append("aSOPR (modeled): no BGeometrics SOPR data this run -- column will show N/A for every day")
    if bg_series.get("SUPPLY_PROFIT") and total_bitcoins_series:
        register("SUPPLY_LOSS", "% Supply in Loss", "CB",
                  build_supply_loss(bg_series["SUPPLY_PROFIT"], total_bitcoins_series))
    else:
        gaps_notes.append("% Supply in Loss: missing supply-profit or total-bitcoins data -- column will show N/A for every day")
    if bg_series.get("REALIZED_PRICE") and market_price_series:
        register("REALIZED_PRICE", "Price vs. Realized Price", "CB",
                  build_realized_price(bg_series["REALIZED_PRICE"], market_price_series))
    else:
        gaps_notes.append("Price vs. Realized Price: missing realized-price or market-price data -- column will show N/A for every day")
    aa_dev_series = []
    if aa_points:
        aa_dev_built = build_active_addr_dev(aa_points)
        register("ACTIVE_ADDR_DEV", "Active Addresses Power-Law Dev", "CB", aa_dev_built)
        # Captured here (not recomputed) so the WEIGHT_MAP bootstrap-gate
        # check below can reuse these already-computed daily deviation
        # values instead of re-running the expensive day-by-day power-law
        # refit a second time.
        aa_dev_series = [(d, v) for d, v, label, css in aa_dev_built["statuses"]]
    else:
        gaps_notes.append("Active Addresses Power-Law Dev: no addresses_active.json data -- column will show N/A for every day")
    if fng_series:
        register("FNG", "Fear & Greed", "CB", build_fng(fng_series))
    else:
        gaps_notes.append("Fear & Greed: no Alternative.me data -- column will show N/A for every day")
    if market_price_series:
        register("RSI", "Weekly RSI", "CB",
                  build_price_derived("Weekly RSI", "RSI", market_price_series, "low", 30.0, ud.compute_weekly_rsi))
        register("MACD", "MACD (weekly)", "CB",
                  build_price_derived("MACD (weekly)", "MACD", market_price_series, "high", 0.0,
                                       lambda w: ud.compute_weekly_macd(w)[0]))
        register("BOLLINGER", "Bollinger %B", "CB",
                  build_price_derived("Bollinger %B", "BOLLINGER", market_price_series, "low", 0.2, ud.compute_bollinger))
        register("MAYER", "Mayer Multiple", "CB",
                  build_price_derived("Mayer Multiple", "MAYER", market_price_series, "low", 1.0,
                                       lambda w: ud.compute_mayer_and_drawdown(w)[0]))
        register("DRAWDOWN", "Drawdown Magnitude", "CB",
                  build_price_derived("Drawdown Magnitude", "DRAWDOWN", market_price_series, "low", -77.0,
                                       lambda w: ud.compute_mayer_and_drawdown(w)[1]))
    else:
        gaps_notes.append("RSI/MACD/Bollinger/Mayer/Drawdown: no market-price data -- columns will show N/A for every day")
    if market_price_series and tx_volume_series:
        register("NVT_GC", "NVT Golden Cross", "CB", build_nvt_gc(market_price_series, tx_volume_series))
    else:
        gaps_notes.append("NVT Golden Cross: missing market-price or tx-volume data -- column will show N/A for every day")
    if hashrate_series and market_price_series:
        register("MINER_CAP", "Miner Capitulation", "CB", build_miner_capitulation(hashrate_series, market_price_series))
        register("PROD_COST", "Production Cost", "CB", build_production_cost(hashrate_series, market_price_series))
    else:
        gaps_notes.append("Miner Capitulation/Production Cost: missing hash-rate or market-price data -- columns will show N/A for every day")

    print("Building Table 3 (Cycle Top) indicator columns...")
    for token, name in (("MVRV_Z", "MVRV Z-Score (Top)"), ("PUELL", "Puell Multiple (Top)"),
                         ("THERMOCAP", "Thermocap Multiple (Top)")):
        if bg_series.get(token):
            register(f"{token}_TOP", name, "CT",
                      build_percentile_indicator(name, token, bg_series[token],
                                                  f"BGeometrics ({ud.BG_METRICS[token][0]}, top-decile)", direction="high"))
        else:
            gaps_notes.append(f"{name}: no BGeometrics data this run -- column will show N/A for every day")
    if bg_series.get("RESERVE_RISK"):
        register("RESERVE_RISK_TOP", "Reserve Risk (Top)", "CT",
                  build_simple_bg_indicator("Reserve Risk (Top)", "RESERVE_RISK", bg_series["RESERVE_RISK"], "high", 0.02))
    else:
        gaps_notes.append("Reserve Risk (Top): no BGeometrics data this run -- column will show N/A for every day")
    if fng_series:
        register("FNG_TOP", "Fear & Greed (Top)", "CT",
                  build_simple_bg_indicator("Fear & Greed (Top)", "FNG", fng_series, "high", 75.0,
                                             source="Alternative.me (full history, limit=0)"))
    else:
        gaps_notes.append("Fear & Greed (Top): no Alternative.me data -- column will show N/A for every day")
    if market_price_series:
        register("RSI_TOP", "Weekly RSI (Top)", "CT",
                  build_price_derived("Weekly RSI (Top)", "RSI", market_price_series, "high", 80.0, ud.compute_weekly_rsi))
        register("BOLLINGER_TOP", "Bollinger %B (Top)", "CT",
                  build_price_derived("Bollinger %B (Top)", "BOLLINGER", market_price_series, "high", 0.8, ud.compute_bollinger))
        register("MAYER_TOP", "Mayer Multiple (Top)", "CT",
                  build_price_derived("Mayer Multiple (Top)", "MAYER", market_price_series, "high", 2.4,
                                       lambda w: ud.compute_mayer_and_drawdown(w)[0]))
    else:
        gaps_notes.append("RSI/Bollinger/Mayer (Top): no market-price data -- columns will show N/A for every day")
    if market_price_series and tx_volume_series:
        register("NVT_GC_TOP", "NVT Golden Cross (Top)", "CT", build_nvt_gc_top(market_price_series, tx_volume_series))
    else:
        gaps_notes.append("NVT Golden Cross (Top): missing market-price or tx-volume data -- column will show N/A for every day")
    if pi_series:
        register("PI_CYCLE_TOP", "Pi Cycle Top", "CT", build_pi_cycle_top(pi_series))
    else:
        gaps_notes.append("Pi Cycle Top: no pi-cycle data this run -- column will show N/A for every day")

    print("Building Table 2 (Momentum Shift) indicator columns...")
    if market_price_series:
        register("RSI_DIVERGENCE", "RSI Divergence (bearish)", "MS",
                  build_rsi_divergence_daily(market_price_series, target_dates))
        register("MACD_SLOPE", "MACD Histogram Slope", "MS",
                  build_macd_slope_daily(market_price_series, target_dates))
    else:
        gaps_notes.append("RSI Divergence/MACD Histogram Slope: no market-price data -- columns will show N/A for every day")
    if bg_series.get("MVRV_Z"):
        register("MVRV_MOMENTUM", "MVRV Momentum (vs. 365d MA)", "MS", build_mvrv_momentum_daily(bg_series["MVRV_Z"]))
    else:
        gaps_notes.append("MVRV Momentum: no BGeometrics MVRV Z-Score data this run -- column will show N/A for every day")

    build_elapsed = time.time() - t_start - fetch_elapsed
    print(f"Indicator reconstruction complete in {build_elapsed:.1f}s")

    # --- Walk-forward weighted verdict percentages (one per table, per day) ---
    # Reuses ud.build_verdict()/build_verdict_top()/build_verdict_momentum()
    # UNCHANGED -- the exact functions that produce VERDICT_PCT/_TOP/_MOM
    # live -- rather than reimplementing the weighted-tally math here. Those
    # functions read two module-level globals: `values` (a dict of
    # "{token}_STATUS_CLASS" readings, which this file already has for
    # every day via `columns`) and ud.WEIGHT_MAP/WEIGHT_MAP_TOP/
    # WEIGHT_MAP_MOMENTUM (mutable module globals main() itself
    # conditionally grows at runtime, e.g. `WEIGHT_MAP[token] = 1.5` once
    # Thermocap/NRPL/Active Addresses bootstrap). To reproduce a genuine
    # AS-OF-THAT-DAY weight pool -- not today's already-fully-bootstrapped
    # one -- this temporarily mutates those same globals to each day's
    # correct membership, calls the real verdict function, then moves on.
    # This is the same mechanism production itself uses (a plain dict
    # mutation), just replayed day-by-day instead of once per run.
    print("\nComputing walk-forward weighted verdict percentages (reusing ud.build_verdict()/_top()/_momentum())...")

    def _bootstrap_gate_by_day(series, token, gate_fn):
        """Walks `series` day by day into a fresh rolling-history cache via
        ud.history_append() (identical mechanism to every walk-forward
        builder above), calling the REAL gate function after each append --
        ud.rolling_threshold (MIN_HISTORY_DAYS=90 gate: Table 1's
        Thermocap/NRPL/Active-Addr-Dev and Table 3's MVRV Z/Puell/
        Thermocap-Top joins) or ud.get_mvrv_momentum_ma (365-day gate:
        Table 2's MVRV Momentum join). Returns {date: bool} -- whether the
        SAME gate main() checks before conditionally adding `token` to a
        WEIGHT_MAP* dict was satisfied as of that day."""
        cache = {}
        gated = {}
        for d, v in series:
            ud.history_append(cache, token, float(v), date=d)
            result, n = gate_fn(cache, token)
            gated[d] = result is not None
        return gated

    thermocap_gate = _bootstrap_gate_by_day(bg_series["THERMOCAP"], "THERMOCAP", ud.rolling_threshold) \
        if bg_series.get("THERMOCAP") else {}
    nrpl_gate = _bootstrap_gate_by_day(bg_series["NRPL"], "NRPL", ud.rolling_threshold) \
        if bg_series.get("NRPL") else {}
    aa_gate = _bootstrap_gate_by_day(aa_dev_series, "ACTIVE_ADDR_DEV", ud.rolling_threshold) \
        if aa_dev_series else {}
    mvrv_top_gate = _bootstrap_gate_by_day(bg_series["MVRV_Z"], "MVRV_Z", ud.rolling_threshold) \
        if bg_series.get("MVRV_Z") else {}
    puell_top_gate = _bootstrap_gate_by_day(bg_series["PUELL"], "PUELL", ud.rolling_threshold) \
        if bg_series.get("PUELL") else {}
    mvrv_momentum_gate = _bootstrap_gate_by_day(bg_series["MVRV_Z"], "MVRV_Z", lambda c, t: ud.get_mvrv_momentum_ma(c)) \
        if bg_series.get("MVRV_Z") else {}

    # Static (module-load) baseline -- since this script never calls
    # ud.main(), these three globals are still exactly what they were at
    # import time (no dynamic joins have happened yet in this process).
    # Snapshotted once, before any mutation below, and restored at the end.
    base_weight_map = dict(ud.WEIGHT_MAP)
    base_weight_map_top = dict(ud.WEIGHT_MAP_TOP)
    base_weight_map_momentum = dict(ud.WEIGHT_MAP_MOMENTUM)

    # Table 3's own bare-token WEIGHT_MAP_TOP keys (MVRV_Z, PUELL,
    # THERMOCAP, RESERVE_RISK, RSI, BOLLINGER, MAYER, FNG, NVT_GC) map to
    # this file's own "_TOP"-suffixed column keys.
    TOP_KEY_MAP = {"RESERVE_RISK": "RESERVE_RISK_TOP", "NVT_GC": "NVT_GC_TOP", "RSI": "RSI_TOP",
                   "BOLLINGER": "BOLLINGER_TOP", "MAYER": "MAYER_TOP", "FNG": "FNG_TOP",
                   "MVRV_Z": "MVRV_Z_TOP", "PUELL": "PUELL_TOP", "THERMOCAP": "THERMOCAP_TOP"}

    def _css_for(key, d):
        col = columns.get(key)
        entry = col["lookup"].get(d) if col else None
        return entry[1] if entry else None

    weighted_pct = {}  # date -> {"CB": pct_or_None, "MS": pct_or_None, "CT": pct_or_None}
    weight_pool_history = {"CB": {}, "MS": {}, "CT": {}}  # date -> total_weight, for the stability check
    for d in target_dates:
        ud.WEIGHT_MAP.clear()
        ud.WEIGHT_MAP.update(base_weight_map)
        if thermocap_gate.get(d):
            ud.WEIGHT_MAP["THERMOCAP"] = 1.5
        if nrpl_gate.get(d):
            ud.WEIGHT_MAP["NRPL"] = 1.5
        if aa_gate.get(d):
            ud.WEIGHT_MAP["ACTIVE_ADDR_DEV"] = 1.5
        values_cb = {f"{tok}_STATUS_CLASS": _css_for(tok, d) for tok, _ in TABLE1_COLUMNS}
        weighted_pct.setdefault(d, {})["CB"] = ud.build_verdict(values_cb)["VERDICT_PCT"]
        weight_pool_history["CB"][d] = sum(ud.WEIGHT_MAP.values())

        ud.WEIGHT_MAP_TOP.clear()
        ud.WEIGHT_MAP_TOP.update(base_weight_map_top)
        if mvrv_top_gate.get(d):
            ud.WEIGHT_MAP_TOP["MVRV_Z"] = 1.5
        if puell_top_gate.get(d):
            ud.WEIGHT_MAP_TOP["PUELL"] = 1.5
        if thermocap_gate.get(d):
            ud.WEIGHT_MAP_TOP["THERMOCAP"] = 1.0
        values_ct = {f"{tok}_TOP_STATUS_CLASS": _css_for(TOP_KEY_MAP.get(tok, tok), d) for tok in ud.WEIGHT_MAP_TOP}
        weighted_pct[d]["CT"] = ud.build_verdict_top(values_ct)["VERDICT_PCT_TOP"]
        weight_pool_history["CT"][d] = sum(ud.WEIGHT_MAP_TOP.values())

        ud.WEIGHT_MAP_MOMENTUM.clear()
        ud.WEIGHT_MAP_MOMENTUM.update(base_weight_map_momentum)
        if mvrv_momentum_gate.get(d):
            ud.WEIGHT_MAP_MOMENTUM["MVRV_MOMENTUM"] = 1.5
        values_ms = {f"{tok}_MOM_STATUS_CLASS": _css_for(tok, d) for tok in ud.WEIGHT_MAP_MOMENTUM}
        weighted_pct[d]["MS"] = ud.build_verdict_momentum(values_ms)["VERDICT_PCT_MOM"]
        weight_pool_history["MS"][d] = sum(ud.WEIGHT_MAP_MOMENTUM.values())

    # Restore the pristine static baseline -- leave no trace of this walk
    # in the module globals for anything else that might import ud later
    # in the same process.
    ud.WEIGHT_MAP.clear()
    ud.WEIGHT_MAP.update(base_weight_map)
    ud.WEIGHT_MAP_TOP.clear()
    ud.WEIGHT_MAP_TOP.update(base_weight_map_top)
    ud.WEIGHT_MAP_MOMENTUM.clear()
    ud.WEIGHT_MAP_MOMENTUM.update(base_weight_map_momentum)

    weight_pool_stability = {}
    for table in ("CB", "MS", "CT"):
        totals = [weight_pool_history[table][d] for d in target_dates]
        changed_on = [d for i, d in enumerate(target_dates) if i > 0 and totals[i] != totals[i - 1]]
        weight_pool_stability[table] = {
            "stable": len(changed_on) == 0, "min_total": min(totals), "max_total": max(totals),
            "changed_on": changed_on,
        }
        stability_note = "STABLE all month" if weight_pool_stability[table]["stable"] else \
            f"CHANGED on {[d.isoformat() for d in changed_on]} (total weight ranged {min(totals)}-{max(totals)})"
        print(f"  {table} weight pool: {stability_note}")

    # --- Assemble the matrix ---
    all_columns = [(k, n, "CB") for k, n in TABLE1_COLUMNS] + \
                  [(k, n, "MS") for k, n in TABLE2_COLUMNS] + \
                  [(k, n, "CT") for k, n in TABLE3_COLUMNS]

    rows = []  # (date, {key: tier}, cb_count, ms_count, ct_count, total_count, {table: weighted_pct})
    for d in target_dates:
        tiers = {}
        cb_count = ms_count = ct_count = 0
        for key, name, table in all_columns:
            col = columns.get(key)
            entry = col["lookup"].get(d) if col else None
            label, css = entry if entry else (None, None)
            tier = normalize_tier(label, css)
            tiers[key] = tier
            if tier in BUY_FAVORABLE_TIERS:
                if table == "CB":
                    cb_count += 1
                elif table == "MS":
                    ms_count += 1
                elif table == "CT":
                    ct_count += 1
        rows.append((d, tiers, cb_count, ms_count, ct_count, cb_count + ms_count + ct_count, weighted_pct[d]))

    # Per-column totals (bottom summary row): days buy-favorable out of len(target_dates)
    column_totals = {
        key: sum(1 for _, tiers, *_ in rows if tiers[key] in BUY_FAVORABLE_TIERS)
        for key, name, table in all_columns
    }

    os.makedirs(out_dir, exist_ok=True)
    month_tag = f"{start_date.isoformat()}_to_{end_date.isoformat()}"
    csv_path = os.path.join(out_dir, f"daily_indicator_matrix_{month_tag}.csv")
    md_path = os.path.join(out_dir, f"daily_indicator_matrix_{month_tag}.md")

    _write_daily_matrix_csv(csv_path, rows, column_totals, all_columns, len(target_dates))
    _write_daily_matrix_md(md_path, rows, column_totals, all_columns, len(target_dates), start_date, end_date, gaps_notes)

    # --- Notable-pattern stats for the report ---
    max_total_row = max(rows, key=lambda r: r[5])
    both_cb_ct_days = [r[0] for r in rows if r[2] > 0 and r[4] > 0]
    total_elapsed = time.time() - t_start
    print(f"\n{'='*100}")
    print(f"DAILY MATRIX COMPLETE -- {len(target_dates)} days x {len(all_columns)} indicator columns")
    print(f"Wrote: {csv_path}")
    print(f"Wrote: {md_path}")
    print(f"Highest single-day confluence: {max_total_row[0]} ({max_total_row[5]} of {len(all_columns)} indicators buy-favorable "
          f"-- CB={max_total_row[2]}, MS={max_total_row[3]}, CT={max_total_row[4]})")
    print(f"Days with BOTH Cycle Bottom AND Cycle Top indicators firing simultaneously: {len(both_cb_ct_days)}"
          + (f" ({', '.join(d.isoformat() for d in both_cb_ct_days)})" if both_cb_ct_days else ""))
    if gaps_notes:
        print(f"Data gaps encountered ({len(gaps_notes)}):")
        for note in gaps_notes:
            print(f"  ! {note}")
    else:
        print("No data gaps or rate-limit issues encountered.")
    for table, table_name in (("CB", "Cycle Bottom"), ("MS", "Momentum Shift"), ("CT", "Cycle Top")):
        st = weight_pool_stability[table]
        note = "STABLE all month" if st["stable"] else f"CHANGED on {[d.isoformat() for d in st['changed_on']]}"
        print(f"{table_name} weight pool total: {note} (weight ranged {st['min_total']}-{st['max_total']} across the month)")
    print(f"TOTAL RUNTIME: {total_elapsed:.1f}s")
    print(f"{'='*100}")

    return {
        "csv_path": csv_path, "md_path": md_path, "total_elapsed": total_elapsed,
        "gaps_notes": gaps_notes, "rows": rows, "column_totals": column_totals,
        "all_columns": all_columns, "max_total_row": max_total_row, "both_cb_ct_days": both_cb_ct_days,
        "weight_pool_stability": weight_pool_stability,
    }


def _fmt_pct(pct):
    return f"{pct}%" if isinstance(pct, (int, float)) else "N/A"


# Extra summary columns appended after the indicator columns: the three
# real walk-forward weighted percentages (reusing ud.build_verdict()/
# _top()/_momentum() unchanged -- see the "Walk-forward weighted verdict
# percentages" block in build_daily_matrix()), then the existing
# buy-favorable day counts.
SUMMARY_COLUMN_NAMES = ["Cycle Bottom Weighted %", "Momentum Shift Weighted %", "Cycle Top Weighted %",
                        "Cycle Bottom Favorable", "Momentum Shift Favorable", "Cycle Top Favorable", "Total Favorable"]


def _write_daily_matrix_csv(path, rows, column_totals, all_columns, n_days):
    table_group_name = {"CB": "CYCLE BOTTOM", "MS": "MOMENTUM SHIFT", "CT": "CYCLE TOP"}
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        # Row 1: table-group super-header (blank except the first column of
        # each group) -- padded to exactly match row 2's length so every
        # row in the file has the same cell count.
        group_row = ["", ""]
        prev_table = None
        for key, name, table in all_columns:
            group_row.append(table_group_name[table] if table != prev_table else "")
            prev_table = table
        group_row.append("WEIGHTED % / SUMMARY")
        group_row += [""] * (len(SUMMARY_COLUMN_NAMES) - 1)
        # Row 2: column names
        header = ["Date", "Weekday"] + [name for key, name, table in all_columns] + SUMMARY_COLUMN_NAMES
        assert len(group_row) == len(header), f"header row length mismatch: {len(group_row)} vs {len(header)}"
        w.writerow(group_row)
        w.writerow(header)
        for d, tiers, cb, ms, ct, total, pct in rows:
            w.writerow([d.isoformat(), d.strftime("%a")] + [tiers[key] for key, name, table in all_columns] +
                       [_fmt_pct(pct["CB"]), _fmt_pct(pct["MS"]), _fmt_pct(pct["CT"]), cb, ms, ct, total])

        def _avg_pct(table):
            vals = [r[6][table] for r in rows if isinstance(r[6][table], (int, float))]
            return round(sum(vals) / len(vals), 1) if vals else "N/A"

        totals_row = ["TOTAL DAYS FAVORABLE", f"(of {n_days})"] + \
                     [column_totals[key] for key, name, table in all_columns] + \
                     [_avg_pct("CB"), _avg_pct("MS"), _avg_pct("CT"), "", "", "", sum(r[5] for r in rows)]
        w.writerow(totals_row)


def _write_daily_matrix_md(path, rows, column_totals, all_columns, n_days, start_date, end_date, gaps_notes):
    table_group_name = {"CB": "Cycle Bottom", "MS": "Momentum Shift", "CT": "Cycle Top"}
    lines = []
    lines.append(f"# Daily Indicator Firing Matrix — {start_date.isoformat()} to {end_date.isoformat()}")
    lines.append("")
    lines.append(f"One row per day, one column per indicator across all three dashboard tables. Cell values are the "
                  f"indicator's real tier that day, reconstructed walk-forward from `update_dashboard.py`'s own "
                  f"functions (see `backtest_indicators.py` module docstring for the no-look-ahead methodology).")
    lines.append("")
    lines.append("**Tier vocabulary:** `STRONG BUY` / `BUY` / `BORDERLINE` (all three count toward the favorable "
                  "columns) / `NOT YET` / `N/A (building history)` (covers both a genuine bootstrap gate not yet met "
                  "AND a same-day data gap -- this backtest has no live-cache-fallback concept to distinguish them) "
                  "/ `STALE` (never occurs in this backtest -- see report notes).")
    lines.append("")
    if gaps_notes:
        lines.append(f"**Data gaps this run ({len(gaps_notes)}):**")
        for note in gaps_notes:
            lines.append(f"- {note}")
        lines.append("")
    lines.append(f"Columns are grouped and separated by a `‖` divider: **Cycle Bottom** ({sum(1 for _,_,t in all_columns if t=='CB')} indicators) "
                  f"‖ **Momentum Shift** ({sum(1 for _,_,t in all_columns if t=='MS')} indicators) "
                  f"‖ **Cycle Top** ({sum(1 for _,_,t in all_columns if t=='CT')} indicators).")
    lines.append("")

    # Build header with divider columns inserted between table groups
    header_cells = ["Date"]
    prefix = {"CB": "CB", "MS": "MS", "CT": "CT"}
    prev_table = None
    col_render_order = []  # list of ("col", key) or ("div", table)
    for key, name, table in all_columns:
        if prev_table is not None and table != prev_table:
            header_cells.append("‖")
            col_render_order.append(("div", None))
        header_cells.append(f"{prefix[table]} · {name}")
        col_render_order.append(("col", key))
        prev_table = table
    header_cells.append("‖")
    col_render_order.append(("div", None))
    header_cells += ["CB Weighted %", "MS Weighted %", "CT Weighted %", "CB Fav.", "MS Fav.", "CT Fav.", "**Total Fav.**"]

    lines.append("| " + " | ".join(header_cells) + " |")
    lines.append("|" + "|".join(["---"] * len(header_cells)) + "|")

    for d, tiers, cb, ms, ct, total, pct in rows:
        cells = [f"{d.isoformat()} ({d.strftime('%a')})"]
        for kind, key in col_render_order:
            cells.append("‖" if kind == "div" else tiers[key])
        cells += [_fmt_pct(pct["CB"]), _fmt_pct(pct["MS"]), _fmt_pct(pct["CT"]), str(cb), str(ms), str(ct), f"**{total}**"]
        lines.append("| " + " | ".join(cells) + " |")

    def _avg_pct(table):
        vals = [r[6][table] for r in rows if isinstance(r[6][table], (int, float))]
        return f"avg {round(sum(vals) / len(vals), 1)}%" if vals else "N/A"

    totals_cells = [f"**TOTAL (of {n_days} days)**"]
    for kind, key in col_render_order:
        totals_cells.append("‖" if kind == "div" else str(column_totals[key]))
    totals_cells += [_avg_pct("CB"), _avg_pct("MS"), _avg_pct("CT"),
                      "", "", "", f"**{sum(r[5] for r in rows)}**"]
    lines.append("| " + " | ".join(totals_cells) + " |")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def run_miner_capitulation_variant_analysis():
    """Standalone (not part of main()) analysis answering: is Miner
    Capitulation's RECOVERING state a viable earlier entry point than
    waiting for the full Edwards BUY SIGNAL? Reuses
    ud.compute_miner_capitulation() directly (via
    _build_miner_capitulation_dated_raw()) and the same walk-forward,
    no-look-ahead, streak-detection, and forward-return-testing machinery
    every other backtest in this file uses -- see module docstring.

    Read-only analysis. Does not touch WEIGHT_MAP, status_pill, or
    mc_label_map."""
    t_start = time.time()
    print("MINER CAPITULATION -- RECOVERING vs. BUY SIGNAL entry-point analysis")
    print("Reuses ud.compute_miner_capitulation() directly; no reimplementation of the state machine.\n")

    print("Fetching Blockchain.com deep daily series (market-price, hash-rate)...")
    market_price_series = fetch_blockchain_chart_range("market-price", 2200)
    hashrate_series = fetch_blockchain_chart_range("hash-rate", 2200)
    print(f"  market-price: n={len(market_price_series)}")
    print(f"  hash-rate: n={len(hashrate_series)}")

    dated_raw = _build_miner_capitulation_dated_raw(hashrate_series, market_price_series)
    print(f"  testable days (60d hash-rate + 20d price warm-up satisfied): {len(dated_raw)}")

    price_dict = to_dict(market_price_series)
    last_price_date = max(price_dict)

    # --- TEST 1: sanity-check Variant 1 still reproduces the confirmed 52-event baseline
    baseline_indicator = build_miner_capitulation(hashrate_series, market_price_series)
    variant1_events = detect_firing_events(baseline_indicator["statuses"])
    print(f"\n{'-'*100}\n[TEST 1] Variant 1 (STRICT) baseline reproduction\n{'-'*100}")
    match_note = "MATCHES" if len(variant1_events) == 52 else "DOES NOT MATCH"
    print(f"  Variant 1 (BUY SIGNAL only): {len(variant1_events)} events -- {match_note} the confirmed 52-event baseline")
    if len(variant1_events) != 52:
        print("  ! INVESTIGATE before trusting variants 2/3 -- event count drifted from the confirmed baseline.")

    # --- TEST 3: synthetic streak-detection correctness
    _test_streak_detection()

    # --- TEST 2: concrete no-look-ahead proof on real data
    _demonstrate_no_look_ahead(hashrate_series, market_price_series, dated_raw)

    # --- Streak extraction for variants 2 & 3
    streaks = extract_combined_streaks(dated_raw)
    classification = classify_streaks(streaks)
    variant2_events = classification["variant2_events"]
    variant3_events = classification["variant3_events"]

    print(f"\n{'='*100}\nMINER CAPITULATION: RECOVERING vs. BUY SIGNAL -- THREE-VARIANT COMPARISON\n{'='*100}")
    print_variant_row("VARIANT 1 -- STRICT (existing baseline: only full BUY SIGNAL fires, "
                       "entry on the BUY SIGNAL day)", variant1_events, price_dict, last_price_date)
    print_variant_row("VARIANT 2 -- RECOVERING-INCLUSIVE (entry on the FIRST day of RECOVERING or "
                       "BUY SIGNAL; a RECOVERING->BUY SIGNAL transition is one continuous event)",
                       variant2_events, price_dict, last_price_date)
    print_variant_row("VARIANT 3 -- RECOVERING-ONLY, ISOLATED (false starts: RECOVERING events that "
                       "reverted to CAPITULATION without ever reaching BUY SIGNAL)",
                       variant3_events, price_dict, last_price_date)

    # --- Conversion-rate / time-to-confirmation metrics
    n_recovering = classification["recovering_streaks_total"]
    n_converted = len(classification["converted"])
    n_isolated = len(classification["isolated"])
    n_ongoing = len(classification["ongoing"])
    print(f"\n{'-'*100}\nRECOVERING -> BUY SIGNAL CONVERSION RATE\n{'-'*100}")
    print(f"  Total RECOVERING-started streaks: {n_recovering}")
    if n_recovering:
        print(f"  Converted to BUY SIGNAL:  {n_converted}/{n_recovering} ({round(n_converted/n_recovering*100,1)}%)")
        print(f"  False starts (Variant 3): {n_isolated}/{n_recovering} ({round(n_isolated/n_recovering*100,1)}%)")
    else:
        print("  No RECOVERING-started streaks found.")
    if n_ongoing:
        print(f"  Still open/unresolved at end of dataset (excluded from the rates above): {n_ongoing}")
    print(f"  Direct-to-BUY-SIGNAL streaks (no RECOVERING precursor day at all): {classification['direct_buy_streaks']}")

    if n_converted:
        days = [c["days_to_confirm"] for c in classification["converted"]]
        days_sorted = sorted(days)
        print(f"\n  Days from RECOVERING entry to first BUY SIGNAL confirmation (n={len(days)}):")
        print(f"    mean={sum(days)/len(days):.1f}   median={median(days)}   min={days_sorted[0]}   max={days_sorted[-1]}")
        print(f"    full distribution (sorted, days): {days_sorted}")
    else:
        print("\n  No converted RECOVERING streaks -- no time-to-confirmation distribution available.")

    total_elapsed = time.time() - t_start
    print(f"\n{'='*100}")
    print(f"TOTAL RUNTIME: {total_elapsed:.1f}s")
    print(f"{'='*100}")

    return {
        "variant1_events": variant1_events, "variant2_events": variant2_events,
        "variant3_events": variant3_events, "classification": classification,
        "dated_raw": dated_raw,
    }


def main():
    t_start = time.time()
    print(BG_KEY_MISSING_NOTE)
    print()

    # -----------------------------------------------------------------
    # 1. BGeometrics on-chain fetches -- exactly 9 calls, paced, sourced
    #    from ud.BG_METRICS so slugs/directions/thresholds never drift
    #    from the live dashboard's own definitions.
    # -----------------------------------------------------------------
    print("Fetching BGeometrics historical ranges (9 calls, ~4s apart)...")
    bg_series = {}
    bg_fetch_notes = {}
    bg_tokens = list(ud.BG_METRICS.items())
    for i, (token, (slug, direction, threshold)) in enumerate(bg_tokens):
        series, err = fetch_bg_range(slug)
        bg_series[token] = series
        bg_fetch_notes[token] = err
        status = f"FAILED: {err}" if err else f"OK, n={len(series)}"
        print(f"  [{i+1}/9] {token} ({slug}): {status}")
        if i < len(bg_tokens) - 1:
            time.sleep(4)

    # -----------------------------------------------------------------
    # 2. Blockchain.com deep daily series -- confirmed in Step 1/follow-
    #    ups to hold true daily granularity up to ~2,200-2,250 days on a
    #    single unchunked call. 2,200 days reaches back to ~2020-02,
    #    comfortably before the BGeometrics window starts (2022-08-06)
    #    and before Active Addresses/F&G's own ranges begin.
    # -----------------------------------------------------------------
    print("\nFetching Blockchain.com deep daily series (market-price, hash-rate, tx-volume)...")
    market_price_series = fetch_blockchain_chart_range("market-price", 2200)
    print(f"  market-price: n={len(market_price_series)}")
    hashrate_series = fetch_blockchain_chart_range("hash-rate", 2200)
    print(f"  hash-rate: n={len(hashrate_series)}")
    tx_volume_series = fetch_blockchain_chart_range("estimated-transaction-volume-usd", 2200)
    print(f"  estimated-transaction-volume-usd: n={len(tx_volume_series)}")
    total_bitcoins_series = fetch_blockchain_chart_range("total-bitcoins", 1600)
    print(f"  total-bitcoins: n={len(total_bitcoins_series)}")

    print("\nFetching Active Addresses full history (unmetered chart file)...")
    aa_points = fetch_active_addresses_range()
    print(f"  addresses_active.json: n={len(aa_points)}")

    print("\nFetching Fear & Greed full history...")
    fng_series = fetch_fng_range()
    print(f"  Alternative.me: n={len(fng_series)}")

    fetch_elapsed = time.time() - t_start
    print(f"\nAll fetches complete in {fetch_elapsed:.1f}s")

    # -----------------------------------------------------------------
    # 3. Build every indicator's walk-forward status series
    # -----------------------------------------------------------------
    print("\nReconstructing indicator series (real update_dashboard.py functions, walk-forward)...")
    indicators = []

    for token, name in (("MVRV_Z", "MVRV Z-Score"), ("PUELL", "Puell Multiple"),
                         ("RESERVE_RISK", "Reserve Risk"), ("LTH_SOPR", "LTH-SOPR")):
        slug, direction, threshold = ud.BG_METRICS[token]
        if bg_series.get(token):
            indicators.append(build_simple_bg_indicator(name, token, bg_series[token], direction, threshold))

    if bg_series.get("THERMOCAP"):
        indicators.append(build_percentile_indicator("Thermocap Multiple", "THERMOCAP", bg_series["THERMOCAP"],
                                                       "BGeometrics (thermocap-multiple)"))
    if bg_series.get("NRPL"):
        indicators.append(build_percentile_indicator("NRPL (Net Realized P&L)", "NRPL", bg_series["NRPL"],
                                                       "BGeometrics (nrpl-btc)"))
    if bg_series.get("SOPR"):
        indicators.append(build_asopr_est(bg_series["SOPR"]))
    if bg_series.get("SUPPLY_PROFIT") and total_bitcoins_series:
        indicators.append(build_supply_loss(bg_series["SUPPLY_PROFIT"], total_bitcoins_series))
    if bg_series.get("REALIZED_PRICE") and market_price_series:
        indicators.append(build_realized_price(bg_series["REALIZED_PRICE"], market_price_series))

    if aa_points:
        indicators.append(build_active_addr_dev(aa_points))
    if fng_series:
        indicators.append(build_fng(fng_series))

    if market_price_series:
        indicators.append(build_price_derived("Weekly RSI", "RSI", market_price_series, "low", 30.0,
                                                ud.compute_weekly_rsi))
        indicators.append(build_price_derived("MACD (weekly)", "MACD", market_price_series, "high", 0.0,
                                                lambda w: ud.compute_weekly_macd(w)[0]))
        indicators.append(build_price_derived("Bollinger %B", "BOLLINGER", market_price_series, "low", 0.2,
                                                ud.compute_bollinger))
        indicators.append(build_price_derived("Mayer Multiple", "MAYER", market_price_series, "low", 1.0,
                                                lambda w: ud.compute_mayer_and_drawdown(w)[0]))
        indicators.append(build_price_derived("Drawdown Magnitude", "DRAWDOWN", market_price_series, "low", -77.0,
                                                lambda w: ud.compute_mayer_and_drawdown(w)[1]))

    if market_price_series and tx_volume_series:
        indicators.append(build_nvt_gc(market_price_series, tx_volume_series))
    if hashrate_series and market_price_series:
        indicators.append(build_miner_capitulation(hashrate_series, market_price_series))
        indicators.append(build_production_cost(hashrate_series, market_price_series))

    build_elapsed = time.time() - t_start - fetch_elapsed
    print(f"Reconstruction complete in {build_elapsed:.1f}s")

    # -----------------------------------------------------------------
    # 4. Firing-event detection + forward-return testing
    # -----------------------------------------------------------------
    price_dict = to_dict(market_price_series)
    last_price_date = max(price_dict) if price_dict else date.today()

    price_category = [i for i in indicators if i["category"] == "price"]
    onchain_category = [i for i in indicators if i["category"] == "onchain"]

    price_rows = [render_row(i, price_dict, last_price_date) for i in price_category]
    onchain_rows = [render_row(i, price_dict, last_price_date) for i in onchain_category]

    print_table(price_rows, "PRICE-DERIVED INDICATORS (mean-reversion test — does the indicator's own "
                             "source asset revert after firing?)")
    print_table(onchain_rows, "ON-CHAIN / INDEPENDENT-SOURCE INDICATORS (does an independent data "
                               "source predict forward price?)")

    total_elapsed = time.time() - t_start
    print(f"\n{'='*100}")
    print(f"TOTAL RUNTIME: {total_elapsed:.1f}s")
    print(f"Indicators tested: {len(indicators)} ({len(price_rows)} price-derived, {len(onchain_rows)} on-chain/independent)")
    rate_limited = [t for t, err in bg_fetch_notes.items() if err]
    if rate_limited:
        print(f"Rate-limited / failed BGeometrics fetches: {rate_limited}")
    else:
        print("No rate limits or fetch failures hit during this run.")
    print(f"{'='*100}")

    return {
        "indicators": indicators, "price_rows": price_rows, "onchain_rows": onchain_rows,
        "bg_series": bg_series, "market_price_series": market_price_series,
        "total_elapsed": total_elapsed, "rate_limited": rate_limited,
    }


if __name__ == "__main__":
    if "--miner-capitulation-variants" in sys.argv:
        run_miner_capitulation_variant_analysis()
    elif "--daily-matrix" in sys.argv:
        # Usage: python backtest_indicators.py --daily-matrix [START END]
        # START/END are ISO dates (YYYY-MM-DD), inclusive. Defaults to
        # June 1-30, 2026 (the range this mode was originally built for).
        idx = sys.argv.index("--daily-matrix")
        extra = [a for a in sys.argv[idx + 1:] if not a.startswith("--")]
        if len(extra) >= 2:
            _start = date.fromisoformat(extra[0])
            _end = date.fromisoformat(extra[1])
        else:
            _start, _end = date(2026, 6, 1), date(2026, 6, 30)
        build_daily_matrix(_start, _end)
    else:
        main()
