# 单只/行业分析 + 梦境训练回测

探索性分析（模式 C/D）与历史回测（模式 E）的操作指南。这些**不走 `decide.py`**——是探索/验证，不是实盘决策。

## 目录
- 单只基金分析（模式 C）
- 行业方向分析（模式 D）
- 梦境训练回测（模式 E）
- 梦境实验室（策略进化闭环）

---

## 单只基金分析（模式 C）

```bash
python3 scripts/fetch_fund.py estimate <code>
python3 scripts/fetch_fund.py nav <code> --days 60
```

输出：

```markdown
## 基金分析: {名称} ({code})

**实时估值**: {gsz} ({gszzl:+.2f}%)
**近 60 天收益**: {pct:+.2f}%
**最大回撤**: -X.XX%

### 趋势分析
{支撑位 / 压力位 / 波动评级}

### 建议
{观望 / 可买入 / 已涨过高 …}
```

## 行业方向分析（模式 D）

**铁律：看板块必看多窗口（今日 / 7日 / 30日 / 6月）+ 典型新闻，别追一日脉冲。**

```bash
python3 scripts/fetch_fund.py sector-scan --top 8          # 板块 今日/7日/30日/6月波动 + 趋势分类
python3 scripts/fetch_fund.py sector-scan --board 半导体     # 下钻单个板块的多窗口
python3 scripts/fetch_fund.py discover --sector 半导体       # 该方向场外候选（多窗口一致性打分，排除持仓）
python3 scripts/fetch_fund.py rank --type gp --period 6n --top 20 --otc-only   # 备用：场外股票型排行
```

判读规则：
- **强势趋势**（7日/30日/6月同向上行）= 真趋势，优先；
- **超跌反弹·谨慎** = 今日反弹但 30 日仍下行，多为下跌中继，少追；
- **高位退潮 / 弱势下行** = 回避。

工作流：`sector-scan` 选出真强势方向 → `discover --sector <方向>` 下钻挑 2-3 只场外候选 → WebSearch 该方向政策/产业新闻佐证 → 结合 6 个月波动给仓位建议。短线波段标的买 C 类、长线核心买 A 类（`share-class <code> --prefer C|A` 查代码）。

---

## 梦境训练回测（模式 E）

历史回测验证策略。**关键约束：只用当天及之前的数据，无未来函数。**

```bash
python3 scripts/simulate.py run \
  --start YYYY-MM-DD --end YYYY-MM-DD --budget 50000
```

预设 6 只基金池（006479 / 512480 / 660011 / 540010 / 005825 / 161725），可用 `--funds` 自定义。

回测自动对比沪深 300、上证指数、等权持有。结果存到 `data/simulations/<sim_id>/`，回测报告：

```bash
python3 scripts/simulate.py list
python3 scripts/simulate.py report <sim_id>
```

> 内部逻辑详见 `README_DB.md`。`--engine` 旗标让回测复用 `decision_engine.decide()`，回测与实盘同一套规则。

## 梦境实验室（策略进化闭环）

`strategy_lab.py` 在**同一历史窗口**跑多个策略变体并排名，是"提出策略 → 梦境验证 → 择优晋升"的引擎：

```bash
python3 scripts/strategy_lab.py variants          # 看内置变体（基线/趋势退出/低吸闸门/关止盈/仓位管理…）
python3 scripts/strategy_lab.py run \
  --start 2025-06-10 --end 2026-06-09 --budget 20000 \
  [--variants name1,name2] [--evolve] [--promote v2.2 [--promote-variant NAME]]
```

- `--evolve`：冠军≠基线时写 `strategy_evolutions`（进化审计）
- `--promote vX.Y`：把冠军（或 `--promote-variant` 指定的变体）注册为新决策树版本，**同时改写 `data/decision_tree.json`**；引擎默认版本跟随该文件，`decide.py`/`daily_report.py` 下次运行即用新规则
- 数据只拉一次注入复用；指数自动多回看 450 天供 200 日线计算；东财失败自动走新浪/腾讯备源；沪深300 全拉不到才报错中止（趋势规则依赖它，缺数据的回测无意义）
- **铁律**：改规则必须先过实验室——至少两个不同市况窗口（牛市 + 震荡/熊市）都不劣化才能晋升

已沉淀的回测证据：
- **关闭分层止盈**（`take_profit_policy.mode=off`，v2.1）：牛市 +73.98% vs 基线 +47.27%，震荡市基本打平 → 已采纳（让利润奔跑的引擎级形态）
- **趋势退出**（`trend_exit`，参考指数连续 N 天破 200 日线减仓）：事件触发（只在跨越确认日当天卖一次），破位期间不重复卖——状态触发版在震荡市 whipsaw 亏 8 个点，已修正
- **低吸趋势闸门**（`trend_filter`，HS300 在 200 日线下低吸打折）：牛市窗口 +2.8 个点改善
- **总仓位管理**（`position_management`，v2.2）：三窗口 A 牛市 65.90 / B 震荡 -6.35（亏损年转正）/ C 下跌 -2.12；要求 HS300 在 200 日线上方才建仓。信号买入规则（rsi/breakout/trim）证据不足，保持禁用。
