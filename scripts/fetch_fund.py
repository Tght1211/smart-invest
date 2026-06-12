#!/usr/bin/env python3
"""
基金数据抓取脚本 — Smart Invest Skill 数据源
纯 Python 3 标准库，无第三方依赖。
数据源：天天基金 / 东方财富（免费公开接口）
"""

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR.parent / "data"
PORTFOLIO_FILE = DATA_DIR / "portfolio.json"
ORDERS_FILE = DATA_DIR / "orders.json"

# 导入数据库模块
sys.path.insert(0, str(SCRIPT_DIR))
try:
    from db import Database
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://fund.eastmoney.com/",
}

# 主要指数 secid 映射
INDICES = {
    "上证指数": "1.000001",
    "深证成指": "0.399001",
    "创业板指": "0.399006",
    "沪深300": "1.000300",
    "中证500": "1.000905",
    "上证50": "1.000016",
}

# 美股指数 secid（eastmoney 美股行情）。QDII 基金当日净值方向 ≈ 对应美股指数隔夜涨跌，
# 美股北京时间约 21:30–次日 04:00 交易，故凌晨即可大致判断当日 QDII 结算涨跌。
US_INDICES = {
    "纳斯达克100": "100.NDX",
    "标普500": "100.SPX",
    "道琼斯": "100.DJIA",
}

# QDII 基金 → 参考美股指数（看该指数隔夜涨跌判断基金当日方向）
QDII_INDEX_MAP = {
    "006479": "纳斯达克100",   # 广发纳斯达克100ETF联接C → NDX
}


def fund_venue(code, name):
    """判断基金交易场所：场内(需证券账户) vs 场外(支付宝可直接申购)。

    用户只用支付宝，只能买场外基金。判定规则（以名称为准，最可靠）：
    - 纯 ETF（名称含 "ETF" 但不含 "联接"）= 场内，需开证券账户，支付宝买不了。
    - ETF联接 / LOF / 普通指数 / 股票 / 混合 / QDII联接 = 场外，支付宝可直接申购。
    LOF（如 161725 招商白酒）是双渠道，支付宝可买，按场外处理。
    """
    nm = name or ""
    is_pure_etf = ("ETF" in nm.upper()) and ("联接" not in nm)
    return "场内" if is_pure_etf else "场外"


def is_otc(code, name):
    """是否场外（支付宝可直接申购）"""
    return fund_venue(code, name) == "场外"


def _get(url, headers=None, retries=2):
    """通用 HTTP GET 请求，带重试"""
    req_headers = dict(HEADERS)
    if headers:
        req_headers.update(headers)
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=req_headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read().decode("utf-8")
        except Exception as e:
            if attempt < retries:
                import time
                time.sleep(0.5)
                continue
            # 东财 CDN 偶发掐 Python 的 https 连接（TLS 指纹），公开行情接口可降级 http 再试一次
            if url.startswith("https://"):
                try:
                    req = urllib.request.Request(
                        "http://" + url[len("https://"):], headers=req_headers)
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        return resp.read().decode("utf-8")
                except Exception:
                    pass
            print(f"[ERROR] 请求失败: {url}\n  {e}", file=sys.stderr)
            return None


def _parse_jsonp(text):
    """解析 JSONP 格式，提取 JSON 部分"""
    if not text:
        return None
    m = re.search(r"\((\{.*\})\)", text)
    if m:
        return json.loads(m.group(1))
    return None


# ========== 免费财经新闻（东方财富 7x24 快讯，无需 key） ==========

NEWS_ENDPOINTS = [
    # 7x24 全球财经快讯（var ajaxResult={...}）
    "https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_50_1_.html",
    # 备用：web fast news 接口
    "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
    "?client=web&biz=web_724&fastColumn=102&sortEnd=&pageSize=30&req_trace=si",
]


def _extract_json_blob(text):
    """从 JS/JSONP（var x={...}）响应里抠出第一个 JSON 对象。失败返回 None。"""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _news_items_from(blob):
    """从不同结构的东财响应里取新闻列表 → [{time,title,summary,url}]。"""
    if not isinstance(blob, dict):
        return []
    items = None
    for key in ("LivesList", "fastNewsList", "list", "newslist", "News"):
        if isinstance(blob.get(key), list):
            items = blob[key]
            break
    if items is None and isinstance(blob.get("data"), dict):
        for key in ("fastNewsList", "list", "News", "newslist", "LivesList"):
            if isinstance(blob["data"].get(key), list):
                items = blob["data"][key]
                break
    out = []
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        title = (it.get("title") or it.get("Title") or it.get("digest") or "").strip()
        if not title:
            continue
        summary = (it.get("digest") or it.get("summary") or it.get("Digest") or "").strip()
        t = (it.get("showtime") or it.get("showTime") or it.get("ShowTime")
             or it.get("time") or "")
        url = (it.get("url_w") or it.get("url") or it.get("Url_W") or it.get("Url") or "")
        out.append({"time": str(t)[:16], "title": title,
                    "summary": summary[:120], "url": url})
    return out


def gather_market_news(keyword=None, limit=10):
    """免费财经快讯（东方财富 7x24）。失败返回 []，绝不抛异常。

    keyword 给定时按 标题+摘要 关键词过滤（如 半导体/纳指/降准）。
    这是**报告层**数据，不驱动引擎决策。供 daily_report / market-snapshot 的无 LLM 路使用；
    Claude 在交互会话里分析时优先用 WebSearch 拿更丰富的当日新闻，本函数是定时路兜底。
    """
    items = []
    for ep in NEWS_ENDPOINTS:
        text = _get(ep, headers={"Referer": "https://kuaixun.eastmoney.com/"})
        items = _news_items_from(_extract_json_blob(text))
        if items:
            break
    if keyword:
        kw = str(keyword).strip()
        items = [it for it in items
                 if kw in it["title"] or kw in it.get("summary", "")]
    return items[:limit]


def cmd_news(args):
    """免费财经快讯（可 --keyword 过滤）"""
    items = gather_market_news(keyword=args.keyword, limit=args.limit)
    if not items:
        print("（未取到快讯——交互分析时请改用 WebSearch 拿当日新闻）")
        return
    kw = f"（关键词: {args.keyword}）" if args.keyword else ""
    print(f"\n📰 财经要闻 {kw}— 东方财富 7x24")
    print("-" * 80)
    for it in items:
        ts = f"[{it['time']}] " if it.get("time") else ""
        print(f"{ts}{it['title']}")
        if it.get("summary"):
            print(f"    {it['summary']}")
    print()


# ========== 持有天数 / 收益趋势 辅助 ==========

def _held_days(buy_date, ref=None):
    """持有天数：买入日期 → 今天（自然日）。无法解析返回 None。"""
    if not buy_date:
        return None
    ref = ref or datetime.now()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            d = datetime.strptime(str(buy_date)[:10], fmt)
            return max(0, (ref.date() - d.date()).days)
        except ValueError:
            continue
    return None


def _sparkline(values):
    """数值序列 → unicode 迷你走势（▁▂▃▄▅▆▇█）。None 渲染为空格。"""
    bars = "▁▂▃▄▅▆▇█"
    nums = [v for v in values if v is not None]
    if not nums:
        return ""
    lo, hi = min(nums), max(nums)
    if hi == lo:
        return bars[3] * len(values)
    out = ""
    for v in values:
        if v is None:
            out += " "
            continue
        idx = int((v - lo) / (hi - lo) * (len(bars) - 1))
        out += bars[idx]
    return out


def _align_total_return_series(holdings):
    """把多只基金的净值序列对齐成组合「总收益率」时间序列（网络派生，不依赖快照）。

    holdings: [(shares, cost_nav, [(date, nav)])]
    返回 [(date, total_return_pct)]，仅取所有基金都有净值的日期区间（前向填充）。
    """
    series_maps = []
    total_cost = 0.0
    for shares, cost, series in holdings:
        if not series or not cost or shares <= 0:
            continue
        series_maps.append((shares, dict(series), [d for d, _ in series]))
        total_cost += shares * cost
    if not series_maps or total_cost <= 0:
        return []

    all_dates = sorted({d for _, m, _ in series_maps for d in m})
    # 起点 = 各基金最早净值日的最大值（保证每只都有数据）
    start = max(dates[0] for _, _, dates in series_maps)
    out = []
    last_nav = {}
    for d in all_dates:
        for i, (_, m, _) in enumerate(series_maps):
            if d in m:
                last_nav[i] = m[d]
        if d < start or len(last_nav) < len(series_maps):
            continue
        total_val = sum(series_maps[i][0] * last_nav[i] for i in range(len(series_maps)))
        out.append((d, (total_val - total_cost) / total_cost))
    return out


def cmd_indices(args):
    """获取大盘指数实时行情"""
    secids = ",".join(INDICES.values())
    url = (
        f"https://push2.eastmoney.com/api/qt/ulist.np/get?"
        f"fltt=2&invt=2&ut=fa5fd1943c7b386f172d6893dbfba10b"
        f"&fields=f2,f3,f4,f6,f12,f14,f104,f105"
        f"&secids={secids}"
    )
    text = _get(url)
    if not text:
        print("获取指数数据失败")
        return

    data = json.loads(text)
    items = data.get("data", {}).get("diff", [])
    if not items:
        print("未获取到指数数据")
        return

    print("=" * 70)
    print(f"{'指数名称':<10} {'最新点位':>10} {'涨跌幅':>8} {'涨跌点':>10} {'成交额(亿)':>12}")
    print("-" * 70)
    for item in items:
        name = item.get("f14", "")
        price = item.get("f2", 0)
        pct = item.get("f3", 0)
        change = item.get("f4", 0)
        volume = item.get("f6", 0)
        vol_yi = volume / 1e8 if volume else 0
        sign = "+" if pct >= 0 else ""
        print(f"{name:<10} {price:>10.2f} {sign}{pct:>6.2f}% {sign}{change:>9.2f} {vol_yi:>11.1f}")
    print("=" * 70)


def _parse_us_index(data, fallback_name):
    """从 eastmoney ulist 响应里解出美股指数 {name, price, pct}。纯函数，便于测试。"""
    items = (data or {}).get("data", {}).get("diff") or []
    if not items:
        return None
    it = items[0]
    return {
        "name": it.get("f14") or fallback_name,
        "price": it.get("f2"),
        "pct": it.get("f3"),
    }


def fetch_us_index(name="纳斯达克100"):
    """抓美股指数实时/隔夜行情，返回 {name, price, pct} 或 None。
    用于 QDII 基金（如 006479）的当日方向判断 —— 看 NDX 隔夜涨跌。"""
    secid = US_INDICES.get(name)
    if not secid:
        return None
    url = (
        "https://push2.eastmoney.com/api/qt/ulist.np/get?"
        "fltt=2&invt=2&ut=fa5fd1943c7b386f172d6893dbfba10b"
        f"&fields=f2,f3,f4,f12,f14&secids={secid}"
    )
    text = _get(url)
    if not text:
        return None
    try:
        return _parse_us_index(json.loads(text), name)
    except Exception:
        return None


def qdii_overnight_signal(code):
    """给定 QDII 基金代码，返回其参考美股指数的隔夜涨跌 dict（含 fund_code/index_name），
    无映射或抓取失败返回 None。供开盘/盘尾分析判断 006479 等当日方向。"""
    idx_name = QDII_INDEX_MAP.get(code)
    if not idx_name:
        return None
    res = fetch_us_index(idx_name)
    if not res:
        return None
    res["fund_code"] = code
    res["index_name"] = idx_name
    return res


def _parse_trend(data):
    """解析 eastmoney trends2 分时响应 → {name, pre_close, points:[(time, price)]}。
    纯函数，便于测试。空数据返回 None。"""
    d = (data or {}).get("data")
    if not d or not d.get("trends"):
        return None
    points = []
    for row in d["trends"]:
        segs = row.split(",")
        if len(segs) >= 2:
            points.append((segs[0], float(segs[1])))
    return {"name": d.get("name", ""), "pre_close": d.get("preClose"), "points": points}


def fetch_index_trend(secid, ndays=1):
    """抓指数分时（A股或美股 secid 通用），失败返回 None。"""
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/trends2/get?"
        f"secid={secid}&fields1=f1,f2,f3,f7,f8&fields2=f51,f53&iscr=0&ndays={ndays}"
    )
    text = _get(url)
    if not text:
        return None
    try:
        return _parse_trend(json.loads(text))
    except Exception:
        return None


_US_ALIASES = {"NDX": "纳斯达克100", "SPX": "标普500", "DJIA": "道琼斯"}


def _resolve_chart_target(target):
    """chart 子命令目标解析：指数名/美股别名/6位基金代码。
    返回 ("index", secid, name) | ("fund", code, None) | None。纯函数。"""
    t = target.strip()
    alias = _US_ALIASES.get(t.upper())
    if alias:
        return ("index", US_INDICES[alias], alias)
    if t in US_INDICES:
        return ("index", US_INDICES[t], t)
    if t in INDICES:
        return ("index", INDICES[t], t)
    if t.isdigit() and len(t) == 6:
        return ("fund", t, None)
    return None


def fetch_nav_series(code, days=60):
    """基金历史净值（升序时间）→ [(date, nav)]，失败返回 []。

    东财 lsjz 接口每页固定 20 根（pageSize 参数不生效），需用 pageIndex 翻页，
    故此处翻页累积到取够 `days` 根（newest-first → 反转为升序）。
    """
    out = []
    page = 1
    max_pages = days // 20 + 3  # 多翻几页兜底，避免节假日导致的稀疏
    while len(out) < days and page <= max_pages:
        url = (
            f"https://api.fund.eastmoney.com/f10/lsjz?"
            f"fundCode={code}&pageIndex={page}&pageSize=20"
        )
        text = _get(url, headers={"Referer": "http://fundf10.eastmoney.com/"})
        if not text:
            break
        try:
            nav_list = json.loads(text).get("Data", {}).get("LSJZList", [])
        except Exception:
            break
        if not nav_list:
            break  # 翻到底
        for item in nav_list:
            try:
                out.append((item.get("FSRQ", ""), float(item.get("DWJZ"))))
            except (TypeError, ValueError):
                continue
        if len(nav_list) < 20:
            break  # 最后一页
        page += 1
    out = out[:days]      # newest-first，截到所需天数
    out.reverse()         # → 升序
    return out


def cmd_chart(args):
    """终端走势图：指数分时 / 基金净值曲线。"""
    import chart as chart_mod

    resolved = _resolve_chart_target(args.target)
    if not resolved:
        print(f"不认识的目标: {args.target}（支持指数名如 纳斯达克100/沪深300、别名 NDX/SPX/DJIA、6位基金代码）")
        return
    kind, key, name = resolved
    height, width = args.height, args.width

    if kind == "index":
        trend = fetch_index_trend(key, ndays=args.ndays)
        if not trend or len(trend["points"]) < 2:
            print(f"获取 {name} 分时数据失败")
            return
        prices = [p[1] for p in trend["points"]]
        times = [p[0][11:16] for p in trend["points"]]
        pre = trend.get("pre_close")
        last = prices[-1]
        chg = (last - pre) / pre * 100 if pre else 0.0
        sign = "+" if chg >= 0 else ""
        print(f"\n  {trend['name'] or name}  {last:,.2f}  {sign}{chg:.2f}%   "
              f"(昨收 {pre:,.2f} | 高 {max(prices):,.2f} | 低 {min(prices):,.2f})\n")
        print(chart_mod.render_chart(prices, height=height, width=width))
        print(chart_mod.axis_line([times[0], times[len(times) // 2], times[-1]], width, 12))
    else:
        series = fetch_nav_series(key, days=args.days)
        if len(series) < 2:
            print(f"获取基金 {key} 净值数据失败")
            return
        navs = [nav for _, nav in series]
        dates = [d for d, _ in series]
        chg = (navs[-1] - navs[0]) / navs[0] * 100 if navs[0] else 0.0
        sign = "+" if chg >= 0 else ""
        print(f"\n  基金 {key} 近 {len(navs)} 个交易日净值  最新 {navs[-1]:.4f}  "
              f"区间 {sign}{chg:.2f}%\n")
        print(chart_mod.render_chart(navs, height=height, width=width,
                                     label_fmt="{:>10.4f}"))
        print(chart_mod.axis_line([dates[0], dates[len(dates) // 2], dates[-1]], width, 12))


def fetch_indices():
    """返回大盘指数 [{name, pct, price}]，供卡片热力块。失败返回 []。"""
    url = (
        "https://push2.eastmoney.com/api/qt/ulist.np/get?"
        "fltt=2&invt=2&ut=fa5fd1943c7b386f172d6893dbfba10b"
        f"&fields=f2,f3,f12,f14&secids={','.join(INDICES.values())}"
    )
    text = _get(url)
    if not text:
        return []
    try:
        items = json.loads(text).get("data", {}).get("diff") or []
    except Exception:
        return []
    return [
        {"name": it.get("f14", ""), "pct": it.get("f3"), "price": it.get("f2")}
        for it in items
    ]


def fetch_sectors(top_n=5):
    """返回涨幅前 top_n 行业板块 [{name, pct}]，供卡片热力块。失败返回 []。"""
    url = (
        "https://push2.eastmoney.com/api/qt/clist/get?"
        "pn=1&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:90+t:2&fields=f3,f14"
    )
    text = _get(url)
    if not text:
        return []
    try:
        items = json.loads(text).get("data", {}).get("diff") or []
    except Exception:
        return []
    items = [it for it in items if it.get("f3") is not None]
    items.sort(key=lambda x: x["f3"], reverse=True)
    return [{"name": it.get("f14", ""), "pct": it.get("f3")} for it in items[:top_n]]


def cmd_us_index(args):
    """打印美股指数隔夜行情（QDII 方向判断）。"""
    name = args.name
    res = fetch_us_index(name)
    if not res or res.get("pct") is None:
        print(f"获取美股指数失败: {name}（secid={US_INDICES.get(name, '?')}，请在联网环境核对）")
        return
    pct = res["pct"]
    sign = "+" if pct >= 0 else ""
    arrow = "📈涨" if pct > 0 else ("📉跌" if pct < 0 else "持平")
    print(f"{res['name']}  {res['price']}  {sign}{pct}%  → 对应 QDII 今日预计{arrow}")


def cmd_sectors(args):
    """获取行业板块涨跌排行"""
    # 获取全部板块（按涨幅降序）
    url = (
        "https://push2.eastmoney.com/api/qt/clist/get?"
        "pn=1&pz=100&po=1&np=1&fltt=2&invt=2"
        "&fid=f3&fs=m:90+t:2"
        "&fields=f2,f3,f4,f12,f14"
    )
    text = _get(url)
    if not text:
        print("获取板块数据失败")
        return

    data = json.loads(text)
    items = data.get("data", {}).get("diff", [])
    if not items:
        print("未获取到板块数据")
        return

    # 涨幅前15
    gainers = sorted(items, key=lambda x: x.get("f3", 0) if x.get("f3") is not None else 0, reverse=True)[:15]
    # 跌幅前15
    losers = sorted(items, key=lambda x: x.get("f3", 0) if x.get("f3") is not None else 0)[:15]

    print("\n📈 涨幅前15行业板块:")
    print("-" * 40)
    for i, item in enumerate(gainers, 1):
        name = item.get("f14", "")
        pct = item.get("f3", 0)
        print(f"  {i:>2}. {name:<12} {pct:>+6.2f}%")

    print(f"\n📉 跌幅前15行业板块:")
    print("-" * 40)
    for i, item in enumerate(losers, 1):
        name = item.get("f14", "")
        pct = item.get("f3", 0)
        print(f"  {i:>2}. {name:<12} {pct:>+6.2f}%")


def cmd_estimate(args):
    """获取单只基金实时估值"""
    code = args.code
    url = f"http://fundgz.1234567.com.cn/js/{code}.js"
    text = _get(url)
    if not text:
        print(f"获取基金 {code} 估值失败")
        return

    data = _parse_jsonp(text)
    if not data:
        print(f"基金 {code} 估值数据解析失败（可能已下线估值功能）")
        return

    print(f"\n{'=' * 50}")
    print(f"基金代码: {data.get('fundcode', '')}")
    print(f"基金名称: {data.get('name', '')}")
    print(f"净值日期: {data.get('jzrq', '')}")
    print(f"单位净值: {data.get('dwjz', '')}")
    print(f"估算净值: {data.get('gsz', '')}")
    gszzl = data.get('gszzl', '0')
    sign = "+" if float(gszzl) >= 0 else ""
    print(f"估算涨幅: {sign}{gszzl}%")
    print(f"估值时间: {data.get('gztime', '')}")
    print(f"{'=' * 50}")


def cmd_nav(args):
    """获取基金历史净值"""
    code = args.code
    days = args.days
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    url = (
        f"https://api.fund.eastmoney.com/f10/lsjz?"
        f"fundCode={code}&pageIndex=1&pageSize={days}"
        f"&startDate={start_date}&endDate={end_date}"
    )
    text = _get(url)
    if not text:
        print(f"获取基金 {code} 历史净值失败")
        return

    data = json.loads(text)
    nav_list = data.get("Data", {}).get("LSJZList", [])
    if not nav_list:
        print(f"基金 {code} 未获取到历史净值数据")
        return

    print(f"\n基金 {code} 近 {days} 天历史净值:")
    print(f"{'日期':<12} {'单位净值':>10} {'累计净值':>10} {'日涨幅':>8}")
    print("-" * 45)
    for item in nav_list:
        date = item.get("FSRQ", "")
        dwjz = item.get("DWJZ", "")
        ljjz = item.get("LJJZ", "")
        jzzzl = item.get("JZZZL", "0")
        if jzzzl is None:
            jzzzl = "0"
        pct = float(jzzzl)
        sign = "+" if pct >= 0 else ""
        print(f"{date:<12} {dwjz:>10} {ljjz:>10} {sign}{pct:>6.2f}%")

    # 统计
    if nav_list:
        latest_nav = float(nav_list[0].get("DWJZ", 0))
        oldest_nav = float(nav_list[-1].get("DWJZ", 0))
        period_return = (latest_nav - oldest_nav) / oldest_nav * 100 if oldest_nav else 0
        print(f"\n区间统计: 最新 {latest_nav} → 起始 {oldest_nav}, 区间收益 {period_return:+.2f}%")


def cmd_rank(args):
    """获取基金排行"""
    # 类型映射
    type_map = {"gp": "gp", "hh": "hh", "zj": "zj", "zs": "zs", "qdii": "qdii", "all": "all"}
    ft = type_map.get(args.type, "all")

    # 排序字段映射
    period_map = {"1n": "1nzf", "3n": "3nzf", "6n": "6nzf", "1n": "1nzf", "2n": "2nzf", "3y": "3nzf", "jn": "jnzf"}
    sc = period_map.get(args.period, "1nzf")

    # 日期范围
    now = datetime.now()
    ed = now.strftime("%Y-%m-%d")
    if args.period == "1n":
        sd = (now - timedelta(days=365)).strftime("%Y-%m-%d")
    elif args.period == "3n":
        sd = (now - timedelta(days=365 * 3)).strftime("%Y-%m-%d")
    elif args.period == "6n":
        sd = (now - timedelta(days=180)).strftime("%Y-%m-%d")
    elif args.period == "2n":
        sd = (now - timedelta(days=365 * 2)).strftime("%Y-%m-%d")
    elif args.period == "jn":
        sd = now.replace(month=1, day=1).strftime("%Y-%m-%d")
    else:
        sd = (now - timedelta(days=365)).strftime("%Y-%m-%d")

    top = args.top
    url = (
        f"http://fund.eastmoney.com/data/rankhandler.aspx?"
        f"op=ph&dt=kf&ft={ft}&rs=&gs=0&sc={sc}&st=desc"
        f"&sd={sd}&ed={ed}&qdii=&tabSubtype=,,,,,"
        f"&pi=1&pn={top}&dx=1"
    )
    text = _get(url)
    if not text:
        print("获取基金排行失败")
        return

    # 解析 JS 变量格式
    m = re.search(r'datas:\[(.*?)\]', text, re.DOTALL)
    if not m:
        print("基金排行数据解析失败")
        return

    items_str = m.group(1)
    items = re.findall(r'"([^"]+)"', items_str)

    type_name = {"gp": "股票型", "hh": "混合型", "zj": "债券型", "zs": "指数型", "qdii": "QDII", "all": "全部"}.get(ft, ft)
    period_name = {"1n": "近1年", "3n": "近3年", "6n": "近6月", "2n": "近2年", "jn": "今年来"}.get(args.period, args.period)

    # 排序字段 → 数据字段索引映射
    # 原始数据字段: [6]=日涨幅 [7]=近1周 [8]=近1月 [9]=近3月 [10]=近6月 [11]=近1年 [12]=近2年 [13]=近3年 [14]=成立以来
    field_index_map = {"jnzf": 14, "1nzf": 11, "6nzf": 10, "3nzf": 13, "2nzf": 12, "1yzf": 7, "1yzf": 8}
    field_idx = field_index_map.get(sc, 10)

    otc_only = getattr(args, "otc_only", False)
    suffix = "（仅场外·支付宝可买）" if otc_only else ""
    print(f"\n基金排行 ({type_name} · {period_name}) Top {top}{suffix}:")
    print(f"{'排名':>4} {'代码':<8} {'名称':<24} {'场所':<5} {'最新净值':>8} {'日期':<12} {'区间涨幅':>10}")
    print("-" * 82)

    rank_no = 0
    for item in items:
        fields = item.split(",")
        if len(fields) < 5:
            continue
        code = fields[0]
        name = fields[1]
        venue = fund_venue(code, name)
        # --otc-only：过滤掉场内 ETF（用户走支付宝买不了）
        if otc_only and venue == "场内":
            continue
        rank_no += 1
        date = fields[3] if len(fields) > 3 else ""
        nav = fields[4] if len(fields) > 4 else ""
        zzf = fields[field_idx] if len(fields) > field_idx and fields[field_idx] else "--"
        print(f"{rank_no:>4} {code:<8} {name:<24} {venue:<5} {nav:>8} {date:<12} {zzf:>9}%")


def _pct(x, signed=False):
    """把小数转百分比字符串，None → —"""
    if x is None:
        return "—"
    return f"{x*100:+.1f}%" if signed else f"{x*100:.1f}%"


def cmd_tech(args):
    """技术/波动面分析（报告层，只读，不改引擎决策）。

    汇总近期/历史 波动率·回撤·趋势·突破·RSI·动量，供每次分析时加权推理。
    用法：tech <基金代码>  或  tech --account 主线（分析全持仓）。
    """
    import signals as sig

    targets = []
    code = getattr(args, "code", None)
    if code:
        targets.append((code, code))
    elif getattr(args, "account", None) and DB_AVAILABLE:
        db = Database()
        acc = db.get_account(name=args.account)
        if not acc:
            print(f"[ERROR] 账户不存在: {args.account}")
            db.close()
            return
        for p in db.get_positions(acc["id"]):
            targets.append((p["code"], p["name"]))
        db.close()
    if not targets:
        print("用法: python3 fetch_fund.py tech <基金代码>  或  tech --account 主线")
        return

    print("\n技术/波动面分析（报告层 · 仅供参考与加权推理，不驱动引擎买卖）")
    print("=" * 74)
    for code, name in targets:
        series = fetch_nav_series(code, days=90)
        navs = [v for _, v in series]
        venue = fund_venue(code, name)
        if len(navs) < 10:
            print(f"\n■ {name} ({code}) [{venue}]：净值数据不足（{len(navs)} 根），跳过")
            continue
        p = sig.tech_panel(navs)
        rsi = p["rsi_14"]
        rsi_tag = "超卖" if rsi is not None and rsi < 30 else ("超买" if rsi is not None and rsi > 70 else "中性")
        ma20, ma60 = p["ma20_slope"] or 0, p["ma60_slope"] or 0
        trend20 = "上行" if ma20 > 0 else ("下行" if ma20 < 0 else "走平")
        trend60 = "上行" if ma60 > 0 else ("下行" if ma60 < 0 else "走平")
        vol = p["vol_60d"]
        vol_tag = "高" if vol is not None and vol > 0.35 else ("低" if vol is not None and vol < 0.18 else "中")
        macd_tag = "多头" if (p["macd_hist"] or 0) > 0 else "空头"
        bo = "是" if p["breakout_20d"] else "否"
        rsi_str = f"{rsi:.0f}（{rsi_tag}）" if rsi is not None else "—"

        print(f"\n■ {name} ({code}) [{venue}]")
        print(f"  动量   近1月 {_pct(p['ret_1m'], True)} | 近3月 {_pct(p['ret_3m'], True)}")
        print(f"  波动   60日年化波动率 {_pct(vol)}（{vol_tag}）| 近60日最大回撤 {_pct(p['max_drawdown_60d'], True)}")
        print(f"  趋势   MA20 {trend20} | MA60 {trend60} | 突破20日新高 {bo}")
        print(f"  强弱   RSI14 {rsi_str} | MACD {macd_tag}")
    print("=" * 74)


def cmd_index_kline(args):
    """获取指数历史K线"""
    secid = args.secid
    days = args.days
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")

    url = (
        f"https://push2his.eastmoney.com/api/qt/stock/kline/get?"
        f"secid={secid}&fields1=f1,f2,f3"
        f"&fields2=f51,f52,f53,f54,f55,f56"
        f"&klt=101&fqt=0&beg={start_date}&end={end_date}"
    )
    text = _get(url)
    if not text:
        print(f"获取指数 {secid} K线失败")
        return

    data = json.loads(text)
    klines = data.get("data", {}).get("klines", [])
    name = data.get("data", {}).get("name", secid)
    if not klines:
        print(f"指数 {secid} 未获取到K线数据")
        return

    print(f"\n{name} 近 {days} 天日K线:")
    print(f"{'日期':<12} {'开盘':>10} {'收盘':>10} {'最高':>10} {'最低':>10}")
    print("-" * 55)
    for line in klines:
        parts = line.split(",")
        if len(parts) >= 5:
            date, open_p, close_p, high, low = parts[0], parts[1], parts[2], parts[3], parts[4]
            print(f"{date:<12} {open_p:>10} {close_p:>10} {high:>10} {low:>10}")

    # 区间涨跌
    if klines:
        first_close = float(klines[0].split(",")[2])
        last_close = float(klines[-1].split(",")[2])
        change_pct = (last_close - first_close) / first_close * 100
        print(f"\n区间涨跌: {first_close:.2f} → {last_close:.2f} ({change_pct:+.2f}%)")


def cmd_portfolio_check(args):
    """批量检查持仓基金实时估值，计算盈亏"""
    account_name = getattr(args, 'account', None)

    # 优先从数据库读取
    if DB_AVAILABLE and account_name:
        db = Database()
        account = db.get_account(name=account_name)
        if not account:
            print(f"[ERROR] 账户不存在: {account_name}")
            return
        portfolio = db.get_positions(account["id"])
        db.close()
    elif PORTFOLIO_FILE.exists():
        # 回退到 JSON 文件
        with open(PORTFOLIO_FILE, "r") as f:
            portfolio = json.load(f)
    else:
        print("持仓文件不存在，请先添加持仓")
        return

    if not portfolio:
        print("当前无持仓，请先添加持仓记录")
        return

    account_label = account_name or "本地"
    print(f"\n持仓基金实时诊断 ({datetime.now().strftime('%Y-%m-%d %H:%M')}) - 账户: {account_label}")
    print("=" * 80)
    print(f"{'基金名称':<20} {'估值涨幅':>8} {'估算净值':>8} {'成本净值':>8} {'持有份额':>10} {'估算盈亏':>12} {'累计收益':>10} {'持有':>7}")
    print("-" * 88)

    total_estimated_value = 0
    total_cost = 0
    total_today_pnl = 0

    for holding in portfolio:
        code = holding.get("code", "")
        name = holding.get("name", code)
        shares = holding.get("shares", 0)
        cost_nav = holding.get("cost_nav", 0)
        held = _held_days(holding.get("buy_date"))
        held_str = f"{held}天" if held is not None else "--"

        # 获取实时估值
        url = f"http://fundgz.1234567.com.cn/js/{code}.js"
        text = _get(url)
        data = _parse_jsonp(text) if text else None

        if data and data.get("gsz"):
            est_nav = float(data["gsz"])
            est_pct = float(data.get("gszzl", 0))
            prev_nav = float(data.get("dwjz", est_nav))

            # 估算市值和盈亏
            est_value = est_nav * shares
            today_pnl = (est_nav - prev_nav) * shares
            total_pnl = (est_nav - cost_nav) * shares
            total_pnl_pct = (est_nav - cost_nav) / cost_nav * 100 if cost_nav else 0

            total_estimated_value += est_value
            total_cost += cost_nav * shares
            total_today_pnl += today_pnl

            sign = "+" if est_pct >= 0 else ""
            pnl_sign = "+" if total_pnl >= 0 else ""
            print(
                f"{name:<20} {sign}{est_pct:>6.2f}% {est_nav:>8.4f} {cost_nav:>8.4f} "
                f"{shares:>10.2f} {pnl_sign}{today_pnl:>10.2f} {pnl_sign}{total_pnl_pct:>8.2f}% {held_str:>7}"
            )
        else:
            print(f"{name:<20} {'--':>8} {'--':>8} {cost_nav:>8.4f} {shares:>10.2f} {'--':>12} {'--':>10} {held_str:>7}")

    print("=" * 80)
    if total_cost > 0:
        total_return = (total_estimated_value - total_cost) / total_cost * 100
        print(f"合计估算市值: {total_estimated_value:,.2f} 元")
        print(f"合计成本: {total_cost:,.2f} 元")
        print(f"今日估算盈亏: {total_today_pnl:+,.2f} 元")
        print(f"累计收益率: {total_return:+.2f}%")
        print(f"累计收益: {total_estimated_value - total_cost:+,.2f} 元")


def cmd_portfolio_show(args):
    """显示当前持仓"""
    account_name = getattr(args, 'account', None)

    # 优先从数据库读取
    if DB_AVAILABLE and account_name:
        db = Database()
        account = db.get_account(name=account_name)
        if not account:
            print(f"[ERROR] 账户不存在: {account_name}")
            return
        portfolio = db.get_positions(account["id"])
        db.close()
    elif PORTFOLIO_FILE.exists():
        # 回退到 JSON 文件
        with open(PORTFOLIO_FILE, "r") as f:
            portfolio = json.load(f)
    else:
        print("持仓文件不存在")
        return

    if not portfolio:
        print("当前无持仓")
        return

    account_label = account_name or "本地"
    print(f"\n当前持仓 ({len(portfolio)} 只基金) - 账户: {account_label}:")
    print("-" * 82)
    print(f"{'基金代码':<8} {'基金名称':<20} {'持有份额':>10} {'成本净值':>8} {'买入日期':<12} {'持有天数':>8} {'备注':<10}")
    print("-" * 82)
    for h in portfolio:
        held = _held_days(h.get('buy_date'))
        held_str = f"{held}天" if held is not None else "--"
        print(
            f"{h.get('code', ''):<8} {h.get('name', ''):<20} "
            f"{h.get('shares', 0):>10.2f} {h.get('cost_nav', 0):>8.4f} "
            f"{h.get('buy_date', ''):<12} {held_str:>8} {h.get('note', ''):<10}"
        )


def _load_portfolio(account_name):
    """读持仓（优先 DB，回退 JSON）。返回 list 或 None。"""
    if DB_AVAILABLE and account_name:
        db = Database()
        account = db.get_account(name=account_name)
        if not account:
            db.close()
            return None
        portfolio = db.get_positions(account["id"])
        db.close()
        return portfolio
    if PORTFOLIO_FILE.exists():
        with open(PORTFOLIO_FILE, "r") as f:
            return json.load(f)
    return []


def cmd_returns(args):
    """单只基金收益变化 + 组合总收益变化（净值序列派生，含迷你走势）。"""
    account_name = getattr(args, "account", None)
    days = getattr(args, "days", 30)
    only_code = getattr(args, "code", None)

    portfolio = _load_portfolio(account_name)
    if portfolio is None:
        print(f"[ERROR] 账户不存在: {account_name}")
        return
    if not portfolio:
        print("当前无持仓")
        return
    if only_code:
        portfolio = [h for h in portfolio if h.get("code") == only_code]
        if not portfolio:
            print(f"持仓中未找到 {only_code}")
            return

    account_label = account_name or "本地"
    print(f"\n收益变化趋势（最近 {days} 天）- 账户: {account_label}")
    print("=" * 92)
    print(f"{'基金名称':<20} {'现值净值':>9} {'累计收益':>9} {f'较{days}天前':>10} {'持有':>6}  走势")
    print("-" * 92)

    holdings_for_total = []
    for h in portfolio:
        code = h.get("code", "")
        name = h.get("name", code)
        shares = h.get("shares", 0)
        cost_nav = h.get("cost_nav", 0)
        series = fetch_nav_series(code, days=days)

        # 今日实时估值作为序列最新点
        est = _parse_jsonp(_get(f"http://fundgz.1234567.com.cn/js/{code}.js"))
        cur_nav = float(est["gsz"]) if est and est.get("gsz") else (
            series[-1][1] if series else cost_nav)
        held = _held_days(h.get("buy_date"))
        held_str = f"{held}天" if held is not None else "--"

        if not series or not cost_nav:
            print(f"{name:<20} {cur_nav:>9.4f} {'--':>9} {'--':>10} {held_str:>6}  （无净值序列）")
            holdings_for_total.append((shares, cost_nav, series))
            continue

        traj = [(d, (nav - cost_nav) / cost_nav) for d, nav in series]
        traj.append(("now", (cur_nav - cost_nav) / cost_nav))
        cur_ret = traj[-1][1]
        delta = cur_ret - traj[0][1]
        spark = _sparkline([r for _, r in traj])
        print(f"{name:<20} {cur_nav:>9.4f} {cur_ret*100:>+8.2f}% "
              f"{delta*100:>+9.2f}pt {held_str:>6}  {spark}")
        holdings_for_total.append((shares, cost_nav, series))

    # ===== 组合总收益变化 =====
    print("=" * 92)
    total_series = _align_total_return_series(holdings_for_total)
    # 当前总收益（用实时估值）
    cur_val = cur_cost = 0.0
    for h in portfolio:
        code = h.get("code", "")
        shares = h.get("shares", 0)
        cost_nav = h.get("cost_nav", 0)
        est = _parse_jsonp(_get(f"http://fundgz.1234567.com.cn/js/{code}.js"))
        nav = float(est["gsz"]) if est and est.get("gsz") else cost_nav
        cur_val += shares * nav
        cur_cost += shares * cost_nav
    cur_total_ret = (cur_val - cur_cost) / cur_cost if cur_cost else 0
    print(f"组合总收益: {cur_total_ret*100:+.2f}%  （现值 ¥{cur_val:,.0f} / 成本 ¥{cur_cost:,.0f}）")
    if total_series and len(total_series) >= 2:
        first = total_series[0][1]
        spark = _sparkline([r for _, r in total_series])
        print(f"总收益变化: 较{days}天前 {(cur_total_ret-first)*100:+.2f}pt "
              f"（{total_series[0][0]} {first*100:+.1f}% → 今日 {cur_total_ret*100:+.1f}%）")
        print(f"  走势: {spark}")
    print()


def cmd_orders_show(args):
    """显示交易订单"""
    account_name = getattr(args, 'account', None)

    # 优先从数据库读取
    if DB_AVAILABLE and account_name:
        db = Database()
        account = db.get_account(name=account_name)
        if not account:
            print(f"[ERROR] 账户不存在: {account_name}")
            return
        trades = db.get_trades(account["id"], limit=getattr(args, 'limit', 50))
        db.close()
        orders = [{
            "date": t["date"],
            "action": t["action"],
            "code": t["code"],
            "name": t["name"],
            "amount": t["amount"],
            "nav": t["nav"],
            "shares": t["shares"],
            "note": t.get("reason") or t.get("rule_name") or ""
        } for t in trades]
    elif ORDERS_FILE.exists():
        # 回退到 JSON 文件
        with open(ORDERS_FILE, "r") as f:
            orders = json.load(f)
    else:
        print("订单文件不存在")
        return

    if not orders:
        print("暂无交易记录")
        return

    account_label = account_name or "本地"
    print(f"\n交易记录 ({len(orders)} 条) - 账户: {account_label}:")
    print("-" * 80)
    print(f"{'日期':<12} {'操作':<6} {'基金代码':<8} {'基金名称':<20} {'金额':>10} {'净值':>8} {'备注':<10}")
    print("-" * 80)
    for o in orders:
        print(
            f"{o.get('date', ''):<12} {o.get('action', ''):<6} "
            f"{o.get('code', ''):<8} {o.get('name', ''):<20} "
            f"{o.get('amount', 0):>10.2f} {o.get('nav', 0):>8.4f} "
            f"{o.get('note', ''):<10}"
        )


def cmd_market_summary(args):
    """市场全景：指数 + 板块 + 资金流向"""
    print("\n" + "=" * 70)
    print(f" 市场全景 ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    print("=" * 70)

    # 1. 指数
    secids = ",".join(INDICES.values())
    url = (
        f"https://push2.eastmoney.com/api/qt/ulist.np/get?"
        f"fltt=2&invt=2&ut=fa5fd1943c7b386f172d6893dbfba10b"
        f"&fields=f2,f3,f4,f6,f12,f14,f104,f105"
        f"&secids={secids}"
    )
    text = _get(url)
    if text:
        data = json.loads(text)
        items = data.get("data", {}).get("diff", [])
        print("\n📊 大盘指数:")
        for item in items:
            name = item.get("f14", "")
            price = item.get("f2", 0)
            pct = item.get("f3", 0)
            sign = "+" if pct >= 0 else ""
            up_count = item.get("f104", 0)
            down_count = item.get("f105", 0)
            print(f"  {name}: {price:.2f} ({sign}{pct:.2f}%)  涨:{up_count} 跌:{down_count}")

    # 2. 板块涨跌 Top 10
    url2 = (
        "https://push2.eastmoney.com/api/qt/clist/get?"
        "pn=1&pz=100&po=1&np=1&fltt=2&invt=2"
        "&fid=f3&fs=m:90+t:2"
        "&fields=f2,f3,f4,f12,f14"
    )
    text2 = _get(url2)
    if text2:
        data2 = json.loads(text2)
        items2 = data2.get("data", {}).get("diff", [])
        print("\n🔥 涨幅前10行业:")
        for i, item in enumerate(items2[:10], 1):
            name = item.get("f14", "")
            pct = item.get("f3", 0) if item.get("f3") is not None else 0
            print(f"  {i:>2}. {name}: {pct:+.2f}%")

    # 3. 跌幅前10
    if text2:
        losers = sorted(items2, key=lambda x: x.get("f3", 0) if x.get("f3") is not None else 0)[:10]
        print("\n💧 跌幅前10行业:")
        for i, item in enumerate(losers, 1):
            name = item.get("f14", "")
            pct = item.get("f3", 0) if item.get("f3") is not None else 0
            print(f"  {i:>2}. {name}: {pct:+.2f}%")


SECTOR_KEYWORDS = {
    "科技": ["半导体", "芯片", "AI", "人工智能", "信息科技", "数字经济",
              "电子", "传媒", "科技"],
    "消费": ["白酒", "食品", "医药", "消费"],
    "新能源": ["光伏", "锂电", "新能源"],
    "金融": ["银行", "券商", "保险"],
    "资源": ["黄金", "有色", "煤炭", "石油"],
    "宽基": ["沪深300", "中证500", "创业板", "上证50"],
    "海外": ["纳斯达克", "标普", "QDII", "港股", "纳指"],
}


def _infer_sector(name):
    if not name:
        return "其他"
    for sector, kws in SECTOR_KEYWORDS.items():
        for kw in kws:
            if kw in name:
                return sector
    return "其他"


def fetch_index_daily_fallback(secid, start_date, end_date):
    """东财 push2his 挂掉时的指数日K备源：A股指数走新浪，纳指走腾讯。

    start/end: YYYY-MM-DD。返回 {date: close}（仅区间内）。任何失败返回 {}。
    新浪单次最多 1023 根日K（约可回看 4 年）；腾讯按区间+条数取。
    """
    out = {}
    try:
        if secid.startswith(("0.", "1.")):
            sym = ("sh" if secid.startswith("1.") else "sz") + secid.split(".")[1]
            url = (
                "https://quotes.sina.cn/cn/api/json_v2.php/"
                "CN_MarketDataService.getKLineData?"
                f"symbol={sym}&scale=240&ma=no&datalen=1023"
            )
            text = _get(url)
            if text:
                for r in json.loads(text):
                    d, c = r.get("day"), r.get("close")
                    if d and c and start_date <= d <= end_date:
                        out[d] = float(c)
        elif secid == "100.NDX":
            url = (
                "https://web.ifzq.gtimg.cn/appstock/app/usfqkline/get?"
                f"param=us.NDX,day,{start_date},{end_date},800,qfq"
            )
            text = _get(url)
            if text:
                data = json.loads(text).get("data", {}).get("us.NDX", {})
                rows = data.get("qfqday") or data.get("day") or []
                for parts in rows:
                    if len(parts) >= 3 and start_date <= parts[0] <= end_date:
                        out[parts[0]] = float(parts[2])
    except Exception:
        return {}
    return out


def _index_closes(secid, days=320):
    """指数日K收盘序列（升序），失败返回 []。供 200 日线状态计算。"""
    try:
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=int(days * 1.6))).strftime("%Y%m%d")
        url = (
            "https://push2his.eastmoney.com/api/qt/stock/kline/get?"
            f"secid={secid}&fields1=f1,f2,f3"
            "&fields2=f51,f53"
            f"&klt=101&fqt=0&beg={start_date}&end={end_date}"
        )
        text = _get(url)
        klines = (json.loads(text).get("data", {}) or {}).get("klines", []) if text else []
        if klines:
            return [float(k.split(",")[1]) for k in klines]
        # 东财失败 → 备源（新浪/腾讯）
        iso = lambda s: f"{s[:4]}-{s[4:6]}-{s[6:]}"
        data = fetch_index_daily_fallback(secid, iso(start_date), iso(end_date))
        return [data[d] for d in sorted(data)]
    except Exception:
        return []


def gather_index_trend():
    """实时 200日线趋势状态 {HS300, NDX}（P5 趋势规则数据源）。失败项缺省。"""
    import signals as _signals
    out = {}
    for secid, key in (("1.000300", "HS300"), ("100.NDX", "NDX")):
        closes = _index_closes(secid)
        if len(closes) >= 200:
            st = _signals.compute_ma_state(closes, window=200)
            if st:
                out[key] = st
    return out


def _hs300_returns():
    """Return (5d_return, 20d_return) for HS300. (None, None) on fetch failure."""
    try:
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=40)).strftime("%Y%m%d")
        url = (
            "https://push2his.eastmoney.com/api/qt/stock/kline/get?"
            "secid=1.000300&fields1=f1,f2,f3"
            "&fields2=f51,f52,f53,f54,f55,f56"
            f"&klt=101&fqt=0&beg={start_date}&end={end_date}"
        )
        text = _get(url)
        klines = (json.loads(text).get("data", {}) or {}).get("klines", []) if text else []
        closes = [float(k.split(",")[2]) for k in klines]
        if len(closes) < 21:
            # 东财失败 → 备源（新浪）
            iso = lambda s: f"{s[:4]}-{s[4:6]}-{s[6:]}"
            data = fetch_index_daily_fallback(
                "1.000300", iso(start_date), iso(end_date))
            closes = [data[d] for d in sorted(data)]
        if len(closes) < 21:
            return None, None
        latest = closes[-1]
        five_ago = closes[-6]
        twenty_ago = closes[-21]
        return (
            (latest - five_ago) / five_ago,
            (latest - twenty_ago) / twenty_ago,
        )
    except Exception:
        return None, None


def _fund_snapshot(code, name, sector):
    """Fetch latest snapshot for one fund. Returns dict or None if data missing.

    Includes Phase 3 technical signals (RSI / MACD / MA20 slope / breakout)
    computed from the same NAV history we already fetched — no extra network.
    """
    try:
        gz_url = f"http://fundgz.1234567.com.cn/js/{code}.js"
        gz_text = _get(gz_url)
        gz = _parse_jsonp(gz_text) if gz_text else None
        if gz:
            current_nav = float(gz.get("gsz") or gz.get("dwjz") or 0.0)
            day_return = float(gz.get("gszzl") or 0.0) / 100.0
            display_name = name or gz.get("name", "")
        else:
            current_nav = 0.0
            day_return = 0.0
            display_name = name or ""

        nav_url = (
            f"https://api.fund.eastmoney.com/f10/lsjz?"
            f"fundCode={code}&pageIndex=1&pageSize=60"   # widened for MACD (needs 35+)
        )
        nav_text = _get(
            nav_url,
            headers={"Referer": "https://fundf10.eastmoney.com/"},
        )
        navs = []
        if nav_text:
            try:
                nav_data = json.loads(nav_text)
                items = nav_data.get("Data", {}).get("LSJZList", [])
                # Items come newest-first; we need oldest→newest for signal funcs
                navs_newest_first = [float(i["DWJZ"]) for i in items if i.get("DWJZ")]
                navs = list(reversed(navs_newest_first))
            except Exception:
                navs = []

        if not current_nav and navs:
            current_nav = navs[-1]

        def _ret(n):
            # navs is oldest-first; latest is navs[-1]; n days ago is navs[-(n+1)]
            if len(navs) > n and navs[-(n + 1)]:
                return (current_nav - navs[-(n + 1)]) / navs[-(n + 1)]
            return 0.0

        if not current_nav:
            return None  # genuinely missing — engine will emit data_missing alert

        # Phase 3 signals
        try:
            from signals import attach_signals
            sig = attach_signals(navs)
        except Exception:
            sig = {"rsi_14": None, "macd_hist": None,
                   "ma20_slope": None, "breakout_20d": None}

        return {
            "name": display_name,
            "current_nav": current_nav,
            "day_return": day_return,
            "fund_3d_return": _ret(3),
            "fund_5d_return": _ret(5),
            "fund_20d_return": _ret(20),
            "high_20d": max(navs[-20:]) if len(navs) >= 20 else current_nav,
            "sector": sector or _infer_sector(display_name),
            "signals": sig,
        }
    except Exception:
        return None


def gather_market_snapshot(account_name="主线", date=None):
    """Aggregate inputs DecisionEngine.decide() needs.

    Returns dict with hs300 returns + per-fund snapshots for portfolio + watchlist.
    Each missing fund becomes None — engine emits data_missing alert for those.
    """
    import sys as _sys
    _sys.path.insert(0, str(SCRIPT_DIR := Path(__file__).resolve().parent))
    from db import Database

    db = Database()
    try:
        hs300_5d, hs300_20d = _hs300_returns()

        cur = db.conn.cursor()
        row = cur.execute(
            "SELECT id FROM accounts WHERE name = ?", (account_name,)
        ).fetchone()
        if not row:
            return {"error": f"account '{account_name}' not found"}
        account_id = row["id"]

        positions = cur.execute(
            "SELECT code, name, sector FROM positions WHERE account_id = ?",
            (account_id,),
        ).fetchall()

        funds = {}
        for p in positions:
            snap = _fund_snapshot(p["code"], p["name"], p["sector"])
            if snap:
                funds[p["code"]] = snap

        # P5 趋势状态：QDII 基金标记参考指数（trend_exit 用 NDX 而非 A 股）
        index_trend = gather_index_trend()
        for code in funds:
            if code in QDII_INDEX_MAP:
                funds[code]["ref_index"] = "NDX"

        # Watchlist: simulate.py's DEFAULT_FUNDS, if importable
        try:
            from simulate import DEFAULT_FUNDS  # type: ignore
            for code, name in DEFAULT_FUNDS.items():
                if code not in funds:
                    snap = _fund_snapshot(code, name, None)
                    if snap:
                        funds[code] = snap
        except Exception:
            pass

        # Peak value: use max(total_value) from daily_snapshots if available
        peak = None
        try:
            row = cur.execute(
                "SELECT MAX(total_value) AS peak FROM daily_snapshots "
                "WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            peak = row["peak"] if row and row["peak"] else None
        except Exception:
            peak = None

        # 报告层附加：财经快讯 + 操作复盘记忆（不驱动引擎，仅供叙事/宏观判断）
        try:
            news = gather_market_news(limit=8)
        except Exception:
            news = []
        try:
            review_summary = db.get_review_summary(account_id, lookback_days=60)
        except Exception:
            review_summary = {"count": 0}

        return {
            "hs300_5d_return": hs300_5d,
            "hs300_20d_return": hs300_20d,
            "regime_hint": None,
            "funds": funds,
            "portfolio_peak_value": peak,
            "index_trend": index_trend,
            "news": news,
            "recent_review_summary": review_summary,
        }
    finally:
        db.close()


def cmd_market_snapshot(args):
    snap = gather_market_snapshot(account_name=args.account, date=args.date)
    print(json.dumps(snap, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="基金数据抓取工具 — Smart Invest Skill")
    sub = parser.add_subparsers(dest="command", help="子命令")

    # indices
    sub.add_parser("indices", help="获取大盘指数实时行情")

    # sectors
    sub.add_parser("sectors", help="获取行业板块涨跌排行")

    # us-index — 美股指数隔夜行情（QDII 方向判断）
    p_us = sub.add_parser("us-index", help="美股指数隔夜行情（QDII 方向，如纳斯达克100）")
    p_us.add_argument("name", nargs="?", default="纳斯达克100",
                      help="指数名：纳斯达克100/标普500/道琼斯（默认纳斯达克100）")

    # chart — 终端走势图
    p_chart = sub.add_parser("chart", help="终端走势图（指数分时/基金净值曲线）")
    p_chart.add_argument("target", help="指数名(纳斯达克100/沪深300…)、别名(NDX/SPX/DJIA) 或 6位基金代码")
    p_chart.add_argument("--days", type=int, default=60, help="基金净值回看天数（默认60）")
    p_chart.add_argument("--ndays", type=int, default=1, help="指数分时天数（默认1）")
    p_chart.add_argument("--height", type=int, default=14, help="图高（默认14行）")
    p_chart.add_argument("--width", type=int, default=90, help="图宽（默认90列）")

    # estimate
    p_est = sub.add_parser("estimate", help="获取单只基金实时估值")
    p_est.add_argument("code", help="基金代码，如 110011")

    # nav
    p_nav = sub.add_parser("nav", help="获取基金历史净值")
    p_nav.add_argument("code", help="基金代码")
    p_nav.add_argument("--days", type=int, default=30, help="查询天数（默认30）")

    # tech — 技术/波动面分析（报告层）
    p_tech = sub.add_parser("tech", help="技术/波动面分析：波动率/回撤/趋势/突破/RSI/动量（报告层只读）")
    p_tech.add_argument("code", nargs="?", help="基金代码（省略则配合 --account 分析全持仓）")
    p_tech.add_argument("--account", "-a", help="账户名（分析该账户全部持仓）")

    # rank
    p_rank = sub.add_parser("rank", help="基金排行")
    p_rank.add_argument("--type", default="all", choices=["gp", "hh", "zj", "zs", "qdii", "all"], help="基金类型")
    p_rank.add_argument("--period", default="1n", choices=["jn", "1n", "6n", "2n", "3n"], help="统计区间")
    p_rank.add_argument("--top", type=int, default=20, help="排名数量（默认20）")
    p_rank.add_argument("--otc-only", action="store_true", help="只显示场外(支付宝可买)基金，过滤场内ETF")

    # index-kline
    p_kline = sub.add_parser("index-kline", help="获取指数历史K线")
    p_kline.add_argument("secid", help="指数secid，如 1.000001（上证）")
    p_kline.add_argument("--days", type=int, default=30, help="查询天数（默认30）")

    # portfolio-check
    p_pcheck = sub.add_parser("portfolio-check", help="批量检查持仓基金估值")
    p_pcheck.add_argument("--account", "-a", help="账户名称（可选，默认读取本地持仓）")

    # portfolio-show
    p_pshow = sub.add_parser("portfolio-show", help="显示当前持仓")
    p_pshow.add_argument("--account", "-a", help="账户名称（可选，默认读取本地持仓）")

    # orders-show
    p_oshow = sub.add_parser("orders-show", help="显示交易订单")
    p_oshow.add_argument("--account", "-a", help="账户名称（可选，默认读取本地订单）")
    p_oshow.add_argument("--limit", "-l", type=int, default=50, help="显示条数（默认50）")

    # returns — 单只 + 总收益变化趋势
    p_ret = sub.add_parser("returns", help="单只基金 + 组合总收益变化（含迷你走势）")
    p_ret.add_argument("--account", "-a", help="账户名称（可选，默认读取本地持仓）")
    p_ret.add_argument("--code", "-c", help="只看单只基金代码")
    p_ret.add_argument("--days", "-d", type=int, default=30, help="回看天数（默认30）")

    # news — 免费财经快讯
    p_news = sub.add_parser("news", help="免费财经快讯（东方财富7x24，可--keyword过滤）")
    p_news.add_argument("--keyword", "-k", help="按关键词过滤（如 半导体/纳指/降准）")
    p_news.add_argument("--limit", "-l", type=int, default=10, help="条数（默认10）")

    # market-summary
    sub.add_parser("market-summary", help="市场全景（指数+板块）")

    # market-snapshot — Phase 1: 喂给 decision_engine 的聚合数据
    p_snap = sub.add_parser(
        "market-snapshot",
        help="聚合大盘+持仓+候选池数据，喂给决策引擎",
    )
    p_snap.add_argument("--account", "-a", default="主线", help="账户名称")
    p_snap.add_argument("--date", default=None, help="日期（YYYY-MM-DD）")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    cmd_map = {
        "indices": cmd_indices,
        "sectors": cmd_sectors,
        "us-index": cmd_us_index,
        "chart": cmd_chart,
        "estimate": cmd_estimate,
        "nav": cmd_nav,
        "tech": cmd_tech,
        "rank": cmd_rank,
        "index-kline": cmd_index_kline,
        "portfolio-check": cmd_portfolio_check,
        "portfolio-show": cmd_portfolio_show,
        "orders-show": cmd_orders_show,
        "returns": cmd_returns,
        "news": cmd_news,
        "market-summary": cmd_market_summary,
        "market-snapshot": cmd_market_snapshot,
    }

    handler = cmd_map.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
