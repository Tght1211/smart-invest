"""Tests for scripts/signals.py — RSI / MACD / MA slope / breakout."""
import math
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


class SignalsTest(unittest.TestCase):
    def test_rsi_neutral_when_flat(self):
        from signals import compute_rsi
        navs = [1.0] * 20
        rsi = compute_rsi(navs, period=14)
        # All-flat → average gain = 0, average loss = 0 → undefined, defined as 50
        self.assertEqual(rsi, 50.0)

    def test_rsi_high_on_uptrend(self):
        from signals import compute_rsi
        # 15 days of 1% gain each
        navs = [1.0 + 0.01 * i for i in range(20)]
        rsi = compute_rsi(navs, period=14)
        # Only gains → RSI should be 100
        self.assertEqual(rsi, 100.0)

    def test_rsi_low_on_downtrend(self):
        from signals import compute_rsi
        # 15 days of 1% loss each
        navs = [1.0 - 0.01 * i for i in range(20)]
        rsi = compute_rsi(navs, period=14)
        # Only losses → RSI should be 0
        self.assertEqual(rsi, 0.0)

    def test_rsi_returns_none_on_too_few(self):
        from signals import compute_rsi
        self.assertIsNone(compute_rsi([1.0, 1.1, 1.05], period=14))

    def test_macd_hist_positive_on_recent_acceleration(self):
        from signals import compute_macd
        # Slow uptrend for 40 days, then 5 days of fast spike
        navs = [1.0 + 0.001 * i for i in range(40)]
        navs += [navs[-1] * (1 + 0.05) ** (i + 1) for i in range(5)]
        macd, signal, hist = compute_macd(navs)
        self.assertGreater(hist, 0,
                           f"expected positive hist after acceleration, got {hist}")

    def test_macd_returns_none_on_too_few(self):
        from signals import compute_macd
        self.assertEqual(compute_macd([1.0] * 10), (None, None, None))

    def test_ma_slope_positive_uptrend(self):
        from signals import compute_ma_slope
        navs = [1.0 + 0.005 * i for i in range(30)]
        slope = compute_ma_slope(navs, window=20, lookback=5)
        self.assertIsNotNone(slope)
        self.assertGreater(slope, 0)

    def test_ma_slope_negative_downtrend(self):
        from signals import compute_ma_slope
        navs = [1.0 - 0.005 * i for i in range(30)]
        slope = compute_ma_slope(navs, window=20, lookback=5)
        self.assertLess(slope, 0)

    def test_breakout_true_on_new_high(self):
        from signals import compute_breakout
        navs = [1.0 + 0.001 * i for i in range(20)] + [1.05]
        self.assertTrue(compute_breakout(navs, lookback=20))

    def test_breakout_false_when_below_high(self):
        from signals import compute_breakout
        navs = [1.10] + [1.0 + 0.001 * i for i in range(20)]
        # latest is 1.019, prior 20-day high is 1.10 — no breakout
        self.assertFalse(compute_breakout(navs, lookback=20))


if __name__ == "__main__":
    unittest.main()
