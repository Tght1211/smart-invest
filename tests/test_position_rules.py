"""P6 仓位管理规则测试：position_build 分批建仓 + position_cap_trim 超配回撤
+ portfolio_advice 决策包块。

设计依据: docs/superpowers/specs/2026-06-13-position-mgmt-multi-signal-design.md

Run with:
  python3 -m unittest tests.test_position_rules -v
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
    "tolerance": 0.05,
    "batch_fraction": 0.10,
    "max_funds_per_batch": 2,
    "min_order_amount": 300,
}


class PositionRulesTestBase(unittest.TestCase):
    def setUp(self):
        self.rules = load_rules()
        self.rules["position_management"] = copy.deepcopy(PM_RULES)
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
    def _builds(packet):
        return [a for a in packet["actions"] if a.get("rule_id") == "position_build"]


class PositionBuildTest(PositionRulesTestBase):
    def test_underweight_deploys_batch_across_top_candidates(self):
        """空仓 + 震荡市：部署 batch_fraction=10% 总资产，按 20 日动量排序分两只。"""
        packet = self._decide(make_market_data(), positions=[],
                              cash=10000.0, total_value=10000.0)
        builds = self._builds(packet)
        self.assertEqual(len(builds), 2)
        self.assertEqual({b["action"] for b in builds}, {"buy"})
        self.assertAlmostEqual(sum(b["suggested_amount"] for b in builds),
                               1000.0, places=2)
        # 006479 的 20 日动量 (0.05) 高于 512480 (0.0)，排第一
        self.assertEqual(builds[0]["code"], "006479")

    def test_in_band_no_action(self):
        """仓位已在目标区间（55% ≥ floor-tolerance）→ 不建仓。"""
        shares = 5500.0 / 2.30
        positions = [make_position("512480", "半导体ETF国联安",
                                   shares=shares, cost_nav=2.34, sector="科技")]
        packet = self._decide(make_market_data(), positions=positions,
                              cash=4500.0, total_value=10000.0)
        self.assertEqual(self._builds(packet), [])

    def test_disabled_by_default(self):
        """未配置 position_management → 行为与 v2.x 基线一致，无建仓动作。"""
        rules = load_rules()
        packet = self._decide(make_market_data(), positions=[],
                              cash=10000.0, total_value=10000.0, rules=rules)
        self.assertEqual(self._builds(packet), [])

    def test_unknown_regime_skips(self):
        md = make_market_data(hs300_20d_return=None)
        packet = self._decide(md, positions=[],
                              cash=10000.0, total_value=10000.0)
        self.assertEqual(self._builds(packet), [])

    def test_bear_market_only_adds_to_existing(self):
        """熊市：floor=30%，只允许对已持仓加仓，新建仓被排除。"""
        md = make_market_data(hs300_20d_return=-0.12)
        shares = 1000.0 / 2.30
        positions = [make_position("512480", "半导体ETF国联安",
                                   shares=shares, cost_nav=2.34, sector="科技")]
        packet = self._decide(md, positions=positions,
                              cash=9000.0, total_value=10000.0)
        builds = self._builds(packet)
        self.assertEqual([b["code"] for b in builds], ["512480"])
        self.assertAlmostEqual(builds[0]["suggested_amount"], 500.0, places=2)

    def test_small_gap_reduces_batch_to_one_fund(self):
        """deploy=400 只够一笔 ≥min_order 的单子 → 集中给动量第一名。"""
        packet = self._decide(make_market_data(), positions=[],
                              cash=4000.0, total_value=4000.0)
        builds = self._builds(packet)
        self.assertEqual(len(builds), 1)
        self.assertEqual(builds[0]["code"], "006479")
        self.assertAlmostEqual(builds[0]["suggested_amount"], 400.0, places=2)

    def test_deploy_below_min_order_no_action(self):
        packet = self._decide(make_market_data(), positions=[],
                              cash=2000.0, total_value=2000.0)
        self.assertEqual(self._builds(packet), [])

    def test_anti_chase_excludes_candidate(self):
        """5 日涨幅 >10% 的基金不进建仓名单。"""
        md = make_market_data()
        md["funds"]["006479"]["fund_5d_return"] = 0.12
        packet = self._decide(md, positions=[],
                              cash=10000.0, total_value=10000.0)
        builds = self._builds(packet)
        self.assertEqual([b["code"] for b in builds], ["512480"])

    def test_fund_constraint_skips_limited_fund(self):
        """限购 ¥10/天 的基金（裁剪后 < min_order）不占建仓名额。"""
        self.rules["fund_constraints"] = {"006479": {"max_daily_buy": 10}}
        packet = self._decide(make_market_data(), positions=[],
                              cash=10000.0, total_value=10000.0)
        builds = self._builds(packet)
        self.assertEqual([b["code"] for b in builds], ["512480"])

    def test_require_trend_above_blocks_build_below_ma200(self):
        """P6.1 强趋势闸门：HS300 在 200 日线下方 → 完全不建仓。"""
        self.rules["position_management"]["require_trend_above"] = True
        md = make_market_data(index_trend={"HS300": {
            "ma": 3500.0, "gap_pct": -0.03, "below_days": 5, "above": False}})
        packet = self._decide(md, positions=[],
                              cash=10000.0, total_value=10000.0)
        self.assertEqual(self._builds(packet), [])

    def test_require_trend_above_allows_build_above_ma200(self):
        self.rules["position_management"]["require_trend_above"] = True
        md = make_market_data(index_trend={"HS300": {
            "ma": 3500.0, "gap_pct": 0.02, "below_days": 0, "above": True}})
        packet = self._decide(md, positions=[],
                              cash=10000.0, total_value=10000.0)
        self.assertEqual(len(self._builds(packet)), 2)

    def test_drawdown_protection_skips_position_build(self):
        md = make_market_data(portfolio_peak_value=12000.0)
        packet = self._decide(md, positions=[],
                              cash=10000.0, total_value=10000.0)
        self.assertEqual(self._builds(packet), [])


class PositionCapTrimTest(PositionRulesTestBase):
    def test_overweight_trims_largest_position(self):
        """仓位 93.6% > cap 85% + tol 5% → 卖出最大持仓把仓位拉回 cap。"""
        positions = [
            make_position("512480", "半导体ETF国联安",
                          shares=3000.0, cost_nav=2.34, sector="科技"),
            make_position("006479", "广发纳斯达克100ETF联接C",
                          shares=300.0, cost_nav=8.30, sector="海外"),
        ]
        packet = self._decide(make_market_data(), positions=positions,
                              cash=640.0, total_value=10000.0)
        trims = [a for a in packet["actions"]
                 if a.get("rule_id") == "position_cap_trim"]
        self.assertEqual(len(trims), 1)
        self.assertEqual(trims[0]["code"], "512480")
        self.assertEqual(trims[0]["action"], "sell")
        self.assertAlmostEqual(trims[0]["suggested_amount"], 860.0, delta=1.0)

    def test_within_cap_no_trim(self):
        shares = 5500.0 / 2.30
        positions = [make_position("512480", "半导体ETF国联安",
                                   shares=shares, cost_nav=2.34, sector="科技")]
        packet = self._decide(make_market_data(), positions=positions,
                              cash=4500.0, total_value=10000.0)
        self.assertEqual([a for a in packet["actions"]
                          if a.get("rule_id") == "position_cap_trim"], [])


class PortfolioAdviceTest(PositionRulesTestBase):
    def test_advice_present_and_underweight(self):
        packet = self._decide(make_market_data(), positions=[],
                              cash=10000.0, total_value=10000.0)
        adv = packet["portfolio_advice"]
        self.assertEqual(adv["status"], "underweight")
        self.assertAlmostEqual(adv["position_pct"], 0.0, places=4)
        self.assertAlmostEqual(adv["target_floor"], 0.50, places=4)
        self.assertAlmostEqual(adv["position_cap"], 0.85, places=4)
        self.assertAlmostEqual(adv["gap_amount"], 5000.0, places=2)
        self.assertAlmostEqual(adv["deployable_cash"], 9000.0, places=2)
        self.assertTrue(adv["advice_zh"])

    def test_advice_present_without_pm_config(self):
        """portfolio_advice 永远在包里，即使 position_management 未配置。"""
        rules = load_rules()
        packet = self._decide(make_market_data(), positions=[],
                              cash=10000.0, total_value=10000.0, rules=rules)
        self.assertIn("portfolio_advice", packet)
        self.assertEqual(packet["portfolio_advice"]["status"], "underweight")

    def test_advice_in_band(self):
        shares = 5500.0 / 2.30
        positions = [make_position("512480", "半导体ETF国联安",
                                   shares=shares, cost_nav=2.34, sector="科技")]
        packet = self._decide(make_market_data(), positions=positions,
                              cash=4500.0, total_value=10000.0)
        self.assertEqual(packet["portfolio_advice"]["status"], "in_band")


if __name__ == "__main__":
    unittest.main()
