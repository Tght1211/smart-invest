# Smart-Invest 全面改造设计 — Phase 1：决策引擎接管

**日期**: 2026-05-28
**作者**: Claude（按用户「自己规划别问」的授权）
**范围**: 仅 Phase 1（T1 决策引擎接管实盘 + T4 顺手精简 SKILL.md）。Phase 2-4 在「路线图」节列出，启动前各自再做一次 brainstorm。

---

## 1. 用户目标

用户原话：「用 superpowers 帮我完整改造优化完善当前 skill，让 skill 更加准确，市场决策判断更加准确，让我可以更加稳定的赚到更多钱！并且交互体验上要更加友好！」

排序：**决策稳定 > 准确度 > UX**。

## 2. 当前架构的结构性缺陷（即改造动机）

1. **决策是「Claude 凭印象应用规则」** — `SKILL.md` 第七节列出几十条规则（现金 <10% 禁买、单只 >25% 禁买、单日跌 >7% 卖 50% 等），但实际执行时是 Claude 用自然语言"理解并应用"。同样的市场状态，不同次输出的建议可能在阈值边界、规则触发顺序、加码倍率上有差异。**不可复现 = 不可信赖**。
2. **`decision_engine.py` 已存在但未被使用** — 文件里有 `check_buy_preconditions` / `check_stop_loss` / `check_take_profit` / `check_low_buy` 四个函数，但 `SKILL.md` 的工作流从未调用它们。这是一个"半成品骨架"。
3. **回测与实盘是两套规则实现** — `simulate.py` 内置了自己的一套止损/止盈/低吸代码（第 14 章列出），和 `decision_engine.py` 不共享。回测验证的不是实盘策略。
4. **`strategy_evolutions` 表为空** — 数据库表都建了，但没有任何东西往里写。"策略持续进化"是张白卷。
5. **`SKILL.md` 1098 行** — 模式 A/B/C/D/E 描述重叠、首次邮件引导穿插在中间、Cron 字面命令在 11.1 节又出现一遍、风控红线在 7.6 和 6.1 重复列出。Claude 读这种长 prompt 容易丢细节。

## 3. Phase 1 目标终态

**一个新入口**：`python3 scripts/decide.py run --account 主线 [--date YYYY-MM-DD]`

**输出**：结构化「决策包」JSON（schema 在 §6 详述），同时支持 `--format md` 输出人类可读摘要。

**SKILL.md 的全部分析模式**统一改为：
1. 调 `decide.py` 拿决策包
2. 调 `fetch_fund.py` 取补充数据（板块、新闻搜索）
3. 把决策包翻译成中文报告
4. 发邮件（如需要）

**决策本身在引擎里定死**，Claude 不再"自己判断买不买、卖多少"。Claude 只负责：解释、补充市场叙事、写风险提示、调度通知。

## 4. 非目标（明确排除在 Phase 1）

- 不引入新数据源（新闻 API、雪球、蛋卷）— Phase 3 范围。
- 不加技术指标（RSI/MACD/MA 斜率）— Phase 3 范围。
- 不重写 `simulate.py` 的内部逻辑（只在 Phase 2 让它复用 engine）。
- 不改邮件 HTML 模板。
- 不加新的 cron 报告类型。
- 不动数据库 schema（除了 `decision_tree.json` 加字段）。

## 5. 组件清单

| 文件 | 类型 | 改动 |
|---|---|---|
| `scripts/decision_engine.py` | 重写 | 新增统一 `decide(account, date, market_data, positions, cash, total_value)` 入口；补全 take_profit 分档、rebalance、drawdown_protection、cash_deploy；统一返回 §6 的决策包结构。旧 4 个 check_* 函数保留为内部 helper。 |
| `scripts/decide.py` | 新建 | 薄壳 CLI（~150 行）：调 `fetch_fund.market_snapshot` 拿数据 + 调引擎 + 输出 JSON 或 Markdown。 |
| `scripts/fetch_fund.py` | 增量 | 新增 `market-snapshot --account X [--date YYYY-MM-DD]` 子命令：单次调用聚合引擎所需全部输入。 |
| `data/decision_tree.json` | schema 扩展 | 每条规则加 `id`（slug，如 `cash_reserve`）、`enabled`（bool，便于禁用单条）、`severity`（`block`/`warn`/`info`）。保留旧字段。 |
| `SKILL.md` | 重写 | 从 1098 行降到 ≤ 700 行。结构精简：模式表压缩成 1 张；首次引导独立成 1 节并前置；删除"Claude 凭理解应用规则"的整章（第七节），换成"调 decide.py 阅读决策包"流程；定时报告章节合并冗余。 |
| `tests/test_decision_engine.py` | 新建 | 单元测试，pytest（也兼容 `python3 -m unittest`），覆盖 §10 列出的核心场景。 |
| `tests/conftest.py` | 新建 | 共享 fixtures：内存 SQLite、合成市场数据、合成持仓。 |
| `CLAUDE.md` | 增量更新 | 加一节"决策入口：所有决策必须经过 decide.py"。 |
| `docs/superpowers/specs/...` | 新建 | 本文件 + 后续 Phase 设计文档。 |

## 6. 决策包 schema（核心契约）

```jsonc
{
  "schema_version": "1.0",
  "generated_at": "2026-05-28T14:30:00+08:00",
  "account": "主线",
  "date": "2026-05-28",
  "rule_version": "v2.0",

  "market_regime": {
    "label": "震荡市",                // "牛市" | "震荡市" | "熊市"
    "hs300_5d_return": -0.012,
    "hs300_20d_return": 0.018,
    "position_cap": 0.85,             // 当前环境下的总仓位上限
    "single_cap": 0.25,               // 单只上限
    "stop_loss_threshold": -0.12      // 当前环境的成本止损阈值
  },

  "portfolio_snapshot": {
    "total_value": 28300.18,
    "cash": 6500.00,
    "cash_pct": 0.2297,
    "position_value": 21800.18,
    "position_pct": 0.7703,
    "sectors": {"科技": 0.45, "海外": 0.30, "宽基": 0.02},
    "by_position": [
      {"code": "512480", "name": "半导体ETF国联安",
       "shares": 2129.79, "cost_nav": 2.3432, "current_nav": 2.31,
       "value": 4919.81, "pct_of_total": 0.1739,
       "profit_pct": -0.0142, "hold_days": 2}
    ]
  },

  "actions": [
    {
      "code": "512480", "name": "半导体ETF国联安",
      "action": "buy",                // "buy" | "sell" | "hold" | "watch"
      "rule_id": "low_buy",
      "rule_label": "低吸",
      "confidence": 0.78,             // 0-1, 见 §7
      "suggested_amount": 1500.0,     // 元；sell 时为份额对应金额
      "suggested_shares": null,       // sell 时填份额，buy 时为 null
      "context": {                    // 触发这个决策的关键市场数据
        "fund_5d_return": -0.06,
        "fund_day_return": -0.034,
        "hs300_5d_return": -0.012
      },
      "checks_passed": [
        {"id": "cash_reserve", "actual": 0.22, "threshold_min": 0.10},
        {"id": "single_position", "actual": 0.17, "threshold_max": 0.25},
        {"id": "sector_concentration", "sector": "科技",
         "actual": 0.45, "threshold_max": 0.50},
        {"id": "anti_chase", "actual": -0.06, "threshold_max": 0.10},
        {"id": "market_regime", "label": "震荡市"}
      ],
      "checks_failed": [],
      "reason_zh": "符合低吸规则：当日跌 3.4%（>3%），近 5 天跌 6%，大盘震荡非熊市。现金 22%、单只仓位 17%、科技赛道 45% 均在阈值内。"
    }
  ],

  "blocked_actions": [
    {"code": "540010", "name": "汇丰晋信科技先锋股票",
     "attempted_action": "buy",
     "blocked_by": "sector_concentration",
     "reason_zh": "科技赛道已占 45%，加上目标基金会突破 50% 上限。"}
  ],

  "alerts": [
    {"severity": "warn", "id": "drawdown",
     "reason_zh": "组合从峰值回撤 8.2%，接近 10% 减仓阈值。"}
  ],

  "summary": {
    "action_count": {"buy": 1, "sell": 0, "hold": 5, "watch": 2},
    "highest_confidence_action": {"code": "512480", "action": "buy", "confidence": 0.78}
  }
}
```

### 关键设计选择

- **decisions 是建议而非自动执行**。`decide.py` 只输出包，不写库。买卖落库仍走 `db.py add-order` / `add-position`（由 Claude 在用户确认后调用）。这保留了"人在回路"。
- **`reason_zh` 必填**。引擎层就生成中文解释，Claude 直接用，不需要"理解后翻译"。这是核心 — 把"Claude 自由发挥"压缩到最小。
- **`confidence` 让 Claude 知道该多大声音地推荐**。低置信度（<0.5）的建议在报告里降级为"可观望"，高置信度（>0.7）才说"建议买/卖"。
- **`blocked_actions` 显式列出"为什么没买" — 透明度**。

## 7. confidence 计算公式（v1.0）

```
buy 类决策:
  base = 0.5
  + 0.15 if fund_5d_return < -0.05            (超跌)
  + 0.10 if hs300_5d_return > 0.03            (大盘转暖)
  + 0.10 if hold_days==0 (新建仓) and sector_concentration < 0.3
  + 0.05 if single_position < 0.10            (轻仓加码空间大)
  - 0.10 if 4 < hold_days < 10                (刚买完短期内再买，避免摊薄迷恋)
  clamp [0, 1]

sell 类决策:
  base = 0.6  (止损止盈普遍更确定)
  + 0.20 if profit_pct >= 0.40                (高位止盈)
  + 0.20 if profit_pct <= -0.15               (大幅止损)
  + 0.10 if drawdown_from_peak > 0.10
  clamp [0, 1]

hold/watch:
  null (无意义)
```

这是 v1.0 的简单线性加权。Phase 2 用回测胜率数据校准权重。

## 8. 规则优先级冲突

引擎内执行顺序（同一只基金多条规则同时触发时只执行最高优先级一条）：

1. `emergency_stop_loss`（单日 >7% / 3 日 >10%）→ sell 50%
2. `absolute_stop_loss`（亏 >20%）→ sell 100%
3. `time_based_stop_loss`（持有期分档亏损）→ sell 50%
4. `take_profit_tier`（+20/30/40/50% 分档）→ sell 25-100%
5. `drawdown_protection`（组合回撤 >10%）→ 减仓至 40% 现金（账户层，非单只）
6. `low_buy`（低吸）→ buy
7. `rebalance`（再平衡）→ buy or sell
8. `cash_deploy`（现金部署）→ buy

如果同一只既触发买又触发卖，**卖优先**（风控优于进攻）。

## 9. 错误处理与降级

| 场景 | 引擎行为 |
|---|---|
| 某只基金 NAV 拉不到 | 该只跳过决策，决策包 `actions` 不包含它，`alerts` 加一条 `data_missing` |
| 指数数据拉不到（沪深 300） | `market_regime.label = "unknown"`，所有 buy 决策降级为 `watch`（不敢动） |
| 数据库为空（无账户） | `decide.py` 退出码 2，stderr 提示"先 init + add-position" |
| 决策树版本不存在 | 引擎从 `data/decision_tree.json` 兜底（与现状保持） |
| 引擎抛异常 | `decide.py` 捕获，输出 `{"error": "...", "traceback": "..."}` JSON，退出码 3 |

**Claude 收到决策包**：先看是否有 `error` 字段，有则告知用户并暂停后续步骤；否则正常翻译。

## 10. 测试矩阵（`tests/test_decision_engine.py`）

每条都用合成 fixture，确保**输入 → 输出**确定性：

| 测试 | 输入 | 期望 |
|---|---|---|
| `test_cash_reserve_blocks_buy` | 现金 5%，尝试 buy | `blocked_actions` 含 `cash_reserve` |
| `test_single_position_cap_blocks_buy` | 单只已占 26%，尝试加仓 | `blocked_actions` 含 `single_position` |
| `test_sector_concentration_blocks_buy` | 科技已 48%，目标也是科技 | `blocked_actions` 含 `sector_concentration` |
| `test_anti_chase_blocks_buy` | 基金近 5 天涨 12% | `blocked_actions` 含 `anti_chase` |
| `test_low_buy_triggers` | 跌 3.5%、近 5 天跌 6%、现金 20% | `actions` 含 buy with `rule_id=low_buy` |
| `test_low_buy_boosted` | 跌 5.5%、近 5 天跌 9%、大盘当日跌 2.5% | suggested_amount 是基础的 2 倍 |
| `test_emergency_stop_loss` | 单日跌 7.5% | `actions` 含 sell 50% with `rule_id=emergency_stop_loss` |
| `test_absolute_stop_loss` | 亏 22% | `actions` 含 sell 100% with `rule_id=absolute_stop_loss` |
| `test_take_profit_tier_20` | profit 22% | sell 25% |
| `test_take_profit_tier_30` | profit 32% | 再 sell 25%（累计 50%）|
| `test_take_profit_clearout` | profit 52% | sell 100% |
| `test_drawdown_protection` | 组合回撤 11% | `alerts` 含 `drawdown`，全部 buy 决策被改为 watch |
| `test_bear_market_blocks_new_position` | hs300 20d -12%，无该基金持仓 | blocked `bear_market_new_position` |
| `test_bear_market_allows_low_buy_existing` | hs300 20d -12%，已持有该基金 | low_buy 仍允许，但 suggested_amount × 0.5 |
| `test_data_missing_skips` | 某基金 NAV 为 None | 该基金不出现在 actions，alerts 含 data_missing |
| `test_confidence_buy` | 标准低吸 + 超跌 | confidence ≥ 0.65 |
| `test_confidence_sell` | profit 45% 触发止盈 | confidence ≥ 0.80 |
| `test_priority_sell_over_buy` | 同一只同时触发止盈和低吸（不太可能但要 cover）| 只生成 sell |
| `test_decide_packet_schema` | 任何输入 | 输出包含所有 §6 列出的顶级字段，类型正确 |

至少 19 个测试。新建时 `python3 -m pytest tests/ -v` 全绿是 Phase 1 的完工标准。

## 11. SKILL.md 重构纲要（顺手 T4 精简）

**目标行数**：≤ 700 行（从 1098）。

**新结构**：

```
0. 角色与原则（30 行）
1. 触发场景（用户怎么唤起）—— 1 张表压缩 60 行 → 25 行
2. 决策入口（核心）—— 全部分析模式统一调 decide.py
3. 持仓与交易管理 —— 保持，但删除 step-by-step 模板（让 Claude 看引擎包决定）
4. 邮件首次引导 —— 独立 1 节，前置
5. 定时报告体系（午/晚/周/月）—— 4 个报告合并表格+共用模板，删重复
6. 梦境训练 —— 保留入口，标注"内部逻辑见 Phase 2 计划"
7. 注意事项 + 风控红线 —— 合并去重
```

**删掉**：
- §一-B 模式详解 (与 §11.x 重复)
- §五 工作流 step-by-step（引擎接管后不需要）
- §七 投资建议策略详表（搬到 decision_tree.md，SKILL.md 只引用）
- §11.3 手动触发判断逻辑（引擎吃所有路径，不再需要意图判定 if-else）

**保留**：
- 中文输出原则
- 报告 Markdown 模板（用户视觉契约）
- 邮件触发规则（哪些模式发邮件）
- 数据来源 + 风险提示语

## 12. 兼容性 / 回滚策略

- **旧通路不删** — `decision_engine.py` 的旧 4 个函数保留为内部 helper，原 CLI `decision_engine.py analyze` 子命令保留。`fetch_fund.py portfolio-check` 保留。
- **decision_tree.json 加字段不删字段** — 旧版引擎兼容读。
- **回滚**：`git revert` 单次提交即可。Phase 1 所有改动放在 1 个 PR / 1 个 commit。
- **数据库 schema 不动** — 决策包不入库，只是 stdout JSON。Phase 2 再决定要不要落 `daily_decisions` 表。

## 13. Phase 1 验证清单（完工标准）

1. `python3 -m pytest tests/ -v` 全绿（≥ 19 测试）
2. `python3 scripts/decide.py run --account 主线` 输出合法 JSON，schema 匹配 §6
3. `python3 scripts/decide.py run --account 主线 --format md` 输出可读 Markdown
4. SKILL.md 行数 ≤ 700，目录与 §11 一致
5. `python3 scripts/db.py positions --account 主线` 仍正常（兼容性）
6. `python3 scripts/simulate.py run --start 2026-02-01 --end 2026-03-01 --budget 10000` 仍正常（兼容性 — 内部逻辑没改）
7. `git diff` 不触碰 `data/portfolio.json`、`data/orders.json`、`data/smart_invest.db`

## 14. 路线图（Phase 2-4 占位）

| Phase | 范围 | 启动条件 |
|---|---|---|
| **P2 回测-进化闭环** | `simulate.py` 改为复用 `decision_engine.decide()`；每次回测产出按规则分组的胜率/期望/样本量；新增 `decide.py evolve --from-sim <id>` 自动写 `strategy_evolutions` 表 | P1 通过验证清单 + 至少跑过 1 周实盘 |
| **P3 信号扩展** | 新增 `scripts/signals.py` 模块（RSI/MACD/MA 斜率/突破/换手率）；`fetch_fund.py` 加资金流接口；SKILL.md 加 WebSearch 新闻整合规范；决策包 `actions[].context` 扩展技术指标字段 | P2 完成；新信号要先在回测里有正胜率证据才入主线规则 |
| **P4 UX 深化** | 邮件 HTML 模板重做（响应式 / 支付宝风格优化）；首次引导改为 Claude 智能对话式；错误消息友好化；新增"为什么没建议买入"快捷查询；本地命令行 dashboard（可选） | P3 完成 |

每个 Phase 启动前重新跑 brainstorming（不复用本文）。

---

**本设计文档是 Phase 1 的契约。** 实施过程中如发现偏差（如某个测试场景无法稳定通过、某条规则定义模糊），先修文档，再修代码。
