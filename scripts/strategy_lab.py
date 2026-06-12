#!/usr/bin/env python3
"""梦境实验室 — 同一历史窗口跑多个策略变体，对比择优，驱动策略进化。

闭环：make_variants() 定义候选策略（基线 + 趋势规则变体）
   → run 子命令同窗口逐一回测（数据只拉一次，注入复用）
   → compute_metrics / rank_results 评分排名
   → --evolve 把冠军写入 strategy_evolutions
   → --promote vX.Y 把冠军规则注册为新决策树版本（之后 decide.py --strategy 可用）

纯 stdlib。趋势规则依据见 tests/test_trend_rules.py 文档串。
"""

import argparse
import contextlib
import copy
import io
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR.parent / "data"
sys.path.insert(0, str(SCRIPT_DIR))


# ---------- 纯函数：指标 / 变体 / 排名 ----------

def compute_metrics(daily_records, budget, trades):
    """从每日快照 + 交易列表算回测指标。"""
    values = [r["total_value"] for r in daily_records]
    if not values or not budget:
        return {"total_return_pct": 0.0, "annual_return_pct": 0.0,
                "max_drawdown_pct": 0.0, "sharpe": 0.0,
                "win_rate": 0.0, "num_trades": 0, "final_value": budget}
    final = values[-1]
    total_return = (final - budget) / budget * 100
    n = len(values)
    annual = total_return * (252 / n) if n else 0.0

    peak, max_dd = values[0], 0.0
    for v in values:
        if v > peak:
            peak = v
        dd = (v - peak) / peak * 100
        if dd < max_dd:
            max_dd = dd

    rets = [(values[i] - values[i - 1]) / values[i - 1]
            for i in range(1, n) if values[i - 1]]
    sharpe = 0.0
    if len(rets) >= 2:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        std = var ** 0.5
        if std > 0:
            sharpe = mean / std * (252 ** 0.5)

    sells = [t for t in trades if t.get("action") == "sell"]
    wins = [t for t in sells if t.get("profit_pct", 0) > 0]
    win_rate = len(wins) / len(sells) if sells else 0.0

    return {
        "total_return_pct": round(total_return, 4),
        "annual_return_pct": round(annual, 4),
        "max_drawdown_pct": round(max_dd, 4),
        "sharpe": round(sharpe, 4),
        "win_rate": round(win_rate, 4),
        "num_trades": len(trades),
        "final_value": round(final, 2),
    }


def _load_base_rules():
    """v2.0 基线规则：live 文件可能已是 v2.1+（含 P5 键），剥离后作为对照组起点，
    各变体在此之上显式叠加自己的 patch，保证对照实验干净。"""
    with open(DATA_DIR / "decision_tree.json", "r", encoding="utf-8") as f:
        rules = json.load(f).get("rules", {})
    for k in ("take_profit_policy", "trend_exit", "trend_filter"):
        rules.pop(k, None)
    return rules


def make_variants(base_rules=None):
    """候选策略变体。每个 {name, desc, rules}，rules 互为独立深拷贝。"""
    base = base_rules or _load_base_rules()

    def _v(name, desc, **patch):
        rules = copy.deepcopy(base)
        rules.update(patch)
        return {"name": name, "desc": desc, "rules": rules}

    trend_exit = {"enabled": True, "confirm_days": 2, "sell_fraction": 0.5}
    trend_filter = {"enabled": True, "low_buy_factor": 0.5}
    tp_off = {"mode": "off"}

    # P6: v2.1 线上形态（关止盈 + 趋势退出/闸门）作为新基线
    v21 = {"take_profit_policy": dict(tp_off),
           "trend_exit": dict(trend_exit), "trend_filter": dict(trend_filter)}
    pm = {"enabled": True,
          "target_floor": {"牛市": 0.70, "震荡市": 0.50, "熊市": 0.30},
          "tolerance": 0.05, "batch_fraction": 0.10,
          "max_funds_per_batch": 2, "min_order_amount": 300}
    sig_buys = {"rsi_buy": {"enabled": True, "threshold": 32, "amount_ratio": 0.03},
                "breakout_buy": {"enabled": True, "amount_ratio": 0.03}}
    sig_trim = {"rsi_trim": {"enabled": True, "threshold": 82,
                             "min_profit": 0.15, "sell_fraction": 0.20}}
    return [
        _v("baseline-v2.0", "现行 v2.0 规则（对照组）"),
        _v("trend-exit", "v2.0 + 200日线破位减仓（Faber 趋势退出）",
           trend_exit=dict(trend_exit)),
        _v("trend-gate", "v2.0 + HS300 破200日线时低吸减半（别接飞刀）",
           trend_filter=dict(trend_filter)),
        _v("trend-full", "v2.0 + 趋势退出 + 低吸闸门",
           trend_exit=dict(trend_exit), trend_filter=dict(trend_filter)),
        _v("trend-full-confirm3", "趋势全开但确认期 3 天（参数稳健性检查）",
           trend_exit={**trend_exit, "confirm_days": 3},
           trend_filter=dict(trend_filter)),
        _v("let-winners-run", "关闭分层止盈 + 趋势退出兜底（线上 LET_WINNERS_RUN 形态）",
           take_profit_policy=dict(tp_off),
           trend_exit=dict(trend_exit), trend_filter=dict(trend_filter)),
        _v("let-winners-run-raw", "只关止盈、无趋势兜底（验证趋势退出的增量价值）",
           take_profit_policy=dict(tp_off)),
        # ---- P6 变体（基线 = v2.1 线上形态）----
        _v("baseline-v2.1", "现行 v2.1 规则（P6 对照组）", **copy.deepcopy(v21)),
        _v("v21-position-mgmt", "v2.1 + 总仓位管理（分批建仓/超配回撤）",
           **copy.deepcopy(v21), position_management=copy.deepcopy(pm)),
        _v("v21-signal-buys", "v2.1 + RSI超卖低吸 + 20日突破顺势买",
           **copy.deepcopy(v21), signal_rules=copy.deepcopy(sig_buys)),
        _v("v21-full-arsenal", "v2.1 + 仓位管理 + 信号买入（P6 主推组合）",
           **copy.deepcopy(v21), position_management=copy.deepcopy(pm),
           signal_rules=copy.deepcopy(sig_buys)),
        _v("v21-full-plus-trim", "P6 主推 + RSI超买减仓（软利润保护）",
           **copy.deepcopy(v21), position_management=copy.deepcopy(pm),
           signal_rules={**copy.deepcopy(sig_buys), **copy.deepcopy(sig_trim)}),
        # ---- P6.1 强趋势闸门（200日线下完全停火，证据：窗口C 下跌年）----
        _v("v21-pm-gated", "仓位管理 + 200日线下不建仓",
           **copy.deepcopy(v21),
           position_management={**copy.deepcopy(pm), "require_trend_above": True}),
        _v("v21-arsenal-gated", "仓位管理 + 信号买入，均 200日线下停火",
           **copy.deepcopy(v21),
           position_management={**copy.deepcopy(pm), "require_trend_above": True},
           signal_rules={**copy.deepcopy(sig_buys), "require_trend_above": True}),
    ]


def rank_results(results):
    """按 score = 年化 + 0.5*最大回撤（回撤为负即惩罚）降序。"""
    out = []
    for r in results:
        m = r["metrics"]
        score = m.get("annual_return_pct", 0.0) + 0.5 * m.get("max_drawdown_pct", 0.0)
        out.append({**r, "score": round(score, 4)})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


# ---------- 回测驱动 ----------

def _preload_data(fund_pool, start, end, lookback_days=450):
    """拉一次数据，所有变体共享。指数多回看 450 天以便算 200 日线（无未来函数）。"""
    from simulate import (fetch_nav_history, fetch_index_history,
                          BENCHMARKS, QDII_REF_INDEX)
    ext_start = (datetime.strptime(start, "%Y-%m-%d")
                 - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    nav_start = (datetime.strptime(start, "%Y-%m-%d")
                 - timedelta(days=90)).strftime("%Y-%m-%d")
    print(f"📡 预加载数据：基金 {len(fund_pool)} 只 (净值自 {nav_start} 预热信号, "
          f"窗口 {start}~{end})，指数自 {ext_start}（200日线需要）")
    fund_navs = {}
    for code, name in fund_pool.items():
        navs = fetch_nav_history(code, nav_start, end)
        fund_navs[code] = navs
        print(f"  {code} {name}: {len(navs)} 条")
    index_data = {}
    secids = dict(BENCHMARKS)
    if any(c in QDII_REF_INDEX for c in fund_pool):
        secids["100.NDX"] = "纳斯达克100"
    import time as _time
    for secid, name in secids.items():
        data = {}
        for attempt in range(7):  # CDN 偶发掐连接，指数数据是趋势规则的命根子
            data = fetch_index_history(secid, ext_start, end)
            if data:
                break
            _time.sleep(3.0 * (attempt + 1))
        index_data[secid] = data
        print(f"  {name}({secid}): {len(data)} 条")
    if not index_data.get("1.000300"):
        raise RuntimeError(
            "沪深300 指数数据拉取失败 — 大盘环境/趋势规则都依赖它，"
            "此时回测结果无意义，请稍后重试")
    return fund_navs, index_data


def _run_variant(variant, start, end, budget, fund_pool, fund_navs, index_data, db):
    from simulate import Simulator
    sim_id = f"lab-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{variant['name']}"
    sim = Simulator(
        start, end, budget, funds=dict(fund_pool), sim_id=sim_id,
        verbose=False, db=db, strategy_version=variant["name"],
        engine_mode=True, rules_override=variant["rules"],
    )
    sim.fund_navs = copy.deepcopy(fund_navs)
    sim.index_data = index_data  # 只读，可共享
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sim.run()
    metrics = compute_metrics(sim.daily_records, budget, sim.trades)
    return {"name": variant["name"], "desc": variant["desc"],
            "sim_id": sim_id, "metrics": metrics, "rules": variant["rules"]}


def _benchmark_hold(fund_navs, budget, start, end):
    """基准：首日等权买入全部基金并持有到底。"""
    series = {}
    for code, navs in fund_navs.items():
        dates = sorted(d for d in navs if start <= d <= end)
        if len(dates) >= 2:
            series[code] = [navs[d] for d in dates]
    if not series:
        return None
    n_days = min(len(v) for v in series.values())
    per = budget / len(series)
    daily = []
    for i in range(n_days):
        total = sum(per * (v[i] / v[0]) for v in series.values())
        daily.append({"date": str(i), "total_value": total})
    return compute_metrics(daily, budget, [])


def cmd_run(args):
    from db import Database
    fund_pool = None
    if args.funds:
        from simulate import DEFAULT_FUNDS
        fund_pool = {}
        for code in args.funds.split(","):
            code = code.strip()
            fund_pool[code] = DEFAULT_FUNDS.get(code, code)
    else:
        from simulate import DEFAULT_FUNDS
        fund_pool = dict(DEFAULT_FUNDS)

    variants = make_variants()
    if args.variants:
        wanted = {v.strip() for v in args.variants.split(",")}
        variants = [v for v in variants if v["name"] in wanted]
        if not variants:
            print(f"[ERROR] 没有匹配的变体: {args.variants}")
            return 2

    fund_navs, index_data = _preload_data(fund_pool, args.start, args.end)
    if not any(fund_navs.values()):
        print("[ERROR] 基金净值数据加载失败")
        return 2

    db = Database()
    results = []
    try:
        for i, v in enumerate(variants, 1):
            print(f"\n🧪 [{i}/{len(variants)}] {v['name']} — {v['desc']}")
            r = _run_variant(v, args.start, args.end, args.budget,
                             fund_pool, fund_navs, index_data, db)
            m = r["metrics"]
            print(f"   收益 {m['total_return_pct']:+.2f}% | 年化 {m['annual_return_pct']:+.2f}% | "
                  f"回撤 {m['max_drawdown_pct']:.2f}% | Sharpe {m['sharpe']:.2f} | "
                  f"交易 {m['num_trades']} 笔 | 胜率 {m['win_rate']*100:.0f}%")
            results.append(r)

        ranked = rank_results(results)
        bench = _benchmark_hold(fund_navs, args.budget, args.start, args.end)

        print("\n" + "=" * 78)
        print(f"🏁 梦境实验室排名（{args.start} ~ {args.end}, 预算 ¥{args.budget:,.0f}）")
        print("-" * 78)
        print(f"{'排名':<4}{'变体':<22}{'总收益':>9}{'年化':>9}{'回撤':>9}{'Sharpe':>8}{'得分':>8}")
        for i, r in enumerate(ranked, 1):
            m = r["metrics"]
            print(f"{i:<4}{r['name']:<22}{m['total_return_pct']:>8.2f}%"
                  f"{m['annual_return_pct']:>8.2f}%{m['max_drawdown_pct']:>8.2f}%"
                  f"{m['sharpe']:>8.2f}{r['score']:>8.2f}")
        if bench:
            print("-" * 78)
            print(f"{'':4}{'买入持有基准':<22}{bench['total_return_pct']:>8.2f}%"
                  f"{bench['annual_return_pct']:>8.2f}%{bench['max_drawdown_pct']:>8.2f}%"
                  f"{bench['sharpe']:>8.2f}{'—':>8}")
        print("=" * 78)

        champion = ranked[0]
        baseline = next((r for r in ranked if r["name"] == "baseline-v2.0"), None)

        try:
            with open(DATA_DIR / "decision_tree.json", "r", encoding="utf-8") as f:
                live_version = json.load(f).get("version", "v2.0")
        except Exception:
            live_version = "v2.0"

        if args.evolve and baseline and champion["name"] != "baseline-v2.0":
            db.add_evolution(
                from_version=live_version, to_version=champion["name"],
                title=f"梦境实验室冠军: {champion['name']}",
                description=champion["desc"],
                trigger_source="strategy_lab",
                trigger_detail=f"{args.start}~{args.end} 同窗口对比 {len(ranked)} 个变体",
                before_metrics=baseline["metrics"],
                after_metrics=champion["metrics"],
                lessons_learned=f"score {champion['score']} vs baseline "
                                f"{next(r['score'] for r in ranked if r['name'] == 'baseline-v2.0')}",
            )

        if args.promote:
            chosen = champion
            if args.promote_variant:
                chosen = next((r for r in ranked if r["name"] == args.promote_variant), None)
                if not chosen:
                    print(f"[ERROR] 变体不存在: {args.promote_variant}")
                    return 2
            changelog = f"采纳梦境实验室变体 {chosen['name']}: {chosen['desc']}"
            reason = (f"回测 {args.start}~{args.end} 得分 {chosen['score']}"
                      f"（年化 {chosen['metrics']['annual_return_pct']:+.2f}%, "
                      f"回撤 {chosen['metrics']['max_drawdown_pct']:.2f}%）")
            db.add_tree_version(
                version=args.promote, parent_version=live_version,
                changelog=changelog, reason=reason,
                rules_json=chosen["rules"],
                evidence=json.dumps({"ranking": [
                    {"name": r["name"], "score": r["score"], **r["metrics"]}
                    for r in ranked]}, ensure_ascii=False),
                backtest_results=chosen["sim_id"],
            )
            # 同步 live ruleset 文件（引擎默认版本跟随该文件）
            tree_file = DATA_DIR / "decision_tree.json"
            tree = {
                "version": args.promote,
                "parent": live_version,
                "changelog": changelog,
                "reason": reason,
                "created_at": datetime.now().strftime("%Y-%m-%d"),
                "created_by": "strategy_lab",
                "rules": chosen["rules"],
            }
            with open(tree_file, "w", encoding="utf-8") as f:
                json.dump(tree, f, ensure_ascii=False, indent=2)
            print(f"\n🏆 {chosen['name']} 已注册为决策树 {args.promote} 并写入 {tree_file.name}")
        return 0
    finally:
        db.close()


def cmd_variants(args):
    for v in make_variants():
        extra = []
        if "trend_exit" in v["rules"]:
            extra.append(f"trend_exit={v['rules']['trend_exit']}")
        if "trend_filter" in v["rules"]:
            extra.append(f"trend_filter={v['rules']['trend_filter']}")
        print(f"- {v['name']}: {v['desc']}")
        for e in extra:
            print(f"    {e}")


def main():
    ap = argparse.ArgumentParser(description="梦境实验室 — 多策略变体回测对比")
    sub = ap.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="同窗口跑全部变体并排名")
    p_run.add_argument("--start", required=True)
    p_run.add_argument("--end", required=True)
    p_run.add_argument("--budget", type=float, default=20000)
    p_run.add_argument("--funds", help="基金代码逗号分隔（默认 DEFAULT_FUNDS 池）")
    p_run.add_argument("--variants", help="只跑指定变体（逗号分隔名字）")
    p_run.add_argument("--evolve", action="store_true",
                       help="冠军≠基线时写 strategy_evolutions")
    p_run.add_argument("--promote", metavar="vX.Y",
                       help="把冠军规则注册为新决策树版本并写入 decision_tree.json")
    p_run.add_argument("--promote-variant", metavar="NAME",
                       help="指定晋升的变体名（默认晋升得分冠军）")

    sub.add_parser("variants", help="列出内置变体")

    args = ap.parse_args()
    if args.command == "run":
        sys.exit(cmd_run(args) or 0)
    elif args.command == "variants":
        cmd_variants(args)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
