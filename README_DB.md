# Smart Invest 数据库架构

## 概述

系统已从 JSON 文件存储迁移到 SQLite 数据库，支持：
- 多账户管理（主线账户 + 梦境训练账户）
- 决策树版本控制
- 策略进化追踪
- 每笔交易的审计日志（记录"为什么"做决策）

## 数据库位置

```
~/.claude/skills/smart-invest/data/smart_invest.db
```

## 核心表结构

### 1. accounts — 账户表
- `id`: 账户ID
- `name`: 账户名称（"主线" / "梦境-2024全年"）
- `type`: 账户类型（"main" / "dream"）
- `budget`: 初始资金
- `cash`: 当前现金
- `strategy_version`: 使用的决策树版本

### 2. positions — 持仓表
- `account_id`: 关联账户
- `code`: 基金代码
- `name`: 基金名称
- `shares`: 持有份额
- `cost_nav`: 成本净值
- `sector`: 所属赛道

### 3. trades — 交易表（含审计信息）
- 基础字段：日期、代码、名称、方向、金额、净值、份额
- **审计字段**：
  - `rule_name`: 触发的规则（"低吸"/"止损"/"止盈20%"）
  - `rule_version`: 使用的决策树版本
  - `decision_context`: 当时的市场状态（JSON）
  - `reason`: 人类可读的决策原因
  - `checks_passed`: 通过了哪些检查（JSON）
  - `checks_failed`: 哪些检查导致放弃（JSON）
  - `profit_pct`: 卖出时的收益率
  - `outcome`: "win"/"loss"/"pending"

### 4. daily_snapshots — 每日快照
- 记录每天的总资产、现金、持仓市值、收益率、回撤
- 记录市场状态（牛市/震荡/熊市）
- 记录各赛道占比

### 5. decision_tree_versions — 决策树版本
- `version`: 版本号（"v1.0", "v2.0", "v2.1"）
- `parent_version`: 从哪个版本演化而来
- `changelog`: 改了什么
- `reason`: 为什么改
- `rules_json`: 完整规则（JSON）

### 6. strategy_evolutions — 策略进化记录
- `from_version` / `to_version`: 版本变化
- `title`: 进化标题（"加入赛道集中度控制"）
- `trigger_source`: 触发来源（"backtest_sim-xxx" / "real_trade"）
- `before_metrics` / `after_metrics`: 进化前后指标对比
- `lessons_learned`: 学到了什么

### 7. simulation_runs — 回测运行记录
- `sim_id`: 回测ID
- `account_id`: 关联的梦境账户
- 回测参数和结果

## CLI 命令

### 数据库管理

```bash
# 初始化数据库
python3 scripts/db.py init

# 从 JSON 导入数据
python3 scripts/db.py import-json

# 查看所有账户
python3 scripts/db.py accounts

# 查看指定账户持仓
python3 scripts/db.py positions --account 主线

# 查看指定账户交易
python3 scripts/db.py trades --account 主线 --limit 50

# 查看决策树版本
python3 scripts/db.py tree-versions

# 查看进化历史
python3 scripts/db.py evolutions
```

### 持仓和订单查看

```bash
# 查看主线持仓（从DB）
python3 scripts/fetch_fund.py portfolio-show --account 主线

# 查看梦境持仓
python3 scripts/fetch_fund.py portfolio-show --account 梦境-2024

# 查看主线订单
python3 scripts/fetch_fund.py orders-show --account 主线 --limit 50

# 检查主线持仓估值
python3 scripts/fetch_fund.py portfolio-check --account 主线
```

### 决策引擎

```bash
# 分析账户表现
python3 scripts/decision_engine.py analyze --account 主线
```

## 使用流程

### 1. 初始化（首次使用）

```bash
cd ~/.claude/skills/smart-invest/scripts
python3 db.py init
python3 db.py import-json  # 从现有 JSON 文件导入
```

### 2. 日常使用

所有持仓查看、交易记录命令都支持 `--account` 参数：

```bash
# 查看主线账户
python3 fetch_fund.py portfolio-show --account 主线
python3 fetch_fund.py orders-show --account 主线

# 查看梦境账户
python3 fetch_fund.py portfolio-show --account 梦境-xxx
```

### 3. 梦境训练（回测）

```bash
# 运行回测（自动创建梦境账户）
python3 simulate.py --start 2024-01-01 --end 2024-12-31 --budget 10000

# 查看回测结果
python3 db.py positions --account 梦境-2024
python3 db.py trades --account 梦境-2024
```

### 4. 策略进化

```bash
# 1. 分析当前策略表现
python3 decision_engine.py analyze --account 主线

# 2. 根据分析结果优化决策树
# 修改 data/decision_tree.json

# 3. 保存新版本到数据库
python3 -c "
from db import Database
import json
db = Database()
with open('../data/decision_tree.json', 'r') as f:
    tree = json.load(f)
db.add_tree_version(
    version=tree['version'],
    parent_version=tree['parent'],
    changelog=tree['changelog'],
    reason=tree['reason'],
    rules_json=tree['rules']
)
db.close()
"

# 4. 用新版本重新回测
python3 simulate.py --start 2024-01-01 --end 2024-12-31 --budget 10000

# 5. 记录进化
python3 -c "
from db import Database
db = Database()
db.add_evolution(
    from_version='v2.0',
    to_version='v2.1',
    title='降低科技赛道集中度',
    description='从50%降至40%，减少板块回调风险',
    trigger_source='backtest_sim-2024',
    lessons_learned='科技赛道50%上限在板块回调时损失过大'
)
db.close()
"
```

## 决策审计示例

每笔交易都会记录详细的决策上下文：

```json
{
  "date": "2026-05-26",
  "code": "512480",
  "name": "半导体ETF",
  "action": "buy",
  "amount": 5000,
  "rule_name": "低吸",
  "rule_version": "v2.0",
  "decision_context": {
    "market_regime": "震荡市",
    "hs300_5d_return": -0.02,
    "fund_5d_return": -0.06,
    "fund_day_return": -0.035
  },
  "reason": "符合低吸条件：大盘非熊市，基金近5天跌>5%，当日跌>3%",
  "checks_passed": [
    "现金比例 25% >= 10%",
    "单只仓位 0% <= 25%",
    "科技赛道 0% <= 50%",
    "近5天涨幅 -6% <= 10%",
    "大盘环境正常",
    "趋势正常"
  ],
  "checks_failed": [],
  "outcome": "win",
  "profit_pct": 0.08
}
```

## 迁移说明

- **JSON 文件保留**：作为备份和兼容层
- **DB 为主**：所有新操作优先使用数据库
- **向后兼容**：如果指定了 `--account`，从 DB 读取；否则回退到 JSON 文件

## 下一步

- [ ] 完成 simulate.py 重构，全面使用 Database + DecisionEngine
- [ ] 添加自动进化建议功能
- [ ] 添加可视化报表（胜率、收益曲线等）
