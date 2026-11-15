"""Tests for the display-only Coin Metrics cross-check (shadow mode). Mocked network only.
Run from the repo root:  python -m unittest tests.test_coinmetrics_crosscheck
"""
import json
import os
import re
import sys
import unittest
import urllib.error
from datetime import date, datetime, timedelta, timezone
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import update_dashboard as ud  # noqa: E402
from tests import test_data_as_of as base  # noqa: E402

RUN = date(2026, 11, 15)


def synth_rows(last=date(2026, 11, 14), n=500, drop=None):
    rows = []
    for i in range(n):
        d = last - timedelta(days=n - 1 - i)
        mc = 1.0e12 + 1.0e9 * i
        r = {"asset": "btc", "time": f"{d.isoformat()}T00:00:00.000000000Z",
             "CapMVRVCur": "1.5", "CapMrktCurUSD": str(mc), "IssTotUSD": "30000000", "SplyCur": "19000000"}
        if drop:
            r.pop(drop, None)
        rows.append(r)
    return rows


class TestDerive(unittest.TestCase):
    def test_success_values(self):
        res = ud.derive_coinmetrics_crosscheck(synth_rows(), RUN)
        self.assertEqual(set(res), set(ud.CM_TOKENS))
        z, asof = res["MVRV_Z"]
        self.assertEqual(asof, "2026-11-14")
        self.assertGreater(z, 0)                                  # MC > RC when MVRV > 1
        self.assertAlmostEqual(res["PUELL"][0], 1.0, places=3)     # flat issuance
        mc = 1.0e12 + 1.0e9 * 499
        self.assertEqual(res["REALIZED_PRICE"][0], round(mc / 1.5 / 19000000))

    def test_failures(self):
        with self.assertRaises(ValueError):
            ud.derive_coinmetrics_crosscheck(synth_rows(last=date(2026, 11, 1)), RUN)   # stale
        with self.assertRaises(ValueError):
            ud.derive_coinmetrics_crosscheck(synth_rows(drop="CapMVRVCur"), RUN)        # missing metric
        with self.assertRaises(ValueError):
            ud.derive_coinmetrics_crosscheck([{"time": "x"}], RUN)                       # malformed
        with self.assertRaises(ValueError):
            ud.derive_coinmetrics_crosscheck([], RUN)
        with self.assertRaises(ValueError):
            ud.derive_coinmetrics_crosscheck(synth_rows(n=200), RUN)                     # <365d issuance


class TestCaption(unittest.TestCase):
    def test_ok_and_unavailable(self):
        v = {"CM_XCHECK_STATE": "ok", "CM_XCHECK": {"MVRV_Z": (0.33, "2026-11-14")}}
        c = ud.crosscheck_caption_html(v, "MVRV_Z")
        self.assertIn("derived from Coin Metrics data (as of 2026-11-14): 0.33", c)
        self.assertIn("informational, not scored", c)
        self.assertIn("unavailable", ud.crosscheck_caption_html(v, "PUELL"))
        self.assertEqual(ud.crosscheck_caption_html(v, "RSI"), "")
        self.assertEqual(ud.crosscheck_caption_html({}, "MVRV_Z"), "")
        v2 = {"CM_XCHECK_STATE": "unavailable", "CM_XCHECK": {}}
        self.assertIn("unavailable", ud.crosscheck_caption_html(v2, "MVRV_Z"))


_ORIG_MAKE = base.make_urlopen


def cm_urlopen(mode):
    bg = _ORIG_MAKE((datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat())

    def urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "coinmetrics.io" in url:
            if mode == "ok":
                return base.FakeResp({"data": synth_rows(datetime.now(timezone.utc).date() - timedelta(days=1))})
            if mode == "429":
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
            if mode == "timeout":
                raise TimeoutError("timed out")
            if mode == "badjson":
                return base.FakeResp({"unexpected": 1})
            if mode == "stale":
                return base.FakeResp({"data": synth_rows(datetime.now(timezone.utc).date() - timedelta(days=20))})
            if mode == "missing":
                return base.FakeResp({"data": synth_rows(datetime.now(timezone.utc).date() - timedelta(days=1), drop="CapMVRVCur")})
        return bg(req, timeout)
    return urlopen


def run_mode(mode):
    with mock.patch.object(base, "make_urlopen", lambda bg_date: cm_urlopen(mode)):
        return base.run_main("ignored")


def strip_xcheck(html):
    html = re.sub(r'<div class="xcheck">.*?</div>', "", html)
    html = re.sub(r'<div class="manual-line">Cross-check data:.*?</div>', "", html)
    return base.strip_presentation(html)


class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runs = {m: run_mode(m) for m in ("ok", "429", "timeout", "badjson", "stale", "missing")}

    def test_no_leftover_tokens(self):
        for m, (html, _, _) in self.runs.items():
            self.assertEqual(re.findall(r"\{\{[A-Z0-9_]+\}\}", html), [], m)

    def test_success_shows_three_captions_and_attribution(self):
        html = self.runs["ok"][0]
        self.assertGreaterEqual(html.count("derived from Coin Metrics data (as of"), 3)
        self.assertIn("CC BY-NC 4.0", html)
        self.assertIn("informational, not scored", html)

    def test_each_failure_shows_unavailable_and_no_attribution(self):
        for m in ("429", "timeout", "badjson", "stale", "missing"):
            html = self.runs[m][0]
            self.assertIn("derived from Coin Metrics data: unavailable", html, m)
            self.assertNotIn("derived from Coin Metrics data (as of", html, m)
            self.assertNotIn("CC BY-NC 4.0", html, m)

    def test_scoring_and_history_identical_in_every_mode(self):
        base_html = strip_xcheck(self.runs["ok"][0])
        def hist(c):
            return {k: v for k, v in c.items() if k == "VERDICT_HISTORY" or k.endswith("__history")}
        for m, (html, cache, _) in self.runs.items():
            self.assertEqual(strip_xcheck(html), base_html, m)
            self.assertEqual(hist(cache), hist(self.runs["ok"][1]), m)

    def test_cross_check_never_stored_in_cache(self):
        for m, (_, cache, _) in self.runs.items():
            self.assertFalse([k for k in cache if "CM_" in k or "XCHECK" in k.upper()], m)


if __name__ == "__main__":
    unittest.main()
