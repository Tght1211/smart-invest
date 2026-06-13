# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A **Claude Code Skill** (not a standalone app). The user installs it by copying this directory to `~/.claude/skills/smart-invest/`, after which the skill is invoked from inside a Claude Code session. `SKILL.md` is the operational prompt loaded by Claude Code at runtime — it defines triggers, workflows, decision rules, and which CLI commands to call. The Python scripts in `scripts/` are the tools that prompt invokes.

When editing this repo, you are editing **either** the runtime prompt (`SKILL.md`, `reference/*.md`, `data/decision_tree.{md,json}`) **or** the Python tools the prompt calls (`scripts/*.py`). Keep the two in sync — if you rename a CLI subcommand or change its flags, also update `SKILL.md` and `reference/*.md` (search for the command string).

`SKILL.md` follows Anthropic's progressive-disclosure guidance: it's a lean navigable core (<500 lines) that links one level deep to `reference/` detail files loaded on demand — `reference/report-templates.md` (card DSL + three-session/evening/weekly/monthly report templates + 自主经营纪律), `reference/analysis-and-backtest.md` (single-fund/sector modes + 梦境 backtest/lab), `reference/rule-and-fund-reference.md` (rule-ID lookup + secid/fund-pool appendix). Heavy reference content goes in these files, not inline in SKILL.md.

## Architecture

**Single source of truth: SQLite** at `data/smart_invest.db` (gitignored). The legacy `portfolio.json` / `orders.json` are backup/migration only — never read or write them from new code, and never use Read/Write/Edit on them at runtime. All position and order mutations go through `python3 scripts/db.py <subcommand>` so that decision-audit fields (rule, version, context, checks_passed/failed) are captured.

Two account types share the same schema:
- `主线` (`type=main`) — real-money portfolio
- `梦境-<sim_id>` (`type=dream`) — backtest accounts auto-created by `simulate.py`

All position/trade CLIs take `--account <name>` to switch between them.

**Module layout** (all pure Python 3 stdlib — no `pip install`, no third-party deps; do not introduce any):

| Script | Role |
|---|---|
| `scripts/db.py` | SQLite schema + CRUD CLI. Defines the `Database` class imported by other scripts. Honours `SMART_INVEST_DB` env var to relocate the DB (used by tests). Tables: `accounts`, `positions`, `trades` (with audit fields), `daily_snapshots`, `decision_tree_versions`, `strategy_evolutions`, `simulation_runs`, **`trade_reviews`** (per-trade timing verdict "memory"; self-heals on existing DBs via `_ensure_review_table`), `auto_invest_plans`. Review CRUD: `add_trade_review`/`get_trade_reviews`/`get_review_summary`; CLI `db.py reviews`. Also has `cash` (现金校准) and `dca add/list/remove/toggle` (定投计划). |
| `scripts/auto_invest.py` | 定投（DCA）执行。`is_due()` 纯函数按周期键判定到期（日/周/双周/月，周末·月末顺延，幂等去重）；`record_due_plans()` 把到期计划记账（写交易+累加持仓+扣现金+通知）。daily_report close 调用。定投基金代码经 `gather_market_snapshot` 注入 `market_data['auto_invest_codes']`，引擎据此对这些基金不出买入建议（卖出照常）。 |
| `scripts/fetch_fund.py` | Market data via 天天基金/东方财富 public HTTP endpoints (no auth). Subcommands: `market-summary`, `indices`, `sectors`, `estimate`, `nav`, `rank`, `index-kline`, `portfolio-check`, `portfolio-show`, `orders-show`, **`market-snapshot`**, **`returns`** (per-fund + total return trajectory w/ sparkline), **`news`** (free 东方财富 7x24 快讯, `gather_market_news()`, no key). `portfolio-check/show` now show **持有天数** (`_held_days`). `gather_market_snapshot()` feeds `decide.py` and now also carries `news` + `recent_review_summary` (report-layer, not rule-driving). |
| `scripts/decision_engine.py` | The rule engine. `DecisionEngine.decide()` returns a structured **decision packet** (schema in `docs/superpowers/specs/2026-05-28-smart-invest-overhaul-design.md` §6). 25 unit tests in `tests/test_decision_engine.py` pin its behaviour. Old `check_*` helpers retained for backtest compatibility. Also exports module-level pure fn `evaluate_trade_timing(action, nav_at_trade, nav_after)` → 踩中/追高套牢/规避下跌/卖飞/中性 verdict + score (used by `decide.py review`). |
| `scripts/decide.py` | **The single decision entry point.** Thin CLI wrapping `fetch_fund.gather_market_snapshot` + `DecisionEngine.decide`. Subcommands: `run` (JSON/md/brief), `stats` (per-rule win/loss), `evolve` (writes strategy_evolutions), `why-not` (explains why a code isn't in actions), **`review`** (retrospective timing verdict on past trades, `--save` writes `trade_reviews`). `build_trade_reviews()`/`summarize_reviews()` are reusable (shared with `daily_report.py`). All `SKILL.md` analysis modes route through this. |
| `scripts/signals.py` | Phase 3 technical indicators (pure stdlib): `compute_rsi`, `compute_macd`, `compute_ma_slope`, `compute_breakout`, `attach_signals`. Used by `fetch_fund._fund_snapshot` to enrich each fund with a `signals` block. **Not used by any rule yet** — attached to `actions[].context.signals` for visibility; new rules gated on backtest evidence (P5). |
| `scripts/simulate.py` | Backtest engine ("梦境训练"). Replays historical NAVs day-by-day, **must avoid future leak** — only use data with date ≤ current sim date. Auto-creates a `梦境-<sim_id>` account. Phase 2: `--engine` flag drives backtest via `DecisionEngine.decide()` (old inline rule path retained for compatibility). |
| `scripts/send_email.py` | QQ-SMTP HTML email. Has `check` / `setup` / `setup --no-email` / `test` / `send` / `trade-notify` subcommands. `trade-notify` MUST fire after every buy/sell. Card DSL renderer (`:::card`/`:::spark`/`:::action`/`:::blocks`/`:::timeline`) lives in `markdown_to_html()`. |
| `scripts/strategy_lab.py` | 梦境实验室: runs N strategy variants (rules_override injected into engine) over the same history window → metrics & ranking → `--evolve` writes strategy_evolutions → `--promote vX.Y` registers a new tree version AND rewrites `data/decision_tree.json`. Engine default version follows that file. Rule changes must pass the lab on ≥2 different market-regime windows before promotion. |
| `scripts/chart.py` | Pure rendering, no network: terminal line charts (`fetch_fund.py chart …`) + email sparkline HTML (`:::spark`). |
| `scripts/daily_report.py` | Deterministic three-session (open/mid/close) card report: data → engine → card → email → (close) auto-record. The entry point OpenClaw cron calls. Cards now also render: per-holding 30d NAV sparks (`card_holding_sparks`), holdings 持有天数 column, free 财经要闻 (`card_news`), and a review-annotated operation timeline (`card_timeline` calls `build_trade_reviews(..., save=True)`). `--html [path]` renders the email HTML to a file for browser preview (no send). Safety guards: take-profit skip (LET_WINNERS_RUN), stop-loss execute, QDII-buy skip, 7-day same-rule dedup. |

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
python3 scripts/decide.py run --account 主线 --format brief        # 3-5 line summary
python3 scripts/decide.py why-not --account 主线 --code 512480     # explain non-recommendation
python3 scripts/decide.py stats --account 主线                     # per-rule win rate / expectancy
python3 scripts/decide.py evolve --account 主线 --to-version v2.1  # write strategy_evolutions row
python3 scripts/decide.py review --account 主线 --save             # retrospective timing verdict → trade_reviews
python3 scripts/decide.py review --account 主线 --summary          # read stored review memory

# Holding days / return trajectory / news / review memory
python3 scripts/fetch_fund.py returns --account 主线 --days 30     # per-fund + total return change (sparkline)
python3 scripts/fetch_fund.py news --keyword 半导体                # free 7x24 finance news (no key)
python3 scripts/db.py reviews --account 主线                       # inspect stored operation reviews

# Email card HTML preview (no send)
python3 scripts/daily_report.py --session close --account 主线 --no-email --no-record --html

# Backtest with engine
python3 scripts/simulate.py run --start 2026-02-26 --end 2026-05-26 --budget 50000 --engine

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

**Test suite**: `tests/test_decision_engine.py` (25 tests pinning engine rules) + `tests/test_decide_cli.py` (CLI smoke test) + `tests/test_review.py` (24 tests: `evaluate_trade_timing`, `trade_reviews` CRUD/upsert, `_held_days`/`_sparkline`/`_align_total_return_series`, `build_trade_reviews`/`summarize_reviews` with `fetch_nav_series` mocked) + others. 123 tests total, all stdlib `unittest`. No linter or build step.

**Design docs** (Phase 1+):
- Spec: `docs/superpowers/specs/2026-05-28-smart-invest-overhaul-design.md` — describes the decision-packet contract, rule priority, confidence formula, error handling.
- Plan: `docs/superpowers/plans/2026-05-28-smart-invest-phase1.md` — TDD implementation plan.
- Phase 2-4 roadmap is in the spec's §14 "路线图".

## When the skill prompt asks you to do something

If a Claude Code session has loaded `SKILL.md` and you're asked to act as the smart-invest skill, follow `SKILL.md` literally — it has detailed workflows for "每日分析" (mode A, full report + email + notification), "快速看看" (mode B, quick check, no email), single-fund analysis (mode C), sector analysis (mode D), and "梦境训练" backtesting (mode E). Trade-notify after every buy/sell is non-negotiable.

If you're instead editing this repo's source (the more common case here), treat `SKILL.md` as a spec to keep consistent with code changes — not as instructions to execute.
