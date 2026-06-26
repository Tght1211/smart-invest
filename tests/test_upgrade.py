"""Tests for the 2026-06 upgrade: 会话对表 / 板块多窗口 / 选基发现 / 短C长A.

All pure functions, no network (discover mocks fetch_fund_rank).
stdlib unittest only.
"""
import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_fund as F
from decision_engine import horizon_for_rule, DecisionEngine

CST = timezone(timedelta(hours=8))


class MarketClockTest(unittest.TestCase):
    def _key(self, h, m, day=26):  # 2026-06-26 is a Friday
        return F.market_clock(datetime(2026, 6, day, h, m, tzinfo=CST))["session_key"]

    def test_sessions_across_the_day(self):
        self.assertEqual(self._key(8, 0), "pre")
        self.assertEqual(self._key(9, 20), "pre")     # 集合竞价仍算盘前
        self.assertEqual(self._key(9, 40), "open")
        self.assertEqual(self._key(11, 45), "lunch")
        self.assertEqual(self._key(13, 30), "mid")
        self.assertEqual(self._key(14, 25), "mid")    # 14:30 前仍是盘中
        self.assertEqual(self._key(14, 35), "close")  # 盘尾下单窗口提前到 14:30
        self.assertEqual(self._key(14, 50), "close")
        self.assertEqual(self._key(15, 30), "after")

    def test_weekend(self):
        c = F.market_clock(datetime(2026, 6, 27, 10, 0, tzinfo=CST))  # Saturday
        self.assertEqual(c["session_key"], "weekend")
        self.assertFalse(c["is_trading_day"])
        self.assertFalse(c["market_open"])

    def test_market_open_flag(self):
        self.assertTrue(F.market_clock(datetime(2026, 6, 26, 10, 0, tzinfo=CST))["market_open"])
        self.assertFalse(F.market_clock(datetime(2026, 6, 26, 12, 0, tzinfo=CST))["market_open"])

    def test_tz_offset_format(self):
        c = F.market_clock(datetime(2026, 6, 26, 10, 0, tzinfo=CST))
        self.assertEqual(c["utc_offset"], "+08:00")
        self.assertEqual(c["weekday_zh"], "周五")
        self.assertEqual(c["date"], "2026-06-26")


class ShareClassDetectTest(unittest.TestCase):
    def test_detect(self):
        cases = {
            "广发纳斯达克100ETF联接人民币(QDII)C": "C",
            "招商中证白酒指数A": "A",
            "汇丰晋信科技先锋股票": None,
            "农银中证500指数A": "A",
            "易方达蓝筹精选": None,
            "中证500ETF": None,      # F 不算份额
            "某基金C类": "C",
            "上证50": None,           # 数字结尾
        }
        for name, exp in cases.items():
            self.assertEqual(F.detect_share_class(name), exp, name)

    def test_base_name_strips_class(self):
        self.assertEqual(
            F.base_fund_name("广发纳斯达克100ETF联接人民币(QDII)C"),
            "广发纳斯达克100ETF联接人民币(QDII)")
        self.assertEqual(F.base_fund_name("招商中证白酒指数A"), "招商中证白酒指数")
        self.assertEqual(F.base_fund_name("汇丰晋信科技先锋股票"), "汇丰晋信科技先锋股票")

    def test_pick_siblings_keeps_currency_distinct(self):
        rows = [
            {"code": "006479", "name": "广发纳斯达克100ETF联接人民币(QDII)C"},
            {"code": "270042", "name": "广发纳斯达克100ETF联接人民币(QDII)A"},
            {"code": "006480", "name": "广发纳斯达克100ETF联接美元(QDII)C"},
        ]
        sib = F.pick_siblings(rows, "广发纳斯达克100ETF联接人民币(QDII)")
        self.assertEqual(sib, {"C": "006479", "A": "270042"})  # 美元版不混入


class WindowReturnsTest(unittest.TestCase):
    def test_compute_window_returns(self):
        closes = list(range(1, 131))  # 1..130 monotonic up
        w = F.compute_window_returns([float(x) for x in closes])
        self.assertAlmostEqual(w["d1"], (130 / 129 - 1) * 100, places=2)
        self.assertAlmostEqual(w["d5"], (130 / 125 - 1) * 100, places=2)
        self.assertIsNotNone(w["d120"])
        self.assertIsNotNone(w["vol30"])

    def test_short_series_returns_none(self):
        w = F.compute_window_returns([1.0, 1.1])
        self.assertIsNone(w["d22"])
        self.assertIsNone(w["d120"])

    def test_classify_strong_trend(self):
        label, _ = F.classify_board_trend({"d1": 1, "d5": 3, "d22": 5, "d120": 10})
        self.assertEqual(label, "强势趋势")

    def test_classify_dead_cat_bounce(self):
        # 今日反弹但 30 日仍下行 → 超跌反弹·谨慎
        label, _ = F.classify_board_trend({"d1": 4, "d5": -1, "d22": -20, "d120": -20})
        self.assertEqual(label, "超跌反弹·谨慎")

    def test_classify_weak(self):
        label, _ = F.classify_board_trend({"d1": -1, "d5": -3, "d22": -5, "d120": -8})
        self.assertEqual(label, "弱势下行")


class DiscoverTest(unittest.TestCase):
    def setUp(self):
        self._orig = F.fetch_fund_rank
        # synthetic universe across sectors
        rows = [
            {"code": "A1", "name": "半导体芯片混合C", "venue": "场外",
             "w_1d": 1, "w_1w": 3, "w_1m": 10, "w_3m": 30, "w_6m": 60, "w_1y": 80},
            {"code": "A2", "name": "半导体设备股票A", "venue": "场外",
             "w_1d": 1, "w_1w": 2, "w_1m": 8, "w_3m": 25, "w_6m": 50, "w_1y": 70},
            {"code": "B1", "name": "白酒消费指数A", "venue": "场外",
             "w_1d": 0, "w_1w": 1, "w_1m": 5, "w_3m": 12, "w_6m": 20, "w_1y": 30},
            {"code": "C1", "name": "纳斯达克100指数C", "venue": "场外",
             "w_1d": 0, "w_1w": 1, "w_1m": 4, "w_3m": 10, "w_6m": 15, "w_1y": 25},
            {"code": "HELD", "name": "已持有科技C", "venue": "场外",
             "w_1d": 9, "w_1w": 40, "w_1m": 5, "w_3m": 5, "w_6m": 5, "w_1y": 5},
        ]
        F.fetch_fund_rank = lambda **kw: rows

    def tearDown(self):
        F.fetch_fund_rank = self._orig

    def test_excludes_held(self):
        cands = F.discover_candidates(exclude={"HELD"}, limit=8)
        self.assertNotIn("HELD", [c["code"] for c in cands])

    def test_cross_sector_diversity(self):
        # per_sector=1 should not return both A1 and A2 (same 科技 sector)
        cands = F.discover_candidates(per_sector=1, limit=8)
        secs = [c["sector"] for c in cands]
        self.assertEqual(len(secs), len(set(secs)))  # one per sector

    def test_sector_filter(self):
        cands = F.discover_candidates(sectors=["半导体"], limit=8, per_sector=5)
        self.assertTrue(all("半导体" in c["name"] for c in cands))

    def test_score_penalizes_overheated_week(self):
        cool = F.score_candidate({"w_6m": 20, "w_3m": 10, "w_1m": 5, "w_1w": 3})
        hot = F.score_candidate({"w_6m": 20, "w_3m": 10, "w_1m": 5, "w_1w": 40})
        self.assertLess(hot, cool)


class HorizonShareClassTest(unittest.TestCase):
    def test_horizon_for_rule(self):
        self.assertEqual(horizon_for_rule("low_buy")[:2], ("short", "C"))
        self.assertEqual(horizon_for_rule("position_build")[:2], ("long", "A"))
        self.assertEqual(horizon_for_rule("low_buy_deferred_drawdown")[:2], ("short", "C"))
        self.assertEqual(horizon_for_rule("emergency_stop_loss"), (None, None, None))

    def test_annotate_only_buys(self):
        eng = DecisionEngine.__new__(DecisionEngine)  # no DB needed
        actions = [
            {"action": "buy", "rule_id": "low_buy", "code": "540010",
             "name": "汇丰晋信科技先锋股票"},
            {"action": "buy", "rule_id": "position_build", "code": "006479",
             "name": "广发纳斯达克100ETF联接人民币(QDII)C"},
            {"action": "sell", "rule_id": "emergency_stop_loss",
             "code": "x", "name": "y"},
        ]
        eng._annotate_horizon(actions)
        self.assertEqual(actions[0]["horizon"], "short")
        self.assertEqual(actions[0]["share_class"]["preferred"], "C")
        # 长线建仓在 C 类基金上 → 应提示改买 A 类兄弟
        self.assertEqual(actions[1]["horizon"], "long")
        self.assertEqual(actions[1]["share_class"]["preferred"], "A")
        self.assertEqual(actions[1]["share_class"]["current"], "C")
        self.assertIn("A", actions[1]["share_class"]["reason_zh"])
        # 卖出不标注
        self.assertNotIn("horizon", actions[2])


class CandidateMomentumTest(unittest.TestCase):
    def test_only_20d_preserves_legacy_order(self):
        # 仅有 20 日时退化为 0.5*w20，排序与原 fund_20d_return 一致
        a = DecisionEngine._candidate_momentum({"fund_20d_return": 0.10})
        b = DecisionEngine._candidate_momentum({"fund_20d_return": 0.05})
        self.assertGreater(a, b)

    def test_multiwindow_consistency_bonus(self):
        consistent = DecisionEngine._candidate_momentum(
            {"fund_20d_return": 0.05, "fund_60d_return": 0.05, "fund_120d_return": 0.05})
        spiky = DecisionEngine._candidate_momentum(
            {"fund_20d_return": 0.05, "fund_60d_return": -0.02, "fund_120d_return": -0.05})
        self.assertGreater(consistent, spiky)

    def test_rank_windows_fallback(self):
        m = DecisionEngine._candidate_momentum(
            {"fund_20d_return": 0.05, "rank_windows": {"w_3m": 30, "w_6m": 60}})
        # 60/120 日从 rank_windows 回退（百分比→小数）
        self.assertGreater(m, 0.05 * 0.5)


if __name__ == "__main__":
    unittest.main()
