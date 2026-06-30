#!/usr/bin/env python3
"""
梦境训练模式 - 历史回测模拟器
用历史数据验证投资策略的有效性，只使用当天及之前的数据（无未来函数）
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR.parent / "data"
SIM_DIR = DATA_DIR / "simulations"

# 导入数据库和决策引擎
sys.path.insert(0, str(SCRIPT_DIR))
try:
    from db import Database
    from decision_engine import DecisionEngine
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://fund.eastmoney.com/",
}

# 默认基金池（覆盖不同方向）
DEFAULT_FUNDS = {
    "006479": "广发纳斯达克100ETF联接C",
    "512480": "半导体ETF国联安",
    "660011": "农银中证500指数A",
    "540010": "汇丰晋信科技先锋股票",
    "005825": "海富通电子传媒股票A",
    "161725": "招商中证白酒指数A",
}

# 基准指数
BENCHMARKS = {
    "1.000300": "沪深300",
    "1.000001": "上证指数",
}

# QDII 基金 → 参考指数（与 fetch_fund.QDII_INDEX_MAP 对应，trend_exit 用）
QDII_REF_INDEX = {
    "006479": "NDX",
}

# 赛道定义（用于集中度检查）
SECTOR_KEYWORDS = {
    "科技": ["半导体", "芯片", "人工智能", "信息科技", "数字经济", "科技", "电子", "传媒"],
    "消费": ["白酒", "食品", "医药", "消费", "医疗", "生物"],
    "新能源": ["新能源", "光伏", "锂电", "碳中和", "电力设备"],
    "金融": ["银行", "券商", "保险", "金融", "非银"],
    "资源": ["黄金", "有色", "煤炭", "石油", "资源", "矿业"],
    "宽基": ["沪深300", "中证500", "中证1000", "创业板", "上证50", "科创50"],
    "海外": ["纳斯达克", "标普", "美股", "港股", "恒生", "QDII", "全球"],
}

def _infer_sector_local(name):
    """Local copy of sector inference (used by engine_mode market_data builder)."""
    if not name:
        return "其他"
    for sector, kws in SECTOR_KEYWORDS.items():
        for kw in kws:
            if kw in name:
                return sector
    return "其他"


# 赛道仓位上限
SECTOR_LIMITS = {
    "科技": 0.50,
    "消费": 0.30,
    "新能源": 0.30,
    "金融": 0.20,
    "资源": 0.20,
    "宽基": 0.30,
    "海外": 0.40,
}


def _get(url, retries=3, wait=1.5):
    """HTTP GET with retry"""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read().decode("utf-8")
        except Exception as e:
            if attempt < retries:
                time.sleep(wait * (attempt + 1))
                continue
            # 东财 CDN 偶发掐 Python 的 https（TLS 指纹），公开行情接口降级 http 再试
            if url.startswith("https://"):
                try:
                    req = urllib.request.Request(
                        "http://" + url[len("https://"):], headers=HEADERS)
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        return resp.read().decode("utf-8")
                except Exception:
                    pass
            return None


def fetch_nav_history(code, start_date, end_date):
    """获取基金历史净值 {date_str: nav}，自动分页"""
    result = {}
    page = 1
    page_size = 20  # API 最多返回 20 条/页

    while True:
        url = (
            f"https://api.fund.eastmoney.com/f10/lsjz?"
            f"fundCode={code}&pageIndex={page}&pageSize={page_size}"
            f"&startDate={start_date}&endDate={end_date}"
        )
        text = _get(url)
        if not text:
            break

        data = json.loads(text)
        raw = data.get("Data") or {}
        nav_list = raw.get("LSJZList") or []

        if not nav_list:
            break

        for item in nav_list:
            date = item.get("FSRQ", "")
            nav = item.get("DWJZ")
            if date and nav:
                result[date] = float(nav)

        if len(nav_list) < page_size:
            break
        page += 1
        time.sleep(0.2)

    return result


def fetch_index_history(secid, start_date, end_date):
    """获取指数历史K线 {date_str: close}"""
    beg = start_date.replace("-", "")
    end = end_date.replace("-", "")
    url = (
        f"https://push2his.eastmoney.com/api/qt/stock/kline/get?"
        f"secid={secid}&fields1=f1,f2,f3"
        f"&fields2=f51,f52,f53,f54,f55,f56"
        f"&klt=101&fqt=0&beg={beg}&end={end}"
    )
    text = _get(url)
    result = {}
    if text:
        data = json.loads(text)
        inner = data.get("data") or {}
        for line in inner.get("klines") or []:
            parts = line.split(",")
            if len(parts) >= 3:
                result[parts[0]] = float(parts[2])  # close price
    if not result:
        # 东财 push2his 整体故障时走备源（新浪 A 股 / 腾讯纳指）
        try:
            from fetch_fund import fetch_index_daily_fallback
            result = fetch_index_daily_fallback(secid, start_date, end_date)
            if result:
                print(f"  [备源] {secid} 经新浪/腾讯取得 {len(result)} 条")
        except Exception:
            pass
    return result


def get_trading_days(nav_data_dict):
    """从基金净值数据中提取交易日列表"""
    all_dates = set()
    for navs in nav_data_dict.values():
        all_dates.update(navs.keys())
    return sorted(all_dates)


class Simulator:
    """历史回测模拟器"""

    def __init__(self, start_date, end_date, budget, funds=None, sim_id=None, verbose=True,
                 db=None, strategy_version="v2.0", engine_mode=False, rules_override=None):
        self.start_date = start_date
        self.end_date = end_date
        self.budget = budget
        self.cash = budget
        self.positions = {}  # code -> {shares, cost_nav, name}
        self.trades = []
        self.daily_records = []
        self.fund_names = funds or DEFAULT_FUNDS
        self.fund_navs = {}  # code -> {date: nav}
        self.index_data = {}  # secid -> {date: close}
        # 历史新闻缓存（按月×赛道），引擎回测合成新闻时优先使用真实新闻
        try:
            from news_sentiment import load_news_cache
            self._news_cache = load_news_cache()
        except ImportError:
            self._news_cache = {}
        self.peak_value = budget
        self.sim_id = sim_id or datetime.now().strftime("sim-%Y%m%d-%H%M%S")
        self.sim_dir = SIM_DIR / self.sim_id
        self.diary_lines = []  # 日记内容
        self.verbose = verbose    # 是否在终端打印每日完整报告

        # Phase 2: --engine 让回测调 DecisionEngine.decide() 而不是内置规则
        self.engine_mode = engine_mode

        # 数据库集成
        self.db = db
        self.account_id = None
        self.strategy_version = strategy_version
        self.engine = None
        self.rules_override = rules_override  # 梦境实验室：变体规则直接喂引擎
        if self.db and DB_AVAILABLE:
            self.engine = DecisionEngine(self.db, 0, strategy_version,
                                         rules_override=rules_override)  # account_id later

    def load_data(self):
        """加载所有历史数据（已被实验室预注入时跳过网络）"""
        if self.fund_navs:
            print("📡 使用预注入的历史数据（实验室模式）")
            return
        print(f"📡 加载历史数据 ({self.start_date} ~ {self.end_date})...")

        # 加载基金净值（向前多拉 90 天作信号预热：RSI/MACD/突破从窗口首日即可用；
        # trading_days 仍限定窗口内，无未来函数）
        nav_start = (datetime.strptime(self.start_date, "%Y-%m-%d")
                     - timedelta(days=90)).strftime("%Y-%m-%d")
        for i, (code, name) in enumerate(self.fund_names.items()):
            print(f"  [{i+1}/{len(self.fund_names)}] {code} {name}...", end=" ", flush=True)
            navs = fetch_nav_history(code, nav_start, self.end_date)
            self.fund_navs[code] = navs
            print(f"{len(navs)} 条数据")
            time.sleep(0.3)

        # 加载指数数据（需要等待一下避免频率限制）
        print("  ⏳ 切换数据源...")
        time.sleep(2)
        for secid, name in BENCHMARKS.items():
            print(f"  📊 {name}...", end=" ", flush=True)
            data = fetch_index_history(secid, self.start_date, self.end_date)
            # 如果失败，等待更久再试
            if not data:
                time.sleep(3)
                data = fetch_index_history(secid, self.start_date, self.end_date)
            self.index_data[secid] = data
            print(f"{len(data)} 条数据")
            time.sleep(1)

    def get_trading_days(self):
        """获取交易日列表（限定回测窗口内；窗口前的净值只用于信号预热）"""
        return [d for d in get_trading_days(self.fund_navs)
                if self.start_date <= d <= self.end_date]

    def get_nav(self, code, date):
        """获取基金在某天的净值"""
        navs = self.fund_navs.get(code, {})
        if date in navs:
            return navs[date]
        # 如果当天没有数据，找最近的
        dates_before = [d for d in sorted(navs.keys()) if d <= date]
        if dates_before:
            return navs[dates_before[-1]]
        return None

    def get_prev_nav(self, code, date, trading_days):
        """获取基金在前一个交易日的净值"""
        idx = trading_days.index(date) if date in trading_days else -1
        if idx > 0:
            return self.get_nav(code, trading_days[idx - 1])
        return None

    def get_portfolio_value(self, date):
        """计算组合总市值"""
        total = self.cash
        for code, pos in self.positions.items():
            nav = self.get_nav(code, date)
            if nav:
                total += nav * pos["shares"]
        return total

    def get_position_weight(self, code, date, total_value):
        """计算某只基金的仓位占比"""
        if code not in self.positions or total_value == 0:
            return 0
        nav = self.get_nav(code, date)
        if not nav:
            return 0
        return (nav * self.positions[code]["shares"]) / total_value

    def get_recent_return(self, code, date, trading_days, lookback=5):
        """计算近N天收益率"""
        idx = trading_days.index(date) if date in trading_days else -1
        if idx < lookback:
            return 0
        current_nav = self.get_nav(code, date)
        prev_nav = self.get_nav(code, trading_days[idx - lookback])
        if current_nav and prev_nav and prev_nav > 0:
            return (current_nav - prev_nav) / prev_nav
        return 0

    def get_fund_sector(self, code):
        """判断基金所属赛道"""
        name = self.fund_names.get(code, "")
        for sector, keywords in SECTOR_KEYWORDS.items():
            for kw in keywords:
                if kw in name:
                    return sector
        return "其他"

    def get_sector_weight(self, sector, date, total_value):
        """计算某赛道的总仓位"""
        if total_value == 0:
            return 0
        sector_value = 0
        for code, pos in self.positions.items():
            if self.get_fund_sector(code) == sector:
                nav = self.get_nav(code, date)
                if nav:
                    sector_value += nav * pos["shares"]
        return sector_value / total_value

    def get_market_regime(self, date, trading_days):
        """判断大盘环境：牛市/震荡/熊市"""
        # 用沪深300判断
        idx = trading_days.index(date) if date in trading_days else -1
        if idx < 20:
            return "震荡"

        # 获取沪深300数据
        benchmark_data = self.index_data.get("1.000300", {})
        if not benchmark_data:
            return "震荡"

        current = benchmark_data.get(date)
        if not current:
            # 找最近的
            dates_before = [d for d in sorted(benchmark_data.keys()) if d <= date]
            if dates_before:
                current = benchmark_data[dates_before[-1]]
            else:
                return "震荡"

        # 近20天涨跌
        prev_20 = benchmark_data.get(trading_days[idx - 20])
        if not prev_20 or prev_20 == 0:
            return "震荡"
        change_20 = (current - prev_20) / prev_20

        # 近5天涨跌
        prev_5 = benchmark_data.get(trading_days[idx - 5])
        change_5 = (current - prev_5) / prev_5 if prev_5 else 0

        if change_20 > 0.05:
            return "牛市"
        elif change_20 < -0.10:
            return "熊市"
        else:
            return "震荡"

    def check_buy_allowed(self, code, date, trading_days, total_value):
        """买入前检查，返回 (允许, 原因)"""
        # [1] 现金储备检查
        cash_ratio = self.cash / total_value if total_value > 0 else 0
        if cash_ratio < 0.10:
            return False, "现金不足10%"

        # [2] 单只基金仓位检查（目标25%上限）
        current_weight = self.get_position_weight(code, date, total_value)
        if current_weight >= 0.25:
            return False, f"单只仓位已达{current_weight*100:.0f}%"

        # [3] 赛道集中度检查
        sector = self.get_fund_sector(code)
        sector_limit = SECTOR_LIMITS.get(sector, 0.30)
        sector_weight = self.get_sector_weight(sector, date, total_value)
        if sector_weight >= sector_limit:
            return False, f"{sector}赛道已达{sector_weight*100:.0f}%（上限{sector_limit*100:.0f}%）"

        # [4] 追高检查（近5天涨幅>10%不买）
        recent_ret = self.get_recent_return(code, date, trading_days, 5)
        if recent_ret > 0.10:
            return False, f"近5天涨{recent_ret*100:.1f}%，追高风险"

        # [5] 大盘环境检查
        regime = self.get_market_regime(date, trading_days)
        if regime == "熊市":
            # 熊市只能加仓已持有基金，不能新建仓
            if code not in self.positions:
                return False, "熊市禁止新建仓"
        elif regime == "震荡" and code not in self.positions:
            # 震荡市新建仓需要更多现金
            if cash_ratio < 0.20:
                return False, f"震荡市新建仓需现金>20%，当前{cash_ratio*100:.0f}%"

        # [6] 趋势检查（连续5天跌暂缓）
        idx = trading_days.index(date) if date in trading_days else -1
        if idx >= 5:
            consecutive_drops = 0
            for i in range(5):
                today_nav = self.get_nav(code, trading_days[idx - i])
                prev_nav = self.get_nav(code, trading_days[idx - i - 1])
                if today_nav and prev_nav and today_nav < prev_nav:
                    consecutive_drops += 1
            if consecutive_drops >= 5:
                return False, "连续5天下跌，暂缓买入"

        return True, "通过"

    def execute_buy(self, code, date, amount, reason="", rule_name=None,
                    checks_passed=None, checks_failed=None, decision_context=None):
        """执行买入"""
        nav = self.get_nav(code, date)
        if not nav or nav <= 0 or amount <= 0 or amount > self.cash:
            return False

        shares = amount / nav
        name = self.fund_names.get(code, code)

        if code in self.positions:
            old = self.positions[code]
            total_shares = old["shares"] + shares
            new_cost = (old["cost_nav"] * old["shares"] + nav * shares) / total_shares
            self.positions[code] = {
                "shares": total_shares,
                "cost_nav": new_cost,
                "name": name,
                "buy_date": old.get("buy_date", date),  # 保留首次买入日期
            }
        else:
            self.positions[code] = {
                "shares": shares,
                "cost_nav": nav,
                "name": name,
                "buy_date": date,  # 记录首次买入日期
            }

        self.cash -= amount
        trade_record = {
            "date": date,
            "code": code,
            "name": name,
            "action": "buy",
            "amount": amount,
            "nav": nav,
            "shares": shares,
            "reason": reason,
        }
        self.trades.append(trade_record)

        # 写入数据库
        if self.db and self.account_id:
            trade_id = self.db.add_trade(
                self.account_id, date, code, name, "buy", amount, nav, shares,
                rule_name=rule_name,
                rule_version=self.strategy_version,
                decision_context=decision_context,
                reason=reason,
                checks_passed=checks_passed,
                checks_failed=checks_failed
            )
            # 更新持仓到 DB
            sector = self.get_fund_sector(code)
            pos = self.positions[code]
            self.db.set_position(
                self.account_id, code, name, pos["shares"], pos["cost_nav"],
                buy_date=pos.get("buy_date"), sector=sector, note=reason
            )

        return True

    def execute_sell(self, code, date, shares, reason="", rule_name=None,
                     decision_context=None):
        """执行卖出"""
        if code not in self.positions or shares <= 0:
            return False

        nav = self.get_nav(code, date)
        if not nav:
            return False

        pos = self.positions[code]
        actual_shares = min(shares, pos["shares"])
        amount = actual_shares * nav

        remaining = pos["shares"] - actual_shares
        if remaining < 0.01:
            del self.positions[code]
        else:
            self.positions[code]["shares"] = remaining

        self.cash += amount
        profit_pct = (nav - pos["cost_nav"]) / pos["cost_nav"] * 100
        outcome = "win" if profit_pct > 0 else "loss"

        trade_record = {
            "date": date,
            "code": code,
            "name": pos["name"],
            "action": "sell",
            "amount": amount,
            "nav": nav,
            "shares": actual_shares,
            "reason": reason,
            "profit_pct": profit_pct,
        }
        self.trades.append(trade_record)

        # 写入数据库
        if self.db and self.account_id:
            self.db.add_trade(
                self.account_id, date, code, pos["name"], "sell", amount, nav, actual_shares,
                rule_name=rule_name,
                rule_version=self.strategy_version,
                decision_context=decision_context,
                reason=reason,
                profit_pct=profit_pct,
                outcome=outcome
            )
            # 更新持仓
            if code in self.positions:
                sector = self.get_fund_sector(code)
                p = self.positions[code]
                self.db.set_position(
                    self.account_id, code, p["name"], p["shares"], p["cost_nav"],
                    buy_date=p.get("buy_date"), sector=sector
                )
            else:
                self.db.remove_position(self.account_id, code)

        return True

    # ==================== Phase 2: 引擎驱动回测 ====================

    def _synthesize_news_sentiment(self, code, name, funds, date, trading_days, idx_today):
        """合成新闻情绪（回测用）：用市场数据代理新闻情绪。

        逻辑：新闻情绪本质上反映在价格里——
        - 板块大涨 +5% 大概率有利好新闻
        - 板块大跌 -5% 大概率有利空新闻
        - 大盘强势时整体情绪偏乐观

        优先级：真实历史新闻缓存（data/news_cache.json）→ 价格代理合成 → 中性。
        缓存命中时用真实新闻，未覆盖的月份/赛道才回退到价格代理。
        """
        try:
            from news_sentiment import classify_news_sentiment, cached_news_sentiment
        except ImportError:
            return {"score": 0, "label": "中性"}

        # 0. 优先用真实历史新闻缓存（按月×赛道）
        sector = _infer_sector_local(name)
        cached = cached_news_sentiment(date, sector=sector, cache=self._news_cache)
        if cached is not None:
            return cached

        # 1. 该基金当日涨跌 → 代理该基金相关新闻
        fund = funds.get(code, {})
        day_r = fund.get("day_return", 0.0)
        d5 = fund.get("fund_5d_return", 0.0)

        # 2. 合成新闻条目（基于价格行为）
        synthetic_items = []

        # 当日大涨/大跌 → 模拟新闻
        if day_r > 0.03:
            synthetic_items.append({"title": f"{name} 大涨 {day_r*100:.1f}%，市场看好", "summary": "资金流入"})
        elif day_r > 0.01:
            synthetic_items.append({"title": f"{name} 小幅上涨", "summary": ""})
        elif day_r < -0.03:
            synthetic_items.append({"title": f"{name} 大跌 {day_r*100:.1f}%，市场担忧", "summary": "风险事件"})
        elif day_r < -0.01:
            synthetic_items.append({"title": f"{name} 小幅回调", "summary": ""})

        # 近5天趋势 → 模拟持续新闻
        if d5 > 0.05:
            synthetic_items.append({"title": f"{name} 近5日强势上涨 {d5*100:.1f}%", "summary": "利好催化"})
        elif d5 < -0.05:
            synthetic_items.append({"title": f"{name} 近5日持续下跌 {d5*100:.1f}%", "summary": "利空压制"})

        # 3. 大盘情绪加成
        hs300_data = self.index_data.get("1.000300", {}) or {}
        hs300_dates = sorted(d for d in hs300_data.keys() if d <= date)
        if len(hs300_dates) >= 6:
            hs300_now = hs300_data[hs300_dates[-1]]
            hs300_5d = hs300_data[hs300_dates[-6]]
            hs300_5d_ret = (hs300_now - hs300_5d) / hs300_5d if hs300_5d else 0
            if hs300_5d_ret > 0.02:
                synthetic_items.append({"title": "大盘强势，市场情绪乐观", "summary": "资金流入"})
            elif hs300_5d_ret < -0.02:
                synthetic_items.append({"title": "大盘走弱，市场情绪谨慎", "summary": "风险"})

        if not synthetic_items:
            return {"score": 0, "label": "中性"}

        # 4. 用 news_sentiment 模块打分（sector 已在函数开头推断）
        return classify_news_sentiment(synthetic_items, sector=sector)

    def _build_market_data_for_engine(self, date, trading_days):
        """构造 DecisionEngine.decide() 期望的 market_data 字典。

        关键约束（无未来函数）：只使用 date 当天及之前的 NAV 数据。
        """
        # HS300 5d / 20d 回报（用 1.000300 指数数据，已 load_data 拉到）
        hs300_data = self.index_data.get("1.000300", {}) or {}
        hs300_dates = sorted(d for d in hs300_data.keys() if d <= date)
        if len(hs300_dates) >= 21:
            latest = hs300_data[hs300_dates[-1]]
            d5 = hs300_data[hs300_dates[-6]]
            d20 = hs300_data[hs300_dates[-21]]
            hs300_5d = (latest - d5) / d5 if d5 else 0.0
            hs300_20d = (latest - d20) / d20 if d20 else 0.0
        else:
            hs300_5d, hs300_20d = None, None

        funds = {}
        idx_today = trading_days.index(date) if date in trading_days else -1
        for code, name in self.fund_names.items():
            current_nav = self.get_nav(code, date)
            if current_nav is None:
                continue
            # 各历史回报
            def _nav_n_days_ago(n):
                if idx_today >= n:
                    return self.get_nav(code, trading_days[idx_today - n])
                return None

            prev_nav = _nav_n_days_ago(1)
            nav3 = _nav_n_days_ago(3)
            nav5 = _nav_n_days_ago(5)
            nav20 = _nav_n_days_ago(20)

            day_return = ((current_nav - prev_nav) / prev_nav) if prev_nav else 0.0
            fund_3d   = ((current_nav - nav3)  / nav3)  if nav3  else 0.0
            fund_5d   = ((current_nav - nav5)  / nav5)  if nav5  else 0.0
            fund_20d  = ((current_nav - nav20) / nav20) if nav20 else 0.0

            # 20 日最高（不含未来）
            navs_window = []
            for j in range(max(0, idx_today - 19), idx_today + 1):
                v = self.get_nav(code, trading_days[j])
                if v is not None:
                    navs_window.append(v)
            high_20d = max(navs_window) if navs_window else current_nav

            sector = _infer_sector_local(name)
            funds[code] = {
                "name": name,
                "current_nav": current_nav,
                "day_return": day_return,
                "fund_3d_return": fund_3d,
                "fund_5d_return": fund_5d,
                "fund_20d_return": fund_20d,
                "high_20d": high_20d,
                "sector": sector,
            }
            # P6: 技术信号（只用 ≤date 的净值，含窗口前预热数据，无未来函数）
            try:
                import signals as _sig
                nav_dict = self.fund_navs.get(code) or {}
                series = [nav_dict[d] for d in sorted(nav_dict) if d <= date]
                if len(series) >= 15:
                    funds[code]["signals"] = _sig.attach_signals(series[-60:])
            except ImportError:
                pass

        # P5 趋势状态：HS300/NDX 200日线（只用 ≤date 的收盘，无未来函数；
        # 数据不足 200 天时为 None → 引擎自动跳过趋势规则）
        index_trend = {}
        try:
            import signals as _signals
            for secid, key in (("1.000300", "HS300"), ("100.NDX", "NDX")):
                data = self.index_data.get(secid) or {}
                closes = [data[d] for d in sorted(data) if d <= date]
                if len(closes) >= 200:
                    st = _signals.compute_ma_state(closes, window=200)
                    if st:
                        index_trend[key] = st
        except ImportError:
            pass
        for code in funds:
            ref = QDII_REF_INDEX.get(code)
            if ref:
                funds[code]["ref_index"] = ref

        return {
            "hs300_5d_return": hs300_5d,
            "hs300_20d_return": hs300_20d,
            "regime_hint": None,
            "funds": funds,
            "portfolio_peak_value": self.peak_value,
            "index_trend": index_trend,
        }

    def _positions_for_engine(self, date):
        """Convert internal positions dict to engine's positions list shape."""
        out = []
        for code, p in self.positions.items():
            buy_date = p.get("buy_date", date)
            try:
                hold_days = (
                    datetime.strptime(date, "%Y-%m-%d")
                    - datetime.strptime(buy_date, "%Y-%m-%d")
                ).days
            except Exception:
                hold_days = 0
            out.append({
                "code": code,
                "name": p.get("name", ""),
                "shares": p["shares"],
                "cost_nav": p["cost_nav"],
                "sector": _infer_sector_local(p.get("name", "")),
                "hold_days": hold_days,
            })
        return out

    def apply_rules_engine(self, date, trading_days, total_value):
        """Use DecisionEngine.decide() to drive trades for this day."""
        market_data = self._build_market_data_for_engine(date, trading_days)
        positions = self._positions_for_engine(date)
        # ── 合成新闻情绪注入（回测用）──
        idx_today = trading_days.index(date) if date in trading_days else -1
        news_by_fund = {}
        for code, fund in market_data["funds"].items():
            news_by_fund[code] = self._synthesize_news_sentiment(
                code, fund.get("name", ""), market_data["funds"],
                date, trading_days, idx_today
            )
        market_data["news"] = []  # 引擎需要这个键存在
        market_data["news_sentiment_by_fund"] = news_by_fund

        packet = self.engine.decide(
            date=date,
            market_data=market_data,
            positions=positions,
            cash=self.cash,
            total_value=total_value,
        )

        # 执行 actions（卖出优先于买入，保持现金充足）
        sells = [a for a in packet["actions"] if a["action"] == "sell"]
        buys  = [a for a in packet["actions"] if a["action"] == "buy"]

        for a in sells:
            shares = a.get("suggested_shares") or 0.0
            if shares > 0 and a["code"] in self.positions:
                self.execute_sell(
                    a["code"], date, shares,
                    reason=a.get("reason_zh", a.get("rule_label", "")),
                    rule_name=a.get("rule_id", "engine_sell"),
                )

        for a in buys:
            amount = a.get("suggested_amount") or 0.0
            if amount > 0 and amount <= self.cash:
                self.execute_buy(
                    a["code"], date, amount,
                    reason=a.get("reason_zh", a.get("rule_label", "")),
                    rule_name=a.get("rule_id", "engine_buy"),
                )

    # =================================================================

    def apply_rules(self, date, trading_days):
        """应用交易规则（决策树 v2.0）"""
        total_value = self.get_portfolio_value(date)
        if total_value <= 0:
            return

        # 更新峰值
        if total_value > self.peak_value:
            self.peak_value = total_value

        # Phase 2: 引擎驱动路径 — 把日终数据喂给 DecisionEngine，按其建议交易
        if self.engine_mode and self.engine and self.account_id:
            self.apply_rules_engine(date, trading_days, total_value)
            return

        cash_ratio = self.cash / total_value
        regime = self.get_market_regime(date, trading_days)

        # 根据大盘环境调整参数
        if regime == "牛市":
            max_single = 0.30
            stop_loss_pct = -0.15
        elif regime == "熊市":
            max_single = 0.15
            stop_loss_pct = -0.08
        else:
            max_single = 0.25
            stop_loss_pct = -0.12

        # === 1. 紧急止损（单日暴跌>7%或3天跌>10%） ===
        for code in list(self.positions.keys()):
            pos = self.positions[code]
            nav = self.get_nav(code, date)
            nav_prev = self.get_prev_nav(code, date, trading_days)
            if not nav or not nav_prev:
                continue

            # 单日跌幅
            day_change = (nav - nav_prev) / nav_prev
            if day_change <= -0.07:
                sell_shares = pos["shares"] * 0.5
                self.execute_sell(code, date, sell_shares, f"紧急止损: 单日跌{day_change*100:.1f}%",
                                  rule_name="紧急止损")
                continue

            # 3天累计跌幅
            idx = trading_days.index(date)
            if idx >= 3:
                nav_3d = self.get_nav(code, trading_days[idx - 3])
                if nav_3d:
                    change_3d = (nav - nav_3d) / nav_3d
                    if change_3d <= -0.10:
                        sell_shares = pos["shares"] * 0.5
                        self.execute_sell(code, date, sell_shares, f"紧急止损: 3日跌{change_3d*100:.1f}%",
                                          rule_name="紧急止损")

        # === 2. 绝对止损（亏损>20%清仓） ===
        for code in list(self.positions.keys()):
            pos = self.positions[code]
            nav = self.get_nav(code, date)
            if not nav:
                continue
            loss_pct = (nav - pos["cost_nav"]) / pos["cost_nav"]
            if loss_pct <= -0.20:
                self.execute_sell(code, date, pos["shares"], f"绝对止损: 亏损{loss_pct*100:.1f}%",
                                  rule_name="绝对止损")

        # === 3. 成本止损（按持有时间分档） ===
        for code in list(self.positions.keys()):
            if code not in self.positions:
                continue  # 可能已被上面清掉
            pos = self.positions[code]
            nav = self.get_nav(code, date)
            if not nav:
                continue
            loss_pct = (nav - pos["cost_nav"]) / pos["cost_nav"]

            # 简单估计持有天数（用买入日期）
            buy_date = pos.get("buy_date", "")
            if buy_date:
                try:
                    hold_days = (datetime.strptime(date, "%Y-%m-%d") - datetime.strptime(buy_date, "%Y-%m-%d")).days
                except:
                    hold_days = 30
            else:
                hold_days = 30

            if hold_days < 30 and loss_pct <= -0.08:
                sell_shares = pos["shares"] * 0.5
                self.execute_sell(code, date, sell_shares, f"短期止损: 持有{hold_days}天亏{loss_pct*100:.1f}%",
                                  rule_name="短期成本止损")
            elif 30 <= hold_days <= 90 and loss_pct <= -0.12:
                sell_shares = pos["shares"] * 0.5
                self.execute_sell(code, date, sell_shares, f"中期止损: 持有{hold_days}天亏{loss_pct*100:.1f}%",
                                  rule_name="中期成本止损")

        # === 4. 分批止盈 ===
        for code in list(self.positions.keys()):
            if code not in self.positions:
                continue
            pos = self.positions[code]
            nav = self.get_nav(code, date)
            if not nav:
                continue
            profit_pct = (nav - pos["cost_nav"]) / pos["cost_nav"]

            if profit_pct >= 0.50:
                # 盈利50%+清仓
                self.execute_sell(code, date, pos["shares"], f"目标止盈: 盈利{profit_pct*100:.1f}%",
                                  rule_name="止盈50%")
            elif profit_pct >= 0.40:
                # 盈利40%+再卖25%
                sell_shares = pos["shares"] * 0.25
                self.execute_sell(code, date, sell_shares, f"分批止盈40%: 盈利{profit_pct*100:.1f}%",
                                  rule_name="止盈40%")
            elif profit_pct >= 0.30:
                # 盈利30%+再卖25%
                sell_shares = pos["shares"] * 0.25
                self.execute_sell(code, date, sell_shares, f"分批止盈30%: 盈利{profit_pct*100:.1f}%",
                                  rule_name="止盈30%")
            elif profit_pct >= 0.20:
                # 盈利20%+卖25%
                sell_shares = pos["shares"] * 0.25
                self.execute_sell(code, date, sell_shares, f"分批止盈20%: 盈利{profit_pct*100:.1f}%",
                                  rule_name="止盈20%")

        # === 5. 回撤保护 ===
        drawdown = (total_value - self.peak_value) / self.peak_value
        if drawdown <= -0.12 and cash_ratio < 0.35:
            target_cash = total_value * 0.35
            cash_needed = target_cash - self.cash
            if cash_needed > 0:
                # 卖出亏损最大的基金
                worst_code = None
                worst_loss = 0
                for code in self.positions:
                    nav = self.get_nav(code, date)
                    if nav:
                        loss = (nav - self.positions[code]["cost_nav"]) / self.positions[code]["cost_nav"]
                        if loss < worst_loss:
                            worst_loss = loss
                            worst_code = code
                if worst_code and worst_loss < 0:
                    sell_amount = min(cash_needed, self.get_position_value(worst_code, date))
                    if sell_amount > 0:
                        nav = self.get_nav(worst_code, date)
                        self.execute_sell(worst_code, date, sell_amount / nav, f"回撤保护: {drawdown*100:.1f}%",
                                          rule_name="回撤保护")

        # === 6. 低吸（带前置检查） ===
        if regime != "熊市":  # 熊市不低吸
            for code in self.fund_names:
                # 前置检查
                allowed, reason = self.check_buy_allowed(code, date, trading_days, total_value)
                if not allowed:
                    continue

                nav_today = self.get_nav(code, date)
                nav_prev = self.get_prev_nav(code, date, trading_days)
                if nav_today and nav_prev and nav_prev > 0:
                    day_change = (nav_today - nav_prev) / nav_prev
                    if day_change <= -0.03:
                        # 跌3%低吸
                        buy_amount = total_value * 0.03
                        if day_change <= -0.05:
                            buy_amount = total_value * 0.05  # 跌5%加码
                        # 构建审计上下文
                        ctx = {
                            "market_regime": regime,
                            "fund_day_return": day_change,
                            "fund_5d_return": self.get_recent_return(code, date, trading_days, 5),
                            "cash_ratio": cash_ratio,
                        }
                        self.execute_buy(code, date, buy_amount, f"低吸: 日跌{day_change*100:.1f}%",
                                         rule_name="低吸", decision_context=ctx)

        # === 7. 周度再平衡（每5个交易日） ===
        day_idx = trading_days.index(date) if date in trading_days else 0
        if day_idx > 0 and day_idx % 5 == 0:
            self._rebalance_v2(date, trading_days, total_value)

        # === 8. 现金部署（不追高，选低估赛道） ===
        if self.cash > total_value * 0.25:
            # 选近5天跌得最多的赛道（逆向思维）
            worst_sector = None
            worst_return = 999
            for sector in SECTOR_KEYWORDS.keys():
                # 找该赛道的基金
                for code in self.fund_names:
                    if self.get_fund_sector(code) == sector:
                        ret = self.get_recent_return(code, date, trading_days, 5)
                        if ret < worst_return:
                            worst_return = ret
                            worst_sector = sector
                            worst_code = code

            if worst_sector and worst_code:
                allowed, reason = self.check_buy_allowed(worst_code, date, trading_days, total_value)
                if allowed:
                    deploy = total_value * 0.10
                    ctx = {
                        "market_regime": regime,
                        "worst_sector": worst_sector,
                        "worst_sector_5d_return": worst_return,
                        "cash_ratio": cash_ratio,
                    }
                    self.execute_buy(worst_code, date, deploy,
                                     f"现金部署: {worst_sector}赛道超跌{worst_return*100:.1f}%",
                                     rule_name="现金部署", decision_context=ctx)

    def _rebalance_v2(self, date, trading_days, total_value):
        """再平衡 v2（考虑赛道）"""
        # 单只基金超配检查
        for code in list(self.positions.keys()):
            weight = self.get_position_weight(code, date, total_value)
            if weight > 0.30:  # 单只上限30%
                excess = (weight - 0.25) * total_value
                nav = self.get_nav(code, date)
                if nav and excess > 100:
                    self.execute_sell(code, date, excess / nav, "再平衡: 单只超配减仓",
                                      rule_name="再平衡")

        # 赛道超配检查
        for sector, limit in SECTOR_LIMITS.items():
            sector_weight = self.get_sector_weight(sector, date, total_value)
            if sector_weight > limit * 1.2:  # 超配20%以上
                # 卖出该赛道仓位最大的基金
                max_code = None
                max_weight = 0
                for code in self.positions:
                    if self.get_fund_sector(code) == sector:
                        w = self.get_position_weight(code, date, total_value)
                        if w > max_weight:
                            max_weight = w
                            max_code = code
                if max_code:
                    sell_pct = (sector_weight - limit) / sector_weight * 0.5
                    nav = self.get_nav(max_code, date)
                    shares_to_sell = self.positions[max_code]["shares"] * sell_pct
                    if nav and shares_to_sell > 0.01:
                        self.execute_sell(max_code, date, shares_to_sell,
                                          f"再平衡: {sector}赛道超配减仓",
                                          rule_name="再平衡")

    def get_position_value(self, code, date):
        """计算单个持仓市值"""
        if code not in self.positions:
            return 0
        nav = self.get_nav(code, date)
        if not nav:
            return 0
        return nav * self.positions[code]["shares"]

    def _get_day_trades(self, date):
        """获取某天的交易记录"""
        return [t for t in self.trades if t["date"] == date]

    def _format_daily_diary(self, date, trading_days, day_idx, total_days):
        """格式化每日日记条目，返回字符串列表"""
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        try:
            dt = datetime.strptime(date, "%Y-%m-%d")
            weekday = weekdays[dt.weekday()]
        except Exception:
            weekday = ""

        total_value = self.get_portfolio_value(date)
        total_return = (total_value - self.budget) / self.budget * 100
        drawdown = (total_value - self.peak_value) / self.peak_value * 100 if self.peak_value > 0 else 0
        cash_ratio = self.cash / total_value * 100 if total_value > 0 else 100

        lines = []
        lines.append(f"\n{'━'*58}")
        lines.append(f"📅 第 {day_idx}/{total_days} 天 | {date} ({weekday})")
        lines.append(f"{'━'*58}")

        # 市场环境
        lines.append(f"\n📊 市场环境:")
        for code, name in self.fund_names.items():
            nav_today = self.get_nav(code, date)
            nav_prev = self.get_prev_nav(code, date, trading_days)
            if nav_today and nav_prev and nav_prev > 0:
                change = (nav_today - nav_prev) / nav_prev * 100
                arrow = "🔴" if change < -1 else ("🟢" if change > 1 else "⚪")
                lines.append(f"  {arrow} {code} {name:<12s}  ¥{nav_today:<8.4f} {change:+.2f}%")
            elif nav_today:
                lines.append(f"  ⚪ {code} {name:<12s}  ¥{nav_today:<8.4f}")

        # 持仓诊断
        lines.append(f"\n💼 持仓诊断:")
        for code, pos in self.positions.items():
            nav = self.get_nav(code, date)
            if not nav:
                continue
            value = nav * pos["shares"]
            pnl = (nav - pos["cost_nav"]) / pos["cost_nav"] * 100
            prev_nav = self.get_prev_nav(code, date, trading_days)
            if prev_nav and prev_nav > 0:
                day_pnl = value * (nav - prev_nav) / nav
            else:
                day_pnl = 0
            weight = value / total_value * 100 if total_value > 0 else 0
            lines.append(f"  {pos['name']:<16s}  {pos['shares']:>8.2f}份  ¥{value:>9,.2f}  持有{pnl:+.1f}%  日盈亏¥{day_pnl:+,.0f}  仓位{weight:.0f}%")

        if not self.positions:
            lines.append(f"  (空仓)")

        lines.append(f"  {'现金':<16s}  ¥{self.cash:>9,.2f}  ({cash_ratio:.0f}%)")
        lines.append(f"  {'总市值':<16s}  ¥{total_value:>9,.2f}  累计{total_return:+.2f}%  回撤{drawdown:.1f}%")

        # 今日决策
        today_trades = self._get_day_trades(date)
        if today_trades:
            lines.append(f"\n🔄 今日决策:")
            for t in today_trades:
                if t["action"] == "buy":
                    lines.append(f"  🟢 买入 {t['name']:<12s}  ¥{t['amount']:>8,.0f}  净值¥{t['nav']:.4f}  {t['shares']:.2f}份  理由: {t['reason']}")
                else:
                    profit_info = f"  {t.get('profit_pct', 0):+.1f}%" if 'profit_pct' in t else ""
                    lines.append(f"  🔴 卖出 {t['name']:<12s}  ¥{t['amount']:>8,.0f}  净值¥{t['nav']:.4f}  {t['shares']:.2f}份{profit_info}  理由: {t['reason']}")
        else:
            lines.append(f"\n🔄 今日决策: 无操作（持有观望）")

        return lines

    def _print_compact_line(self, date, trading_days, day_idx, total_days):
        """打印一行紧凑摘要到终端"""
        total_value = self.get_portfolio_value(date)
        total_return = (total_value - self.budget) / self.budget * 100
        drawdown = (total_value - self.peak_value) / self.peak_value * 100 if self.peak_value > 0 else 0

        today_trades = self._get_day_trades(date)
        if today_trades:
            actions = []
            for t in today_trades:
                if t["action"] == "buy":
                    actions.append(f"🟢买{t['code']}")
                else:
                    profit_info = f"{t.get('profit_pct', 0):+.0f}%" if 'profit_pct' in t else ""
                    actions.append(f"🔴卖{t['code']}{profit_info}")
            action_str = " | ".join(actions)
            print(f"  {date}  ¥{total_value:>10,.2f}  {total_return:+6.2f}%  回撤{drawdown:5.1f}%  ⚡{action_str}")
        else:
            # 找涨跌最大的基金
            changes = []
            for code in self.fund_names:
                nav_today = self.get_nav(code, date)
                nav_prev = self.get_prev_nav(code, date, trading_days)
                if nav_today and nav_prev and nav_prev > 0:
                    changes.append((nav_today - nav_prev) / nav_prev * 100)
            if changes:
                best = max(changes)
                worst = min(changes)
                emoji = "🟢" if best > 0 else "🔴"
                print(f"  {date}  ¥{total_value:>10,.2f}  {total_return:+6.2f}%  回撤{drawdown:5.1f}%  {emoji}{worst:+.1f}%~{best:+.1f}%")
            else:
                print(f"  {date}  ¥{total_value:>10,.2f}  {total_return:+6.2f}%  回撤{drawdown:5.1f}%")

    def run(self):
        """运行完整回测"""
        # 创建梦境账户
        if self.db and DB_AVAILABLE:
            account_name = f"梦境-{self.sim_id}"
            self.account_id = self.db.create_account(
                account_name, "dream", self.budget,
                strategy_version=self.strategy_version,
                sim_id=self.sim_id
            )
            # 更新 engine 的 account_id
            if self.engine:
                self.engine.account_id = self.account_id
            # 记录回测运行
            self.db.add_simulation(
                self.sim_id, self.account_id,
                self.start_date, self.end_date,
                self.budget, self.strategy_version,
                fund_pool=self.fund_names
            )

        self.load_data()
        trading_days = self.get_trading_days()

        if not trading_days:
            print("❌ 未找到交易日数据")
            return

        total_days = len(trading_days)
        print(f"\n📅 交易日: {total_days} 天 ({trading_days[0]} ~ {trading_days[-1]})")
        print(f"💰 初始资金: ¥{self.budget:,.2f}")
        print(f"🎯 基金池: {', '.join(f'{n}({c})' for c, n in self.fund_names.items())}")
        print(f"📝 每日日记将保存到: {self.sim_dir}/diary.md")

        # 日记开头
        self.diary_lines.append(f"# 📓 梦境训练日记")
        self.diary_lines.append(f"**回测ID**: {self.sim_id}")
        self.diary_lines.append(f"**期间**: {trading_days[0]} ~ {trading_days[-1]}（{total_days} 个交易日）")
        self.diary_lines.append(f"**初始资金**: ¥{self.budget:,.2f}")
        self.diary_lines.append(f"**基金池**: {', '.join(f'{n}({c})' for c, n in self.fund_names.items())}")
        self.diary_lines.append(f"")

        # === Day 1: 初始建仓 ===
        first_day = trading_days[0]

        # 目标配置：等额分配，保留15%现金
        invest_amount = self.budget * 0.85
        per_fund = invest_amount / len(self.fund_names)

        # 日记
        self.diary_lines.append(f"{'━'*58}")
        self.diary_lines.append(f"📅 第 1/{total_days} 天 | {first_day} — 🚀 初始建仓")
        self.diary_lines.append(f"{'━'*58}")
        self.diary_lines.append(f"\n💰 部署资金: ¥{invest_amount:,.0f}（{invest_amount/self.budget*100:.0f}%），保留 ¥{self.budget - invest_amount:,.0f} 现金")
        self.diary_lines.append(f"\n🔄 建仓操作:")

        print(f"\n🚀 初始建仓:")
        for code, name in self.fund_names.items():
            nav = self.get_nav(code, first_day)
            shares = per_fund / nav if nav else 0
            self.execute_buy(code, first_day, per_fund, "初始建仓", rule_name="初始建仓")
            line = f"  🟢 买入 {name:<16s}  ¥{per_fund:>8,.0f}  净值¥{nav:.4f}  {shares:.2f}份"
            self.diary_lines.append(line)
            print(f"  {line.strip()}")

        # 记录首日快照
        total = self.get_portfolio_value(first_day)
        self.daily_records.append({
            "date": first_day,
            "total_value": total,
            "cash": self.cash,
            "positions_value": total - self.cash,
            "return_pct": (total - self.budget) / self.budget * 100,
        })

        summary = f"\n💼 建仓完成 | 总市值: ¥{total:,.2f} | 现金: ¥{self.cash:,.2f}"
        self.diary_lines.append(summary)
        print(summary)

        # === 后续交易日 ===
        if self.verbose:
            print(f"\n📊 每日完整报告（同时保存到 diary.md）:\n")
        else:
            print(f"\n📊 每日摘要（完整日记见 diary.md）:\n")
        for i, date in enumerate(trading_days[1:], 2):
            # 应用交易规则
            self.apply_rules(date, trading_days)

            # 记录日终快照
            total = self.get_portfolio_value(date)
            drawdown = (total - self.peak_value) / self.peak_value * 100 if self.peak_value > 0 else 0

            self.daily_records.append({
                "date": date,
                "total_value": total,
                "cash": self.cash,
                "positions_value": total - self.cash,
                "return_pct": (total - self.budget) / self.budget * 100,
                "drawdown": drawdown,
            })

            # 写入 DB 每日快照
            if self.db and self.account_id:
                regime = self.get_market_regime(date, trading_days)
                # 赛道占比
                sector_exp = {}
                for sector in SECTOR_KEYWORDS:
                    sw = self.get_sector_weight(sector, date, total)
                    if sw > 0.001:
                        sector_exp[sector] = round(sw, 4)
                self.db.add_snapshot(
                    self.account_id, date, total, self.cash,
                    total - self.cash,
                    (total - self.budget) / self.budget * 100,
                    drawdown, regime, sector_exp
                )
                # 同步更新账户现金
                self.db.update_account(self.account_id, cash=self.cash)

            # 写入日记
            diary_entries = self._format_daily_diary(date, trading_days, i, total_days)
            self.diary_lines.extend(diary_entries)

            # 终端输出
            if self.verbose:
                # verbose 模式：打印完整每日报告
                for line in diary_entries:
                    print(line)
            else:
                # 默认：只打印一行摘要
                self._print_compact_line(date, trading_days, i, total_days)

        print(f"\n{'━'*58}")
        print(f"✅ 回测完成！共 {total_days} 个交易日")
        print(f"📝 完整日记已保存: {self.sim_dir}/diary.md")
        print(f"{'━'*58}")
        self._save_results()

    def _save_results(self):
        """保存结果"""
        self.sim_dir.mkdir(parents=True, exist_ok=True)

        # 保存配置
        config = {
            "sim_id": self.sim_id,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "budget": self.budget,
            "funds": self.fund_names,
            "strategy_version": self.strategy_version,
            "created_at": datetime.now().isoformat(),
        }
        with open(self.sim_dir / "config.json", "w") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

        # 保存持仓
        with open(self.sim_dir / "portfolio.json", "w") as f:
            json.dump(self.positions, f, ensure_ascii=False, indent=2)

        # 保存交易记录
        with open(self.sim_dir / "trades.json", "w") as f:
            json.dump(self.trades, f, ensure_ascii=False, indent=2)

        # 保存每日快照
        with open(self.sim_dir / "daily.json", "w") as f:
            json.dump(self.daily_records, f, ensure_ascii=False, indent=2)

        # 保存日记
        if self.diary_lines:
            with open(self.sim_dir / "diary.md", "w", encoding="utf-8") as f:
                f.write("\n".join(self.diary_lines))

        # 更新 DB 回测记录
        if self.db and self.daily_records:
            final_value = self.daily_records[-1]["total_value"]
            total_return = (final_value - self.budget) / self.budget * 100

            # 最大回撤
            max_dd = 0
            peak = self.budget
            for rec in self.daily_records:
                if rec["total_value"] > peak:
                    peak = rec["total_value"]
                dd = (rec["total_value"] - peak) / peak * 100
                if dd < max_dd:
                    max_dd = dd

            # 胜率
            sell_trades = [t for t in self.trades if t["action"] == "sell"]
            win_trades = [t for t in sell_trades if t.get("profit_pct", 0) > 0]
            win_rate = len(win_trades) / len(sell_trades) if sell_trades else 0

            self.db.update_simulation(
                self.sim_id,
                total_return=total_return,
                max_drawdown=max_dd,
                win_rate=win_rate,
                total_trades=len(self.trades),
                diary_path=str(self.sim_dir / "diary.md"),
                status="completed"
            )
            print(f"\n📊 回测结果已保存到数据库")

    def generate_report(self):
        """生成回测报告"""
        if not self.daily_records:
            return "无回测数据"

        # 计算关键指标
        final_value = self.daily_records[-1]["total_value"]
        total_return = (final_value - self.budget) / self.budget * 100
        trading_days = len(self.daily_records)
        annual_return = total_return * (252 / trading_days) if trading_days > 0 else 0

        # 最大回撤
        max_drawdown = 0
        peak = self.budget
        for rec in self.daily_records:
            if rec["total_value"] > peak:
                peak = rec["total_value"]
            dd = (rec["total_value"] - peak) / peak * 100
            if dd < max_drawdown:
                max_drawdown = dd

        # 交易统计
        buy_trades = [t for t in self.trades if t["action"] == "buy"]
        sell_trades = [t for t in self.trades if t["action"] == "sell"]
        win_trades = [t for t in sell_trades if t.get("profit_pct", 0) > 0]
        win_rate = len(win_trades) / len(sell_trades) * 100 if sell_trades else 0

        # 最佳/最差交易
        best_trade = max(sell_trades, key=lambda t: t.get("profit_pct", 0)) if sell_trades else None
        worst_trade = min(sell_trades, key=lambda t: t.get("profit_pct", 0)) if sell_trades else None

        # 基准对比
        benchmark_returns = {}
        for secid, name in BENCHMARKS.items():
            idx_data = self.index_data.get(secid, {})
            if idx_data:
                dates = sorted(idx_data.keys())
                if len(dates) >= 2:
                    start_val = idx_data[dates[0]]
                    end_val = idx_data[dates[-1]]
                    benchmark_returns[name] = (end_val - start_val) / start_val * 100

        # 等权基准（买入所有基金等权持有）
        eq_return = 0
        eq_count = 0
        for code in self.fund_names:
            navs = self.fund_navs.get(code, {})
            dates = sorted(navs.keys())
            if len(dates) >= 2:
                eq_return += (navs[dates[-1]] - navs[dates[0]]) / navs[dates[0]] * 100
                eq_count += 1
        eq_return = eq_return / eq_count if eq_count > 0 else 0

        # 生成报告
        report = f"""# 🎮 梦境训练报告

**回测ID**: {self.sim_id}
**回测期间**: {self.start_date} 至 {self.end_date} ({trading_days} 个交易日)
**初始资金**: ¥{self.budget:,.2f}
**期末市值**: ¥{final_value:,.2f}
**基金池**: {len(self.fund_names)} 只基金

---

## 一、业绩概览

| 指标 | 模拟策略 | 沪深300 | 上证指数 | 等权持有 |
|------|---------|---------|---------|---------|
| 总收益 | **{total_return:+.2f}%** | {benchmark_returns.get('沪深300', 0):+.2f}% | {benchmark_returns.get('上证指数', 0):+.2f}% | {eq_return:+.2f}% |
| 绝对收益 | **¥{final_value - self.budget:+,.2f}** | — | — | — |
| 年化收益 | {annual_return:+.2f}% | — | — | — |
| 最大回撤 | {max_drawdown:.2f}% | — | — | — |

---

## 二、交易统计

| 指标 | 数值 |
|------|------|
| 总交易次数 | {len(self.trades)} |
| 买入次数 | {len(buy_trades)} |
| 卖出次数 | {len(sell_trades)} |
| 胜率 | {win_rate:.1f}% |
"""

        if best_trade:
            report += f"| 最佳交易 | {best_trade['name']} +{best_trade.get('profit_pct', 0):.1f}% |\n"
        if worst_trade:
            report += f"| 最差交易 | {worst_trade['name']} {worst_trade.get('profit_pct', 0):.1f}% |\n"

        report += f"\n---\n\n## 三、最终持仓\n\n"
        if self.positions:
            report += "| 基金 | 份额 | 成本净值 | 最新净值 | 持有收益 |\n"
            report += "|------|------|---------|---------|--------|\n"
            for code, pos in self.positions.items():
                nav = self.get_nav(code, self.end_date) or 0
                pnl = (nav - pos['cost_nav']) / pos['cost_nav'] * 100 if pos['cost_nav'] > 0 else 0
                report += f"| {pos['name']} | {pos['shares']:.2f} | {pos['cost_nav']:.4f} | {nav:.4f} | {pnl:+.1f}% |\n"
            report += f"\n**现金余额**: ¥{self.cash:,.2f}\n"
        else:
            report += "空仓\n"

        # 收益曲线（简化版）
        report += f"\n---\n\n## 四、收益曲线（采样）\n\n"
        report += "| 日期 | 市值 | 收益率 | 回撤 |\n"
        report += "|------|------|--------|------|\n"
        step = max(1, len(self.daily_records) // 15)
        for i in range(0, len(self.daily_records), step):
            rec = self.daily_records[i]
            dd = rec.get('drawdown', 0)
            report += f"| {rec['date']} | ¥{rec['total_value']:,.2f} | {rec['return_pct']:+.2f}% | {dd:.1f}% |\n"
        # 最后一天
        last = self.daily_records[-1]
        dd = last.get('drawdown', 0)
        report += f"| **{last['date']}** | **¥{last['total_value']:,.2f}** | **{last['return_pct']:+.2f}%** | **{dd:.1f}%** |\n"

        # 结论
        report += f"\n---\n\n## 五、结论\n\n"
        if total_return > benchmark_returns.get('沪深300', 0):
            verdict = "✅ **策略跑赢沪深300**，说明策略在该时间段有效"
        else:
            verdict = "⚠️ **策略跑输沪深300**，需要调整策略参数"

        report += f"""{verdict}

**策略特征**:
- 总收益 {total_return:+.2f}% vs 沪深300 {benchmark_returns.get('沪深300', 0):+.2f}%
- 最大回撤 {max_drawdown:.2f}%
- 交易 {len(self.trades)} 次，胜率 {win_rate:.1f}%

**改进方向**:
1. 如果回撤过大 → 加强止损纪律
2. 如果胜率过低 → 减少交易频率，提高入场标准
3. 如果跑输基准 → 检查基金池选择，考虑更换标的

---
⚠️ 历史回测不代表未来表现，仅供验证策略逻辑参考。
"""

        # 保存报告
        report_path = self.sim_dir / "report.md"
        with open(report_path, "w") as f:
            f.write(report)

        return report


def cmd_run(args):
    """运行回测"""
    funds = DEFAULT_FUNDS
    if args.funds:
        fund_codes = args.funds.split(",")
        funds = {code: DEFAULT_FUNDS.get(code, f"基金{code}") for code in fund_codes}

    # 数据库连接
    db = None
    if DB_AVAILABLE:
        db = Database()
        db.init_tables()

    strategy_version = getattr(args, 'strategy', None) or "v2.0"

    sim = Simulator(
        start_date=args.start,
        end_date=args.end,
        budget=args.budget,
        funds=funds,
        verbose=getattr(args, 'verbose', False),
        db=db,
        strategy_version=strategy_version,
        engine_mode=getattr(args, 'engine', False),
    )
    sim.run()
    report = sim.generate_report()
    print(f"\n{'='*60}")
    print(report)
    print(f"\n📁 结果保存在: {sim.sim_dir}")

    # 回测后分析 + 进化建议
    if db and sim.account_id:
        engine = DecisionEngine(db, sim.account_id, strategy_version)
        analysis = engine.analyze_performance()
        suggestions = engine.suggest_evolution(analysis)
        if suggestions:
            print(f"\n🧬 策略进化建议:")
            for s in suggestions:
                print(f"  - [{s['rule']}] {s['suggestion']}")

    if db:
        db.close()


def cmd_list(args):
    """列出所有回测"""
    # 优先从 DB 读取
    if DB_AVAILABLE:
        db = Database()
        sims = db.list_simulations()
        if sims:
            print(f"\n回测记录 (DB) ({len(sims)} 个):")
            print("-" * 80)
            for s in sims:
                status_emoji = "✅" if s["status"] == "completed" else "🔄"
                ret_str = f"{s['total_return']:+.2f}%" if s["total_return"] is not None else "运行中"
                print(f"  {status_emoji} {s['sim_id']}")
                print(f"    期间: {s['start_date']} ~ {s['end_date']}  资金: ¥{s['budget']:,.2f}  "
                      f"策略: {s.get('strategy_version', '?')}  收益: {ret_str}")
                if s.get("win_rate") is not None:
                    print(f"    交易: {s.get('total_trades', 0)}笔  胜率: {s['win_rate']:.1%}  "
                          f"回撤: {s['max_drawdown']:.1f}%")
                print()
            db.close()
            return
        db.close()

    # 回退到文件系统
    if not SIM_DIR.exists():
        print("暂无回测记录")
        return

    sims = sorted([d.name for d in SIM_DIR.iterdir() if d.is_dir()])
    if not sims:
        print("暂无回测记录")
        return

    print(f"\n回测记录 ({len(sims)} 个):")
    print("-" * 60)
    for sim_id in sims:
        config_file = SIM_DIR / sim_id / "config.json"
        if config_file.exists():
            with open(config_file) as f:
                config = json.load(f)
            print(f"  {sim_id}")
            print(f"    期间: {config['start_date']} ~ {config['end_date']}")
            print(f"    资金: ¥{config['budget']:,.2f}")
            print()


def cmd_report(args):
    """显示回测报告"""
    sim_id = args.sim_id
    sim_dir = SIM_DIR / sim_id

    if not sim_dir.exists():
        print(f"回测 {sim_id} 不存在")
        return

    report_file = sim_dir / "report.md"
    if report_file.exists():
        with open(report_file) as f:
            print(f.read())
    else:
        # 重新生成报告
        with open(sim_dir / "config.json") as f:
            config = json.load(f)

        sim = Simulator(
            start_date=config["start_date"],
            end_date=config["end_date"],
            budget=config["budget"],
            funds=config["funds"],
            sim_id=sim_id,
        )
        sim.sim_dir = sim_dir
        with open(sim_dir / "daily.json") as f:
            sim.daily_records = json.load(f)
        with open(sim_dir / "trades.json") as f:
            sim.trades = json.load(f)
        with open(sim_dir / "portfolio.json") as f:
            sim.positions = json.load(f)

        report = sim.generate_report()
        print(report)


def main():
    parser = argparse.ArgumentParser(description="梦境训练 - 历史回测模拟器")
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="运行回测")
    p_run.add_argument("--start", required=True, help="开始日期 YYYY-MM-DD")
    p_run.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD")
    p_run.add_argument("--budget", type=float, default=50000, help="初始资金（默认50000）")
    p_run.add_argument("--funds", help="基金代码，逗号分隔（默认使用预设基金池）")
    p_run.add_argument("--verbose", "-v", action="store_true", help="终端也打印完整每日报告（默认只打印摘要）")
    p_run.add_argument("--strategy", help="决策树版本（默认 v2.0）")
    p_run.add_argument(
        "--engine", action="store_true",
        help="使用 DecisionEngine.decide() 驱动回测（Phase 2 新增；旧路径保留兼容）",
    )

    sub.add_parser("list", help="列出所有回测")

    p_report = sub.add_parser("report", help="查看回测报告")
    p_report.add_argument("sim_id", help="回测ID")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    if args.command == "run":
        cmd_run(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "report":
        cmd_report(args)


if __name__ == "__main__":
    main()
