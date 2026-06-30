# Smart Invest 在线平台 —— 架构与路线图

> 目标（用户原话归纳）：把现在「Claude Code Skill + 本地单机面板」升级为一个**完善的在线 Web 系统**，
> 发布到自有服务器；引入 **Claude Code SDK / Anthropic 格式三方 API**；Skill 同时支持**本地离线模式**
> 与**在线连通模式**（在线时与远程服务器**双向同步**）；在线 Web 版多用户、多**虚拟钱包**做实战测试
> （区别于本地的「梦境」回测）。

本文是执行蓝图，按阶段拆解、标注**阻塞项**（依赖用户提供的服务器/API 信息）与**可立即动工项**。

---

## 0. 现状（已完成的基础）

- **决策内核**（纯 Python stdlib，无三方依赖）：`decision_engine` 决策包、`fetch_fund` 行情/基本面、
  `simulate` 回测、`db` SQLite 单一事实源、新闻感知动态阈值、基金质量红旗。
- **本地面板 v2**：`web_panel.py`（JSON API + 自包含响应式前端 `web_panel.html`，ECharts K 线/净值/收益曲线）。
  本轮已把首屏 `/api/overview` 从 ~9.3s 降到 ~1.3s（并发抓取 + discover 懒加载解耦）。
- 单机、单用户、SQLite 落地。**还不是**多用户在线系统。

---

## 1. 目标架构（分层）

```
┌─────────────────────────────────────────────────────────────┐
│  客户端                                                       │
│  ┌──────────────┐   ┌───────────────────────────────────┐    │
│  │ Claude Code   │   │  在线 Web App（多用户/多钱包）       │    │
│  │ Skill（本地）  │   │  浏览器 SPA + 服务端                │    │
│  │ 离线/在线两模式 │   │                                   │    │
│  └──────┬───────┘   └─────────────┬─────────────────────┘    │
│         │ 在线模式双向同步           │                          │
└─────────┼──────────────────────────┼──────────────────────────┘
          │  HTTPS + token            │
┌─────────▼──────────────────────────▼──────────────────────────┐
│  远程服务器（用户自有）                                         │
│  ┌────────────┐ ┌──────────────┐ ┌────────────────────────┐   │
│  │ API 网关    │ │ 同步服务      │ │ LLM 适配层（Anthropic   │   │
│  │ /auth /sync │ │ (冲突合并)    │ │ 格式三方 API + CC SDK)  │   │
│  └─────┬──────┘ └──────┬───────┘ └───────────┬────────────┘   │
│        │               │                     │                │
│  ┌─────▼───────────────▼─────────────────────▼────────────┐   │
│  │ 持久层：Postgres（多用户/多钱包/交易/快照） + 对象存储     │   │
│  └──────────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────────────┘
```

**核心内核保持单一**：`decision_engine` / `fetch_fund` / `simulate` 是纯逻辑，本地与服务器**共用同一份**，
避免两套规则漂移（与现有「SKILL.md 与脚本保持同步」纪律一致）。

---

## 2. 本地离线 / 在线连通 双模式

Skill 增加一个**模式开关**（配置文件 `data/app_config.json` 或环境变量）：

| | 离线模式（默认，向后兼容） | 在线连通模式 |
|---|---|---|
| 数据源 | 本地 SQLite | 本地 SQLite + 远程双向同步 |
| 行情 | 天天基金/东财公开接口 | 同左（行情不走服务器） |
| LLM | 不需要 | 可用服务器侧 Anthropic 适配（叙事/复盘增强） |
| 触发 | 现有 cron/对话 | 同左 + 服务器推送 |

**双向同步协议（设计）**：
- 每条可变记录（positions/trades/daily_snapshots/daily_plans/...）加 `updated_at`（已有时间戳）+ `rev`（单调递增）+ `origin`（device id）。
- 同步端点：`POST /sync/push`（上行本地自上次以来的变更）、`GET /sync/pull?since=<cursor>`（下行远端变更）。
- **冲突策略**：交易类（trades）是 append-only → 以 `(account, trade_id)` 去重，不冲突；
  持仓/计划类是状态 → **last-writer-wins by `updated_at`**，但保留审计行；现金/净值以服务器为权威。
- 离线期间本地照常写，恢复在线时按 cursor 增量对账（断点续传），失败进 outbox 重试（复用现有 outbox 模式思路）。

**可立即动工**：模式开关 + `sync_client.py` 的接口契约与本地变更日志（rev/origin），先不接真服务器（stub）。

---

## 3. 多用户 / 多虚拟钱包

- 现有 schema 已天然支持多账户（`accounts`：主线 / 梦境-*）。在线化需要：
  - 新增 `users`（id, 第三方/邮箱登录, 配额）。
  - `accounts` 增 `user_id` + `kind`：`main`(真实) / `dream`(回测) / **`paper`(虚拟钱包实战)**。
  - 虚拟钱包 = `kind=paper`，初始资金可配，跑**真实当日行情 + 真实决策引擎**（区别于 `dream` 的历史回放），
    用于「实战测试」而非未来函数回测。
- 隔离：所有查询带 `user_id`；服务器侧行级权限。
- **可立即动工**：在本地 `db.py` 给 `accounts` 加 `kind`（默认 main，自愈式迁移，向后兼容），
  并让 `simulate`/面板能开 `paper` 钱包——这不依赖服务器。

---

## 4. LLM 适配层（Anthropic 格式三方 API + Claude Code SDK）

- **`llm_client.py`（纯 stdlib urllib）**：调用 **Anthropic Messages 兼容** 端点
  （`POST {base_url}/v1/messages`，`x-api-key` 或 `Authorization: Bearer`），
  `base_url`/`key`/`model` 全部来自配置/环境 → **天然支持任何 Anthropic 格式三方 API**。
  用途：报告叙事增强、复盘点评、自然语言问答（不驱动买卖决策，决策仍归引擎，守住现有红线）。
- **Claude Code SDK**（Node `@anthropic-ai/claude-code` / Agent SDK）：用于**在线 Web 服务端**的 agentic 能力
  （让 Web 用户用自然语言操作钱包/查分析），由服务器进程承载；与本地 stdlib 内核解耦。
- **阻塞**：需用户提供 ① 三方 API `base_url` + `key` + 模型名；② 是否要在服务器侧跑 CC SDK（Node 运行时）。
- **可立即动工**：`llm_client.py` 的接口 + 配置读取 + 离线降级（无 key 时不调用），写好后**填 key 即用**。

---

## 5. 服务器 / 部署

- **阻塞**：需用户提供 ① 服务器访问方式（SSH/host/region）；② 运行时（裸机/Docker/K8s）；
  ③ 域名 + TLS；④ DB（自建 Postgres / 托管）。
- 建议形态：单台起步 = Docker Compose（API 服务 + Postgres + 反代 TLS + 静态 Web）。
- 持久层从 SQLite → **Postgres**：内核 `db.py` 已通过 `Database` 类隔离，迁移时抽象 SQL 方言或引入轻量 DAL；
  本地仍可 SQLite，服务器用 Postgres（同一套 CRUD 接口）。

---

## 6. 在线 Web App（区别于 Skill 面板）

- 现有 `web_panel.html` 是单用户只读看板。在线版需要：登录、账户/钱包切换、下单（虚拟钱包）、
  多用户隔离、服务端鉴权、WebSocket 实时推送。
- 前端可在现有响应式 + ECharts 基础上扩展（已是深色 fintech 风、PC/移动自适应）。
- 服务端 API 复用本地 `_overview_data`/`_kline_data`/`_discover_data` 的数据形状（已是 JSON 契约）。

---

## 7. 阶段路线图

| 阶段 | 内容 | 依赖 | 状态 |
|---|---|---|---|
| P0 | 面板性能（首屏 9.3s→1.3s）、discover 懒加载、K 线重试 | — | ✅ 本轮完成 |
| P1 | 模式开关 + 变更日志(rev/origin) + `sync_client` 契约(stub) | — | 可立即动工 |
| P2 | `accounts.kind`(main/dream/**paper**) + 虚拟钱包实战 | — | 可立即动工 |
| P3 | `llm_client.py`（Anthropic 兼容，配置驱动，离线降级） | 三方 API key | 接口可先写，填 key 即用 |
| P4 | Postgres DAL 抽象（本地 SQLite/服务器 PG 同接口） | — | 可立即动工 |
| P5 | 服务器：API 网关 + auth + sync 服务 + 部署 | 服务器信息 | **阻塞** |
| P6 | 在线多用户 Web App（登录/下单/实时推送） | P5 | **阻塞** |
| P7 | Claude Code SDK 服务端 agent（自然语言操作） | API + 服务器 | **阻塞** |

---

## 8. 现在就需要你提供的信息（解阻塞 P3/P5/P6/P7）

1. **三方 Anthropic 格式 API**：`base_url`、`api_key`、可用模型名（如 `claude-*` 或三方自定义名）。
2. **服务器**：访问方式（SSH host/用户/密钥）、运行时（Docker？）、是否已有域名/TLS、DB 选型。
3. **是否要服务器侧 Node 运行时**跑 Claude Code SDK（还是仅用 HTTP 调 Anthropic 兼容端点）。
4. **多用户范围**：仅自己多钱包，还是要对外开放注册（影响 auth/配额/合规）。

在你提供前，我会推进 P1/P2/P4 这些不阻塞的基础（向后兼容、纯 stdlib、带测试）。
