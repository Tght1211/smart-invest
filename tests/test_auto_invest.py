"""P7 定投计划测试：is_due 到期判定（纯函数）+ record_due_plans 记账闭环。

设计依据: docs/superpowers/specs/2026-06-13-auto-invest-dca-design.md

is_due 按"周期键"去重，自动处理周末/节假日顺延、月末顺延。

Run with:
  python3 -m unittest tests.test_auto_invest -v
"""
import sys
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from tests._helpers import make_in_memory_db, add_test_account  # noqa: E402


def _plan(frequency, day_field=None, anchor_date=None,
          last_executed_date=None, enabled=1, amount=100.0,
          code="161725", name="测试基金"):
    return {
        "code": code, "name": name, "amount": amount,
        "frequency": frequency, "day_field": day_field,
        "anchor_date": anchor_date, "enabled": enabled,
        "last_executed_date": last_executed_date,
    }


class IsDueDailyTest(unittest.TestCase):
    def setUp(self):
        from auto_invest import is_due
        self.is_due = is_due

    def test_daily_due_each_trading_day(self):
        p = _plan("daily")
        self.assertTrue(self.is_due(p, date(2026, 6, 15), nav_available=True))

    def test_daily_not_due_when_already_executed_today(self):
        p = _plan("daily", last_executed_date="2026-06-15")
        self.assertFalse(self.is_due(p, date(2026, 6, 15), nav_available=True))

    def test_daily_due_next_day_after_yesterday_execution(self):
        p = _plan("daily", last_executed_date="2026-06-14")
        self.assertTrue(self.is_due(p, date(2026, 6, 15), nav_available=True))

    def test_not_due_when_nav_unavailable(self):
        p = _plan("daily")
        self.assertFalse(self.is_due(p, date(2026, 6, 13), nav_available=False))

    def test_disabled_plan_never_due(self):
        p = _plan("daily", enabled=0)
        self.assertFalse(self.is_due(p, date(2026, 6, 15), nav_available=True))


class IsDueMonthlyTest(unittest.TestCase):
    def setUp(self):
        from auto_invest import is_due
        self.is_due = is_due

    def test_monthly_due_on_scheduled_day(self):
        p = _plan("monthly", day_field=5)
        self.assertTrue(self.is_due(p, date(2026, 6, 5), nav_available=True))

    def test_monthly_not_due_before_scheduled_day(self):
        p = _plan("monthly", day_field=5)
        self.assertFalse(self.is_due(p, date(2026, 6, 4), nav_available=True))

    def test_monthly_due_after_scheduled_day_if_not_yet_executed(self):
        """5号是周末没交易→8号(周一)首个交易日仍触发（顺延）。"""
        p = _plan("monthly", day_field=5)
        self.assertTrue(self.is_due(p, date(2026, 6, 8), nav_available=True))

    def test_monthly_not_due_twice_same_month(self):
        p = _plan("monthly", day_field=5, last_executed_date="2026-06-05")
        self.assertFalse(self.is_due(p, date(2026, 6, 20), nav_available=True))

    def test_monthly_due_next_month(self):
        p = _plan("monthly", day_field=5, last_executed_date="2026-06-05")
        self.assertTrue(self.is_due(p, date(2026, 7, 6), nav_available=True))

    def test_monthly_day31_clamps_to_month_end(self):
        """day=31 在 2 月（28天）→ 月末触发。"""
        p = _plan("monthly", day_field=31)
        self.assertFalse(self.is_due(p, date(2026, 2, 27), nav_available=True))
        self.assertTrue(self.is_due(p, date(2026, 2, 28), nav_available=True))


class IsDueWeeklyTest(unittest.TestCase):
    def setUp(self):
        from auto_invest import is_due
        self.is_due = is_due

    def test_weekly_due_on_weekday(self):
        # 2026-06-15 是周一
        p = _plan("weekly", day_field=1)
        self.assertTrue(self.is_due(p, date(2026, 6, 15), nav_available=True))

    def test_weekly_not_due_before_weekday(self):
        # 周三定投，周一未到
        p = _plan("weekly", day_field=3)
        self.assertFalse(self.is_due(p, date(2026, 6, 15), nav_available=True))

    def test_weekly_not_due_twice_same_week(self):
        p = _plan("weekly", day_field=1, last_executed_date="2026-06-15")
        self.assertFalse(self.is_due(p, date(2026, 6, 17), nav_available=True))

    def test_weekly_due_next_week(self):
        p = _plan("weekly", day_field=1, last_executed_date="2026-06-15")
        self.assertTrue(self.is_due(p, date(2026, 6, 22), nav_available=True))

    def test_weekly_within_week_holiday_rollover(self):
        """周三(3)定投但周三休市→同周周四(6/18)首个交易日触发（周内顺延）。"""
        p = _plan("weekly", day_field=3)
        self.assertTrue(self.is_due(p, date(2026, 6, 18), nav_available=True))


class IsDueBiweeklyTest(unittest.TestCase):
    def setUp(self):
        from auto_invest import is_due
        self.is_due = is_due

    def test_biweekly_due_on_anchor_week(self):
        # anchor 2026-06-15(周一)，同周周一即投资周
        p = _plan("biweekly", day_field=1, anchor_date="2026-06-15")
        self.assertTrue(self.is_due(p, date(2026, 6, 15), nav_available=True))

    def test_biweekly_skip_off_week(self):
        # anchor 6/15，下一周 6/22 是非投资周
        p = _plan("biweekly", day_field=1, anchor_date="2026-06-15",
                  last_executed_date="2026-06-15")
        self.assertFalse(self.is_due(p, date(2026, 6, 22), nav_available=True))

    def test_biweekly_due_two_weeks_later(self):
        # 6/29 是 anchor 后第 2 周（投资周）
        p = _plan("biweekly", day_field=1, anchor_date="2026-06-15",
                  last_executed_date="2026-06-15")
        self.assertTrue(self.is_due(p, date(2026, 6, 29), nav_available=True))


class DuePlansTest(unittest.TestCase):
    def test_filters_to_due_only(self):
        from auto_invest import due_plans
        plans = [
            _plan("daily", code="A"),
            _plan("monthly", day_field=20, code="B"),  # 6/15 未到 20 号
        ]
        nav_lookup = {"A": 1.0, "B": 2.0}
        due = due_plans(plans, date(2026, 6, 15), nav_lookup)
        self.assertEqual([p["code"] for p in due], ["A"])

    def test_skips_plan_without_nav(self):
        from auto_invest import due_plans
        plans = [_plan("daily", code="A")]
        due = due_plans(plans, date(2026, 6, 15), nav_lookup={})
        self.assertEqual(due, [])


class RecordDuePlansTest(unittest.TestCase):
    def setUp(self):
        self.db = make_in_memory_db()
        self.account_id = add_test_account(self.db, name="主线", budget=50000.0)

    def tearDown(self):
        self.db.close()

    def test_records_trade_position_and_decrements_cash(self):
        from auto_invest import record_due_plans
        self.db.add_dca_plan(self.account_id, "161725", "招商白酒", 500.0,
                             "daily")
        funds = {"161725": {"current_nav": 2.0, "name": "招商白酒",
                            "sector": "消费"}}
        recorded = record_due_plans(
            self.db, self.account_id, "主线", date(2026, 6, 15),
            funds, do_email=False)
        self.assertEqual(len(recorded), 1)
        # 持仓写入
        pos = self.db.conn.execute(
            "SELECT shares FROM positions WHERE account_id=? AND code=?",
            (self.account_id, "161725")).fetchone()
        self.assertAlmostEqual(pos["shares"], 250.0, places=2)  # 500/2.0
        # 现金扣减
        cash = self.db.conn.execute(
            "SELECT cash FROM accounts WHERE id=?", (self.account_id,)
        ).fetchone()["cash"]
        self.assertAlmostEqual(cash, 49500.0, places=2)
        # 交易记录 rule_name
        tr = self.db.conn.execute(
            "SELECT rule_name FROM trades WHERE account_id=?",
            (self.account_id,)).fetchone()
        self.assertEqual(tr["rule_name"], "auto_invest")
        # last_executed_date 已记
        plan = self.db.get_dca_plans(self.account_id)[0]
        self.assertEqual(plan["last_executed_date"], "2026-06-15")

    def test_idempotent_same_day(self):
        from auto_invest import record_due_plans
        self.db.add_dca_plan(self.account_id, "161725", "招商白酒", 500.0,
                             "daily")
        funds = {"161725": {"current_nav": 2.0, "name": "招商白酒"}}
        record_due_plans(self.db, self.account_id, "主线", date(2026, 6, 15),
                         funds, do_email=False)
        second = record_due_plans(self.db, self.account_id, "主线",
                                  date(2026, 6, 15), funds, do_email=False)
        self.assertEqual(second, [])  # 同日不重复
        cnt = self.db.conn.execute(
            "SELECT COUNT(*) c FROM trades WHERE account_id=?",
            (self.account_id,)).fetchone()["c"]
        self.assertEqual(cnt, 1)


if __name__ == "__main__":
    unittest.main()
