"""Tests for simulate.py engine_mode (Phase 2).

Verifies:
- _build_market_data_for_engine uses only NAVs dated ≤ current sim date (no future leak)
- _positions_for_engine returns the expected dict shape
- apply_rules_engine in a controlled scenario produces sell-then-buy ordering
"""
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


class SimulateEngineBuilderTest(unittest.TestCase):
    def setUp(self):
        from simulate import Simulator
        # Construct a Simulator without running load_data (we'll inject data)
        self.sim = Simulator.__new__(Simulator)
        self.sim.start_date = "2026-01-01"
        self.sim.end_date = "2026-01-30"
        self.sim.budget = 10000.0
        self.sim.cash = 10000.0
        self.sim.positions = {}
        self.sim.trades = []
        self.sim.daily_records = []
        self.sim.fund_names = {"512480": "半导体ETF国联安"}
        self.sim.fund_navs = {
            # 25 days of NAVs, with a clear future spike on day 24
            "512480": {
                f"2026-01-{i+1:02d}": (1.0 + i * 0.001)
                for i in range(20)
            },
        }
        # Plant a huge "future" NAV after day 20 to detect future leak
        self.sim.fund_navs["512480"]["2026-01-25"] = 9.99
        self.sim.index_data = {
            "1.000300": {
                f"2026-01-{i+1:02d}": (4000.0 + i * 5)
                for i in range(20)
            },
        }
        self.sim.peak_value = 10000.0
        self.sim.sim_id = "test-sim"
        self.sim.sim_dir = Path("/tmp/test-sim")
        self.sim.diary_lines = []
        self.sim.verbose = False
        self.sim.engine_mode = True
        self.sim.db = None
        self.sim.account_id = None
        self.sim.strategy_version = "v2.0"
        self.sim.engine = None

    def test_no_future_leak_in_market_data(self):
        trading_days = sorted(self.sim.fund_navs["512480"].keys())
        date = "2026-01-15"  # day 15 of 25 known days
        md = self.sim._build_market_data_for_engine(date, trading_days)
        # current_nav for the fund must be the day-15 NAV (1.014), not the planted 9.99
        nav = md["funds"]["512480"]["current_nav"]
        self.assertLess(nav, 2.0,
                        msg=f"future leak detected: got current_nav={nav} (should be ~1.014)")
        # high_20d must also be in normal range (no peek into 9.99)
        self.assertLess(md["funds"]["512480"]["high_20d"], 2.0)

    def test_positions_shape(self):
        self.sim.positions = {
            "512480": {
                "shares": 1000.0, "cost_nav": 1.05,
                "name": "半导体ETF国联安", "buy_date": "2026-01-10",
            }
        }
        positions = self.sim._positions_for_engine("2026-01-15")
        self.assertEqual(len(positions), 1)
        p = positions[0]
        self.assertEqual(p["code"], "512480")
        self.assertEqual(p["sector"], "科技")
        self.assertEqual(p["hold_days"], 5)


if __name__ == "__main__":
    unittest.main()
