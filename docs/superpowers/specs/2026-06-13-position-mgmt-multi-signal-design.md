# P6 设计：总仓位管理 + 多信号买卖 + 可照抄下单指令

日期：2026-06-13
背景：用户反馈三个核心痛点 —— ① 策略单一（唯一买入规则 low_buy 要求当日跌 ≥3%，几乎从不触发，导致"skill 基本不让我操作什么"）；② 引擎完全不关心总仓位（主线 ¥50,000 预算只持有 ~24% 仓位，没有任何规则把仓位推向目标区间）；③ 已算好的技术信号（RSI/MACD/MA 斜率/20 日突破）没有接入任何规则，市场监控与买卖判断能力薄弱。

目标：在不破坏既有契约（决策走 decide.py、规则改动须过梦境实验室 ≥2 个市况窗口、纯 stdlib、无未来函数）的前提下，让引擎：
1. 每次决策都给出 **总仓位评估**（当前 vs 目标区间）并在仓位不足时产生 **分批建仓** 买入动作；
2. 用 RSI / 20 日突破等信号补充买卖触发，不再只靠"当日跌 3%"；
3. 输出 **可直接照抄执行的下单指令**（含 006479 限购 ¥10/天 这类真实约束）。

## 方案选择

考虑过三个方向：
- A. 只改 SKILL.md 提示词，让 Claude 在报告里自行补充仓位建议 —— 违反"决策走引擎"契约，不可审计、不可回测，否决。
- B. 在 daily_report.py 后处理阶段叠加仓位逻辑 —— 决策逻辑散落两处，回测覆盖不到，否决。
- **C（采纳）. 引擎内新增规则族 + 决策包新增 portfolio_advice 块**，规则参数进 decision_tree.json 受版本管理，由梦境实验室验证后晋升 v2.2 启用。渲染层（decide.py / daily_report.py）只做翻译。

## 新规则族（全部在 decision_engine.py，配置键缺省=禁用，保证 v2.1 行为不变）

### R1 position_management（分批建仓 —— 解决"不关心总仓位"）

```json
"position_management": {
  "enabled": true,
  "target_floor": {"牛市": 0.70, "震荡市": 0.50, "熊市": 0.30},
  "tolerance": 0.05,
  "batch_fraction": 0.10,
  "max_funds_per_batch": 2,
  "min_order_amount": 300
}
```

- 触发：`position_pct < target_floor[regime] - tolerance` 且 regime ∉ {unknown}。熊市只允许对已持仓加仓（沿用 _check_market_allows_buy）。
- 部署额：`min(gap, batch_fraction) × total_value`，且不得使现金跌破 10% 储备线。
- 候选排序：对快照中所有基金按动量打分（fund_20d_return 为主），剔除 5 日涨幅 >10%（anti_chase）、参考指数在 200 日线下方的（趋势闸门复用 trend_filter，破位则金额减半而非剔除）、单只/赛道将超限的；取前 max_funds_per_batch 只均分。
- 每个候选走既有五项 check；产出 action `rule_id=position_build`，`rule_label=分批建仓`。
- 单笔 < min_order_amount 时丢弃（避免碎单）。

### R2 signal_buy（RSI 超卖低吸 —— 补充 low_buy）

```json
"signal_rules": {"rsi_buy": {"enabled": true, "threshold": 32, "amount_ratio": 0.03}}
```

- 触发：`signals.rsi_14 ≤ threshold` 且该基金当日未触发 low_buy（day_return > -3%，否则让位）且未触发卖出。
- 金额 `total_value × amount_ratio`，走全部五项 check + 趋势闸门减半。`rule_id=rsi_oversold_buy`。

### R3 momentum_breakout（20 日突破顺势买 —— 牛市里也有买点）

```json
"signal_rules": {"breakout_buy": {"enabled": true, "amount_ratio": 0.03}}
```

- 触发：`signals.breakout_20d == true` 且 `signals.ma20_slope > 0` 且 regime ∈ {牛市, 震荡市} 且 anti_chase 通过（5 日 ≤10% 天然限制追高）。
- `rule_id=momentum_breakout`。与 R2 互斥（同一基金同一天最多一条买入 action，优先级：low_buy > rsi_oversold_buy > momentum_breakout > position_build）。

### R4 rsi_trim（RSI 超买减仓 —— LET_WINNERS_RUN 的软保护）

```json
"signal_rules": {"rsi_trim": {"enabled": true, "threshold": 82, "min_profit": 0.15, "sell_fraction": 0.20}}
```

- 触发：持仓 `signals.rsi_14 ≥ threshold` 且浮盈 ≥ min_profit。排在 trend_exit 之后、take_profit 之前。
- v2.1 已关闭分层止盈，此规则是否净增益完全交由实验室判定，回测不支持就不晋升。

### R5 position_cap_trim（超配回撤 —— 总仓位上限执行）

- 触发：`position_pct > regime.position_cap + tolerance` → 卖出超配比例最高的一只，把总仓位拉回 cap 内。`rule_id=position_cap_trim`。与 position_management 同一配置块（enabled 共用）。

### fund_constraints（真实世界限购）

```json
"fund_constraints": {"006479": {"max_daily_buy": 10, "note": "QDII 限购"}}
```

- 引擎对任何 buy action 的 suggested_amount 做最终裁剪：`min(amount, max_daily_buy)`；裁剪后 < min_order_amount 的 position_build 候选直接跳过换下一名（006479 限购 ¥10 不该占用建仓名额）；low_buy/rsi_buy 对它仍可输出 ¥10 指令（用户已定投，照抄即可）。

## 决策包新增块

```json
"portfolio_advice": {
  "position_pct": 0.24, "target_floor": 0.50, "position_cap": 0.85,
  "gap_amount": 13000.0, "deployable_cash": 30000.0,
  "status": "underweight|in_band|overweight",
  "advice_zh": "当前仓位 24%，低于震荡市目标下限 50%，建议分批部署约 ¥13,000。"
}
```

无论是否产生 action，每个决策包都带此块 —— 用户每天都能看到总仓位状态（解决"不关心总仓位"的感知问题）。

## 渲染层（decide.py / daily_report.py / send_email）

- md/brief 格式头部新增「📊 仓位概览」行：`仓位 24% | 目标 50%~85% | 可部署 ¥30,000`。
- 每条 buy/sell action 渲染「可照抄指令」：`📋 在天天基金/支付宝搜索 006479，买入 ¥10`（卖出给份额+约当金额）。
- daily_report 邮件卡片加仓位概览块。

## 回测与晋升路径

1. simulate.py `_build_market_data_for_engine` 给每只基金附 `signals = attach_signals(navs ≤ date)`（无未来函数）；基金 NAV 预加载向前多拉 90 天，保证窗口首日就有 RSI/MACD（trading_days 仍只取窗口内）。
2. strategy_lab.make_variants 新增（以 **v2.1 现行规则** 为基线）：`baseline-v2.1`、`position-mgmt`、`signal-buys`（R2+R3）、`full-arsenal`（R1+R2+R3+R5）、`full-plus-trim`（再加 R4）。
3. 在 ≥2 个不同市况窗口跑 lab（计划：2024-10~2025-04 与 2025-06~2026-06）；冠军在两个窗口都不输 baseline-v2.1 才 `--promote v2.2`。
4. 晋升失败的子规则保持 disabled，留在代码里等下次实验。

## 测试

- tests/test_position_rules.py：建仓触发/不触发、tolerance、熊市行为、现金储备约束、限购裁剪、cap_trim。
- tests/test_signal_rules.py：R2/R3/R4 触发边界、与 low_buy 互斥优先级、signals 缺失时静默跳过。
- 既有 25 个引擎测试必须全绿（新键缺省禁用 ⇒ 行为不变）。

## P6.1 修订（2026-06-13 回测后追加）

三窗口初轮证据（budget ¥20,000，得分 = 年化 + 0.5×回撤）：

| 变体 | A 2025-26 牛市 | B 2024-25 震荡 | C 2023-24 下跌 | 合计 |
|---|---|---|---|---|
| baseline-v2.1 | **68.75** | -11.52 | **1.06** | 58.29 |
| v21-position-mgmt | 65.90 | -5.45 | -8.16 | 52.29 |
| v21-signal-buys | 64.70 | -5.14 | -15.80 | 43.76 |
| v21-full-arsenal | 60.18 | **-3.13** | -22.10 | 34.95 |
| v21-full-plus-trim | 57.55 | -1.02 | — | — |

结论：P6 规则把 B 窗口的亏损年（基线 -4.06%）扭成正收益，但在 C 下跌年因
趋势线下持续建仓而大幅亏损（减半闸门不够）。修订：新增
`position_management.require_trend_above` 与 `signal_rules.require_trend_above`
—— HS300 不在 200 日线上方时建仓/信号买入**完全停火**（不是打折）。
变体 v21-pm-gated / v21-arsenal-gated 重测三窗口，晋升标准不变：
合计得分超过 baseline-v2.1 且 C 窗口不再显著落后才晋升 v2.2。

## 风险

- 公募基金 T+1 确认、QDII T+2，回测按当日 NAV 成交略乐观 —— 与现状一致，不在本期修。
- 信号基于净值而非盘中价，RSI 阈值对场外基金偏钝 —— 阈值放进配置，由实验室调。
