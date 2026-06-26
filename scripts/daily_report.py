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
    "close": {"label": "盘尾", "time": "14:30"},
}

# 让利润奔跑：盈利不机械止盈（趋势品种越涨越卖反而踏空），只在亏损时止损。
# 止盈规则仍会在卡片里提示，但自动记账不执行卖出。设 False 可恢复机械止盈。
LET_WINNERS_RUN = True
TAKE_PROFIT_RULES = {
    "take_profit_tier_20", "take_profit_tier_30",
    "take_profit_tier_40", "take_profit_clearout",
}


# ---------- 数据组装 ----------

def build_context(db, account, date, discover=0):
    """复刻 decide.cmd_run 的决策包构建，并保留原始 funds 快照（含 day_return）。

    discover>0：额外注入 N 只跨板块发现的新候选（仅供卡片「新方向」展示，
    auto_record 不会自动买入 source=discovered 的标的，避免每日轮换追新）。
    """
    snap = fetch_fund.gather_market_snapshot(account_name=account, date=date,
                                             discover=discover)
    if isinstance(snap, dict) and "error" in snap:
        return None, snap["error"]
    row = db.conn.execute(
        "SELECT id, cash, budget FROM accounts WHERE name = ?", (account,)
    ).fetchone()
    if not row:
        return None, f"account '{account}' not found"
    account_id = row["id"]
    cash = row["cash"] or 0.0
    budget = (row["budget"] if "budget" in row.keys() else None) or 0.0

    positions = []
    today_str = datetime.now().strftime("%Y-%m-%d")
    for r in db.conn.execute(
        "SELECT code, name, shares, cost_nav, sector, buy_date "
        "FROM positions WHERE account_id = ?", (account_id,)
    ):
        hold_days = 0
        is_pending = False
        if r["buy_date"]:
            try:
                buy_dt = datetime.fromisoformat(r["buy_date"])
                hold_days = (datetime.now() - buy_dt).days
                is_pending = (r["buy_date"] == today_str)
            except Exception:
                pass
        positions.append({
            "code": r["code"], "name": r["name"], "shares": r["shares"],
            "cost_nav": r["cost_nav"], "sector": r["sector"], "hold_days": hold_days,
            "is_pending": is_pending,
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
    try:
        dca_plans = db.get_dca_plans(account_id, enabled_only=True)
    except Exception:
        dca_plans = []
    return {
        "packet": packet, "funds": funds, "positions": positions,
        "account_id": account_id, "total_value": total_value, "cash": cash,
        "news": snap.get("news", []) or [], "account": account,
        "dca_plans": dca_plans, "discovered": snap.get("discovered", []) or [],
        "budget": budget, "date": date,
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

def spark_lines(label, pre_close, values):
    """:::spark DSL 行。纯函数：label + 昨收 + 价格序列 → 卡片 markdown 行。"""
    if not values or len(values) < 2:
        return []
    last = values[-1]
    if pre_close:
        pct = (last - pre_close) / pre_close * 100
    else:
        pct = (last - values[0]) / values[0] * 100 if values[0] else 0.0
    sign = "+" if pct >= 0 else ""
    csv = ",".join(f"{v:.2f}" for v in values)
    return [":::spark", f"{label} | {last:,.2f} {sign}{pct:.2f}%", csv, ":::", ""]


def card_spark(ctx):
    """纳指100 隔夜分时 sparkline（QDII 持仓方向参考）。网络失败静默跳过。"""
    try:
        trend = fetch_fund.fetch_index_trend("100.NDX", ndays=1)
        if not trend or len(trend["points"]) < 10:
            return []
        import chart as chart_mod
        vals = chart_mod.downsample([p[1] for p in trend["points"]], 70)
        return spark_lines("纳指100 隔夜走势（006479 方向）", trend.get("pre_close"), vals)
    except Exception:
        return []


def card_top(ctx, session):
    sess = SESSIONS[session]
    funds, positions = ctx["funds"], ctx["positions"]
    total_today = 0.0
    total_cost = 0.0
    total_value = 0.0
    for p in positions:
        if p.get("is_pending"):
            continue
        f = funds.get(p["code"]) or {}
        cur = f.get("current_nav", p["cost_nav"])
        dr = f.get("day_return", 0.0) or 0.0
        total_today += _today_pnl(p["shares"], cur, dr)
        total_cost += p["shares"] * p["cost_nav"]
        total_value += p["shares"] * cur
    cum = total_value - total_cost
    cum_pct = (cum / total_cost * 100) if total_cost else 0.0  # 持仓收益率：÷持仓成本
    label = f"今日估算盈亏（{sess['label']} {sess['time']}）"
    stats = (f"持仓市值 ¥{total_value:,.0f} | 持仓累计 {_fmt_amt(cum)} "
             f"| {cum_pct:+.2f}%")
    return [":::card", label, f"{total_today:+,.2f}元", stats, ":::", ""]


def card_wallet(ctx):
    """总钱包卡片：总钱包(持仓+现金) / 可用现金 / 现金储备线(10%) / 定投额度
    + 总收益近30天迷你走势。收益口径与快照一致（已确认持仓的成本制浮盈）。"""
    funds, positions = ctx["funds"], ctx["positions"]
    cash = ctx.get("cash", 0.0) or 0.0
    conf_mv = conf_cost = pend_cost = 0.0
    for p in positions:
        if p.get("is_pending"):
            pend_cost += p["shares"] * p["cost_nav"]      # 待确认按成本计
            continue
        cur = (funds.get(p["code"]) or {}).get("current_nav", p["cost_nav"])
        conf_mv += p["shares"] * cur
        conf_cost += p["shares"] * p["cost_nav"]
    pos_value = conf_mv + pend_cost
    total_wallet = cash + pos_value
    total_pnl = conf_mv - conf_cost                        # 已确认持仓浮盈
    pnl_pct = (total_pnl / conf_cost * 100) if conf_cost else 0.0  # 持仓收益率：÷持仓成本，现金不计入
    budget = ctx.get("budget", 0.0) or 0.0
    reserve = total_wallet * 0.10
    reserve_state = "充足" if cash >= reserve else "不足，慎再加仓"

    out = [
        ":::card",
        "总钱包（持仓市值 + 现金）",
        f"¥{total_wallet:,.0f}",
        f"持仓市值 ¥{pos_value:,.0f} | 可用现金 ¥{cash:,.0f} | "
        f"持仓收益 {_fmt_amt(total_pnl)} ({pnl_pct:+.2f}%)",
        ":::",
        "",
        "### 本金 & 现金",
    ]
    if budget:
        out.append(f"- 本金（累计投入）：¥{budget:,.0f}")
    # 现金 = 本金 − 持仓成本 仅在无已实现盈亏时成立；对得上才标注公式，避免卖出后误导。
    reconciles = budget and abs(cash - (budget - conf_cost)) < 1.0
    cash_note = "（= 本金 − 持仓成本）" if reconciles else ""
    out += [
        f"- 持仓成本 ¥{conf_cost:,.0f}　持仓市值 ¥{conf_mv:,.0f}　"
        f"持仓收益 {_fmt_amt(total_pnl)}（{pnl_pct:+.2f}%，现金不计入）",
        f"- 可用现金：¥{cash:,.0f}{cash_note}",
        f"- 现金储备线(10%)：¥{reserve:,.0f}（{reserve_state}）",
    ]
    plans = ctx.get("dca_plans") or []
    if plans:
        quota = "；".join(
            f"¥{(pl['amount'] or 0):,.0f}/{pl.get('frequency', '')} "
            f"{_short_name(pl.get('name') or pl.get('code', ''))}"
            for pl in plans
        )
        out.append(f"- 定投额度：{quota}")
    out.append("")

    # 总收益变化迷你走势（净值派生，红涨绿跌）
    try:
        series = fetch_fund.portfolio_return_series(ctx.get("account"), days=30)
    except Exception:
        series = []
    if series and len(series) >= 2:
        vals = [round(p * 100, 2) for _, p in series]
        latest = vals[-1]
        sign = "+" if latest >= 0 else ""
        csv = ",".join(f"{v:.2f}" for v in vals)
        out += [":::spark",
                f"总收益走势（近30天，持仓） | {sign}{latest:.2f}%",
                csv, ":::", ""]
    return out


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
            instr = []
            for a in act:
                if a["action"] == "buy":
                    instr.append(f"支付宝/天天基金搜 {a['code']} "
                                 f"买入 ¥{a['suggested_amount']:,.0f}")
                else:
                    instr.append(f"卖出 {a['code']} "
                                 f"{a.get('suggested_shares') or 0:,.0f} 份"
                                 f"（约 ¥{a.get('suggested_amount') or 0:,.0f}）")
            parts.append("📋 照抄指令：" + "；".join(instr) + "。")
        elif held_winner:
            parts.append(prefix + "已盈利，继续持有让利润奔跑，跌破止损再走。")
        else:
            parts.append(prefix + ("按兵不动，持有观察。" if session == "open"
                                    else "暂无操作信号，维持持有，尾盘再确认。"))
    if overnight:
        parts.append(overnight)
    return [":::action", " ".join(parts), ":::", ""]


# ---------- 今日操作计划（三时段预告 / 划改对比，R: 提前确认 + 旁注说明）----------

def _plan_ops_from_packet(ctx):
    """从决策包抽出今日可执行操作（买/卖），带份额类别与说明。

    排除：止盈（让利润奔跑）、跨板块发现的新候选（在「值得关注」卡，不自动执行）。
    """
    packet = ctx.get("packet", {})
    funds = ctx.get("funds", {})
    ops = []
    for a in packet.get("actions", []):
        if a.get("action") not in ("buy", "sell"):
            continue
        if LET_WINNERS_RUN and a.get("rule_id") in TAKE_PROFIT_RULES:
            continue
        if (funds.get(a.get("code")) or {}).get("source") == "discovered":
            continue
        sc = a.get("share_class") or {}
        ops.append({
            "action": a["action"], "code": a["code"], "name": a.get("name", ""),
            "amount": a.get("suggested_amount"), "shares": a.get("suggested_shares"),
            "rule_id": a.get("rule_id"), "rule_label": a.get("rule_label", ""),
            "reason": a.get("reason_zh", ""),
            "pref_class": sc.get("preferred"), "cur_class": sc.get("current"),
        })
    return ops


def _op_line(op):
    """把一条操作渲染成「买入「X」¥金额（规则）［短/长线→份额］」。"""
    if op["action"] == "buy":
        head = f"买入「{_short_name(op['name'])}」¥{(op.get('amount') or 0):,.0f}"
    else:
        head = (f"卖出「{_short_name(op['name'])}」约 "
                f"{(op.get('shares') or 0):,.0f} 份")
    tail = f"（{op['rule_label']}）" if op.get("rule_label") else ""
    sc = ""
    pref = op.get("pref_class")
    if pref:
        term = "短线" if pref == "C" else "长线"
        if op.get("cur_class") and op["cur_class"] != pref:
            sc = f"［{term}→改买{pref}类兄弟份额］"
        else:
            sc = f"［{term}→{pref}类］"
    return f"{head}{tail}{sc}"


def _explain_dropped(op, ctx):
    """解释开盘计划里某操作为何在盘中/盘尾被撤销（旁注说明）。"""
    code = op["code"]
    for b in ctx.get("packet", {}).get("blocked_actions", []):
        if b.get("code") == code:
            return b.get("reason_zh", "被风控拦截")
    f = (ctx.get("funds") or {}).get(code) or {}
    dr = f.get("day_return")
    if op["action"] == "buy" and dr is not None:
        return (f"{_short_name(op['name'])} 当前 {dr*100:+.1f}%，"
                f"低吸/买入条件不再满足")
    return "市场变化，引擎不再建议"


def card_plan(ctx, session, db, persist=False):
    """今日操作计划：开盘预告 → 盘中/盘尾对比开盘，撤销项划掉+说明、新增项标注、盘尾确认。

    persist=True 才落库（仅定时真跑时）；web 面板/预览只读渲染，绝不覆盖开盘基线。
    """
    account_id = ctx["account_id"]
    date = ctx.get("date") or datetime.now().strftime("%Y-%m-%d")
    cur = _plan_ops_from_packet(ctx)
    cur_keys = {(o["action"], o["code"]) for o in cur}

    def _save(sess):
        if not persist:
            return
        try:
            db.save_daily_plan(account_id, date, sess, cur)
        except Exception:
            pass

    if session == "open":
        out = ["### 📋 今日操作计划（开盘预告）", ""]
        if cur:
            for o in cur:
                out.append(f"- 拟{_op_line(o)} — {o.get('reason', '')}")
        else:
            out.append("- 今日暂无操作计划，持有观察（盘中/盘尾有变化再更新）。")
        _save("open")
        out.append("")
        return out

    # 盘中 / 盘尾：对比开盘计划
    try:
        prev = db.get_daily_plan(account_id, date, "open") or []
    except Exception:
        prev = []
    prev_keys = {(o["action"], o["code"]) for o in prev}
    label = "盘中更新" if session == "mid" else "盘尾最终确认"
    out = [f"### 📋 今日操作计划（{label}）", ""]

    # 没有开盘基线（如只跑了盘尾）→ 当成新计划直接列，不逐条标「新增」
    if not prev:
        if cur:
            for o in cur:
                out.append(f"- 拟{_op_line(o)} — {o.get('reason', '')}")
        else:
            out.append("- 全天无操作信号，持有观察。")
        if session == "close":
            out.append("**✅ 今日最终就这么操作"
                       + ("（已按上方执行并记账）。**" if cur else "：不操作，全部持有。**"))
        _save(session)
        out.append("")
        return out

    changed = False
    for o in prev:
        if (o["action"], o["code"]) in cur_keys:
            out.append(f"- ✅ 维持：{_op_line(o)}")
        else:
            changed = True
            out.append(f"- ~~{_op_line(o)}~~ ❌ 撤销：{_explain_dropped(o, ctx)}")
    for o in cur:
        if (o["action"], o["code"]) not in prev_keys:
            changed = True
            out.append(f"- 🆕 新增：{_op_line(o)} — {o.get('reason', '')}")

    if not changed:
        out.append("（较开盘计划无变化）")

    if session == "close":
        out.append("**✅ 今日最终就这么操作"
                   + ("（已按上方执行并记账）。**" if cur else "：不操作，全部持有。**"))
    _save(session)
    out.append("")
    return out


def card_position(ctx):
    """P6: 总仓位概览 —— 每封日报都让用户看到仓位 vs 目标区间。"""
    adv = ctx["packet"].get("portfolio_advice")
    if not adv:
        return []
    icon = {"underweight": "📉", "overweight": "📈", "in_band": "✅"}.get(
        adv["status"], "•")
    return [
        "### 📊 仓位概览", "",
        f"{icon} 仓位 **{adv['position_pct'] * 100:.0f}%**"
        f"（目标 {adv['target_floor'] * 100:.0f}%~"
        f"{adv['position_cap'] * 100:.0f}%）| "
        f"可部署现金 ¥{adv['deployable_cash']:,.0f}",
        "",
        f"> {adv['advice_zh']}",
        "",
    ]


def card_holdings(ctx):
    """每只持仓：今日% / 今日预估收益(元) / 持有收益(元) / 累计% / 市值 / 天数。

    列语义对齐 send_email 持仓卡渲染器（cells[3]=持有收益金额）。
    持有收益 = (现价 − 成本) × 份额；待确认基金当日不计收益。
    """
    funds, positions = ctx["funds"], ctx["positions"]
    by_pos = {p["code"]: p for p in ctx["packet"]["portfolio_snapshot"]["by_position"]}
    out = ["### 我的持仓（实时估值）", "",
           "| 基金 | 今日 | 今日盈亏 | 持有收益 | 累计 | 市值 | 天数 |",
           "|------|------|---------|---------|------|------|------|"]
    for p in positions:
        f = funds.get(p["code"]) or {}
        cur = f.get("current_nav", p["cost_nav"])
        value = p["shares"] * cur
        if p.get("is_pending"):
            out.append(
                f"| {_short_name(p['name'])} | -- | 待确认 | -- | "
                f"-- | {value:,.0f} | -- |"
            )
            continue
        dr = (f.get("day_return", 0.0) or 0.0)
        today_pnl = _today_pnl(p["shares"], cur, dr)
        hold_pnl = (cur - p["cost_nav"]) * p["shares"]   # 持有收益（累计浮盈金额）
        cum_pct = by_pos.get(p["code"], {}).get("profit_pct", 0.0) * 100
        tp = _fmt_amt(today_pnl) if dr else "--"
        hd = p.get("hold_days") or 0
        hd_s = f"{hd}天" if hd else "--"
        out.append(
            f"| {_short_name(p['name'])} | {dr*100:+.2f}% | {tp} | {_fmt_amt(hold_pnl)} | "
            f"{cum_pct:+.2f}% | {value:,.0f} | {hd_s} |"
        )
    out.append("")
    return out


def card_holding_sparks(ctx, days=30):
    """每只持仓近 N 天净值迷你走势（用户要的「持仓走势图都列出来」）。

    逐只拉净值序列 → downsample → :::spark。某只网络失败则静默跳过它。
    """
    positions = ctx["positions"]
    if not positions:
        return []
    try:
        import chart as chart_mod
    except Exception:
        return []
    out = [f"### 持仓走势（近{days}天净值）", ""]
    any_spark = False
    for p in positions:
        series = fetch_fund.fetch_nav_series(p["code"], days=days)
        navs = [nav for _, nav in series]
        if len(navs) < 5:
            continue
        vals = chart_mod.downsample(navs, 60)
        out += spark_lines(f"{_short_name(p['name'])} 近{days}天净值", navs[0], vals)
        any_spark = True
    return out if any_spark else []


def card_news(ctx, limit=3):
    """财经要闻（免费 7x24 快讯）。优先用 snapshot 已抓的 news，否则现拉。失败跳过。"""
    news = (ctx.get("news") if isinstance(ctx, dict) else None) or []
    if not news:
        try:
            news = fetch_fund.gather_market_news(limit=limit)
        except Exception:
            news = []
    if not news:
        return []
    out = ["### 财经要闻", ""]
    for it in news[:limit]:
        ts = (it.get("time") or "")[-5:]
        title = it.get("title", "").strip()
        out.append(f"- {('['+ts+'] ') if ts else ''}{title}")
    out.append("")
    return out


def card_discover(ctx):
    """新方向候选（跨板块发现）。只展示供人工结合短C长A判断，不自动买入。"""
    disc = (ctx.get("discovered") if isinstance(ctx, dict) else None) or []
    if not disc:
        return []
    out = ["### 🔭 值得关注的新方向（跨板块发现，未持有）", ""]
    for d in disc[:5]:
        sc = f" · 评分{d['score']:.0f}" if d.get("score") is not None else ""
        out.append(f"- {d['name']}（{d['code']}）— {d.get('sector', '其他')}{sc}")
    out.append("> 跳出固定持仓看更多赛道；选定后短线买C类、长线买A类。")
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


def card_timeline(db, ctx):
    """近期操作时间线 + 事后复盘点评（每笔标注「精准踩中 / 卖飞了」+ 事后涨跌）。"""
    account_id = ctx["account_id"]
    rows = db.get_trades(account_id, limit=8)
    if not rows:
        return []

    # 复盘评定：trade_id → 评定结果；并产出宏观胜率（save=True 顺手写入记忆，幂等 upsert）
    reviews, macro = {}, {}
    try:
        from decide import build_trade_reviews, summarize_reviews
        results, _ = build_trade_reviews(db, account_id, horizon=7, lookback=45, save=True)
        for r in results:
            if r.get("trade_id") is not None and r.get("score") is not None:
                reviews[r["trade_id"]] = r
        macro = summarize_reviews(results)
    except Exception:
        pass

    out = ["### 近期操作 & 复盘", ""]
    if macro.get("count"):
        bw, sw = macro.get("buy_timing_winrate"), macro.get("sell_timing_winrate")
        bw_s = f"{bw*100:.0f}%" if bw is not None else "—"
        sw_s = f"{sw*100:.0f}%" if sw is not None else "—"
        out += [f"近 {macro['count']} 笔已评定：买入择时胜率 {bw_s}、卖出择时胜率 {sw_s}。逐笔回看 👇", ""]
    out += [":::timeline"]
    for t in rows:
        dt = str(t["date"])[5:].replace("-", "/")
        act = "买入" if t["action"] == "buy" else "卖出"
        line = f"{dt} | {act} {_short_name(t['name'])} ¥{t['amount']:,.0f}"
        rv = reviews.get(t["id"])
        if rv:
            pr = rv.get("post_return_pct")
            pr_s = f"（事后{pr*100:+.1f}%）" if pr is not None else ""
            line += f" → {rv['badge']}{pr_s}"
        out.append(line)
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
            if f.get("source") == "discovered":
                # 新发现的跨板块候选：只展示、不自动买，留给人工结合短C长A判断
                skipped.append(f"{_short_name(name)} 新方向候选（待人工确认，自动路不追新）")
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
            _adjust_cash(db, account_id, -amt)
            recorded.append(f"买入「{_short_name(name)}」¥{amt:,.0f}（支付宝搜 {code}）")
            if do_email:
                news = fetch_fund.relevant_news(ctx.get("news") or [],
                                                name=name, sector=a.get("sector"))
                _notify(account, "buy", code, name, amt, nav, shares,
                        a.get("rule_label", ""), reason=a.get("reason_zh"),
                        news=news, wallet=_wallet_line(db, account_id, "buy", amt))
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
            _adjust_cash(db, account_id, amt)
            recorded.append(f"卖出「{_short_name(name)}」约 {shares:,.0f} 份（¥{amt:,.0f}）")
            if do_email:
                news = fetch_fund.relevant_news(ctx.get("news") or [],
                                                name=name, sector=a.get("sector"))
                _notify(account, "sell", code, name, amt, nav, shares,
                        a.get("rule_label", ""), reason=a.get("reason_zh"),
                        news=news, wallet=_wallet_line(db, account_id, "sell", amt))
    return recorded, skipped


def _adjust_cash(db, account_id, delta):
    """买卖后同步账户现金（P6：仓位管理依赖现金准确）。"""
    row = db.conn.execute(
        "SELECT cash FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    if row is not None:
        db.update_account(account_id, cash=(row["cash"] or 0.0) + delta)


def _accum_shares(db, account_id, code, add):
    """已持有则累加份额（set_position 是覆盖式，需先取旧份额）。"""
    row = db.conn.execute(
        "SELECT shares FROM positions WHERE account_id = ? AND code = ?",
        (account_id, code),
    ).fetchone()
    return (row["shares"] if row else 0.0) + add


def _wallet_line(db, account_id, action, amt):
    """操作后钱包一行（读最新现金；现金已由 _adjust_cash 联动）。"""
    row = db.conn.execute(
        "SELECT cash FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    cash = (row["cash"] if row else 0.0) or 0.0
    flow = "买入扣" if action == "buy" else "卖出加"
    return f"现金 ¥{cash:,.0f}（本次{flow} ¥{amt:,.0f}）"


def _notify(account, action, code, name, amt, nav, shares, note,
            reason=None, news=None, wallet=None):
    """发交易操作报告邮件（强制，每笔买卖必发）。携带操作依据 + 相关要闻 + 操作后钱包。"""
    import subprocess
    cmd = [
        sys.executable, str(SCRIPT_DIR / "send_email.py"), "trade-notify",
        "--action", action, "--code", code, "--name", name,
        "--amount", f"{amt:.2f}", "--nav", f"{nav:.4f}",
        "--shares", f"{shares:.2f}", "--note", note or "",
    ]
    if reason:
        cmd += ["--reason", reason]
    for n in (news or []):
        if n:
            cmd += ["--news", n]
    if wallet:
        cmd += ["--wallet", wallet]
    try:
        subprocess.run(cmd, check=False, timeout=40)
    except Exception:
        pass


def record_daily_snapshot(db, ctx, date):
    """将当日账户快照写入 daily_snapshots 表（幂等，同一天覆盖）。"""
    packet = ctx.get("packet", {})
    account_id = ctx["account_id"]
    total_value = ctx.get("total_value", 0.0)
    cash = ctx.get("cash", 0.0)
    # 排除 pending 基金的市值
    pending_value = sum(
        p["shares"] * (ctx["funds"].get(p["code"], {}).get("current_nav", p["cost_nav"]))
        for p in ctx.get("positions", [])
        if p.get("is_pending")
    )
    adjusted_total_value = total_value - pending_value
    positions_value = adjusted_total_value - cash
    # 计算收益率：只算已确认持仓的「持仓收益率」= 持仓浮盈 ÷ 持仓成本。
    # 现金完全不参与（曾用 adjusted_total_value 含现金做分子，导致现金被当成收益，收益率虚高）。
    confirmed_positions = [p for p in ctx.get("positions", []) if not p.get("is_pending")]
    cost_total = sum(
        p["shares"] * p["cost_nav"] for p in confirmed_positions
    )
    return_pct = ((positions_value - cost_total) / cost_total * 100) if cost_total > 0 else 0.0
    # 回撤：对比历史最高
    row = db.conn.execute(
        "SELECT MAX(total_value) AS peak FROM daily_snapshots WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    peak = (row["peak"] if row and row["peak"] else adjusted_total_value)
    drawdown = ((adjusted_total_value - peak) / peak * 100) if peak > 0 else 0.0
    market_regime = packet.get("market_regime", {}).get("label")
    sector_exposure = {}
    for p in confirmed_positions:
        sector = p.get("sector") or "未分类"
        funds = ctx.get("funds", {})
        f = funds.get(p["code"], {})
        val = p["shares"] * f.get("current_nav", p["cost_nav"])
        sector_exposure[sector] = sector_exposure.get(sector, 0.0) + val
    try:
        db.add_snapshot(account_id, date, adjusted_total_value, cash, positions_value,
                        return_pct, drawdown, market_regime, sector_exposure)
        print(f"[SNAPSHOT] {date} ¥{adjusted_total_value:,.2f} ({return_pct:+.2f}%)")
    except Exception as e:
        print(f"[WARN] 快照写入失败: {e}", file=sys.stderr)


# ---------- 主流程 ----------

def assemble(db, ctx, session, recorded, skipped=None, persist=False):
    md = []
    md += card_top(ctx, session)
    md += card_wallet(ctx)              # 总钱包：持仓+现金/额度/总收益走势
    md += card_spark(ctx)               # 纳指隔夜（QDII 当日方向）
    md += card_action(ctx, session, recorded, skipped)
    md += card_plan(ctx, session, db, persist=persist)  # 今日操作计划：预告→划改→确认
    md += card_position(ctx)
    md += card_holdings(ctx)
    md += card_holding_sparks(ctx)      # 每只持仓近30天净值走势
    md += card_blocks()
    md += card_discover(ctx)            # 跨板块发现的新方向候选（不自动买）
    md += card_news(ctx)                # 财经要闻（免费 7x24 快讯）
    md += card_timeline(db, ctx)        # 近期操作 + 事后复盘点评
    return "\n".join(md)


def main():
    ap = argparse.ArgumentParser(description="三时段卡片日报（确定性，供定时调用）")
    ap.add_argument("--session", choices=list(SESSIONS), required=True)
    ap.add_argument("--account", default="主线")
    ap.add_argument("--date", default=None)
    ap.add_argument("--no-email", action="store_true", help="只生成不发邮件")
    ap.add_argument("--no-record", action="store_true", help="盘尾不自动记账")
    ap.add_argument("--print", action="store_true", help="把卡片 markdown 打到 stdout")
    ap.add_argument("--discover", type=int, default=-1, metavar="N",
                    help="注入 N 只跨板块发现的新候选（默认 -1=盘尾自动6只、其它时段0）")
    ap.add_argument("--html", metavar="PATH", nargs="?", const="__auto__",
                    help="渲染邮件 HTML 到文件供浏览器预览（不发信）；省略路径则写 reports/preview-<session>-<date>.html")
    ap.add_argument("--no-update", action="store_true",
                    help="跳过每日自更新检查")
    args = ap.parse_args()

    date = args.date or datetime.now().strftime("%Y-%m-%d")

    # 每天首次运行：版本文件比对 → 有更新就 git pull 拉最新 skill（幂等，每天一次）。
    # 预览(--html) 不触发；失败绝不阻塞日报。
    if not args.html and not args.no_update:
        try:
            import update_check
            res = update_check.check(apply=True)
            if res.get("checked"):
                print(f"[UPDATE] {res['message']}")
        except Exception as e:
            print(f"[WARN] 更新检查失败: {e}", file=sys.stderr)

    db = Database()
    try:
        # 自动净值校准：昨日按估值记的单笔新买入，待真实收盘净值公布后修正成本/份额。
        # 预览(--html)/不记账(--no-record) 视为只读，不写库。
        if not (args.no_record or args.html):
            calib = fetch_fund.calibrate_costs(args.account, apply=True)
            applied = [c for c in calib if c.get("status") == "applied"]
            if applied:
                print("[OK] 净值校准 " + "；".join(
                    f"{_short_name(c['name'])} {c['old_nav']:.4f}→{c['new_nav']:.4f}"
                    for c in applied))

        # 盘尾默认顺带发现 6 只跨板块新候选（仅展示、不自动买）；其它时段不发现以提速。
        n_disc = args.discover
        if n_disc < 0:
            n_disc = 6 if args.session == "close" else 0
        ctx, err = build_context(db, args.account, date, discover=n_disc)
        if err:
            print(f"[ERROR] {err}", file=sys.stderr)
            return 2

        recorded, skipped = [], []
        if args.session == "close" and not args.no_record:
            # P7: 先记账今日到期的定投（写交易+累加持仓+扣现金+通知）
            import auto_invest
            dca_done = auto_invest.record_due_plans(
                db, ctx["account_id"], args.account, datetime.now().date(),
                ctx["funds"], do_email=not args.no_email)
            recorded, skipped = auto_record(db, ctx, args.account, do_email=not args.no_email)
            recorded = dca_done + recorded

        # 真跑（非 --html 预览）才把今日操作计划落库，作为后续时段的对比基线
        md = assemble(db, ctx, args.session, recorded, skipped,
                      persist=not args.html)

        # 记录每日快照（每次运行都写，盘尾最完整）
        record_daily_snapshot(db, ctx, date)
        if args.print:
            print(md)

        # HTML 预览：渲染到文件供浏览器打开（不发信）
        if args.html:
            html = send_email.markdown_to_html(md)
            if args.html == "__auto__":
                out_dir = SCRIPT_DIR.parent / "reports"
                out_dir.mkdir(exist_ok=True)
                out_path = out_dir / f"preview-{args.session}-{date}.html"
            else:
                out_path = Path(args.html)
            out_path.write_text(html, encoding="utf-8")
            print(f"[OK] 邮件 HTML 预览已写入: {out_path}")
            print(f"     用浏览器打开:  open {out_path}")
            return 0

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
