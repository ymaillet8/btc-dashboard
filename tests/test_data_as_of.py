"""Tests for the "data as of" presentation layer in update_dashboard.py.
No real network, no API key, no reads/writes of the repo's last_known_good.json or
dashboard.html (the end-to-end run happens in a temp dir against a mocked urlopen).
Not run by the daily workflow. Run manually from the repo root:

    python -m unittest tests.test_data_as_of
"""
import contextlib
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


class TestAgeMaths(unittest.TestCase):
    def test_age_in_days(self):
        self.assertEqual(ud.data_age_days("2026-11-14", RUN), 1)
        self.assertEqual(ud.data_age_days("2026-11-15", RUN), 0)
        self.assertEqual(ud.data_age_days("2026-11-08", RUN), 7)
        self.assertEqual(ud.data_age_days("2026-11-08T05:00:00+00:00", RUN), 7)

    def test_missing_or_bad_stamp_is_none_not_zero(self):
        for bad in (None, "", "garbage", "2026-13-45"):
            self.assertIsNone(ud.data_age_days(bad, RUN))

    def test_entry_date_extraction(self):
        self.assertEqual(ud._bg_entry_data_date({"d": "2026-11-08", "x": 1}), "2026-11-08")
        ts = int(datetime(2026, 11, 8, tzinfo=timezone.utc).timestamp())
        self.assertEqual(ud._bg_entry_data_date({"unixTs": ts, "x": 1}), "2026-11-08")
        self.assertIsNone(ud._bg_entry_data_date({"x": 1}))          # missing 'd'
        self.assertIsNone(ud._bg_entry_data_date(None))


class TestCaptionAndBanner(unittest.TestCase):
    def vals(self, **kw):
        v = {"MVRV_Z": 0.7, "MVRV_Z_AS_OF": "2026-11-08", "PUELL": 0.8, "PUELL_AS_OF": "2026-11-14"}
        v.update(kw)
        return v

    def test_caption_fresh_vs_warn(self):
        v = self.vals()
        self.assertIn("as of 2026-11-14 (1 day old)", ud.asof_caption_html(v, "PUELL", RUN))
        self.assertNotIn("asof-warn", ud.asof_caption_html(v, "PUELL", RUN))
        c = ud.asof_caption_html(v, "MVRV_Z", RUN)
        self.assertIn("as of 2026-11-08 (7 days old)", c)
        self.assertIn("asof-warn", c)

    def test_boundary_two_days_is_not_warning(self):
        v = self.vals(PUELL_AS_OF="2026-11-13")
        self.assertNotIn("asof-warn", ud.asof_caption_html(v, "PUELL", RUN))
        v = self.vals(PUELL_AS_OF="2026-11-12")
        self.assertIn("asof-warn", ud.asof_caption_html(v, "PUELL", RUN))

    def test_missing_stamp_is_labelled_unknown(self):
        v = self.vals(PUELL_AS_OF=None)
        self.assertIn("as of unknown", ud.asof_caption_html(v, "PUELL", RUN))

    def test_no_caption_for_non_bg_or_missing_reading(self):
        self.assertEqual(ud.asof_caption_html(self.vals(), "RSI", RUN), "")
        self.assertEqual(ud.asof_caption_html(self.vals(MVRV_Z=None), "MVRV_Z", RUN), "")

    def test_banner_only_when_over_two_days(self):
        self.assertEqual(ud.data_age_banner_html({"PUELL": 1, "PUELL_AS_OF": "2026-11-14"}, RUN), "")
        b = ud.data_age_banner_html(self.vals(), RUN)
        self.assertIn("On-chain data from the free BGeometrics tier is 7 days delayed; affected readings are marked.", b)
        self.assertEqual(b.count("DATA FRESHNESS NOTICE"), 1)

    def test_banner_range_when_ages_differ(self):
        v = self.vals(PUELL_AS_OF="2026-11-10")
        self.assertIn("is 5-7 days delayed", ud.data_age_banner_html(v, RUN))

    def test_cache_stores_data_date(self):
        c = {}
        ud.cache_store(c, "X", 1.5, data_date="2026-11-08")
        self.assertEqual(ud.cache_data_date(c, "X"), "2026-11-08")
        ud.cache_store(c, "Y", 1.5)
        self.assertIsNone(ud.cache_data_date(c, "Y"))
        self.assertEqual(ud.cache_lookup(c, "X")[0], 1.5)


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


def make_urlopen(bg_date):
    """Router: BGeometrics rows carry 'd'=bg_date (or no stamp when None). Everything else
    unknown -> URLError, i.e. that source fails exactly as it would offline."""
    now = datetime.now(timezone.utc)

    def urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "bitcoin-data.com" in url or ud.BG_BASE in url:
            if url.rstrip("/").endswith("pi-cycle"):
                row = {"piSma111": 80000.0, "piSma350x2": 150000.0}
            else:
                row = {"metric": 0.7}
            if bg_date:
                row = {"d": bg_date, **row}
            return FakeResp([row])
        raise urllib.error.URLError("mock: offline")

    return urlopen


def run_main(bg_date):
    """Run main() in a temp dir with a mocked network; return (html, cache dict, stdout)."""
    tmp = tempfile.mkdtemp()
    try:
        shutil.copy(os.path.join(ROOT, "dashboard_template.html"), tmp)
        # Seed the temp cache with history only. The repo's live cache also holds dated provider
        # readings; whichever of them is older than the 2-day warn limit on the day the test runs
        # (e.g. a fallback entry from an earlier run) would trigger the banner and make these
        # tests depend on the real calendar date.
        with open(os.path.join(ROOT, "last_known_good.json"), encoding="utf-8") as f:
            real = json.load(f)
        hist_only = {k: v for k, v in real.items() if k == "VERDICT_HISTORY" or k.endswith("__history")}
        with open(os.path.join(tmp, "last_known_good.json"), "w", encoding="utf-8") as f:
            json.dump(hist_only, f)
        cwd = os.getcwd()
        os.chdir(tmp)
        buf = io.StringIO()
        try:
            with mock.patch("urllib.request.urlopen", make_urlopen(bg_date)), \
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
        return html, cache, buf.getvalue()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def strip_presentation(html):
    html = re.sub(r'<div class="asof[^"]*">[^<]*</div>', "", html)
    html = re.sub(r'<div class="master" style="border-color:#4a3a14;[^>]*>.*?DATA FRESHNESS NOTICE.*?</div></div></div></div>',
                  "", html, flags=re.S)
    return html


class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        today = datetime.now(timezone.utc).date()
        cls.fresh = run_main((today - timedelta(days=1)).isoformat())
        cls.stale = run_main((today - timedelta(days=7)).isoformat())
        cls.nostamp = run_main(None)

    def test_no_leftover_template_tokens(self):
        for name in ("fresh", "stale", "nostamp"):
            html = getattr(self, name)[0]
            self.assertEqual(re.findall(r"\{\{[A-Z0-9_]+\}\}", html), [], name)

    def test_stale_run_shows_captions_and_one_banner(self):
        html = self.stale[0]
        self.assertEqual(html.count("DATA FRESHNESS NOTICE"), 1)
        self.assertIn("days delayed; affected readings are marked.", html)
        self.assertIn("asof asof-warn", html)
        self.assertRegex(html, r"as of \d{4}-\d\d-\d\d \(7 days old\)")

    def test_fresh_run_has_captions_but_no_banner(self):
        html = self.fresh[0]
        self.assertNotIn("DATA FRESHNESS NOTICE", html)
        self.assertRegex(html, r"as of \d{4}-\d\d-\d\d \(1 day old\)")
        # no row is flagged as old; the only warning allowed is "unknown" for the Active
        # Addresses fallback to a cache entry written before dates were stored
        self.assertEqual(re.findall(r'asof asof-warn">as of \d{4}[^<]*', html), [])

    def test_missing_stamp_marked_unknown_without_crash(self):
        self.assertIn("as of unknown", self.nostamp[0])
        self.assertNotIn("DATA FRESHNESS NOTICE", self.nostamp[0])

    def test_scoring_identical_regardless_of_data_age(self):
        """Same readings, only the provider date differs: verdict, weights, tiers and the
        persisted history must be byte-identical once captions/banner are stripped."""
        base = strip_presentation(self.fresh[0])
        for other in (self.stale, self.nostamp):
            self.assertEqual(strip_presentation(other[0]), base)

    def test_history_rows_unchanged_by_data_age(self):
        def hist(c):
            return {k: v for k, v in c.items() if k == "VERDICT_HISTORY" or k.endswith("__history")}
        self.assertEqual(hist(self.fresh[1]), hist(self.stale[1]))
        self.assertEqual(hist(self.fresh[1]), hist(self.nostamp[1]))

    def test_cache_records_provider_date(self):
        c = self.stale[1]
        self.assertEqual(ud.cache_data_date(c, "MVRV_Z"),
                         (datetime.now(timezone.utc).date() - timedelta(days=7)).isoformat())


if __name__ == "__main__":
    unittest.main()
