# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A **Claude Code Skill** (not a standalone app). The user installs it by copying this directory to `~/.claude/skills/smart-invest/`, after which the skill is invoked from inside a Claude Code session. `SKILL.md` is the operational prompt loaded by Claude Code at runtime — it defines triggers, workflows, decision rules, and which CLI commands to call. The Python scripts in `scripts/` are the tools that prompt invokes.

When editing this repo, you are editing **either** the runtime prompt (`SKILL.md`, `data/decision_tree.{md,json}`) **or** the Python tools the prompt calls (`scripts/*.py`). Keep the two in sync — if you rename a CLI subcommand or change its flags, also update `SKILL.md` (search for the command string).

## Architecture

**Single source of truth: SQLite** at `data/smart_invest.db` (gitignored). The legacy `portfolio.json` / `orders.json` are backup/migration only — never read or write them from new code, and never use Read/Write/Edit on them at runtime. All position and order mutations go through `python3 scripts/db.py <subcommand>` so that decision-audit fields (rule, version, context, checks_passed/failed) are captured.

Two account types share the same schema:
- `主线` (`type=main`) — real-money portfolio
- `梦境-<sim_id>` (`type=dream`) — backtest accounts auto-created by `simulate.py`

All position/trade CLIs take `--account <name>` to switch between them.

**Module layout** (all pure Python 3 stdlib — no `pip install`, no third-party deps; do not introduce any):

| Script | Role |
|---|---|
| `scripts/db.py` | SQLite schema + CRUD CLI. Defines the `Database` class imported by other scripts. Honours `SMART_INVEST_DB` env var to relocate the DB (used by tests). Tables: `accounts`, `positions`, `trades` (with audit fields), `daily_snapshots`, `decision_tree_versions`, `strategy_evolutions`, `simulation_runs`. |
| `scripts/fetch_fund.py` | Market data via 天天基金/东方财富 public HTTP endpoints (no auth). Subcommands: `market-summary`, `indices`, `sectors`, `estimate`, `nav`, `rank`, `index-kline`, `portfolio-check`, `portfolio-show`, `orders-show`, **`market-snapshot`**. The `gather_market_snapshot()` function is the data feed for `decide.py`. |
| `scripts/decision_engine.py` | The rule engine. `DecisionEngine.decide()` returns a structured **decision packet** (schema in `docs/superpowers/specs/2026-05-28-smart-invest-overhaul-design.md` §6). 25 unit tests in `tests/test_decision_engine.py` pin its behaviour. Old `check_*` helpers retained for backtest compatibility. |
| `scripts/decide.py` | **The single decision entry point.** Thin CLI wrapping `fetch_fund.gather_market_snapshot` + `DecisionEngine.decide`. Outputs JSON (default) or Markdown summary. All `SKILL.md` analysis modes route through this. |
| `scripts/simulate.py` | Backtest engine ("梦境训练"). Replays historical NAVs day-by-day, **must avoid future leak** — only use data with date ≤ current sim date. Auto-creates a `梦境-<sim_id>` account. Phase 2 will refactor it to call `DecisionEngine.decide()` directly. |
| `scripts/send_email.py` | QQ-SMTP HTML email. Has `check` / `setup` / `setup --no-email` / `test` / `send` / `trade-notify` subcommands. `trade-notify` MUST fire after every buy/sell. |

Decision rules are versioned: `data/decision_tree.json` is the live ruleset and `decision_tree_versions` table stores history with parent/changelog/reason for each version. The `strategy_evolutions` table records before/after metrics when a version is promoted from a backtest.

## Hard rules (these are user-facing contracts; do not weaken them)

- **Never edit `data/portfolio.json` / `data/orders.json` directly.** Use `db.py add-position` / `remove-position` / `add-order`. `add-position` is upsert-style — it accumulates shares and recomputes weighted-average cost for existing holdings.
- **Every buy/sell must call `send_email.py trade-notify`** after the DB write succeeds. The skill prompt enforces this; the Python side does not.
- **Decisions go through `scripts/decide.py`.** At runtime, Claude does not independently apply buy/sell rules — the engine produces a structured decision packet; Claude translates it. If you want to change a rule, change `decision_engine.py` + add a unit test, not `SKILL.md` prose.
- **Stdlib only — including tests.** Tests use `unittest`, not `pytest`. If you find yourself wanting `requests`/`pandas`/etc., use `urllib.request` / built-in `sqlite3` / hand-rolled CSV like the rest of the codebase does.
- **No future leak in `simulate.py`.** Any new feature added to the simulator must only access data dated ≤ the current simulation day.

## Common commands

```bash
# First-time setup
python3 scripts/db.py init
python3 scripts/db.py import-json     # only if migrating from legacy JSON

# Inspect state
python3 scripts/db.py accounts
python3 scripts/db.py positions --account 主线
python3 scripts/db.py trades    --account 主线 --limit 50
python3 scripts/db.py tree-versions
python3 scripts/db.py evolutions

# Market data
python3 scripts/fetch_fund.py market-summary
python3 scripts/fetch_fund.py portfolio-check --account 主线
python3 scripts/fetch_fund.py nav 110011 --days 60
python3 scripts/fetch_fund.py market-snapshot --account 主线   # aggregated feed for decide.py

# Run live decision engine
python3 scripts/decide.py run --account 主线 --format md
python3 scripts/decide.py run --account 主线 --format json

# Tests (stdlib unittest, no pytest)
python3 -m unittest discover tests -v
python3 -m unittest tests.test_decision_engine -v

# Run with a sandboxed DB (avoids touching the real one)
SMART_INVEST_DB=/tmp/sandbox.db python3 scripts/db.py init
SMART_INVEST_DB=/tmp/sandbox.db python3 scripts/decide.py run --account demo

# Backtest
python3 scripts/simulate.py run --start 2026-02-26 --end 2026-05-26 --budget 50000
python3 scripts/simulate.py list
python3 scripts/simulate.py report <sim_id>

# Email
python3 scripts/send_email.py check       # CONFIGURED / DISABLED / NOT_CONFIGURED
python3 scripts/send_email.py test
```

**Test suite**: `tests/test_decision_engine.py` (25 tests pinning engine rules) + `tests/test_decide_cli.py` (CLI smoke test). All stdlib `unittest`. No linter or build step.

**Design docs** (Phase 1+):
- Spec: `docs/superpowers/specs/2026-05-28-smart-invest-overhaul-design.md` — describes the decision-packet contract, rule priority, confidence formula, error handling.
- Plan: `docs/superpowers/plans/2026-05-28-smart-invest-phase1.md` — TDD implementation plan.
- Phase 2-4 roadmap is in the spec's §14 "路线图".

## When the skill prompt asks you to do something

If a Claude Code session has loaded `SKILL.md` and you're asked to act as the smart-invest skill, follow `SKILL.md` literally — it has detailed workflows for "每日分析" (mode A, full report + email + notification), "快速看看" (mode B, quick check, no email), single-fund analysis (mode C), sector analysis (mode D), and "梦境训练" backtesting (mode E). Trade-notify after every buy/sell is non-negotiable.

If you're instead editing this repo's source (the more common case here), treat `SKILL.md` as a spec to keep consistent with code changes — not as instructions to execute.
