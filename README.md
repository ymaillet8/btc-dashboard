# BTC Bottom Watch — Free Automated Dashboard (v5)

Updates itself once a day, for genuinely free, at a shareable public URL.
This version adds 3 more self-computed technical indicators (MACD, weekly
RSI, Bollinger %B — zero extra API calls, reused from existing price data)
plus a weighted verdict synthesis at the bottom that reads across every
indicator on the page. **19 automated indicators total.**

## What's new in v5

- **MACD (weekly, 12/26/9), Weekly RSI (14-period), Bollinger %B (20-week,
  2σ)** — all computed from the same CoinGecko price history already being
  fetched for Mayer Multiple and NVT Golden Cross. No new API calls, no new
  rate-limit exposure.
- **The full ranked list now includes these three**, inserted at #16, #19,
  and #21 respectively (see the standalone ranking artifact from earlier,
  or the WEIGHT_MAP in `update_dashboard.py`). They rank below the on-chain
  tier because they're generic technicals — the same math works on any
  asset, not just Bitcoin, and RSI/Bollinger in particular can stay pinned
  in "oversold" territory for months during a strong downtrend.
- **Two rows that were static before now have real computed signals:**
  Price vs. Realized Price (buy-favorable when spot trades below the
  realized-price cost basis) and Fear & Greed (buy-favorable at ≤20,
  "Extreme Fear"). Both used to just display "context only."
- **A weighted verdict section at the very bottom of the dashboard**,
  synthesizing every indicator's current state into one read.

## How the verdict synthesis works — and what it deliberately is not

Every indicator with a real (non-"CHECK") reading this run gets weighted
by its rank on your own ranked list (MVRV Z-Score, Realized Price, Puell,
Reserve Risk = weight 3; the on-chain tier below them = weight 2; Miner
Capitulation and MACD = weight 1.5; sentiment/blunt technicals = weight 1).
The script tallies what fraction of that weighted total is currently
buy-favorable and buckets the result into one of four narrative ranges
(minimal / partial / strong / near-maximal confluence), each with a short,
historically-grounded description of what that confluence level has
preceded in prior cycles.

**What this is:** a mechanical, fully transparent re-reading of the exact
rows already on the dashboard — nothing it says comes from outside the
data you can already see above it.

**What this is NOT:** financial advice, a price prediction, or a
substitute for your Power Law check. The script has no way to know your
Power Law status — it can only read the automated rows. The verdict text
says this explicitly every time it renders, and the master banner at the
top of the page still carries the actual veto.

If you want to change how much weight any indicator carries in this tally,
edit `WEIGHT_MAP` near the bottom of `update_dashboard.py` — it's a plain
dictionary, safe to adjust as your own conviction in specific indicators
shifts.

## What's automated now

**Your mandated top 8** (everything but Power Law, per your own ranking),
via BGeometrics — 7 pulled directly, 1 self-computed to save quota:
- MVRV Z-Score, Price vs. Realized Price, Puell Multiple, Reserve Risk,
  Thermocap Multiple, LTH-SOPR, Pi Cycle Bottom Indicator — 7 BGeometrics
  calls
- Price vs. Production Cost — **not** from BGeometrics; computed directly
  from live Blockchain.com hashrate/difficulty data plus two disclosed
  assumptions (see below), freeing up a BGeometrics call
- % Supply in Loss — using the slot freed up by computing Production Cost
  separately

**2 more, added in v4 once the real 10/hour limit was confirmed:**
- VDD Multiplier, aSOPR — your next-highest-ranked indicators (#10, #11 on
  your list), pulled directly from BGeometrics

That's **10 of exactly 10** confirmed free hourly requests. Zero spare —
see the warning below.

**9 more, self-computed from other free sources, using zero extra
BGeometrics quota:**
- Mayer Multiple, Drawdown Magnitude — from CoinGecko's free price history
- Miner Capitulation Index (Hash Ribbons proxy), NVT Golden Cross — from
  Blockchain.com's free hashrate/transaction-volume history
- MACD (weekly), Weekly RSI, Bollinger %B — from the same CoinGecko price
  history, added in v5
- 1064/364-Day Cycle Rhythm — pure calendar math, no API at all
- Fear & Greed Index — Alternative.me, as before

**Total: 19 automated indicators, synthesized into one weighted verdict at
the bottom of the page, updating daily with no action from you.**

## ⚠️ Zero buffer — read this before you manually re-run the workflow

Using all 10/10 hourly requests means there's no headroom left. If you
manually trigger the workflow a second time within the same hour (e.g. to
test a fix), it will hit a 429 (rate-limited) error and most rows will show
"CHECK" for that run. This isn't a bug — it's the direct tradeoff of using
the full quota. If it happens: just wait until the top of the next hour and
trigger it again, or wait for tomorrow's scheduled run. If this gets
annoying in practice, drop back to 8 or 9 metrics (remove VDD or aSOPR from
`BG_METRICS` near the top of `update_dashboard.py`) to restore a buffer.

## Still manual — no free source exists

Realized Cap HODL Waves, Whale accumulation/exchange netflow. These
require UTXO-age-cohort or exchange-flow data that every free public
source gates behind a paywall — genuinely couldn't find a way
around this. Plus Power Law, which stays manual by design (see your
TradingView-alert plan).

## What you need (all free)
- A GitHub account
- A free BGeometrics API key (portal.bgeometrics.com/register)
- Nothing else — CoinGecko, Blockchain.com, and Alternative.me are all
  keyless, no signup required

## Setup steps

1. **Create a new public GitHub repository** (e.g. `btc-dashboard`).
2. **Upload:** `update_dashboard.py`, `dashboard_template.html`, and the
   `.github/workflows/update.yml` folder (keep that exact path).
3. **Get your BGeometrics key** at portal.bgeometrics.com/register.
4. **Add it as a secret:** repo Settings → Secrets and variables → Actions
   → New repository secret → name it `BGEOMETRICS_API_KEY` → paste the key.
5. **Enable GitHub Pages:** Settings → Pages → Source → GitHub Actions.
6. **Run it once manually:** Actions tab → "Update BTC Dashboard" →
   Run workflow. Check your live URL after ~30 seconds:
   `https://<your-github-username>.github.io/<repo-name>/dashboard.html`

Reruns automatically once a day from there.

## Two things worth understanding about the self-computed rows

*(Updated — these were refined against the actual published methodologies
after a deeper research pass, not the rough first-draft versions.)*

**Production Cost** is now grounded in a cited source rather than a picked
number: the $0.05/kWh electricity assumption is Cambridge University's own
CBECI model's stated global-average assumption (ccaf.io/cbnsi/cbeci/
methodology) — about as authoritative as a free source gets. The 20 J/TH
efficiency assumption sits in the middle of the range cited across current
sources (newest ASICs run ~15 J/TH, network-wide blended estimates
including older hardware run as high as 28 J/TH). Both are adjustable at
the top of `update_dashboard.py`. Important: this is an **electricity-only**
estimate, so it will read lower than "all-in" bank headlines like
JPMorgan's ~$78,000 (which also bake in hardware depreciation and
corporate overhead) — that's expected, not an inconsistency.

**Miner Capitulation Index** now implements Charles Edwards' full original
Hash Ribbons methodology (capriole.com/hash-ribbons-bitcoin-bottoms), not
just a simplified percentage: it tracks the 30-day/60-day hashrate
moving-average cross for capitulation/recovery, AND checks his recommended
price-momentum confirmation (10-day/20-day price moving average) before
calling it a genuine "BUY SIGNAL" rather than just "RECOVERING."

**NVT Golden Cross** now uses CryptoQuant's exact published formula
(userguide.cryptoquant.com/cryptoquant-metrics/network/nvt-golden-cross):
a z-score of the 10-day/30-day NVT moving-average spread against its own
300-day rolling volatility — not a raw percentage approximation. This
needed extending the CoinGecko and Blockchain.com history fetches from
~60-210 days to 340 days to have enough data for a proper 300-day
standard deviation window.

One number I checked and deliberately did NOT use: a widely-repeated
"infrastructure overhead multiplier" (~2.3x) from a secondary source that,
on closer inspection, didn't hold together mathematically — its own worked
example had an internal unit-conversion error. Rather than propagate that,
Production Cost here stays as a straightforward, verifiable calculation.


## If a BGeometrics slug turns out wrong

Actions tab → latest run → "Run update script" step → look for a line like
`! puell-multiple: HTTP 404`. That row will show "CHECK" on the dashboard
instead of breaking anything. Fix: log into portal.bgeometrics.com, find
the metric's real endpoint slug, update it in the `BG_METRICS` dictionary
near the top of `update_dashboard.py` — or paste the error to Claude.

## Updating the cycle-rhythm anchor dates

`CYCLE_ANCHORS` near the top of `update_dashboard.py` holds the last cycle
top's date and your model's projected offset to the next bottom. Update
these if your own cycle-timing model changes. Same for `ALL_TIME_HIGH_USD`
if a new ATH prints — used for the Drawdown Magnitude row.

## Sharing with your brother

Once Pages is live, send him the URL from step 6 — normal public webpage,
no login, always shows the latest daily-refreshed numbers.

## Adaptive Thresholds & Pattern Signals (v19)

### Why MVRV Z-Score's fixed ≤0.0 threshold was replaced

Bitcoin's cycle-bottom drawdowns have structurally compressed over time
(peak-to-trough roughly -93% in 2011, down to -77% in 2015, -84% in 2018,
and -54% overall by the 2022 low). A single fixed number picked from past
cycles risks quietly becoming too easy — or too hard — to cross as that
compression continues. Worse, there are only 3 real historical cycle
bottoms (2015/2018/2022) to anchor a *new* fixed constant on — nowhere
near enough data points to justify inventing one with any real confidence.

So MVRV Z-Score now uses the same self-normalizing approach already built
for Thermocap and NRPL: the 5th–10th percentile (7.5th, as the midpoint)
of its own trailing 2-year (730-day) history, recomputed fresh every day
from the same daily cache the dashboard already builds — zero extra API
cost. `get_adaptive_mvrv_threshold()` in `update_dashboard.py` does this;
the live target cell shows the actual number plus the percentile and
window it came from ("≤ -0.15 (live, 7.5th pct. of trailing 400d, capped
730d)"), never a bare, unexplained number.

**Honest limitation, stated up front:** unlike Active Addresses Power-Law
Deviation, there's no free unmetered bulk-history source for MVRV
Z-Score — checked directly, no such file exists on BGeometrics' public
chart infrastructure. So this has no bootstrap shortcut: it needs 365 real
accumulated daily points (one per day this dashboard actually runs and
gets a fresh MVRV_Z reading) before it activates. Until then, MVRV Z-Score
stays on the original fixed ≤0.0 threshold — the exact same fallback
behavior `get_effective_sigma()` already uses elsewhere on this page.
Given the cache only started accumulating in August 2026, expect roughly a
year before the adaptive threshold goes live.

### Capital Deployment Tranches (§5)

Three tranches, sequenced by the order these signal groups have
historically turned in 2015/2018/2022 — not an arbitrary grouping:

1. **Tranche 1 (~20-25% allocation) — Early Movers.** Reserve Risk and
   LTH-SOPR, both long-term-holder-conviction signals, have tended to turn
   favorable first, well before the eventual low.
2. **Tranche 2 (~35-40% allocation) — Core Confirmers.** MVRV Z-Score
   (using the adaptive threshold above) and Puell Multiple — the core
   valuation-vs-issuance signals — have tended to confirm next.
3. **Tranche 3 (remainder) — All Clear.** Hash Ribbons flipping from
   CAPITULATION to its recovery/BUY SIGNAL state (30d hashrate MA crossing
   back above 60d MA, confirmed by price momentum) has historically been
   the last, most-confirming signal of the group.

Each tranche fires only when **all** of its components are independently
buy-favorable — this is a read-only, additive display feature: it never
feeds `WEIGHT_MAP`, `get_master_rank_order()`, or the weighted verdict
above it. It's a second lens on the same underlying indicators, not a
second scoring system.

### The 5-day persistence rule

A tranche only shows CONFIRMED once every one of its components has held
its buy-favorable status for 5 or more *consecutive calendar days* — not a
single good print. This reuses `consecutive_buy_days()` (already built for
Reserve Risk/LTH-SOPR's "X days" annotation) for the three numeric
threshold-crossing components (Reserve Risk, LTH-SOPR, MVRV Z-Score,
Puell), and a small parallel `state_streak()` helper for Hash Ribbons'
categorical CAPITULATION/RECOVERING/BUY SIGNAL state, which isn't a number
crossing a line so `consecutive_buy_days()` doesn't apply to it directly.
A component favorable for fewer than 5 days shows as "🟡 flashing (day
N/5)" rather than counting toward confirmation — the whole point is to
filter out a one-day wobble, not to hide it.

### MVRV-Z / Price Divergence detector (§6)

Watches for the pattern that preceded the actual Nov 2022 bottom: price
making a **lower low** while MVRV Z-Score simultaneously makes a **higher
low** (-1.36 in Nov 2022 vs. -2.53 in June 2022) — a price washout not
matched by an equally deep on-chain capitulation, a classic bullish
divergence.

`detect_mvrv_price_divergence()` identifies the two most recent local
price minima over a trailing 180-day window (standard swing-low detection:
a day counts as a local minimum only if it's the lowest price within
±14 days on both sides), then looks up each minimum's date against MVRV
Z-Score's own dated history. If the second, more recent minimum's price is
lower but its Z-Score is higher, that's `DIVERGENCE DETECTED`.

This is a **confirming/context signal only** — like the tranches above, it
never joins `WEIGHT_MAP`, `_ALL_TRACKED_TOKENS`, or either pie chart. It
lives in its own §6 Pattern Signals section, separate from both weighted
verdict tables.

**Honest limitation:** this needs real dated MVRV Z-Score history spanning
however far back the two price minima it finds actually are — currently
only a handful of days deep (same bootstrap gap as the adaptive threshold
above), so this will realistically read N/A for months. It reports exactly
why it's N/A (how many local minima it found, how sparse the matching
Z-Score history is) rather than guessing at a result it can't actually
support yet.
