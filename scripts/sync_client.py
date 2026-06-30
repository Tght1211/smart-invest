#!/usr/bin/env python3
"""在线连通模式：把本地某钱包账户与远程服务器**双向同步**（纯 stdlib urllib）。

流程：serialize 本地账户 → POST /api/sync（带 token）→ 服务器合并并回传权威状态 →
本地再 merge_account 一次（幂等）→ apply 回写本地 DB。trades append-only 并集、
positions/cash LWW（见 sync_state）。离线 / 未配置 / 网络失败 → 返回 error，绝不抛、不动数据。

序列化/回写依赖 db.Database；合并逻辑全在纯函数 sync_state 里（已单测）。
"""
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import app_config  # noqa: E402
import sync_state  # noqa: E402

TIMEOUT = 30


# ---------- DB ↔ 状态 ----------

def serialize_account(db, account_name):
    """读本地某账户 → 同步状态 dict（trades 带 uid，positions/账户带 updated_at）。"""
    acc = db.get_account(name=account_name)
    if not acc:
        return None
    aid = acc["id"]
    positions = []
    for p in db.get_positions(aid):
        positions.append({
            "code": p["code"], "name": p["name"], "shares": p["shares"],
            "cost_nav": p["cost_nav"], "sector": p.get("sector"),
            "buy_date": p.get("buy_date"),
            "updated_at": p.get("updated_at") or acc.get("updated_at") or "",
        })
    trades = []
    for t in db.get_trades(aid):
        td = {"date": t["date"], "code": t["code"], "name": t["name"],
              "action": t["action"], "amount": t["amount"], "nav": t["nav"],
              "shares": t["shares"], "rule_name": t.get("rule_name"),
              "reason": t.get("reason")}
        td["uid"] = sync_state.trade_uid(td)
        trades.append(td)
    return {
        "account": acc["name"], "type": acc["type"], "budget": acc["budget"],
        "cash": acc["cash"], "cash_ts": acc.get("updated_at") or "",
        "updated_at": acc.get("updated_at") or "",
        "positions": positions, "trades": trades,
    }


def apply_account(db, state):
    """把合并后的权威状态回写本地 DB（upsert 账户/持仓、补缺失交易、置现金）。

    返回 {trades_added, positions_set, created}。幂等：已存在的交易/持仓不重复。
    """
    name = state["account"]
    acc = db.get_account(name=name)
    created = False
    if not acc:
        db.create_account(name, state.get("type") or "paper",
                          state.get("budget") or 0.0)
        acc = db.get_account(name=name)
        created = True
    aid = acc["id"]

    # 交易：补本地没有的 uid（append-only）
    have = {sync_state.trade_uid(t) for t in db.get_trades(aid)}
    added = 0
    for t in state.get("trades", []):
        if (t.get("uid") or sync_state.trade_uid(t)) in have:
            continue
        db.add_trade(aid, t["date"], t["code"], t["name"], t["action"],
                     t["amount"], t["nav"], t["shares"],
                     rule_name=t.get("rule_name"), reason=t.get("reason"))
        added += 1

    # 持仓：按合并结果 upsert；本地多出的（已被对端清仓）删除
    merged_codes = set()
    pset = 0
    for p in state.get("positions", []):
        db.set_position(aid, p["code"], p["name"], p["shares"], p["cost_nav"],
                        buy_date=p.get("buy_date"), sector=p.get("sector"))
        merged_codes.add(p["code"])
        pset += 1
    for p in db.get_positions(aid):
        if p["code"] not in merged_codes:
            db.remove_position(aid, p["code"])

    if state.get("cash") is not None:
        db.update_account(aid, cash=state["cash"])
    return {"trades_added": added, "positions_set": pset, "created": created}


# ---------- HTTP ----------

def push_pull(base_url, token, state, _opener=None):
    """POST 本地状态到服务器 /api/sync，返回服务器合并后的权威状态；失败返回 None。"""
    url = base_url.rstrip("/") + "/api/sync"
    body = json.dumps({"token": token, "account": state["account"],
                       "state": state}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"content-type": "application/json",
                 "authorization": f"Bearer {token}"},
        method="POST")
    try:
        opener = _opener or urllib.request.urlopen
        with opener(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        return data.get("merged") if isinstance(data, dict) else None
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
            json.JSONDecodeError, TimeoutError, OSError):
        return None


def sync_account(db, account_name, cfg=None, _opener=None):
    """双向同步一个账户。返回 {ok, ...summary} 或 {ok:False, error}。"""
    c = cfg if cfg is not None else app_config.sync_config()
    if not (c.get("base_url") and c.get("token")):
        return {"ok": False, "error": "sync not configured (need base_url+token)"}
    local = serialize_account(db, account_name)
    if local is None:
        return {"ok": False, "error": f"account '{account_name}' not found locally"}

    server_state = push_pull(c["base_url"], c["token"], local, _opener=_opener)
    if server_state is None:
        return {"ok": False, "error": "server unreachable / sync failed"}

    merged = sync_state.merge_account(local, server_state)
    summary = apply_account(db, merged)
    return {"ok": True, "account": account_name, **summary,
            "synced_at": datetime.now().isoformat(timespec="seconds")}
