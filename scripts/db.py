#!/usr/bin/env python3
"""
数据库层 — Smart Invest Skill
SQLite 数据库管理，支持多账户、决策树版本、进化记录。
纯 Python 3 标准库。
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_FILE = Path(os.environ.get("SMART_INVEST_DB") or (DATA_DIR / "smart_invest.db"))


class Database:
    """SQLite 数据库管理"""

    def __init__(self, db_path=None):
        self.db_path = db_path or DB_FILE
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    def close(self):
        """关闭连接"""
        if self.conn:
            self.conn.close()

    def init_tables(self):
        """初始化所有表"""
        cursor = self.conn.cursor()

        # 1. 账户表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                budget REAL NOT NULL,
                cash REAL NOT NULL,
                status TEXT DEFAULT 'active',
                sim_id TEXT,
                strategy_version TEXT DEFAULT 'v2.0',
                created_at TEXT,
                updated_at TEXT
            )
        """)

        # 2. 持仓表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY,
                account_id INTEGER NOT NULL REFERENCES accounts(id),
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                shares REAL NOT NULL,
                cost_nav REAL NOT NULL,
                buy_date TEXT,
                sector TEXT,
                platform TEXT DEFAULT '支付宝',
                note TEXT,
                updated_at TEXT,
                UNIQUE(account_id, code)
            )
        """)

        # 3. 交易表（含审计信息）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY,
                account_id INTEGER NOT NULL REFERENCES accounts(id),
                date TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                action TEXT NOT NULL,
                amount REAL NOT NULL,
                nav REAL NOT NULL,
                shares REAL NOT NULL,
                rule_name TEXT,
                rule_version TEXT,
                decision_context TEXT,
                reason TEXT,
                checks_passed TEXT,
                checks_failed TEXT,
                profit_pct REAL,
                outcome TEXT
            )
        """)

        # 4. 每日快照
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_snapshots (
                id INTEGER PRIMARY KEY,
                account_id INTEGER NOT NULL REFERENCES accounts(id),
                date TEXT NOT NULL,
                total_value REAL,
                cash REAL,
                positions_value REAL,
                return_pct REAL,
                drawdown REAL,
                market_regime TEXT,
                sector_exposure TEXT,
                UNIQUE(account_id, date)
            )
        """)

        # 5. 决策树版本表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS decision_tree_versions (
                id INTEGER PRIMARY KEY,
                version TEXT NOT NULL UNIQUE,
                parent_version TEXT,
                changelog TEXT NOT NULL,
                reason TEXT NOT NULL,
                evidence TEXT,
                rules_json TEXT NOT NULL,
                backtest_results TEXT,
                created_at TEXT,
                created_by TEXT
            )
        """)

        # 6. 策略进化记录
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS strategy_evolutions (
                id INTEGER PRIMARY KEY,
                from_version TEXT NOT NULL,
                to_version TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                trigger_source TEXT NOT NULL,
                trigger_detail TEXT,
                before_metrics TEXT,
                after_metrics TEXT,
                lessons_learned TEXT,
                created_at TEXT
            )
        """)

        # 7. 回测运行记录
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS simulation_runs (
                id INTEGER PRIMARY KEY,
                sim_id TEXT NOT NULL UNIQUE,
                account_id INTEGER REFERENCES accounts(id),
                start_date TEXT,
                end_date TEXT,
                budget REAL,
                strategy_version TEXT,
                fund_pool TEXT,
                total_return REAL,
                max_drawdown REAL,
                win_rate REAL,
                total_trades INTEGER,
                diary_path TEXT,
                status TEXT DEFAULT 'running',
                created_at TEXT
            )
        """)

        # 8. 操作复盘评定（"记忆"：每笔历史交易事后是否踩中）
        self._ensure_review_table(cursor)

        # 创建索引
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_positions_account ON positions(account_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_account ON trades(account_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(date)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_account ON daily_snapshots(account_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_simulations_account ON simulation_runs(account_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_reviews_account ON trade_reviews(account_id)")

        self.conn.commit()
        print(f"[OK] 数据库已初始化: {self.db_path}")

    # ========== 账户操作 ==========

    def create_account(self, name, type_, budget, strategy_version="v2.0", sim_id=None):
        """创建账户"""
        now = datetime.now().isoformat()
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO accounts (name, type, budget, cash, status, sim_id, strategy_version, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)
        """, (name, type_, budget, budget, sim_id, strategy_version, now, now))
        self.conn.commit()
        account_id = cursor.lastrowid
        print(f"[OK] 账户已创建: {name} (ID: {account_id})")
        return account_id

    def get_account(self, name=None, type_=None, account_id=None):
        """获取账户"""
        cursor = self.conn.cursor()
        if account_id:
            cursor.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
        elif name:
            cursor.execute("SELECT * FROM accounts WHERE name = ?", (name,))
        elif type_:
            cursor.execute("SELECT * FROM accounts WHERE type = ?", (type_,))
        else:
            return None
        row = cursor.fetchone()
        return dict(row) if row else None

    def list_accounts(self):
        """列出所有账户"""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM accounts ORDER BY created_at DESC")
        return [dict(row) for row in cursor.fetchall()]

    def update_account(self, account_id, **kwargs):
        """更新账户"""
        if not kwargs:
            return
        kwargs["updated_at"] = datetime.now().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in kwargs.keys())
        values = list(kwargs.values()) + [account_id]
        cursor = self.conn.cursor()
        cursor.execute(f"UPDATE accounts SET {set_clause} WHERE id = ?", values)
        self.conn.commit()

    def get_account_summary(self, account_id):
        """获取账户摘要（含持仓统计）"""
        account = self.get_account(account_id=account_id)
        if not account:
            return None

        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) as count, SUM(shares * cost_nav) as value FROM positions WHERE account_id = ?", (account_id,))
        pos_stats = dict(cursor.fetchone())

        cursor.execute("SELECT COUNT(*) as count FROM trades WHERE account_id = ?", (account_id,))
        trade_count = cursor.fetchone()["count"]

        return {
            **account,
            "position_count": pos_stats["count"] or 0,
            "position_value": pos_stats["value"] or 0,
            "total_trades": trade_count
        }

    # ========== 持仓操作 ==========

    def set_position(self, account_id, code, name, shares, cost_nav, buy_date=None, sector=None, platform="支付宝", note=None):
        """设置持仓（新增或更新）"""
        now = datetime.now().isoformat()
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO positions (account_id, code, name, shares, cost_nav, buy_date, sector, platform, note, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, code) DO UPDATE SET
                shares = excluded.shares,
                cost_nav = excluded.cost_nav,
                note = COALESCE(excluded.note, positions.note),
                updated_at = excluded.updated_at
        """, (account_id, code, name, shares, cost_nav, buy_date, sector, platform, note, now))
        self.conn.commit()

    def get_positions(self, account_id):
        """获取账户持仓"""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM positions WHERE account_id = ? ORDER BY code", (account_id,))
        return [dict(row) for row in cursor.fetchall()]

    def remove_position(self, account_id, code):
        """删除持仓"""
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM positions WHERE account_id = ? AND code = ?", (account_id, code))
        self.conn.commit()
        return cursor.rowcount > 0

    def update_position_shares(self, account_id, code, shares_delta, new_cost_nav=None):
        """更新持仓份额（加仓/减仓）"""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM positions WHERE account_id = ? AND code = ?", (account_id, code))
        pos = cursor.fetchone()
        if not pos:
            return False

        new_shares = pos["shares"] + shares_delta
        if new_shares <= 0:
            self.remove_position(account_id, code)
            return True

        if new_cost_nav:
            cursor.execute("""
                UPDATE positions SET shares = ?, cost_nav = ?, updated_at = ?
                WHERE account_id = ? AND code = ?
            """, (new_shares, new_cost_nav, datetime.now().isoformat(), account_id, code))
        else:
            cursor.execute("""
                UPDATE positions SET shares = ?, updated_at = ?
                WHERE account_id = ? AND code = ?
            """, (new_shares, datetime.now().isoformat(), account_id, code))
        self.conn.commit()
        return True

    # ========== 交易操作 ==========

    def add_trade(self, account_id, date, code, name, action, amount, nav, shares,
                  rule_name=None, rule_version=None, decision_context=None, reason=None,
                  checks_passed=None, checks_failed=None, profit_pct=None, outcome=None):
        """添加交易记录"""
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO trades (account_id, date, code, name, action, amount, nav, shares,
                              rule_name, rule_version, decision_context, reason,
                              checks_passed, checks_failed, profit_pct, outcome)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (account_id, date, code, name, action, amount, nav, shares,
              rule_name, rule_version,
              json.dumps(decision_context, ensure_ascii=False) if decision_context else None,
              reason,
              json.dumps(checks_passed, ensure_ascii=False) if checks_passed else None,
              json.dumps(checks_failed, ensure_ascii=False) if checks_failed else None,
              profit_pct, outcome))
        self.conn.commit()
        return cursor.lastrowid

    def get_trades(self, account_id, date=None, limit=None):
        """获取交易记录"""
        cursor = self.conn.cursor()
        query = "SELECT * FROM trades WHERE account_id = ?"
        params = [account_id]
        if date:
            query += " AND date = ?"
            params.append(date)
        query += " ORDER BY date DESC, id DESC"
        if limit:
            query += f" LIMIT {limit}"
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def get_trade_stats(self, account_id):
        """获取交易统计"""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) as losses,
                AVG(profit_pct) as avg_profit
            FROM trades
            WHERE account_id = ? AND outcome IS NOT NULL
        """, (account_id,))
        row = dict(cursor.fetchone())
        if row["total_trades"] > 0:
            row["win_rate"] = row["wins"] / row["total_trades"]
        else:
            row["win_rate"] = 0
        return row

    # ========== 快照操作 ==========

    def add_snapshot(self, account_id, date, total_value, cash, positions_value,
                     return_pct, drawdown=None, market_regime=None, sector_exposure=None):
        """添加每日快照"""
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO daily_snapshots (account_id, date, total_value, cash, positions_value,
                                        return_pct, drawdown, market_regime, sector_exposure)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, date) DO UPDATE SET
                total_value = excluded.total_value,
                cash = excluded.cash,
                positions_value = excluded.positions_value,
                return_pct = excluded.return_pct,
                drawdown = excluded.drawdown,
                market_regime = excluded.market_regime,
                sector_exposure = excluded.sector_exposure
        """, (account_id, date, total_value, cash, positions_value, return_pct, drawdown,
              market_regime,
              json.dumps(sector_exposure, ensure_ascii=False) if sector_exposure else None))
        self.conn.commit()

    def get_snapshots(self, account_id, start_date=None, end_date=None):
        """获取快照"""
        cursor = self.conn.cursor()
        query = "SELECT * FROM daily_snapshots WHERE account_id = ?"
        params = [account_id]
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        query += " ORDER BY date"
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    # ========== 决策树版本 ==========

    def add_tree_version(self, version, parent_version, changelog, reason, rules_json,
                         evidence=None, backtest_results=None, created_by="ai"):
        """添加决策树版本"""
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO decision_tree_versions (version, parent_version, changelog, reason,
                                              evidence, rules_json, backtest_results, created_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (version, parent_version, changelog, reason, evidence,
              json.dumps(rules_json, ensure_ascii=False) if isinstance(rules_json, dict) else rules_json,
              backtest_results, datetime.now().isoformat(), created_by))
        self.conn.commit()
        print(f"[OK] 决策树版本已保存: {version}")
        return cursor.lastrowid

    def get_tree_version(self, version=None):
        """获取决策树版本（None=最新）"""
        cursor = self.conn.cursor()
        if version:
            cursor.execute("SELECT * FROM decision_tree_versions WHERE version = ?", (version,))
        else:
            cursor.execute("SELECT * FROM decision_tree_versions ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        if row:
            result = dict(row)
            if result["rules_json"]:
                result["rules"] = json.loads(result["rules_json"])
            return result
        return None

    def list_tree_versions(self):
        """列出所有决策树版本"""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM decision_tree_versions ORDER BY id DESC")
        return [dict(row) for row in cursor.fetchall()]

    # ========== 进化记录 ==========

    def add_evolution(self, from_version, to_version, title, description, trigger_source,
                      trigger_detail=None, before_metrics=None, after_metrics=None, lessons_learned=None):
        """添加进化记录"""
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO strategy_evolutions (from_version, to_version, title, description,
                                            trigger_source, trigger_detail, before_metrics,
                                            after_metrics, lessons_learned, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (from_version, to_version, title, description, trigger_source, trigger_detail,
              json.dumps(before_metrics, ensure_ascii=False) if before_metrics else None,
              json.dumps(after_metrics, ensure_ascii=False) if after_metrics else None,
              lessons_learned, datetime.now().isoformat()))
        self.conn.commit()
        print(f"[OK] 进化记录已保存: {title}")
        return cursor.lastrowid

    def get_evolution_history(self):
        """获取进化历史"""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM strategy_evolutions ORDER BY id DESC")
        return [dict(row) for row in cursor.fetchall()]

    # ========== 回测运行 ==========

    def add_simulation(self, sim_id, account_id, start_date, end_date, budget,
                       strategy_version, fund_pool=None):
        """添加回测运行记录"""
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO simulation_runs (sim_id, account_id, start_date, end_date, budget,
                                        strategy_version, fund_pool, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)
        """, (sim_id, account_id, start_date, end_date, budget, strategy_version,
              json.dumps(fund_pool, ensure_ascii=False) if fund_pool else None,
              datetime.now().isoformat()))
        self.conn.commit()
        return cursor.lastrowid

    def update_simulation(self, sim_id, **kwargs):
        """更新回测结果"""
        if not kwargs:
            return
        set_clause = ", ".join(f"{k} = ?" for k in kwargs.keys())
        values = list(kwargs.values()) + [sim_id]
        cursor = self.conn.cursor()
        cursor.execute(f"UPDATE simulation_runs SET {set_clause} WHERE sim_id = ?", values)
        self.conn.commit()

    def get_simulation(self, sim_id):
        """获取回测记录"""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM simulation_runs WHERE sim_id = ?", (sim_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def list_simulations(self):
        """列出所有回测"""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM simulation_runs ORDER BY created_at DESC")
        return [dict(row) for row in cursor.fetchall()]

    # ========== 操作复盘评定（记忆） ==========

    def _ensure_review_table(self, cursor=None):
        """建 trade_reviews 表（幂等）。老库无需重跑 init 也能自愈。"""
        c = cursor or self.conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS trade_reviews (
                id INTEGER PRIMARY KEY,
                account_id INTEGER NOT NULL,
                trade_id INTEGER,
                code TEXT,
                name TEXT,
                action TEXT,
                trade_date TEXT,
                horizon_days INTEGER,
                nav_at_trade REAL,
                nav_after REAL,
                nav_after_date TEXT,
                post_return_pct REAL,
                verdict TEXT,
                score REAL,
                lesson TEXT,
                market_context TEXT,
                reviewed_at TEXT,
                UNIQUE(trade_id, horizon_days)
            )
        """)
        if cursor is None:
            self.conn.commit()

    def add_trade_review(self, account_id, trade_id, code, name, action, trade_date,
                         horizon_days, nav_at_trade, nav_after, nav_after_date,
                         post_return_pct, verdict, score, lesson=None, market_context=None):
        """写入/更新一笔操作复盘（按 trade_id + horizon 去重 upsert）。"""
        self._ensure_review_table()
        now = datetime.now().isoformat()
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO trade_reviews
                (account_id, trade_id, code, name, action, trade_date, horizon_days,
                 nav_at_trade, nav_after, nav_after_date, post_return_pct,
                 verdict, score, lesson, market_context, reviewed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_id, horizon_days) DO UPDATE SET
                nav_after = excluded.nav_after,
                nav_after_date = excluded.nav_after_date,
                post_return_pct = excluded.post_return_pct,
                verdict = excluded.verdict,
                score = excluded.score,
                lesson = excluded.lesson,
                market_context = excluded.market_context,
                reviewed_at = excluded.reviewed_at
        """, (account_id, trade_id, code, name, action, trade_date, horizon_days,
              nav_at_trade, nav_after, nav_after_date, post_return_pct,
              verdict, score, lesson, market_context, now))
        self.conn.commit()
        return cursor.lastrowid

    def get_trade_reviews(self, account_id, code=None, limit=None):
        """取已存复盘记录（按交易日倒序）。"""
        self._ensure_review_table()
        cursor = self.conn.cursor()
        query = "SELECT * FROM trade_reviews WHERE account_id = ?"
        params = [account_id]
        if code:
            query += " AND code = ?"
            params.append(code)
        query += " ORDER BY trade_date DESC, id DESC"
        if limit:
            query += f" LIMIT {int(limit)}"
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def get_review_summary(self, account_id, lookback_days=None):
        """聚合复盘：买/卖择时胜率、各结论计数、均分、近期教训。"""
        rows = self.get_trade_reviews(account_id)
        if lookback_days:
            cutoff = (datetime.now().date() - timedelta(days=lookback_days)).isoformat()
            rows = [r for r in rows if (r.get("trade_date") or "") >= cutoff]
        if not rows:
            return {"count": 0}

        def _winrate(items):
            scored = [r for r in items if r.get("score") is not None]
            if not scored:
                return None
            wins = sum(1 for r in scored if r["score"] > 0)
            return round(wins / len(scored), 4)

        buys = [r for r in rows if r.get("action") == "buy"]
        sells = [r for r in rows if r.get("action") == "sell"]
        verdict_counts = {}
        for r in rows:
            v = r.get("verdict") or "未知"
            verdict_counts[v] = verdict_counts.get(v, 0) + 1
        scores = [r["score"] for r in rows if r.get("score") is not None]
        lessons = [r["lesson"] for r in rows[:5] if r.get("lesson")]
        return {
            "count": len(rows),
            "buy_count": len(buys),
            "sell_count": len(sells),
            "buy_timing_winrate": _winrate(buys),
            "sell_timing_winrate": _winrate(sells),
            "overall_winrate": _winrate(rows),
            "avg_score": round(sum(scores) / len(scores), 4) if scores else None,
            "verdict_counts": verdict_counts,
            "recent_lessons": lessons,
        }


# ========== CLI ==========

def cmd_init(args):
    """初始化数据库"""
    db = Database()
    db.init_tables()
    db.close()


def cmd_accounts(args):
    """列出所有账户"""
    db = Database()
    accounts = db.list_accounts()
    if not accounts:
        print("暂无账户")
        return

    print(f"\n{'='*80}")
    print(f"{'ID':<5} {'名称':<20} {'类型':<10} {'预算':>12} {'现金':>12} {'策略版本':<10}")
    print(f"{'='*80}")
    for acc in accounts:
        print(f"{acc['id']:<5} {acc['name']:<20} {acc['type']:<10} "
              f"¥{acc['budget']:>10,.2f} ¥{acc['cash']:>10,.2f} {acc['strategy_version']:<10}")
    print(f"{'='*80}\n")
    db.close()


def cmd_positions(args):
    """查看持仓"""
    db = Database()
    account = db.get_account(name=args.account)
    if not account:
        print(f"[ERROR] 账户不存在: {args.account}")
        return

    positions = db.get_positions(account["id"])
    if not positions:
        print(f"账户 {args.account} 暂无持仓")
        return

    print(f"\n账户: {args.account} (ID: {account['id']})")
    print(f"{'='*100}")
    print(f"{'代码':<10} {'名称':<30} {'份额':>12} {'成本净值':>10} {'市值':>12} {'赛道':<10}")
    print(f"{'='*100}")
    for pos in positions:
        value = pos["shares"] * pos["cost_nav"]
        print(f"{pos['code']:<10} {pos['name']:<30} {pos['shares']:>12,.2f} "
              f"¥{pos['cost_nav']:>8,.4f} ¥{value:>10,.2f} {pos.get('sector', ''):<10}")
    print(f"{'='*100}\n")
    db.close()


def cmd_trades(args):
    """查看交易"""
    db = Database()
    account = db.get_account(name=args.account)
    if not account:
        print(f"[ERROR] 账户不存在: {args.account}")
        return

    trades = db.get_trades(account["id"], limit=args.limit)
    if not trades:
        print(f"账户 {args.account} 暂无交易")
        return

    print(f"\n账户: {args.account} - 最近 {len(trades)} 笔交易")
    print(f"{'='*120}")
    print(f"{'日期':<12} {'代码':<10} {'名称':<25} {'方向':<6} {'金额':>12} {'净值':>10} {'规则':<20}")
    print(f"{'='*120}")
    for t in trades:
        action_cn = "买入" if t["action"] == "buy" else "卖出"
        nav_str = f"¥{t['nav']:>8,.4f}" if t['nav'] else "N/A"
        rule_str = t.get('rule_name') or ""
        print(f"{t['date']:<12} {t['code']:<10} {t['name']:<25} {action_cn:<6} "
              f"¥{t['amount']:>10,.2f} {nav_str} {rule_str:<20}")
    print(f"{'='*120}\n")
    db.close()


def cmd_reviews(args):
    """查看操作复盘评定（记忆）"""
    db = Database()
    account = db.get_account(name=args.account)
    if not account:
        print(f"[ERROR] 账户不存在: {args.account}")
        return

    reviews = db.get_trade_reviews(account["id"], limit=args.limit)
    if not reviews:
        print(f"账户 {args.account} 暂无复盘记录（先跑 decide.py review --save）")
        db.close()
        return

    print(f"\n账户: {args.account} - 操作复盘评定 ({len(reviews)} 条)")
    print(f"{'='*104}")
    print(f"{'交易日':<12} {'方向':<6} {'名称':<22} {'视界':>5} {'事后涨跌':>9} {'评定':<10} {'分':>6}")
    print(f"{'='*104}")
    for r in reviews:
        action_cn = "买入" if r["action"] == "buy" else "卖出"
        pr = r.get("post_return_pct")
        pr_str = f"{pr*100:+.2f}%" if pr is not None else "--"
        sc = r.get("score")
        sc_str = f"{sc:+.2f}" if sc is not None else "--"
        print(f"{r.get('trade_date',''):<12} {action_cn:<6} {(r.get('name') or '')[:22]:<22} "
              f"{str(r.get('horizon_days',''))+'d':>5} {pr_str:>9} {(r.get('verdict') or ''):<10} {sc_str:>6}")
    print(f"{'='*104}")

    summary = db.get_review_summary(account["id"])
    if summary.get("count"):
        bw = summary.get("buy_timing_winrate")
        sw = summary.get("sell_timing_winrate")
        bw_str = f"{bw*100:.0f}%" if bw is not None else "—"
        sw_str = f"{sw*100:.0f}%" if sw is not None else "—"
        print(f"宏观: 买入择时胜率 {bw_str}（{summary.get('buy_count',0)}笔） | "
              f"卖出择时胜率 {sw_str}（{summary.get('sell_count',0)}笔） | "
              f"均分 {summary.get('avg_score')}")
        vc = summary.get("verdict_counts") or {}
        if vc:
            print("  分布: " + " ".join(f"{k}×{v}" for k, v in vc.items()))
    print()
    db.close()


def cmd_tree_versions(args):
    """查看决策树版本"""
    db = Database()
    versions = db.list_tree_versions()
    if not versions:
        print("暂无决策树版本")
        return

    print(f"\n{'='*100}")
    print(f"{'版本':<10} {'父版本':<10} {'变更说明':<40} {'创建时间':<20}")
    print(f"{'='*100}")
    for v in versions:
        print(f"{v['version']:<10} {v.get('parent_version', ''):<10} "
              f"{v['changelog']:<40} {v['created_at'][:10]:<20}")
    print(f"{'='*100}\n")
    db.close()


def cmd_evolutions(args):
    """查看进化历史"""
    db = Database()
    evolutions = db.get_evolution_history()
    if not evolutions:
        print("暂无进化记录")
        return

    print(f"\n策略进化历史")
    print(f"{'='*100}")
    for e in evolutions:
        print(f"\n[{e['created_at'][:10]}] {e['title']}")
        print(f"  {e['from_version']} → {e['to_version']}")
        print(f"  {e['description']}")
        print(f"  触发: {e['trigger_source']}")
    print(f"{'='*100}\n")
    db.close()


def cmd_import_json(args):
    """从 JSON 文件导入数据"""
    db = Database()
    db.init_tables()

    # 导入 portfolio.json 和 orders.json 到主线账户
    portfolio_file = DATA_DIR / "portfolio.json"
    orders_file = DATA_DIR / "orders.json"
    config_file = DATA_DIR / "config.json"

    # 读取配置
    budget = 50000
    if config_file.exists():
        with open(config_file, "r", encoding="utf-8") as f:
            config = json.load(f)
            budget = config.get("total_budget", 50000)

    # 创建主线账户
    account_id = db.create_account("主线", "main", budget)

    # 导入持仓
    if portfolio_file.exists():
        with open(portfolio_file, "r", encoding="utf-8") as f:
            portfolio = json.load(f)
            for pos in portfolio:
                db.set_position(
                    account_id, pos["code"], pos["name"],
                    pos["shares"], pos["cost_nav"],
                    pos.get("buy_date"), pos.get("sector"),
                    pos.get("platform", "支付宝"), pos.get("note")
                )
        print(f"[OK] 导入 {len(portfolio)} 条持仓")

    # 导入订单
    if orders_file.exists():
        with open(orders_file, "r", encoding="utf-8") as f:
            orders = json.load(f)
            for order in orders:
                db.add_trade(
                    account_id, order["date"], order["code"], order["name"],
                    order["action"], order["amount"], order["nav"], order["shares"],
                    reason=order.get("note")
                )
        print(f"[OK] 导入 {len(orders)} 条订单")

    db.close()


def cmd_add_position(args):
    """添加持仓"""
    db = Database()
    account = db.get_account(name=args.account)
    if not account:
        print(f"[ERROR] 账户不存在: {args.account}")
        return

    db.set_position(
        account["id"], args.code, args.name,
        args.shares, args.cost,
        buy_date=args.date, sector=args.sector,
        note=args.note
    )
    print(f"[OK] 持仓已添加: {args.code} {args.name}")
    db.close()


def cmd_remove_position(args):
    """删除持仓"""
    db = Database()
    account = db.get_account(name=args.account)
    if not account:
        print(f"[ERROR] 账户不存在: {args.account}")
        return

    if db.remove_position(account["id"], args.code):
        print(f"[OK] 持仓已删除: {args.code}")
    else:
        print(f"[WARN] 未找到持仓: {args.code}")
    db.close()


def cmd_add_order(args):
    """添加订单"""
    db = Database()
    account = db.get_account(name=args.account)
    if not account:
        print(f"[ERROR] 账户不存在: {args.account}")
        return

    trade_id = db.add_trade(
        account["id"], args.date, args.code, args.name,
        args.action, args.amount, args.nav, args.shares,
        reason=args.note
    )
    print(f"[OK] 订单已添加: {args.action.upper()} {args.code} {args.name}")
    db.close()


def main():
    parser = argparse.ArgumentParser(description="数据库管理工具 — Smart Invest Skill")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="初始化数据库")

    sub.add_parser("accounts", help="列出所有账户")

    p_positions = sub.add_parser("positions", help="查看持仓")
    p_positions.add_argument("--account", "-a", required=True, help="账户名称")

    p_trades = sub.add_parser("trades", help="查看交易")
    p_trades.add_argument("--account", "-a", required=True, help="账户名称")
    p_trades.add_argument("--limit", "-l", type=int, default=50, help="显示条数")

    p_reviews = sub.add_parser("reviews", help="查看操作复盘评定（记忆）")
    p_reviews.add_argument("--account", "-a", required=True, help="账户名称")
    p_reviews.add_argument("--limit", "-l", type=int, default=30, help="显示条数")

    sub.add_parser("tree-versions", help="查看决策树版本")
    sub.add_parser("evolutions", help="查看进化历史")

    sub.add_parser("import-json", help="从 JSON 文件导入数据")

    # 持仓管理
    p_add_pos = sub.add_parser("add-position", help="添加持仓")
    p_add_pos.add_argument("--account", "-a", required=True, help="账户名称")
    p_add_pos.add_argument("--code", "-c", required=True, help="基金代码")
    p_add_pos.add_argument("--name", "-n", required=True, help="基金名称")
    p_add_pos.add_argument("--shares", type=float, required=True, help="持有份额")
    p_add_pos.add_argument("--cost", type=float, required=True, help="成本净值")
    p_add_pos.add_argument("--date", "-d", help="买入日期")
    p_add_pos.add_argument("--sector", "-s", help="所属赛道")
    p_add_pos.add_argument("--note", help="备注")

    p_rm_pos = sub.add_parser("remove-position", help="删除持仓")
    p_rm_pos.add_argument("--account", "-a", required=True, help="账户名称")
    p_rm_pos.add_argument("--code", "-c", required=True, help="基金代码")

    # 订单管理
    p_add_order = sub.add_parser("add-order", help="添加订单")
    p_add_order.add_argument("--account", "-a", required=True, help="账户名称")
    p_add_order.add_argument("--date", "-d", required=True, help="交易日期")
    p_add_order.add_argument("--code", "-c", required=True, help="基金代码")
    p_add_order.add_argument("--name", "-n", required=True, help="基金名称")
    p_add_order.add_argument("--action", choices=["buy", "sell"], required=True, help="操作方向")
    p_add_order.add_argument("--amount", type=float, required=True, help="交易金额")
    p_add_order.add_argument("--nav", type=float, required=True, help="成交净值")
    p_add_order.add_argument("--shares", type=float, required=True, help="交易份额")
    p_add_order.add_argument("--note", help="备注")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    commands = {
        "init": cmd_init,
        "accounts": cmd_accounts,
        "positions": cmd_positions,
        "trades": cmd_trades,
        "reviews": cmd_reviews,
        "tree-versions": cmd_tree_versions,
        "evolutions": cmd_evolutions,
        "import-json": cmd_import_json,
        "add-position": cmd_add_position,
        "remove-position": cmd_remove_position,
        "add-order": cmd_add_order,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
