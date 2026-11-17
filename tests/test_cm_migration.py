"""Tests for the Coin Metrics scoring migration (MVRV Z, Puell, Realized Price). No real network, no API
key; end-to-end runs happen in a temp dir against a mocked urlopen. Run from the repo root:

    python -m unittest tests.test_cm_migration
"""
import contextlib
import copy
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import urllib.error
from datetime import date, datetime, timedelta, timezone
from unittest import mock

sys.dont_write_bytecode = True
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import update_dashboard as ud  # noqa: E402

RUN = date(2026, 11, 15)
CM_CACHE = os.path.join(ROOT, "backtest_output", "coinmetrics_cache", "btc_1d.json")
TODAY = datetime.now(timezone.utc).date()


def synth_rows(last=None, n=500, drop=None, mvrv="1.5"):
    last = last or (TODAY - timedelta(days=1))
    rows = []
    for i in range(n):
        d = last - timedelta(days=n - 1 - i)
        mc = 1.0e12 + 1.0e9 * i
        r = {"asset": "btc", "time": f"{d.isoformat()}T00:00:00.000000000Z",
             "CapMVRVCur": mvrv, "CapMrktCurUSD": str(mc), "IssTotUSD": "30000000", "SplyCur": "19000000"}
        if drop:
            r.pop(drop, None)
        rows.append(r)
    return rows


class FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()
        self.headers = {}

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# --------------------------------------------------------------------------- derivations
class TestDerive(unittest.TestCase):
    def test_series_values(self):
        res = ud.derive_coinmetrics_series(synth_rows(date(2026, 11, 14)), RUN)
        self.assertEqual(res["latest"], date(2026, 11, 14))
        self.assertGreater(res["MVRV_Z"][res["latest"]], 0)             # MC > RC when MVRV > 1
        self.assertAlmostEqual(res["PUELL"][res["latest"]], 1.0, places=6)   # flat issuance
        mc = 1.0e12 + 1.0e9 * 499
        self.assertAlmostEqual(res["REALIZED_PRICE"][res["latest"]], mc / 1.5 / 19000000, places=6)

    def test_crosscheck_wrapper_rounding(self):
        res = ud.derive_coinmetrics_crosscheck(synth_rows(date(2026, 11, 14)), RUN)
        self.assertEqual(set(res), set(ud.CM_TOKENS))
        self.assertEqual(res["MVRV_Z"][1], "2026-11-14")

    def test_failure_modes_raise(self):
        bad = {
            "stale": synth_rows(date(2026, 11, 1)),
            "missing metric": synth_rows(date(2026, 11, 14), drop="CapMVRVCur"),
            "malformed": [{"time": "x"}],
            "empty": [],
            "short history": synth_rows(date(2026, 11, 14), n=200),
        }
        for name, rows in bad.items():
            with self.subTest(name), self.assertRaises(ValueError):
                ud.derive_coinmetrics_series(rows, RUN)

    @unittest.skipUnless(os.path.exists(CM_CACHE), "Coin Metrics cache not present (git-ignored)")
    def test_matches_validated_stored_values(self):
        """Real cached Coin Metrics history: the values validated against the dashboard in the Nov 2026
        research (Z 0.327, Puell 0.799, Realized Price 53,730 on 2026-11-14)."""
        with open(CM_CACHE, encoding="utf-8") as f:
            rows = json.load(f)["rows"]
        res = ud.derive_coinmetrics_crosscheck(rows, date(2026, 11, 15))
        self.assertAlmostEqual(res["MVRV_Z"][0], 0.327, places=3)
        self.assertAlmostEqual(res["PUELL"][0], 0.799, places=3)
        self.assertEqual(res["REALIZED_PRICE"][0], 53730)

    @unittest.skipUnless(os.path.exists(CM_CACHE), "Coin Metrics cache not present (git-ignored)")
    def test_tracks_stored_dashboard_z_at_seven_day_lag(self):
        """Since ~11 Sep the BG Z on run date D equals the Coin Metrics Z of D-7 (validated: within +0.001..+0.010)."""
        with open(CM_CACHE, encoding="utf-8") as f:
            rows = json.load(f)["rows"]
        z = ud.derive_coinmetrics_series(rows, date(2026, 11, 15))["MVRV_Z"]
        with open(os.path.join(ROOT, "last_known_good.json"), encoding="utf-8") as f:
            h = json.load(f)["MVRV_Z__history"]
        vals, dts = h["values"], h["dates"]
        off = len(vals) - len(dts)
        checked = 0
        for i, dt in enumerate(dts):
            d = date.fromisoformat(dt)
            if date(2026, 11, 2) <= d <= date(2026, 11, 14) and (d - timedelta(days=7)) in z:
                self.assertLess(abs(vals[off + i] - z[d - timedelta(days=7)]), 0.02, dt)
                checked += 1
        self.assertGreaterEqual(checked, 8)


class TestFlagsAndShadowHelpers(unittest.TestCase):
    def test_flags_default_false(self):
        self.assertIs(ud.MVRV_ADAPTIVE_THRESHOLD_ACTIVE, False)
        self.assertIs(ud.MVRV_MOMENTUM_LIVE, False)

    def test_shadow_thresholds_walk_forward(self):
        z = {date(2024, 1, 1) + timedelta(days=i): float(i % 50) / 10 for i in range(1500)}
        asof = date(2024, 1, 1) + timedelta(days=700)
        early = ud.shadow_adaptive_thresholds(z, asof)
        full = ud.shadow_adaptive_thresholds(z, max(z))
        self.assertEqual(set(early), {"730", "1460", "full"})
        # only data up to as_of is used: truncating the future cannot change the answer
        z2 = {d: v for d, v in z.items() if d <= asof}
        self.assertEqual(early, ud.shadow_adaptive_thresholds(z2, asof))
        short = {d: v for d, v in list(z.items())[:100]}
        self.assertTrue(all(v is None for v in ud.shadow_adaptive_thresholds(short, max(short)).values()))
        self.assertIsNotNone(full["730"])

    def test_adaptive_flag_controls_tranche_threshold(self):
        """n >= 365 Coin Metrics days exist, but with the flag off the fixed <= 0.0 stays in force."""
        cache = {"MVRV_Z_CM__history": {"values": [0.1] * 400, "dates": []},
                 "PUELL_CM__history": {"values": [0.9] * 200, "dates": []}}
        values = {}
        with mock.patch.object(ud, "MVRV_ADAPTIVE_THRESHOLD_ACTIVE", False):
            off = ud._compute_tranche_data(values, cache)
        with mock.patch.object(ud, "MVRV_ADAPTIVE_THRESHOLD_ACTIVE", True):
            on = ud._compute_tranche_data(values, cache)
        days_off = dict(off[1]["components"])["MVRV Z-Score"]
        days_on = dict(on[1]["components"])["MVRV Z-Score"]
        self.assertEqual(days_off, 0)
        self.assertGreater(days_on, 0)

    def test_leeway_sigma_uses_last_200_points_of_cm_arrays_only(self):
        import statistics
        wide = [((i % 7) - 3) * 1.0 for i in range(530)]            # old, volatile part
        calm = [0.30 + 0.01 * ((i % 5) - 2) for i in range(200)]      # last 200 points
        cache = {"MVRV_Z_CM__history": {"values": wide + calm}, "MVRV_Z__history": {"values": wide + calm}}
        sig_cm, src = ud.get_effective_sigma(cache, "MVRV_Z_CM", 0.0)
        self.assertAlmostEqual(sig_cm, statistics.pstdev(calm), places=9)
        self.assertEqual(src, "n=200")
        # the legacy BG key keeps its old behaviour (backtests/tests rely on it): full array
        sig_bg, _ = ud.get_effective_sigma(cache, "MVRV_Z", 0.0)
        self.assertAlmostEqual(sig_bg, statistics.pstdev(wide + calm), places=9)
        # band and STRONG BUY tier follow the same sigma
        band, _ = ud.near_threshold_band(cache, "MVRV_Z_CM", 0.0)
        self.assertAlmostEqual(band, sig_cm * ud.NEAR_BAND_STDEV_FRACTION, places=9)
        self.assertEqual(ud.status_pill(-(ud.STRONG_BUY_STDEV_FRACTION * sig_cm) - 0.001, "low", 0.0, cache=cache, token="MVRV_Z_CM")[1], "st-strong-buy")
        self.assertNotEqual(ud.status_pill(-(ud.STRONG_BUY_STDEV_FRACTION * sig_cm) + 0.01, "low", 0.0, cache=cache, token="MVRV_Z_CM")[1], "st-strong-buy")
        # Puell's array is already 200 points: unchanged
        pc = {"PUELL_CM__history": {"values": calm}}
        self.assertAlmostEqual(ud.get_effective_sigma(pc, "PUELL_CM", 0.5)[0], statistics.pstdev(calm), places=9)

    def test_table3_and_shadow_windows_still_read_full_cm_arrays(self):
        vals = [float(i % 40) / 10 for i in range(730)]
        cache = {"MVRV_Z_CM__history": {"values": vals}}
        thr, n = ud.rolling_threshold(cache, "MVRV_Z_CM", percentile=90)
        self.assertEqual(n, 730)
        self.assertAlmostEqual(thr, ud.compute_percentile(vals, 90))
        self.assertEqual(ud.LEEWAY_WINDOW_OVERRIDES, {"MVRV_Z_CM": ud.HISTORY_MAX_LEN, "PUELL_CM": ud.HISTORY_MAX_LEN})

    def test_shadow_log_one_entry_per_day(self):
        cache = {}
        v = {"MVRV_Z_SHADOW": {"z": 0.3, "thresholds": {"730": 0.4, "1460": 0.2, "full": -0.1},
                               "statuses": {"730": "BUY ZONE", "1460": "NOT YET", "full": "NOT YET"}, "data_date": "2026-11-14"},
             "MVRV_MOMENTUM_SHADOW_LABEL": "BUY ZONE"}
        ud.record_shadow_log(cache, v, RUN)
        ud.record_shadow_log(cache, v, RUN)
        ud.record_shadow_log(cache, v, RUN + timedelta(days=1))
        self.assertEqual([e["date"] for e in cache["SHADOW_LOG"]], ["2026-11-15", "2026-11-16"])
        self.assertEqual(cache["SHADOW_LOG"][0]["thresholds"]["1460"], 0.2)
        ud.record_shadow_log(cache, {"MVRV_Z_SHADOW": None}, RUN + timedelta(days=2))      # no CM -> no entry
        self.assertEqual(len(cache["SHADOW_LOG"]), 2)

    def test_rebuild_history_true_dates_and_trim(self):
        series = {date(2024, 1, 1) + timedelta(days=i): float(i) for i in range(1000)}
        cache = {}
        ud.rebuild_cm_history(cache, "MVRV_Z_CM", series)
        h = cache["MVRV_Z_CM__history"]
        self.assertEqual(len(h["values"]), 730)
        self.assertEqual(h["dates"][-1], max(series).isoformat())
        ud.rebuild_cm_history(cache, "PUELL_CM", series)
        self.assertEqual(len(cache["PUELL_CM__history"]["values"]), ud.HISTORY_MAX_LEN)

    def test_verdict_history_optional_field_and_trend_ignores_it(self):
        cache = {"VERDICT_HISTORY": [{"date": "2026-11-14", "cycle_bottom_pct": 40.0, "momentum_shift_pct": 0.0,
                                      "cycle_top_pct": 0.0, "source": "live"}]}
        old_row = dict(cache["VERDICT_HISTORY"][0])
        ud.verdict_history_append(cache, date(2026, 11, 15), 42.6, 0.0, 0.0, data_source="cm-2026-11")
        self.assertEqual(cache["VERDICT_HISTORY"][0], old_row)                 # old rows untouched
        self.assertEqual(cache["VERDICT_HISTORY"][1]["data_source"], "cm-2026-11")
        self.assertIsInstance(ud.build_verdict_trend_html(cache), str)          # unknown field ignored


# --------------------------------------------------------------------------- end to end
def make_router(cm_mode, bg_calls):
    def urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "api.bitcoin-data.com" in url:
            slug = url.rstrip("/").split("/")[-1]
            bg_calls.append(slug)
            d = (TODAY - timedelta(days=7)).isoformat()
            if slug == "pi-cycle":
                return FakeResp([{"d": d, "piSma111": 80000.0, "piSma350x2": 150000.0}])
            return FakeResp([{"d": d, "metric": 0.7}])
        if "coinmetrics.io" in url:
            if cm_mode == "ok":
                return FakeResp({"data": synth_rows()})
            if cm_mode == "429":
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
            if cm_mode == "timeout":
                raise TimeoutError("timed out")
            if cm_mode == "badjson":
                return FakeResp({"unexpected": 1})
            if cm_mode == "stale":
                return FakeResp({"data": synth_rows(TODAY - timedelta(days=20))})
            if cm_mode == "missing":
                return FakeResp({"data": synth_rows(drop="CapMVRVCur")})
            if cm_mode == "short":
                return FakeResp({"data": synth_rows(n=200)})
        raise urllib.error.URLError("mock: offline")
    return urlopen


def run_main(cm_mode, seed_cache=None):
    """Run main() in a temp dir; returns (html, cache dict, stdout, bg_calls)."""
    tmp = tempfile.mkdtemp()
    bg_calls = []
    try:
        shutil.copy(os.path.join(ROOT, "dashboard_template.html"), tmp)
        if seed_cache is None:
            shutil.copy(os.path.join(ROOT, "last_known_good.json"), tmp)
        else:
            with open(os.path.join(tmp, "last_known_good.json"), "w", encoding="utf-8") as f:
                json.dump(seed_cache, f)
        cwd = os.getcwd()
        os.chdir(tmp)
        buf = io.StringIO()
        try:
            with mock.patch("urllib.request.urlopen", make_router(cm_mode, bg_calls)), \
                    mock.patch("time.sleep", lambda *_: None), \
                    mock.patch.object(ud, "BG_KEY", "test-key"), \
                    contextlib.redirect_stdout(buf):
                ud.main()
        finally:
            os.chdir(cwd)
        with open(os.path.join(tmp, "dashboard.html"), encoding="utf-8") as f:
            html = f.read()
        with open(os.path.join(tmp, "last_known_good.json"), encoding="utf-8") as f:
            cache = json.load(f)
        return html, cache, buf.getvalue(), bg_calls
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def row_for(html, name):
    """The <tr> of the first table row whose indicator name starts with `name`."""
    m = re.search(r'<tr><td class="rank-num">\d+</td><td class="ind-name">' + re.escape(name) + r'.*?</tr>', html, flags=re.S)
    return m.group(0) if m else ""


class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "last_known_good.json"), encoding="utf-8") as f:
            cls.seed = json.load(f)
        cls.ok = run_main("ok")
        cls.failures = {m: run_main(m) for m in ("429", "timeout", "badjson", "stale", "missing", "short")}
        # a second run on the cache produced by the good run, with Coin Metrics down -> STALE
        cls.stale_after_ok = run_main("429", seed_cache=cls.ok[1])

    def test_no_leftover_tokens(self):
        for name, (html, *_rest) in [("ok", self.ok)] + list(self.failures.items()) + [("stale-after-ok", self.stale_after_ok)]:
            self.assertEqual(re.findall(r"\{\{[A-Z0-9_]+\}\}", html), [], name)

    def test_bg_calls_drop_by_two(self):
        calls = self.ok[3]
        self.assertNotIn("puell-multiple", calls)
        self.assertNotIn("realized-price", calls)
        self.assertEqual(sorted(calls), sorted(["mvrv-zscore", "reserve-risk", "thermocap-multiple", "lth-sopr",
                                                "nrpl-btc", "supply-profit", "sopr", "pi-cycle"]))
        self.assertEqual(len(calls), 8)       # was 10

    def test_primary_readings_are_coinmetrics_with_true_dates(self):
        html, cache = self.ok[0], self.ok[1]
        yday = (TODAY - timedelta(days=1)).isoformat()
        for name in ("MVRV Z-Score", "Puell Multiple"):
            r = row_for(html, name)
            self.assertIn(f"as of {yday} (1 day old)", r, name)
            self.assertIn("Coin Metrics-derived", r, name)
        self.assertIn("Source: Coin Metrics Community Data", html)
        self.assertIn("CC BY-NC 4.0", html)
        h = cache["MVRV_Z_CM__history"]
        self.assertEqual(h["dates"][-1], yday)
        self.assertEqual(h["source"], "coinmetrics")

    def test_bg_cross_check_caption_secondary(self):
        r = row_for(self.ok[0], "MVRV Z-Score")
        self.assertIn("BGeometrics cross-check (as of", r)
        self.assertIn("7 days old", r)
        self.assertIn("informational, not scored", r)
        self.assertIn("adaptive, shadow, not scored: 1460d =", r)

    def test_arrays_never_mixed(self):
        cache = self.ok[1]
        # BG arrays: Puell / Realized Price frozen exactly as seeded
        for k in ("PUELL__history", "REALIZED_PRICE__history"):
            self.assertEqual(cache[k], self.seed[k], k)
        # BG MVRV array keeps accumulating the BG cross-check value (mock BG value 0.7), not the CM value
        self.assertEqual(cache["MVRV_Z__history"]["values"][-1], 0.7)
        self.assertNotEqual(cache["MVRV_Z_CM__history"]["values"][-1], 0.7)
        seed_vals = self.seed["MVRV_Z__history"]["values"]
        self.assertEqual(cache["MVRV_Z__history"]["values"][:len(seed_vals) - 1], seed_vals[:-1])   # history preserved
        # the CM arrays are separate keys with the CM source marker
        for k in ("MVRV_Z_CM__history", "PUELL_CM__history", "REALIZED_PRICE_CM__history"):
            self.assertEqual(cache[k]["source"], "coinmetrics")
            self.assertNotIn("source", cache.get(k.replace("_CM", ""), {}))

    def test_failure_without_cm_cache_shows_no_reading_and_never_falls_back_to_bg(self):
        for m, (html, cache, out, _calls) in self.failures.items():
            for name in ("MVRV Z-Score", "Puell Multiple"):
                r = row_for(html, name)
                cell = r.split('class="reading">')[1].split("</td>")[0]
                self.assertIn("NO READING TODAY", r, f"{m} {name}")
                self.assertEqual(cell.split("<div")[0], "—", f"{m} {name}")      # no number scored/shown as primary
                self.assertNotIn("Coin Metrics-derived", r, f"{m} {name}")
            self.assertNotIn("Source: Coin Metrics Community Data", html, m)
            self.assertFalse(cache.get("MVRV_Z_CM__history"), m)
            self.assertIn("unavailable", out, m)

    def test_failure_after_good_run_shows_stale_with_data_date(self):
        html, cache, out, _ = self.stale_after_ok
        yday = (TODAY - timedelta(days=1)).isoformat()
        for name in ("MVRV Z-Score", "Puell Multiple"):
            r = row_for(html, name)
            self.assertIn("STALE (", r, name)
            self.assertIn(f"as of {yday}", r, name)
        # arrays untouched by the failed fetch
        self.assertEqual(cache["MVRV_Z_CM__history"], self.ok[1]["MVRV_Z_CM__history"])

    def test_flags_off_means_no_adaptive_or_momentum_scoring(self):
        html = self.ok[0]
        self.assertNotIn("MVRV_MOMENTUM", ud.WEIGHT_MAP_MOMENTUM)
        self.assertIn("fixed; adaptive threshold runs in shadow only, not scored", html)
        self.assertIn("SHADOW, NOT SCORED", html)

    def test_shadow_labels_cannot_read_as_scored(self):
        html = self.ok[0]
        i = html.find("MVRV Momentum (vs. 365d MA)")
        mom = html[html.rfind("<tr", 0, i):html.find("</tr>", i)]
        self.assertIn("SHADOW, NOT SCORED (would read:", mom)
        self.assertIn("shadow, not scored: ≤", mom)                       # target text
        self.assertIn("status-pill st-mid", mom)                                 # neutral pill, not a buy colour
        self.assertEqual(re.findall(r"(?<!NOT )(?<!not )SCORED", mom), [])      # never a bare "SCORED"
        z = row_for(html, "MVRV Z-Score")
        self.assertRegex(z, r"adaptive, shadow, not scored: 1460d = -?\d+\.\d+ -> [a-z ]+</div>")
        self.assertIn('title="shadow, not scored.', z)

    def test_weights_unchanged(self):
        self.assertEqual(ud.WEIGHT_MAP["MVRV_Z"], 3)
        self.assertEqual(ud.WEIGHT_MAP["PUELL"], 3)
        self.assertNotIn("REALIZED_PRICE", ud.WEIGHT_MAP)

    def test_shadow_log_and_verdict_marker(self):
        cache = self.ok[1]
        self.assertEqual(cache["SHADOW_LOG"][-1]["date"], TODAY.isoformat())
        self.assertEqual(set(cache["SHADOW_LOG"][-1]["thresholds"]), {"730", "1460", "full"})
        n_before = len(self.seed["VERDICT_HISTORY"])
        self.assertEqual(cache["VERDICT_HISTORY"][:n_before - 1], self.seed["VERDICT_HISTORY"][:n_before - 1])   # no row rewritten
        self.assertEqual(cache["VERDICT_HISTORY"][-1]["data_source"], "cm-2026-11")
        # running again on the same day replaces, not appends
        again = run_main("ok", seed_cache=cache)
        self.assertEqual(len([e for e in again[1]["SHADOW_LOG"] if e["date"] == TODAY.isoformat()]), 1)


if __name__ == "__main__":
    unittest.main()
