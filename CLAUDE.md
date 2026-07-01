# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A **Claude Code Skill** (not a standalone app). The user installs it by copying this directory to `~/.claude/skills/smart-invest/`, after which the skill is invoked from inside a Claude Code session. `SKILL.md` is the operational prompt loaded by Claude Code at runtime — it defines triggers, workflows, decision rules, and which CLI commands to call. The Python scripts in `scripts/` are the tools that prompt invokes.

When editing this repo, you are editing **either** the runtime prompt (`SKILL.md`, `reference/*.md`, `data/decision_tree.{md,json}`) **or** the Python tools the prompt calls (`scripts/*.py`). Keep the two in sync — if you rename a CLI subcommand or change its flags, also update `SKILL.md` and `reference/*.md` (search for the command string).

`SKILL.md` follows Anthropic's progressive-disclosure guidance: it's a lean navigable core (<500 lines) that links one level deep to `reference/` detail files loaded on demand — `reference/report-templates.md` (card DSL + three-session/evening/weekly/monthly report templates + 自主经营纪律), `reference/analysis-and-backtest.md` (single-fund/sector modes + 梦境 backtest/lab), `reference/strategy-playbook.md` (full strategy: regimes/priority/板块下钻/短C长A/复盘闭环), `reference/rule-and-fund-reference.md` (rule-ID lookup + share-class pairings + secid/fund-pool appendix). Heavy reference content goes in these files, not inline in SKILL.md. SKILL.md §〇 mandates a session-start "对表"(`fetch_fund.py now`) + "回溯"(`decide.py review/stats`) before any analysis.

## Architecture

**Single source of truth: SQLite** at `data/smart_invest.db` (gitignored). The legacy `portfolio.json` / `orders.json` are backup/migration only — never read or write them from new code, and never use Read/Write/Edit on them at runtime. All position and order mutations go through `python3 scripts/db.py <subcommand>` so that decision-audit fields (rule, version, context, checks_passed/failed) are captured.

Two account types share the same schema:
- `主线` (`type=main`) — real-money portfolio
- `梦境-<sim_id>` (`type=dream`) — backtest accounts auto-created by `simulate.py`

All position/trade CLIs take `--account <name>` to switch between them.

**Module layout** (all pure Python 3 stdlib — no `pip install`, no third-party deps; do not introduce any):

| Script | Role |
|---|---|
| `scripts/db.py` | SQLite schema + CRUD CLI. Defines the `Database` class imported by other scripts. Honours `SMART_INVEST_DB` env var to relocate the DB (used by tests). Tables: `accounts`, `positions`, `trades` (with audit fields), `daily_snapshots`, `decision_tree_versions`, `strategy_evolutions`, `simulation_runs`, **`trade_reviews`** (per-trade timing verdict "memory"; self-heals on existing DBs via `_ensure_review_table`), **`daily_plans`** (三时段操作计划基线，`save_daily_plan`/`get_daily_plan`，自愈 `_ensure_plan_table`), `auto_invest_plans`. Review CRUD: `add_trade_review`/`get_trade_reviews`/`get_review_summary`; CLI `db.py reviews`. Also has `cash` (现金校准) and `dca add/list/remove/toggle` (定投计划). |
| `scripts/auto_invest.py` | 定投（DCA）执行。`is_due()` 纯函数按周期键判定到期（日/周/双周/月，周末·月末顺延，幂等去重）；`record_due_plans()` 把到期计划记账（写交易+累加持仓+扣现金+通知）。daily_report close 调用。定投基金代码经 `gather_market_snapshot` 注入 `market_data['auto_invest_codes']`，引擎据此对这些基金不出买入建议（卖出照常）。 |
| `scripts/fetch_fund.py` | Market data via 天天基金/东方财富 public HTTP endpoints (no auth). Subcommands: `market-summary`, `indices`, `sectors`, `estimate`, `nav`, `rank`, `index-kline`, `portfolio-check`, `portfolio-show`, `orders-show`, **`market-snapshot`** (`--discover N` injects fresh cross-sector candidates), **`returns`** (per-fund + total return trajectory w/ sparkline), **`news`** (free 东方财富 7x24 快讯, `gather_market_news()`, no key), **`now`** (`market_clock()` pure fn — local time/tz/A股 session, R1 会话对表), **`sector-scan`** (`scan_sectors`/`fetch_board_windows`/`classify_board_trend` — board 今日/7日/30日/6月 multi-window + trend label, R2), **`discover`** (`discover_candidates`/`fetch_fund_rank`/`score_candidate` — cross-sector OTC fund discovery, multi-window consistency score, R3/R4；`--quality`/`quality=True` 对入选标的拉基本面跑红旗、剔除 critical），**`fundamentals`** (`fetch_fundamentals` 单拉 `fund.eastmoney pingzhongdata/{code}.js`+持仓解析规模/持有人机构占比/经理从业年限+能力/资产配置/前十大集中度/费率；`evaluate_red_flags` **纯函数**红旗清单——清盘<2亿·机构>90%·杠杆债占>120% 为 critical，规模激增/缩水·经理<3年·抗风险稳定性短板·前十大>60% 为 warn；`has_critical`。借鉴竞品 jiafei 五层质量框架，补我们量价/动量之外的「基金质量结构性风险」盲区；`decide.py why-not` 内嵌质量体检，`discover --quality` 做选基闸门）， **`share-class`** (`resolve_share_class`/`detect_share_class`/`base_fund_name`/`pick_siblings` — A↔C sibling lookup, 短C长A, R5). `portfolio-check/show` now show **持有天数** (`_held_days`). `gather_market_snapshot()` feeds `decide.py` and now also carries `news` + `recent_review_summary` (report-layer, not rule-driving). **`relevant_news(news, name, sector)`** matches headlines to a fund (theme-keyword list + substring, falls back to top news so every op has news support). **`portfolio_return_series(account, days)`** reuses `_align_total_return_series` for the wallet-card sparkline (excludes today's pending buys via `_buy_unconfirmed`). **`calibrate_costs(account, apply)`** (CLI `calibrate`) corrects estimate-recorded buys to the real close NAV (cost+shares) once published — **single-lot only, skips accumulated/imported**, idempotent (`outcome=nav_calibrated`); `daily_report` runs it at each session start (skipped in `--html`/`--no-record`). |
| `scripts/decision_engine.py` | The rule engine. `DecisionEngine.decide()` returns a structured **decision packet** (schema in `docs/superpowers/specs/2026-05-28-smart-invest-overhaul-design.md` §6; packet now also carries `discovered[]` + per-buy `horizon`/`share_class`). 25 unit tests in `tests/test_decision_engine.py` pin its behaviour. Old `check_*` helpers retained for backtest compatibility. Also exports module-level pure fn `evaluate_trade_timing(action, nav_at_trade, nav_after)` → 踩中/追高套牢/规避下跌/卖飞/中性 verdict + score (used by `decide.py review`), and `horizon_for_rule(rule_id)` → (short/long, C/A) driving `_annotate_horizon()` (R5 短C长A). `_candidate_momentum()` blends 20/60/120-day returns (multi-window consistency) for `position_build` candidate ranking (R3/R4). |
| `scripts/decide.py` | **The single decision entry point.** Thin CLI wrapping `fetch_fund.gather_market_snapshot` + `DecisionEngine.decide`. Subcommands: `run` (JSON/md/brief; `--discover N` injects fresh candidates; md renders `horizon`/`share_class` via `_share_class_suffix` + a `discovered` section), `stats` (per-rule win/loss), `evolve` (writes strategy_evolutions), `why-not` (explains why a code isn't in actions), **`review`** (retrospective timing verdict on past trades, `--save` writes `trade_reviews`). `build_trade_reviews()`/`summarize_reviews()` are reusable (shared with `daily_report.py`). All `SKILL.md` analysis modes route through this. |
| `scripts/news_sentiment.py` | 新闻感知（v2.3 实验）。`classify_news_sentiment(items, sector)` 用利好/利空关键词权重表给新闻打情绪分 ∈[-3,3]+标签（关键词表已扩到覆盖真实标题词汇：涨停/暴涨/领涨/创历史新高/重挫/抛售/蒸发…，否则真实新闻常被误判中性）；`get_dynamic_low_buy_threshold(trend_strength, news_sentiment, base=-0.03)` 把写死的 -3% 低吸阈值改成「趋势强+利好放宽、趋势弱+利空收紧」的动态阈值（限幅 +2%/-2.5%）。回测历史新闻缓存：`load_news_cache()` 读 **`data/news_cache.json`**（按 `YYYY-MM`×赛道存真实新闻标题，由 WebSearch 整理，2025-06..2026-06），`cached_news_sentiment(date, sector)` 取某月某赛道情绪（赛道 0.7+大盘 0.3 混合，过 `classify_news_sentiment` 打分，口径与实盘一致；无缓存返回 None 让调用方回退）。引擎 `_compute_news_sentiment`（实盘从 `market_data['news']` 匹配）+ `_try_low_buy`（动态阈值）消费它；`simulate._synthesize_news_sentiment` 回测时**优先用真实缓存**、未覆盖月份才回退价格代理合成。纯 stdlib。测试 `tests/test_news_sentiment.py`（14）。**A/B 回测（2025-06..2026-06）：真实新闻 vs 合成新闻 → 胜率 19%→44%、回撤同为 -9.37%、总收益≈持平**。新闻仅驱动动态低吸阈值，其余仅供 reason_zh 展示。 |
| `scripts/signals.py` | Phase 3 technical indicators (pure stdlib): `compute_rsi`, `compute_macd`, `compute_ma_slope`, `compute_breakout`, `attach_signals`. Used by `fetch_fund._fund_snapshot` to enrich each fund with a `signals` block. **Not used by any rule yet** — attached to `actions[].context.signals` for visibility; new rules gated on backtest evidence (P5). |
| `scripts/simulate.py` | Backtest engine ("梦境训练"). Replays historical NAVs day-by-day, **must avoid future leak** — only use data with date ≤ current sim date. Auto-creates a `梦境-<sim_id>` account. Phase 2: `--engine` flag drives backtest via `DecisionEngine.decide()` (old inline rule path retained for compatibility). |
| `scripts/send_email.py` | QQ-SMTP HTML email. Subcommands `check`/`setup`/`test`/`send`/`trade-notify`/**`flush-outbox`**. **Reliable delivery**: `send_email()` → `flush_outbox()` (drain `data/outbox/`) → `_deliver()` (`_smtp_send` retries 3× w/ 1-2-4s backoff) → on failure `_enqueue_outbox()` 落盘待发，下次任何发信自动补发。`trade-notify` MUST fire after every buy/sell and renders an **operation report** with `--reason`/`--news`(repeatable)/`--wallet`. Card DSL renderer (`:::card`/`:::spark`/`:::action`/`:::blocks`/`:::timeline`/`:::returns`) lives in `markdown_to_html()`. |
| `scripts/strategy_lab.py` | 梦境实验室: runs N strategy variants (rules_override injected into engine) over the same history window → metrics & ranking → `--evolve` writes strategy_evolutions → `--promote vX.Y` registers a new tree version AND rewrites `data/decision_tree.json`. Engine default version follows that file. Rule changes must pass the lab on ≥2 different market-regime windows before promotion. |
| `scripts/chart.py` | Pure rendering, no network: terminal line charts (`fetch_fund.py chart …`) + email sparkline HTML (`:::spark`). |
| `scripts/daily_report.py` | Deterministic three-session (open/mid/close) card report: data → engine → card → email → (close) auto-record. The entry point OpenClaw cron calls. Cards now also render: per-holding 30d NAV sparks (`card_holding_sparks`), holdings 持有天数 column, free 财经要闻 (`card_news`), a cross-sector discovery card (`card_discover`, close session injects `discover=6` by default; `auto_record` **skips** `source=="discovered"` buys so the cron never auto-chases rotating new funds), and a review-annotated operation timeline (`card_timeline` calls `build_trade_reviews(..., save=True)`). `--html [path]` renders the email HTML to a file for browser preview (no send). **`card_wallet`** renders 总钱包(持仓+现金)/可用现金/现金储备线(10%)/定投额度 + a total-return sparkline (`portfolio_return_series`); `_notify` passes `--reason`(`reason_zh`)/`--news`(`relevant_news`)/`--wallet` to trade-notify; `main` auto-runs `fetch_fund.calibrate_costs(apply=True)` each session (skipped in preview). Safety guards: take-profit skip (LET_WINNERS_RUN), stop-loss execute, QDII-buy skip, 7-day same-rule dedup. |
| `scripts/app_config.py` | 应用配置层（P1/P3 基础）。读 `data/app_config.json`（gitignored，可含密钥）+ 环境变量覆盖，回退「离线·无LLM·无同步」安全默认。`mode()`(offline/online)/`is_online()`/`llm_config()`/`sync_config()`/`load_config(force=)`/`reset_cache()`。纯 stdlib。测试在 `tests/test_llm_client.py`。 |
| `scripts/llm_client.py` | LLM 适配层（接 **Anthropic Messages 兼容的三方 API**，纯 stdlib urllib）。`base_url`/`api_key`/`model`/`auth_style`(x-api-key\|bearer) 全来自 `app_config.llm_config()` → 任何 Anthropic 格式网关填配置即用。`is_configured()`/`chat(messages, system, ...)`/`narrate(prompt)`；未配置或网络失败**优雅降级返回 None**（不抛异常、不阻塞）。**红线：LLM 不驱动买卖决策**，仅报告叙事/复盘/问答表达层。`_opener` 注入便于测试。测试 `tests/test_llm_client.py`（12，全 mock）。 |
| `scripts/web_panel.py` | Always-on local web dashboard (stdlib `http.server`, no deps). `start`(后台)/`serve`(前台)/`stop`/`status`; PID in `data/web_panel.pid`. **v2 重构（不再复用邮件 HTML）**：后端只出 JSON（`/api/overview`、`/api/kline`、**`/api/discover`** 懒加载），**性能**：`gather_market_snapshot` 已改 `ThreadPoolExecutor` 并发抓行情/基金快照（首屏 ~9.3s→~1.3s），discover 从总览解耦为懒加载端点（75s/300s/600s 三级缓存），K 线前端失败自动重试 1 次 + 「点此重试」。前端是自包含的响应式单页 `scripts/web_panel.html`（PC+移动端自适应，深色 fintech 风，ECharts 画指数蜡烛 K 线+MA5/MA20+成交量+dataZoom、持仓净值面积图、总收益迷你曲线；点持仓行弹出抽屉看净值+技术信号 RSI/MACD/MA斜率/突破；深链 `/#h=<code>`；`#dbg` 叠加层报告布局宽度便于排障）。`_overview_data` 复用 `daily_report.build_context`（75s 缓存），`_kline_data` 复用 `fetch_fund._resolve_chart_target`+`fetch_nav_series`/`fetch_index_kline`（300s 缓存），账户选择器 `主线` 置顶。前端每 120s 静默 fetch 刷新（不整页 reload）。`--host 0.0.0.0` 暴露到局域网。Conversational triggers「打开面板/关闭面板」。所有异常都进 JSON `error` 字段，绝不 500。 |
| `scripts/update_check.py` | 每日自更新（版本文件比对）。仓库根 **`VERSION`** 维护版本号（每次 push 手动 bump）；`remote_version()` 拉 GitHub raw `VERSION` 与本地比对，不同即有更新 → `check(apply=True)` 在**干净 git clone** 上 `git pull --ff-only origin main`（本地有未提交改动则跳过、只提示）。`due_today()`/`mark_checked()`(`data/.update_check`) 保证每天只真正拉一次。`daily_report.main()` 每天首个时段自动调用（`--no-update` 关闭）。纯 stdlib（urllib + subprocess git），失败绝不阻塞日报。 |

**三时段「今日操作计划」（`daily_report.card_plan`）**: 开盘把可执行操作（买/卖，排除止盈/discovered）预告并落库 `daily_plans`（`db.save_daily_plan`/`get_daily_plan`，自愈表 `_ensure_plan_table`）；盘中/盘尾对比开盘基线 —— 撤销项 `~~划掉~~`+`_explain_dropped` 说明（blocked / 条件不再满足）、维持项 ✅、新增项 🆕，盘尾给「最终就这么操作」确认；每条带 reason + 短C长A 旁注。`send_email._md` 支持 `~~删除线~~`→`<s>`。**盘尾下单确认窗口从 14:48 提前到 14:30**（`SESSIONS`/`market_clock`：盘中 13:00–14:30、盘尾 14:30–15:00；OpenClaw cron 触发时间需同步改到 14:30）。

Decision rules are versioned: `data/decision_tree.json` is the live ruleset and `decision_tree_versions` table stores history with parent/changelog/reason for each version. The `strategy_evolutions` table records before/after metrics when a version is promoted from a backtest.

## Hard rules (these are user-facing contracts; do not weaken them)

- **Never edit `data/portfolio.json` / `data/orders.json` directly.** Use `db.py add-position` / `remove-position` / `add-order`. `add-position` is upsert-style — it accumulates shares and recomputes weighted-average cost for existing holdings.
- **Every buy/sell must call `send_email.py trade-notify`** after the DB write succeeds. The skill prompt enforces this; the Python side does not.
- **Decisions go through `scripts/decide.py`.** At runtime, Claude does not independently apply buy/sell rules — the engine produces a structured decision packet; Claude translates it. If you want to change a rule, change `decision_engine.py` + add a unit test, not `SKILL.md` prose.
- **Stdlib only — including tests.** Tests use `unittest`, not `pytest`. If you find yourself wanting `requests`/`pandas`/etc., use `urllib.request` / built-in `sqlite3` / hand-rolled CSV like the rest of the codebase does.
- **No future leak in `simulate.py`.** Any new feature added to the simulator must only access data dated ≤ the current simulation day.

## Common commands

```bash
# Daily self-update (version-file diff; auto-runs in daily_report first session of day)
python3 scripts/update_check.py --apply        # remote VERSION vs local → git pull --ff-only if newer

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
python3 scripts/fetch_fund.py now                              # R1 会话对表：本机时间/时区/A股时段
python3 scripts/fetch_fund.py market-summary
python3 scripts/fetch_fund.py portfolio-check --account 主线
python3 scripts/fetch_fund.py nav 110011 --days 60
python3 scripts/fetch_fund.py market-snapshot --account 主线 --discover 6  # feed for decide.py (+发现新候选)

# Sector survey → fund discovery → share class (R2/R3/R4/R5)
python3 scripts/fetch_fund.py sector-scan --top 8             # 板块 今日/7日/30日/6月 + 趋势分类
python3 scripts/fetch_fund.py discover --sector 半导体,新能源   # 跨板块下钻挑场外候选（排除持仓）
python3 scripts/fetch_fund.py share-class 006479 --prefer A   # A↔C 兄弟份额（短C长A）

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

# 在线同步（两条路径，皆 stdlib、离线/网络失败优雅降级、不动本地数据）
python3 scripts/sync_client.py ...         # 对接 skill 自己的同步服务器 server.py（:8800，token 鉴权，命名空间隔离）
python3 scripts/web_sync.py sync --account 主线   # ★ 对接 smart-invest-web 平台（:8090）：skill↔web 双向
```

**skill ↔ smart-invest-web 双向同步（`scripts/web_sync.py`）**: smart-invest-web 是那个多用户在线平台（另一个 git 仓库，同级目录，端口 8090）。`web_sync.sync_account()` 用 `app_config.web_config()`（`data/app_config.json` 的 `web` 段：`base_url/email/password/account/wallet`，或 `SMART_INVEST_WEB_*` 环境变量）→ `login()` 换 session token → 把本地账户序列化成 web 的 `{wallet,holdings,trades,cash}` 形态 POST `/api/sync`（**skill 控制 web**）→ web 合并后回传该钱包权威状态（含浏览器/AI 在 web 上产生的新交易）→ `from_web_state` 转回 skill 形态、复用 `sync_client.apply_account` 写回本地（**web 数据回流 skill**）。复用 `sync_client.serialize_account/apply_account`。约定：**被映射的钱包以 skill 账户为持仓/现金权威**（适合 主线 实盘），web 侧新增交易作为历史并回本地；AI 驱动的竞赛钱包不要映射到 skill 账户。测试 `tests/test_web_sync.py`（8，transport 全 mock）。

**Test suite**: `tests/test_decision_engine.py` (engine rules) + `tests/test_decide_cli.py` (CLI smoke) + `tests/test_review.py` (timing verdicts/reviews) + **`tests/test_wallet.py`** (22 tests: `relevant_news`, `portfolio_return_series`, email retry/outbox, operation-report `trade-notify`, `card_wallet`, `calibrate_costs`, web_panel — all mocked) + **`tests/test_upgrade.py`** (21 tests: `market_clock` sessions, `detect_share_class`/`base_fund_name`/`pick_siblings`, `compute_window_returns`/`classify_board_trend`, `discover_candidates`/`score_candidate`, `horizon_for_rule`/`_annotate_horizon`, `_candidate_momentum` — all network mocked) + **`tests/test_update_check.py`** (8 tests: version compare / due-today idempotency / apply-pulls-when-clean / refuse-when-dirty — git+network mocked) + `tests/test_daily_report.py` (incl. `daily_plans` roundtrip + `card_plan` open→close diff with strikethrough) + **`tests/test_news_sentiment.py`** (14: 关键词打分/动态阈值限幅/历史新闻缓存 load+lookup+赛道混合) + **`tests/test_fundamentals.py`** (11) + **`tests/test_llm_client.py`** (12: app_config 加载/env覆盖/Anthropic 兼容 chat/bearer/降级) + **`tests/test_paper_wallet.py`** (3: 虚拟钱包多账户隔离) + others. 289 tests total, all stdlib `unittest`. No linter or build step.

**收益口径（统一）**: 邮件/面板的「持仓收益率 = 持仓浮盈 ÷ 持仓成本，现金完全不计入」。`card_top`/`card_wallet` 显示 本金(budget)/持仓成本/可用现金/持仓收益 四件套；`record_daily_snapshot` 的 `return_pct` 也只用 `positions_value`（**不含现金**，曾误用含现金总资产做分子导致收益率虚高至 200%+，已修）。`card_holdings` 每只展示 今日预估收益 + **持有收益(元)** + 累计% + 市值（不再有恒为空的「昨日盈亏」列）；`send_email` 持仓卡渲染器 cells[3]=持有收益。

**Design docs** (Phase 1+):
- Spec: `docs/superpowers/specs/2026-05-28-smart-invest-overhaul-design.md` — describes the decision-packet contract, rule priority, confidence formula, error handling.
- Plan: `docs/superpowers/plans/2026-05-28-smart-invest-phase1.md` — TDD implementation plan.
- Phase 2-4 roadmap is in the spec's §14 "路线图".

## When the skill prompt asks you to do something

If a Claude Code session has loaded `SKILL.md` and you're asked to act as the smart-invest skill, follow `SKILL.md` literally — it has detailed workflows for "每日分析" (mode A, full report + email + notification), "快速看看" (mode B, quick check, no email), single-fund analysis (mode C), sector analysis (mode D), and "梦境训练" backtesting (mode E). Trade-notify after every buy/sell is non-negotiable.

If you're instead editing this repo's source (the more common case here), treat `SKILL.md` as a spec to keep consistent with code changes — not as instructions to execute.
