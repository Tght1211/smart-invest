"""定投计划（DCA）执行 — Smart Invest Skill P7。

纯 stdlib。`is_due` 是纯函数：按"周期键"去重，自动处理周末/节假日顺延、
月末顺延。`record_due_plans` 把到期计划记账（写交易+累加持仓+扣现金+通知）。

设计依据: docs/superpowers/specs/2026-06-13-auto-invest-dca-design.md
"""
import calendar
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def _as_date(d):
    if isinstance(d, date):
        return d
    return datetime.strptime(d[:10], "%Y-%m-%d").date()


def _period_key(frequency, d, anchor=None):
    """周期键：同键内只投一次。"""
    if frequency == "daily":
        return d.isoformat()
    if frequency == "monthly":
        return f"{d.year}-{d.month:02d}"
    if frequency == "weekly":
        iso = d.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    if frequency == "biweekly":
        if not anchor:
            return d.isoformat()
        delta = (d - _as_date(anchor)).days
        return f"BW{delta // 14}"
    return d.isoformat()


def _reached_trigger(plan, d):
    """本周期内是否已到/过触发点（用于周末/月末顺延）。"""
    freq = plan["frequency"]
    if freq == "daily":
        return True
    if freq == "monthly":
        day = plan.get("day_field") or 1
        last_day = calendar.monthrange(d.year, d.month)[1]
        return d.day >= min(day, last_day)
    if freq == "weekly":
        return d.isoweekday() >= (plan.get("day_field") or 1)
    if freq == "biweekly":
        anchor = plan.get("anchor_date")
        if not anchor:
            return False
        weeks = (d - _as_date(anchor)).days // 7
        if weeks < 0 or weeks % 2 != 0:  # 非投资周
            return False
        return d.isoweekday() >= (plan.get("day_field") or 1)
    return False


def is_due(plan, today, nav_available):
    """该定投计划在 today 是否应当执行。

    today: datetime.date。nav_available: 当日该基金 NAV 是否可得（非交易日为 False）。
    """
    if not plan.get("enabled", 1):
        return False
    if not nav_available:
        return False
    today = _as_date(today)
    freq = plan["frequency"]
    anchor = plan.get("anchor_date")
    cur_key = _period_key(freq, today, anchor)
    last = plan.get("last_executed_date")
    if last and _period_key(freq, _as_date(last), anchor) == cur_key:
        return False  # 本周期已投
    return _reached_trigger(plan, today)


def due_plans(plans, today, nav_lookup):
    """返回今日到期的计划（nav_lookup: code -> nav，缺失即非交易日跳过）。"""
    out = []
    for p in plans:
        nav = nav_lookup.get(p["code"])
        if is_due(p, today, nav_available=nav is not None and nav > 0):
            out.append(p)
    return out


def _short_name(name):
    return (name or "").replace("ETF联接", "").replace("(QDII)", "")


def record_due_plans(db, account_id, account_name, today, funds, do_email=True):
    """对到期定投计划记账：写交易+累加持仓+扣现金+通知+记 last_executed_date。

    funds: code -> {current_nav, name, sector}（沿用引擎 market_data 的 funds 形状）。
    返回中文短句列表（已记账的计划）。幂等：同周期不重复。
    """
    today = _as_date(today)
    today_str = today.isoformat()
    plans = db.get_dca_plans(account_id, enabled_only=True)
    nav_lookup = {c: (f or {}).get("current_nav") for c, f in funds.items()}
    recorded = []
    for p in due_plans(plans, today, nav_lookup):
        code = p["code"]
        f = funds.get(code) or {}
        nav = f.get("current_nav") or 0.0
        amount = p["amount"]
        if nav <= 0 or amount <= 0:
            continue
        shares = amount / nav
        name = p["name"] or f.get("name", "")
        # 累加持仓（set_position 覆盖式，先取旧份额）
        row = db.conn.execute(
            "SELECT shares FROM positions WHERE account_id=? AND code=?",
            (account_id, code)).fetchone()
        new_shares = (row["shares"] if row else 0.0) + shares
        db.set_position(account_id, code, name, new_shares, nav,
                        buy_date=today_str, sector=f.get("sector"),
                        platform=p.get("platform", "支付宝"), note="auto_invest")
        db.add_trade(account_id, today_str, code, name, "buy", amount, nav,
                     shares, rule_name="auto_invest", reason="定投自动买入")
        # 扣现金
        acc = db.conn.execute(
            "SELECT cash FROM accounts WHERE id=?", (account_id,)).fetchone()
        if acc is not None:
            db.update_account(account_id, cash=(acc["cash"] or 0.0) - amount)
        db.set_dca_last_executed(account_id, code, today_str)
        recorded.append(f"定投「{_short_name(name)}」¥{amount:,.0f}")
        if do_email:
            _notify(account_name, code, name, amount, nav, shares)
    return recorded


def _notify(account, code, name, amount, nav, shares):
    try:
        subprocess.run([
            sys.executable, str(SCRIPT_DIR / "send_email.py"), "trade-notify",
            "--action", "buy", "--code", code, "--name", name,
            "--amount", f"{amount:.2f}", "--nav", f"{nav:.4f}",
            "--shares", f"{shares:.2f}", "--note", "定投",
        ], check=False, timeout=40)
    except Exception:
        pass
