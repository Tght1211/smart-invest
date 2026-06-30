#!/usr/bin/env python3
"""本地 Web 面板 — 随时用浏览器查看钱包/持仓/收益/K线/新闻/操作。

v2 重构：不再复用邮件 HTML。改为「JSON API + 单页响应式前端」：
  - 后端只产出 JSON（/api/overview、/api/kline），复用 daily_report / fetch_fund 的纯逻辑；
  - 前端是一份自包含的响应式页面（PC + 移动端自适应），用 ECharts 画指数 K 线、
    持仓净值走势、总收益曲线，点击持仓弹出抽屉看净值图 + 技术信号。
纯 Python 3 标准库（http.server），无第三方依赖；前端图表用 CDN 版 ECharts。

用法（对话里也可触发「打开面板 / 关闭面板」）:
  python3 scripts/web_panel.py start [--port 8765] [--host 127.0.0.1] [--account 主线]
  python3 scripts/web_panel.py stop
  python3 scripts/web_panel.py status
  python3 scripts/web_panel.py serve  [...]   # 前台运行（调试用，Ctrl-C 退出）
"""
import argparse
import json
import os
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
import fetch_fund  # noqa: E402
import llm_client  # noqa: E402
from db import Database  # noqa: E402

DATA_DIR = SCRIPT_DIR.parent / "data"
PID_FILE = DATA_DIR / "web_panel.pid"
LOG_FILE = DATA_DIR / "web_panel.log"

DEFAULT_PORT = 8765
DEFAULT_HOST = "127.0.0.1"
DEFAULT_ACCOUNT = "主线"
OVERVIEW_TTL = 75       # /api/overview 重算节流（秒）——避免每次刷新都打一堆行情接口
KLINE_TTL = 300         # K线缓存（秒）——历史数据变化慢
DISCOVER_TTL = 600      # /api/discover 缓存（秒）——发现新候选慢且变化慢，懒加载

AI_NOTE_TTL = 900       # /api/ai-note 缓存（秒）——LLM 点评慢且按时段变化不大

_OVERVIEW_CACHE = {}    # (account, session) -> (ts, dict)
_KLINE_CACHE = {}       # (target, days) -> (ts, dict)
_DISCOVER_CACHE = {}    # (account, n) -> (ts, dict)
_AINOTE_CACHE = {}      # (account, session) -> (ts, dict)


# ---------- 数据层 ----------

def _list_accounts():
    db = Database()
    try:
        names = [a["name"] for a in db.list_accounts()]
        # 主线置顶，梦境账户按名次之（回测 lab 账户较多，主账户优先）
        names.sort(key=lambda n: (n != DEFAULT_ACCOUNT, n.startswith("梦境"), n))
        return names or [DEFAULT_ACCOUNT]
    except Exception:
        return [DEFAULT_ACCOUNT]
    finally:
        db.close()


def _map_action(a):
    ctx = a.get("context") or {}
    return {
        "action": a.get("action"),
        "code": a.get("code"),
        "name": a.get("name"),
        "rule": a.get("rule_label") or a.get("rule_id"),
        "amount": a.get("suggested_amount"),
        "shares": a.get("suggested_shares"),
        "reason": a.get("reason_zh"),
        "horizon": ctx.get("horizon"),
        "share_class": ctx.get("share_class"),
    }


def _overview_data(account, session):
    """组装某账户某时段的总览 JSON（带 TTL 缓存）。绝不抛异常，错误进 error 字段。"""
    key = (account, session)
    hit = _OVERVIEW_CACHE.get(key)
    if hit and (time.time() - hit[0]) < OVERVIEW_TTL:
        return hit[1]

    clock = {}
    try:
        clock = fetch_fund.market_clock()
    except Exception:
        clock = {"time": datetime.now().strftime("%H:%M"), "session": "—"}

    db = Database()
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        # discover 解耦：主总览始终 discover=0（~0.3s 出钱包/持仓），
        # 「新方向」改由 /api/discover 懒加载，不阻塞首屏。
        ctx, err = daily_report.build_context(db, account, today, discover=0)
        if err:
            data = {"error": err, "account": account, "session": session,
                    "accounts": _list_accounts(), "clock": clock}
            _OVERVIEW_CACHE[key] = (time.time(), data)
            return data

        funds = ctx["funds"]
        positions = ctx["positions"]
        holdings = []
        cost_sum = pnl_sum = today_sum = pos_val = 0.0
        for p in positions:
            f = funds.get(p["code"]) or {}
            cur = f.get("current_nav") or p["cost_nav"]
            dr = f.get("day_return") or 0.0
            mv = p["shares"] * cur
            cost = p["shares"] * p["cost_nav"]
            pnl = mv - cost
            tpnl = 0.0 if p.get("is_pending") else daily_report._today_pnl(p["shares"], cur, dr)
            pos_val += mv
            cost_sum += cost
            pnl_sum += pnl
            today_sum += tpnl
            sig = f.get("signals") or {}
            holdings.append({
                "code": p["code"], "name": p["name"], "sector": p.get("sector") or "",
                "shares": round(p["shares"], 2), "cost_nav": round(p["cost_nav"], 4),
                "current_nav": round(cur, 4), "day_return": dr,
                "market_value": round(mv, 2), "hold_pnl": round(pnl, 2),
                "hold_pct": (cur / p["cost_nav"] - 1) if p["cost_nav"] else 0.0,
                "today_pnl": round(tpnl, 2), "hold_days": p.get("hold_days", 0),
                "is_pending": bool(p.get("is_pending")),
                "r5": f.get("fund_5d_return") or 0.0,
                "r20": f.get("fund_20d_return") or 0.0,
                "r60": f.get("fund_60d_return") or 0.0,
                "rsi": sig.get("rsi_14"), "macd": sig.get("macd_hist"),
                "ma_slope": sig.get("ma20_slope"), "breakout": sig.get("breakout_20d"),
            })
        holdings.sort(key=lambda h: h["market_value"], reverse=True)

        cash = ctx["cash"]
        total = cash + pos_val
        budget = ctx.get("budget") or 0.0
        return_pct = (pnl_sum / cost_sum) if cost_sum else 0.0

        packet = ctx["packet"] or {}
        actions = [_map_action(a) for a in (packet.get("actions") or [])]
        alerts = packet.get("alerts") or []
        regime = packet.get("market_regime") or {}

        news = []
        for n in (ctx["news"] or [])[:12]:
            news.append({"title": n.get("title", ""), "summary": n.get("summary", ""),
                         "time": n.get("time", ""), "url": n.get("url", "")})

        discovered = []
        for d in (ctx.get("discovered") or [])[:8]:
            discovered.append({"code": d.get("code"), "name": d.get("name"),
                               "sector": d.get("sector"), "score": d.get("score"),
                               "reason": d.get("reason_zh") or d.get("reason")})

        dca = []
        for d in (ctx.get("dca_plans") or []):
            dca.append({"code": d.get("code") if isinstance(d, dict) else d["code"],
                        "name": (d.get("name") if isinstance(d, dict) else d["name"]),
                        "amount": (d.get("amount") if isinstance(d, dict) else d["amount"]),
                        "period": (d.get("period") if isinstance(d, dict) else d["period"])})

        try:
            series = fetch_fund.portfolio_return_series(account, days=45)
            ret_series = [{"date": dt, "pct": pct} for dt, pct in (series or [])]
        except Exception:
            ret_series = []

        data = {
            "account": account, "session": session, "accounts": _list_accounts(),
            "clock": clock, "date": today,
            "wallet": {
                "total": round(total, 2), "position_value": round(pos_val, 2),
                "cash": round(cash, 2), "budget": round(budget, 2),
                "cost": round(cost_sum, 2), "hold_pnl": round(pnl_sum, 2),
                "today_pnl": round(today_sum, 2), "return_pct": return_pct,
                "reserve_line": round(total * 0.10, 2),
            },
            "holdings": holdings, "actions": actions, "alerts": alerts,
            "regime": regime, "news": news, "discovered": discovered,
            "dca": dca, "return_series": ret_series,
        }
        _OVERVIEW_CACHE[key] = (time.time(), data)
        return data
    except Exception as e:  # 兜底：任何异常都进 JSON，不让面板 500
        return {"error": f"{type(e).__name__}: {e}", "account": account,
                "session": session, "accounts": _list_accounts(), "clock": clock}
    finally:
        db.close()


# 指数选择器（前端 K 线卡片用）
INDEX_CHOICES = [
    {"key": "沪深300", "label": "沪深300"},
    {"key": "上证指数", "label": "上证"},
    {"key": "创业板指", "label": "创业板"},
    {"key": "中证500", "label": "中证500"},
    {"key": "NDX", "label": "纳指100"},
    {"key": "SPX", "label": "标普500"},
]


def _kline_data(target, days):
    """基金净值曲线 或 指数日K线 → JSON（带 TTL 缓存）。"""
    key = (target, days)
    hit = _KLINE_CACHE.get(key)
    if hit and (time.time() - hit[0]) < KLINE_TTL:
        return hit[1]
    try:
        resolved = fetch_fund._resolve_chart_target(target)
        if not resolved:
            return {"error": f"unknown target: {target}"}
        kind, ref, name = resolved
        if kind == "fund":
            series = fetch_fund.fetch_nav_series(ref, days) or []
            data = {"type": "nav", "name": name or ref, "code": ref,
                    "points": [{"date": d, "nav": n} for d, n in series]}
        else:
            k = fetch_fund.fetch_index_kline(ref, days)
            data = {"type": "ohlc", "name": k.get("name", target),
                    "points": k.get("points", [])}
        _KLINE_CACHE[key] = (time.time(), data)
        return data
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _discover_data(account, n=6):
    """跨板块发现新候选（懒加载，慢，单独缓存）。绝不抛异常。"""
    key = (account, n)
    hit = _DISCOVER_CACHE.get(key)
    if hit and (time.time() - hit[0]) < DISCOVER_TTL:
        return hit[1]
    db = Database()
    try:
        exclude = set()
        row = db.conn.execute("SELECT id FROM accounts WHERE name=?", (account,)).fetchone()
        if row:
            for p in db.conn.execute(
                    "SELECT code FROM positions WHERE account_id=?", (row["id"],)):
                exclude.add(p["code"])
        cands = fetch_fund.discover_candidates(limit=n, exclude=exclude, quality=True)
        out = {"discovered": [{
            "code": c.get("code"), "name": c.get("name"), "sector": c.get("sector"),
            "score": c.get("score"),
            "red_flags": [f["msg"] for f in c.get("red_flags", [])],
        } for c in cands]}
        _DISCOVER_CACHE[key] = (time.time(), out)
        return out
    except Exception as e:
        return {"discovered": [], "error": f"{type(e).__name__}: {e}"}
    finally:
        db.close()


def _ai_note_data(account, session):
    """LLM 一句话点评（懒加载，接 Anthropic 格式三方 API）。未配置返回空，优雅降级。"""
    if not llm_client.is_configured():
        return {"note": "", "enabled": False}
    key = (account, session)
    hit = _AINOTE_CACHE.get(key)
    if hit and (time.time() - hit[0]) < AI_NOTE_TTL:
        return hit[1]
    ov = _overview_data(account, session)
    if ov.get("error"):
        return {"note": "", "enabled": True}
    w = ov.get("wallet", {})
    hold = ov.get("holdings", [])
    top = "、".join(f"{h['name']}({h['day_return']*100:+.1f}%)" for h in hold[:4])
    prompt = (
        f"我的虚拟基金组合今日预估盈亏 {w.get('today_pnl', 0):+.0f} 元，"
        f"持仓收益率 {w.get('return_pct', 0)*100:+.1f}%，现金占比约 "
        f"{(w.get('cash', 0)/w.get('total', 1)*100) if w.get('total') else 0:.0f}%。"
        f"主要持仓今日：{top or '无'}。"
        f"请用一句话（40字内）给出稳健、克制的点评，不要给出买卖指令。"
    )
    note = llm_client.narrate(
        prompt, system="你是 Smart Invest 的投资助理，中文、简洁、克制，不荐股不给买卖指令。",
        max_tokens=120)
    out = {"note": note or "", "enabled": True}
    _AINOTE_CACHE[key] = (time.time(), out)
    return out


# ---------- HTTP ----------

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        if path in ("/favicon.ico", "/robots.txt"):
            self.send_response(204)
            self.end_headers()
            return
        if path == "/healthz":
            return self._write(200, "text/plain; charset=utf-8", b"ok")
        if path == "/" or path == "/index.html":
            return self._write(200, "text/html; charset=utf-8",
                               PAGE_HTML.encode("utf-8"))

        qs = parse_qs(u.query)
        if path == "/api/overview":
            account = qs.get("account", [self.server.default_account])[0]
            session = qs.get("session", ["close"])[0]
            if session not in ("open", "mid", "close"):
                session = "close"
            return self._json(_overview_data(account, session))
        if path == "/api/kline":
            target = qs.get("code", [qs.get("target", [""])[0]])[0]
            try:
                days = max(20, min(250, int(qs.get("days", ["120"])[0])))
            except ValueError:
                days = 120
            if not target:
                return self._json({"error": "missing code/target"})
            return self._json(_kline_data(target, days))
        if path == "/api/discover":
            account = qs.get("account", [self.server.default_account])[0]
            try:
                n = max(3, min(12, int(qs.get("n", ["6"])[0])))
            except ValueError:
                n = 6
            return self._json(_discover_data(account, n))
        if path == "/api/ai-note":
            account = qs.get("account", [self.server.default_account])[0]
            session = qs.get("session", ["close"])[0]
            return self._json(_ai_note_data(account, session))

        self._write(404, "text/plain; charset=utf-8", b"not found")

    def _json(self, obj):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self._write(200, "application/json; charset=utf-8", body)

    def _write(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def log_message(self, *args):
        pass   # 静默


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


# 前端页面（自包含；数据全部走 /api/*）。放在文件末尾以免干扰阅读后端逻辑。
PAGE_HTML = (SCRIPT_DIR / "web_panel.html").read_text(encoding="utf-8") \
    if (SCRIPT_DIR / "web_panel.html").exists() else "<h1>web_panel.html missing</h1>"


if __name__ == "__main__":
    main()
