#!/usr/bin/env python3
"""skill ↔ smart-invest-web 双向同步（在线平台就是那个多用户 Web 系统，端口 8090）。

方向：
  · skill → web：把本地某账户（默认 主线）的持仓/交易/现金推到 web 的同名钱包（"skill 控制 web"）。
  · web → skill：web 侧合并后回传该钱包的权威状态（含浏览器/AI 在 web 上产生的新交易），
    据此把 web 的新增交易/持仓拉回本地 DB（"web 数据回流 skill"）。

与 `sync_client`（对接 skill 自己的 server.py:8800）不同，这里直接对接 web 平台 app.py 的
`/api/sync`：用 email+password 登录换 session token，payload 用 web 的 {wallet,holdings,...} 形态。
复用 `sync_client.serialize_account/apply_account` 做本地读写，纯 stdlib urllib，3.6 兼容。

未配置 web / 网络失败 → 返回 {ok:False,error}，绝不抛异常、不动本地数据。

  python3 scripts/web_sync.py sync --account 主线      # 双向同步一次
  python3 scripts/web_sync.py sync                      # 用 app_config.web.account
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import app_config  # noqa: E402
import sync_client  # noqa: E402

TIMEOUT = 30


# ---------- 形态转换：skill 同步状态 <-> web /api/sync ----------

def to_web_payload(state, wallet_name):
    """skill state（positions/trades/cash）→ web /api/sync 请求体（holdings/trades/cash）。"""
    return {
        "wallet": wallet_name,
        "budget": state.get("budget") or 0.0,
        "cash": state.get("cash"),
        "holdings": [{"code": p["code"], "name": p.get("name"), "shares": p["shares"],
                      "cost_nav": p["cost_nav"], "sector": p.get("sector")}
                     for p in state.get("positions", [])],
        "trades": [{"uid": t.get("uid"), "date": t["date"], "code": t["code"],
                    "name": t["name"], "action": t["action"], "amount": t["amount"],
                    "nav": t["nav"], "shares": t["shares"], "reason": t.get("reason")}
                   for t in state.get("trades", [])],
    }


def from_web_state(web_state, account_name, acc_type="paper"):
    """web 回传的钱包状态（holdings/trades）→ skill state（positions/trades），供 apply_account。"""
    return {
        "account": account_name,
        "type": acc_type,
        "budget": web_state.get("budget") or 0.0,
        "cash": web_state.get("cash"),
        "positions": [{"code": h["code"], "name": h.get("name"), "shares": h["shares"],
                       "cost_nav": h["cost_nav"], "sector": h.get("sector"),
                       "buy_date": None} for h in web_state.get("holdings", [])],
        "trades": [{"uid": t.get("uid"), "date": t["date"], "code": t["code"],
                    "name": t["name"], "action": t["action"], "amount": t["amount"],
                    "nav": t["nav"], "shares": t["shares"], "reason": t.get("reason")}
                   for t in web_state.get("trades", [])],
    }


# ---------- HTTP ----------

def _post(url, body, token=None, _opener=None):
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = "Bearer " + token
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers, method="POST")
    opener = _opener or urllib.request.urlopen
    with opener(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def login(base_url, email, password, _opener=None):
    """POST /api/login → session token；失败返回 None。"""
    try:
        data = _post(base_url.rstrip("/") + "/api/login",
                     {"email": email, "password": password}, _opener=_opener)
        return data.get("token") if isinstance(data, dict) else None
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
            json.JSONDecodeError, TimeoutError, OSError):
        return None


def push_pull(base_url, token, payload, _opener=None):
    """POST /api/sync → web 合并后回传的钱包权威状态（data.state）；失败返回 None。"""
    try:
        data = _post(base_url.rstrip("/") + "/api/sync", payload, token=token, _opener=_opener)
        return data.get("state") if isinstance(data, dict) else None
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
            json.JSONDecodeError, TimeoutError, OSError):
        return None


# ---------- 编排 ----------

def sync_account(db, account_name=None, wallet_name=None, cfg=None, _opener=None):
    """把本地账户与 web 同名钱包双向同步。返回 {ok,...} 或 {ok:False,error}。"""
    c = cfg if cfg is not None else app_config.web_config()
    base = c.get("base_url")
    email = c.get("email")
    password = c.get("password")
    if not (base and email and password):
        return {"ok": False, "error": "web 未配置（需 base_url+email+password）"}
    account_name = account_name or c.get("account") or "主线"
    wallet_name = wallet_name or c.get("wallet") or account_name

    local = sync_client.serialize_account(db, account_name)
    if local is None:
        return {"ok": False, "error": "本地账户 '%s' 不存在" % account_name}

    token = login(base, email, password, _opener=_opener)
    if not token:
        return {"ok": False, "error": "web 登录失败（检查邮箱/密码/服务可达）"}

    web_state = push_pull(base, token, to_web_payload(local, wallet_name), _opener=_opener)
    if web_state is None:
        return {"ok": False, "error": "web /api/sync 不可达或同步失败"}

    merged = from_web_state(web_state, account_name, acc_type=local.get("type") or "paper")
    summary = sync_client.apply_account(db, merged)
    return {"ok": True, "account": account_name, "wallet": wallet_name, **summary,
            "synced_at": datetime.now().isoformat(timespec="seconds")}


def main():
    ap = argparse.ArgumentParser(description="skill ↔ smart-invest-web 双向同步")
    sub = ap.add_subparsers(dest="command")
    sub.required = True  # 3.6 兼容：属性方式设必填
    p = sub.add_parser("sync", help="双向同步一个账户 ↔ web 钱包")
    p.add_argument("--account", default=None, help="本地账户名（缺省用 app_config.web.account）")
    p.add_argument("--wallet", default=None, help="web 钱包名（缺省同账户名）")
    args = ap.parse_args()
    if args.command == "sync":
        from db import Database
        db = Database()
        res = sync_account(db, account_name=args.account, wallet_name=args.wallet)
        json.dump(res, sys.stdout, ensure_ascii=False, indent=2)
        print()


if __name__ == "__main__":
    main()
