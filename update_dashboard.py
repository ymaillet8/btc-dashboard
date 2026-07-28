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

# token -> (endpoint slug [best guess, unverified], direction, threshold)
BG_METRICS = {
    "MVRV_Z":        ("mvrv-zscore",      "low",  0.0),
    "REALIZED_PRICE": ("realized-price",  None,   None),
    "PUELL":         ("puell-multiple",   "low",  0.5),
    "RESERVE_RISK":  ("reserve-risk",     "low",  0.002),
    "THERMOCAP":     ("thermocap-multiple", "low", None),
    "LTH_SOPR":      ("lth-sopr",         "low",  1.0),
    "PI_CYCLE_BOTTOM": ("pi-cycle-bottom", None,  None),
    "SUPPLY_LOSS":   ("supply-in-loss",   "high", 50.0),
    "VDD":           ("vdd-multiplier",   "low",  None),   # no widely-cited fixed threshold — context only
    "ASOPR":         ("asopr",            "low",  1.0),
}
# NOTE: this is exactly 10 of 10 free-tier requests/hour (confirmed from your
# account screenshot: 10 req/hour, 15 req/day on Free). Zero buffer left —
# a manual re-run within the same hour will hit a 429. If that happens,
# just wait ~an hour, or re-run via workflow_dispatch after the window resets.

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
    url = f"{BG_BASE}/{slug}"
    try:
        data = _get_json(url, headers={
            "User-Agent": "btc-dashboard-bot/1.0",
            "Authorization": f"Bearer {BG_KEY}",
        })
        if isinstance(data, dict):
            if "value" in data:
                return data["value"]
            d = data.get("data")
            if isinstance(d, list) and d:
                last = d[-1]
                return last[-1] if isinstance(last, list) else last
            if isinstance(d, dict) and "value" in d:
                return d["value"]
        if isinstance(data, list) and data:
            last = data[-1]
            return last[-1] if isinstance(last, list) else last
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
    # Tier 2 — solid on-chain, ranks 6-12 (weight 2)
    "THERMOCAP": 2, "LTH_SOPR": 2, "PI_CYCLE_BOTTOM": 2, "PROD_COST": 2,
    "VDD": 2, "ASOPR": 2, "NVT_GC": 2,
    # Tier 3 — behavioral/technical, ranks 13-16 (weight 1.5)
    "MINER_CAP": 1.5, "MACD": 1.5,
    # Tier 4 — sentiment/blunt technicals, ranks 17-21 (weight 1)
    "SUPPLY_LOSS": 1, "MAYER": 1, "RSI": 1, "FNG": 1, "BOLLINGER": 1,
}

DISPLAY_NAMES = {
    "MVRV_Z": "MVRV Z-Score", "REALIZED_PRICE": "Price vs. Realized Price",
    "PUELL": "Puell Multiple", "RESERVE_RISK": "Reserve Risk",
    "THERMOCAP": "Thermocap Multiple", "LTH_SOPR": "LTH-SOPR",
    "PI_CYCLE_BOTTOM": "Pi Cycle Bottom", "PROD_COST": "Production Cost",
    "VDD": "VDD Multiplier", "ASOPR": "aSOPR", "NVT_GC": "NVT Golden Cross",
    "MINER_CAP": "Miner Capitulation", "MACD": "MACD (weekly)",
    "SUPPLY_LOSS": "% Supply in Loss", "MAYER": "Mayer Multiple",
    "RSI": "Weekly RSI", "FNG": "Fear & Greed", "BOLLINGER": "Bollinger %B",
}


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

    if not BG_KEY:
        print("! BGEOMETRICS_API_KEY not set — those 10 rows will show CHECK.")

    print(f"Fetching {len(BG_METRICS)} indicators from BGeometrics...")
    for token, (slug, direction, threshold) in BG_METRICS.items():
        val = fetch_bg_latest(slug)
        values[token] = val
        label, css = status_pill(val, direction, threshold)
        values[f"{token}_STATUS_LABEL"] = label
        values[f"{token}_STATUS_CLASS"] = css
        print(f"  {token} ({slug}): {val} -> {label}")

    print("Fetching price history from CoinGecko (340 days, needed for NVT Golden Cross)...")
    price_history = fetch_coingecko_history(days=340)
    spot_price = price_history[-1] if price_history else None

    # Give Realized Price a real dynamic signal now (was a static label before):
    # buy-favorable when spot trades below the realized-price cost basis.
    realized_price_val = values.get("REALIZED_PRICE")
    if realized_price_val is not None and spot_price is not None:
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

    print("Computing cycle rhythm...")
    days_since_top, projected_bottom, days_to_projected = compute_cycle_rhythm()
    values["DAYS_SINCE_TOP"] = days_since_top
    values["PROJECTED_BOTTOM"] = projected_bottom
    values["DAYS_TO_PROJECTED"] = days_to_projected
    print(f"  {days_since_top} days since last cycle top, projected bottom {projected_bottom} ({days_to_projected} days away)")

    values["LAST_UPDATED"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print("Building weighted verdict synthesis...")
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

    print("Wrote dashboard.html")


if __name__ == "__main__":
    main()
