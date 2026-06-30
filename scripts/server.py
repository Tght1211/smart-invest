#!/usr/bin/env python3
"""Smart Invest 在线同步服务器（多用户）—— 本地可跑，部署到自有服务器即上云。

职责：token 鉴权 → 按用户隔离钱包 → 接收客户端账户状态、与服务器侧权威状态合并
（trades 并集、positions/cash LWW，复用 sync_state 纯函数）→ 回传合并结果。
持久层用 db.Database（SQLite；服务器 DB 路径走 `SMART_INVEST_SERVER_DB`），
用户隔离用账户名命名空间 `u<uid>:<account>`，复用现有 accounts/positions/trades 表，
不引入新 schema。多用户、多虚拟钱包天然支持。纯 stdlib http.server，无三方依赖。

token 映射来源（任一）：环境 `SMART_INVEST_SERVER_TOKENS`(JSON {token:user_id}) 或
`data/server_tokens.json`。生产部署时换成真正的用户表 + 签发流程（见架构文档 P5）。

  python3 scripts/server.py serve --host 0.0.0.0 --port 8800
"""
import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import sync_client  # noqa: E402
import sync_state  # noqa: E402

DATA_DIR = SCRIPT_DIR.parent / "data"
TOKENS_FILE = DATA_DIR / "server_tokens.json"


def load_tokens():
    """{token: user_id}。环境变量优先，其次文件，都没有则空（拒绝所有请求）。"""
    env = os.environ.get("SMART_INVEST_SERVER_TOKENS")
    if env:
        try:
            return json.loads(env)
        except (json.JSONDecodeError, ValueError):
            pass
    if TOKENS_FILE.exists():
        try:
            return json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError, OSError):
            pass
    return {}


def _ns(user_id, account):
    """用户命名空间下的账户名（服务器内部存储用，对客户端透明）。"""
    return f"u{user_id}:{account}"


def handle_sync(server_db, user_id, account_name, client_state):
    """服务器侧合并一个账户：取服务器现状 → merge → 回写 → 返回合并结果（去命名空间）。

    纯逻辑封装（无 HTTP），便于单测与本地往返测试。
    """
    ns = _ns(user_id, account_name)
    server_state = sync_client.serialize_account(server_db, ns)  # 没有则 None
    # 把客户端状态的账户名临时改成命名空间名再合并/回写
    incoming = dict(client_state, account=ns)
    if server_state is None:
        server_state = {"account": ns, "type": incoming.get("type") or "paper",
                        "budget": incoming.get("budget"), "cash": incoming.get("cash"),
                        "positions": [], "trades": []}
    merged = sync_state.merge_account(server_state, incoming)
    merged["account"] = ns
    sync_client.apply_account(server_db, merged)
    # 回传给客户端时去掉命名空间
    out = dict(merged, account=account_name)
    return out


def list_wallets(server_db, user_id):
    prefix = _ns(user_id, "")
    return [a["name"][len(prefix):] for a in server_db.list_accounts()
            if a["name"].startswith(prefix)]


class _Handler(BaseHTTPRequestHandler):
    def _auth(self):
        tok = (self.headers.get("authorization") or "").replace("Bearer ", "").strip()
        if not tok:
            # 也允许 body 里带 token（push_pull 同时放了 header 和 body）
            return None, tok
        return self.server.tokens.get(tok), tok

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            return self._write(200, b"ok", "text/plain")
        if path == "/api/wallets":
            uid, _ = self._auth()
            if uid is None:
                return self._json(401, {"error": "unauthorized"})
            return self._json(200, {"wallets": list_wallets(self.server.db, uid)})
        self._write(404, b"not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8", "ignore"))
        except (json.JSONDecodeError, ValueError):
            return self._json(400, {"error": "bad json"})
        if path == "/api/sync":
            uid, _ = self._auth()
            if uid is None:
                uid = self.server.tokens.get(payload.get("token"))
            if uid is None:
                return self._json(401, {"error": "unauthorized"})
            account = payload.get("account")
            state = payload.get("state")
            if not account or not isinstance(state, dict):
                return self._json(400, {"error": "missing account/state"})
            try:
                with self.server.lock:  # 串行化单连接的跨线程访问
                    merged = handle_sync(self.server.db, uid, account, state)
                return self._json(200, {"merged": merged})
            except Exception as e:  # 不让单个请求打挂服务
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        self._write(404, b"not found", "text/plain")

    def _json(self, code, obj):
        self._write(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8")

    def _write(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def log_message(self, *a):
        pass


def make_server(host, port, db=None, tokens=None):
    """构造 HTTP 服务器（注入 db/tokens 便于测试）。"""
    import threading
    from db import Database
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.db = db or Database()
    httpd.tokens = tokens if tokens is not None else load_tokens()
    httpd.lock = threading.Lock()
    return httpd


def cmd_serve(args):
    httpd = make_server(args.host, args.port)
    print(f"[OK] 同步服务器启动: http://{args.host}:{args.port}/  "
          f"(tokens: {len(httpd.tokens)} 个用户)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    ap = argparse.ArgumentParser(description="Smart Invest 在线同步服务器")
    sub = ap.add_subparsers(dest="command")
    p = sub.add_parser("serve", help="前台运行同步服务器")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8800)
    args = ap.parse_args()
    if args.command == "serve":
        cmd_serve(args)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
