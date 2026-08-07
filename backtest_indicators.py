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
import json as json_module
import math
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


def build_simple_bg_indicator(name, token, series, direction, threshold,
                               source="BGeometrics (startday/endday range)"):
    return {
        "name": name, "source": source,
        "category": "onchain",
        "statuses": walk_forward_statuses(series, direction, lambda c, d, v: threshold, token),
    }


def build_percentile_indicator(name, token, series, source_label):
    """Thermocap / NRPL / Active Addresses pattern: direction=low, dynamic
    threshold = ud.rolling_threshold()'s live bottom-10th-percentile of the
    token's own walk-forward-built trailing history (n>=90 gate, identical
    to production's MIN_HISTORY_DAYS bootstrap)."""
    def threshold_fn(cache, d, v):
        threshold, n_points = ud.rolling_threshold(cache, token)
        return threshold
    return {
        "name": name, "source": source_label, "category": "onchain",
        "statuses": walk_forward_statuses(series, "low", threshold_fn, token),
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


def build_nvt_gc(market_price_series, tx_volume_series):
    mp_dict_series = market_price_series
    dated_raw = []
    for d, _ in market_price_series:
        price_window = _trailing_window(market_price_series, d, PRICE_WINDOW_DAYS)
        vol_window = _trailing_window(tx_volume_series, d, PRICE_WINDOW_DAYS)
        if len(vol_window) < 330:
            continue
        nvt_gc = ud.compute_nvt_golden_cross(price_window, vol_window)
        dated_raw.append((d, nvt_gc))
    dated_raw = [(d, v) for d, v in dated_raw if v is not None]

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


def build_miner_capitulation(hashrate_series, market_price_series):
    dated_raw = []
    for d, _ in hashrate_series:
        hr_window = _trailing_window(hashrate_series, d, 100)  # matches live fetch_blockchain_chart(..., days=100)
        price_window = _trailing_window(market_price_series, d, PRICE_WINDOW_DAYS)
        if len(hr_window) < 60 or len(price_window) < 20:
            continue
        mc_state, hr_dev = ud.compute_miner_capitulation(hr_window, price_window)
        dated_raw.append((d, hr_dev, mc_state))

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
    main()
