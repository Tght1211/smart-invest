#!/usr/bin/env python3
"""Decision CLI — single entry point for live decisions.

Usage:
  python3 scripts/decide.py run --account 主线 [--date YYYY-MM-DD] [--format json|md]

Pipeline:
  fetch_fund.gather_market_snapshot(account)
  → DecisionEngine.decide(...)
  → JSON (default) or Markdown summary on stdout

Exit codes:
  0  success
  2  missing data / account not found / unresolvable preconditions
  3  unexpected error (with stderr traceback)
"""
import argparse
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from db import Database  # noqa: E402
from decision_engine import DecisionEngine  # noqa: E402
import fetch_fund  # noqa: E402


def _format_md(packet):
    lines = []
    lines.append(f"# 决策包 — 账户 {packet['account']} — {packet['date']}")
    lines.append("")
    r = packet["market_regime"]
    lines.append(
        f"**市场环境**: {r['label']}"
        f"  (HS300 5d {r['hs300_5d_return'] * 100:+.1f}%, "
        f"20d {r['hs300_20d_return'] * 100:+.1f}%)"
    )
    lines.append(
        f"**仓位上限**: {r['position_cap'] * 100:.0f}%  "
        f"**单只上限**: {r['single_cap'] * 100:.0f}%  "
        f"**止损线**: {r['stop_loss_threshold'] * 100:+.0f}%"
    )

    s = packet["portfolio_snapshot"]
    lines.append("")
    lines.append(
        f"**总资产**: ¥{s['total_value']:,.2f}  "
        f"**现金**: ¥{s['cash']:,.2f} ({s['cash_pct'] * 100:.1f}%)  "
        f"**持仓**: ¥{s['position_value']:,.2f} ({s['position_pct'] * 100:.1f}%)"
    )
    if s["sectors"]:
        sec = " / ".join(f"{k} {v * 100:.0f}%" for k, v in s["sectors"].items())
        lines.append(f"**赛道分布**: {sec}")

    if s["by_position"]:
        lines.append("")
        lines.append("## 持仓明细")
        lines.append("| 基金 | 代码 | 份额 | 成本 | 现值 | 持有收益 | 占比 |")
        lines.append("|------|------|------|------|------|---------|------|")
        for p in s["by_position"]:
            lines.append(
                f"| {p['name']} | {p['code']} | {p['shares']:,.2f} | "
                f"{p['cost_nav']:.4f} | {p['current_nav']:.4f} | "
                f"{p['profit_pct'] * 100:+.2f}% | "
                f"{p['pct_of_total'] * 100:.1f}% |"
            )

    if packet["actions"]:
        lines.append("")
        lines.append("## 建议操作")
        for a in packet["actions"]:
            conf = (
                f" 置信度 {a['confidence']:.2f}"
                if a.get("confidence") is not None else ""
            )
            if a["action"] == "buy":
                lines.append(
                    f"- 🟢 **买入** {a['name']} ({a['code']}) "
                    f"¥{a['suggested_amount']:.0f}  "
                    f"[{a['rule_label']}]{conf}"
                )
            elif a["action"] == "sell":
                lines.append(
                    f"- 🔴 **卖出** {a['name']} ({a['code']}) "
                    f"{a['suggested_shares']:.2f} 份 "
                    f"(约 ¥{a['suggested_amount']:.0f})  "
                    f"[{a['rule_label']}]{conf}"
                )
            elif a["action"] == "watch":
                lines.append(
                    f"- 🟡 **观察** {a['name']} ({a['code']}) "
                    f"[{a['rule_label']}]"
                )
            else:
                lines.append(
                    f"- ⏸️  **{a['action']}** {a['name']} ({a['code']})"
                )
            lines.append(f"  > {a['reason_zh']}")

    if packet["blocked_actions"]:
        lines.append("")
        lines.append("## 已拦截的买入意图")
        for b in packet["blocked_actions"]:
            lines.append(
                f"- ❌ {b['name']} ({b['code']}) — {b['reason_zh']}"
            )

    if packet["alerts"]:
        lines.append("")
        lines.append("## 预警")
        for a in packet["alerts"]:
            lines.append(
                f"- ⚠️ [{a['severity']}] {a['reason_zh']}"
            )

    sm = packet["summary"]
    lines.append("")
    lines.append("---")
    lines.append(
        f"**汇总**: 买 {sm['action_count']['buy']} / "
        f"卖 {sm['action_count']['sell']} / "
        f"持 {sm['action_count']['hold']} / "
        f"观察 {sm['action_count']['watch']}"
    )
    if sm.get("highest_confidence_action"):
        h = sm["highest_confidence_action"]
        lines.append(
            f"**最高置信度**: {h['action']} {h['code']} "
            f"(conf={h['confidence']:.2f})"
        )
    lines.append("")
    lines.append("> ⚠️ 以上仅供参考，投资有风险，入市需谨慎。")
    return "\n".join(lines)


def cmd_run(args):
    try:
        snap = fetch_fund.gather_market_snapshot(
            account_name=args.account, date=args.date,
        )
        if isinstance(snap, dict) and "error" in snap:
            print(
                json.dumps({"error": snap["error"]}, ensure_ascii=False),
                file=sys.stderr,
            )
            return 2

        db = Database()
        try:
            row = db.conn.execute(
                "SELECT id FROM accounts WHERE name = ?", (args.account,)
            ).fetchone()
            if not row:
                print(
                    json.dumps(
                        {"error": f"account '{args.account}' not found"},
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                )
                return 2
            account_id = row["id"]

            positions = []
            for r in db.conn.execute(
                "SELECT code, name, shares, cost_nav, sector, buy_date "
                "FROM positions WHERE account_id = ?",
                (account_id,),
            ):
                hold_days = 0
                if r["buy_date"]:
                    try:
                        bd = datetime.fromisoformat(r["buy_date"])
                        hold_days = (datetime.now() - bd).days
                    except Exception:
                        pass
                positions.append({
                    "code": r["code"],
                    "name": r["name"],
                    "shares": r["shares"],
                    "cost_nav": r["cost_nav"],
                    "sector": r["sector"],
                    "hold_days": hold_days,
                })

            cash_row = db.conn.execute(
                "SELECT cash FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
            cash = cash_row["cash"] if cash_row else 0.0

            funds = snap.get("funds", {}) or {}
            position_value = sum(
                p["shares"] * (
                    (funds.get(p["code"]) or {}).get("current_nav", p["cost_nav"])
                )
                for p in positions
            )
            total_value = cash + position_value

            date = args.date or datetime.now().strftime("%Y-%m-%d")
            engine = DecisionEngine(db, account_id)
            packet = engine.decide(
                date=date,
                market_data=snap,
                positions=positions,
                cash=cash,
                total_value=total_value,
            )

            if args.format == "md":
                print(_format_md(packet))
            else:
                print(json.dumps(packet, ensure_ascii=False, indent=2))
            return 0
        finally:
            db.close()
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 3


def main():
    ap = argparse.ArgumentParser(
        prog="decide.py",
        description="Smart-Invest decision CLI — single entry for live decisions",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="跑决策引擎产出决策包")
    p.add_argument("--account", default="主线", help="账户名称（默认: 主线）")
    p.add_argument(
        "--date", default=None,
        help="日期 YYYY-MM-DD（默认: 今天）",
    )
    p.add_argument(
        "--format", choices=["json", "md"], default="json",
        help="输出格式（默认: json）",
    )
    p.set_defaults(func=cmd_run)
    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
