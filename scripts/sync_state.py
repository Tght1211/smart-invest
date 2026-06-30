#!/usr/bin/env python3
"""双向同步的核心：账户状态序列化 + 冲突合并（纯函数，无 IO，便于测试）。

模型（见 docs/web-platform-architecture.md §2）：
- 同步单位 = 一个钱包账户 `(user, account_name)`。
- **trades 是 append-only**：跨设备用内容哈希 `uid` 去重并集，永不丢单。
- **positions / cash 是状态**：last-writer-wins by `updated_at`（谁写得晚谁赢）。
- 每个状态带 `origin`(device_id) + `updated_at`，便于审计与冲突判定。

这一层不碰网络、不碰 DB —— 只对「dict 状态」做确定性合并，因此可纯单测。
sync_client 负责 IO（序列化 DB ↔ 状态、HTTP push/pull），server 负责持久化与鉴权。
"""
import hashlib


def trade_uid(t):
    """交易的跨设备稳定标识（内容哈希）。同内容交易视为同一笔，天然幂等去重。"""
    key = "|".join(str(x) for x in (
        t.get("date", ""), t.get("code", ""), t.get("action", ""),
        round(float(t.get("amount", 0) or 0), 2),
        round(float(t.get("nav", 0) or 0), 4),
        round(float(t.get("shares", 0) or 0), 4),
    ))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def merge_trades(local, remote):
    """append-only 并集：按 uid 去重，按 (date, uid) 排序。输入/输出均为 dict 列表。"""
    by_uid = {}
    for t in list(local or []) + list(remote or []):
        by_uid.setdefault(t.get("uid") or trade_uid(t), t)
    return sorted(by_uid.values(),
                  key=lambda t: (t.get("date", ""), t.get("uid") or trade_uid(t)))


def merge_positions(local, remote):
    """状态 LWW：按 code，updated_at 晚者胜；一方独有则保留。shares<=0 视为已清仓删除。"""
    out = {}
    for p in list(local or []):
        out[p["code"]] = p
    for p in list(remote or []):
        cur = out.get(p["code"])
        if cur is None or (p.get("updated_at") or "") >= (cur.get("updated_at") or ""):
            out[p["code"]] = p
    return [p for p in out.values() if (p.get("shares") or 0) > 0]


def merge_scalar_lww(local_val, local_ts, remote_val, remote_ts):
    """标量（如 cash）LWW：updated_at 晚者胜。"""
    if (remote_ts or "") >= (local_ts or ""):
        return remote_val, remote_ts
    return local_val, local_ts


def merge_account(local, remote):
    """合并两份账户状态 → 一份权威状态。两边都没有的字段取对方。纯函数。

    state = {account, type, budget, cash, cash_ts, updated_at, origin,
             positions:[{code,name,shares,cost_nav,sector,updated_at}],
             trades:[{uid,date,code,name,action,amount,nav,shares,...}]}
    """
    local = local or {}
    remote = remote or {}
    cash, cash_ts = merge_scalar_lww(
        local.get("cash"), local.get("cash_ts") or local.get("updated_at"),
        remote.get("cash"), remote.get("cash_ts") or remote.get("updated_at"))
    merged = {
        "account": local.get("account") or remote.get("account"),
        "type": local.get("type") or remote.get("type") or "paper",
        "budget": local.get("budget") if local.get("budget") is not None
        else remote.get("budget"),
        "cash": cash,
        "cash_ts": cash_ts,
        "updated_at": max(local.get("updated_at") or "",
                          remote.get("updated_at") or ""),
        "positions": merge_positions(local.get("positions"), remote.get("positions")),
        "trades": merge_trades(local.get("trades"), remote.get("trades")),
    }
    return merged
