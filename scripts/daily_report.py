#!/usr/bin/env python3
"""三时段卡片日报 — 确定性脚本（无需 LLM，供 OpenClaw/launchd 定时调用）。

用法:
  python3 scripts/daily_report.py --session open|mid|close --account 主线
                                  [--no-email] [--no-record] [--print]

流程:
  fetch_fund.gather_market_snapshot → DecisionEngine.decide
  → 组装对应时段卡片 markdown（:::card/:::action/持仓表/:::blocks/:::timeline）
  → send_email 发卡片邮件
盘尾(close)且引擎给出未被拦截的 buy/sell 时，自动记账(db) + trade-notify。
QDII（如 006479）当日方向附带纳指隔夜信号；其加仓由定投/限购处理，自动记账只走卖出侧。
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from db import Database  # noqa: E402
from decision_engine import DecisionEngine  # noqa: E402
import fetch_fund  # noqa: E402
import send_email  # noqa: E402

SESSIONS = {
    "open":  {"label": "开盘", "time": "09:30"},
    "mid":   {"label": "盘中", "time": "13:00"},
    "close": {"label": "盘尾", "time": "14:48"},
}

# 让利润奔跑：盈利不机械止盈（趋势品种越涨越卖反而踏空），只在亏损时止损。
# 止盈规则仍会在卡片里提示，但自动记账不执行卖出。设 False 可恢复机械止盈。
LET_WINNERS_RUN = True
TAKE_PROFIT_RULES = {
    "take_profit_tier_20", "take_profit_tier_30",
    "take_profit_tier_40", "take_profit_clearout",
}


# ---------- 数据组装 ----------

def build_context(db, account, date):
    """复刻 decide.cmd_run 的决策包构建，并保留原始 funds 快照（含 day_return）。"""
    snap = fetch_fund.gather_market_snapshot(account_name=account, date=date)
    if isinstance(snap, dict) and "error" in snap:
        return None, snap["error"]
    row = db.conn.execute(
        "SELECT id, cash FROM accounts WHERE name = ?", (account,)
    ).fetchone()
    if not row:
        return None, f"account '{account}' not found"
    account_id = row["id"]
    cash = row["cash"] or 0.0

    positions = []
    for r in db.conn.execute(
        "SELECT code, name, shares, cost_nav, sector, buy_date "
        "FROM positions WHERE account_id = ?", (account_id,)
    ):
        hold_days = 0
        if r["buy_date"]:
            try:
                hold_days = (datetime.now() - datetime.fromisoformat(r["buy_date"])).days
            except Exception:
                pass
        positions.append({
            "code": r["code"], "name": r["name"], "shares": r["shares"],
            "cost_nav": r["cost_nav"], "sector": r["sector"], "hold_days": hold_days,
        })

    funds = snap.get("funds", {}) or {}
    position_value = sum(
        p["shares"] * ((funds.get(p["code"]) or {}).get("current_nav", p["cost_nav"]))
        for p in positions
    )
    total_value = cash + position_value
    engine = DecisionEngine(db, account_id)
    packet = engine.decide(
        date=date, market_data=snap, positions=positions,
        cash=cash, total_value=total_value,
    )
    return {
        "packet": packet, "funds": funds, "positions": positions,
        "account_id": account_id, "total_value": total_value, "cash": cash,
    }, None


def _today_pnl(shares, cur_nav, day_return):
    """当日盈亏 = (现价 - 昨收) * 份额；昨收 = 现价 / (1 + 当日涨幅)。"""
    if not day_return:
        return 0.0
    prev = cur_nav / (1 + day_return)
    return (cur_nav - prev) * shares


def _fmt_amt(v):
    return f"{v:+,.2f}"


def _short_name(name):
    return (name or "").replace("ETF联接", "").replace("(QDII)", "").replace("指数", "")


# ---------- 卡片各段 ----------

def card_top(ctx, session):
    sess = SESSIONS[session]
    funds, positions = ctx["funds"], ctx["positions"]
    total_today = 0.0
    total_cost = 0.0
    total_value = 0.0
    for p in positions:
        f = funds.get(p["code"]) or {}
        cur = f.get("current_nav", p["cost_nav"])
        dr = f.get("day_return", 0.0) or 0.0
        total_today += _today_pnl(p["shares"], cur, dr)
        total_cost += p["shares"] * p["cost_nav"]
        total_value += p["shares"] * cur
    cum = total_value - total_cost
    cum_pct = (cum / total_cost * 100) if total_cost else 0.0
    label = f"今日估算盈亏（{sess['label']} {sess['time']}）"
    stats = f"总市值 ¥{total_value:,.0f} | 累计 {_fmt_amt(cum)} | {cum_pct:+.2f}%"
    return [":::card", label, f"{total_today:+,.2f}元", stats, ":::", ""]


def card_action(ctx, session, recorded, skipped=None):
    """黄框：忠实反映引擎 actions[]；close 用『已下单』口吻。"""
    skipped = skipped or []
    packet = ctx["packet"]
    actions = packet.get("actions", [])
    funds = ctx["funds"]
    # QDII 隔夜信号（若持有映射内基金）
    overnight = ""
    for p in ctx["positions"]:
        sig = fetch_fund.qdii_overnight_signal(p["code"])
        if sig and sig.get("pct") is not None:
            d = "涨" if sig["pct"] > 0 else ("跌" if sig["pct"] < 0 else "平")
            overnight = f"纳指隔夜 {sig['pct']:+.2f}% → {p['code']} 今日预计{d}。"
            break

    buys = [a for a in actions if a["action"] == "buy"]
    sells = [a for a in actions if a["action"] == "sell"]
    parts = []
    if session == "close":
        if recorded:
            parts.append(f"今日已下单：{'；'.join(recorded)}。其余持有。")
        else:
            parts.append("今日不操作，全部持有。")
        if skipped:
            parts.append("（" + "；".join(skipped) + "）")
    else:  # open / mid —— 让利润奔跑，计划里也不提止盈
        prefix = "今日计划：" if session == "open" else "盘中信号："
        act = [a for a in (buys + sells)
               if not (LET_WINNERS_RUN and a.get("rule_id") in TAKE_PROFIT_RULES)]
        held_winner = any(a.get("rule_id") in TAKE_PROFIT_RULES for a in sells)
        if act:
            parts.append(prefix + "；".join(a["reason_zh"] for a in act))
        elif held_winner:
            parts.append(prefix + "已盈利，继续持有让利润奔跑，跌破止损再走。")
        else:
            parts.append(prefix + ("按兵不动，持有观察。" if session == "open"
                                    else "暂无操作信号，维持持有，尾盘再确认。"))
    if overnight:
        parts.append(overnight)
    return [":::action", " ".join(parts), ":::", ""]


def card_holdings(ctx):
    funds, positions = ctx["funds"], ctx["positions"]
    by_pos = {p["code"]: p for p in ctx["packet"]["portfolio_snapshot"]["by_position"]}
    out = ["### 我的持仓（实时估值）", "",
           "| 基金 | 今日 | 今日盈亏 | 昨日盈亏 | 累计 | 持有 |",
           "|------|------|---------|---------|------|------|"]
    for p in positions:
        f = funds.get(p["code"]) or {}
        cur = f.get("current_nav", p["cost_nav"])
        dr = (f.get("day_return", 0.0) or 0.0)
        today_pnl = _today_pnl(p["shares"], cur, dr)
        value = p["shares"] * cur
        cum_pct = by_pos.get(p["code"], {}).get("profit_pct", 0.0) * 100
        tp = _fmt_amt(today_pnl) if dr else "--"
        out.append(
            f"| {_short_name(p['name'])} | {dr*100:+.2f}% | {tp} | -- | "
            f"{cum_pct:+.2f}% | {value:,.0f} |"
        )
    out.append("")
    return out


def card_blocks():
    blocks = fetch_fund.fetch_sectors(3) + [
        i for i in fetch_fund.fetch_indices()
        if i["name"] in ("创业板指", "上证指数", "深证成指", "沪深300", "中证500")
    ]
    if not blocks:
        return []
    out = ["### 大盘行情", "", ":::blocks"]
    for b in blocks:
        if b.get("pct") is None:
            continue
        out.append(f"{b['name']} {b['pct']:+.2f}%")
    out += [":::", ""]
    return out


def card_timeline(db, account_id):
    rows = db.get_trades(account_id, limit=6)
    if not rows:
        return []
    out = ["### 近期操作", "", ":::timeline"]
    for t in rows:
        dt = str(t["date"])[5:].replace("-", "/")
        act = "买入" if t["action"] == "buy" else "卖出"
        out.append(f"{dt} | {act} {_short_name(t['name'])} ¥{t['amount']:,.0f}")
    out += [":::", ""]
    return out


# ---------- 盘尾自动记账 ----------

def _recently_traded(db, account_id, code, action, rule_id, days=7):
    """同一 (code, action, rule_id) 近 days 天内是否已记账。
    防止止盈/止损这类按 profit_pct 触发的规则每日重复执行（卖出不改 profit_pct → 否则会天天减仓）。"""
    if not rule_id:
        return False
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    row = db.conn.execute(
        "SELECT COUNT(*) AS c FROM trades WHERE account_id=? AND code=? "
        "AND action=? AND rule_name=? AND date >= ?",
        (account_id, code, action, rule_id, cutoff),
    ).fetchone()
    return (row["c"] if row else 0) > 0


def auto_record(db, ctx, account, do_email=True):
    """盘尾把引擎未被拦截的 buy/sell 写入 DB + trade-notify。
    QDII（限购+定投）的买入跳过；同规则 7 天内已执行则跳过（防 runaway）。
    返回 (recorded[], skipped[]) 中文短句列表。"""
    packet = ctx["packet"]
    funds = ctx["funds"]
    account_id = ctx["account_id"]
    today = datetime.now().strftime("%Y-%m-%d")
    recorded, skipped = [], []
    for a in packet.get("actions", []):
        code, name, rule_id = a["code"], a["name"], a.get("rule_id")
        f = funds.get(code) or {}
        nav = f.get("current_nav") or 0.0
        if a["action"] == "buy":
            if code in fetch_fund.QDII_INDEX_MAP:
                skipped.append(f"{_short_name(name)} 加仓（QDII 限购/定投，自动跳过）")
                continue
            amt = a.get("suggested_amount") or 0.0
            if amt <= 0 or nav <= 0:
                continue
            if _recently_traded(db, account_id, code, "buy", rule_id):
                skipped.append(f"{_short_name(name)} 买入（{rule_id} 近 7 天已执行）")
                continue
            shares = amt / nav
            db.set_position(account_id, code, name,
                            _accum_shares(db, account_id, code, shares), nav,
                            buy_date=today, sector=a.get("sector"), note=rule_id)
            db.add_trade(account_id, today, code, name, "buy", amt, nav, shares,
                         rule_name=rule_id)
            recorded.append(f"买入「{_short_name(name)}」¥{amt:,.0f}（支付宝搜 {code}）")
            if do_email:
                _notify(account, "buy", code, name, amt, nav, shares, a.get("rule_label", ""))
        elif a["action"] == "sell":
            shares = a.get("suggested_shares") or 0.0
            if shares <= 0 or nav <= 0:
                continue
            if LET_WINNERS_RUN and rule_id in TAKE_PROFIT_RULES:
                skipped.append(f"{_short_name(name)} 止盈线已到但继续持有（让利润奔跑，跌破止损再走）")
                continue
            if _recently_traded(db, account_id, code, "sell", rule_id):
                skipped.append(f"{_short_name(name)} 减仓（{rule_id} 近 7 天已执行，不重复）")
                continue
            amt = shares * nav
            db.update_position_shares(account_id, code, -shares)
            db.add_trade(account_id, today, code, name, "sell", amt, nav, shares,
                         rule_name=rule_id)
            recorded.append(f"卖出「{_short_name(name)}」约 {shares:,.0f} 份（¥{amt:,.0f}）")
            if do_email:
                _notify(account, "sell", code, name, amt, nav, shares, a.get("rule_label", ""))
    return recorded, skipped


def _accum_shares(db, account_id, code, add):
    """已持有则累加份额（set_position 是覆盖式，需先取旧份额）。"""
    row = db.conn.execute(
        "SELECT shares FROM positions WHERE account_id = ? AND code = ?",
        (account_id, code),
    ).fetchone()
    return (row["shares"] if row else 0.0) + add


def _notify(account, action, code, name, amt, nav, shares, note):
    import subprocess
    try:
        subprocess.run([
            sys.executable, str(SCRIPT_DIR / "send_email.py"), "trade-notify",
            "--action", action, "--code", code, "--name", name,
            "--amount", f"{amt:.2f}", "--nav", f"{nav:.4f}",
            "--shares", f"{shares:.2f}", "--note", note or "",
        ], check=False, timeout=40)
    except Exception:
        pass


# ---------- 主流程 ----------

def assemble(db, ctx, session, recorded, skipped=None):
    md = []
    md += card_top(ctx, session)
    md += card_action(ctx, session, recorded, skipped)
    md += card_holdings(ctx)
    md += card_blocks()
    md += card_timeline(db, ctx["account_id"])
    return "\n".join(md)


def main():
    ap = argparse.ArgumentParser(description="三时段卡片日报（确定性，供定时调用）")
    ap.add_argument("--session", choices=list(SESSIONS), required=True)
    ap.add_argument("--account", default="主线")
    ap.add_argument("--date", default=None)
    ap.add_argument("--no-email", action="store_true", help="只生成不发邮件")
    ap.add_argument("--no-record", action="store_true", help="盘尾不自动记账")
    ap.add_argument("--print", action="store_true", help="把卡片 markdown 打到 stdout")
    args = ap.parse_args()

    date = args.date or datetime.now().strftime("%Y-%m-%d")
    db = Database()
    try:
        ctx, err = build_context(db, args.account, date)
        if err:
            print(f"[ERROR] {err}", file=sys.stderr)
            return 2

        recorded, skipped = [], []
        if args.session == "close" and not args.no_record:
            recorded, skipped = auto_record(db, ctx, args.account, do_email=not args.no_email)

        md = assemble(db, ctx, args.session, recorded, skipped)
        if args.print:
            print(md)

        if not args.no_email:
            sess = SESSIONS[args.session]
            # subject 带当日盈亏
            top_pnl_line = md.split("\n")[2] if md else ""
            subject = f"📊 {sess['label']} {date}　{top_pnl_line}"
            html = send_email.markdown_to_html(md)
            ok = send_email.send_email(subject, md, html)
            print("[OK] 邮件已发送" if ok else "[WARN] 邮件未发送（检查配置）")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
