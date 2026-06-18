"""Unit tests for the operation-review / holding-days / returns features.

Covers (all stdlib unittest, no network — fetch_nav_series is mocked):
  - decision_engine.evaluate_trade_timing  (pure timing-verdict function)
  - db.trade_reviews CRUD + upsert + get_review_summary
  - fetch_fund._held_days / _sparkline / _align_total_return_series
  - decide.build_trade_reviews / summarize_reviews

Run with:
  python3 -m unittest tests.test_review -v
"""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from tests._helpers import make_in_memory_db, add_test_account  # noqa: E402


class EvaluateTradeTimingTest(unittest.TestCase):
    """纯函数：买卖择时是否踩中。"""

    def setUp(self):
        from decision_engine import evaluate_trade_timing
        self.ev = evaluate_trade_timing

    def test_buy_up_is_caught(self):
        r = self.ev("buy", 6.5, 6.8, horizon_days=5)
        self.assertEqual(r["verdict"], "踩中")
        self.assertGreater(r["score"], 0)
        self.assertAlmostEqual(r["post_return_pct"], (6.8 - 6.5) / 6.5, places=4)

    def test_buy_down_is_chase(self):
        r = self.ev("buy", 6.5, 6.2, horizon_days=5)
        self.assertEqual(r["verdict"], "追高套牢")
        self.assertLess(r["score"], 0)

    def test_buy_flat_is_neutral(self):
        r = self.ev("buy", 6.50, 6.52, horizon_days=5)  # +0.3% < 2% threshold
        self.assertEqual(r["verdict"], "中性")

    def test_sell_down_is_avoided_drop(self):
        r = self.ev("sell", 1.2, 1.1, horizon_days=5)
        self.assertEqual(r["verdict"], "规避下跌")
        self.assertGreater(r["score"], 0)  # 卖出后跌 = 对的

    def test_sell_up_is_sold_too_early(self):
        r = self.ev("sell", 1.2, 1.35, horizon_days=5)
        self.assertEqual(r["verdict"], "卖飞")
        self.assertLess(r["score"], 0)

    def test_score_is_clamped_to_one(self):
        r = self.ev("buy", 1.0, 2.0, horizon_days=5)  # +100% → 远超 scale
        self.assertEqual(r["score"], 1.0)

    def test_missing_nav_is_data_gap(self):
        for bad in (0, None, -1):
            r = self.ev("buy", bad, 1.0)
            self.assertEqual(r["verdict"], "数据缺失")
            self.assertIsNone(r["score"])
        r2 = self.ev("buy", 1.0, None)
        self.assertEqual(r2["verdict"], "数据缺失")

    def test_unknown_action(self):
        r = self.ev("hold", 1.0, 1.1)
        self.assertEqual(r["verdict"], "未知方向")
        self.assertIsNone(r["score"])

    def test_lesson_is_nonempty_string(self):
        r = self.ev("buy", 6.5, 6.8, horizon_days=7)
        self.assertIn("买入", r["lesson"])
        self.assertIn("7天", r["lesson"])


class TradeReviewDbTest(unittest.TestCase):
    """trade_reviews 表 CRUD + upsert + 汇总。"""

    def setUp(self):
        self.db = make_in_memory_db()
        self.aid = add_test_account(self.db, name="rev")

    def tearDown(self):
        self.db.close()

    def _add(self, trade_id, action, verdict, score, horizon=7):
        self.db.add_trade_review(
            self.aid, trade_id, "006479", "纳指100C", action, "2026-06-01",
            horizon, 7.0, 7.5, "2026-06-08", 0.07, verdict, score, "教训x")

    def test_add_and_get(self):
        self._add(1, "buy", "踩中", 0.7)
        rows = self.db.get_trade_reviews(self.aid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "踩中")
        self.assertEqual(rows[0]["code"], "006479")

    def test_upsert_dedup_by_trade_and_horizon(self):
        self._add(1, "buy", "踩中", 0.7)
        self._add(1, "buy", "追高套牢", -0.4)  # 同 trade_id+horizon → 覆盖
        rows = self.db.get_trade_reviews(self.aid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "追高套牢")
        self.assertAlmostEqual(rows[0]["score"], -0.4)

    def test_different_horizon_keeps_both(self):
        self._add(1, "buy", "踩中", 0.7, horizon=5)
        self._add(1, "buy", "踩中", 0.6, horizon=14)
        self.assertEqual(len(self.db.get_trade_reviews(self.aid)), 2)

    def test_summary_winrates(self):
        self._add(1, "buy", "踩中", 0.8)
        self._add(2, "buy", "追高套牢", -0.3)
        self._add(3, "sell", "规避下跌", 0.6)
        s = self.db.get_review_summary(self.aid)
        self.assertEqual(s["count"], 3)
        self.assertEqual(s["buy_count"], 2)
        self.assertEqual(s["sell_count"], 1)
        self.assertAlmostEqual(s["buy_timing_winrate"], 0.5)   # 1 win / 2
        self.assertAlmostEqual(s["sell_timing_winrate"], 1.0)  # 1 win / 1
        self.assertEqual(s["verdict_counts"]["踩中"], 1)

    def test_summary_empty(self):
        self.assertEqual(self.db.get_review_summary(self.aid), {"count": 0})


class FetchFundHelpersTest(unittest.TestCase):
    """持有天数 / sparkline / 总收益序列对齐（纯函数，无网络）。"""

    def setUp(self):
        import fetch_fund
        self.ff = fetch_fund

    def test_held_days_formats(self):
        ref = datetime(2026, 6, 11)
        self.assertEqual(self.ff._held_days("2026-06-01", ref=ref), 10)
        self.assertEqual(self.ff._held_days("2026/06/01", ref=ref), 10)
        self.assertEqual(self.ff._held_days("20260601", ref=ref), 10)
        self.assertEqual(self.ff._held_days("2026-06-01T00:00:00", ref=ref), 10)

    def test_held_days_invalid(self):
        self.assertIsNone(self.ff._held_days(None))
        self.assertIsNone(self.ff._held_days(""))
        self.assertIsNone(self.ff._held_days("not-a-date"))

    def test_sparkline_length_and_empty(self):
        self.assertEqual(self.ff._sparkline([]), "")
        s = self.ff._sparkline([1, 2, 3, 4, 5])
        self.assertEqual(len(s), 5)
        # 递增序列：最后一个 bar 应是最高
        self.assertEqual(s[-1], "█")

    def test_sparkline_flat(self):
        s = self.ff._sparkline([3, 3, 3])
        self.assertEqual(len(s), 3)

    def test_align_total_return_series(self):
        # 两只基金，份额相同，成本 1.0，序列在同样日期上涨 → 总收益率单调
        s1 = [("2026-06-01", 1.0), ("2026-06-02", 1.1), ("2026-06-03", 1.2)]
        s2 = [("2026-06-01", 1.0), ("2026-06-02", 1.0), ("2026-06-03", 1.0)]
        out = self.ff._align_total_return_series([(100, 1.0, s1), (100, 1.0, s2)])
        self.assertEqual(len(out), 3)
        # day1: (110+100 - 200)/200 vs cost: 第一天 (100+100-200)/200 = 0
        self.assertAlmostEqual(out[0][1], 0.0, places=6)
        # day3: (120+100-200)/200 = 0.10
        self.assertAlmostEqual(out[-1][1], 0.10, places=6)

    def test_align_total_return_empty(self):
        self.assertEqual(self.ff._align_total_return_series([]), [])
        self.assertEqual(self.ff._align_total_return_series([(0, 1.0, [])]), [])


class BuildTradeReviewsTest(unittest.TestCase):
    """decide.build_trade_reviews：拉历史交易→评定（fetch_nav_series 被 mock）。"""

    def setUp(self):
        self.db = make_in_memory_db()
        self.aid = add_test_account(self.db, name="主线")

    def tearDown(self):
        self.db.close()

    def _seed(self):
        today = datetime.now().date()
        d = lambda n: (today - timedelta(days=n)).isoformat()
        # 够老（>=7天）可评定
        self.db.add_trade(self.aid, d(30), "006479", "纳指100C", "buy", 2000, 7.0, 285)
        self.db.add_trade(self.aid, d(20), "512480", "半导体", "sell", 1500, 1.3, 1153)
        # 太新（<7天）→ pending
        self.db.add_trade(self.aid, d(2), "660011", "中证500", "buy", 2000, 2.0, 1000)

    def _fake_series(self, code, days=60):
        """每只基金：今天往前 days 天，净值从 1.0 线性涨到 2.0（递增）。"""
        today = datetime.now().date()
        out = []
        for i in range(days, -1, -1):
            dt = (today - timedelta(days=i)).isoformat()
            nav = 1.0 + (days - i) / days  # 升序、递增
            out.append((dt, round(nav, 4)))
        return out

    def test_build_splits_ready_and_pending(self):
        self._seed()
        import decide
        with mock.patch.object(decide.fetch_fund, "fetch_nav_series",
                               side_effect=self._fake_series):
            results, pending = decide.build_trade_reviews(
                self.db, self.aid, horizon=7, lookback=60, save=False)
        self.assertEqual(pending, 1)        # 最近的那笔太新
        self.assertEqual(len(results), 2)   # 两笔可评定
        for r in results:
            self.assertIn("verdict", r)
            self.assertIn("badge", r)
            self.assertIsNotNone(r["post_return_pct"])

    def test_build_save_persists_memory(self):
        self._seed()
        import decide
        with mock.patch.object(decide.fetch_fund, "fetch_nav_series",
                               side_effect=self._fake_series):
            decide.build_trade_reviews(
                self.db, self.aid, horizon=7, lookback=60, save=True)
        stored = self.db.get_trade_reviews(self.aid)
        self.assertEqual(len(stored), 2)
        summary = self.db.get_review_summary(self.aid)
        self.assertEqual(summary["count"], 2)

    def test_summarize_reviews_pure(self):
        from decide import summarize_reviews
        results = [
            {"action": "buy", "score": 0.8},
            {"action": "buy", "score": -0.2},
            {"action": "sell", "score": 0.5},
            {"action": "sell", "score": None},  # 数据缺失，忽略
        ]
        s = summarize_reviews(results)
        self.assertEqual(s["count"], 3)
        self.assertAlmostEqual(s["buy_timing_winrate"], 0.5)
        self.assertAlmostEqual(s["sell_timing_winrate"], 1.0)

    def test_summarize_empty(self):
        from decide import summarize_reviews
        self.assertEqual(summarize_reviews([]), {"count": 0})
        self.assertEqual(summarize_reviews([{"action": "buy", "score": None}]),
                         {"count": 0})


if __name__ == "__main__":
    unittest.main()
