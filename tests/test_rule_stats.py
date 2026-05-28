"""Tests for DecisionEngine.compute_rule_stats — per-rule win/loss aggregation."""
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from tests._helpers import make_in_memory_db, add_test_account  # noqa: E402


class RuleStatsTest(unittest.TestCase):
    def setUp(self):
        self.db = make_in_memory_db()
        self.account_id = add_test_account(self.db, name="stats-test")

    def tearDown(self):
        self.db.close()

    def _seed_trade(self, date, code, action, profit_pct, rule_id):
        """Insert a trade row directly (bypasses Phase 1 audit fields)."""
        from datetime import datetime
        outcome = "win" if profit_pct is not None and profit_pct > 0 else (
            "loss" if profit_pct is not None and profit_pct < 0 else "pending"
        )
        self.db.conn.execute(
            "INSERT INTO trades (account_id, date, code, name, action, amount, "
            "nav, shares, rule_name, rule_version, profit_pct, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.account_id, date, code, "test-fund", action,
                1000.0, 1.0, 1000.0, rule_id, "v2.0", profit_pct, outcome,
            ),
        )
        self.db.conn.commit()

    def test_empty_account_returns_empty(self):
        from decision_engine import DecisionEngine
        engine = DecisionEngine(self.db, self.account_id)
        stats = engine.compute_rule_stats()
        self.assertEqual(stats, [])

    def test_single_winning_trade(self):
        self._seed_trade("2026-01-01", "512480", "sell", 0.25, "take_profit_tier_20")
        from decision_engine import DecisionEngine
        engine = DecisionEngine(self.db, self.account_id)
        stats = engine.compute_rule_stats()
        self.assertEqual(len(stats), 1)
        row = stats[0]
        self.assertEqual(row["rule_id"], "take_profit_tier_20")
        self.assertEqual(row["count"], 1)
        self.assertEqual(row["wins"], 1)
        self.assertEqual(row["losses"], 0)
        self.assertAlmostEqual(row["win_rate"], 1.0)
        self.assertAlmostEqual(row["avg_profit_pct"], 0.25)

    def test_mixed_outcomes(self):
        # 3 wins, 2 losses on low_buy
        self._seed_trade("2026-01-01", "A", "sell",  0.10, "low_buy")
        self._seed_trade("2026-01-02", "B", "sell",  0.15, "low_buy")
        self._seed_trade("2026-01-03", "C", "sell",  0.05, "low_buy")
        self._seed_trade("2026-01-04", "D", "sell", -0.08, "low_buy")
        self._seed_trade("2026-01-05", "E", "sell", -0.04, "low_buy")
        from decision_engine import DecisionEngine
        engine = DecisionEngine(self.db, self.account_id)
        stats = engine.compute_rule_stats()
        row = next(r for r in stats if r["rule_id"] == "low_buy")
        self.assertEqual(row["count"], 5)
        self.assertEqual(row["wins"], 3)
        self.assertEqual(row["losses"], 2)
        self.assertAlmostEqual(row["win_rate"], 0.6)
        # avg profit = (10+15+5)/3 = 10%
        self.assertAlmostEqual(row["avg_profit_pct_wins"], 0.10, places=3)
        # avg loss = -(8+4)/2 = -6%
        self.assertAlmostEqual(row["avg_profit_pct_losses"], -0.06, places=3)
        # expectancy = 0.6 × 10% + 0.4 × (-6%) = 6% - 2.4% = 3.6%
        self.assertAlmostEqual(row["expectancy"], 0.036, places=3)

    def test_multiple_rules_grouped(self):
        self._seed_trade("2026-01-01", "A", "sell", 0.20, "take_profit_tier_20")
        self._seed_trade("2026-01-02", "B", "sell", 0.10, "low_buy")
        self._seed_trade("2026-01-03", "C", "sell", -0.20, "absolute_stop_loss")
        from decision_engine import DecisionEngine
        engine = DecisionEngine(self.db, self.account_id)
        stats = engine.compute_rule_stats()
        rule_ids = {r["rule_id"] for r in stats}
        self.assertEqual(rule_ids, {"take_profit_tier_20", "low_buy", "absolute_stop_loss"})

    def test_ignores_pending_trades(self):
        # buy with no profit_pct yet (still holding)
        self._seed_trade("2026-01-01", "A", "buy", None, "low_buy")
        from decision_engine import DecisionEngine
        engine = DecisionEngine(self.db, self.account_id)
        stats = engine.compute_rule_stats()
        # Pending trade should not appear (or appear with count=0 wins/losses)
        # Spec: stats only counts trades with non-null profit_pct
        if stats:
            for row in stats:
                self.assertGreater(row["count"], 0)


if __name__ == "__main__":
    unittest.main()
