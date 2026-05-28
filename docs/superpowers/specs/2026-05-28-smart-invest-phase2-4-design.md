# Smart-Invest Phase 2-4 合并设计

**日期**: 2026-05-28（紧接 Phase 1 之后）
**前置**: `2026-05-28-smart-invest-overhaul-design.md` 的 Phase 1 已落地（commits `2dbc3cb..2e62109`）
**作者**: Claude（用户授权"自己规划别问我"）

---

## 1. 为什么合并

Phase 1 spec §14 路线图原本要求每个 Phase 启动前重新 brainstorm 并要求 P1 "至少跑过 1 周实盘"。但：

1. 用户的最终目标是「完整改造」，分阶段交付而不交付全部，不满足"完整"。
2. "跑过 1 周实盘"在当前会话里物理上不可能验证 — 这是上线后的观察期，不是开发期的依赖。
3. 用户直接授权"你自己规划别问我"，所以跳过逐 phase brainstorm 仪式。

合并的代价：P3 的新规则原本要先在 P2 验证胜率才入主线。本次 Phase 3 **只加观测信号（context 字段），不加新规则** — 保留这条纪律。

## 2. Phase 2：回测-进化闭环

### 2.1 问题陈述

Phase 1 让实盘走引擎，但 `simulate.py` 内部还有一套独立的规则代码（第 14 章列出）。两套规则不一致 → 回测验证的不是实盘策略。

`strategy_evolutions` 表存在但永远是空 — 没有任何东西自动写它。

### 2.2 目标终态

1. `simulate.py run --engine` 让回测每天调 `DecisionEngine.decide()` 拿决策包，按包里的 actions 执行交易。
2. 回测结束时，每笔交易写入 `trades` 表，含 `rule_id` 字段（trades 表已经有这字段）。
3. `decide.py stats --account <name>` 输出按规则分组的统计：
   ```
   rule_id              触发次数    胜率     平均收益   平均亏损   期望
   low_buy                  42      62%      +3.4%      -2.1%    +1.30%
   take_profit_tier_20      18      94%      +21.2%      -0%    +19.93%
   emergency_stop_loss       6      67%      +1.1%     -2.8%    -0.20%
   ```
4. `decide.py evolve --from-sim <sim_id>` 把这些统计写入 `strategy_evolutions` 表，并打印改进建议（哪些规则期望为负、哪些规则触发次数太少不可信、哪些规则胜率高可以加权）。

### 2.3 实现要点

- **新方法 `DecisionEngine.compute_rule_stats(account_id)`** — 纯 SQL，从 `trades` 按 `rule_id` 聚合。`profit_pct` 字段已经在 trades schema 里（Phase 1 之前就有）。卖出时计算并存。
- **simulate.py 改造范围最小化** — 现有 `Simulator` 类不动；新增 `_engine_step(day, market_window)` 方法，`--engine` 旗标走这条路；旧路径保留兼容性。
- **不强制 simulate.py 当天就完全迁移到引擎驱动** — 复杂工程（要构造每天的 market_data 切片，避免未来函数）。本次先做 stats + evolve，让用户可以查看现有回测数据的规则胜率；engine 模式作为 opt-in 验证。

### 2.4 P2 非目标

- 不重写 `simulate.py` 的非引擎路径
- 不实现自动调阈值（如 "low_buy 期望负，自动改为日跌 4% 才触发"）— 这是 P5 工作

## 3. Phase 3：信号扩展（只加观测，不加规则）

### 3.1 问题陈述

当前规则只看简单回报序列（日 / 5 日 / 20 日）。专业投资者参考的指标（RSI、MACD、MA 斜率、突破）完全缺失。

### 3.2 目标终态

1. 新文件 `scripts/signals.py` 实现 4 个纯函数：
   - `compute_rsi(navs, period=14)` → 0-100
   - `compute_macd(navs, fast=12, slow=26, signal=9)` → (macd_line, signal_line, hist)
   - `compute_ma_slope(navs, window=20, lookback=5)` → 斜率%（每天均线变化的均值，正=上行）
   - `compute_breakout(navs, lookback=20)` → bool（当前价 > 近 20 天最高）
2. `fetch_fund.gather_market_snapshot` 为每只基金加上 `rsi_14`, `macd_hist`, `ma20_slope`, `breakout_20d` 字段。
3. 决策包 `actions[].context` 显示这些信号（让 Claude 在报告里展示，但不影响 buy/sell 决定）。
4. 设计文档新版（v2.1）记录"P3 加了这些信号但还没接入规则；P3.5/P5 在回测验证后接入"。

### 3.3 P3 非目标

- 不加任何新规则（如 "RSI < 30 加大 low_buy 仓位"）
- 不引入资金流（北向 / 主力净流入）— P5 范围
- 不引入新闻情绪 NLP — P5 范围（SKILL.md 仍用 WebSearch 拉新闻）

## 4. Phase 4：UX 深化

### 4.1 目标终态

1. **`decide.py why-not --code <code>`** — 直接答"为什么没建议买 XXX"。引擎跑完后查 `blocked_actions[code]`，没有则查 `actions[code]`，最后查市场数据是否存在。
2. **`decide.py run --brief`** — 3-5 行 Markdown 摘要，用于 SKILL.md 模式 B "快速看看"。当前完整 md 输出对快速场景过长。
3. **`send_email.py setup --interactive`** — 引导式问答，逐字段提示+示例（QQ 邮箱授权码示例链接、收件人邮箱格式校验）。
4. **错误消息**：`send_email.py` 现有报错"配置文件不存在"改为带行动建议的形式（"运行 `python3 send_email.py setup --interactive`"）。

### 4.2 P4 非目标

- 邮件 HTML 模板不重做 — 现有的支付宝风格已经够好，用户最近也没抱怨
- 不做命令行 dashboard
- 不做 Web UI

## 5. 完工标准（合并验证）

1. 所有 Phase 1 测试仍绿（≥ 27）。
2. 新增测试：`tests/test_rule_stats.py`、`tests/test_signals.py`，全绿。
3. `scripts/signals.py` 至少 4 个函数全部有单元测试。
4. `decide.py stats` `decide.py evolve` `decide.py why-not` 都至少有一个 smoke 测试。
5. `--brief` 输出 < 10 行且包含决策摘要。
6. `simulate.py --engine` 在合成数据上跑一遍不崩溃（不要求结果好，只要求接口通）。
7. SKILL.md 加 §X "信号字段速查" + §Y "why-not / brief 调用方式"。

## 6. 风险与回滚

- **simulate.py engine 模式可能有未来函数 bug** — 严格按"day d 只用 ≤ d 的数据"。如果发现，**关闭 `--engine` 旗标，旧路径不动**。
- **signals.py 计算成本** — 每基金每次需要 26+ 天 NAV 历史。`gather_market_snapshot` 已经拉了 25 天历史，复用即可，0 额外网络。
- **回滚**：所有改动通过新文件/新方法/新旗标进行，旧路径都保留。

---

## 7. 路线图后续（P5+）

| Phase | 范围 | 启动条件 |
|---|---|---|
| **P5 信号→规则升级** | 在 P3 信号上构造新规则（如 RSI+低吸联合触发、MACD 死叉减仓），先在回测有正期望证据才入主线 | P2 stats + P3 信号双双稳定 |
| **P6 资金流与新闻** | 北向资金、主力净流入、新闻情绪 NLP | 用户实盘半年后再评估必要性 |
| **P7 Web UI** | 浏览器看板 | 用户主动要求时 |
