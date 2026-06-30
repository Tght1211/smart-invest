# Smart Invest — Claude Code 基金投资助手 Skill

一个运行在 [Claude Code](https://claude.ai/claude-code) 上的**个人基金投资顾问** Skill，支持 A 股 / QDII 基金的市场分析、持仓管理、交易记录和每日投资建议。
支持**本地离线**单机使用，也支持**在线连通**模式与自有服务器双向同步、多用户多虚拟钱包。

## 功能

- **决策引擎** — 结构化决策包产出买卖建议（规则可版本化、可回测、可复盘）
- **新闻感知** — 结合财经新闻情绪 + 趋势强度动态调整低吸阈值（非写死 -3%）
- **基金质量体检** — 规模/机构占比/经理/前十大集中度 + 红旗清单（清盘·踩踏·杠杆否决）
- **每日分析** — 大盘指数、板块热点、持仓诊断、操作建议，一键生成报告
- **持仓管理** — 买入/卖出/加仓/减仓，自动记录交易历史（SQLite 单一事实源 + 审计字段）
- **Web 面板** — 响应式深色仪表盘（PC/移动自适应）：ECharts 指数蜡烛 K 线 + 持仓净值抽屉 + 总收益曲线，首屏 ~1.3s
- **虚拟钱包** — `paper` 钱包用真实当日行情做实战测试（区别于历史回放的「梦境」回测）
- **在线同步** — 在线模式与远程服务器双向同步（trades 并集 + positions/cash LWW），多用户隔离
- **LLM 适配** — 接 Anthropic 格式三方 API（配置驱动、优雅降级），用于报告点评等表达层
- **邮件通知** — 分析报告和交易通知自动发送到邮箱（重试 + 落盘补发）
- **梦境训练** — 历史回测，验证投资策略有效性
- **定时报告** — 三时段（开盘/盘中/盘尾）+ 晚报、周报、月报全覆盖

## 安装

1. 将此目录复制到 Claude Code 的 skills 目录：

```bash
cp -r smart-invest ~/.claude/skills/
```

2. 首次使用时，Skill 会自动引导你配置邮件通知（可选）。

3. 初始化数据库：

```bash
python3 ~/.claude/skills/smart-invest/scripts/db.py init
```

## 使用

在 Claude Code 中直接对话即可：

| 说法 | 功能 |
|------|------|
| `/smart-invest 每日分析` | 完整分析 + 发邮件 |
| `快速看看` / `今天怎么样` | 快速分析大盘 + 持仓 |
| `帮我看看 110011` | 分析单只基金 |
| `买了半导体ETF 5000元` | 记录买入交易 |
| `黄金可以抄底吗` | 行业/方向分析 |
| `梦境训练` | 历史回测 |

## 邮件报告效果

支付宝基金风格，移动端友好：

- 顶部大号盈亏数字（红涨绿跌）
- 持仓卡片（今日/昨日盈亏、累计收益）
- 板块涨跌热力图（treemap 色块）
- 操作时间线

## Web 面板（本地）

```bash
python3 scripts/web_panel.py start         # 后台启动，浏览器开 http://127.0.0.1:8765/
python3 scripts/web_panel.py stop          # 关闭
```

深色 fintech 风、PC/移动自适应；指数蜡烛 K 线（MA+成交量+缩放）、点持仓看净值抽屉、
财经要闻、跨板块「新方向」（基本面红旗闸门）、可选「🤖 AI 点评」。

## 在线模式 / 多用户同步（可选）

离线模式（默认）单机即用。在线模式可与自有服务器双向同步，并支持多用户、多虚拟钱包：

```bash
# 1) 虚拟钱包（真实当日行情实战，区别于梦境回测）
python3 scripts/db.py paper-wallet create 实战-激进-A --budget 100000

# 2) 配置（data/app_config.json，gitignored，可含密钥；或用环境变量）
#    {"mode":"online","llm":{"base_url":"https://<anthropic兼容网关>","api_key":"...","model":"..."},
#     "sync":{"base_url":"https://<你的服务器>","token":"...","device_id":"mac-1"}}

# 3) 服务器侧（部署到自有服务器，纯 stdlib、内存占用低）
python3 scripts/server.py serve --host 0.0.0.0 --port 8800
```

详见 `docs/web-platform-architecture.md`（分层架构 + 路线图）。

## 数据来源

所有数据来自天天基金/东方财富公开接口，仅供学习研究，不构成投资建议。

## 依赖

- Python 3.8+
- 纯标准库，无需 pip install（前端图表用 CDN 版 ECharts）

## License

MIT
