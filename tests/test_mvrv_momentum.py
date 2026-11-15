"""Unit tests for update_dashboard.get_mvrv_momentum_trend() and
classify_mvrv_momentum(). Synthetic data only: no network, no API key, no
reads or writes of last_known_good.json. Not imported by update_dashboard.py
and not run by the daily workflow. Run manually from the repo root:

    python -m unittest tests.test_mvrv_momentum
    python tests/test_mvrv_momentum.py
"""
import os
import sys
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import update_dashboard as ud  # noqa: E402

FIRING = ("st-strong-buy", "st-buy", "st-near")


def cache_of(values):
    return {"MVRV_Z__history": {"values": list(values), "dates": []}}


def classify(values, current=None):
    current = values[-1] if current is None and values else current
    return ud.classify_mvrv_momentum(cache_of(values), current)


class TestClassifyMvrvMomentum(unittest.TestCase):
    def test_falling_below_ma_fires(self):
        vals = [2.0] * 305 + [2.0 - 1.5 * i / 59 for i in range(60)]
        label, css, ma, n, today, past = classify(vals)
        self.assertIn(css, FIRING)
        self.assertEqual(n, 365)
        self.assertLess(today, past)

    def test_flat_below_ma_does_not_fire(self):
        vals = [2.0] * 305 + [0.5] * 60
        label, css, ma, n, today, past = classify(vals)
        self.assertEqual(css, "st-no")
        self.assertTrue(label.startswith("BELOW MA, NOT DECELERATING"))

    def test_rising_below_ma_does_not_fire(self):
        vals = [2.0] * 305 + [0.3 + 0.01 * i for i in range(60)]
        label, css, ma, n, today, past = classify(vals)
        self.assertEqual(css, "st-no")
        self.assertGreater(today, past)

    def test_above_ma_does_not_fire(self):
        vals = [1.0] * 305 + [3.0] * 60
        self.assertNotIn(classify(vals)[1], FIRING)

    def test_under_365_days_returns_no_reading(self):
        for n in (0, 40, 100, 364):
            label, css, ma, ma_n, today, past = classify([1.0] * n, 1.0)
            self.assertEqual((label, css, ma, today, past), (None, None, None, None, None))
            self.assertEqual(ma_n, n)

    def test_short_trend_history_reports_building_trend(self):
        # Level gate forced open while trend history is still short (cannot
        # happen with the real 365/40 constants; guards the defensive branch).
        orig = ud.get_mvrv_momentum_ma
        ud.get_mvrv_momentum_ma = lambda cache, *a, **k: (2.0, len(cache["MVRV_Z__history"]["values"]))
        try:
            label, css, ma, n, today, past = classify([0.5] * 20, 0.5)
        finally:
            ud.get_mvrv_momentum_ma = orig
        self.assertTrue(label.startswith("BUILDING TREND HISTORY"))
        self.assertEqual(css, "st-mid")
        self.assertIsNone(today)
        self.assertIsNone(past)

    def test_missing_or_bad_inputs_do_not_raise(self):
        fall = [2.0] * 305 + [2.0 - 1.5 * i / 59 for i in range(60)]
        self.assertEqual(ud.classify_mvrv_momentum(cache_of(fall), None)[:2], (None, None))
        self.assertEqual(ud.classify_mvrv_momentum({}, 0.5)[:2], (None, None))
        self.assertEqual(ud.classify_mvrv_momentum(cache_of([]), 0.5)[:2], (None, None))
        self.assertEqual(ud.classify_mvrv_momentum({"MVRV_Z__history": {}}, 0.5)[:2], (None, None))
        self.assertEqual(ud.classify_mvrv_momentum(cache_of(fall), "abc")[:2], ("CHECK", "st-mid"))


class TestGetMvrvMomentumTrend(unittest.TestCase):
    def test_below_gate_returns_none(self):
        self.assertEqual(ud.get_mvrv_momentum_trend(cache_of([1.0] * 39)), (None, None, None, 39))
        self.assertEqual(ud.get_mvrv_momentum_trend({}), (None, None, None, 0))

    def test_at_gate_flat_is_not_falling(self):
        self.assertEqual(ud.get_mvrv_momentum_trend(cache_of([1.0] * 40)), (False, 1.0, 1.0, 40))

    def test_falling_and_rising(self):
        down = ud.get_mvrv_momentum_trend(cache_of([2.0] * 30 + [1.0] * 10))
        up = ud.get_mvrv_momentum_trend(cache_of([1.0] * 30 + [2.0] * 10))
        self.assertTrue(down[0])
        self.assertFalse(up[0])


if __name__ == "__main__":
    unittest.main()
