"""strategy_lab 梦境实验室测试：指标计算、变体定义、排名。纯函数，无网络。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import strategy_lab  # noqa: E402


def _records(values):
    return [{"date": f"2026-01-{i+1:02d}", "total_value": v} for i, v in enumerate(values)]


class TestComputeMetrics(unittest.TestCase):
    def test_total_return_and_drawdown(self):
        # 10000 → 11000，中途回撤到 9900（峰值 10500 → -5.71%）
        recs = _records([10000, 10500, 9900, 10800, 11000])
        m = strategy_lab.compute_metrics(recs, budget=10000, trades=[])
        self.assertAlmostEqual(m["total_return_pct"], 10.0, places=4)
        self.assertAlmostEqual(m["max_drawdown_pct"], -5.7143, places=3)
        self.assertGreater(m["sharpe"], 0)

    def test_win_rate_from_trades(self):
        trades = [
            {"action": "sell", "profit_pct": 5.0},
            {"action": "sell", "profit_pct": -2.0},
            {"action": "buy"},
        ]
        m = strategy_lab.compute_metrics(_records([10000, 10100]), 10000, trades)
        self.assertAlmostEqual(m["win_rate"], 0.5)
        self.assertEqual(m["num_trades"], 3)

    def test_flat_series(self):
        m = strategy_lab.compute_metrics(_records([10000, 10000, 10000]), 10000, [])
        self.assertEqual(m["total_return_pct"], 0.0)
        self.assertEqual(m["max_drawdown_pct"], 0.0)
        self.assertEqual(m["sharpe"], 0.0)


class TestVariants(unittest.TestCase):
    def test_baseline_plus_trend_variants(self):
        variants = strategy_lab.make_variants()
        names = [v["name"] for v in variants]
        self.assertIn("baseline-v2.0", names)
        # 至少包含趋势规则变体
        self.assertTrue(any("trend" in n for n in names))
        # 每个变体有 rules dict 且 baseline 不含 trend_exit
        base = next(v for v in variants if v["name"] == "baseline-v2.0")
        self.assertNotIn("trend_exit", base["rules"])
        trend = next(v for v in variants if "trend" in v["name"])
        self.assertTrue(
            trend["rules"].get("trend_exit", {}).get("enabled")
            or trend["rules"].get("trend_filter", {}).get("enabled"))

    def test_variant_rules_are_independent_copies(self):
        variants = strategy_lab.make_variants()
        variants[0]["rules"]["buy_preconditions"]["min_cash_ratio"] = 0.99
        self.assertNotEqual(
            variants[1]["rules"]["buy_preconditions"]["min_cash_ratio"], 0.99)


class TestRanking(unittest.TestCase):
    def test_rank_by_score_desc(self):
        results = [
            {"name": "a", "metrics": {"annual_return_pct": 10.0, "max_drawdown_pct": -20.0}},
            {"name": "b", "metrics": {"annual_return_pct": 12.0, "max_drawdown_pct": -8.0}},
            {"name": "c", "metrics": {"annual_return_pct": -5.0, "max_drawdown_pct": -30.0}},
        ]
        ranked = strategy_lab.rank_results(results)
        self.assertEqual([r["name"] for r in ranked], ["b", "a", "c"])
        for r in ranked:
            self.assertIn("score", r)


if __name__ == "__main__":
    unittest.main()
