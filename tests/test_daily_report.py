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


if __name__ == "__main__":
    unittest.main()
