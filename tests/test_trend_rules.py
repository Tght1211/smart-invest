"""P5 趋势规则测试：200日均线状态 + trend_exit 卖出 + low_buy 趋势闸门。

依据（docs 调研 2026-06-10）：Faber 10月线/200日线趋势过滤；
QQQ 2000-2024 策略791% vs 持有428%，回撤28.6% vs 83%。
规则全部参数化（rules["trend_exit"] / rules["trend_filter"]），默认缺省=关闭，
不改变 v2.0 行为；由 strategy_lab 回测验证后才在 v2.1 启用。
"""
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from tests._helpers import (  # noqa: E402
    load_rules, make_in_memory_db, add_test_account,
    make_market_data, make_position, find_action,
)
import signals  # noqa: E402


class TestComputeMaState(unittest.TestCase):
    def test_insufficient_data_returns_none(self):
        self.assertIsNone(signals.compute_ma_state([1.0] * 10, window=200))

    def test_above_ma(self):
        closes = list(range(1, 251))  # 单边上涨，收盘远在 MA 上方
        st = signals.compute_ma_state([float(x) for x in closes], window=200)
        self.assertTrue(st["above"])
        self.assertEqual(st["below_days"], 0)
        self.assertGreater(st["gap_pct"], 0)

    def test_below_days_counts_consecutive(self):
        # 200 天平稳在 100，然后连跌 3 天到 80 → 连续 3 天破线（含 1% buffer）
        closes = [100.0] * 200 + [85.0, 82.0, 80.0]
        st = signals.compute_ma_state(closes, window=200, buffer=0.01)
        self.assertFalse(st["above"])
        self.assertEqual(st["below_days"], 3)
        self.assertLess(st["gap_pct"], -0.01)

    def test_buffer_tolerates_shallow_dip(self):
        # 跌破不足 buffer 幅度 → 不计破线天数
        closes = [100.0] * 200 + [99.8]
        st = signals.compute_ma_state(closes, window=200, buffer=0.01)
        self.assertEqual(st["below_days"], 0)


TREND_RULES = {
    "trend_exit": {
        "enabled": True, "confirm_days": 2, "sell_fraction": 0.5,
    },
    "trend_filter": {
        "enabled": True, "low_buy_factor": 0.5,
    },
}


class TrendRuleTestBase(unittest.TestCase):
    def setUp(self):
        self.rules = load_rules()
        self.rules.update({k: dict(v) for k, v in TREND_RULES.items()})
        self.db = make_in_memory_db()
        self.account_id = add_test_account(self.db, name="test")

    def tearDown(self):
        self.db.close()

    def _decide(self, market_data, positions, cash=5000.0,
                total_value=10000.0, rules=None):
        from decision_engine import DecisionEngine
        engine = DecisionEngine(
            self.db, self.account_id,
            strategy_version="v2.0", rules_override=rules or self.rules,
        )
        return engine.decide(
            date="2026-06-10", market_data=market_data, positions=positions,
            cash=cash, total_value=total_value,
        )


class TestTrendExit(TrendRuleTestBase):
    def _md_ndx_broken(self, below_days=2):
        md = make_market_data()
        md["index_trend"] = {
            "NDX": {"ma": 26000.0, "gap_pct": -0.03,
                    "below_days": below_days, "above": False},
            "HS300": {"ma": 3900.0, "gap_pct": 0.02,
                      "below_days": 0, "above": True},
        }
        md["funds"]["006479"]["ref_index"] = "NDX"
        return md

    def _pos_qdii(self):
        return [make_position("006479", "广发纳斯达克100C",
                              2400, 6.5, "海外", hold_days=120)]

    def test_fires_after_confirm_days(self):
        packet = self._decide(self._md_ndx_broken(below_days=2), self._pos_qdii())
        sells = find_action(packet, "006479", "sell")
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["rule_id"], "trend_exit_ma200")
        # sell_fraction=0.5 → 一半份额
        self.assertAlmostEqual(sells[0]["suggested_shares"], 1200, delta=1)

    def test_not_fired_before_confirm(self):
        packet = self._decide(self._md_ndx_broken(below_days=1), self._pos_qdii())
        self.assertEqual(
            [a for a in find_action(packet, "006479", "sell")
             if a["rule_id"] == "trend_exit_ma200"], [])

    def test_fires_only_on_crossing_day_no_daily_refire(self):
        # 破位第 5 天（已过确认日）→ 不再重复减仓，防震荡市 whipsaw
        packet = self._decide(self._md_ndx_broken(below_days=5), self._pos_qdii())
        self.assertEqual(
            [a for a in find_action(packet, "006479", "sell")
             if a["rule_id"] == "trend_exit_ma200"], [])

    def test_disabled_by_default_rules(self):
        plain = load_rules()  # v2.0 无 trend_exit 配置
        packet = self._decide(self._md_ndx_broken(below_days=2),
                              self._pos_qdii(), rules=plain)
        self.assertEqual(
            [a for a in find_action(packet, "006479", "sell")
             if a["rule_id"] == "trend_exit_ma200"], [])

    def test_stop_loss_takes_precedence(self):
        md = self._md_ndx_broken(below_days=2)
        md["funds"]["006479"]["day_return"] = -0.08  # 触发紧急止损
        packet = self._decide(md, self._pos_qdii())
        sells = find_action(packet, "006479", "sell")
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["rule_id"], "emergency_stop_loss")

    def test_a_share_falls_back_to_hs300_trend(self):
        # A股基金无 ref_index → 用 HS300 趋势；HS300 完好 → 不触发
        md = self._md_ndx_broken(below_days=3)
        positions = [make_position("512480", "半导体ETF国联安",
                                   1000, 2.5, "科技", hold_days=60)]
        packet = self._decide(md, positions)
        self.assertEqual(
            [a for a in find_action(packet, "512480", "sell")
             if a["rule_id"] == "trend_exit_ma200"], [])


class TestTakeProfitPolicy(TrendRuleTestBase):
    """take_profit_policy.mode=off → 让利润奔跑（线上 LET_WINNERS_RUN 的引擎级形态）。"""

    def _pos_winner(self):
        # 成本 6.5 现价 8.2 → +26%，会触发止盈首档
        return [make_position("006479", "广发纳斯达克100C",
                              2400, 6.5, "海外", hold_days=120)]

    def test_off_mode_suppresses_take_profit(self):
        rules = load_rules()
        rules["take_profit_policy"] = {"mode": "off"}
        packet = self._decide(make_market_data(), self._pos_winner(), rules=rules)
        self.assertEqual(find_action(packet, "006479", "sell"), [])

    def test_default_tiers_still_fire(self):
        packet = self._decide(make_market_data(), self._pos_winner(),
                              rules=load_rules())
        sells = find_action(packet, "006479", "sell")
        self.assertEqual(len(sells), 1)
        self.assertTrue(sells[0]["rule_id"].startswith("take_profit"))

    def test_off_mode_keeps_trend_exit(self):
        rules = load_rules()
        rules["take_profit_policy"] = {"mode": "off"}
        rules["trend_exit"] = {"enabled": True, "confirm_days": 2,
                               "sell_fraction": 0.5}
        md = make_market_data()
        md["index_trend"] = {"NDX": {"ma": 26000.0, "gap_pct": -0.03,
                                     "below_days": 2, "above": False}}
        md["funds"]["006479"]["ref_index"] = "NDX"
        packet = self._decide(md, self._pos_winner(), rules=rules)
        sells = find_action(packet, "006479", "sell")
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["rule_id"], "trend_exit_ma200")


class TestLowBuyTrendGate(TrendRuleTestBase):
    def _md_low_buy(self, hs300_above):
        md = make_market_data()
        md["funds"]["512480"]["day_return"] = -0.04   # 触发低吸
        md["index_trend"] = {
            "HS300": {"ma": 3900.0, "gap_pct": 0.02 if hs300_above else -0.04,
                      "below_days": 0 if hs300_above else 5,
                      "above": hs300_above},
        }
        return md

    def test_halved_when_hs300_below_ma(self):
        above = self._decide(self._md_low_buy(True), positions=[])
        below = self._decide(self._md_low_buy(False), positions=[])
        buy_above = find_action(above, "512480", "buy")
        buy_below = find_action(below, "512480", "buy")
        self.assertEqual(len(buy_above), 1)
        self.assertEqual(len(buy_below), 1)
        self.assertAlmostEqual(
            buy_below[0]["suggested_amount"],
            buy_above[0]["suggested_amount"] * 0.5, places=2)
        # 理由里说明被趋势闸门打折
        self.assertIn("200日线", buy_below[0]["reason_zh"])

    def test_no_gate_when_filter_absent(self):
        plain = load_rules()
        below = self._decide(self._md_low_buy(False), positions=[], rules=plain)
        above = self._decide(self._md_low_buy(True), positions=[], rules=plain)
        self.assertAlmostEqual(
            find_action(below, "512480", "buy")[0]["suggested_amount"],
            find_action(above, "512480", "buy")[0]["suggested_amount"],
            places=2)


class TestDefaultVersionFollowsFile(unittest.TestCase):
    """引擎默认 strategy_version 跟随 data/decision_tree.json 的 version 字段，
    这样 strategy_lab --promote 更新文件后，decide.py/daily_report 自动用新版。"""

    def test_default_version_matches_live_tree_file(self):
        import json as _json
        from decision_engine import DecisionEngine
        db = make_in_memory_db()
        aid = add_test_account(db, name="t2")
        with open(REPO_ROOT / "data" / "decision_tree.json", encoding="utf-8") as f:
            file_version = _json.load(f).get("version")
        engine = DecisionEngine(db, aid)  # 不指定版本
        self.assertEqual(engine.strategy_version, file_version)
        db.close()


if __name__ == "__main__":
    unittest.main()
