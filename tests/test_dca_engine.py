"""P7 引擎感知定投：定投基金排除出所有买入建议，卖出不受影响，
portfolio_advice 文案含定投说明。

设计依据: docs/superpowers/specs/2026-06-13-auto-invest-dca-design.md
定投基金代码通过 market_data['auto_invest_codes'] 传入。

Run with:
  python3 -m unittest tests.test_dca_engine -v
"""
import copy
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from tests._helpers import (  # noqa: E402
    load_rules, make_in_memory_db, add_test_account,
    make_market_data, make_position, find_action,
)

PM_RULES = {
    "enabled": True,
    "target_floor": {"牛市": 0.70, "震荡市": 0.50, "熊市": 0.30},
    "tolerance": 0.05, "batch_fraction": 0.10,
    "max_funds_per_batch": 2, "min_order_amount": 300,
}
SIGNAL_RULES = {"rsi_buy": {"enabled": True, "threshold": 32, "amount_ratio": 0.03}}


class DcaEngineTestBase(unittest.TestCase):
    def setUp(self):
        self.rules = load_rules()
        self.rules["position_management"] = copy.deepcopy(PM_RULES)
        self.rules["signal_rules"] = copy.deepcopy(SIGNAL_RULES)
        self.db = make_in_memory_db()
        self.account_id = add_test_account(self.db, name="test")

    def tearDown(self):
        self.db.close()

    def _decide(self, market_data, positions, cash=10000.0, total_value=10000.0):
        from decision_engine import DecisionEngine
        engine = DecisionEngine(
            self.db, self.account_id,
            strategy_version="v2.0", rules_override=self.rules,
        )
        return engine.decide(
            date="2026-06-13", market_data=market_data, positions=positions,
            cash=cash, total_value=total_value,
        )

    @staticmethod
    def _builds(packet):
        return [a for a in packet["actions"] if a.get("rule_id") == "position_build"]


class DcaExcludesBuysTest(DcaEngineTestBase):
    def test_dca_fund_excluded_from_position_build(self):
        """006479 在定投 → 不进分批建仓候选，名额让给 512480。"""
        md = make_market_data()
        md["auto_invest_codes"] = ["006479"]
        packet = self._decide(md, positions=[])
        codes = [b["code"] for b in self._builds(packet)]
        self.assertNotIn("006479", codes)
        self.assertIn("512480", codes)

    def test_dca_fund_excluded_from_low_buy(self):
        md = make_market_data()
        md["funds"]["512480"]["day_return"] = -0.035
        md["funds"]["512480"]["fund_5d_return"] = -0.06
        md["auto_invest_codes"] = ["512480"]
        packet = self._decide(md, positions=[])
        self.assertEqual(find_action(packet, "512480", "buy"), [])

    def test_dca_fund_excluded_from_signal_buy(self):
        md = make_market_data()
        for f in md["funds"].values():
            f["signals"] = {"rsi_14": 28.0, "macd_hist": 0.0,
                            "ma20_slope": 0.0, "breakout_20d": False}
        md["auto_invest_codes"] = ["512480"]
        packet = self._decide(md, positions=[])
        self.assertEqual(find_action(packet, "512480", "buy"), [])

    def test_non_dca_fund_still_buys(self):
        """未定投的基金照常出 RSI 低吸建议。"""
        md = make_market_data()
        md["funds"]["512480"]["signals"] = {
            "rsi_14": 28.0, "macd_hist": 0.0, "ma20_slope": 0.0,
            "breakout_20d": False}
        md["auto_invest_codes"] = ["006479"]  # 只有 006479 定投
        packet = self._decide(md, positions=[])
        buys = find_action(packet, "512480", "buy")
        self.assertTrue(any(b["rule_id"] == "rsi_oversold_buy" for b in buys))

    def test_dca_does_not_block_sell(self):
        """定投基金触发止损时照常卖出。"""
        md = make_market_data()
        md["funds"]["512480"]["day_return"] = -0.08  # 紧急止损
        md["auto_invest_codes"] = ["512480"]
        positions = [make_position("512480", "半导体ETF国联安",
                                   shares=1000.0, cost_nav=2.34, sector="科技")]
        packet = self._decide(md, positions=positions, cash=7700.0)
        sells = find_action(packet, "512480", "sell")
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["rule_id"], "emergency_stop_loss")


class DcaAdviceTest(DcaEngineTestBase):
    def test_advice_mentions_dca(self):
        md = make_market_data()
        md["auto_invest_codes"] = ["006479"]
        packet = self._decide(md, positions=[])
        self.assertIn("定投", packet["portfolio_advice"]["advice_zh"])

    def test_advice_no_dca_mention_without_plans(self):
        md = make_market_data()
        packet = self._decide(md, positions=[])
        self.assertNotIn("定投", packet["portfolio_advice"]["advice_zh"])


if __name__ == "__main__":
    unittest.main()
