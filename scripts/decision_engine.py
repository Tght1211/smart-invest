#!/usr/bin/env python3
"""
决策引擎 — Smart Invest Skill
执行决策树规则，记录审计日志，支持策略进化分析。
纯 Python 3 标准库。
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import Database


class DecisionEngine:
    """决策引擎：执行规则 + 记录审计日志"""

    def __init__(self, db, account_id, strategy_version=None, rules_override=None):
        self.db = db
        self.account_id = account_id
        self.strategy_version = strategy_version or "v2.0"
        if rules_override is not None:
            self.rules = rules_override
        else:
            self.rules = self._load_rules()

    def _load_rules(self):
        """加载决策树规则"""
        tree = self.db.get_tree_version(self.strategy_version)
        if tree and "rules" in tree:
            return tree["rules"]

        # 如果 DB 中没有，从 JSON 文件加载
        json_file = DATA_DIR / "decision_tree.json"
        if json_file.exists():
            with open(json_file, "r", encoding="utf-8") as f:
                tree = json.load(f)
                return tree.get("rules", {})

        return {}

    def get_fund_sector(self, code, name):
        """判断基金所属赛道"""
        keywords = self.rules.get("sector_keywords", {})
        for sector, kws in keywords.items():
            for kw in kws:
                if kw in name or kw in code:
                    return sector
        return "其他"

    # ==================== 新统一入口（Phase 1）====================

    def decide(self, date, market_data, positions, cash, total_value):
        """Single entry point: produce a structured decision packet.

        See docs/superpowers/specs/2026-05-28-smart-invest-overhaul-design.md §6.
        """
        from datetime import datetime
        row = self.db.conn.execute(
            "SELECT name FROM accounts WHERE id = ?", (self.account_id,)
        ).fetchone()
        account_name = row["name"] if row else "unknown"

        regime = self._compute_market_regime(market_data)
        snapshot = self._compute_portfolio_snapshot(
            positions, market_data, cash, total_value,
        )
        actions, blocked, alerts = self._evaluate_rules(
            date, market_data, positions, snapshot, regime, cash, total_value,
        )

        return {
            "schema_version": "1.0",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "account": account_name,
            "date": date,
            "rule_version": self.strategy_version,
            "market_regime": regime,
            "portfolio_snapshot": snapshot,
            "actions": actions,
            "blocked_actions": blocked,
            "alerts": alerts,
            "summary": self._build_summary(actions),
        }

    def _compute_market_regime(self, market_data):
        """Classify market regime by HS300 20-day return.

        - 牛市 : 20d > +5%   → position_cap 95%, single_cap 30%, stop -15%
        - 熊市 : 20d < -10%  → position_cap 60%, single_cap 15%, stop -8%
        - 震荡市: otherwise    → position_cap 85%, single_cap 25%, stop -12%
        - unknown: 20d is None → conservative posture, blocks new buys
        """
        hs300_20d_raw = market_data.get("hs300_20d_return")
        hs300_5d = market_data.get("hs300_5d_return") or 0.0
        if hs300_20d_raw is None:
            label, pcap, scap, sl = "unknown", 0.85, 0.25, -0.12
            hs300_20d = 0.0
        else:
            hs300_20d = hs300_20d_raw
            if hs300_20d > 0.05:
                label, pcap, scap, sl = "牛市", 0.95, 0.30, -0.15
            elif hs300_20d < -0.10:
                label, pcap, scap, sl = "熊市", 0.60, 0.15, -0.08
            else:
                label, pcap, scap, sl = "震荡市", 0.85, 0.25, -0.12
        return {
            "label": label,
            "hs300_5d_return": hs300_5d,
            "hs300_20d_return": hs300_20d,
            "position_cap": pcap,
            "single_cap": scap,
            "stop_loss_threshold": sl,
        }

    def _compute_portfolio_snapshot(self, positions, market_data, cash, total_value):
        """Aggregate positions: total/cash/position value, sector pct, per-position."""
        funds = market_data.get("funds", {})
        by_pos = []
        sectors = {}
        position_value = 0.0
        for pos in positions:
            code = pos["code"]
            fund = funds.get(code) or {}
            nav = fund.get("current_nav", pos["cost_nav"])
            value = pos["shares"] * nav
            position_value += value
            sector = pos.get("sector") or fund.get("sector") or "其他"
            sectors[sector] = sectors.get(sector, 0.0) + value
            profit_pct = (
                (nav - pos["cost_nav"]) / pos["cost_nav"]
                if pos["cost_nav"] else 0.0
            )
            by_pos.append({
                "code": code,
                "name": pos.get("name", fund.get("name", "")),
                "shares": pos["shares"],
                "cost_nav": pos["cost_nav"],
                "current_nav": nav,
                "value": value,
                "pct_of_total": value / total_value if total_value else 0.0,
                "profit_pct": profit_pct,
                "hold_days": pos.get("hold_days", 0),
                "sector": sector,
            })
        sectors_pct = {
            k: (v / total_value if total_value else 0.0)
            for k, v in sectors.items()
        }
        return {
            "total_value": total_value,
            "cash": cash,
            "cash_pct": cash / total_value if total_value else 0.0,
            "position_value": position_value,
            "position_pct": (
                position_value / total_value if total_value else 0.0
            ),
            "sectors": sectors_pct,
            "by_position": by_pos,
        }

    # ---------- precondition checks ----------

    SECTOR_CAP_MAP = {
        "科技": 0.50, "消费": 0.30, "新能源": 0.30, "金融": 0.20,
        "资源": 0.20, "宽基": 0.30, "海外": 0.40, "其他": 0.30,
    }

    def _check_cash_reserve(self, cash_pct):
        return cash_pct >= 0.10, {
            "id": "cash_reserve", "actual": cash_pct, "threshold_min": 0.10,
        }

    def _check_single_position(self, code, snapshot, target_amount, total_value):
        existing = next(
            (p for p in snapshot["by_position"] if p["code"] == code), None,
        )
        existing_pct = existing["pct_of_total"] if existing else 0.0
        projected = existing_pct + (
            target_amount / total_value if total_value else 0.0
        )
        return projected <= 0.25, {
            "id": "single_position",
            "actual": existing_pct,
            "projected": projected,
            "threshold_max": 0.25,
        }

    def _check_sector_concentration(self, sector, snapshot, target_amount, total_value):
        if not sector or sector == "其他":
            return True, {"id": "sector_concentration", "sector": sector, "skipped": True}
        cap = self.SECTOR_CAP_MAP.get(sector, 0.30)
        current = snapshot["sectors"].get(sector, 0.0)
        projected = current + (
            target_amount / total_value if total_value else 0.0
        )
        return projected <= cap, {
            "id": "sector_concentration",
            "sector": sector,
            "actual": current,
            "projected": projected,
            "threshold_max": cap,
        }

    def _check_anti_chase(self, fund):
        r5 = fund.get("fund_5d_return", 0.0)
        return r5 <= 0.10, {
            "id": "anti_chase", "actual": r5, "threshold_max": 0.10,
        }

    def _check_market_allows_buy(self, regime, has_existing_position):
        label = regime["label"]
        if label == "熊市" and not has_existing_position:
            return False, {"id": "bear_market_new_position", "label": label}
        if label == "unknown":
            return False, {"id": "market_regime_unknown", "label": label}
        return True, {"id": "market_regime", "label": label}

    # ---------- low_buy rule ----------

    def _try_low_buy(self, code, fund, snapshot, regime, cash, total_value):
        """Return (action_dict, blocked_dict) — exactly one is None, or both None."""
        day_r = fund.get("day_return", 0.0)
        if day_r > -0.03:
            return None, None  # not a low-buy candidate

        base_amount = total_value * 0.03
        boost = 1.0
        if day_r <= -0.05 or fund.get("fund_5d_return", 0.0) <= -0.08:
            boost = 2.0
        if (regime.get("hs300_5d_return") or 0.0) <= -0.02:
            boost = 2.0
        target_amount = base_amount * boost

        existing = next(
            (p for p in snapshot["by_position"] if p["code"] == code), None,
        )
        if regime["label"] == "熊市" and existing:
            target_amount *= 0.5

        sector = fund.get("sector") or "其他"
        checks_passed, checks_failed = [], []
        for ok, info in [
            self._check_cash_reserve(snapshot["cash_pct"]),
            self._check_single_position(code, snapshot, target_amount, total_value),
            self._check_sector_concentration(sector, snapshot, target_amount, total_value),
            self._check_anti_chase(fund),
            self._check_market_allows_buy(regime, has_existing_position=existing is not None),
        ]:
            (checks_passed if ok else checks_failed).append(info)

        context = {
            "fund_5d_return": fund.get("fund_5d_return", 0.0),
            "fund_day_return": day_r,
            "hs300_5d_return": regime.get("hs300_5d_return", 0.0),
        }
        # Phase 3: 把信号附加到 context（观测用，不影响决策）
        if fund.get("signals"):
            context["signals"] = fund["signals"]
        if checks_failed:
            primary = checks_failed[0]
            return None, {
                "code": code,
                "name": fund.get("name", ""),
                "attempted_action": "buy",
                "blocked_by": primary["id"],
                "reason_zh": self._block_reason_zh(primary, context),
            }

        return {
            "code": code,
            "name": fund.get("name", ""),
            "action": "buy",
            "rule_id": "low_buy",
            "rule_label": "低吸",
            "confidence": None,  # filled in Task 10
            "suggested_amount": round(target_amount, 2),
            "suggested_shares": None,
            "context": context,
            "checks_passed": checks_passed,
            "checks_failed": [],
            "reason_zh": (
                f"符合低吸规则：当日跌 {abs(day_r) * 100:.1f}%、"
                f"近 5 天跌 {abs(fund.get('fund_5d_return', 0.0)) * 100:.1f}%；"
                f"大盘 {regime['label']}；现金 "
                f"{snapshot['cash_pct'] * 100:.0f}% 在阈值内。"
            ),
        }, None

    def _block_reason_zh(self, info, context):
        m = {
            "cash_reserve": (
                f"现金占比 {info.get('actual', 0) * 100:.1f}% < 10% 最低储备线。"
            ),
            "single_position": (
                f"单只仓位将达 {info.get('projected', 0) * 100:.1f}% > 25% 上限。"
            ),
            "sector_concentration": (
                f"{info.get('sector', '')}赛道将达 "
                f"{info.get('projected', 0) * 100:.1f}% > "
                f"{info.get('threshold_max', 0) * 100:.0f}% 上限。"
            ),
            "anti_chase": (
                f"该基金近 5 天涨 {info.get('actual', 0) * 100:.1f}% > 10%，"
                f"禁止追高。"
            ),
            "bear_market_new_position": "大盘处于熊市，禁止新建仓。",
            "market_regime_unknown": "大盘数据缺失，谨慎起见暂不建仓。",
        }
        return m.get(info["id"], f"未通过检查：{info['id']}")

    # ---------- stop-loss rules ----------

    def _try_stop_loss(self, code, fund, position, regime):
        """Highest-priority sell. Returns action dict or None."""
        nav = fund.get("current_nav", position["cost_nav"])
        profit_pct = (
            (nav - position["cost_nav"]) / position["cost_nav"]
            if position["cost_nav"] else 0.0
        )
        day_r = fund.get("day_return", 0.0)
        three_d = fund.get("fund_3d_return", 0.0)
        hold_days = position.get("hold_days", 0)

        def _sell(rule_id, label, fraction, reason):
            ctx = {
                "profit_pct": profit_pct,
                "day_return": day_r,
                "hold_days": hold_days,
            }
            if fund.get("signals"):
                ctx["signals"] = fund["signals"]
            return {
                "code": code,
                "name": position.get("name") or fund.get("name", ""),
                "action": "sell",
                "rule_id": rule_id,
                "rule_label": label,
                "confidence": None,  # Task 10
                "suggested_amount": round(position["shares"] * fraction * nav, 2),
                "suggested_shares": round(position["shares"] * fraction, 4),
                "context": ctx,
                "checks_passed": [],
                "checks_failed": [],
                "reason_zh": reason,
            }

        # Priority 1: emergency
        if day_r <= -0.07:
            return _sell(
                "emergency_stop_loss", "紧急止损", 0.5,
                f"单日跌 {abs(day_r) * 100:.1f}% > 7%，立即减仓 50%。",
            )
        if three_d <= -0.10:
            return _sell(
                "emergency_stop_loss", "紧急止损", 0.5,
                f"近 3 天累跌 {abs(three_d) * 100:.1f}% > 10%，立即减仓 50%。",
            )
        # Priority 2: absolute (hard -20%)
        if profit_pct <= -0.20:
            return _sell(
                "absolute_stop_loss", "绝对止损", 1.0,
                f"亏损 {abs(profit_pct) * 100:.1f}% > 20%，清仓。",
            )
        # Priority 3: time-based
        if hold_days < 30 and profit_pct <= -0.08:
            return _sell(
                "time_based_stop_loss", "短期止损", 0.5,
                f"持有 {hold_days} 天亏 {abs(profit_pct) * 100:.1f}% > 8%，"
                f"减仓 50%。",
            )
        if 30 <= hold_days <= 90 and profit_pct <= -0.12:
            return _sell(
                "time_based_stop_loss", "中期止损", 0.5,
                f"持有 {hold_days} 天亏 {abs(profit_pct) * 100:.1f}% > 12%，"
                f"减仓 50%。",
            )
        if hold_days > 90 and profit_pct <= -0.15:
            return _sell(
                "time_based_stop_loss", "长期止损", 0.5,
                f"持有 {hold_days} 天亏 {abs(profit_pct) * 100:.1f}% > 15%，"
                f"减仓 50%。",
            )
        return None

    # ---------- take-profit tiers ----------

    def _try_take_profit(self, code, fund, position):
        nav = fund.get("current_nav", position["cost_nav"])
        profit_pct = (
            (nav - position["cost_nav"]) / position["cost_nav"]
            if position["cost_nav"] else 0.0
        )
        if profit_pct < 0.20:
            return None

        def _sell(rule_id, label, fraction, reason):
            ctx = {"profit_pct": profit_pct}
            if fund.get("signals"):
                ctx["signals"] = fund["signals"]
            return {
                "code": code,
                "name": position.get("name") or fund.get("name", ""),
                "action": "sell",
                "rule_id": rule_id,
                "rule_label": label,
                "confidence": None,
                "suggested_amount": round(position["shares"] * fraction * nav, 2),
                "suggested_shares": round(position["shares"] * fraction, 4),
                "context": ctx,
                "checks_passed": [],
                "checks_failed": [],
                "reason_zh": reason,
            }

        # Highest tier first
        if profit_pct >= 0.50:
            return _sell(
                "take_profit_clearout", "止盈清仓", 1.0,
                f"盈利 {profit_pct * 100:.1f}% ≥ 50%，清仓锁利。",
            )
        if profit_pct >= 0.40:
            return _sell(
                "take_profit_tier_40", "止盈第三档", 0.25,
                f"盈利 {profit_pct * 100:.1f}% ≥ 40%，再减 25%。",
            )
        if profit_pct >= 0.30:
            return _sell(
                "take_profit_tier_30", "止盈第二档", 0.25,
                f"盈利 {profit_pct * 100:.1f}% ≥ 30%，再减 25%。",
            )
        return _sell(
            "take_profit_tier_20", "止盈首档", 0.25,
            f"盈利 {profit_pct * 100:.1f}% ≥ 20%，减仓 25%。",
        )

    # ---------- confidence scoring ----------

    def _score_confidence(self, action, position=None):
        """Heuristic confidence in [0, 1]. Phase 2 will calibrate from backtest data."""
        if action["action"] == "buy":
            base = 0.5
            ctx = action.get("context", {})
            if ctx.get("fund_5d_return", 0.0) <= -0.05:
                base += 0.15
            if ctx.get("hs300_5d_return", 0.0) >= 0.03:
                base += 0.10
            if position is None:
                base += 0.10  # new position bonus
                base += 0.05  # light cash deploy bonus
            return max(0.0, min(1.0, round(base, 2)))
        if action["action"] == "sell":
            base = 0.6
            profit_pct = action.get("context", {}).get("profit_pct")
            if profit_pct is not None:
                if profit_pct >= 0.40:
                    base += 0.20
                if profit_pct <= -0.15:
                    base += 0.20
            return max(0.0, min(1.0, round(base, 2)))
        return None

    # ---------- main evaluator ----------

    def _evaluate_rules(self, date, market_data, positions, snapshot,
                        regime, cash, total_value):
        actions, blocked, alerts = [], [], []
        funds = market_data.get("funds", {})

        # Account-level drawdown protection
        peak = market_data.get("portfolio_peak_value")
        drawdown = (
            (peak - total_value) / peak
            if peak and peak > total_value else 0.0
        )
        in_drawdown_protection = drawdown >= 0.10
        if in_drawdown_protection:
            alerts.append({
                "severity": "warn",
                "id": "drawdown_protection",
                "drawdown": round(drawdown, 4),
                "reason_zh": (
                    f"组合从峰值回撤 {drawdown * 100:.1f}% ≥ 10%，"
                    f"所有买入降级为观察。"
                ),
            })

        # Pass 1: sells on existing positions (stop-loss > take-profit, sell wins over buy)
        positions_with_sell = set()
        for pos in positions:
            code = pos["code"]
            fund = funds.get(code)
            if not fund:
                alerts.append({
                    "severity": "warn",
                    "id": "data_missing",
                    "code": code,
                    "reason_zh": (
                        f"无法获取基金 {code}（{pos.get('name', '')}）的"
                        f"实时数据，已跳过决策。"
                    ),
                })
                continue
            sell = self._try_stop_loss(code, fund, pos, regime)
            if not sell:
                sell = self._try_take_profit(code, fund, pos)
            if sell:
                sell["confidence"] = self._score_confidence(sell, position=pos)
                actions.append(sell)
                positions_with_sell.add(code)

        # Pass 2: low_buy on each candidate fund (skip if sell already triggered)
        for code, fund in funds.items():
            if code in positions_with_sell:
                continue
            action, block = self._try_low_buy(
                code, fund, snapshot, regime, cash, total_value,
            )
            if action:
                existing = next(
                    (p for p in positions if p["code"] == code), None,
                )
                action["confidence"] = self._score_confidence(action, position=existing)
                if in_drawdown_protection:
                    actions.append({
                        **action,
                        "action": "watch",
                        "rule_id": "low_buy_deferred_drawdown",
                        "rule_label": "低吸暂缓（回撤保护）",
                        "suggested_amount": 0.0,
                        "confidence": None,
                        "reason_zh": (
                            action["reason_zh"] + " 但组合回撤 ≥ 10%，暂缓买入。"
                        ),
                    })
                else:
                    actions.append(action)
            if block:
                blocked.append(block)

        return actions, blocked, alerts

    def _build_summary(self, actions):
        counts = {"buy": 0, "sell": 0, "hold": 0, "watch": 0}
        for a in actions:
            counts[a["action"]] = counts.get(a["action"], 0) + 1
        highest = None
        for a in actions:
            conf = a.get("confidence")
            if conf is None:
                continue
            if highest is None or conf > highest["confidence"]:
                highest = {
                    "code": a["code"], "action": a["action"], "confidence": conf,
                }
        return {
            "action_count": counts,
            "highest_confidence_action": highest,
        }

    # ==================== Phase 2: 规则统计 ====================

    def compute_rule_stats(self, start_date=None, end_date=None):
        """Aggregate trades by rule_name (which we treat as rule_id).

        Returns list of dicts sorted by expectancy desc:
          {rule_id, count, wins, losses,
           win_rate, avg_profit_pct_wins, avg_profit_pct_losses,
           avg_profit_pct, expectancy}

        Only includes trades with non-null profit_pct (i.e., closed positions).
        """
        clauses = [
            "account_id = ?",
            "profit_pct IS NOT NULL",
            "rule_name IS NOT NULL",
            "rule_name != ''",
        ]
        params = [self.account_id]
        if start_date:
            clauses.append("date >= ?")
            params.append(start_date)
        if end_date:
            clauses.append("date <= ?")
            params.append(end_date)
        where = " AND ".join(clauses)

        rows = self.db.conn.execute(
            f"SELECT rule_name, profit_pct FROM trades WHERE {where}",
            params,
        ).fetchall()

        buckets = {}
        for r in rows:
            rid = r["rule_name"]
            buckets.setdefault(rid, []).append(r["profit_pct"])

        stats = []
        for rid, profits in buckets.items():
            wins   = [p for p in profits if p > 0]
            losses = [p for p in profits if p < 0]
            n = len(profits)
            win_rate = len(wins) / n if n else 0.0
            avg_w  = sum(wins) / len(wins) if wins else 0.0
            avg_l  = sum(losses) / len(losses) if losses else 0.0
            avg    = sum(profits) / n if n else 0.0
            expectancy = win_rate * avg_w + (1 - win_rate) * avg_l
            stats.append({
                "rule_id": rid,
                "count": n,
                "wins": len(wins),
                "losses": len(losses),
                "win_rate": round(win_rate, 4),
                "avg_profit_pct_wins":   round(avg_w, 4),
                "avg_profit_pct_losses": round(avg_l, 4),
                "avg_profit_pct":        round(avg, 4),
                "expectancy":            round(expectancy, 4),
            })
        stats.sort(key=lambda s: s["expectancy"], reverse=True)
        return stats

    # ==================== 旧 helper（保留供回测兼容）====================

    def check_buy_preconditions(self, code, name, date, market_data, positions, total_value, cash):
        """
        检查买入前置条件
        返回: (allowed, passed_checks, failed_checks)
        """
        preconds = self.rules.get("buy_preconditions", {})
        passed = []
        failed = []

        # 1. 现金储备检查
        cash_ratio = cash / total_value if total_value > 0 else 0
        min_cash = preconds.get("min_cash_ratio", 0.10)
        if cash_ratio < min_cash:
            failed.append(f"现金比例 {cash_ratio:.1%} < {min_cash:.0%}")
        else:
            passed.append(f"现金比例 {cash_ratio:.1%} >= {min_cash:.0%}")

        # 2. 单只基金仓位检查
        max_single = preconds.get("max_single_weight", 0.25)
        current_value = sum(p["shares"] * p["cost_nav"] for p in positions if p["code"] == code)
        current_weight = current_value / total_value if total_value > 0 else 0
        if current_weight > max_single:
            failed.append(f"单只仓位 {current_weight:.1%} > {max_single:.0%}")
        else:
            passed.append(f"单只仓位 {current_weight:.1%} <= {max_single:.0%}")

        # 3. 赛道集中度检查
        sector = self.get_fund_sector(code, name)
        sector_limits = self.rules.get("sector_limits", {})
        sector_limit = sector_limits.get(sector, 0.50)
        sector_value = sum(
            p["shares"] * p["cost_nav"]
            for p in positions
            if self.get_fund_sector(p["code"], p["name"]) == sector
        )
        sector_weight = sector_value / total_value if total_value > 0 else 0
        if sector_weight > sector_limit:
            failed.append(f"{sector}赛道 {sector_weight:.1%} > {sector_limit:.0%}")
        else:
            passed.append(f"{sector}赛道 {sector_weight:.1%} <= {sector_limit:.0%}")

        # 4. 追高检查
        max_chase = preconds.get("max_chase_return_5d", 0.10)
        fund_5d_return = market_data.get("fund_5d_return", 0)
        if fund_5d_return > max_chase:
            failed.append(f"近5天涨幅 {fund_5d_return:.1%} > {max_chase:.0%}（追高）")
        else:
            passed.append(f"近5天涨幅 {fund_5d_return:.1%} <= {max_chase:.0%}")

        # 5. 大盘环境检查
        market_check = preconds.get("market_check", {})
        hs300_5d = market_data.get("hs300_5d_return", 0)
        hs300_20d = market_data.get("hs300_20d_return", 0)

        if hs300_5d < market_check.get("hs300_5d_drop_limit", -0.05):
            failed.append(f"沪深300近5天跌 {hs300_5d:.1%}，仅允许加仓已持仓")
        elif hs300_20d < market_check.get("hs300_20d_drop_limit", -0.15):
            failed.append(f"沪深300近20天跌 {hs300_20d:.1%}，熊市禁止买入")
        else:
            passed.append(f"大盘环境正常（5天 {hs300_5d:.1%}，20天 {hs300_20d:.1%}）")

        # 6. 趋势检查
        max_drops = preconds.get("max_consecutive_drops", 5)
        consecutive_drops = market_data.get("fund_consecutive_drops", 0)
        if consecutive_drops >= max_drops:
            failed.append(f"连续下跌 {consecutive_drops} 天，暂缓")
        else:
            passed.append(f"趋势正常（连续下跌 {consecutive_drops} 天）")

        allowed = len(failed) == 0
        return allowed, passed, failed

    def check_stop_loss(self, code, date, position, market_data):
        """
        检查止损规则
        返回: (triggered, rule_name, sell_ratio, reason)
        """
        stop_loss_rules = self.rules.get("stop_loss", [])
        # 按优先级排序
        stop_loss_rules = sorted(stop_loss_rules, key=lambda x: x.get("priority", 99))

        cost_nav = position["cost_nav"]
        current_nav = market_data.get("current_nav", cost_nav)
        loss_pct = (current_nav - cost_nav) / cost_nav if cost_nav > 0 else 0

        # 买入日期
        buy_date_str = position.get("buy_date")
        if buy_date_str:
            buy_date = datetime.fromisoformat(buy_date_str[:10])
            hold_days = (datetime.fromisoformat(date) - buy_date).days
        else:
            hold_days = 0

        for rule in stop_loss_rules:
            conditions = rule.get("conditions", {})
            triggered = False

            # 绝对止损
            if "loss_pct" in conditions and conditions["loss_pct"] == -0.20:
                if loss_pct <= -0.20:
                    triggered = True

            # 紧急止损
            elif "single_day_drop" in conditions:
                day_drop = market_data.get("fund_day_return", 0)
                three_day_drop = market_data.get("fund_3d_return", 0)
                if day_drop <= conditions["single_day_drop"] or three_day_drop <= conditions.get("three_day_drop", -0.10):
                    triggered = True

            # 趋势止损
            elif "consecutive_drops" in conditions and "below_ma20" in conditions:
                if (hold_days >= conditions.get("hold_days_min", 30) and
                    market_data.get("fund_consecutive_drops", 0) >= conditions["consecutive_drops"] and
                    market_data.get("below_ma20", False)):
                    triggered = True

            # 短期成本止损
            elif "hold_days_max" in conditions and conditions.get("hold_days_max") == 30:
                if hold_days < 30 and loss_pct <= conditions["loss_pct"]:
                    triggered = True

            # 中期成本止损
            elif "hold_days_min" in conditions and conditions.get("hold_days_min") == 30:
                if 30 <= hold_days < 90 and loss_pct <= conditions["loss_pct"]:
                    triggered = True

            # 长期成本止损
            elif "hold_days_min" in conditions and conditions.get("hold_days_min") == 90:
                if hold_days >= 90 and loss_pct <= conditions["loss_pct"]:
                    triggered = True

            if triggered:
                action = rule["action"]
                sell_ratio = {
                    "sell_30%": 0.30,
                    "sell_50%": 0.50,
                    "sell_100%": 1.00
                }.get(action, 0.50)
                return True, rule["name"], sell_ratio, rule["description"]

        return False, None, 0, None

    def check_take_profit(self, code, date, position, market_data):
        """
        检查止盈规则
        返回: (triggered, rule_name, sell_ratio, reason)
        """
        take_profit_rules = self.rules.get("take_profit", [])
        cost_nav = position["cost_nav"]
        current_nav = market_data.get("current_nav", cost_nav)
        profit_pct = (current_nav - cost_nav) / cost_nav if cost_nav > 0 else 0

        for rule in take_profit_rules:
            if profit_pct >= rule["level"]:
                action = rule["action"]
                sell_ratio = {
                    "sell_25%": 0.25,
                    "sell_50%": 0.50,
                    "sell_100%": 1.00
                }.get(action, 0.25)
                return True, f"止盈{rule['level']:.0%}", sell_ratio, rule["description"]

        # 回撤止盈
        drawdown_rules = self.rules.get("drawdown_take_profit", {})
        if drawdown_rules.get("track_highest"):
            highest_nav = market_data.get("highest_nav", current_nav)
            if highest_nav > cost_nav:
                drawdown_from_high = (highest_nav - current_nav) / highest_nav
                if drawdown_from_high >= 0.15:
                    return True, "回撤止盈15%", 1.00, "从最高点回撤 > 15% 清仓"
                elif drawdown_from_high >= 0.10:
                    return True, "回撤止盈10%", 0.50, "从最高点回撤 > 10% 卖出50%"

        return False, None, 0, None

    def check_low_buy(self, code, name, date, market_data, positions, total_value, cash):
        """
        检查低吸条件
        返回: (allowed, reason)
        """
        low_buy = self.rules.get("low_buy", {})
        conditions = low_buy.get("conditions", {})

        # 检查所有条件
        if market_data.get("market_regime") == "熊市":
            return False, "熊市环境"

        if market_data.get("fund_5d_return", 0) > conditions.get("target_5d_return_max", 0.05):
            return False, "近5天涨幅过高"

        if market_data.get("fund_day_return", 0) > conditions.get("target_day_drop_min", -0.03):
            return False, "当日跌幅不够"

        if cash / total_value < conditions.get("cash_ratio_min", 0.15):
            return False, "现金不足"

        # 检查单只仓位
        current_value = sum(p["shares"] * p["cost_nav"] for p in positions if p["code"] == code)
        if current_value / total_value > conditions.get("single_weight_max", 0.20):
            return False, "仓位已满"

        return True, "符合低吸条件"

    def calculate_buy_amount(self, code, date, market_data, positions, total_value, cash):
        """计算买入金额"""
        buy_amount_rules = self.rules.get("buy_amount", {})
        base_ratio = buy_amount_rules.get("base_ratio", 0.05)
        base_amount = total_value * base_ratio

        # 判断信号强度
        strong_signals = 0
        weak_signals = 0

        # 大盘近5天反弹 > 3%
        if market_data.get("hs300_5d_return", 0) > 0.03:
            strong_signals += 1

        # 目标基金近5天跌 > 5%
        if market_data.get("fund_5d_return", 0) < -0.05:
            strong_signals += 1

        # 大盘震荡
        if abs(market_data.get("hs300_5d_return", 0)) < 0.02:
            weak_signals += 1

        # 已持有（加仓）
        if any(p["code"] == code for p in positions):
            weak_signals += 1

        # 调整倍数
        if strong_signals > 0:
            multiplier = buy_amount_rules.get("strong_signal_multiplier", 2.0)
        elif weak_signals > 0:
            multiplier = buy_amount_rules.get("weak_signal_multiplier", 0.5)
        else:
            multiplier = 1.0

        amount = base_amount * multiplier
        # 不超过现金的 80%
        amount = min(amount, cash * 0.8)
        return amount

    def log_decision(self, date, code, name, action, rule_name, decision_context,
                     reason, checks_passed, checks_failed, trade_id=None):
        """记录决策审计日志"""
        # 更新交易记录的审计字段
        if trade_id:
            cursor = self.db.conn.cursor()
            cursor.execute("""
                UPDATE trades SET
                    rule_name = ?,
                    rule_version = ?,
                    decision_context = ?,
                    reason = ?,
                    checks_passed = ?,
                    checks_failed = ?
                WHERE id = ?
            """, (
                rule_name,
                self.strategy_version,
                json.dumps(decision_context, ensure_ascii=False),
                reason,
                json.dumps(checks_passed, ensure_ascii=False) if checks_passed else None,
                json.dumps(checks_failed, ensure_ascii=False) if checks_failed else None,
                trade_id
            ))
            self.db.conn.commit()

    def analyze_performance(self, start_date=None, end_date=None):
        """分析账户表现"""
        trades = self.db.get_trades(self.account_id)

        # 筛选日期范围
        if start_date:
            trades = [t for t in trades if t["date"] >= start_date]
        if end_date:
            trades = [t for t in trades if t["date"] <= end_date]

        # 统计
        total_trades = len(trades)
        wins = sum(1 for t in trades if t.get("outcome") == "win")
        losses = sum(1 for t in trades if t.get("outcome") == "loss")
        pending = sum(1 for t in trades if t.get("outcome") == "pending" or not t.get("outcome"))

        # 按规则统计
        rule_stats = {}
        for t in trades:
            rule = t.get("rule_name") or "未分类"
            if rule not in rule_stats:
                rule_stats[rule] = {"total": 0, "wins": 0, "losses": 0}
            rule_stats[rule]["total"] += 1
            if t.get("outcome") == "win":
                rule_stats[rule]["wins"] += 1
            elif t.get("outcome") == "loss":
                rule_stats[rule]["losses"] += 1

        return {
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "pending": pending,
            "win_rate": wins / total_trades if total_trades > 0 else 0,
            "rule_stats": rule_stats
        }

    def suggest_evolution(self, performance_report):
        """基于表现建议进化方向"""
        suggestions = []

        # 分析各规则表现
        for rule, stats in performance_report.get("rule_stats", {}).items():
            if stats["total"] >= 5:  # 至少5笔交易
                win_rate = stats["wins"] / stats["total"]
                if win_rate < 0.4:
                    suggestions.append({
                        "rule": rule,
                        "issue": f"胜率过低 {win_rate:.1%}",
                        "suggestion": f"考虑收紧 {rule} 的条件"
                    })
                elif win_rate > 0.7:
                    suggestions.append({
                        "rule": rule,
                        "issue": f"胜率很高 {win_rate:.1%}",
                        "suggestion": f"可以考虑放宽 {rule} 的条件"
                    })

        return suggestions


def main():
    """CLI 测试"""
    import argparse
    parser = argparse.ArgumentParser(description="决策引擎 — Smart Invest Skill")
    sub = parser.add_subparsers(dest="command")

    p_analyze = sub.add_parser("analyze", help="分析账户表现")
    p_analyze.add_argument("--account", "-a", required=True, help="账户名称")
    p_analyze.add_argument("--start", help="开始日期")
    p_analyze.add_argument("--end", help="结束日期")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    if args.command == "analyze":
        db = Database()
        account = db.get_account(name=args.account)
        if not account:
            print(f"[ERROR] 账户不存在: {args.account}")
            return

        engine = DecisionEngine(db, account["id"], account.get("strategy_version"))
        report = engine.analyze_performance(args.start, args.end)

        print(f"\n账户: {args.account} 表现分析")
        print(f"{'='*60}")
        print(f"总交易: {report['total_trades']}")
        print(f"盈利: {report['wins']}")
        print(f"亏损: {report['losses']}")
        print(f"胜率: {report['win_rate']:.1%}")
        print(f"\n各规则表现:")
        for rule, stats in report["rule_stats"].items():
            win_rate = stats["wins"] / stats["total"] if stats["total"] > 0 else 0
            print(f"  {rule}: {stats['total']}笔, 胜率 {win_rate:.1%}")
        print(f"{'='*60}\n")

        suggestions = engine.suggest_evolution(report)
        if suggestions:
            print("进化建议:")
            for s in suggestions:
                print(f"  - {s['rule']}: {s['suggestion']}")

        db.close()


if __name__ == "__main__":
    main()
