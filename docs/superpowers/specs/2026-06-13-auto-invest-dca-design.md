# P7 设计：定投计划（DCA）配置支持

日期：2026-06-13
背景：用户的部分基金已在券商/支付宝开启自动定投（如 006479 每日 ¥10）。目前定投仅对 006479 硬编码处理（`fund_constraints` 限购 + daily_report 跳过买入），不可配置、不入账。需求：让用户可配置任意基金的定投计划，引擎感知并纳入仓位/现金/建仓决策，到期自动记账。

用户已确认的设计选择：
1. 到期自动记账（写交易+累加份额+扣现金+发通知）。
2. 定投基金排除出分批建仓候选（交给定投累积）。
3. 支持周期：每日 / 每周（按星期）/ 每两周 / 每月（按日）。

替用户定的默认：定投基金**所有**买入建议都抑制（分批建仓 + 低吸 + 信号买入），因为这些都是"替你买入这只基金"，与已委托的定投重复；卖出规则（止盈/止损/趋势退出）不受影响。这统一了现有 006479 的"只卖不买"逻辑。

## 架构（沿用既有约束：纯 stdlib、SQLite 单一真相、决策走引擎、无未来函数）

四个改动单元，边界清晰：

### 1. 存储 — 新表 `auto_invest_plans`（db.py）

```sql
CREATE TABLE IF NOT EXISTS auto_invest_plans (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    amount REAL NOT NULL,              -- 每期金额
    frequency TEXT NOT NULL,          -- daily|weekly|biweekly|monthly
    day_field INTEGER,                -- monthly: 1-28(>当月天数顺延月末); weekly/biweekly: 1-7(周一=1)
    anchor_date TEXT,                 -- biweekly 算单双周基准；也是起投日
    platform TEXT DEFAULT '支付宝',
    enabled INTEGER DEFAULT 1,
    note TEXT,
    last_executed_date TEXT,          -- 幂等去重
    created_at TEXT,
    updated_at TEXT,
    UNIQUE(account_id, code)          -- 一只基金一个定投计划
)
```

CLI（db.py 新子命令）：
- `dca add --account 主线 --code 006479 --name "..." --amount 10 --freq daily [--day N] [--anchor YYYY-MM-DD] [--platform 支付宝] [--note ...]`
- `dca list --account 主线`
- `dca remove --account 主线 --code 006479`
- `dca toggle --account 主线 --code 006479 [--on|--off]`

DB 方法：`add_dca_plan`(upsert by account+code)、`get_dca_plans(account_id, enabled_only=True)`、`remove_dca_plan`、`set_dca_enabled`、`set_dca_last_executed`。

### 2. 到期判定 — 纯函数 `scripts/auto_invest.py`

`is_due(plan, today, nav_available)` → bool。按"周期键"去重，自动处理周末/节假日顺延：

- **周期键**：daily→`YYYY-MM-DD`；weekly→ISO 年-周；biweekly→`(today - anchor).days // 14`；monthly→`YYYY-MM`。
- **已执行去重**：若 `last_executed_date` 落在与 today 相同的周期键内 → 未到期（本周期已投）。
- **本周期内是否已到触发点**：
  - daily：恒 True
  - weekly：`today.isoweekday() >= day_field`（当周一旦到/过设定星期即触发，周内节假日顺延到本周下个交易日）。**day_field 限 1–5（交易日）**：周末无交易日，跨周顺延会破坏按周去重，故不支持周六/周日定投——真实平台也只在交易日扣款。
  - biweekly：当前双周序号为"投资周期"（`((today-anchor).days//7) % 2 == 0`，以 anchor 所在周为第 0 周）且 `today.isoweekday() >= day_field`
  - monthly：`today.day >= min(day_field, 当月天数)`
- **nav_available**：False（非交易日，NAV 取不到）→ 不到期。daily_report 仅交易日由 cron 调用，天然满足。

辅助：`due_plans(plans, today, nav_lookup)` 返回今日到期计划列表；`record_due_plans(db, account_id, account_name, today, funds, do_email)` 执行记账（依赖注入 funds 快照取 NAV，便于测试）。

### 3. 引擎感知 — decision_engine.py

`market_data["auto_invest_codes"]`（list[str]，沿用 index_trend 传参模式；回测/无定投时为空，行为不变）：
- `_try_position_build`：候选过滤增加 `code not in auto_invest_codes`。
- `_evaluate_rules` 买入 Pass：`code in auto_invest_codes` 时跳过 low_buy / signal_buy（不进 actions 也不进 blocked——这是用户主动委托，非被拦截）。
- `_compute_portfolio_advice`：若有定投，advice_zh 追加"（含定投自动投入，仓位会随定投自然提升）"。
- 卖出 Pass 完全不变。

引擎从 `market_data` 读取，不直接查 DB（保持可测试 + 回测纯净）。

### 4. 接线 — fetch_fund / decide / daily_report

- `fetch_fund.gather_market_snapshot`：查 `get_dca_plans(account_id)`，把 enabled 计划的 code 放进 `snapshot["auto_invest_codes"]`。
- `decide.py` / `daily_report.build_context`：snapshot 已带该字段，引擎自动感知，无需额外改动。
- `daily_report` 盘尾(close)：在 `auto_record` 之前调用 `auto_invest.record_due_plans(...)`，记账定投并发通知。

## 数据流

```
cron → daily_report --session close
  → gather_market_snapshot（注入 auto_invest_codes）
  → record_due_plans（到期定投记账：写交易+累加持仓+扣现金+通知+记 last_executed_date）
  → engine.decide（定投基金不出买入建议，仓位概览含定投说明）
  → auto_record（引擎的非定投买卖）
  → 卡片邮件
```

## 错误处理

- 定投基金当日 NAV 取不到 → 该计划本次跳过（非交易日或数据故障），下个交易日 today.day 仍 ≥ 设定点且本周期未执行 → 自动补投，不漏不重。
- amount > 现金 → 仍记账（真实定投券商也会扣，余额不足是用户侧问题），但现金可能转负 → daily_report 记 warn 提示补充现金校准。
- 月投 day_field=31 而当月 30 天 → `min(31, 30)`，月末触发。

## 测试

- `tests/test_auto_invest.py`：四周期 is_due 触发/不触发边界、周期键去重（同周期不重复）、周末顺延（设周六实际下周一交易日执行）、月末顺延（day=31 在 2 月触发于月末交易日）、biweekly 单双周、enabled=0 跳过、nav 不可得跳过、record_due_plans 写库+扣现金+set last_executed。
- 引擎：定投基金不进 position_build；low_buy/signal_buy 对定投基金静默跳过；portfolio_advice 文案含定投；非定投基金行为不变（既有测试全绿）。
- CLI 冒烟：dca add/list/remove/toggle。

## 风险与非目标

- 不支持"定投在涨时暂停/智能定投"等券商高级策略——只记直投。
- T+1/T+2 确认按当日 NAV 成交，与现有记账口径一致。
- 不回测定投（定投是真实账户配置，不进 strategy_lab；simulate 不读该表）。
