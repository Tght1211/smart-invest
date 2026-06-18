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
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from db import Database  # noqa: E402
from decision_engine import DecisionEngine, evaluate_trade_timing  # noqa: E402
import fetch_fund  # noqa: E402


# 操作复盘评定结论 → 邮件/终端用的醒目短标签（满足"精准踩中 / 卖飞了"语感）
VERDICT_BADGE = {
    "踩中": "精准踩中 ✅",
    "追高套牢": "追高套牢 ⚠️",
    "规避下跌": "成功规避 ✅",
    "卖飞": "卖飞了 ❗",
    "中性": "影响有限 ◽",
    "数据缺失": "数据缺失 ·",
}


def build_trade_reviews(db, account_id, horizon=7, lookback=60, save=False):
    """复盘账户近 lookback 天、已满 horizon 天的历史操作（决定 + daily_report 共用）。

    用 trades.nav 作买入/卖出当时净值，事后 horizon 天的净值判定择时是否踩中。
    返回 (results, pending)：results 每条含 evaluate_trade_timing 字段 + 交易元数据；
    pending = 太新、还不够 horizon 天、暂不评定的笔数。save=True 时写入 trade_reviews（记忆）。
    """
    today = datetime.now().date()
    cutoff = (today - timedelta(days=lookback)).isoformat()
    trades = [t for t in db.get_trades(account_id) if (t.get("date") or "") >= cutoff]

    ready, pending = [], 0
    for t in trades:
        try:
            td = datetime.strptime(str(t["date"])[:10], "%Y-%m-%d").date()
        except Exception:
            continue
        if (today - td).days >= horizon:
            ready.append((t, td))
        else:
            pending += 1
    if not ready:
        return [], pending

    # 每个代码只拉一次净值序列（覆盖最老交易 → 今天 + horizon）
    oldest_by_code = {}
    for t, td in ready:
        c = t["code"]
        oldest_by_code[c] = min(td, oldest_by_code.get(c, td))
    series_by_code = {}
    for code, oldest in oldest_by_code.items():
        days_needed = (today - oldest).days + horizon + 8
        series_by_code[code] = fetch_fund.fetch_nav_series(code, days=min(days_needed, 400))

    results = []
    for t, td in ready:
        series = series_by_code.get(t["code"]) or []
        target = (td + timedelta(days=horizon)).isoformat()
        nav_after = nav_after_date = None
        for d, nav in series:  # 升序：取交易后 horizon 天首个可用净值
            if d >= target:
                nav_after, nav_after_date = nav, d
                break
        nav_at = t.get("nav")
        if not nav_at and series:
            for d, nav in reversed(series):
                if d <= str(t["date"])[:10]:
                    nav_at = nav
                    break
        ev = evaluate_trade_timing(t["action"], nav_at, nav_after, horizon_days=horizon)
        out = {
            "trade_id": t.get("id"), "date": str(t["date"])[:10],
            "code": t["code"], "name": t["name"], "action": t["action"],
            "amount": t.get("amount"), "nav_at_trade": nav_at,
            "nav_after": nav_after, "nav_after_date": nav_after_date,
            "badge": VERDICT_BADGE.get(ev["verdict"], ev["verdict"]),
            **ev,
        }
        results.append(out)
        if save and ev["score"] is not None:
            db.add_trade_review(
                account_id, t.get("id"), t["code"], t["name"], t["action"],
                out["date"], horizon, nav_at, nav_after, nav_after_date,
                ev["post_return_pct"], ev["verdict"], ev["score"], ev["lesson"])
    return results, pending


def summarize_reviews(results):
    """从 build_trade_reviews 的 results 现算宏观总结（无需读库）。"""
    scored = [r for r in results if r.get("score") is not None]
    if not scored:
        return {"count": 0}

    def _wr(items):
        s = [r for r in items if r.get("score") is not None]
        if not s:
            return None
        return round(sum(1 for r in s if r["score"] > 0) / len(s), 4)

    buys = [r for r in scored if r["action"] == "buy"]
    sells = [r for r in scored if r["action"] == "sell"]
    return {
        "count": len(scored),
        "buy_count": len(buys), "sell_count": len(sells),
        "buy_timing_winrate": _wr(buys), "sell_timing_winrate": _wr(sells),
        "avg_score": round(sum(r["score"] for r in scored) / len(scored), 4),
    }


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

            if args.format == "brief":
                print(_format_brief(packet))
            elif args.format == "md":
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


def _format_brief(packet):
    """Phase 4: 3-5 line condensed summary for the "快速看看" mode."""
    r = packet["market_regime"]
    s = packet["portfolio_snapshot"]
    lines = [
        f"📅 {packet['date']} 账户 {packet['account']} — 大盘 {r['label']} "
        f"(HS300 5d {r['hs300_5d_return']*100:+.1f}%)",
        f"💰 总 ¥{s['total_value']:,.0f} | 现金 {s['cash_pct']*100:.0f}% | "
        f"持仓 {s['position_pct']*100:.0f}%",
    ]
    sm = packet["summary"]
    counts = sm["action_count"]
    if any(counts.values()):
        actions_line = " | ".join(
            f"{k} {v}" for k, v in counts.items() if v > 0
        )
        lines.append(f"🎯 引擎建议：{actions_line}")
    else:
        lines.append("🎯 引擎建议：无操作（hold）")
    # Top action
    hi = sm.get("highest_confidence_action")
    if hi:
        top = next(
            a for a in packet["actions"]
            if a["code"] == hi["code"] and a["action"] == hi["action"]
        )
        sym = {"buy": "🟢", "sell": "🔴", "watch": "🟡", "hold": "⏸"}.get(
            top["action"], "•",
        )
        if top["action"] == "buy":
            detail = f"¥{top['suggested_amount']:.0f}"
        elif top["action"] == "sell":
            detail = f"{top['suggested_shares']:.0f} 份"
        else:
            detail = ""
        lines.append(
            f"{sym} {top['rule_label']}: {top['name']} {detail} "
            f"(conf {top['confidence']:.2f})"
        )
    if packet["alerts"]:
        lines.append(f"⚠️  {packet['alerts'][0]['reason_zh']}")
    return "\n".join(lines)


def cmd_why_not(args):
    """Phase 4: 用户问"为什么没建议买 XXX"，快速给答案."""
    # 跑一次 decide，然后查 blocked/actions/alerts 看代码是否出现
    db = Database()
    try:
        row = db.conn.execute(
            "SELECT id FROM accounts WHERE name = ?", (args.account,)
        ).fetchone()
        if not row:
            print(f"[ERROR] account '{args.account}' not found", file=sys.stderr)
            return 2
        account_id = row["id"]

        snap = fetch_fund.gather_market_snapshot(account_name=args.account)
        if isinstance(snap, dict) and "error" in snap:
            print(snap["error"], file=sys.stderr)
            return 2

        # If user's target code isn't in the snapshot's funds, we add it ourselves
        funds = snap.get("funds", {}) or {}
        if args.code not in funds:
            extra = fetch_fund._fund_snapshot(args.code, None, None)
            if extra:
                funds[args.code] = extra
                snap["funds"] = funds

        positions = []
        for r in db.conn.execute(
            "SELECT code, name, shares, cost_nav, sector, buy_date "
            "FROM positions WHERE account_id = ?",
            (account_id,),
        ):
            positions.append({
                "code": r["code"], "name": r["name"],
                "shares": r["shares"], "cost_nav": r["cost_nav"],
                "sector": r["sector"], "hold_days": 0,
            })
        cash_row = db.conn.execute(
            "SELECT cash FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        cash = cash_row["cash"] if cash_row else 0.0
        position_value = sum(
            p["shares"] * (funds.get(p["code"], {}) or {}).get("current_nav", p["cost_nav"])
            for p in positions
        )
        total_value = cash + position_value
        engine = DecisionEngine(db, account_id)
        packet = engine.decide(
            date=datetime.now().strftime("%Y-%m-%d"),
            market_data=snap, positions=positions,
            cash=cash, total_value=total_value,
        )

        code = args.code
        print(f"# 为什么没建议买 {code}\n")

        # 1. Was it actively recommended?
        actions = [a for a in packet["actions"] if a["code"] == code]
        if any(a["action"] == "buy" for a in actions):
            buy = [a for a in actions if a["action"] == "buy"][0]
            print(f"✅ 引擎其实是有建议买的：")
            print(f"   - 规则：{buy['rule_label']} (`{buy['rule_id']}`)")
            print(f"   - 金额：¥{buy['suggested_amount']:.0f}")
            print(f"   - 置信度：{buy['confidence']:.2f}")
            print(f"   - 理由：{buy['reason_zh']}")
            return 0

        # 2. Was it blocked?
        blocked = [b for b in packet["blocked_actions"] if b["code"] == code]
        if blocked:
            print(f"❌ 引擎想买但被前置检查拦截：")
            for b in blocked:
                print(f"   - 拦截规则：`{b['blocked_by']}`")
                print(f"   - 理由：{b['reason_zh']}")
            return 0

        # 3. Was data missing?
        data_alerts = [
            a for a in packet["alerts"]
            if a.get("id") == "data_missing" and a.get("code") == code
        ]
        if data_alerts:
            print(f"⚠️ 该基金数据缺失，引擎跳过了决策：")
            print(f"   - {data_alerts[0]['reason_zh']}")
            return 0

        # 4. Otherwise: low_buy condition was not met
        fund = funds.get(code)
        if fund:
            day_r = fund.get("day_return", 0.0)
            r5 = fund.get("fund_5d_return", 0.0)
            print(
                f"ℹ️ 该基金未触发任何买入规则（low_buy 要求当日跌 > 3%）："
            )
            print(f"   - 当日涨跌：{day_r * 100:+.2f}%")
            print(f"   - 近 5 天涨跌：{r5 * 100:+.2f}%")
            print(f"   - 现金占比：{packet['portfolio_snapshot']['cash_pct']*100:.1f}%")
            print(f"   - 大盘环境：{packet['market_regime']['label']}")
            if day_r > -0.03:
                print(f"   → 没有跌得够深以触发低吸；可以加入观察池等待回调。")
        else:
            print(f"❓ 基金 {code} 不在持仓也不在候选池，引擎未评估。")
        return 0
    finally:
        db.close()


def cmd_stats(args):
    """Print per-rule win/loss stats for an account."""
    db = Database()
    try:
        row = db.conn.execute(
            "SELECT id FROM accounts WHERE name = ?", (args.account,)
        ).fetchone()
        if not row:
            print(f"[ERROR] account '{args.account}' not found", file=sys.stderr)
            return 2
        engine = DecisionEngine(db, row["id"])
        stats = engine.compute_rule_stats(
            start_date=args.start, end_date=args.end,
        )
        if args.format == "json":
            print(json.dumps(stats, ensure_ascii=False, indent=2))
            return 0
        # Markdown table
        if not stats:
            print(f"# 规则统计 — 账户 {args.account}\n\n（无已平仓交易数据）")
            return 0
        print(f"# 规则统计 — 账户 {args.account}")
        if args.start or args.end:
            print(f"\n区间: {args.start or '起始'} → {args.end or '至今'}")
        print()
        print("| rule_id | 次数 | 胜率 | 均盈 | 均亏 | 期望 |")
        print("|---------|-----:|------:|------:|------:|------:|")
        for s in stats:
            print(
                f"| `{s['rule_id']}` | {s['count']} | "
                f"{s['win_rate'] * 100:.0f}% | "
                f"{s['avg_profit_pct_wins'] * 100:+.2f}% | "
                f"{s['avg_profit_pct_losses'] * 100:+.2f}% | "
                f"**{s['expectancy'] * 100:+.2f}%** |"
            )
        print()
        # Suggestions
        suggestions = []
        for s in stats:
            if s["count"] >= 10 and s["expectancy"] < -0.005:
                suggestions.append(
                    f"- ⚠️ `{s['rule_id']}` 期望 {s['expectancy'] * 100:+.2f}% "
                    f"（{s['count']} 次样本），考虑收紧触发条件或降低权重。"
                )
            elif s["count"] < 5:
                suggestions.append(
                    f"- ℹ️ `{s['rule_id']}` 样本只 {s['count']} 次，"
                    f"统计不显著，继续观察。"
                )
            elif s["expectancy"] >= 0.05:
                suggestions.append(
                    f"- ✅ `{s['rule_id']}` 期望 {s['expectancy'] * 100:+.2f}% "
                    f"（{s['count']} 次样本），表现稳定，可考虑放宽触发条件吸纳更多机会。"
                )
        if suggestions:
            print("## 建议\n")
            for s in suggestions:
                print(s)
        return 0
    finally:
        db.close()


def cmd_evolve(args):
    """Read rule_stats from an account, write a strategy_evolutions row with suggestions."""
    db = Database()
    try:
        row = db.conn.execute(
            "SELECT id, strategy_version FROM accounts WHERE name = ?",
            (args.account,),
        ).fetchone()
        if not row:
            print(f"[ERROR] account '{args.account}' not found", file=sys.stderr)
            return 2
        account_id = row["id"]
        from_version = row["strategy_version"] or "v2.0"
        to_version = args.to_version or f"{from_version}+r{int(datetime.now().timestamp())}"

        engine = DecisionEngine(db, account_id)
        stats = engine.compute_rule_stats()
        if not stats:
            print("[INFO] no closed trades found — nothing to evolve from")
            return 2

        # Build summary metrics
        before_metrics = {
            "total_trades": sum(s["count"] for s in stats),
            "rules": {s["rule_id"]: {
                "count": s["count"],
                "win_rate": s["win_rate"],
                "expectancy": s["expectancy"],
            } for s in stats},
        }

        # Generate suggestions
        suggestions = []
        for s in stats:
            if s["count"] >= 10 and s["expectancy"] < -0.005:
                suggestions.append(
                    f"`{s['rule_id']}` 期望 {s['expectancy'] * 100:+.2f}% "
                    f"({s['count']} 样本) → 建议收紧触发阈值或降低仓位倍数"
                )
            elif s["count"] >= 10 and s["expectancy"] >= 0.05:
                suggestions.append(
                    f"`{s['rule_id']}` 期望 {s['expectancy'] * 100:+.2f}% "
                    f"({s['count']} 样本) → 表现稳定，可放宽触发条件吸纳更多机会"
                )
            elif s["count"] < 5:
                suggestions.append(
                    f"`{s['rule_id']}` 仅 {s['count']} 样本 → 继续观察"
                )

        lessons = "\n".join(f"- {x}" for x in suggestions) or "（无足够样本产生建议）"

        title = args.title or (
            f"从账户 {args.account} 的回测/历史数据产生的规则评估 ({to_version})"
        )
        description = args.description or (
            f"基于 {before_metrics['total_trades']} 笔已平仓交易，"
            f"按 rule_id 聚合的胜率与期望分析。"
        )

        evolution_id = db.add_evolution(
            from_version=from_version,
            to_version=to_version,
            title=title,
            description=description,
            trigger_source=f"account:{args.account}",
            trigger_detail=args.sim_id,
            before_metrics=before_metrics,
            after_metrics=None,  # filled when new version is actually deployed
            lessons_learned=lessons,
        )
        print(f"\n[evolution #{evolution_id}] from {from_version} → {to_version}")
        print(f"  {len(stats)} 条规则统计已保存")
        print(f"  {len(suggestions)} 条建议:")
        for s in suggestions:
            print(f"    - {s}")
        return 0
    finally:
        db.close()


def cmd_review(args):
    """复盘历史操作：事后 N 天净值判断当初买卖是否踩中，--save 写入记忆。"""
    db = Database()
    try:
        row = db.conn.execute(
            "SELECT id FROM accounts WHERE name = ?", (args.account,)
        ).fetchone()
        if not row:
            print(f"[ERROR] account '{args.account}' not found", file=sys.stderr)
            return 2
        account_id = row["id"]

        # --summary：只读已存评定（记忆）
        if args.summary:
            summary = db.get_review_summary(account_id, lookback_days=args.lookback)
            if args.format == "json":
                print(json.dumps(summary, ensure_ascii=False, indent=2))
                return 0
            _print_review_macro(args.account, summary, stored=True)
            return 0

        results, pending = build_trade_reviews(
            db, account_id, horizon=args.horizon, lookback=args.lookback, save=args.save)

        if args.format == "json":
            print(json.dumps(
                {"reviews": results, "pending": pending,
                 "summary": summarize_reviews(results)},
                ensure_ascii=False, indent=2, default=str))
            return 0

        if not results:
            print(f"# 操作复盘 — 账户 {args.account}\n\n"
                  f"近 {args.lookback} 天内没有满 {args.horizon} 天可评定的操作"
                  f"（{pending} 笔太新、待观察）。")
            return 0

        print(f"# 操作复盘评定 — 账户 {args.account}（事后 {args.horizon} 天回看）\n")
        print("| 交易日 | 方向 | 基金 | 事后涨跌 | 评定 | 教训 |")
        print("|--------|------|------|---------:|------|------|")
        for r in results:
            act = "买入" if r["action"] == "buy" else "卖出"
            pr = r.get("post_return_pct")
            pr_s = f"{pr*100:+.2f}%" if pr is not None else "—"
            print(f"| {r['date']} | {act} | {r['name']} | {pr_s} | "
                  f"{r['badge']} | {r['lesson']} |")
        if pending:
            print(f"\n> 另有 {pending} 笔操作太新（不足 {args.horizon} 天），暂列待观察。")
        print()
        _print_review_macro(args.account, summarize_reviews(results), stored=False)
        if args.save:
            print(f"\n✅ 已写入记忆（trade_reviews），可用 `db.py reviews --account {args.account}` 回看。")
        return 0
    finally:
        db.close()


def _print_review_macro(account, summary, stored=False):
    """打印复盘宏观总结。"""
    if not summary.get("count"):
        print("（暂无足够样本产生宏观结论）")
        return
    bw, sw = summary.get("buy_timing_winrate"), summary.get("sell_timing_winrate")
    bw_s = f"{bw*100:.0f}%" if bw is not None else "—"
    sw_s = f"{sw*100:.0f}%" if sw is not None else "—"
    src = "（已存记忆）" if stored else ""
    print(f"## 宏观复盘{src}\n")
    print(f"- 买入择时胜率 **{bw_s}**（{summary.get('buy_count',0)} 笔）"
          f" · 卖出择时胜率 **{sw_s}**（{summary.get('sell_count',0)} 笔）"
          f" · 综合均分 **{summary.get('avg_score')}**")
    if summary.get("recent_lessons"):
        print("- 近期教训：")
        for ls in summary["recent_lessons"]:
            print(f"  - {ls}")


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
        "--format", choices=["json", "md", "brief"], default="json",
        help="输出格式（默认: json；brief 是 3-5 行摘要）",
    )
    p.set_defaults(func=cmd_run)

    p_stats = sub.add_parser("stats", help="按规则查询历史胜率/期望")
    p_stats.add_argument("--account", default="主线", help="账户名称")
    p_stats.add_argument("--start", default=None, help="起始日期 YYYY-MM-DD")
    p_stats.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD")
    p_stats.add_argument(
        "--format", choices=["json", "md"], default="md",
        help="输出格式（默认: md）",
    )
    p_stats.set_defaults(func=cmd_stats)

    p_evolve = sub.add_parser(
        "evolve",
        help="基于规则胜率/期望产生进化建议并写入 strategy_evolutions 表",
    )
    p_evolve.add_argument("--account", default="主线", help="账户名称")
    p_evolve.add_argument("--to-version", default=None, help="新版本号（默认: 自动生成）")
    p_evolve.add_argument("--title", default=None, help="进化标题")
    p_evolve.add_argument("--description", default=None, help="详细描述")
    p_evolve.add_argument(
        "--sim-id", default=None,
        help="trigger_detail（若来自某次回测可填 sim_id）",
    )
    p_evolve.set_defaults(func=cmd_evolve)

    p_why = sub.add_parser(
        "why-not",
        help="为什么没建议买 XXX？查 blocked_actions / alerts / 规则未触发原因",
    )
    p_why.add_argument("--account", default="主线", help="账户名称")
    p_why.add_argument("--code", required=True, help="基金代码")
    p_why.set_defaults(func=cmd_why_not)

    p_review = sub.add_parser(
        "review",
        help="复盘历史操作：事后N天净值判断当初买卖是否踩中，--save 写入记忆",
    )
    p_review.add_argument("--account", default="主线", help="账户名称")
    p_review.add_argument("--horizon", type=int, default=7,
                          help="事后回看天数（自然日，默认7≈一周）")
    p_review.add_argument("--lookback", type=int, default=60,
                          help="复盘最近多少天内的操作（默认60）")
    p_review.add_argument("--save", action="store_true",
                          help="把评定写入 trade_reviews（记忆），供宏观判断复用")
    p_review.add_argument("--summary", action="store_true",
                          help="只读已存记忆的宏观总结，不重新计算")
    p_review.add_argument("--format", choices=["md", "json"], default="md",
                          help="输出格式（默认: md）")
    p_review.set_defaults(func=cmd_review)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
