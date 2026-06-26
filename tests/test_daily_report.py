"""daily_report 自动记账安全逻辑测试（真金白银，重点测护栏）。stdlib unittest。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from tests._helpers import make_in_memory_db  # noqa: E402
import daily_report  # noqa: E402


def _ctx(db, account_id, actions, funds):
    return {
        "packet": {"actions": actions, "portfolio_snapshot": {"by_position": []}},
        "funds": funds,
        "account_id": account_id,
        "positions": [],
    }


class TestTodayPnl(unittest.TestCase):
    def test_basic(self):
        # 现价 11，涨 10% → 昨收 10，100 份当日盈亏 = (11-10)*100 = 100
        self.assertAlmostEqual(daily_report._today_pnl(100, 11.0, 0.10), 100.0, places=4)

    def test_zero_when_flat(self):
        self.assertEqual(daily_report._today_pnl(100, 11.0, 0.0), 0.0)


class TestAutoRecord(unittest.TestCase):
    def setUp(self):
        import io
        import contextlib
        self.db = make_in_memory_db()
        with contextlib.redirect_stdout(io.StringIO()):
            self.aid = self.db.create_account("主线", "main", 0)
        # 建仓：止盈标的、止损标的
        self.db.set_position(self.aid, "TP001", "止盈基金", 1000, 5.0)
        self.db.set_position(self.aid, "SL001", "止损基金", 1000, 5.0)

    def _run(self, actions, funds):
        return daily_report.auto_record(
            self.db, _ctx(self.db, self.aid, actions, funds),
            "主线", do_email=False,
        )

    def test_take_profit_skipped_no_db_write(self):
        actions = [{"action": "sell", "code": "TP001", "name": "止盈基金",
                    "rule_id": "take_profit_tier_20", "suggested_shares": 250}]
        recorded, skipped = self._run(actions, {"TP001": {"current_nav": 6.0}})
        self.assertEqual(recorded, [])
        self.assertEqual(len(skipped), 1)
        # 止盈不写交易
        self.assertEqual(len(self.db.get_trades(self.aid)), 0)

    def test_stop_loss_executed(self):
        actions = [{"action": "sell", "code": "SL001", "name": "止损基金",
                    "rule_id": "emergency_stop_loss", "suggested_shares": 100}]
        recorded, skipped = self._run(actions, {"SL001": {"current_nav": 4.0}})
        self.assertEqual(len(recorded), 1)
        trades = self.db.get_trades(self.aid)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["action"], "sell")
        self.assertEqual(trades[0]["rule_name"], "emergency_stop_loss")

    def test_qdii_buy_skipped(self):
        # 006479 在 QDII_INDEX_MAP，限购/定投 → 自动加仓跳过
        actions = [{"action": "buy", "code": "006479", "name": "广发纳斯达克100C",
                    "rule_id": "low_buy", "suggested_amount": 1000}]
        recorded, skipped = self._run(actions, {"006479": {"current_nav": 8.0}})
        self.assertEqual(recorded, [])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(len(self.db.get_trades(self.aid)), 0)

    def test_dedup_guard_blocks_repeat(self):
        # 第一次止损成交，第二次同规则 7 天内应被去重跳过（防 runaway）
        actions = [{"action": "sell", "code": "SL001", "name": "止损基金",
                    "rule_id": "emergency_stop_loss", "suggested_shares": 50}]
        funds = {"SL001": {"current_nav": 4.0}}
        r1, s1 = self._run(actions, funds)
        self.assertEqual(len(r1), 1)
        r2, s2 = self._run(actions, funds)
        self.assertEqual(r2, [])
        self.assertEqual(len(s2), 1)
        # 仍只有 1 笔交易
        self.assertEqual(len(self.db.get_trades(self.aid)), 1)


class TestSnapshotReturnExcludesCash(unittest.TestCase):
    """record_daily_snapshot 的 return_pct 必须是「持仓收益率」，现金不计入。

    回归：曾用 含现金的总资产 做分子，导致现金被当成收益（如 +206%）。
    """
    def setUp(self):
        import io
        import contextlib
        self.db = make_in_memory_db()
        with contextlib.redirect_stdout(io.StringIO()):
            self.aid = self.db.create_account("主线", "main", 50000)

    def test_return_pct_is_position_return_not_wallet(self):
        import io
        import contextlib
        # 持仓成本 5000（1000@5），现价 6 → 市值 6000，浮盈 1000；现金 4000；总资产 10000
        ctx = {
            "packet": {"market_regime": {"label": "震荡市"}},
            "account_id": self.aid,
            "total_value": 10000.0,
            "cash": 4000.0,
            "positions": [{"code": "X", "name": "测试", "shares": 1000,
                           "cost_nav": 5.0, "sector": "科技", "is_pending": False}],
            "funds": {"X": {"current_nav": 6.0}},
        }
        with contextlib.redirect_stdout(io.StringIO()):
            daily_report.record_daily_snapshot(self.db, ctx, "2026-06-26")
        row = self.db.conn.execute(
            "SELECT return_pct, total_value FROM daily_snapshots "
            "WHERE account_id=?", (self.aid,)).fetchone()
        # 持仓收益率 = (6000-5000)/5000 = 20%，绝不是 (10000-5000)/5000 = 100%
        self.assertAlmostEqual(row["return_pct"], 20.0, places=2)
        self.assertNotAlmostEqual(row["return_pct"], 100.0, places=1)


class TestCardHoldingsHoldProfit(unittest.TestCase):
    """持仓卡每只要展示「持有收益」金额（= (现价-成本)*份额）。"""
    def test_hold_profit_column(self):
        ctx = {
            "funds": {"X": {"current_nav": 6.0, "day_return": 0.02}},
            "positions": [{"code": "X", "name": "测试基金", "shares": 1000,
                           "cost_nav": 5.0, "hold_days": 10, "is_pending": False}],
            "packet": {"portfolio_snapshot": {"by_position": [
                {"code": "X", "profit_pct": 0.20}]}},
        }
        md = "\n".join(daily_report.card_holdings(ctx))
        self.assertIn("持有收益", md)          # 表头
        self.assertNotIn("昨日盈亏", md)        # 废列已移除
        self.assertIn("+1,000.00", md)         # (6-5)*1000 = 1000 持有收益


class TestDailyPlanCard(unittest.TestCase):
    """今日操作计划：开盘预告 → 盘尾对比（划掉撤销项+说明、维持、新增、确认）。"""
    def setUp(self):
        import io
        import contextlib
        self.db = make_in_memory_db()
        with contextlib.redirect_stdout(io.StringIO()):
            self.aid = self.db.create_account("主线", "main", 50000)

    def _ctx(self, actions, funds, blocked=None):
        return {"packet": {"actions": actions, "blocked_actions": blocked or [],
                           "portfolio_snapshot": {"by_position": []}},
                "funds": funds, "account_id": self.aid, "positions": [],
                "date": "2026-06-26"}

    def test_db_plan_roundtrip_and_earliest(self):
        self.db.save_daily_plan(self.aid, "2026-06-26", "open", [{"code": "X"}])
        self.db.save_daily_plan(self.aid, "2026-06-26", "close", [{"code": "Y"}])
        self.assertEqual(self.db.get_daily_plan(self.aid, "2026-06-26", "open"),
                         [{"code": "X"}])
        # 不指定 session → 取当日最早（open）
        self.assertEqual(self.db.get_daily_plan(self.aid, "2026-06-26"),
                         [{"code": "X"}])

    def test_open_predicts_and_persists(self):
        acts = [{"action": "buy", "code": "540010", "name": "汇丰科技",
                 "rule_id": "low_buy", "rule_label": "低吸", "suggested_amount": 2000,
                 "reason_zh": "当日跌6%", "share_class": {"preferred": "C", "current": None}}]
        md = "\n".join(daily_report.card_plan(
            self._ctx(acts, {"540010": {"current_nav": 6.7, "day_return": -0.06}}),
            "open", self.db, persist=True))
        self.assertIn("开盘预告", md)
        self.assertIn("拟买入「汇丰科技」", md)
        self.assertIn("短线→C类", md)
        # 已落库
        self.assertEqual(len(self.db.get_daily_plan(self.aid, "2026-06-26", "open")), 1)

    def test_close_diff_strikes_dropped_keeps_maintained_adds_new(self):
        # 开盘计划：低吸 540010 + 建仓 660011
        open_acts = [
            {"action": "buy", "code": "540010", "name": "汇丰科技", "rule_id": "low_buy",
             "rule_label": "低吸", "suggested_amount": 2000, "reason_zh": "跌6%",
             "share_class": {"preferred": "C", "current": None}},
            {"action": "buy", "code": "660011", "name": "农银500A", "rule_id": "position_build",
             "rule_label": "分批建仓", "suggested_amount": 2600, "reason_zh": "建仓",
             "share_class": {"preferred": "A", "current": "A"}},
        ]
        daily_report.card_plan(
            self._ctx(open_acts, {"540010": {"day_return": -0.06}, "660011": {"day_return": 0}}),
            "open", self.db, persist=True)
        # 盘尾：540010 回升撤销、660011 维持、新增止损 005825
        close_acts = [
            {"action": "buy", "code": "660011", "name": "农银500A", "rule_id": "position_build",
             "rule_label": "分批建仓", "suggested_amount": 2600, "reason_zh": "建仓",
             "share_class": {"preferred": "A", "current": "A"}},
            {"action": "sell", "code": "005825", "name": "海富通电子", "rule_id": "emergency_stop_loss",
             "rule_label": "紧急止损", "suggested_shares": 100, "reason_zh": "单日跌7%"},
        ]
        md = "\n".join(daily_report.card_plan(
            self._ctx(close_acts, {"540010": {"day_return": 0.005},
                                   "660011": {"day_return": 0}, "005825": {"day_return": -0.07}}),
            "close", self.db, persist=True))
        self.assertIn("~~买入「汇丰科技」", md)   # 撤销项划掉
        self.assertIn("❌ 撤销", md)
        self.assertIn("✅ 维持：买入「农银500A」", md)
        self.assertIn("🆕 新增：卖出「海富通电子」", md)
        self.assertIn("今日最终就这么操作", md)

    def test_persist_false_does_not_save(self):
        acts = [{"action": "buy", "code": "X", "name": "n", "rule_id": "low_buy",
                 "suggested_amount": 100, "reason_zh": "r"}]
        daily_report.card_plan(self._ctx(acts, {"X": {"day_return": -0.05}}),
                               "open", self.db, persist=False)
        self.assertIsNone(self.db.get_daily_plan(self.aid, "2026-06-26", "open"))


if __name__ == "__main__":
    unittest.main()
