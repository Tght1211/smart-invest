"""P6 信号规则测试：rsi_oversold_buy / momentum_breakout / rsi_overbought_trim
+ fund_constraints 限购裁剪。

设计依据: docs/superpowers/specs/2026-06-13-position-mgmt-multi-signal-design.md
信号字段来自 signals.attach_signals()，缺失时规则必须静默跳过。

Run with:
  python3 -m unittest tests.test_signal_rules -v
"""
import copy
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from tests._helpers import (  # noqa: E402
    load_rules, make_in_memory_db, add_test_account,
    make_market_data, make_position, find_action, find_blocked,
)

SIGNAL_RULES = {
    "rsi_buy":      {"enabled": True, "threshold": 32, "amount_ratio": 0.03},
    "breakout_buy": {"enabled": True, "amount_ratio": 0.03},
    "rsi_trim":     {"enabled": True, "threshold": 82,
                     "min_profit": 0.15, "sell_fraction": 0.20},
}

NEUTRAL_SIGNALS = {"rsi_14": 55.0, "macd_hist": 0.0,
                   "ma20_slope": 0.0, "breakout_20d": False}


class SignalRulesTestBase(unittest.TestCase):
    def setUp(self):
        self.rules = load_rules()
        self.rules["signal_rules"] = copy.deepcopy(SIGNAL_RULES)
        self.db = make_in_memory_db()
        self.account_id = add_test_account(self.db, name="test")

    def tearDown(self):
        self.db.close()

    def _decide(self, market_data, positions, cash=5000.0,
                total_value=10000.0, rules=None):
        from decision_engine import DecisionEngine
        engine = DecisionEngine(
            self.db, self.account_id,
            strategy_version="v2.0",
            rules_override=rules if rules is not None else self.rules,
        )
        return engine.decide(
            date="2026-06-13", market_data=market_data, positions=positions,
            cash=cash, total_value=total_value,
        )

    @staticmethod
    def _md_with_signals(**fund_signal_overrides):
        """make_market_data + 每只基金一份中性信号，再按 code 覆盖。"""
        md = make_market_data()
        for code, fund in md["funds"].items():
            fund["signals"] = dict(NEUTRAL_SIGNALS)
            fund["signals"].update(fund_signal_overrides.get(code, {}))
        return md


class RsiBuyTest(SignalRulesTestBase):
    def test_rsi_oversold_triggers_buy(self):
        md = self._md_with_signals(**{"512480": {"rsi_14": 28.0}})
        packet = self._decide(md, positions=[])
        buys = find_action(packet, "512480", "buy")
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["rule_id"], "rsi_oversold_buy")
        self.assertAlmostEqual(buys[0]["suggested_amount"], 300.0, places=2)

    def test_rsi_above_threshold_no_buy(self):
        md = self._md_with_signals(**{"512480": {"rsi_14": 35.0}})
        packet = self._decide(md, positions=[])
        self.assertEqual(find_action(packet, "512480", "buy"), [])

    def test_low_buy_takes_priority_over_rsi(self):
        """当日跌 ≥3% 时 low_buy 接管，同一基金不再出 rsi 买入。"""
        md = self._md_with_signals(**{"512480": {"rsi_14": 28.0}})
        md["funds"]["512480"]["day_return"] = -0.035
        md["funds"]["512480"]["fund_5d_return"] = -0.06
        packet = self._decide(md, positions=[])
        buys = find_action(packet, "512480", "buy")
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["rule_id"], "low_buy")

    def test_missing_signals_silently_skips(self):
        md = make_market_data()  # funds 无 signals 键
        packet = self._decide(md, positions=[])
        self.assertEqual(find_action(packet, "512480", "buy"), [])

    def test_disabled_config_no_buy(self):
        rules = load_rules()  # 无 signal_rules
        md = self._md_with_signals(**{"512480": {"rsi_14": 28.0}})
        packet = self._decide(md, positions=[], rules=rules)
        self.assertEqual(find_action(packet, "512480", "buy"), [])

    def test_fund_constraint_caps_rsi_buy_amount(self):
        self.rules["fund_constraints"] = {"006479": {"max_daily_buy": 10}}
        md = self._md_with_signals(**{"006479": {"rsi_14": 28.0}})
        md["funds"]["006479"]["fund_5d_return"] = 0.0
        packet = self._decide(md, positions=[])
        buys = find_action(packet, "006479", "buy")
        self.assertEqual(len(buys), 1)
        self.assertAlmostEqual(buys[0]["suggested_amount"], 10.0, places=2)


class BreakoutBuyTest(SignalRulesTestBase):
    def test_breakout_with_rising_ma_triggers_buy(self):
        md = self._md_with_signals(
            **{"512480": {"breakout_20d": True, "ma20_slope": 0.002}})
        packet = self._decide(md, positions=[])
        buys = find_action(packet, "512480", "buy")
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["rule_id"], "momentum_breakout")

    def test_breakout_flat_ma_no_buy(self):
        md = self._md_with_signals(
            **{"512480": {"breakout_20d": True, "ma20_slope": -0.001}})
        packet = self._decide(md, positions=[])
        self.assertEqual(find_action(packet, "512480", "buy"), [])

    def test_breakout_blocked_in_bear_market(self):
        md = self._md_with_signals(
            **{"512480": {"breakout_20d": True, "ma20_slope": 0.002}})
        md["hs300_20d_return"] = -0.12
        positions = [make_position("512480", "半导体ETF国联安",
                                   shares=400.0, cost_nav=2.34, sector="科技")]
        packet = self._decide(md, positions=positions)
        self.assertEqual(find_action(packet, "512480", "buy"), [])

    def test_rsi_priority_over_breakout(self):
        md = self._md_with_signals(
            **{"512480": {"rsi_14": 28.0, "breakout_20d": True,
                          "ma20_slope": 0.002}})
        packet = self._decide(md, positions=[])
        buys = find_action(packet, "512480", "buy")
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["rule_id"], "rsi_oversold_buy")

    def test_chase_blocked_breakout(self):
        """5 日涨 12% 的突破 → anti_chase 拦截进 blocked_actions。"""
        md = self._md_with_signals(
            **{"512480": {"breakout_20d": True, "ma20_slope": 0.002}})
        md["funds"]["512480"]["fund_5d_return"] = 0.12
        packet = self._decide(md, positions=[])
        self.assertEqual(find_action(packet, "512480", "buy"), [])
        self.assertTrue(find_blocked(packet, "512480", "anti_chase"))


class RsiTrimTest(SignalRulesTestBase):
    def _pos(self, cost_nav=1.90):
        # current_nav 2.30 → profit ≈ +21%
        return [make_position("512480", "半导体ETF国联安",
                              shares=1000.0, cost_nav=cost_nav, sector="科技")]

    def test_overbought_with_profit_trims(self):
        md = self._md_with_signals(**{"512480": {"rsi_14": 85.0}})
        # 基线 v2.0 规则含分层止盈（+20% 档），rsi_trim 优先级在它之前
        packet = self._decide(md, positions=self._pos(), cash=7700.0)
        sells = find_action(packet, "512480", "sell")
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["rule_id"], "rsi_overbought_trim")
        self.assertAlmostEqual(sells[0]["suggested_shares"], 200.0, places=2)

    def test_overbought_without_profit_no_trim(self):
        md = self._md_with_signals(**{"512480": {"rsi_14": 85.0}})
        packet = self._decide(md, positions=self._pos(cost_nav=2.20),
                              cash=7700.0)
        self.assertEqual(
            [a for a in find_action(packet, "512480", "sell")
             if a["rule_id"] == "rsi_overbought_trim"], [])

    def test_disabled_no_trim(self):
        rules = load_rules()
        rules["take_profit_policy"] = {"mode": "off"}  # 隔离分层止盈
        md = self._md_with_signals(**{"512480": {"rsi_14": 85.0}})
        packet = self._decide(md, positions=self._pos(), cash=7700.0,
                              rules=rules)
        self.assertEqual(find_action(packet, "512480", "sell"), [])

    def test_stop_loss_priority_over_trim(self):
        """单日暴跌触发紧急止损时，止损优先于 rsi_trim。"""
        md = self._md_with_signals(**{"512480": {"rsi_14": 85.0}})
        md["funds"]["512480"]["day_return"] = -0.08
        packet = self._decide(md, positions=self._pos(), cash=7700.0)
        sells = find_action(packet, "512480", "sell")
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["rule_id"], "emergency_stop_loss")


if __name__ == "__main__":
    unittest.main()
