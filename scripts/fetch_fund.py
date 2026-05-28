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

    print(f"\n基金排行 ({type_name} · {period_name}) Top {top}:")
    print(f"{'排名':>4} {'代码':<8} {'名称':<24} {'最新净值':>8} {'日期':<12} {'区间涨幅':>10}")
    print("-" * 75)

    for i, item in enumerate(items, 1):
        fields = item.split(",")
        if len(fields) < 5:
            continue
        code = fields[0]
        name = fields[1]
        date = fields[3] if len(fields) > 3 else ""
        nav = fields[4] if len(fields) > 4 else ""
        zzf = fields[field_idx] if len(fields) > field_idx and fields[field_idx] else "--"
        print(f"{i:>4} {code:<8} {name:<24} {nav:>8} {date:<12} {zzf:>9}%")


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
    print(f"{'基金名称':<20} {'估值涨幅':>8} {'估算净值':>8} {'成本净值':>8} {'持有份额':>10} {'估算盈亏':>12} {'累计收益':>10}")
    print("-" * 80)

    total_estimated_value = 0
    total_cost = 0
    total_today_pnl = 0

    for holding in portfolio:
        code = holding.get("code", "")
        name = holding.get("name", code)
        shares = holding.get("shares", 0)
        cost_nav = holding.get("cost_nav", 0)

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
                f"{shares:>10.2f} {pnl_sign}{today_pnl:>10.2f} {pnl_sign}{total_pnl_pct:>8.2f}%"
            )
        else:
            print(f"{name:<20} {'--':>8} {'--':>8} {cost_nav:>8.4f} {shares:>10.2f} {'--':>12} {'--':>10}")

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
    print("-" * 70)
    print(f"{'基金代码':<8} {'基金名称':<20} {'持有份额':>10} {'成本净值':>8} {'买入日期':<12} {'备注':<10}")
    print("-" * 70)
    for h in portfolio:
        print(
            f"{h.get('code', ''):<8} {h.get('name', ''):<20} "
            f"{h.get('shares', 0):>10.2f} {h.get('cost_nav', 0):>8.4f} "
            f"{h.get('buy_date', ''):<12} {h.get('note', ''):<10}"
        )


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
        if not text:
            return None, None
        data = json.loads(text)
        klines = data.get("data", {}).get("klines", [])
        if len(klines) < 21:
            return None, None
        closes = [float(k.split(",")[2]) for k in klines]
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
    """Fetch latest snapshot for one fund. Returns dict or None if data missing."""
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
            f"fundCode={code}&pageIndex=1&pageSize=25"
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
                navs = [float(i["DWJZ"]) for i in items if i.get("DWJZ")]
            except Exception:
                navs = []

        if not current_nav and navs:
            current_nav = navs[0]

        def _ret(n):
            return ((current_nav - navs[n]) / navs[n]) if len(navs) > n and navs[n] else 0.0

        if not current_nav:
            return None  # genuinely missing — engine will emit data_missing alert

        return {
            "name": display_name,
            "current_nav": current_nav,
            "day_return": day_return,
            "fund_3d_return": _ret(3),
            "fund_5d_return": _ret(5),
            "fund_20d_return": _ret(20),
            "high_20d": max(navs[:20]) if len(navs) >= 20 else current_nav,
            "sector": sector or _infer_sector(display_name),
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

        return {
            "hs300_5d_return": hs300_5d,
            "hs300_20d_return": hs300_20d,
            "regime_hint": None,
            "funds": funds,
            "portfolio_peak_value": peak,
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

    # estimate
    p_est = sub.add_parser("estimate", help="获取单只基金实时估值")
    p_est.add_argument("code", help="基金代码，如 110011")

    # nav
    p_nav = sub.add_parser("nav", help="获取基金历史净值")
    p_nav.add_argument("code", help="基金代码")
    p_nav.add_argument("--days", type=int, default=30, help="查询天数（默认30）")

    # rank
    p_rank = sub.add_parser("rank", help="基金排行")
    p_rank.add_argument("--type", default="all", choices=["gp", "hh", "zj", "zs", "qdii", "all"], help="基金类型")
    p_rank.add_argument("--period", default="1n", choices=["jn", "1n", "6n", "2n", "3n"], help="统计区间")
    p_rank.add_argument("--top", type=int, default=20, help="排名数量（默认20）")

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
        "estimate": cmd_estimate,
        "nav": cmd_nav,
        "rank": cmd_rank,
        "index-kline": cmd_index_kline,
        "portfolio-check": cmd_portfolio_check,
        "portfolio-show": cmd_portfolio_show,
        "orders-show": cmd_orders_show,
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
