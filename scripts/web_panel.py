#!/usr/bin/env python3
"""本地 Web 面板 — 随时用浏览器查看钱包/持仓/收益/新闻/操作。

复用 daily_report 的卡片组装 + send_email 的 HTML 渲染，所以面板和邮件长得一样。
纯 Python 3 标准库（http.server），无第三方依赖。

用法（对话里也可触发「打开面板 / 关闭面板」）:
  python3 scripts/web_panel.py start [--port 8765] [--host 127.0.0.1] [--account 主线]
  python3 scripts/web_panel.py stop
  python3 scripts/web_panel.py status
  python3 scripts/web_panel.py serve  [...]   # 前台运行（调试用，Ctrl-C 退出）

说明:
  - start 默认后台启动并立即返回 URL；浏览器打开即可，页面每 ~2 分钟自动刷新。
  - 想在同一局域网其它设备访问，用 --host 0.0.0.0（注意：会暴露给同网段，谨慎）。
  - stop 通过 PID 文件结束进程（对话说「关闭面板」即调它）。
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import daily_report  # noqa: E402
import send_email  # noqa: E402
from db import Database  # noqa: E402

DATA_DIR = SCRIPT_DIR.parent / "data"
PID_FILE = DATA_DIR / "web_panel.pid"
LOG_FILE = DATA_DIR / "web_panel.log"

DEFAULT_PORT = 8765
DEFAULT_HOST = "127.0.0.1"
DEFAULT_ACCOUNT = "主线"
CACHE_TTL = 90          # 卡片重算节流（秒）——避免每次刷新都打一堆行情接口
REFRESH_SECONDS = 120   # 浏览器自动刷新间隔

_CACHE = {}             # (account, session) -> (ts, markdown)


# ---------- 渲染 ----------

def _build_markdown(account, session):
    """组装某账户某时段的卡片 markdown（带 TTL 缓存）。"""
    key = (account, session)
    hit = _CACHE.get(key)
    if hit and (time.time() - hit[0]) < CACHE_TTL:
        return hit[1]
    db = Database()
    try:
        ctx, err = daily_report.build_context(
            db, account, datetime.now().strftime("%Y-%m-%d"))
        if err:
            raise RuntimeError(err)
        md = daily_report.assemble(db, ctx, session, recorded=[], skipped=[])
    finally:
        db.close()
    _CACHE[key] = (time.time(), md)
    return md


def _nav_bar(account, session):
    accounts = _list_accounts()
    sess_labels = {"open": "开盘", "mid": "盘中", "close": "盘尾"}
    sess_links = " ".join(
        (f'<b>{lbl}</b>' if s == session
         else f'<a href="?account={account}&session={s}">{lbl}</a>')
        for s, lbl in sess_labels.items()
    )
    acc_links = " ".join(
        (f'<b>{a}</b>' if a == account
         else f'<a href="?account={a}&session={session}">{a}</a>')
        for a in accounts
    ) or account
    now = datetime.now().strftime("%H:%M:%S")
    return (
        '<div style="max-width:420px;margin:8px auto 0;padding:10px 14px;'
        'font:13px -apple-system,PingFang SC,Helvetica,sans-serif;color:#333;'
        'background:#fff;border-radius:10px;box-shadow:0 1px 3px rgba(0,0,0,.08)">'
        f'<div>📊 <b>Smart Invest 面板</b> · 账户 {acc_links}</div>'
        f'<div style="margin-top:6px">时段 {sess_links}'
        f'<span style="float:right;color:#999">更新 {now}</span></div>'
        '</div>'
    )


def _list_accounts():
    db = Database()
    try:
        return [a["name"] for a in db.list_accounts()]
    except Exception:
        return [DEFAULT_ACCOUNT]
    finally:
        db.close()


def render_dashboard(account, session="close"):
    """返回完整 HTML 页面（卡片 + 顶部导航 + 自动刷新）。"""
    try:
        md = _build_markdown(account, session)
        page = send_email.markdown_to_html(md)
    except Exception as e:
        page = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<meta http-equiv='refresh' content='{REFRESH_SECONDS}'></head>"
            "<body style='font:14px sans-serif;padding:24px'>"
            f"<h2>面板暂时生成失败</h2><pre style='color:#900'>{e}</pre>"
            "<p>多为行情接口波动，稍后自动重试。</p></body></html>"
        )
        return page

    refresh = f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">'
    if "</head>" in page:
        page = page.replace("</head>", refresh + "</head>", 1)
    else:
        page = refresh + page

    nav = _nav_bar(account, session)
    m = re.search(r"<body[^>]*>", page)
    if m:
        page = page[:m.end()] + nav + page[m.end():]
    else:
        page = nav + page
    return page


# ---------- HTTP ----------

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/favicon.ico", "/robots.txt"):
            self.send_response(204)
            self.end_headers()
            return
        if u.path == "/healthz":
            self._write(200, "text/plain; charset=utf-8", b"ok")
            return
        qs = parse_qs(u.query)
        account = qs.get("account", [self.server.default_account])[0]
        session = qs.get("session", ["close"])[0]
        if session not in ("open", "mid", "close"):
            session = "close"
        html = render_dashboard(account, session)
        self._write(200, "text/html; charset=utf-8", html.encode("utf-8"))

    def _write(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def log_message(self, *args):
        pass   # 静默，避免污染日志


# ---------- 进程管理 ----------

def _read_pid():
    if not PID_FILE.exists():
        return None
    try:
        return json.loads(PID_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def cmd_serve(args):
    """前台运行 HTTP 服务（start 后台调它）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((args.host, args.port), _Handler)
    httpd.default_account = args.account
    url = f"http://{args.host}:{args.port}/"
    PID_FILE.write_text(json.dumps({
        "pid": os.getpid(), "host": args.host, "port": args.port,
        "account": args.account, "url": url,
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False), encoding="utf-8")

    def _bye(*_):
        try:
            PID_FILE.unlink(missing_ok=True)
        finally:
            os._exit(0)
    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)
    print(f"[OK] Web 面板已启动: {url}")
    try:
        httpd.serve_forever()
    finally:
        PID_FILE.unlink(missing_ok=True)


def cmd_start(args):
    """后台启动面板，立即返回 URL。已在运行则直接给出现有 URL。"""
    info = _read_pid()
    if info and _alive(info.get("pid", -1)):
        print(f"[OK] 面板已在运行: {info.get('url')}（PID {info['pid']}）")
        print("     如需关闭：python3 scripts/web_panel.py stop")
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if args.foreground:
        cmd_serve(args)
        return

    logf = open(LOG_FILE, "ab")
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT_DIR / "web_panel.py"), "serve",
         "--host", args.host, "--port", str(args.port), "--account", args.account],
        stdout=logf, stderr=logf, start_new_session=True,
    )
    # 等待 PID 文件出现，确认起来了
    url = f"http://{args.host}:{args.port}/"
    for _ in range(20):
        time.sleep(0.15)
        info = _read_pid()
        if info and info.get("pid") == proc.pid and _alive(proc.pid):
            break
    if proc.poll() is not None:
        print(f"[ERROR] 面板启动失败，查看日志: {LOG_FILE}", file=sys.stderr)
        return
    print(f"[OK] Web 面板已后台启动: {url}")
    print(f"     浏览器打开:  open {url}")
    print("     关闭面板:    python3 scripts/web_panel.py stop")


def cmd_stop(args):
    info = _read_pid()
    if not info or not _alive(info.get("pid", -1)):
        PID_FILE.unlink(missing_ok=True)
        print("面板未在运行")
        return
    pid = info["pid"]
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        print(f"[WARN] 结束进程失败: {e}", file=sys.stderr)
    for _ in range(20):
        if not _alive(pid):
            break
        time.sleep(0.1)
    if _alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    PID_FILE.unlink(missing_ok=True)
    print(f"[OK] 面板已关闭（PID {pid}）")


def cmd_status(args):
    info = _read_pid()
    if info and _alive(info.get("pid", -1)):
        print(f"RUNNING  {info.get('url')}  (PID {info['pid']}, "
              f"账户 {info.get('account')}, 启动 {info.get('started_at')})")
    else:
        print("STOPPED")


def main():
    ap = argparse.ArgumentParser(description="Smart Invest 本地 Web 面板")
    sub = ap.add_subparsers(dest="command")

    for name, helptext in (("start", "后台启动面板"), ("serve", "前台运行（调试）")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--host", default=DEFAULT_HOST,
                       help="绑定地址（0.0.0.0 可局域网访问，谨慎）")
        p.add_argument("--port", type=int, default=DEFAULT_PORT, help="端口")
        p.add_argument("--account", default=DEFAULT_ACCOUNT, help="默认账户")
        if name == "start":
            p.add_argument("--foreground", action="store_true",
                           help="前台运行不后台化")

    sub.add_parser("stop", help="关闭面板")
    sub.add_parser("status", help="查看面板状态")

    args = ap.parse_args()
    if not args.command:
        ap.print_help()
        return
    {"start": cmd_start, "serve": cmd_serve,
     "stop": cmd_stop, "status": cmd_status}[args.command](args)


if __name__ == "__main__":
    main()
