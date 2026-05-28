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
