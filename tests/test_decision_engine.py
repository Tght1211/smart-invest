"""Unit tests for decision_engine.DecisionEngine.

Schema reference:
  docs/superpowers/specs/2026-05-28-smart-invest-overhaul-design.md §6
Test matrix:
  docs/superpowers/specs/2026-05-28-smart-invest-overhaul-design.md §10

Run with:
  python3 -m unittest tests.test_decision_engine -v
"""
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from tests._helpers import (  # noqa: E402
    load_rules, make_in_memory_db, add_test_account,
    make_market_data, make_position, find_action, find_blocked,
)


TOP_LEVEL_KEYS = {
    "schema_version", "generated_at", "account", "date", "rule_version",
    "market_regime", "portfolio_snapshot", "actions", "blocked_actions",
    "alerts", "summary",
}


class DecisionEngineTestBase(unittest.TestCase):
    """Base class providing fresh DB + engine + helpers per test."""

    def setUp(self):
        self.rules = load_rules()
        self.db = make_in_memory_db()
        self.account_id = add_test_account(self.db, name="test")

    def tearDown(self):
        self.db.close()

    def _decide(self, market_data, positions, cash=5000.0,
                total_value=10000.0, date="2026-05-28"):
        from decision_engine import DecisionEngine
        engine = DecisionEngine(
            self.db, self.account_id,
            strategy_version="v2.0", rules_override=self.rules,
        )
        return engine.decide(
            date=date, market_data=market_data, positions=positions,
            cash=cash, total_value=total_value,
        )


class PacketSchemaTest(DecisionEngineTestBase):
    def test_decide_packet_has_all_top_level_keys(self):
        packet = self._decide(make_market_data(), positions=[])
        missing = TOP_LEVEL_KEYS - set(packet.keys())
        self.assertEqual(missing, set(), f"missing top-level keys: {missing}")
        self.assertEqual(packet["schema_version"], "1.0")
        self.assertEqual(packet["account"], "test")
        self.assertEqual(packet["date"], "2026-05-28")
        self.assertEqual(packet["rule_version"], "v2.0")
        self.assertIsInstance(packet["actions"], list)
        self.assertIsInstance(packet["blocked_actions"], list)
        self.assertIsInstance(packet["alerts"], list)


class PortfolioSnapshotTest(DecisionEngineTestBase):
    def test_snapshot_empty(self):
        packet = self._decide(make_market_data(), positions=[],
                              cash=10000.0, total_value=10000.0)
        snap = packet["portfolio_snapshot"]
        self.assertEqual(snap["total_value"], 10000.0)
        self.assertEqual(snap["cash"], 10000.0)
        self.assertEqual(snap["cash_pct"], 1.0)
        self.assertEqual(snap["position_value"], 0.0)
        self.assertEqual(snap["sectors"], {})
        self.assertEqual(snap["by_position"], [])

    def test_snapshot_single_position(self):
        positions = [make_position(
            "512480", "半导体ETF国联安",
            shares=1000.0, cost_nav=2.34, sector="科技",
        )]
        packet = self._decide(make_market_data(), positions=positions,
                              cash=7700.0, total_value=10000.0)
        snap = packet["portfolio_snapshot"]
        # 1000 shares * 2.30 (current_nav from market_data) = 2300
        self.assertAlmostEqual(snap["position_value"], 2300.0, places=2)
        self.assertAlmostEqual(snap["cash_pct"], 0.77, places=3)
        self.assertAlmostEqual(snap["sectors"]["科技"], 0.23, places=3)
        self.assertEqual(len(snap["by_position"]), 1)
        p = snap["by_position"][0]
        self.assertEqual(p["code"], "512480")
        self.assertEqual(p["shares"], 1000.0)
        self.assertEqual(p["cost_nav"], 2.34)
        self.assertEqual(p["current_nav"], 2.30)
        self.assertAlmostEqual(p["pct_of_total"], 0.23, places=3)
        self.assertAlmostEqual(p["profit_pct"], -0.01709, places=4)


class DrawdownAndBearMarketTest(DecisionEngineTestBase):
    def test_drawdown_protection_downgrades_buys(self):
        md = make_market_data()
        md["portfolio_peak_value"] = 11200.0  # current 10000 → 10.7% drawdown
        md["funds"]["512480"]["day_return"]    = -0.035
        md["funds"]["512480"]["fund_5d_return"] = -0.06
        packet = self._decide(md, positions=[],
                              cash=5000.0, total_value=10000.0)
        buys = find_action(packet, "512480", "buy")
        self.assertEqual(buys, [])
        watches = find_action(packet, "512480", "watch")
        self.assertEqual(len(watches), 1)
        self.assertEqual(watches[0]["rule_id"], "low_buy_deferred_drawdown")
        self.assertTrue(any(
            a["id"] == "drawdown_protection" for a in packet["alerts"]
        ))

    def test_bear_market_blocks_new_position(self):
        md = make_market_data(hs300_20d_return=-0.12)
        md["funds"]["512480"]["day_return"]    = -0.035
        md["funds"]["512480"]["fund_5d_return"] = -0.06
        packet = self._decide(md, positions=[],
                              cash=5000.0, total_value=10000.0)
        self.assertEqual(find_action(packet, "512480", "buy"), [])
        self.assertTrue(find_blocked(packet, "512480", "bear_market_new_position"))

    def test_bear_market_allows_low_buy_existing(self):
        positions = [make_position(
            "512480", "半导体ETF国联安",
            shares=500.0, cost_nav=2.30, sector="科技",
        )]
        md = make_market_data(hs300_20d_return=-0.12)
        md["funds"]["512480"]["day_return"]    = -0.035
        md["funds"]["512480"]["fund_5d_return"] = -0.06
        packet = self._decide(md, positions=positions,
                              cash=5000.0, total_value=6150.0)
        buys = find_action(packet, "512480", "buy")
        self.assertEqual(len(buys), 1)
        # base 3% × 6150 = 184.50, boost 1.0 (fund_5d -6% does not hit -8%; day -3.5% does not hit -5%),
        # bear-market existing → ×0.5 → 92.25
        self.assertAlmostEqual(buys[0]["suggested_amount"], 92.25, delta=1.0)


class TakeProfitTest(DecisionEngineTestBase):
    def test_take_profit_tier_20(self):
        # cost 1.90, current 2.30 → +21% profit → sell 25%
        positions = [make_position(
            "512480", "半导体ETF国联安",
            shares=1000.0, cost_nav=1.90, sector="科技",
        )]
        packet = self._decide(make_market_data(), positions=positions,
                              cash=5000.0, total_value=7300.0)
        sells = find_action(packet, "512480", "sell")
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["rule_id"], "take_profit_tier_20")
        self.assertAlmostEqual(sells[0]["suggested_shares"], 250.0, places=2)

    def test_take_profit_clearout(self):
        # cost 1.50, current 2.30 → +53% profit → full clear
        positions = [make_position(
            "512480", "半导体ETF国联安",
            shares=1000.0, cost_nav=1.50, sector="科技",
        )]
        packet = self._decide(make_market_data(), positions=positions,
                              cash=5000.0, total_value=7300.0)
        sells = find_action(packet, "512480", "sell")
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["rule_id"], "take_profit_clearout")
        self.assertAlmostEqual(sells[0]["suggested_shares"], 1000.0, places=2)


class StopLossTest(DecisionEngineTestBase):
    def test_emergency_stop_loss_day(self):
        positions = [make_position(
            "512480", "半导体ETF国联安",
            shares=1000.0, cost_nav=2.30, sector="科技",
        )]
        md = make_market_data()
        md["funds"]["512480"]["day_return"]   = -0.075
        md["funds"]["512480"]["current_nav"]  = 2.30
        packet = self._decide(md, positions=positions,
                              cash=5000.0, total_value=7300.0)
        sells = find_action(packet, "512480", "sell")
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["rule_id"], "emergency_stop_loss")
        self.assertAlmostEqual(sells[0]["suggested_shares"], 500.0, places=2)

    def test_absolute_stop_loss(self):
        # cost 3.0, current 2.30 → -23.3% loss
        positions = [make_position(
            "512480", "半导体ETF国联安",
            shares=1000.0, cost_nav=3.00, sector="科技",
        )]
        md = make_market_data()
        packet = self._decide(md, positions=positions,
                              cash=5000.0, total_value=7300.0)
        sells = find_action(packet, "512480", "sell")
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["rule_id"], "absolute_stop_loss")
        self.assertAlmostEqual(sells[0]["suggested_shares"], 1000.0, places=2)

    def test_time_based_stop_loss_short_hold(self):
        # 10-day hold, cost 2.50, current 2.30 → -8% loss
        positions = [make_position(
            "512480", "半导体ETF国联安",
            shares=1000.0, cost_nav=2.50, sector="科技", hold_days=10,
        )]
        md = make_market_data()
        packet = self._decide(md, positions=positions,
                              cash=5000.0, total_value=7300.0)
        sells = find_action(packet, "512480", "sell")
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["rule_id"], "time_based_stop_loss")
        self.assertAlmostEqual(sells[0]["suggested_shares"], 500.0, places=2)


class LowBuyTest(DecisionEngineTestBase):
    """低吸规则 + 5 个买入前置检查."""

    def test_low_buy_triggers(self):
        md = make_market_data()
        md["funds"]["512480"]["day_return"]    = -0.035
        md["funds"]["512480"]["fund_5d_return"] = -0.06
        packet = self._decide(md, positions=[],
                              cash=2000.0, total_value=10000.0)
        actions = find_action(packet, "512480", "buy")
        self.assertEqual(len(actions), 1, packet["actions"])
        self.assertEqual(actions[0]["rule_id"], "low_buy")
        self.assertGreater(actions[0]["suggested_amount"], 0)

    def test_low_buy_boosted_amount(self):
        md = make_market_data(hs300_5d_return=-0.025)
        md["funds"]["512480"]["day_return"]    = -0.055
        md["funds"]["512480"]["fund_5d_return"] = -0.09
        packet = self._decide(md, positions=[],
                              cash=2000.0, total_value=10000.0)
        action = find_action(packet, "512480", "buy")[0]
        # boost = 2x base; base = 3% × 10000 = 300; expected ≈ 600
        self.assertAlmostEqual(action["suggested_amount"], 600.0, delta=1.0)

    def test_blocked_by_cash_reserve(self):
        md = make_market_data()
        md["funds"]["512480"]["day_return"]    = -0.035
        md["funds"]["512480"]["fund_5d_return"] = -0.06
        # cash 5% — below 10% min
        packet = self._decide(md, positions=[],
                              cash=500.0, total_value=10000.0)
        self.assertEqual(find_action(packet, "512480", "buy"), [])
        self.assertTrue(find_blocked(packet, "512480", "cash_reserve"))

    def test_blocked_by_anti_chase(self):
        md = make_market_data()
        md["funds"]["512480"]["day_return"]     = -0.035
        md["funds"]["512480"]["fund_5d_return"] = 0.12  # surged 12%
        packet = self._decide(md, positions=[],
                              cash=5000.0, total_value=10000.0)
        self.assertEqual(find_action(packet, "512480", "buy"), [])
        self.assertTrue(find_blocked(packet, "512480", "anti_chase"))

    def test_blocked_by_sector_concentration(self):
        # Existing 半导体ETF at 48% of 10000 = 4800. Adding more 科技 would exceed 50%.
        positions = [make_position(
            "512480", "半导体ETF国联安",
            shares=2086.96, cost_nav=2.34, sector="科技",
        )]
        md = make_market_data()
        # Make 005825 (another 科技 fund) trigger low_buy
        md["funds"]["005825"] = {
            "name": "海富通电子传媒股票A", "current_nav": 1.50,
            "day_return": -0.04, "fund_3d_return": 0.0,
            "fund_5d_return": -0.06, "fund_20d_return": 0.0,
            "high_20d": 1.60, "sector": "科技",
        }
        packet = self._decide(md, positions=positions,
                              cash=5200.0, total_value=10000.0)
        self.assertTrue(find_blocked(packet, "005825", "sector_concentration"))

    def test_blocked_by_single_position_cap(self):
        # Already 26% of total — over 25% single cap.
        positions = [make_position(
            "512480", "半导体ETF国联安",
            shares=1130.43, cost_nav=2.30, sector="科技",
        )]
        md = make_market_data()
        md["funds"]["512480"]["day_return"]    = -0.035
        md["funds"]["512480"]["fund_5d_return"] = -0.06
        packet = self._decide(md, positions=positions,
                              cash=7400.0, total_value=10000.0)
        self.assertTrue(find_blocked(packet, "512480", "single_position"))


class MarketRegimeTest(DecisionEngineTestBase):
    def test_bull_market(self):
        md = make_market_data(hs300_20d_return=0.08)
        r = self._decide(md, positions=[])["market_regime"]
        self.assertEqual(r["label"], "牛市")
        self.assertEqual(r["position_cap"], 0.95)
        self.assertEqual(r["single_cap"], 0.30)
        self.assertEqual(r["stop_loss_threshold"], -0.15)

    def test_bear_market(self):
        md = make_market_data(hs300_20d_return=-0.12)
        r = self._decide(md, positions=[])["market_regime"]
        self.assertEqual(r["label"], "熊市")
        self.assertEqual(r["position_cap"], 0.60)
        self.assertEqual(r["single_cap"], 0.15)
        self.assertEqual(r["stop_loss_threshold"], -0.08)

    def test_chop_market(self):
        md = make_market_data(hs300_20d_return=0.02)
        r = self._decide(md, positions=[])["market_regime"]
        self.assertEqual(r["label"], "震荡市")
        self.assertEqual(r["position_cap"], 0.85)
        self.assertEqual(r["single_cap"], 0.25)


if __name__ == "__main__":
    unittest.main()
