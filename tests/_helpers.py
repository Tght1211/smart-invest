"""Shared helpers for decision_engine tests.

Pure stdlib (unittest). No third-party deps. Each helper returns a
deterministic, hand-tuned scenario so tests assert exact engine output
without flakiness from real market data.
"""
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def load_rules():
    """Load production rules from data/decision_tree.json."""
    with open(REPO_ROOT / "data" / "decision_tree.json", "r", encoding="utf-8") as f:
        tree = json.load(f)
    return tree.get("rules", tree)


def make_in_memory_db():
    """In-memory SQLite Database with schema initialized.

    Suppresses init_tables / create_account stdout to keep test output clean.
    """
    import io
    import contextlib
    from db import Database
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db = Database.__new__(Database)
    db.db_path = ":memory:"
    db.conn = conn
    with contextlib.redirect_stdout(io.StringIO()):
        db.init_tables()
    return db


def add_test_account(db, name="test", account_type="main", budget=10000.0):
    """Create an account on the in-memory db, silently. Returns account_id."""
    import io
    import contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        return db.create_account(name=name, type_=account_type, budget=budget)


def make_market_data(**overrides):
    """Build a baseline market_data dict; override any keys.

    Neutral baseline — no rules should fire on this alone.
    """
    base = {
        "hs300_5d_return": 0.0,
        "hs300_20d_return": 0.02,
        "regime_hint": None,
        "funds": {
            "512480": {
                "name": "半导体ETF国联安",
                "current_nav": 2.30,
                "day_return": 0.0,
                "fund_3d_return": 0.0,
                "fund_5d_return": 0.0,
                "fund_20d_return": 0.0,
                "high_20d": 2.40,
                "sector": "科技",
            },
            "006479": {
                "name": "广发纳斯达克100ETF联接C",
                "current_nav": 8.20,
                "day_return": 0.0,
                "fund_3d_return": 0.0,
                "fund_5d_return": 0.02,
                "fund_20d_return": 0.05,
                "high_20d": 8.30,
                "sector": "海外",
            },
        },
    }
    base.update(overrides)
    return base


def make_position(code, name, shares, cost_nav, sector, hold_days=10):
    return {
        "code": code,
        "name": name,
        "shares": shares,
        "cost_nav": cost_nav,
        "sector": sector,
        "hold_days": hold_days,
    }


def find_blocked(packet, code, reason_id):
    return [b for b in packet["blocked_actions"]
            if b["code"] == code and b["blocked_by"] == reason_id]


def find_action(packet, code, action):
    return [a for a in packet["actions"]
            if a["code"] == code and a["action"] == action]
