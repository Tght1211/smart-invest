# 规则 ID 速查 + secid / 基金池附录

引擎 `actions[].rule_id` / `blocked_actions[].blocked_by` 字段对照，以及常用指数 secid 和回测基金池。

## 目录
- 规则 ID 速查（拦截 / 买 / 卖 / 警）
- 主要指数 secid
- 回测预设基金池

---

## 规则 ID 速查

「v2.2 状态」列：✅ 当前生效；⏸️ 已实现但当前决策树未启用（须经梦境实验室晋升才会激活）。

| rule_id | 中文名 | 类别 | 触发条件简述 | v2.2 状态 |
|---------|--------|------|-----------|------|
| `cash_reserve` | 现金储备 | 拦截 | 现金占比 < 10% | ✅ |
| `single_position` | 单只仓位 | 拦截 | 单只仓位将超 25% | ✅ |
| `sector_concentration` | 赛道集中度 | 拦截 | 赛道占比将超上限 | ✅ |
| `anti_chase` | 禁止追涨 | 拦截 | 近 5 天涨幅 > 10% | ✅ |
| `bear_market_new_position` | 熊市禁建仓 | 拦截 | hs300 20d < -10% 且无持仓 | ✅ |
| `market_regime_unknown` | 大盘数据缺失 | 拦截 | hs300 数据拉取失败 | ✅ |
| `low_buy` | 低吸 | 买 | 当日跌 > 3% | ✅ |
| `emergency_stop_loss` | 紧急止损 | 卖 50% | 单日 > 7% 或 3 日 > 10% | ✅ |
| `absolute_stop_loss` | 绝对止损 | 卖 100% | 亏损 > 20% | ✅ |
| `time_based_stop_loss` | 按期止损 | 卖 50% | 持有期分档亏损 | ✅ |
| `trend_exit_ma200` | 趋势破位退出 | 卖 50% | 参考指数连续收于 200 日线下 | ✅ |
| `position_build` | 分批建仓 | 买 | 总仓位低于 regime 目标下限、HS300 在 200 日线上方 | ✅ |
| `position_cap_trim` | 总仓位超限回撤 | 卖 | 总仓位超 regime 上限 + 容差 | ✅ |
| `drawdown_protection` | 回撤保护 | 警 | 组合从峰值回撤 ≥ 10% | ✅ |
| `low_buy_deferred_drawdown` | 低吸暂缓 | 观察 | 回撤保护下 low_buy 降级 | ✅ |
| `data_missing` | 数据缺失 | 警 | 某基金 NAV 拉不到 | ✅ |
| `take_profit_tier_20/30/40` | 分层止盈 | 卖 25% | 盈利 ≥ 20/30/40% | ⏸️（让利润奔跑已关） |
| `take_profit_clearout` | 止盈清仓 | 卖 100% | 盈利 ≥ 50% | ⏸️ |
| `rsi_oversold_buy` | RSI 超卖低吸 | 买 | RSI(14) ≤ 阈值 | ⏸️ |
| `momentum_breakout` | 20 日突破顺势买 | 买 | 创 20 日新高且 MA20 上行 | ⏸️ |
| `rsi_overbought_trim` | RSI 超买减仓 | 卖 | RSI ≥ 阈值且浮盈达标 | ⏸️ |
| `auto_invest` | 定投自动买入 | 买 | 定投计划到期（盘尾自动记账，不在 decide 的 actions 里） | ✅ |

详细决策树见 `data/decision_tree.md`，引擎实现见 `scripts/decision_engine.py`。

---

## 主要指数 secid（`fetch_fund.py index-kline <secid>` 用）

| 指数 | secid |
|------|-------|
| 上证指数 | `1.000001` |
| 深证成指 | `0.399001` |
| 创业板指 | `0.399006` |
| 沪深 300 | `1.000300` |
| 中证 500 | `1.000905` |
| 上证 50 | `1.000016` |

## 回测预设基金池（引擎观察池）

| 代码 | 名称 | 方向 |
|------|------|------|
| 006479 | 广发纳斯达克 100ETF 联接 C | 美股/QDII |
| 512480 | 半导体 ETF 国联安 | A 股科技 |
| 660011 | 农银中证 500 指数 A | A 股宽基 |
| 540010 | 汇丰晋信科技先锋股票 | A 股科技 |
| 005825 | 海富通电子传媒股票 A | A 股科技 |
| 161725 | 招商中证白酒指数 A | 消费 |

> 这只是**默认观察池**。要拓宽视野别只盯它们：`fetch_fund.py discover` 跨板块发现新候选，`decide.py run --discover N` 把新候选并入引擎候选池。

## 买入建议的份额标注（短 C 长 A）

引擎给每条 buy/watch 加：
- `horizon`：`short`（低吸/信号买）/ `long`（分批建仓）
- `share_class`：`{preferred: C|A, current, reason_zh}` — 短线优选 C、长线优选 A

查兄弟份额：`fetch_fund.py share-class <code> --prefer C|A`。示例配对：

| 基金 | C 类 | A 类 |
|------|------|------|
| 广发纳斯达克100ETF联接(人民币) | 006479 | 270042 |
