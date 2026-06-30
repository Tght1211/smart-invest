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


# ──────────────────────────────────────────────────────────────────────
# 会话对表（R1）：本机时间 / 时区 / 星期 / A股交易时段判定（纯函数，可注入 now）
# ──────────────────────────────────────────────────────────────────────
_WEEKDAY_ZH = ["一", "二", "三", "四", "五", "六", "日"]


def market_clock(now=None):
    """返回本机当前时间、时区与 A 股交易时段判定。

    纯函数：传入 `now`（带时区的 datetime）即可离线测试。
    时段键 session_key ∈ {pre, open, lunch, mid, close, after, weekend}，
    其中 open/mid/close 对应三时段日报。
    注意：只按周末判断非交易日，**不含法定节假日**（节假日历需联网，故从略，
    遇节假日请人工核对）。
    """
    if now is None:
        now = datetime.now().astimezone()
    elif now.tzinfo is None:
        now = now.astimezone()

    wd = now.weekday()  # 0=周一
    is_weekend = wd >= 5
    minutes = now.hour * 60 + now.minute

    off = now.utcoffset()
    if off is not None:
        total = int(off.total_seconds() // 60)
        sign = "+" if total >= 0 else "-"
        total = abs(total)
        utc_offset = f"{sign}{total // 60:02d}:{total % 60:02d}"
    else:
        utc_offset = ""

    if is_weekend:
        session, key, market_open = "周末休市", "weekend", False
    elif minutes < 9 * 60:
        session, key, market_open = "盘前", "pre", False
    elif minutes < 9 * 60 + 30:
        session, key, market_open = "集合竞价（盘前）", "pre", False
    elif minutes < 11 * 60 + 30:
        session, key, market_open = "上午盘", "open", True
    elif minutes < 13 * 60:
        session, key, market_open = "午间休市", "lunch", False
    elif minutes < 14 * 60 + 30:
        session, key, market_open = "下午盘", "mid", True
    elif minutes < 15 * 60:
        session, key, market_open = "盘尾下单窗口", "close", True
    else:
        session, key, market_open = "已收盘", "after", False

    advice = {
        "pre": "A股尚未开盘。可先做隔夜复盘、看 QDII 方向（纳指隔夜）和今日计划。",
        "open": "A股上午盘交易中。盘中估值仅供参考，下单决策留到盘尾 14:30。",
        "lunch": "午间休市。可整理上午行情、准备下午策略。",
        "mid": "A股下午盘交易中。临近盘尾，准备最终下单决策。",
        "close": "正处盘尾下单窗口（14:30-15:00），场外基金约 15:00 截单，现在是当日最终买卖决策时刻——留足 30 分钟确认。",
        "after": "A股已收盘。当日场外按收盘净值成交；适合做收盘复盘、记账与晚报。",
        "weekend": "周末休市，无法交易。可做策略复盘、回测与持仓体检。",
    }[key]

    return {
        "now": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "tzname": now.tzname() or "",
        "utc_offset": utc_offset,
        "weekday": wd + 1,
        "weekday_zh": "周" + _WEEKDAY_ZH[wd],
        "is_weekend": is_weekend,
        "is_trading_day": not is_weekend,  # 不含法定节假日
        "session": session,
        "session_key": key,
        "market_open": market_open,
        "advice": advice,
    }


def cmd_now(args):
    """打印本机时间/时区/星期 + A股交易时段（会话开始先对表）。"""
    c = market_clock()
    if getattr(args, "json", False):
        print(json.dumps(c, ensure_ascii=False, indent=2))
        return
    print(f"\n🕐 本机时间：{c['date']} {c['weekday_zh']} {c['time']}"
          f"（{c['tzname']} UTC{c['utc_offset']}）")
    print(f"📅 交易日：{'是' if c['is_trading_day'] else '否（周末）'}"
          f"｜当前时段：{c['session']}"
          f"｜A股{'开市中' if c['market_open'] else '未开市'}")
    print(f"💡 {c['advice']}")
    if c["is_trading_day"]:
        print("（注：仅按周末判断，法定节假日请人工核对）")


# ──────────────────────────────────────────────────────────────────────
# 份额类型识别（R5 短期买C·长期买A）：纯函数，无网络
# ──────────────────────────────────────────────────────────────────────
_CLASS_LETTERS = ("A", "B", "C", "D", "E")
_PAREN_RE = re.compile(r"[（(][^（()）]*[）)]")


def detect_share_class(name):
    """从基金名识别份额类别 → 'A'/'B'/'C'/'D'/'E' 或 None（单一份额/无后缀）。

    纯字符串启发：去掉尾部括注（如「(QDII)」「人民币」修饰保留），
    再看结尾是否为类别字母或「X类」。例：
      「广发纳斯达克100ETF联接人民币(QDII)C」→ C
      「招商中证白酒指数A」→ A
      「汇丰晋信科技先锋股票」→ None（无份额后缀）
    """
    if not name:
        return None
    s = name.strip()
    # 「…A类」「…C类」
    m = re.search(r"([ABCDE])\s*类$", s)
    if m:
        return m.group(1)
    # 去掉尾部成对括注后看结尾字母
    prev = None
    while prev != s:
        prev = s
        s = _PAREN_RE.sub("", s).strip()
    if len(s) >= 2 and s[-1] in _CLASS_LETTERS and not s[-2].isdigit():
        # 排除「ETF」结尾的 F 等：F 不在集合里；A-E 结尾基本就是份额
        return s[-1]
    return None


def base_fund_name(name):
    """去掉尾部份额类别字母，得到兄弟份额共享的基名（纯函数）。

    用于匹配 A/C 兄弟：「…联接人民币(QDII)C」→「…联接人民币(QDII)」。
    """
    if not name:
        return ""
    s = name.strip()
    s = re.sub(r"\s*([ABCDE])\s*类$", "", s)
    if len(s) >= 2 and s[-1] in _CLASS_LETTERS and not s[-2].isdigit():
        s = s[:-1]
    return s.strip()


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


# 基金名/赛道里对新闻匹配无意义的通用词，先剔除
_NEWS_STOPWORDS = ("ETF联接", "ETF", "指数", "(QDII)", "QDII", "联接", "基金",
                   "股票", "混合", "LOF", "A", "C")

# 常见赛道/主题词——基金名常把它嵌在公司前缀后（如「国联安半导体」），
# 中文无 stdlib 分词，靠这张表把主题抠出来与新闻匹配。
_THEME_KEYWORDS = (
    "半导体", "集成电路", "芯片", "白酒", "消费", "医药", "生物", "创新药",
    "新能源", "光伏", "锂电", "电池", "储能", "科技", "券商", "证券", "银行",
    "军工", "国防", "纳斯达克", "纳指", "标普", "美股", "黄金", "有色",
    "中证500", "中证1000", "沪深300", "上证50", "创业板", "科创",
    "人工智能", "算力", "通信", "传媒", "电子", "汽车", "地产", "金融",
)


def _news_keywords(name=None, sector=None):
    """从基金名 + 赛道提炼用于匹配新闻的关键词（主题词 + 粗粒度子串）。"""
    kws = []
    blob = " ".join(str(x) for x in (sector, name) if x)
    for theme in _THEME_KEYWORDS:
        if theme in blob:
            kws.append(theme)
    for raw in (sector, name):
        if not raw:
            continue
        s = str(raw)
        for w in _NEWS_STOPWORDS:
            s = s.replace(w, " ")
        for tok in s.replace("/", " ").split():
            tok = tok.strip()
            if len(tok) >= 2 and not tok.isdigit() and tok not in kws:
                kws.append(tok)
    return kws


def relevant_news(news, name=None, sector=None, limit=3):
    """从 news 列表挑与某基金相关的要闻（按名/赛道关键词子串匹配）。

    无命中则回退 top 市场要闻——保证「每笔操作都有新闻支撑」。返回标题字符串列表。
    交互模式下 Claude 可用 WebSearch 拿更精准的新闻，本函数是定时/无 LLM 路兜底。
    """
    if not news:
        return []
    kws = _news_keywords(name, sector)
    hits = []
    for it in news:
        text = (it.get("title", "") or "") + " " + (it.get("summary", "") or "")
        if any(k in text for k in kws):
            t = it.get("title", "")
            if t and t not in hits:
                hits.append(t)
    if not hits:
        hits = [it.get("title", "") for it in news if it.get("title", "")]
    return hits[:limit]


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


# ──────────────────────────────────────────────────────────────────────
# 板块多窗口扫描（R2）：今日热门/落后板块 + 7日/30日/6月波动 + 趋势分类
# ──────────────────────────────────────────────────────────────────────
def compute_window_returns(closes):
    """收盘序列（oldest→newest）→ 多窗口收益 + 30日波动率（纯函数，百分比）。

    d1≈今日, d5≈近一周, d22≈近一月, d120≈近半年（按交易日近似）。
    """
    def ret(n):
        if len(closes) > n and closes[-1 - n]:
            return round((closes[-1] / closes[-1 - n] - 1) * 100, 2)
        return None

    vol30 = None
    if len(closes) >= 22:
        seg = closes[-22:]
        rets = [(seg[i] / seg[i - 1] - 1) for i in range(1, len(seg)) if seg[i - 1]]
        if rets:
            mean = sum(rets) / len(rets)
            vol30 = round((sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5 * 100, 2)
    return {"d1": ret(1), "d5": ret(5), "d22": ret(22), "d120": ret(120), "vol30": vol30}


def classify_board_trend(w):
    """据多窗口收益分类板块趋势（纯函数）→ (label, note)。

    把「今日的涨」与「7日/30日/6月的真实趋势」区分开——避免追一日脉冲。
    """
    d1 = w.get("d1") or 0.0
    d5 = w.get("d5") or 0.0
    d22 = w.get("d22") or 0.0
    d120 = w.get("d120") or 0.0
    if d5 > 0 and d22 > 0 and d120 > 0:
        return ("强势趋势", "7日/30日/6月全线上行，趋势确立")
    if d1 > 0 and d22 < -1:
        return ("超跌反弹·谨慎", "今日反弹但30日仍下行，可能是下跌中继")
    if d1 < 0 and d5 < 0 and d22 > 1:
        return ("高位退潮", "30日上行但近一周转跌，警惕见顶")
    if d22 < -1 and d120 < -1:
        return ("弱势下行", "30日/6月均下行，趋势偏空")
    if d22 > 0 and d120 > 0:
        return ("温和上行", "中期上行、短期反复")
    return ("震荡", "无明确方向")


def fetch_all_boards():
    """返回全部行业板块 [{code,name,today}]（按今日涨幅降序）。失败返回 []。"""
    url = (
        "https://push2.eastmoney.com/api/qt/clist/get?"
        "pn=1&pz=200&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:90+t:2&fields=f12,f14,f3"
    )
    text = _get(url)
    if not text:
        return []
    try:
        items = json.loads(text).get("data", {}).get("diff") or []
    except Exception:
        return []
    out = [{"code": it.get("f12"), "name": it.get("f14", ""), "today": it.get("f3")}
           for it in items if it.get("f3") is not None]
    out.sort(key=lambda x: x["today"], reverse=True)
    return out


def fetch_board_windows(board_code):
    """拉某板块日K → 多窗口收益（compute_window_returns）。失败返回 None。"""
    url = (
        f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=90.{board_code}"
        f"&klt=101&fqt=1&lmt=140&end=20500101&fields1=f1&fields2=f51,f53"
    )
    text = _get(url)
    if not text:
        return None
    try:
        kl = json.loads(text).get("data", {}).get("klines") or []
        closes = [float(x.split(",")[1]) for x in kl]
    except Exception:
        return None
    if len(closes) < 6:
        return None
    return compute_window_returns(closes)


def scan_sectors(top=8, board=None):
    """组装板块多窗口扫描结果。board 指定则只下钻该板块（名称模糊匹配）。

    返回 [{code,name,today,d5,d22,d120,vol30,trend,note}]。
    """
    boards = fetch_all_boards()
    if not boards:
        return []
    if board:
        boards = [b for b in boards if board in b["name"]] or boards[:0]
        picked = boards
    else:
        picked = boards[:top] + boards[-top:]  # 涨幅榜 + 落后榜
    rows = []
    for b in picked:
        w = fetch_board_windows(b["code"]) or {}
        trend, note = classify_board_trend(w) if w else ("数据缺失", "")
        rows.append({
            "code": b["code"], "name": b["name"], "today": b["today"],
            "d5": w.get("d5"), "d22": w.get("d22"), "d120": w.get("d120"),
            "vol30": w.get("vol30"), "trend": trend, "note": note,
        })
    return rows


def cmd_sector_scan(args):
    """板块多窗口扫描：今日 + 7日 + 30日 + 6月 + 趋势分类。"""
    rows = scan_sectors(top=args.top, board=args.board)
    if not rows:
        print("获取板块数据失败")
        return
    if getattr(args, "json", False):
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return

    def f(v):
        return f"{v:>+6.1f}%" if isinstance(v, (int, float)) else "   --  "
    print(f"\n📊 板块多窗口扫描（{'下钻 ' + args.board if args.board else '热门+落后'}）")
    print(f"{'板块':<10} {'今日':>7} {'7日':>7} {'30日':>7} {'6月':>7} {'30d波动':>7}  趋势")
    print("-" * 78)
    for r in rows:
        nm = r["name"][:9]
        print(f"{nm:<10} {f(r['today'])} {f(r['d5'])} {f(r['d22'])} "
              f"{f(r['d120'])} {f(r['vol30'])}  {r['trend']}")
    print("\n说明：今日涨≠趋势好。优先「强势趋势」（多窗口同向上行）；"
          "「超跌反弹·谨慎」是下跌中继，少追。再用 `discover --sector <板块>` 下钻选基。")


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


# ──────────────────────────────────────────────────────────────────────
# 选基发现（R3/R4）：跨板块下钻挑场外候选，多窗口一致性打分
# ──────────────────────────────────────────────────────────────────────
_RANK_PERIOD_SC = {"1n": "1nzf", "6n": "6nzf", "3n": "3nzf", "2n": "2nzf", "jn": "jnzf"}


def _rank_url(ft, period, top):
    sc = _RANK_PERIOD_SC.get(period, "6nzf")
    now = datetime.now()
    ed = now.strftime("%Y-%m-%d")
    days = {"1n": 365, "6n": 180, "3n": 365 * 3, "2n": 365 * 2}.get(period)
    if period == "jn":
        sd = now.replace(month=1, day=1).strftime("%Y-%m-%d")
    else:
        sd = (now - timedelta(days=days or 180)).strftime("%Y-%m-%d")
    return (
        f"http://fund.eastmoney.com/data/rankhandler.aspx?"
        f"op=ph&dt=kf&ft={ft}&rs=&gs=0&sc={sc}&st=desc"
        f"&sd={sd}&ed={ed}&qdii=&tabSubtype=,,,,,&pi=1&pn={top}&dx=1"
    )


def _fnum(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


# ========== 基金基本面 + 红旗清单（借鉴 jiafei 五层框架，补我们量价盲区）==========

def _pingzhong_var(txt, var):
    """从 pingzhongdata/{code}.js 抽 `var X = ...;` 的右值原文（含 JSON）。"""
    m = re.search(r"var %s\s*=\s*(.+?);" % re.escape(var), txt, re.S)
    return m.group(1) if m else None


def _parse_work_years(s):
    """'10年又343天' → 10.94；'343天' → 0.94；空/异常 → None。纯函数。"""
    if not s:
        return None
    y = re.search(r"(\d+)\s*年", s)
    d = re.search(r"(\d+)\s*天", s)
    if not y and not d:
        return None
    return round((int(y.group(1)) if y else 0) + (int(d.group(1)) if d else 0) / 365.0, 2)


def _pct_str(s):
    """'35.29%' → 35.29；数值原样；异常 → None。纯函数。"""
    if isinstance(s, (int, float)):
        return float(s)
    if not s:
        return None
    m = re.search(r"-?\d+(\.\d+)?", str(s))
    return float(m.group(0)) if m else None


def _fetch_top10_concentration(code):
    """前十大持仓占净值比合计（最新季报）→ float% 或 None。"""
    url = (f"https://fundf10.eastmoney.com/FundArchivesDatas.aspx?"
           f"type=jjcc&code={code}&topline=10&year=&month=&rt=0.1")
    txt = _get(url, headers={"Referer": f"https://fundf10.eastmoney.com/ccmx_{code}.html"})
    if not txt:
        return None
    pcts = [float(x) for x in re.findall(r"(\d+\.\d+)%", txt)][:10]  # 最新一季 top10
    return round(sum(pcts), 1) if pcts else None


def fetch_fundamentals(code):
    """基金基本面（规模/持有人/经理/资产配置/能力雷达/费率/前十大集中度）。

    单次拉 fund.eastmoney pingzhongdata（一个 JS 含全部）+ 一次持仓，纯网络读取，
    失败返回 {}（绝不抛异常）。供红旗检查 / discover 质量闸门 / why-not 复用。
    """
    out = {"code": code}
    try:
        txt = _get(f"http://fund.eastmoney.com/pingzhongdata/{code}.js",
                   headers={"Referer": "http://fund.eastmoney.com/"})
        if not txt:
            return {}
        name = _pingzhong_var(txt, "fS_name")
        if name:
            out["name"] = name.strip().strip('"')

        scale = _pingzhong_var(txt, "Data_fluctuationScale")
        if scale:
            try:
                series = json.loads(scale).get("series", [])
                if series:
                    out["scale"] = series[-1].get("y")            # 最新规模(亿)
                    out["scale_mom"] = _pct_str(series[-1].get("mom"))  # 环比%
                    out["scale_series"] = [s.get("y") for s in series]
            except Exception:
                pass

        hs = _pingzhong_var(txt, "Data_holderStructure")
        if hs:
            try:
                for s in json.loads(hs).get("series", []):
                    if "机构" in s.get("name", ""):
                        data = [x for x in s.get("data", []) if x is not None]
                        if data:
                            out["inst_pct"] = data[-1]
            except Exception:
                pass

        mgr = _pingzhong_var(txt, "Data_currentFundManager")
        if mgr:
            try:
                mj = json.loads(mgr)
                if mj:
                    m0 = mj[0]
                    out["manager"] = {
                        "name": m0.get("name"),
                        "work_years": _parse_work_years(m0.get("workTime")),
                        "ability": _fnum((m0.get("power") or {}).get("avr")),
                    }
            except Exception:
                pass

        aa = _pingzhong_var(txt, "Data_assetAllocation")
        if aa:
            try:
                amap = {}
                for s in json.loads(aa).get("series", []):
                    data = [x for x in s.get("data", []) if x is not None]
                    if not data:
                        continue
                    nm = s.get("name", "")
                    if "股票" in nm:
                        amap["stock"] = data[-1]
                    elif "债券" in nm:
                        amap["bond"] = data[-1]
                    elif "现金" in nm:
                        amap["cash"] = data[-1]
                if amap:
                    out["asset"] = amap
            except Exception:
                pass

        pe = _pingzhong_var(txt, "Data_performanceEvaluation")
        if pe:
            try:
                pj = json.loads(pe)
                cats, data = pj.get("categories", []), pj.get("data", [])
                if cats and data and len(cats) == len(data):
                    out["abilities"] = dict(zip(cats, data))
                    out["ability_avr"] = _fnum(pj.get("avr"))
            except Exception:
                pass

        rate = _pingzhong_var(txt, "fund_Rate")
        if rate:
            out["mgmt_rate"] = _fnum(rate.strip().strip('"'))

        out["top10_concentration"] = _fetch_top10_concentration(code)
        return out
    except Exception:
        return out if len(out) > 1 else {}


def evaluate_red_flags(f, equity=True):
    """基金质量红旗清单（纯函数，借鉴 jiafei 13 红旗的可计算子集）。

    返回 [{level, key, msg}]，level ∈ {'critical'(应否决), 'warn'(提示)}。
    equity=False 时放宽规模/集中度阈值（债基/货基不适用权益口径）。
    """
    flags = []
    if not f:
        return flags

    def add(level, key, msg):
        flags.append({"level": level, "key": key, "msg": msg})

    scale = f.get("scale")
    if scale is not None:
        if scale < 2:
            add("critical", "scale_tiny", f"规模仅 {scale:.2f}亿（<2亿），有清盘风险")
        elif equity and scale > 500:
            add("warn", "scale_huge", f"规模 {scale:.0f}亿（>500亿），主动管理船大难掉头")

    mom = f.get("scale_mom")
    if mom is not None and mom > 50:
        add("warn", "scale_surge", f"单季规模激增 {mom:.0f}%，新钱涌入或稀释收益")

    ss = [x for x in (f.get("scale_series") or []) if x is not None]
    if len(ss) >= 3 and ss[-1] < ss[-2] < ss[-3]:
        add("warn", "scale_shrink", "规模连续两季缩水，资金在撤离")

    inst = f.get("inst_pct")
    if inst is not None and inst > 90:
        add("critical", "inst_heavy", f"机构持有 {inst:.0f}%（>90%），集中赎回易踩踏")

    wy = (f.get("manager") or {}).get("work_years")
    if wy is not None and wy < 3:
        add("warn", "mgr_green", f"现任经理从业 {wy:.1f}年（<3年），未历完整牛熊")

    ab = f.get("abilities") or {}
    for dim in ("抗风险", "稳定性"):
        v = ab.get(dim)
        if v is not None and v < 60:
            add("warn", "ability_low", f"{dim}评分仅 {v:.0f}（<60），是短板")

    bond = (f.get("asset") or {}).get("bond")
    if bond is not None and bond > 120:
        add("critical", "leverage", f"债券占净比 {bond:.0f}%（>120%），加杠杆放大风险")

    conc = f.get("top10_concentration")
    if conc is not None and conc > 60:
        add("warn", "concentration", f"前十大持仓占 {conc:.0f}%（>60%），高度集中押注单一方向")

    return flags


def has_critical(flags):
    """是否含 critical 级红旗（否决用）。纯函数。"""
    return any(x.get("level") == "critical" for x in (flags or []))


def fetch_fund_rank(ft="gp", period="6n", top=60, otc_only=True):
    """结构化基金排行：每只带多窗口区间收益（纯解析可被 cmd_rank/discover 复用）。

    datas 字段：[0]code [1]name [3]date [4]nav [6]日 [7]近1周 [8]近1月
    [9]近3月 [10]近6月 [11]近1年。返回 [{code,name,venue,date,nav,
    w_1d,w_1w,w_1m,w_3m,w_6m,w_1y}]。失败返回 []。
    """
    text = _get(_rank_url(ft, period, top))
    if not text:
        return []
    m = re.search(r'datas:\[(.*?)\]', text, re.DOTALL)
    if not m:
        return []
    out = []
    for item in re.findall(r'"([^"]+)"', m.group(1)):
        f = item.split(",")
        if len(f) < 12:
            continue
        code, name = f[0], f[1]
        venue = fund_venue(code, name)
        if otc_only and venue == "场内":
            continue
        out.append({
            "code": code, "name": name, "venue": venue,
            "date": f[3], "nav": _fnum(f[4]),
            "w_1d": _fnum(f[6]), "w_1w": _fnum(f[7]), "w_1m": _fnum(f[8]),
            "w_3m": _fnum(f[9]), "w_6m": _fnum(f[10]), "w_1y": _fnum(f[11]),
        })
    return out


def score_candidate(c):
    """多窗口一致性评分（纯函数）：偏好中长期持续上行、不追一周脉冲。

    主看近6月/3月/1月同向走强；近一周涨幅过猛（>15%）轻微扣分避免追高。
    """
    w6 = c.get("w_6m") or 0.0
    w3 = c.get("w_3m") or 0.0
    w1 = c.get("w_1m") or 0.0
    ww = c.get("w_1w") or 0.0
    score = 0.5 * w6 + 0.3 * w3 + 0.2 * w1
    if ww > 15:
        score -= (ww - 15) * 0.5  # 一周飙太多，扣分防追高
    # 多窗口全为正的一致性加成
    if w6 > 0 and w3 > 0 and w1 > 0:
        score += 5
    return round(score, 2)


def discover_candidates(sectors=None, limit=8, per_sector=2, exclude=None,
                        top_n=100, quality=False):
    """跨板块下钻挑场外候选基金。

    拉「股票型 + 指数型」近6月排行（仅场外）→ 按基金名归类赛道 →
    多窗口一致性打分 → 每赛道取前 per_sector、跨赛道轮转凑满 limit。
    sectors 给定时只保留命中（按赛道标签或名称子串）。exclude 跳过已持有/已知。
    返回 [{code,name,sector,score,w_1d,w_1w,w_1m,w_3m,w_6m,venue}]（无网络则 []）。

    quality=True：对入选标的拉基本面跑红旗检查（借鉴 jiafei），剔除 critical 红旗
    （规模<2亿/机构>90%/杠杆>120%），其余附 red_flags 字段。每只多一次 pingzhongdata
    抓取（~500KB），故默认关闭——仅手动 discover/选基质检时开，不拖慢三时段日报快照。
    """
    exclude = set(exclude or [])
    pool = {}
    for ft in ("gp", "zs"):
        for c in fetch_fund_rank(ft=ft, period="6n", top=top_n, otc_only=True):
            if c["code"] in exclude or c["code"] in pool:
                continue
            pool[c["code"]] = c

    def matches(name, sec):
        if not sectors:
            return True
        for s in sectors:
            if s and (s in name or s == sec):
                return True
        return False

    buckets = {}
    for c in pool.values():
        sec = _infer_sector(c["name"])
        if not matches(c["name"], sec):
            continue
        c = dict(c, sector=sec, score=score_candidate(c))
        buckets.setdefault(sec, []).append(c)

    for sec in buckets:
        buckets[sec].sort(key=lambda x: x["score"], reverse=True)

    # 跨赛道轮转，保证多样性（不只盯一个赛道）
    ordered = sorted(buckets.keys(),
                     key=lambda s: buckets[s][0]["score"], reverse=True)
    picked, rank_in_sec = [], {s: 0 for s in ordered}
    while len(picked) < limit:
        progressed = False
        for sec in ordered:
            i = rank_in_sec[sec]
            if i < min(per_sector, len(buckets[sec])):
                picked.append(buckets[sec][i])
                rank_in_sec[sec] += 1
                progressed = True
                if len(picked) >= limit:
                    break
        if not progressed:
            break
    picked.sort(key=lambda x: x["score"], reverse=True)

    if quality:
        kept = []
        for c in picked:
            fund = fetch_fundamentals(c["code"])
            equity = c.get("sector") not in ("债券", "货币")
            flags = evaluate_red_flags(fund, equity=equity)
            if has_critical(flags):
                continue  # 质量否决：不进发现列表
            c = dict(c, red_flags=flags,
                     scale=fund.get("scale"), inst_pct=fund.get("inst_pct"))
            kept.append(c)
        picked = kept
    return picked


def cmd_discover(args):
    """跨板块发现新候选基金（默认排除已持有，避免老盯那几只）。"""
    sectors = [s.strip() for s in args.sector.split(",")] if args.sector else None
    exclude = set()
    if DB_AVAILABLE:
        try:
            db = Database()
            row = db.conn.execute(
                "SELECT id FROM accounts WHERE name=?", (args.account,)).fetchone()
            if row:
                for p in db.conn.execute(
                        "SELECT code FROM positions WHERE account_id=?", (row["id"],)):
                    exclude.add(p["code"])
            db.close()
        except Exception:
            pass
    cands = discover_candidates(sectors=sectors, limit=args.top,
                                per_sector=args.per_sector, exclude=exclude,
                                quality=getattr(args, "quality", False))
    if getattr(args, "json", False):
        print(json.dumps(cands, ensure_ascii=False, indent=2))
        return
    if not cands:
        print("未发现候选（可能离线，或筛选过严）")
        return

    def f(v):
        return f"{v:>+6.1f}%" if isinstance(v, (int, float)) else "   --  "
    title = f"赛道 {args.sector}" if sectors else "跨赛道"
    print(f"\n🔭 选基发现（{title}，已排除持仓）Top {len(cands)}：")
    print(f"{'代码':<8} {'名称':<22} {'赛道':<5} {'今日':>7} {'1周':>7} "
          f"{'1月':>7} {'3月':>7} {'6月':>7} {'评分':>6}")
    print("-" * 96)
    for c in cands:
        print(f"{c['code']:<8} {c['name'][:21]:<22} {c['sector']:<5} "
              f"{f(c['w_1d'])} {f(c['w_1w'])} {f(c['w_1m'])} {f(c['w_3m'])} "
              f"{f(c['w_6m'])} {c['score']:>6.1f}")
        for fl in c.get("red_flags", []):
            print(f"         ⚠️ {fl['msg']}")
    if getattr(args, "quality", False):
        print("（已过质量闸门：剔除清盘/踩踏/杠杆等 critical 红旗标的）")
    print("\n短线买C类、长线买A类：选定后用 `share-class <代码> --prefer C|A` 查对应份额。")


def cmd_fundamentals(args):
    """基金基本面体检 + 红旗清单（借鉴 jiafei 五层质量分析，补量价盲区）。"""
    f = fetch_fundamentals(args.code)
    if not f or len(f) <= 1:
        print(f"未取到 {args.code} 的基本面（可能离线或代码无效）")
        return
    equity = True  # 单查默认按权益口径；债基阈值差异在 discover 里按赛道处理
    flags = evaluate_red_flags(f, equity=equity)
    if getattr(args, "json", False):
        print(json.dumps({**f, "red_flags": flags}, ensure_ascii=False, indent=2))
        return
    print(f"\n🩺 基本面体检：{f.get('name', args.code)}（{args.code}）")
    print("-" * 56)
    if f.get("scale") is not None:
        mom = f.get("scale_mom")
        print(f"  规模      {f['scale']:.2f}亿" + (f"（环比 {mom:+.1f}%）" if mom is not None else ""))
    if f.get("inst_pct") is not None:
        print(f"  机构持有  {f['inst_pct']:.1f}%")
    mgr = f.get("manager") or {}
    if mgr.get("name"):
        wy = mgr.get("work_years")
        print(f"  基金经理  {mgr['name']}" + (f"（从业 {wy:.1f}年" if wy else "")
              + (f"，能力评分 {mgr['ability']:.0f}）" if mgr.get("ability") else "）" if wy else ""))
    asset = f.get("asset") or {}
    if asset:
        print(f"  资产配置  股 {asset.get('stock','-')}% / 债 {asset.get('bond','-')}% / 现金 {asset.get('cash','-')}%")
    if f.get("top10_concentration") is not None:
        print(f"  前十大集中度 {f['top10_concentration']:.1f}%")
    if f.get("mgmt_rate") is not None:
        print(f"  管理费率  {f['mgmt_rate']}%/年")
    print()
    if flags:
        print("  🚩 红旗：")
        for fl in flags:
            mark = "🔴" if fl["level"] == "critical" else "🟡"
            print(f"    {mark} {fl['msg']}")
    else:
        print("  ✅ 未触发质量红旗")


# ──────────────────────────────────────────────────────────────────────
# 份额兄弟解析（R5）：A↔C 份额查找，短线买C·长线买A
# ──────────────────────────────────────────────────────────────────────
def _fund_search(key):
    """天天基金搜索接口 → [{code,name}]。失败返回 []。"""
    import urllib.parse
    url = ("https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx?"
           f"m=1&key={urllib.parse.quote(key)}")
    text = _get(url, headers={"Referer": "https://fund.eastmoney.com/"})
    if not text:
        return []
    try:
        rows = json.loads(text).get("Datas") or []
    except Exception:
        return []
    return [{"code": r.get("CODE"), "name": r.get("NAME")}
            for r in rows if r.get("CODE") and r.get("NAME")]


def pick_siblings(rows, base):
    """从搜索结果里按基名匹配同一基金的各类份额（纯函数）→ {类别字母: code}。"""
    sib = {}
    for r in rows:
        if base_fund_name(r["name"]) == base:
            cls = detect_share_class(r["name"]) or "?"
            sib.setdefault(cls, r["code"])
    return sib


def resolve_share_class(code, prefer=None, name=None):
    """基金代码 → 当前份额类别 + A/C 兄弟代码 + 推荐买入代码（需联网）。"""
    if not name:
        text = _get(f"http://fundgz.1234567.com.cn/js/{code}.js")
        gz = _parse_jsonp(text) if text else None
        name = gz.get("name") if gz else None
    if not name:
        for r in _fund_search(code):
            if r["code"] == code:
                name = r["name"]
                break
    if not name:
        return {"error": f"无法获取 {code} 名称（可能离线）"}
    cur = detect_share_class(name)
    base = base_fund_name(name)
    # 搜索接口对含括注（如「(QDII)」）的长名匹配很差，去掉括注再搜，
    # 但仍按完整 base 比对兄弟（保留「人民币/美元」区分，避免错配）。
    search_key = _PAREN_RE.sub("", base).strip()
    sib = pick_siblings(_fund_search(search_key), base)
    if prefer and prefer not in sib and search_key != base:
        sib.update(pick_siblings(_fund_search(base), base))
    sib.setdefault(cur or "?", code)
    rec = sib.get(prefer) if prefer else None
    return {"code": code, "name": name, "current_class": cur, "base": base,
            "siblings": sib, "prefer": prefer, "recommended": rec}


_SHARE_CLASS_NOTE = {
    "C": "短线优选C类（无申购费、按日计销售服务费，持有约<1~2年更省费）",
    "A": "长线优选A类（有申购费但无销售服务费，长期持有总成本更低）",
}


def cmd_share_class(args):
    """查份额类别 + A/C 兄弟代码，给短C长A建议。"""
    r = resolve_share_class(args.code, prefer=args.prefer)
    if getattr(args, "json", False):
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return
    if r.get("error"):
        print(r["error"])
        return
    print(f"\n{r['name']}（{args.code}）当前为 {r['current_class'] or '单一/未知'} 类")
    if r["siblings"]:
        print("兄弟份额：")
        for cls, c in sorted(r["siblings"].items()):
            print(f"  {cls}类: {c}{'  ← 当前' if c == args.code else ''}")
    if args.prefer:
        note = _SHARE_CLASS_NOTE.get(args.prefer, "")
        if r.get("recommended"):
            same = "（已是该类，无需换）" if r["recommended"] == args.code else ""
            print(f"→ 建议买 {args.prefer} 类：{r['recommended']}{same}　{note}")
        else:
            print(f"→ 未找到 {args.prefer} 类兄弟份额（可能该基金无此份额）")


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


def fetch_index_kline(secid, days=120):
    """指数日 K 线 → {name, points:[{date,open,close,high,low,volume}, ...]}（升序）。

    纯数据，无打印；失败返回 {"name": secid, "points": []}。Web 面板与 CLI 共用。
    """
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=days * 2 + 10)).strftime("%Y%m%d")
    url = (
        f"https://push2his.eastmoney.com/api/qt/stock/kline/get?"
        f"secid={secid}&fields1=f1,f2,f3"
        f"&fields2=f51,f52,f53,f54,f55,f56"
        f"&klt=101&fqt=0&beg={start_date}&end={end_date}"
    )
    text = _get(url)
    if not text:
        return {"name": secid, "points": []}
    try:
        data = json.loads(text)
    except Exception:
        return {"name": secid, "points": []}
    name = data.get("data", {}).get("name", secid)
    klines = data.get("data", {}).get("klines", []) or []
    points = []
    for line in klines:
        p = line.split(",")
        if len(p) >= 5:
            try:
                points.append({
                    "date": p[0], "open": float(p[1]), "close": float(p[2]),
                    "high": float(p[3]), "low": float(p[4]),
                    "volume": float(p[5]) if len(p) > 5 and p[5] else 0.0,
                })
            except (ValueError, IndexError):
                continue
    return {"name": name, "points": points[-days:]}


def cmd_index_kline(args):
    """获取指数历史K线"""
    res = fetch_index_kline(args.secid, args.days)
    points = res["points"]
    if not points:
        print(f"获取指数 {args.secid} K线失败")
        return

    print(f"\n{res['name']} 近 {args.days} 天日K线:")
    print(f"{'日期':<12} {'开盘':>10} {'收盘':>10} {'最高':>10} {'最低':>10}")
    print("-" * 55)
    for pt in points:
        print(f"{pt['date']:<12} {pt['open']:>10.2f} {pt['close']:>10.2f} "
              f"{pt['high']:>10.2f} {pt['low']:>10.2f}")

    first_close = points[0]["close"]
    last_close = points[-1]["close"]
    if first_close:
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


def _buy_unconfirmed(buy_date, ref=None):
    """当天（或更晚）买入 → 份额/净值未确认（与 daily_report 的 is_pending 同义）。"""
    if not buy_date:
        return False
    ref = ref or datetime.now()
    s = str(buy_date)[:10]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date() >= ref.date()
        except ValueError:
            continue
    return False


def portfolio_return_series(account_name=None, days=30):
    """组合「总收益率」时间序列 [(date, pct)]（净值派生，含迷你走势用）。

    复用 _align_total_return_series；当天买入（未确认）排除，避免其历史净值扭曲曲线。
    无持仓或无净值返回 []。供邮件钱包卡片 / Web 面板 / CLI 共用。
    """
    portfolio = _load_portfolio(account_name)
    if not portfolio:
        return []
    holdings = []
    for h in portfolio:
        if _buy_unconfirmed(h.get("buy_date")):
            continue
        series = fetch_nav_series(h.get("code", ""), days=days)
        holdings.append((h.get("shares", 0), h.get("cost_nav", 0), series))
    return _align_total_return_series(holdings)


def calibrate_costs(account_name, apply=False):
    """把"按盘中估值记录"的最近买入校准到**实际收盘净值**（份额+成本一起改）。

    场外基金按收盘净值确认份额：记账当时只有估值，晚间真实净值公布后会有偏差。
    本函数等真实净值出来后把这笔买入的 cost_nav 与 shares 修正到真实值。
    **安全闸**：只动"单笔新买入"的持仓（持仓 shares/cost 恰好等于最近一笔买入），
    累计/导入（无对应买单或已多次加仓）的持仓一律跳过、只报告——绝不乱改成本基准。

    apply=False 仅预览；返回每只的校准明细。幂等：校准后把该买单 nav/shares 改真实值
    并标 outcome=nav_calibrated，下次不再重复。
    """
    if not (DB_AVAILABLE and account_name):
        return []
    db = Database()
    try:
        account = db.get_account(name=account_name)
        if not account:
            return []
        aid = account["id"]
        trades = db.get_trades(aid)   # 已按 date DESC, id DESC
        out = []
        for p in db.get_positions(aid):
            code, shares, cost = p["code"], p["shares"], p["cost_nav"]
            buys = [t for t in trades if t["code"] == code and t["action"] == "buy"]
            if not buys:
                continue
            last = buys[0]
            tdate = str(last["date"])[:10]
            if _buy_unconfirmed(tdate):             # 当天净值还没公布，等下次
                continue
            amount, rec_nav, rec_shares = (last["amount"] or 0.0), (last["nav"] or 0.0), (last["shares"] or 0.0)
            if amount <= 0 or rec_nav <= 0:
                continue
            series = dict((str(d)[:10], nav) for d, nav in fetch_nav_series(code, days=15))
            real = series.get(tdate)
            if not real or real <= 0:               # 还没拿到那天的真实净值
                continue
            if abs(real - rec_nav) / rec_nav < 0.0005:   # 已经准确（含已校准过的）
                continue
            single_lot = (abs(shares - rec_shares) <= max(0.01, rec_shares * 0.001)
                          and abs(cost - rec_nav) < 1e-6)
            if not single_lot:
                out.append({"code": code, "name": p["name"], "status": "skip_accumulated",
                            "old_nav": rec_nav, "new_nav": real, "date": tdate})
                continue
            new_shares = round(amount / real, 2)
            entry = {"code": code, "name": p["name"],
                     "status": "applied" if apply else "preview",
                     "old_nav": rec_nav, "new_nav": real,
                     "old_shares": rec_shares, "new_shares": new_shares, "date": tdate}
            if apply:
                db.set_position(aid, code, p["name"], new_shares, real,
                                buy_date=p.get("buy_date"), sector=p.get("sector"),
                                note=p.get("note"))
                db.conn.execute(
                    "UPDATE trades SET nav=?, shares=?, outcome='nav_calibrated' WHERE id=?",
                    (real, new_shares, last["id"]))
                db.conn.commit()
            out.append(entry)
        return out
    finally:
        db.close()


def cmd_calibrate(args):
    """校准按估值记录的最近买入到真实净值（默认预览，--apply 写入）。"""
    rows = calibrate_costs(args.account, apply=args.apply)
    if not rows:
        print("无可校准的买入（要么净值未公布，要么已准确）。")
        return
    verb = "已校准" if args.apply else "待校准（预览，加 --apply 写入）"
    print(f"\n净值校准 — 账户 {args.account}：{verb}")
    print("-" * 72)
    for r in rows:
        if r["status"] == "skip_accumulated":
            print(f"  ⏭  {r['name']}（{r['code']}）累计/导入持仓，跳过："
                  f"估值 {r['old_nav']:.4f} → 实际 {r['new_nav']:.4f}，请手动核对")
            continue
        print(f"  ✓  {r['name']}（{r['code']}）{r['date']}：净值 {r['old_nav']:.4f}→{r['new_nav']:.4f}，"
              f"份额 {r['old_shares']:.2f}→{r['new_shares']:.2f}")
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
            f"fundCode={code}&pageIndex=1&pageSize=140"  # 140≈半年交易日，供6月窗口
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

        # 30日波动率（日收益标准差，年化前的原始百分比）
        vol_30d = None
        if len(navs) >= 22:
            seg = navs[-22:]
            rr = [(seg[i] / seg[i - 1] - 1) for i in range(1, len(seg)) if seg[i - 1]]
            if rr:
                mean = sum(rr) / len(rr)
                vol_30d = (sum((r - mean) ** 2 for r in rr) / len(rr)) ** 0.5

        return {
            "name": display_name,
            "current_nav": current_nav,
            "day_return": day_return,
            "fund_3d_return": _ret(3),
            "fund_5d_return": _ret(5),
            "fund_20d_return": _ret(20),
            "fund_60d_return": _ret(60),    # ≈近3月
            "fund_120d_return": _ret(120),  # ≈近6月
            "vol_30d": vol_30d,
            "high_20d": max(navs[-20:]) if len(navs) >= 20 else current_nav,
            "sector": sector or _infer_sector(display_name),
            "signals": sig,
        }
    except Exception:
        return None


def gather_market_snapshot(account_name="主线", date=None, discover=0):
    """Aggregate inputs DecisionEngine.decide() needs.

    Returns dict with hs300 returns + per-fund snapshots for portfolio + watchlist.
    Each missing fund becomes None — engine emits data_missing alert for those.

    discover>0：再注入 N 只「跨板块发现」的新候选（默认 0=关闭，保持离线/测试
    确定性；decide.py/daily_report 显式开启，让引擎不只盯固定那几只）。
    """
    import sys as _sys
    _sys.path.insert(0, str(SCRIPT_DIR := Path(__file__).resolve().parent))
    from db import Database

    db = Database()
    try:
        hs300_5d, hs300_20d = None, None  # 并发块里抓（见下）

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

        # ── 并发抓取所有独立 IO：HS300/指数趋势/快讯 + 每只基金快照 ──
        # 原本串行（每只基金 2 个请求 + 指数失败重试退避）≈ 9s；并发后约等于
        # 最慢单条链路（~2-3s）。urllib 在 IO 上释放 GIL，线程池有效。
        from concurrent.futures import ThreadPoolExecutor

        specs = [(p["code"], p["name"], p["sector"], "held") for p in positions]
        held_codes = {p["code"] for p in positions}
        try:
            from simulate import DEFAULT_FUNDS  # type: ignore
            for code, name in DEFAULT_FUNDS.items():
                if code not in held_codes:
                    specs.append((code, name, None, "watchlist"))
        except Exception:
            pass

        funds = {}
        with ThreadPoolExecutor(max_workers=12) as ex:
            f_hs = ex.submit(_hs300_returns)
            f_idx = ex.submit(gather_index_trend)
            f_news = ex.submit(gather_market_news, 8)
            fund_futs = {ex.submit(_fund_snapshot, c, n, s): (c, src)
                         for c, n, s, src in specs}
            try:
                hs300_5d, hs300_20d = f_hs.result()
            except Exception:
                hs300_5d, hs300_20d = None, None
            try:
                index_trend = f_idx.result()
            except Exception:
                index_trend = {}
            try:
                news = f_news.result() or []
            except Exception:
                news = []
            for fut, (code, src) in fund_futs.items():
                try:
                    snap = fut.result()
                except Exception:
                    snap = None
                if snap:
                    snap.setdefault("source", src)
                    funds[code] = snap

        # P5 趋势状态：QDII 基金标记参考指数（trend_exit 用 NDX 而非 A 股）
        for code in funds:
            if code in QDII_INDEX_MAP:
                funds[code]["ref_index"] = "NDX"

        # R3/R4 动态候选池：跨板块发现新基金，让引擎不只盯固定那几只。
        # 默认关闭（discover=0）；显式开启时注入，已持有/已知会被排除（候选快照也并发）。
        discovered = []
        if discover and discover > 0:
            try:
                cands = discover_candidates(limit=discover, exclude=set(funds.keys()))
                with ThreadPoolExecutor(max_workers=min(8, len(cands) or 1)) as ex:
                    cfuts = {ex.submit(_fund_snapshot, c["code"], c["name"],
                                       c.get("sector")): c for c in cands}
                    for fut, c in cfuts.items():
                        try:
                            snap = fut.result()
                        except Exception:
                            snap = None
                        if not snap:
                            continue
                        snap["source"] = "discovered"
                        snap["discover_score"] = c.get("score")
                        snap.setdefault("rank_windows", {
                            k: c.get(k) for k in ("w_1w", "w_1m", "w_3m", "w_6m")})
                        funds[c["code"]] = snap
                        discovered.append({"code": c["code"], "name": c["name"],
                                           "sector": c.get("sector"),
                                           "score": c.get("score")})
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

        # 报告层附加：操作复盘记忆（news 已在上方并发块抓取，不再重复请求）
        try:
            review_summary = db.get_review_summary(account_id, lookback_days=60)
        except Exception:
            review_summary = {"count": 0}

        # P7: 定投基金代码（引擎据此不出买入建议）
        try:
            dca_codes = [p["code"] for p in db.get_dca_plans(account_id)]
        except Exception:
            dca_codes = []

        return {
            "hs300_5d_return": hs300_5d,
            "hs300_20d_return": hs300_20d,
            "regime_hint": None,
            "funds": funds,
            "discovered": discovered,
            "portfolio_peak_value": peak,
            "index_trend": index_trend,
            "news": news,
            "recent_review_summary": review_summary,
            "auto_invest_codes": dca_codes,
        }
    finally:
        db.close()


def cmd_market_snapshot(args):
    snap = gather_market_snapshot(account_name=args.account, date=args.date,
                                  discover=getattr(args, "discover", 0))
    print(json.dumps(snap, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="基金数据抓取工具 — Smart Invest Skill")
    sub = parser.add_subparsers(dest="command", help="子命令")

    # now — 会话开始对表（本机时间/时区/交易时段）
    p_now = sub.add_parser("now", help="本机时间/时区/星期 + A股交易时段（会话开始先对表）")
    p_now.add_argument("--json", action="store_true", help="输出 JSON")

    # indices
    sub.add_parser("indices", help="获取大盘指数实时行情")

    # sectors
    sub.add_parser("sectors", help="获取行业板块涨跌排行")

    # sector-scan — 板块多窗口扫描（7日/30日/6月 + 趋势分类）
    p_ss = sub.add_parser("sector-scan", help="板块多窗口扫描：今日/7日/30日/6月波动+趋势分类")
    p_ss.add_argument("--top", type=int, default=8, help="涨幅榜/落后榜各取 N（默认8）")
    p_ss.add_argument("--board", "-b", help="只下钻某板块（名称模糊匹配）")
    p_ss.add_argument("--json", action="store_true", help="输出 JSON")

    # discover — 跨板块发现新候选基金
    p_disc = sub.add_parser("discover", help="跨板块发现新候选基金（多窗口一致性打分，排除持仓）")
    p_disc.add_argument("--sector", "-s", help="限定赛道/关键词（逗号分隔，如 半导体,新能源）")
    p_disc.add_argument("--account", "-a", default="主线", help="排除该账户已持有（默认主线）")
    p_disc.add_argument("--top", type=int, default=8, help="候选数量（默认8）")
    p_disc.add_argument("--per-sector", type=int, default=2, help="每赛道至多取 N（默认2）")
    p_disc.add_argument("--quality", action="store_true",
                        help="对候选拉基本面跑红旗检查，剔除清盘/踩踏/杠杆风险标的（慢，多几次抓取）")
    p_disc.add_argument("--json", action="store_true", help="输出 JSON")

    # fundamentals — 单只基金基本面 + 红旗清单（借鉴 jiafei 五层质量分析）
    p_fund = sub.add_parser("fundamentals", help="基金基本面体检：规模/持有人/经理/集中度 + 红旗清单")
    p_fund.add_argument("code", help="基金代码")
    p_fund.add_argument("--json", action="store_true", help="输出 JSON")

    # share-class — 份额类别 + A/C 兄弟代码（短C长A）
    p_sc = sub.add_parser("share-class", help="查份额类别+A/C兄弟代码（短线买C·长线买A）")
    p_sc.add_argument("code", help="基金代码")
    p_sc.add_argument("--prefer", choices=["A", "B", "C", "D", "E"], help="想买的份额类别")
    p_sc.add_argument("--json", action="store_true", help="输出 JSON")

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

    # calibrate — 把按估值记的买入校准到真实收盘净值
    p_cal = sub.add_parser("calibrate", help="把按估值记的最近买入校准到真实净值（默认预览，--apply 写入）")
    p_cal.add_argument("--account", "-a", required=True, help="账户名称")
    p_cal.add_argument("--apply", action="store_true", help="写入数据库（不加则仅预览）")

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
    p_snap.add_argument("--discover", type=int, default=0,
                        help="额外注入 N 只跨板块发现的新候选（默认0）")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    cmd_map = {
        "now": cmd_now,
        "sector-scan": cmd_sector_scan,
        "discover": cmd_discover,
        "fundamentals": cmd_fundamentals,
        "share-class": cmd_share_class,
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
        "calibrate": cmd_calibrate,
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
