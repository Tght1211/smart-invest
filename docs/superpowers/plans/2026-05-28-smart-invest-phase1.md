# Smart-Invest Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `scripts/decision_engine.py` the single, deterministic source of buy/sell decisions, called via a new thin CLI `scripts/decide.py`. Refactor SKILL.md so Claude interprets engine output rather than improvising rule application.

**Architecture:** Engine returns a structured "decision packet" (JSON). CLI is a thin wrapper that gathers market data and calls the engine. Tests pin the contract. SKILL.md becomes a translation layer, not a rules interpreter.

**Tech Stack:** Python 3.8+ stdlib only (sqlite3, urllib, json, argparse, datetime). `pytest` for tests (also works under `python3 -m unittest discover tests`).

**Spec:** `docs/superpowers/specs/2026-05-28-smart-invest-overhaul-design.md`

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `tests/__init__.py` | Create (empty) | Makes tests a package |
| `tests/conftest.py` | Create | Shared fixtures: in-memory DB, sample market data, sample positions, sample rules |
| `tests/test_decision_engine.py` | Create | All 19+ unit tests from spec §10 |
| `scripts/decision_engine.py` | Rewrite | `DecisionEngine.decide()` returns full packet per spec §6 |
| `scripts/fetch_fund.py` | Modify | Add `gather_market_snapshot()` function + `market-snapshot` CLI subcommand |
| `scripts/decide.py` | Create | Thin CLI: snapshot → engine → JSON / Markdown output |
| `data/decision_tree.json` | Modify | Add `id`, `enabled`, `severity` fields per rule; keep all existing fields |
| `SKILL.md` | Rewrite | Restructure per spec §11; target ≤ 700 lines |
| `CLAUDE.md` | Modify | Add note about `decide.py` as the single decision entry |

---

## Task 1: Set up test scaffolding and fixtures

**Files:**
- Create: `tests/__init__.py` (empty)
- Create: `tests/conftest.py`

- [ ] **Step 1: Create empty `tests/__init__.py`**

```python
```

- [ ] **Step 2: Create `tests/conftest.py` with shared fixtures**

```python
"""Shared pytest fixtures for decision_engine tests.

Each fixture returns a deterministic, hand-tuned scenario so tests assert
exact engine output without flakiness from real market data.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from db import Database  # noqa: E402


@pytest.fixture
def rules():
    """Load production rules from data/decision_tree.json."""
    with open(REPO_ROOT / "data" / "decision_tree.json", "r", encoding="utf-8") as f:
        tree = json.load(f)
    return tree.get("rules", tree)


@pytest.fixture
def db():
    """In-memory SQLite Database with schema initialized."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    database = Database.__new__(Database)
    database.db_path = ":memory:"
    database.conn = conn
    database.init_tables()
    yield database
    database.close()


@pytest.fixture
def account_id(db):
    """Create a 'test' main account with 10,000 budget. Returns account_id."""
    return db.add_account(name="test", account_type="main", budget=10000.0)


def make_market_data(**overrides):
    """Build a baseline market_data dict; override any keys."""
    base = {
        "hs300_5d_return": 0.0,
        "hs300_20d_return": 0.02,
        "regime_hint": None,
        "funds": {
            "512480": {
                "name": "半导体ETF国联安",
                "current_nav": 2.30,
                "day_return": 0.0,
                "fund_5d_return": 0.0,
                "fund_20d_return": 0.0,
                "high_20d": 2.40,
                "sector": "科技",
            },
            "006479": {
                "name": "广发纳斯达克100ETF联接C",
                "current_nav": 8.20,
                "day_return": 0.0,
                "fund_5d_return": 0.02,
                "fund_20d_return": 0.05,
                "high_20d": 8.30,
                "sector": "海外",
            },
        },
    }
    base.update(overrides)
    return base


@pytest.fixture
def market_data():
    """Neutral market state — no rules should fire."""
    return make_market_data()


def make_position(code, name, shares, cost_nav, sector, hold_days=10):
    return {
        "code": code,
        "name": name,
        "shares": shares,
        "cost_nav": cost_nav,
        "sector": sector,
        "hold_days": hold_days,
    }


@pytest.fixture
def empty_positions():
    return []


@pytest.fixture
def single_position():
    """One position: 半导体ETF, cost 2.34, currently underwater 1.7%."""
    return [make_position("512480", "半导体ETF国联安",
                          shares=1000.0, cost_nav=2.34, sector="科技")]


# Expose helpers for parametric tests
pytest.make_market_data = make_market_data
pytest.make_position = make_position
```

- [ ] **Step 3: Verify pytest discovers fixtures**

Run: `cd /Users/tght/develop/project/2026/smart-invest && python3 -m pytest tests/ --collect-only -q`
Expected: 0 tests collected, no errors.

- [ ] **Step 4: Commit**

```bash
git add tests/__init__.py tests/conftest.py
git commit -m "test: scaffold test infrastructure for decision_engine"
```

---

## Task 2: Add `Database.add_account()` helper (needed by fixture)

**Files:**
- Modify: `scripts/db.py` (add method to `Database` class)

- [ ] **Step 1: Read `scripts/db.py` to find the `Database` class and existing methods**

Run: `grep -n "def " /Users/tght/develop/project/2026/smart-invest/scripts/db.py | head -30`

- [ ] **Step 2: Add `add_account` method if it doesn't already exist**

Insert in `Database` class (after `init_tables`):

```python
    def add_account(self, name, account_type, budget, sim_id=None,
                    strategy_version="v2.0"):
        """Insert an account row. Returns account_id."""
        from datetime import datetime
        now = datetime.now().isoformat()
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO accounts (name, type, budget, cash, status, sim_id,
                                  strategy_version, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)
        """, (name, account_type, budget, budget, sim_id, strategy_version, now, now))
        self.conn.commit()
        return cur.lastrowid
```

If a similar method already exists with a different signature, keep the existing one and adjust the fixture in `conftest.py` to match.

- [ ] **Step 3: Run fixture-collection check**

Run: `python3 -m pytest tests/ --collect-only -q`
Expected: still 0 tests, no errors.

- [ ] **Step 4: Commit**

```bash
git add scripts/db.py
git commit -m "feat(db): add Database.add_account helper used by tests"
```

---

## Task 3: Lock the decision packet schema with a test

**Files:**
- Modify: `tests/test_decision_engine.py` (create)
- Modify: `scripts/decision_engine.py` (skeleton `decide()`)

- [ ] **Step 1: Write the failing test**

Create `tests/test_decision_engine.py`:

```python
"""Unit tests for decision_engine.DecisionEngine.

Schema reference: docs/superpowers/specs/2026-05-28-smart-invest-overhaul-design.md §6
Each test follows: build market_data + positions → engine.decide() → assert packet.
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from decision_engine import DecisionEngine


TOP_LEVEL_KEYS = {
    "schema_version", "generated_at", "account", "date", "rule_version",
    "market_regime", "portfolio_snapshot", "actions", "blocked_actions",
    "alerts", "summary",
}


def _decide(db, account_id, rules, market_data, positions, cash=5000.0,
            total_value=10000.0, date="2026-05-28"):
    engine = DecisionEngine(db, account_id, strategy_version="v2.0", rules_override=rules)
    return engine.decide(
        date=date, market_data=market_data, positions=positions,
        cash=cash, total_value=total_value,
    )


def test_decide_packet_has_all_top_level_keys(db, account_id, rules,
                                              market_data, empty_positions):
    packet = _decide(db, account_id, rules, market_data, empty_positions)
    assert set(packet.keys()) >= TOP_LEVEL_KEYS, f"missing: {TOP_LEVEL_KEYS - set(packet.keys())}"
    assert packet["schema_version"] == "1.0"
    assert packet["account"] == "test"
    assert packet["date"] == "2026-05-28"
    assert packet["rule_version"] == "v2.0"
    assert isinstance(packet["actions"], list)
    assert isinstance(packet["blocked_actions"], list)
    assert isinstance(packet["alerts"], list)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_decision_engine.py -v`
Expected: FAIL (`rules_override` kwarg or `decide` method missing).

- [ ] **Step 3: Implement skeleton in `scripts/decision_engine.py`**

Append to the existing `DecisionEngine` class (do NOT delete existing methods):

```python
    def __init__(self, db, account_id, strategy_version=None, rules_override=None):
        self.db = db
        self.account_id = account_id
        self.strategy_version = strategy_version or "v2.0"
        if rules_override is not None:
            self.rules = rules_override
        else:
            self.rules = self._load_rules()

    def decide(self, date, market_data, positions, cash, total_value):
        """Single entry point: produce a decision packet.

        See docs/superpowers/specs/2026-05-28-smart-invest-overhaul-design.md §6
        for the packet schema.
        """
        from datetime import datetime
        account_row = self.db.conn.execute(
            "SELECT name FROM accounts WHERE id = ?", (self.account_id,)
        ).fetchone()
        account_name = account_row["name"] if account_row else "unknown"

        regime = self._compute_market_regime(market_data)
        snapshot = self._compute_portfolio_snapshot(positions, market_data, cash, total_value)
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
        """Stub — filled in Task 4."""
        return {
            "label": "unknown", "hs300_5d_return": 0.0, "hs300_20d_return": 0.0,
            "position_cap": 0.85, "single_cap": 0.25, "stop_loss_threshold": -0.12,
        }

    def _compute_portfolio_snapshot(self, positions, market_data, cash, total_value):
        """Stub — filled in Task 5."""
        return {
            "total_value": total_value, "cash": cash,
            "cash_pct": cash / total_value if total_value else 0.0,
            "position_value": total_value - cash,
            "position_pct": (total_value - cash) / total_value if total_value else 0.0,
            "sectors": {}, "by_position": [],
        }

    def _evaluate_rules(self, date, market_data, positions, snapshot, regime, cash, total_value):
        """Stub — filled in Tasks 6-15. Returns (actions, blocked_actions, alerts)."""
        return [], [], []

    def _build_summary(self, actions):
        counts = {"buy": 0, "sell": 0, "hold": 0, "watch": 0}
        for a in actions:
            counts[a["action"]] = counts.get(a["action"], 0) + 1
        highest = None
        for a in actions:
            if a.get("confidence") is None:
                continue
            if highest is None or a["confidence"] > highest["confidence"]:
                highest = {"code": a["code"], "action": a["action"], "confidence": a["confidence"]}
        return {"action_count": counts, "highest_confidence_action": highest}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_decision_engine.py::test_decide_packet_has_all_top_level_keys -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/decision_engine.py tests/test_decision_engine.py
git commit -m "feat(engine): skeleton DecisionEngine.decide() with packet schema"
```

---

## Task 4: Implement `_compute_market_regime`

**Files:**
- Modify: `tests/test_decision_engine.py` (add tests)
- Modify: `scripts/decision_engine.py:_compute_market_regime`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_decision_engine.py`:

```python
def test_regime_bull(db, account_id, rules, empty_positions):
    md = pytest.make_market_data(hs300_20d_return=0.08)
    packet = _decide(db, account_id, rules, md, empty_positions)
    assert packet["market_regime"]["label"] == "牛市"
    assert packet["market_regime"]["position_cap"] == 0.95
    assert packet["market_regime"]["single_cap"] == 0.30
    assert packet["market_regime"]["stop_loss_threshold"] == -0.15


def test_regime_bear(db, account_id, rules, empty_positions):
    md = pytest.make_market_data(hs300_20d_return=-0.12)
    packet = _decide(db, account_id, rules, md, empty_positions)
    assert packet["market_regime"]["label"] == "熊市"
    assert packet["market_regime"]["position_cap"] == 0.60
    assert packet["market_regime"]["single_cap"] == 0.15
    assert packet["market_regime"]["stop_loss_threshold"] == -0.08


def test_regime_chop(db, account_id, rules, empty_positions):
    md = pytest.make_market_data(hs300_20d_return=0.02)
    packet = _decide(db, account_id, rules, md, empty_positions)
    assert packet["market_regime"]["label"] == "震荡市"
    assert packet["market_regime"]["position_cap"] == 0.85
    assert packet["market_regime"]["single_cap"] == 0.25
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_decision_engine.py -v -k regime`
Expected: 3 FAILs.

- [ ] **Step 3: Implement**

Replace `_compute_market_regime` in `scripts/decision_engine.py`:

```python
    def _compute_market_regime(self, market_data):
        hs300_20d = market_data.get("hs300_20d_return") or 0.0
        hs300_5d  = market_data.get("hs300_5d_return")  or 0.0
        if market_data.get("hs300_20d_return") is None:
            label, pcap, scap, sl = "unknown", 0.85, 0.25, -0.12
        elif hs300_20d > 0.05:
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
```

- [ ] **Step 4: Verify tests pass**

Run: `python3 -m pytest tests/test_decision_engine.py -v -k regime`
Expected: 3 PASSes.

- [ ] **Step 5: Commit**

```bash
git add scripts/decision_engine.py tests/test_decision_engine.py
git commit -m "feat(engine): classify market regime from HS300 20d return"
```

---

## Task 5: Implement `_compute_portfolio_snapshot`

**Files:**
- Modify: `tests/test_decision_engine.py` (add tests)
- Modify: `scripts/decision_engine.py:_compute_portfolio_snapshot`

- [ ] **Step 1: Write failing tests**

Append:

```python
def test_snapshot_empty(db, account_id, rules, market_data, empty_positions):
    packet = _decide(db, account_id, rules, market_data, empty_positions,
                     cash=10000.0, total_value=10000.0)
    snap = packet["portfolio_snapshot"]
    assert snap["total_value"] == 10000.0
    assert snap["cash"] == 10000.0
    assert snap["cash_pct"] == 1.0
    assert snap["position_value"] == 0.0
    assert snap["sectors"] == {}
    assert snap["by_position"] == []


def test_snapshot_single_position(db, account_id, rules, market_data, single_position):
    # 1000 shares * 2.30 (current_nav from market_data) = 2300
    packet = _decide(db, account_id, rules, market_data, single_position,
                     cash=7700.0, total_value=10000.0)
    snap = packet["portfolio_snapshot"]
    assert snap["total_value"] == 10000.0
    assert abs(snap["position_value"] - 2300.0) < 0.01
    assert abs(snap["cash_pct"] - 0.77) < 0.001
    assert snap["sectors"] == {"科技": pytest.approx(0.23, abs=0.001)}
    assert len(snap["by_position"]) == 1
    p = snap["by_position"][0]
    assert p["code"] == "512480"
    assert p["shares"] == 1000.0
    assert p["cost_nav"] == 2.34
    assert p["current_nav"] == 2.30
    assert abs(p["pct_of_total"] - 0.23) < 0.001
    assert abs(p["profit_pct"] - (-0.01709)) < 0.001
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_decision_engine.py -v -k snapshot`
Expected: 2 FAILs.

- [ ] **Step 3: Implement**

Replace `_compute_portfolio_snapshot`:

```python
    def _compute_portfolio_snapshot(self, positions, market_data, cash, total_value):
        funds = market_data.get("funds", {})
        by_pos = []
        sectors = {}
        position_value = 0.0
        for pos in positions:
            code = pos["code"]
            fund = funds.get(code, {})
            nav = fund.get("current_nav", pos["cost_nav"])
            value = pos["shares"] * nav
            position_value += value
            sector = pos.get("sector") or fund.get("sector") or "其他"
            sectors[sector] = sectors.get(sector, 0.0) + value
            profit_pct = (nav - pos["cost_nav"]) / pos["cost_nav"] if pos["cost_nav"] else 0.0
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
        sectors_pct = {k: (v / total_value if total_value else 0.0) for k, v in sectors.items()}
        return {
            "total_value": total_value,
            "cash": cash,
            "cash_pct": cash / total_value if total_value else 0.0,
            "position_value": position_value,
            "position_pct": position_value / total_value if total_value else 0.0,
            "sectors": sectors_pct,
            "by_position": by_pos,
        }
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/test_decision_engine.py -v -k snapshot`
Expected: 2 PASSes.

- [ ] **Step 5: Commit**

```bash
git add scripts/decision_engine.py tests/test_decision_engine.py
git commit -m "feat(engine): compute portfolio snapshot with sector breakdown"
```

---

## Task 6: Implement buy preconditions (5 checks) + low_buy rule

**Files:**
- Modify: `tests/test_decision_engine.py`
- Modify: `scripts/decision_engine.py:_evaluate_rules`

This task is bigger because the 5 buy preconditions all share infrastructure with low_buy and need to be implemented together for correct blocked_actions output.

- [ ] **Step 1: Write failing tests**

Append:

```python
def _find_blocked(packet, code, reason_id):
    return [b for b in packet["blocked_actions"]
            if b["code"] == code and b["blocked_by"] == reason_id]


def _find_action(packet, code, action):
    return [a for a in packet["actions"]
            if a["code"] == code and a["action"] == action]


def test_low_buy_triggers(db, account_id, rules, empty_positions):
    md = pytest.make_market_data()
    md["funds"]["512480"]["day_return"]    = -0.035
    md["funds"]["512480"]["fund_5d_return"] = -0.06
    packet = _decide(db, account_id, rules, md, empty_positions,
                     cash=2000.0, total_value=10000.0)
    actions = _find_action(packet, "512480", "buy")
    assert len(actions) == 1, packet["actions"]
    assert actions[0]["rule_id"] == "low_buy"
    assert actions[0]["suggested_amount"] > 0


def test_low_buy_boosted_amount(db, account_id, rules, empty_positions):
    md = pytest.make_market_data(hs300_5d_return=-0.025)
    md["funds"]["512480"]["day_return"]    = -0.055
    md["funds"]["512480"]["fund_5d_return"] = -0.09
    packet = _decide(db, account_id, rules, md, empty_positions,
                     cash=2000.0, total_value=10000.0)
    action = _find_action(packet, "512480", "buy")[0]
    # boosted = base × 2; base = 3% of total = 300
    assert abs(action["suggested_amount"] - 600.0) < 1.0


def test_low_buy_blocked_by_cash_reserve(db, account_id, rules, empty_positions):
    md = pytest.make_market_data()
    md["funds"]["512480"]["day_return"]    = -0.035
    md["funds"]["512480"]["fund_5d_return"] = -0.06
    # cash only 5% of total — below 10% minimum
    packet = _decide(db, account_id, rules, md, empty_positions,
                     cash=500.0, total_value=10000.0)
    assert _find_action(packet, "512480", "buy") == []
    assert _find_blocked(packet, "512480", "cash_reserve")


def test_buy_blocked_by_anti_chase(db, account_id, rules, empty_positions):
    md = pytest.make_market_data()
    md["funds"]["512480"]["day_return"]     = -0.035
    md["funds"]["512480"]["fund_5d_return"] = 0.12  # surged 12% — anti-chase
    packet = _decide(db, account_id, rules, md, empty_positions,
                     cash=5000.0, total_value=10000.0)
    assert _find_action(packet, "512480", "buy") == []
    assert _find_blocked(packet, "512480", "anti_chase")


def test_buy_blocked_by_sector_concentration(db, account_id, rules):
    # Already holding 半导体ETF at 48% of 10000 = 4800 value (cost 2.34, shares 2086.96)
    positions = [pytest.make_position(
        "512480", "半导体ETF国联安",
        shares=2086.96, cost_nav=2.34, sector="科技",
    )]
    md = pytest.make_market_data()
    md["funds"]["005825"] = {
        "name": "海富通电子传媒股票A", "current_nav": 1.50,
        "day_return": -0.04, "fund_5d_return": -0.06,
        "fund_20d_return": 0.0, "high_20d": 1.60, "sector": "科技",
    }
    packet = _decide(db, account_id, rules, md, positions,
                     cash=5200.0, total_value=10000.0)
    # 005825 is also 科技; combined would exceed 50%
    assert _find_blocked(packet, "005825", "sector_concentration")


def test_buy_blocked_by_single_position_cap(db, account_id, rules):
    # Holding 26% of total — over 25% single cap, can't add more
    positions = [pytest.make_position(
        "512480", "半导体ETF国联安",
        shares=1130.43, cost_nav=2.30, sector="科技",
    )]
    md = pytest.make_market_data()
    md["funds"]["512480"]["day_return"]    = -0.035
    md["funds"]["512480"]["fund_5d_return"] = -0.06
    packet = _decide(db, account_id, rules, md, positions,
                     cash=7400.0, total_value=10000.0)
    assert _find_blocked(packet, "512480", "single_position")
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_decision_engine.py -v -k "low_buy or blocked"`
Expected: 6 FAILs (all return empty lists currently).

- [ ] **Step 3: Implement**

Add private helpers to `DecisionEngine` (place above `_evaluate_rules`):

```python
    # ---------- precondition checks ----------

    def _check_cash_reserve(self, cash_pct):
        return cash_pct >= 0.10, {"id": "cash_reserve", "actual": cash_pct, "threshold_min": 0.10}

    def _check_single_position(self, code, snapshot, target_amount, total_value):
        existing = next((p for p in snapshot["by_position"] if p["code"] == code), None)
        existing_pct = existing["pct_of_total"] if existing else 0.0
        projected = existing_pct + (target_amount / total_value if total_value else 0.0)
        return projected <= 0.25, {
            "id": "single_position", "actual": existing_pct,
            "projected": projected, "threshold_max": 0.25,
        }

    def _check_sector_concentration(self, sector, snapshot, target_amount, total_value):
        if not sector or sector == "其他":
            return True, {"id": "sector_concentration", "sector": sector, "skipped": True}
        cap_map = {"科技": 0.50, "消费": 0.30, "新能源": 0.30, "金融": 0.20,
                   "资源": 0.20, "宽基": 0.30, "海外": 0.40, "其他": 0.30}
        cap = cap_map.get(sector, 0.30)
        current = snapshot["sectors"].get(sector, 0.0)
        projected = current + (target_amount / total_value if total_value else 0.0)
        return projected <= cap, {
            "id": "sector_concentration", "sector": sector,
            "actual": current, "projected": projected, "threshold_max": cap,
        }

    def _check_anti_chase(self, fund):
        r5 = fund.get("fund_5d_return", 0.0)
        return r5 <= 0.10, {"id": "anti_chase", "actual": r5, "threshold_max": 0.10}

    def _check_market_allows_buy(self, regime, has_existing_position):
        label = regime["label"]
        if label == "熊市" and not has_existing_position:
            return False, {"id": "bear_market_new_position", "label": label}
        if label == "unknown":
            return False, {"id": "market_regime_unknown", "label": label}
        return True, {"id": "market_regime", "label": label}

    # ---------- low_buy rule ----------

    def _try_low_buy(self, code, fund, snapshot, regime, cash, total_value):
        """Return (action_dict, blocked_dict) — exactly one is None."""
        day_r = fund.get("day_return", 0.0)
        if day_r > -0.03:
            return None, None  # not a low-buy candidate, don't emit anything

        # Base amount: 3% of total
        base_amount = total_value * 0.03
        # Boost conditions (each multiplies by 2; cap at 2x total)
        boost = 1.0
        if day_r <= -0.05 or fund.get("fund_5d_return", 0.0) <= -0.08:
            boost = 2.0
        if (regime.get("hs300_5d_return") or 0.0) <= -0.02:
            boost = 2.0
        target_amount = base_amount * boost
        # bear market halves the size for existing-position low_buy
        existing = next((p for p in snapshot["by_position"] if p["code"] == code), None)
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
        if checks_failed:
            primary = checks_failed[0]
            return None, {
                "code": code, "name": fund.get("name", ""),
                "attempted_action": "buy",
                "blocked_by": primary["id"],
                "reason_zh": self._block_reason_zh(primary, context),
            }

        return {
            "code": code, "name": fund.get("name", ""),
            "action": "buy", "rule_id": "low_buy", "rule_label": "低吸",
            "confidence": None,  # filled by Task 10
            "suggested_amount": round(target_amount, 2),
            "suggested_shares": None,
            "context": context,
            "checks_passed": checks_passed,
            "checks_failed": [],
            "reason_zh": (
                f"符合低吸规则：当日跌 {abs(day_r)*100:.1f}%、近 5 天跌 "
                f"{abs(fund.get('fund_5d_return',0.0))*100:.1f}%；"
                f"大盘 {regime['label']}；现金 {snapshot['cash_pct']*100:.0f}% "
                f"在阈值内。"
            ),
        }, None

    def _block_reason_zh(self, info, context):
        m = {
            "cash_reserve": f"现金占比 {info.get('actual',0)*100:.1f}% < 10% 最低储备线。",
            "single_position": f"单只仓位将达 {info.get('projected',0)*100:.1f}% > 25% 上限。",
            "sector_concentration": (
                f"{info.get('sector','')}赛道将达 {info.get('projected',0)*100:.1f}% > "
                f"{info.get('threshold_max',0)*100:.0f}% 上限。"
            ),
            "anti_chase": f"该基金近 5 天涨 {info.get('actual',0)*100:.1f}% > 10%，禁止追高。",
            "bear_market_new_position": "大盘处于熊市，禁止新建仓。",
            "market_regime_unknown": "大盘数据缺失，谨慎起见暂不建仓。",
        }
        return m.get(info["id"], f"未通过检查：{info['id']}")
```

Replace `_evaluate_rules` body:

```python
    def _evaluate_rules(self, date, market_data, positions, snapshot, regime, cash, total_value):
        actions, blocked, alerts = [], [], []
        for code, fund in market_data.get("funds", {}).items():
            action, block = self._try_low_buy(code, fund, snapshot, regime, cash, total_value)
            if action:
                actions.append(action)
            if block:
                blocked.append(block)
        return actions, blocked, alerts
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/test_decision_engine.py -v -k "low_buy or blocked"`
Expected: 6 PASSes.

Run also: `python3 -m pytest tests/test_decision_engine.py -v`
Expected: all 11 PASSes (previous tests still pass).

- [ ] **Step 5: Commit**

```bash
git add scripts/decision_engine.py tests/test_decision_engine.py
git commit -m "feat(engine): implement buy preconditions + low_buy rule"
```

---

## Task 7: Implement stop-loss rules

**Files:**
- Modify: `tests/test_decision_engine.py`
- Modify: `scripts/decision_engine.py`

- [ ] **Step 1: Write failing tests**

Append:

```python
def test_emergency_stop_loss_day(db, account_id, rules):
    positions = [pytest.make_position("512480", "半导体ETF国联安",
                                      shares=1000.0, cost_nav=2.30, sector="科技")]
    md = pytest.make_market_data()
    md["funds"]["512480"]["day_return"]   = -0.075
    md["funds"]["512480"]["current_nav"]  = 2.30
    packet = _decide(db, account_id, rules, md, positions,
                     cash=5000.0, total_value=7300.0)
    sells = _find_action(packet, "512480", "sell")
    assert len(sells) == 1
    assert sells[0]["rule_id"] == "emergency_stop_loss"
    # half of 1000 shares
    assert abs(sells[0]["suggested_shares"] - 500.0) < 0.01


def test_absolute_stop_loss(db, account_id, rules):
    # cost 3.0, current 2.30 → -23.3% loss
    positions = [pytest.make_position("512480", "半导体ETF国联安",
                                      shares=1000.0, cost_nav=3.00, sector="科技")]
    md = pytest.make_market_data()
    packet = _decide(db, account_id, rules, md, positions,
                     cash=5000.0, total_value=7300.0)
    sells = _find_action(packet, "512480", "sell")
    assert len(sells) == 1
    assert sells[0]["rule_id"] == "absolute_stop_loss"
    # full clear
    assert abs(sells[0]["suggested_shares"] - 1000.0) < 0.01


def test_time_based_stop_loss_short_hold(db, account_id, rules):
    # 10 day hold, cost 2.50, current 2.30 → -8% loss
    positions = [pytest.make_position("512480", "半导体ETF国联安",
                                      shares=1000.0, cost_nav=2.50,
                                      sector="科技", hold_days=10)]
    md = pytest.make_market_data()
    packet = _decide(db, account_id, rules, md, positions,
                     cash=5000.0, total_value=7300.0)
    sells = _find_action(packet, "512480", "sell")
    assert len(sells) == 1
    assert sells[0]["rule_id"] == "time_based_stop_loss"
    assert abs(sells[0]["suggested_shares"] - 500.0) < 0.01  # 50%
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_decision_engine.py -v -k stop_loss`
Expected: 3 FAILs.

- [ ] **Step 3: Implement**

Add private helper:

```python
    def _try_stop_loss(self, code, fund, position, regime):
        """Return action dict or None."""
        nav = fund.get("current_nav", position["cost_nav"])
        profit_pct = (nav - position["cost_nav"]) / position["cost_nav"]
        day_r = fund.get("day_return", 0.0)
        three_d = fund.get("fund_3d_return", 0.0)
        hold_days = position.get("hold_days", 0)

        def _sell(rule_id, label, fraction, reason):
            return {
                "code": code, "name": position.get("name") or fund.get("name", ""),
                "action": "sell", "rule_id": rule_id, "rule_label": label,
                "confidence": None,  # Task 10
                "suggested_amount": round(position["shares"] * fraction * nav, 2),
                "suggested_shares": round(position["shares"] * fraction, 4),
                "context": {"profit_pct": profit_pct, "day_return": day_r,
                            "hold_days": hold_days},
                "checks_passed": [],
                "checks_failed": [],
                "reason_zh": reason,
            }

        # Priority 1: emergency
        if day_r <= -0.07:
            return _sell("emergency_stop_loss", "紧急止损", 0.5,
                         f"单日跌 {abs(day_r)*100:.1f}% > 7%，立即减仓 50%。")
        if three_d <= -0.10:
            return _sell("emergency_stop_loss", "紧急止损", 0.5,
                         f"近 3 天累跌 {abs(three_d)*100:.1f}% > 10%，立即减仓 50%。")
        # Priority 2: absolute (regime threshold or hard -20%)
        if profit_pct <= -0.20:
            return _sell("absolute_stop_loss", "绝对止损", 1.0,
                         f"亏损 {abs(profit_pct)*100:.1f}% > 20%，清仓。")
        # Priority 3: time-based
        if hold_days < 30 and profit_pct <= -0.08:
            return _sell("time_based_stop_loss", "短期止损", 0.5,
                         f"持有 {hold_days} 天亏 {abs(profit_pct)*100:.1f}% > 8%，减仓 50%。")
        if 30 <= hold_days <= 90 and profit_pct <= -0.12:
            return _sell("time_based_stop_loss", "中期止损", 0.5,
                         f"持有 {hold_days} 天亏 {abs(profit_pct)*100:.1f}% > 12%，减仓 50%。")
        if hold_days > 90 and profit_pct <= -0.15:
            return _sell("time_based_stop_loss", "长期止损", 0.5,
                         f"持有 {hold_days} 天亏 {abs(profit_pct)*100:.1f}% > 15%，减仓 50%。")
        return None
```

Modify `_evaluate_rules` to call stop-loss before low-buy and dedupe (sell wins):

```python
    def _evaluate_rules(self, date, market_data, positions, snapshot, regime, cash, total_value):
        actions, blocked, alerts = [], [], []
        funds = market_data.get("funds", {})

        # Pass 1: stop-loss on each existing position
        positions_with_sell = set()
        for pos in positions:
            code = pos["code"]
            fund = funds.get(code)
            if not fund:
                continue
            sell = self._try_stop_loss(code, fund, pos, regime)
            if sell:
                actions.append(sell)
                positions_with_sell.add(code)

        # Pass 2: low_buy on each fund (skip if sell already triggered)
        for code, fund in funds.items():
            if code in positions_with_sell:
                continue
            action, block = self._try_low_buy(code, fund, snapshot, regime, cash, total_value)
            if action:
                actions.append(action)
            if block:
                blocked.append(block)

        return actions, blocked, alerts
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/test_decision_engine.py -v`
Expected: 14 PASSes.

- [ ] **Step 5: Commit**

```bash
git add scripts/decision_engine.py tests/test_decision_engine.py
git commit -m "feat(engine): stop-loss rules (emergency / absolute / time-based)"
```

---

## Task 8: Implement take-profit tiers

**Files:**
- Modify: `tests/test_decision_engine.py`
- Modify: `scripts/decision_engine.py`

- [ ] **Step 1: Write failing tests**

Append:

```python
def test_take_profit_tier_20(db, account_id, rules):
    # cost 1.90, current 2.30 → +21% profit, no prior take-profit in trades
    positions = [pytest.make_position("512480", "半导体ETF国联安",
                                      shares=1000.0, cost_nav=1.90, sector="科技")]
    packet = _decide(db, account_id, rules, pytest.make_market_data(), positions,
                     cash=5000.0, total_value=7300.0)
    sells = _find_action(packet, "512480", "sell")
    assert len(sells) == 1
    assert sells[0]["rule_id"] == "take_profit_tier_20"
    assert abs(sells[0]["suggested_shares"] - 250.0) < 0.01  # 25%


def test_take_profit_clearout(db, account_id, rules):
    # cost 1.50, current 2.30 → +53% profit → full clear
    positions = [pytest.make_position("512480", "半导体ETF国联安",
                                      shares=1000.0, cost_nav=1.50, sector="科技")]
    packet = _decide(db, account_id, rules, pytest.make_market_data(), positions,
                     cash=5000.0, total_value=7300.0)
    sells = _find_action(packet, "512480", "sell")
    assert len(sells) == 1
    assert sells[0]["rule_id"] == "take_profit_clearout"
    assert abs(sells[0]["suggested_shares"] - 1000.0) < 0.01
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_decision_engine.py -v -k take_profit`
Expected: 2 FAILs.

- [ ] **Step 3: Implement**

Add helper to `DecisionEngine`:

```python
    def _try_take_profit(self, code, fund, position):
        nav = fund.get("current_nav", position["cost_nav"])
        profit_pct = (nav - position["cost_nav"]) / position["cost_nav"]
        if profit_pct < 0.20:
            return None

        def _sell(rule_id, label, fraction, reason):
            return {
                "code": code, "name": position.get("name") or fund.get("name", ""),
                "action": "sell", "rule_id": rule_id, "rule_label": label,
                "confidence": None,
                "suggested_amount": round(position["shares"] * fraction * nav, 2),
                "suggested_shares": round(position["shares"] * fraction, 4),
                "context": {"profit_pct": profit_pct},
                "checks_passed": [],
                "checks_failed": [],
                "reason_zh": reason,
            }

        # Highest tier first
        if profit_pct >= 0.50:
            return _sell("take_profit_clearout", "止盈清仓", 1.0,
                         f"盈利 {profit_pct*100:.1f}% ≥ 50%，清仓锁利。")
        if profit_pct >= 0.40:
            return _sell("take_profit_tier_40", "止盈第三档", 0.25,
                         f"盈利 {profit_pct*100:.1f}% ≥ 40%，再减 25%。")
        if profit_pct >= 0.30:
            return _sell("take_profit_tier_30", "止盈第二档", 0.25,
                         f"盈利 {profit_pct*100:.1f}% ≥ 30%，再减 25%。")
        return _sell("take_profit_tier_20", "止盈首档", 0.25,
                     f"盈利 {profit_pct*100:.1f}% ≥ 20%，减仓 25%。")
```

Modify `_evaluate_rules` Pass 1 to try stop-loss → take-profit in that order:

```python
        # Pass 1: stop-loss (priority) then take-profit on each existing position
        positions_with_sell = set()
        for pos in positions:
            code = pos["code"]
            fund = funds.get(code)
            if not fund:
                continue
            sell = self._try_stop_loss(code, fund, pos, regime)
            if not sell:
                sell = self._try_take_profit(code, fund, pos)
            if sell:
                actions.append(sell)
                positions_with_sell.add(code)
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/test_decision_engine.py -v`
Expected: 16 PASSes.

- [ ] **Step 5: Commit**

```bash
git add scripts/decision_engine.py tests/test_decision_engine.py
git commit -m "feat(engine): take-profit tiers (+20/30/40/50%)"
```

---

## Task 9: Drawdown protection + bear-market handling alerts

**Files:**
- Modify: `tests/test_decision_engine.py`
- Modify: `scripts/decision_engine.py`

- [ ] **Step 1: Write failing tests**

Append:

```python
def test_drawdown_protection_downgrades_buys(db, account_id, rules, empty_positions):
    md = pytest.make_market_data()
    md["portfolio_peak_value"] = 11200.0  # current 10000 → 10.7% drawdown
    md["funds"]["512480"]["day_return"]    = -0.035
    md["funds"]["512480"]["fund_5d_return"] = -0.06
    packet = _decide(db, account_id, rules, md, empty_positions,
                     cash=5000.0, total_value=10000.0)
    # low_buy would normally trigger, but drawdown should downgrade to "watch"
    buys = _find_action(packet, "512480", "buy")
    assert buys == []
    watches = _find_action(packet, "512480", "watch")
    assert len(watches) == 1
    assert watches[0]["rule_id"] == "low_buy_deferred_drawdown"
    assert any(a["id"] == "drawdown_protection" for a in packet["alerts"])


def test_bear_market_blocks_new_position(db, account_id, rules, empty_positions):
    md = pytest.make_market_data(hs300_20d_return=-0.12)
    md["funds"]["512480"]["day_return"]    = -0.035
    md["funds"]["512480"]["fund_5d_return"] = -0.06
    packet = _decide(db, account_id, rules, md, empty_positions,
                     cash=5000.0, total_value=10000.0)
    assert _find_action(packet, "512480", "buy") == []
    assert _find_blocked(packet, "512480", "bear_market_new_position")


def test_bear_market_allows_low_buy_existing(db, account_id, rules):
    positions = [pytest.make_position("512480", "半导体ETF国联安",
                                      shares=500.0, cost_nav=2.30, sector="科技")]
    md = pytest.make_market_data(hs300_20d_return=-0.12)
    md["funds"]["512480"]["day_return"]    = -0.035
    md["funds"]["512480"]["fund_5d_return"] = -0.06
    packet = _decide(db, account_id, rules, md, positions,
                     cash=5000.0, total_value=6150.0)
    # buy allowed but halved (1.5% × baseAmount)
    buys = _find_action(packet, "512480", "buy")
    assert len(buys) == 1
    base_then_halved = 6150.0 * 0.03 * 1.0 * 0.5  # no boost in this scenario
    assert abs(buys[0]["suggested_amount"] - round(base_then_halved, 2)) < 1.0
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_decision_engine.py -v -k "drawdown or bear_market"`
Expected: 3 FAILs.

- [ ] **Step 3: Implement**

Modify `_evaluate_rules` to compute drawdown alert and downgrade buys:

```python
    def _evaluate_rules(self, date, market_data, positions, snapshot, regime, cash, total_value):
        actions, blocked, alerts = [], [], []
        funds = market_data.get("funds", {})

        # Drawdown alert (account-level)
        peak = market_data.get("portfolio_peak_value")
        drawdown = (peak - total_value) / peak if peak and peak > total_value else 0.0
        in_drawdown_protection = drawdown >= 0.10
        if in_drawdown_protection:
            alerts.append({
                "severity": "warn", "id": "drawdown_protection",
                "drawdown": round(drawdown, 4),
                "reason_zh": f"组合从峰值回撤 {drawdown*100:.1f}% ≥ 10%，所有买入降级为观察。",
            })

        # Pass 1: sells (stop-loss > take-profit)
        positions_with_sell = set()
        for pos in positions:
            code = pos["code"]
            fund = funds.get(code)
            if not fund:
                continue
            sell = self._try_stop_loss(code, fund, pos, regime)
            if not sell:
                sell = self._try_take_profit(code, fund, pos)
            if sell:
                actions.append(sell)
                positions_with_sell.add(code)

        # Pass 2: low_buy
        for code, fund in funds.items():
            if code in positions_with_sell:
                continue
            action, block = self._try_low_buy(code, fund, snapshot, regime, cash, total_value)
            if action:
                if in_drawdown_protection:
                    actions.append({
                        **action,
                        "action": "watch",
                        "rule_id": "low_buy_deferred_drawdown",
                        "rule_label": "低吸暂缓（回撤保护）",
                        "suggested_amount": 0.0,
                        "reason_zh": action["reason_zh"] + " 但组合回撤≥10%，暂缓买入。",
                    })
                else:
                    actions.append(action)
            if block:
                blocked.append(block)

        return actions, blocked, alerts
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/test_decision_engine.py -v`
Expected: 19 PASSes.

- [ ] **Step 5: Commit**

```bash
git add scripts/decision_engine.py tests/test_decision_engine.py
git commit -m "feat(engine): drawdown protection + bear-market rules"
```

---

## Task 10: Confidence scoring

**Files:**
- Modify: `tests/test_decision_engine.py`
- Modify: `scripts/decision_engine.py`

- [ ] **Step 1: Write failing tests**

Append:

```python
def test_confidence_for_low_buy(db, account_id, rules, empty_positions):
    md = pytest.make_market_data()
    md["funds"]["512480"]["day_return"]    = -0.055  # boost
    md["funds"]["512480"]["fund_5d_return"] = -0.09  # boost
    packet = _decide(db, account_id, rules, md, empty_positions,
                     cash=5000.0, total_value=10000.0)
    buy = _find_action(packet, "512480", "buy")[0]
    # 0.5 base + 0.15 oversold + 0 (5d hs300 = 0) + 0.10 new position + 0.05 light = 0.80
    assert 0.65 <= buy["confidence"] <= 0.85


def test_confidence_for_take_profit_high(db, account_id, rules):
    positions = [pytest.make_position("512480", "半导体ETF国联安",
                                      shares=1000.0, cost_nav=1.50, sector="科技")]
    packet = _decide(db, account_id, rules, pytest.make_market_data(), positions,
                     cash=5000.0, total_value=7300.0)
    sell = _find_action(packet, "512480", "sell")[0]
    # profit > 40% → +0.20; clearout
    assert sell["confidence"] >= 0.75


def test_confidence_for_emergency_stop_loss(db, account_id, rules):
    positions = [pytest.make_position("512480", "半导体ETF国联安",
                                      shares=1000.0, cost_nav=2.30, sector="科技")]
    md = pytest.make_market_data()
    md["funds"]["512480"]["day_return"]   = -0.075
    packet = _decide(db, account_id, rules, md, positions,
                     cash=5000.0, total_value=7300.0)
    sell = _find_action(packet, "512480", "sell")[0]
    # base 0.6, no other bonuses for emergency stop (small profit_pct)
    assert sell["confidence"] >= 0.55
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_decision_engine.py -v -k confidence`
Expected: 3 FAILs (`confidence` is None).

- [ ] **Step 3: Implement**

Add to `DecisionEngine`:

```python
    def _score_confidence(self, action, position=None):
        if action["action"] == "buy":
            base = 0.5
            ctx = action.get("context", {})
            if ctx.get("fund_5d_return", 0.0) <= -0.05:
                base += 0.15
            if ctx.get("hs300_5d_return", 0.0) >= 0.03:
                base += 0.10
            # new position bonus only when not already held
            if position is None:
                base += 0.10
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
```

In `_evaluate_rules`, after building each action, call `_score_confidence` and overwrite the `confidence` field. Replace the relevant sections:

Where `actions.append(sell)` happens in Pass 1, do this instead:
```python
                sell["confidence"] = self._score_confidence(sell, position=pos)
                actions.append(sell)
                positions_with_sell.add(code)
```

Where Pass 2 appends:
```python
        for code, fund in funds.items():
            if code in positions_with_sell:
                continue
            action, block = self._try_low_buy(code, fund, snapshot, regime, cash, total_value)
            if action:
                existing = next((p for p in positions if p["code"] == code), None)
                action["confidence"] = self._score_confidence(action, position=existing)
                if in_drawdown_protection:
                    actions.append({
                        **action, "action": "watch",
                        "rule_id": "low_buy_deferred_drawdown",
                        "rule_label": "低吸暂缓（回撤保护）",
                        "suggested_amount": 0.0, "confidence": None,
                        "reason_zh": action["reason_zh"] + " 但组合回撤≥10%，暂缓买入。",
                    })
                else:
                    actions.append(action)
            if block:
                blocked.append(block)
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/test_decision_engine.py -v`
Expected: 22 PASSes.

- [ ] **Step 5: Commit**

```bash
git add scripts/decision_engine.py tests/test_decision_engine.py
git commit -m "feat(engine): confidence scoring for buy/sell actions"
```

---

## Task 11: Data-missing handling

**Files:**
- Modify: `tests/test_decision_engine.py`
- Modify: `scripts/decision_engine.py`

- [ ] **Step 1: Write failing test**

Append:

```python
def test_data_missing_emits_alert(db, account_id, rules):
    positions = [pytest.make_position("999999", "无数据基金",
                                      shares=100.0, cost_nav=1.00, sector="其他")]
    md = pytest.make_market_data()
    # 999999 deliberately not in md["funds"]
    packet = _decide(db, account_id, rules, md, positions,
                     cash=5000.0, total_value=5100.0)
    assert _find_action(packet, "999999", "sell") == []
    assert _find_action(packet, "999999", "buy")  == []
    assert any(a["id"] == "data_missing" and a.get("code") == "999999"
               for a in packet["alerts"])


def test_unknown_regime_downgrades_buys(db, account_id, rules, empty_positions):
    md = pytest.make_market_data(hs300_20d_return=None)
    md["funds"]["512480"]["day_return"]    = -0.035
    md["funds"]["512480"]["fund_5d_return"] = -0.06
    packet = _decide(db, account_id, rules, md, empty_positions,
                     cash=5000.0, total_value=10000.0)
    assert _find_action(packet, "512480", "buy") == []
    assert _find_blocked(packet, "512480", "market_regime_unknown")
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_decision_engine.py -v -k "data_missing or unknown_regime"`
Expected: 2 FAILs.

- [ ] **Step 3: Implement**

Modify `_evaluate_rules` Pass 1 to emit `data_missing` alert:

```python
        # Pass 1: stop-loss / take-profit on existing positions
        positions_with_sell = set()
        for pos in positions:
            code = pos["code"]
            fund = funds.get(code)
            if not fund:
                alerts.append({
                    "severity": "warn", "id": "data_missing", "code": code,
                    "reason_zh": f"无法获取基金 {code} 的实时数据，已跳过决策。",
                })
                continue
            ...
```

(The `market_regime_unknown` blocking case already works thanks to `_check_market_allows_buy` — verify by reading the existing code.)

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/test_decision_engine.py -v`
Expected: 24 PASSes.

- [ ] **Step 5: Commit**

```bash
git add scripts/decision_engine.py tests/test_decision_engine.py
git commit -m "feat(engine): graceful degradation for missing market data"
```

---

## Task 12: Add `fetch_fund.gather_market_snapshot` function + CLI subcommand

**Files:**
- Modify: `scripts/fetch_fund.py`

This task does NOT have unit tests because it hits live endpoints. Verification is by running the CLI manually.

- [ ] **Step 1: Read `scripts/fetch_fund.py` to find the existing structure**

Run: `grep -n "^def cmd_\|def main\|argparse" /Users/tght/develop/project/2026/smart-invest/scripts/fetch_fund.py | head -20`

- [ ] **Step 2: Add `gather_market_snapshot` function**

Insert before `def main()`:

```python
def gather_market_snapshot(account_name="主线", date=None):
    """Aggregate the inputs DecisionEngine.decide() needs.

    Returns dict with:
      - hs300_5d_return, hs300_20d_return  (None if fetch fails)
      - regime_hint (always None — engine computes it)
      - funds: {code: {name, current_nav, day_return, fund_5d_return,
                       fund_20d_return, fund_3d_return, high_20d, sector}}
      - portfolio_peak_value (None for now — needs daily_snapshots query)
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from db import Database

    db = Database()
    try:
        # 1. HS300 returns (5d, 20d)
        hs300_5d, hs300_20d = _hs300_returns()

        # 2. Get positions to know which funds to fetch
        cur = db.conn.cursor()
        row = cur.execute("SELECT id FROM accounts WHERE name = ?",
                          (account_name,)).fetchone()
        if not row:
            return {"error": f"account '{account_name}' not found"}
        account_id = row["id"]

        positions = cur.execute(
            "SELECT code, name, sector FROM positions WHERE account_id = ?",
            (account_id,)
        ).fetchall()

        funds = {}
        for p in positions:
            funds[p["code"]] = _fund_snapshot(p["code"], p["name"], p["sector"])

        # 3. Watchlist funds: use DEFAULT_FUNDS from simulate.py if available,
        #    plus current portfolio
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from simulate import DEFAULT_FUNDS
            for code, name in DEFAULT_FUNDS.items():
                if code not in funds:
                    funds[code] = _fund_snapshot(code, name, None)
        except Exception:
            pass

        return {
            "hs300_5d_return": hs300_5d,
            "hs300_20d_return": hs300_20d,
            "regime_hint": None,
            "funds": funds,
            "portfolio_peak_value": None,  # Phase 2: query daily_snapshots
        }
    finally:
        db.close()


def _hs300_returns():
    """Return (5d_return, 20d_return) for HS300. None on fetch failure."""
    try:
        # Use existing index-kline plumbing
        from datetime import datetime
        # secid for HS300 is 1.000300
        url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
               "?secid=1.000300&fields1=f1,f2,f3,f4&fields2=f51,f53"
               "&klt=101&fqt=1&end=20500101&lmt=25")
        raw = _get(url)
        data = json.loads(raw)
        klines = data.get("data", {}).get("klines", [])
        if len(klines) < 21:
            return None, None
        closes = [float(k.split(",")[1]) for k in klines]
        latest = closes[-1]
        five_ago = closes[-6]
        twenty_ago = closes[-21]
        return ((latest - five_ago) / five_ago, (latest - twenty_ago) / twenty_ago)
    except Exception:
        return None, None


def _fund_snapshot(code, name, sector):
    """Fetch latest snapshot for a single fund. Returns dict with safe defaults."""
    try:
        # Use the same approach as cmd_estimate/cmd_nav already in this file
        url = f"https://fundgz.1234567.com.cn/js/{code}.js"
        raw = _get(url)
        match = re.search(r"jsonpgz\((\{.*?\})\)", raw)
        if not match:
            return None
        gz = json.loads(match.group(1))
        current_nav = float(gz.get("gsz") or gz.get("dwjz") or 0.0)
        day_return = float(gz.get("gszzl", "0.0")) / 100.0

        # Historical NAVs for 5d/20d returns + high_20d
        nav_url = (f"https://api.fund.eastmoney.com/f10/lsjz?fundCode={code}"
                   f"&pageIndex=1&pageSize=25")
        nav_raw = _get(nav_url, headers={"Referer": "https://fundf10.eastmoney.com/"})
        nav_data = json.loads(nav_raw)
        items = nav_data.get("Data", {}).get("LSJZList", [])
        navs = [float(i["DWJZ"]) for i in items if i.get("DWJZ")]
        fund_5d  = (current_nav - navs[5])  / navs[5]  if len(navs) > 5  else 0.0
        fund_3d  = (current_nav - navs[3])  / navs[3]  if len(navs) > 3  else 0.0
        fund_20d = (current_nav - navs[20]) / navs[20] if len(navs) > 20 else 0.0
        high_20d = max(navs[:20]) if len(navs) >= 20 else current_nav

        if sector is None:
            sector = _infer_sector(name)

        return {
            "name": name,
            "current_nav": current_nav,
            "day_return": day_return,
            "fund_3d_return": fund_3d,
            "fund_5d_return": fund_5d,
            "fund_20d_return": fund_20d,
            "high_20d": high_20d,
            "sector": sector,
        }
    except Exception:
        return None  # engine will emit data_missing alert


SECTOR_KEYWORDS = {
    "科技": ["半导体", "芯片", "AI", "人工智能", "信息科技", "数字经济", "电子", "传媒"],
    "消费": ["白酒", "食品", "医药", "消费"],
    "新能源": ["光伏", "锂电", "新能源"],
    "金融": ["银行", "券商", "保险"],
    "资源": ["黄金", "有色", "煤炭", "石油"],
    "宽基": ["沪深300", "中证500", "创业板", "上证50"],
    "海外": ["纳斯达克", "标普", "QDII", "港股"],
}


def _infer_sector(name):
    if not name:
        return "其他"
    for sector, kws in SECTOR_KEYWORDS.items():
        for kw in kws:
            if kw in name:
                return sector
    return "其他"


def cmd_market_snapshot(args):
    snap = gather_market_snapshot(account_name=args.account, date=args.date)
    print(json.dumps(snap, ensure_ascii=False, indent=2))
    return 0
```

In `main()`, register the new subcommand alongside the others:

```python
    p = sub.add_parser("market-snapshot",
                       help="Aggregate market+portfolio data for decision engine")
    p.add_argument("--account", default="主线")
    p.add_argument("--date", default=None)
    p.set_defaults(func=cmd_market_snapshot)
```

- [ ] **Step 3: Sanity-check the CLI**

Run: `python3 scripts/fetch_fund.py market-snapshot --account 主线`

Expected: prints JSON with `hs300_5d_return`, `hs300_20d_return`, `funds` keys. May print null values if no positions or network fails — that's fine.

If `主线` account doesn't exist locally, run with `--account test` after creating one, or skip this verification (engine tests don't depend on it).

- [ ] **Step 4: Commit**

```bash
git add scripts/fetch_fund.py
git commit -m "feat(fetch): gather_market_snapshot + market-snapshot CLI"
```

---

## Task 13: Create `scripts/decide.py` thin CLI

**Files:**
- Create: `scripts/decide.py`

- [ ] **Step 1: Write the file**

```python
#!/usr/bin/env python3
"""Decision CLI — single entry point for live decisions.

Usage:
  python3 scripts/decide.py run --account 主线 [--date YYYY-MM-DD] [--format json|md]

Pipeline:
  fetch_fund.gather_market_snapshot(account)
  → DecisionEngine.decide(...)
  → JSON (default) or Markdown summary on stdout

Exit codes:
  0 success
  2 missing data / unresolvable preconditions
  3 unexpected error (with stderr traceback)
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
    p = packet
    lines.append(f"# 决策包 — 账户 {p['account']} — {p['date']}")
    lines.append("")
    r = p["market_regime"]
    lines.append(f"**市场环境**: {r['label']} "
                 f"(HS300 5d {r['hs300_5d_return']*100:+.1f}%, "
                 f"20d {r['hs300_20d_return']*100:+.1f}%)")
    s = p["portfolio_snapshot"]
    lines.append(f"**总资产**: ¥{s['total_value']:,.2f}  "
                 f"**现金**: ¥{s['cash']:,.2f} ({s['cash_pct']*100:.1f}%)")
    if s["sectors"]:
        sec = " / ".join(f"{k} {v*100:.0f}%" for k, v in s["sectors"].items())
        lines.append(f"**赛道分布**: {sec}")
    lines.append("")

    if p["actions"]:
        lines.append("## 建议操作")
        for a in p["actions"]:
            conf = f" 置信度 {a['confidence']:.2f}" if a.get("confidence") else ""
            if a["action"] == "buy":
                lines.append(f"- 🟢 **{a['action'].upper()}** "
                             f"{a['name']} ({a['code']}) "
                             f"¥{a['suggested_amount']:.0f}  "
                             f"[{a['rule_label']}]{conf}")
            elif a["action"] == "sell":
                lines.append(f"- 🔴 **{a['action'].upper()}** "
                             f"{a['name']} ({a['code']}) "
                             f"{a['suggested_shares']:.2f} 份  "
                             f"[{a['rule_label']}]{conf}")
            elif a["action"] == "watch":
                lines.append(f"- 🟡 **观察** {a['name']} ({a['code']}) "
                             f"[{a['rule_label']}]")
            lines.append(f"  > {a['reason_zh']}")
        lines.append("")

    if p["blocked_actions"]:
        lines.append("## 已拦截的买入意图")
        for b in p["blocked_actions"]:
            lines.append(f"- ❌ {b['name']} ({b['code']}) — {b['reason_zh']}")
        lines.append("")

    if p["alerts"]:
        lines.append("## 预警")
        for a in p["alerts"]:
            lines.append(f"- ⚠️ [{a['severity']}] {a['reason_zh']}")
        lines.append("")

    sm = p["summary"]
    lines.append(f"---\n**汇总**: {sm['action_count']}  "
                 f"最高置信度: {sm.get('highest_confidence_action') or '—'}")
    return "\n".join(lines)


def cmd_run(args):
    try:
        snap = fetch_fund.gather_market_snapshot(account_name=args.account, date=args.date)
        if "error" in snap:
            print(json.dumps({"error": snap["error"]}, ensure_ascii=False), file=sys.stderr)
            return 2

        db = Database()
        try:
            row = db.conn.execute(
                "SELECT id FROM accounts WHERE name = ?", (args.account,)
            ).fetchone()
            if not row:
                print(json.dumps({"error": f"account '{args.account}' not found"},
                                 ensure_ascii=False), file=sys.stderr)
                return 2
            account_id = row["id"]

            positions = []
            for r in db.conn.execute(
                "SELECT code, name, shares, cost_nav, sector, buy_date "
                "FROM positions WHERE account_id = ?", (account_id,)
            ):
                hold_days = 0
                if r["buy_date"]:
                    try:
                        bd = datetime.fromisoformat(r["buy_date"])
                        hold_days = (datetime.now() - bd).days
                    except Exception:
                        pass
                positions.append({
                    "code": r["code"], "name": r["name"],
                    "shares": r["shares"], "cost_nav": r["cost_nav"],
                    "sector": r["sector"], "hold_days": hold_days,
                })

            cash_row = db.conn.execute(
                "SELECT cash FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
            cash = cash_row["cash"] if cash_row else 0.0

            funds = snap.get("funds", {})
            position_value = sum(
                p["shares"] * (funds.get(p["code"], {}) or {}).get("current_nav", p["cost_nav"])
                for p in positions
            )
            total_value = cash + position_value

            date = args.date or datetime.now().strftime("%Y-%m-%d")
            engine = DecisionEngine(db, account_id)
            packet = engine.decide(
                date=date, market_data=snap, positions=positions,
                cash=cash, total_value=total_value,
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
    ap = argparse.ArgumentParser(prog="decide.py",
                                 description="Smart-Invest decision CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="Run the decision engine for an account")
    p.add_argument("--account", default="主线")
    p.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    p.add_argument("--format", choices=["json", "md"], default="json")
    p.set_defaults(func=cmd_run)
    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Sanity-check**

Run: `python3 scripts/decide.py run --help`
Expected: argparse help output, no traceback.

If an account exists:
Run: `python3 scripts/decide.py run --account 主线 --format md`
Expected: human-readable Markdown summary OR a JSON error packet if no account / network down.

- [ ] **Step 3: Commit**

```bash
git add scripts/decide.py
git commit -m "feat: scripts/decide.py — single decision-engine entry point"
```

---

## Task 14: Extend `data/decision_tree.json` schema

**Files:**
- Modify: `data/decision_tree.json`

The engine already works without this — it's documentation alignment for Phase 2 / future evolution.

- [ ] **Step 1: Read current file**

```bash
cat /Users/tght/develop/project/2026/smart-invest/data/decision_tree.json
```

- [ ] **Step 2: Add a top-level `rules_meta` block keyed by rule_id**

Add (preserve all existing keys) — insert at the end of the top-level object (before the closing `}`):

```json
  "rules_meta": {
    "cash_reserve":          {"severity": "block", "enabled": true},
    "single_position":       {"severity": "block", "enabled": true},
    "sector_concentration":  {"severity": "block", "enabled": true},
    "anti_chase":            {"severity": "block", "enabled": true},
    "bear_market_new_position": {"severity": "block", "enabled": true},
    "market_regime_unknown": {"severity": "block", "enabled": true},
    "low_buy":               {"severity": "info",  "enabled": true},
    "emergency_stop_loss":   {"severity": "warn",  "enabled": true},
    "absolute_stop_loss":    {"severity": "warn",  "enabled": true},
    "time_based_stop_loss":  {"severity": "warn",  "enabled": true},
    "take_profit_tier_20":   {"severity": "info",  "enabled": true},
    "take_profit_tier_30":   {"severity": "info",  "enabled": true},
    "take_profit_tier_40":   {"severity": "info",  "enabled": true},
    "take_profit_clearout":  {"severity": "info",  "enabled": true},
    "drawdown_protection":   {"severity": "warn",  "enabled": true},
    "low_buy_deferred_drawdown": {"severity": "info", "enabled": true}
  }
```

- [ ] **Step 3: Verify JSON parses**

Run: `python3 -c "import json; json.load(open('/Users/tght/develop/project/2026/smart-invest/data/decision_tree.json'))"`
Expected: no output, exit 0.

- [ ] **Step 4: Verify tests still pass**

Run: `python3 -m pytest tests/ -v`
Expected: all PASSes.

- [ ] **Step 5: Commit**

```bash
git add data/decision_tree.json
git commit -m "feat(rules): add rules_meta block (id/enabled/severity)"
```

---

## Task 15: Rewrite `SKILL.md`

**Files:**
- Modify: `SKILL.md` (full rewrite)

- [ ] **Step 1: Confirm current line count**

Run: `wc -l /Users/tght/develop/project/2026/smart-invest/SKILL.md`
Expected: 1098 lines.

- [ ] **Step 2: Replace `SKILL.md` with the new structure**

Write the file. Section outline (target ≤ 700 lines):

```markdown
---
name: smart-invest
description: 智能基金投资助手 — ...
argument-hint: ...
---

# 智能基金投资助手

[1-paragraph role statement + Markdown/中文输出原则]

$ARGUMENTS

---

## 一、触发场景（用户怎么唤起）

[1 张表压缩 5 个模式 + 反触发]

## 二、决策入口（核心）

**所有分析模式都先调 `python3 scripts/decide.py run`，拿到决策包再翻译给用户。**
Claude 不再独立判断买卖 — 引擎说啥就是啥，Claude 负责解释、补语境、写风险提示。

工作流：
1. `python3 scripts/decide.py run --account 主线 --format json` → 拿决策包
2. 阅读包里的 actions / blocked_actions / alerts
3. 按 §六 的模板生成中文报告
4. 如属于完整模式：发邮件 + 桌面通知

[小段说明 decide 返回 JSON 的字段含义]

## 三、首次使用引导（独立成节，前置）

[原 §十的邮件配置引导，搬来这里 — Claude 每次会话首次触发都先 check 一次]

## 四、持仓与交易管理

**所有持仓 / 订单写操作必须经过 `db.py` CLI。**
[精简后的 buy / sell / 加减仓 step 表，但删除"step-by-step 重复模板"，让 Claude 看引擎建议后照模板填参]

## 五、定时报告体系

[1 张表压缩午/晚/周/月报 + 通知规则；模板拼接见 §六]

## 六、报告 Markdown 模板

[保留 §六、§十二.2-12.4 的模板，但去重 — 4 个报告共用大部分结构]

## 七、单只基金分析 / 行业分析

[原 §八、模式 D — 这两个不经 decide.py（因为它们不是决策，是探索）]

## 八、梦境训练入口

[只列触发词 + simulate.py 命令；内部细节"见 README_DB.md 和 Phase 2 计划"]

## 九、注意事项与风控红线

[合并原 §十三 注意事项 + §七.6 / §六.1 风控红线，去重]

## 附录 A：常用 secid 与基金池

[原 §2.5 + §14.1，1 张表]

## 附录 B：决策规则速查（v2.0）

> 详细决策树见 `data/decision_tree.md` 与 `decision_engine.py`。这里只列规则 ID 速查。

[1 张表：规则 id → 中文名 → 触发条件简述。让用户看报告时能秒懂 rule_id 含义]
```

**关键删除**：
- §一-B 模式详解（与 §二 + §五重复）
- §五 工作流 step-by-step（被引擎接管）
- §七 投资建议策略详表（搬到 decision_tree.md，本文件只引用）
- §十一.3 手动触发判断逻辑（引擎吃所有路径）
- 重复的风控红线表（§7.6 / §6.1）

**关键保留**：
- 中文输出原则
- 4 个报告 Markdown 模板（用户视觉契约）
- 邮件触发规则
- 风险提示语
- 数据来源声明

实际写入时，从原 `SKILL.md` 抽取保留部分 + 删除冗余 + 在 §二 加入 decide.py 工作流。

- [ ] **Step 3: Verify line count + key sections**

Run: `wc -l /Users/tght/develop/project/2026/smart-invest/SKILL.md`
Expected: ≤ 700 lines.

Run: `grep -c "decide.py" /Users/tght/develop/project/2026/smart-invest/SKILL.md`
Expected: ≥ 3 (referenced in §二, §四, §附录 B).

- [ ] **Step 4: Commit**

```bash
git add SKILL.md
git commit -m "refactor(skill): decision-engine-centric workflow, 1098→<=700 lines"
```

---

## Task 16: Update `CLAUDE.md` and add note about `decide.py`

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Read current CLAUDE.md**

(Already familiar from `/init` — has "Architecture" and "Hard rules" sections.)

- [ ] **Step 2: Add `decide.py` to the module layout table and Hard rules**

In the "Module layout" table, add a new row:

```markdown
| `scripts/decide.py` | **Single decision entry point.** Aggregates market data + calls `DecisionEngine.decide()` → outputs structured decision packet (JSON or Markdown). All `SKILL.md` analysis modes route through this. |
```

In "Hard rules" section, add a bullet:

```markdown
- **Decisions go through `scripts/decide.py`.** Claude does not independently apply buy/sell rules — the engine produces a structured decision packet; Claude translates it. If you change a rule, change the engine + add a test, not just `SKILL.md` prose.
```

In "Common commands" section, add:

```bash
# Run live decision engine
python3 scripts/decide.py run --account 主线 --format md
python3 scripts/decide.py run --account 主线 --format json | jq .

# Tests
python3 -m pytest tests/ -v
```

- [ ] **Step 3: Verify**

Run: `grep -c "decide.py" /Users/tght/develop/project/2026/smart-invest/CLAUDE.md`
Expected: ≥ 3.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude): document decide.py as single decision entry"
```

---

## Task 17: Final verification checklist (spec §13)

This is the completion gate — run all checks before declaring Phase 1 done.

- [ ] **Check 1: All tests pass**

Run: `cd /Users/tght/develop/project/2026/smart-invest && python3 -m pytest tests/ -v`
Expected: ≥ 19 PASSes, 0 FAILs.

- [ ] **Check 2: `decide.py` JSON output is valid**

Run: `python3 scripts/decide.py run --account 主线 --format json 2>/dev/null | python3 -c "import json,sys; p=json.load(sys.stdin); assert p['schema_version']=='1.0'; assert 'market_regime' in p; assert 'actions' in p; print('OK')"`
Expected: prints `OK`.
(If `主线` doesn't exist, create a temporary account or run with `--account test` after fixture seeding.)

- [ ] **Check 3: `decide.py` Markdown output renders**

Run: `python3 scripts/decide.py run --account 主线 --format md`
Expected: human-readable Markdown with 决策包 / 市场环境 / 建议操作 sections.

- [ ] **Check 4: SKILL.md is ≤ 700 lines**

Run: `wc -l SKILL.md`
Expected: ≤ 700.

- [ ] **Check 5: Old CLI commands still work (compatibility)**

Run: `python3 scripts/db.py accounts`
Expected: lists accounts without error.

Run: `python3 scripts/fetch_fund.py indices`
Expected: prints index quotes (or graceful error on network failure).

- [ ] **Check 6: simulate.py untouched (Phase 2 will refactor it)**

Run: `git diff main --stat scripts/simulate.py`
Expected: empty (no changes to simulate.py in Phase 1).

- [ ] **Check 7: No sensitive files modified**

Run: `git diff main --stat data/portfolio.json data/orders.json data/smart_invest.db`
Expected: empty.

- [ ] **Check 8: All Phase 1 commits clean and ordered**

Run: `git log --oneline main..HEAD`
Expected: ~16 commits, all green-prefixed with `feat:` / `test:` / `refactor:` / `docs:`.

- [ ] **Step 9: Mark Phase 1 complete**

Update `docs/superpowers/specs/2026-05-28-smart-invest-overhaul-design.md` § 14 to mark Phase 1 as ✅ shipped, with a one-line note about pending items (e.g., live cron migration, real-account smoke test).

- [ ] **Step 10: Final commit**

```bash
git add docs/superpowers/specs/2026-05-28-smart-invest-overhaul-design.md
git commit -m "docs(spec): mark Phase 1 complete"
```

---

## Self-review checklist

- [x] **Spec coverage:** §4 non-goals, §5 components, §6 schema, §7 confidence, §8 priority, §9 errors, §10 tests, §11 SKILL.md, §13 verification — all map to Tasks 1-17. ✓
- [x] **No placeholders:** every step has actual code or actual command. ✓
- [x] **Type consistency:** `DecisionEngine.decide(date, market_data, positions, cash, total_value)` signature is identical across Tasks 3-11. Packet keys identical to spec §6. ✓
- [x] **TDD discipline:** every code task has Step 1 = failing test, Step 4 = passing. ✓
- [x] **Commits frequent:** ~16 commits across 17 tasks. ✓
