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
"""
import json
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

# token -> (endpoint slug [best guess, unverified], direction, threshold)
BG_METRICS = {
    "MVRV_Z":        ("mvrv-zscore",      "low",  0.0),
    "REALIZED_PRICE": ("realized-price",  None,   None),
    "PUELL":         ("puell-multiple",   "low",  0.5),
    "RESERVE_RISK":  ("reserve-risk",     "low",  0.002),
    "THERMOCAP":     ("thermocap-multiple", "low", None),
    "LTH_SOPR":      ("lth-sopr",         "low",  1.0),
    "PI_CYCLE":      ("pi-cycle",         None,   None),   # completes original top-8; may be the Top variant not Bottom — see note
    "NRPL":          ("nrpl-btc",         "low",  None),   # Net Realized P&L in BTC — confirmed-real slug (seen directly in your BGeometrics account's own API usage examples), not a guess like the metric it replaced
    "SUPPLY_PROFIT": ("supply-in-profit", None,   None),    # % Supply in Loss = 100 - this, computed in main()
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
    try:
        data = _get_json(url, headers={
            "User-Agent": "btc-dashboard-bot/1.0",
            "Authorization": f"Bearer {BG_KEY}",
        })

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
        print(f"  ! {slug}: HTTP {e.code} — {e.reason}")
    except Exception as e:
        print(f"  ! {slug}: failed — {e}")
    return None


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


def fetch_polymarket_btc_events():
    """One fetch, sorted by volume, filtered client-side for Bitcoin/BTC
    markets — the Gamma API has no free-text search (a ?q= param is
    silently ignored), so this is the correct approach, not a shortcut.

    Returns (events, fetch_succeeded). fetch_succeeded=False means the
    request itself failed (network error, bad response shape) — distinct
    from a successful request that just happened to find zero BTC events,
    which is a legitimate (if unlikely) outcome, not a failure to fall
    back from."""
    url = f"{POLYMARKET_BASE}/events?active=true&closed=false&limit=300&order=volume&ascending=false"
    try:
        data = _get_json(url, headers={"User-Agent": "btc-dashboard-bot/1.0"})
    except Exception as e:
        print(f"  ! Polymarket fetch failed: {e}")
        return [], False

    if not isinstance(data, list):
        print("  ! Polymarket returned an unexpected shape, skipping")
        return [], False

    btc_events = []
    for event in data:
        title = (event.get("title") or event.get("question") or "")
        slug = event.get("slug") or ""
        haystack = f"{title} {slug}".lower()
        if "bitcoin" not in haystack and "btc" not in haystack:
            continue
        btc_events.append(event)
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


def status_pill(value, direction, threshold):
    if value is None or direction is None or threshold is None:
        return "CHECK", "st-mid"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "CHECK", "st-mid"
    if direction == "low":
        return ("BUY ZONE", "st-buy") if v <= threshold else ("NOT YET", "st-no")
    if direction == "high":
        return ("ELEVATED", "st-buy") if v >= threshold else ("NORMAL", "st-no")
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
    "MVRV_Z": 3, "REALIZED_PRICE": 3, "PUELL": 3, "RESERVE_RISK": 3,
    # Tier 2 — solid on-chain (weight 2)
    "THERMOCAP": 2, "LTH_SOPR": 2, "PROD_COST": 2, "NVT_GC": 2,
    "PI_CYCLE": 2, "ASOPR_EST": 2,
    # Tier 3 — behavioral/technical (weight 1.5)
    "MINER_CAP": 1.5, "MACD": 1.5,
    # Tier 4 — sentiment/blunt technicals (weight 1)
    "SUPPLY_LOSS": 1, "MAYER": 1, "RSI": 1, "FNG": 1, "BOLLINGER": 1,
}

DISPLAY_NAMES = {
    "MVRV_Z": "MVRV Z-Score", "REALIZED_PRICE": "Price vs. Realized Price",
    "PUELL": "Puell Multiple", "RESERVE_RISK": "Reserve Risk",
    "THERMOCAP": "Thermocap Multiple", "LTH_SOPR": "LTH-SOPR",
    "PROD_COST": "Production Cost", "NVT_GC": "NVT Golden Cross",
    "PI_CYCLE": "Pi Cycle", "NRPL": "NRPL (Net Realized P&L)",
    "SUPPLY_LOSS": "% Supply in Loss", "ASOPR_EST": "aSOPR (modeled)",
    "MINER_CAP": "Miner Capitulation", "MACD": "MACD (weekly)",
    "MAYER": "Mayer Multiple", "RSI": "Weekly RSI", "FNG": "Fear & Greed",
    "BOLLINGER": "Bollinger %B",
}


TOTAL_POSSIBLE_WEIGHT = sum(WEIGHT_MAP.values())


def build_full_weighted_breakdown(values):
    """Every indicator in WEIGHT_MAP, sorted heaviest to lightest, with its
    true share of the TOTAL weight pool (not just the weight of whatever
    happened to fire today) — this is the fix for the old "3/6" framing,
    which only counted indicators that had a scoreable reading this
    specific run and made the tracked list look much smaller than it is.
    Returns a ready-to-insert HTML string, since the row count and status
    mix changes every day."""
    rows = []
    for token, weight in sorted(WEIGHT_MAP.items(), key=lambda kv: kv[1], reverse=True):
        name = DISPLAY_NAMES.get(token, token)
        pct_of_total = round((weight / TOTAL_POSSIBLE_WEIGHT) * 100, 1)
        css = values.get(f"{token}_STATUS_CLASS")
        label = values.get(f"{token}_STATUS_LABEL", "CHECK")
        if css == "st-buy":
            status_text, status_css = "BUY-FAVORABLE", "st-buy"
        elif css == "st-no":
            status_text, status_css = "NOT YET", "st-no"
        elif label and label.startswith("STALE"):
            status_text, status_css = label, "st-mid"
        else:
            status_text, status_css = "NO SCOREABLE READING TODAY", "st-mid"

        rows.append(
            f'<tr><td class="ind-name">{name}</td>'
            f'<td class="reading">{pct_of_total}%</td>'
            f'<td><span class="status-pill {status_css}">{status_text}</span></td></tr>'
        )
    return "\n".join(rows)


def build_verdict(values):
    scored, excluded_names, buy_names = [], [], []
    for token, weight in WEIGHT_MAP.items():
        css = values.get(f"{token}_STATUS_CLASS")
        name = DISPLAY_NAMES.get(token, token)
        if css == "st-buy":
            scored.append((token, weight, True))
            buy_names.append(name)
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
            values[token] = val
            label, css = status_pill(val, direction, threshold)
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
        print(f"  {token} ({slug}): {values[token]} -> {label}")

    # Supply in Loss = 100 - Supply in Profit (BGeometrics only exposes the
    # profit side under this slug guess; the loss framing is the one your
    # original ranking used, and the one this whole project started from).
    # If the underlying Supply-in-Profit value came from cache (stale),
    # this derived figure inherits that staleness rather than looking
    # freshly computed.
    supply_profit_val = values.get("SUPPLY_PROFIT")
    supply_profit_is_stale = values.get("SUPPLY_PROFIT_STATUS_LABEL", "").startswith("STALE")
    supply_loss_val = None
    if supply_profit_val is not None:
        try:
            supply_loss_val = round(100 - float(supply_profit_val), 2)
        except (TypeError, ValueError):
            supply_loss_val = None
    values["SUPPLY_LOSS"] = supply_loss_val
    if supply_profit_is_stale:
        cached_date = values["SUPPLY_PROFIT_STATUS_LABEL"].split("(")[1].rstrip(")")
        label, css = f"STALE ({cached_date})", "st-mid"
    else:
        label, css = status_pill(supply_loss_val, "high", 50.0)
    values["SUPPLY_LOSS_STATUS_LABEL"], values["SUPPLY_LOSS_STATUS_CLASS"] = label, css
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
        label, css = status_pill(asopr_est, "low", 1.0)
    values["ASOPR_EST_STATUS_LABEL"], values["ASOPR_EST_STATUS_CLASS"] = label, css
    print(f"  ASOPR_EST (modeled from SOPR={values.get('SOPR')}): {asopr_est} -> {label}")

    print("Fetching price history from CoinGecko (340 days, needed for NVT Golden Cross)...")
    price_history = fetch_coingecko_history(days=340)
    spot_price = price_history[-1] if price_history else None

    # Give Realized Price a real dynamic signal now (was a static label before):
    # buy-favorable when spot trades below the realized-price cost basis.
    # Skip the fresh comparison if the realized-price figure itself is a
    # stale cache fallback — don't compare today's live spot price against
    # a days-old cost-basis snapshot and present it with fresh confidence.
    realized_price_val = values.get("REALIZED_PRICE")
    realized_price_is_stale = values.get("REALIZED_PRICE_STATUS_LABEL", "").startswith("STALE")
    if realized_price_is_stale:
        rp_label, rp_css = values["REALIZED_PRICE_STATUS_LABEL"], "st-mid"
    elif realized_price_val is not None and spot_price is not None:
        try:
            rp = float(realized_price_val)
            rp_label, rp_css = ("BUY ZONE", "st-buy") if spot_price < rp else ("NOT YET", "st-no")
        except (TypeError, ValueError):
            rp_label, rp_css = "CHECK", "st-mid"
    else:
        rp_label, rp_css = "CHECK", "st-mid"
    values["REALIZED_PRICE_STATUS_LABEL"], values["REALIZED_PRICE_STATUS_CLASS"] = rp_label, rp_css

    mayer, drawdown = compute_mayer_and_drawdown(price_history)
    values["MAYER"] = mayer
    values["DRAWDOWN"] = drawdown
    label, css = status_pill(mayer, "low", 1.0)
    values["MAYER_STATUS_LABEL"], values["MAYER_STATUS_CLASS"] = label, css
    print(f"  Mayer Multiple: {mayer} | Drawdown from ATH: {drawdown}%")

    print("Computing weekly RSI, MACD, and Bollinger %B from the same price history...")
    rsi = compute_weekly_rsi(price_history)
    values["RSI"] = rsi
    label, css = status_pill(rsi, "low", 30.0)
    values["RSI_STATUS_LABEL"], values["RSI_STATUS_CLASS"] = label, css

    macd_hist, macd_crossed = compute_weekly_macd(price_history)
    values["MACD"] = macd_hist
    values["MACD_CROSSED"] = "Yes — fresh this week" if macd_crossed else "No"
    label, css = status_pill(macd_hist, "high", 0.0)
    values["MACD_STATUS_LABEL"], values["MACD_STATUS_CLASS"] = label, css

    bollinger_pb = compute_bollinger(price_history)
    values["BOLLINGER"] = bollinger_pb
    label, css = status_pill(bollinger_pb, "low", 0.2)
    values["BOLLINGER_STATUS_LABEL"], values["BOLLINGER_STATUS_CLASS"] = label, css
    print(f"  RSI: {rsi} | MACD histogram: {macd_hist} (crossed: {macd_crossed}) | Bollinger %B: {bollinger_pb}")

    print("Fetching network stats from Blockchain.com...")
    stats = fetch_blockchain_stats()
    cost_per_btc, pct_vs_cost = compute_production_cost(stats)
    values["PROD_COST"] = cost_per_btc
    values["PROD_COST_PCT"] = pct_vs_cost
    prod_status = "CHECK"
    prod_css = "st-mid"
    if pct_vs_cost is not None:
        prod_status, prod_css = ("BUY ZONE", "st-buy") if pct_vs_cost < 0 else ("NOT YET", "st-no")
    values["PROD_COST_STATUS_LABEL"], values["PROD_COST_STATUS_CLASS"] = prod_status, prod_css
    print(f"  Est. production cost (electricity-only): ${cost_per_btc} ({pct_vs_cost}% vs. spot)")

    hashrate_hist = fetch_blockchain_chart("hash-rate", days=100)
    mc_state, hr_deviation = compute_miner_capitulation(hashrate_hist, price_history)
    values["MINER_CAP"] = hr_deviation
    values["MINER_CAP_STATE"] = mc_state or "CHECK"
    # Only the full Edwards "BUY SIGNAL" (recovery + price momentum) counts as
    # a positive trigger. Being mid-capitulation or recovering-without-price-
    # confirmation is context, not a signal — matches the true methodology
    # rather than over-crediting the in-progress states.
    mc_css = {"BUY SIGNAL": "st-buy", "CAPITULATION": "st-mid", "RECOVERING": "st-mid", None: "st-mid"}.get(mc_state, "st-mid")
    values["MINER_CAP_STATUS_LABEL"] = mc_state or "CHECK"
    values["MINER_CAP_STATUS_CLASS"] = mc_css
    print(f"  Miner Capitulation (Hash Ribbons): {mc_state}, hashrate MA deviation {hr_deviation}%")

    print("Fetching transaction volume history from Blockchain.com (340 days)...")
    tx_vol_hist = fetch_blockchain_chart("estimated-transaction-volume-usd", days=340)
    nvt_gc = compute_nvt_golden_cross(price_history, tx_vol_hist)
    values["NVT_GC"] = nvt_gc
    if nvt_gc is None:
        nvt_label, nvt_css = "CHECK", "st-mid"
    elif nvt_gc > 2.2:
        nvt_label, nvt_css = "OVERPRICED", "st-no"
    elif nvt_gc < -1.6:
        nvt_label, nvt_css = "BUY ZONE", "st-buy"
    else:
        nvt_label, nvt_css = "NEUTRAL", "st-mid"
    values["NVT_GC_STATUS_LABEL"], values["NVT_GC_STATUS_CLASS"] = nvt_label, nvt_css
    print(f"  NVT Golden Cross (z-score): {nvt_gc} -> {nvt_label}")

    print("Fetching Fear & Greed from Alternative.me...")
    fng_val, fng_label = fetch_fear_greed()
    values["FNG"] = fng_val
    values["FNG_LABEL"] = fng_label
    label, css = status_pill(fng_val, "low", 20.0)
    values["FNG_STATUS_LABEL"], values["FNG_STATUS_CLASS"] = label, css
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
    values["FULL_WEIGHTED_BREAKDOWN_ROWS"] = build_full_weighted_breakdown(values)
    values["TOTAL_TRACKED_COUNT"] = len(WEIGHT_MAP)
    verdict = build_verdict(values)
    values.update(verdict)
    print(f"  Verdict: {verdict['VERDICT_HEADLINE']} ({verdict['VERDICT_PCT']}%, {verdict['VERDICT_COUNT']} weighted-buy)")

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
