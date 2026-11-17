"""Tests for the provisional Cycle Top MVRV Z threshold ceiling (MVRV_Z_TOP_THRESHOLD_CEILING).
No network, no file access. Run from the repo root:

    python -m unittest tests.test_top_threshold_cap
"""
import os
import sys
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import update_dashboard as ud  # noqa: E402

TOP_PCT = 100 - ud.PERCENTILE_CUTOFF


def cache_with(values):
    return {"MVRV_Z_CM__history": {"values": list(values), "dates": []}}


def top_threshold(values, token="MVRV_Z"):
    """Same two steps the Cycle Top block performs: rolling percentile, then the cap."""
    thr, _ = ud.rolling_threshold(cache_with(values), ud.cm_hist_token(token), percentile=TOP_PCT)
    return ud.cap_top_threshold(token, thr)


class TestTopThresholdCap(unittest.TestCase):
    HOT = [0.5] * 600 + [2.71] * 130      # 730 points, 90th percentile = 2.71
    COOL = [0.5] * 400 + [1.0] * 330      # 730 points, 90th percentile = 1.0

    def test_ceiling_constant(self):
        self.assertEqual(ud.MVRV_Z_TOP_THRESHOLD_CEILING, 1.5)

    def test_percentile_fixture_values(self):
        self.assertAlmostEqual(ud.rolling_threshold(cache_with(self.HOT), "MVRV_Z_CM", percentile=TOP_PCT)[0], 2.71, places=6)
        self.assertAlmostEqual(ud.rolling_threshold(cache_with(self.COOL), "MVRV_Z_CM", percentile=TOP_PCT)[0], 1.0, places=6)

    def test_capped_at_ceiling_when_percentile_is_2_71(self):
        self.assertEqual(top_threshold(self.HOT), 1.5)

    def test_equals_percentile_when_below_ceiling(self):
        self.assertAlmostEqual(top_threshold(self.COOL), 1.0, places=6)

    def test_not_applied_to_other_tokens_or_none(self):
        self.assertEqual(ud.cap_top_threshold("PUELL", 2.71), 2.71)
        self.assertEqual(ud.cap_top_threshold("THERMOCAP", 9.0), 9.0)
        self.assertIsNone(ud.cap_top_threshold("MVRV_Z", None))

    def test_oct_2025_top_fires_and_current_reading_does_not(self):
        thr = top_threshold(self.HOT)
        label, css = ud.status_pill(2.53, "high", thr)
        self.assertEqual(css, "st-buy")
        _, css2 = ud.status_pill(0.37, "high", thr)
        self.assertEqual(css2, "st-no")
        # without the cap the Oct 2025 reading would NOT have fired (the problem being fixed)
        _, css_uncapped = ud.status_pill(2.53, "high", 2.71)
        self.assertEqual(css_uncapped, "st-no")


if __name__ == "__main__":
    unittest.main()
