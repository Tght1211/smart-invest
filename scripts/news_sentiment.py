#!/usr/bin/env python3
"""
新闻情绪分析 — Smart Invest Skill
给财经新闻打情绪分（利好/中性/利空），供决策引擎动态调整阈值。
纯 Python 3 标准库，无第三方依赖。
"""

import json
from pathlib import Path

# 历史新闻缓存（回测用，按月×赛道存标题，由 WebSearch 整理而来）
NEWS_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "news_cache.json"

# 利好关键词（权重）
BULLISH_KEYWORDS = {
    # 政策利好
    "利好": 2, "支持": 2, "扶持": 2, "补贴": 2, "减税": 2, "降准": 2, "降息": 2,
    "国产替代": 3, "自主可控": 3, "新质生产力": 2, "高质量发展": 1, "政策催化": 2,
    # 业绩利好
    "业绩大增": 2, "净利润增长": 2, "超预期": 2, "创新高": 2, "创历史新高": 2,
    "新高": 2, "高增长": 2, "暴增": 3, "翻倍": 2, "扭亏": 2, "业绩预喜": 2,
    # 资金流向
    "资金流入": 2, "北向资金净买入": 2, "净买入": 2, "机构加仓": 2, "加仓": 2,
    "增持": 2, "融资": 1, "资金涌入": 2,
    # 技术突破
    "突破": 2, "突破封锁": 3, "技术领先": 2, "全球首发": 2,
    # 行业催化 / 价格行为
    "需求爆发": 2, "订单激增": 2, "产能扩张": 1, "涨价": 1, "景气": 1, "复苏": 1,
    "涨停": 2, "暴涨": 2, "飙升": 2, "大涨": 2, "领涨": 1, "爆发": 2, "走强": 1,
    "反弹": 1, "拉升": 1, "攀升": 1, "看好": 1, "看多": 1, "红盘": 1, "连涨": 1,
    "强势": 1, "活跃": 1, "回暖": 1,
}

# 利空关键词（权重，负数）
BEARISH_KEYWORDS = {
    # 政策利空
    "利空": -2, "限制": -2, "禁止": -2, "制裁": -3, "关税": -2, "调查": -1,
    "监管收紧": -2, "反垄断": -2,
    # 业绩利空
    "业绩下滑": -2, "净利润下降": -2, "亏损": -2, "爆雷": -3, "暴雷": -3,
    "违约": -3, "退市": -3, "预计亏损": -2,
    # 资金流向
    "资金流出": -2, "北向资金净卖出": -2, "机构减仓": -2, "减仓": -2, "减持": -2,
    "套现": -2, "抛售": -2, "蒸发": -2,
    # 风险事件 / 价格行为
    "风险": -1, "危机": -2, "崩盘": -3, "暴跌": -2, "跌停": -2, "重挫": -2,
    "破产": -3, "倒闭": -3, "大跌": -2, "领跌": -2, "下跌": -1, "回调": -1,
    "走弱": -1, "跳水": -2, "看空": -2, "看跌": -1, "担忧": -1, "谨慎": -1,
    "泡沫": -2, "震荡": -1, "承压": -1, "压制": -1, "连跌": -1, "分化": -1,
}


def classify_news_sentiment(news_items, sector=None):
    """分析新闻列表的整体情绪 → {score, label, bullish_count, bearish_count}。

    score ∈ [-3, 3]：
      - 强利好 (2~3)：多条明确利好，如政策催化+业绩大增
      - 弱利好 (0.5~2)：有利好但不多，或利好力度一般
      - 中性 (-0.5~0.5)：无明显方向，或利好利空对冲
      - 弱利空 (-2~-0.5)：有利空但不严重
      - 强利空 (-3~-2)：重大利空，如制裁/暴雷

    sector 可选：按赛道匹配关键词（如"半导体"匹配"国产替代"权重更高）。
    """
    if not news_items:
        return {"score": 0, "label": "中性", "bullish_count": 0, "bearish_count": 0}

    total_score = 0
    bullish_count = 0
    bearish_count = 0

    for item in news_items:
        text = (item.get("title", "") + " " + item.get("summary", "")).lower()

        item_score = 0
        for kw, weight in BULLISH_KEYWORDS.items():
            if kw in text:
                item_score += weight
                bullish_count += 1

        for kw, weight in BEARISH_KEYWORDS.items():
            if kw in text:
                item_score += weight
                bearish_count += 1

        # 赛道加成：如果新闻与该赛道相关，权重 ×1.5
        if sector and sector in text:
            item_score *= 1.5

        total_score += item_score

    # 归一化到 [-3, 3]
    n = len(news_items)
    if n > 0:
        avg_score = total_score / n
        normalized = max(-3, min(3, avg_score * 1.5))  # 放大系数
    else:
        normalized = 0

    return {
        "score": round(normalized, 2),
        "label": _label_for_score(normalized),
        "bullish_count": bullish_count,
        "bearish_count": bearish_count,
    }


def _label_for_score(score):
    """情绪分 → 中文标签（强利好/弱利好/中性/弱利空/强利空）。"""
    if score >= 2:
        return "强利好"
    elif score >= 0.5:
        return "弱利好"
    elif score <= -2:
        return "强利空"
    elif score <= -0.5:
        return "弱利空"
    return "中性"


def load_news_cache(cache_path=None):
    """读取历史新闻缓存 JSON。文件缺失/损坏返回 {}，绝不抛异常。"""
    path = Path(cache_path) if cache_path else NEWS_CACHE_PATH
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, ValueError):
        return {}


def cached_news_sentiment(date, sector=None, cache=None, cache_path=None):
    """从历史新闻缓存取某月某赛道的情绪，供回测使用。

    缓存格式: {"YYYY-MM": {"<赛道>": ["标题1", "标题2", ...], "_market": [...]}}
    赛道命中 → 混合 赛道(0.7) + 大盘(0.3)；只有大盘 → 用大盘；都缺 → None。
    返回 None 表示该月无缓存，调用方应回退到合成/中性。

    标题统一过 classify_news_sentiment 打分，确保与实盘评分口径一致。
    """
    cache = cache if cache is not None else load_news_cache(cache_path)
    if not cache:
        return None
    month = str(date)[:7]  # "YYYY-MM-DD" → "YYYY-MM"
    month_data = cache.get(month)
    if not month_data:
        return None

    sector_heads = month_data.get(sector or "", []) if sector else []
    market_heads = month_data.get("_market", [])
    if not sector_heads and not market_heads:
        return None

    def _items(heads):
        return [({"title": h, "summary": ""} if isinstance(h, str) else h) for h in heads]

    sec_sent = classify_news_sentiment(_items(sector_heads), sector=sector) if sector_heads else None
    mkt_sent = classify_news_sentiment(_items(market_heads)) if market_heads else None

    if sec_sent and mkt_sent:
        score = 0.7 * sec_sent["score"] + 0.3 * mkt_sent["score"]
    elif sec_sent:
        score = sec_sent["score"]
    else:
        score = mkt_sent["score"]

    score = round(max(-3.0, min(3.0, score)), 2)
    return {"score": score, "label": _label_for_score(score),
            "source": "cache", "month": month, "sector": sector}


def get_dynamic_low_buy_threshold(trend_strength, news_sentiment, base_threshold=-0.03):
    """动态计算低吸阈值（替代写死的 -3%）。

    逻辑：
    - 强趋势 + 利好新闻 → 阈值放宽（如 -1.5% 就买）
    - 弱趋势 + 利空新闻 → 阈值收紧（如 -5% 才买）
    - 中性情况 → 保持基准

    Args:
        trend_strength: 趋势强度 ∈ [-1, 1]，正=上行，负=下行
        news_sentiment: 新闻情绪 score ∈ [-3, 3]
        base_threshold: 基准阈值（默认 -3%）

    Returns:
        动态阈值（如 -0.015 = -1.5%）
    """
    adjustment = 0

    # 趋势加成：强趋势时放宽阈值
    if trend_strength > 0.5:  # 强上行
        adjustment += 0.015  # 放宽 1.5%
    elif trend_strength > 0.2:  # 温和上行
        adjustment += 0.008  # 放宽 0.8%
    elif trend_strength < -0.5:  # 强下行
        adjustment -= 0.02  # 收紧 2%（更谨慎）

    # 新闻加成：利好时放宽，利空时收紧
    if news_sentiment >= 2:  # 强利好
        adjustment += 0.01  # 放宽 1%
    elif news_sentiment >= 0.5:  # 弱利好
        adjustment += 0.005  # 放宽 0.5%
    elif news_sentiment <= -2:  # 强利空
        adjustment -= 0.015  # 收紧 1.5%
    elif news_sentiment <= -0.5:  # 弱利空
        adjustment -= 0.008  # 收紧 0.8%

    # 限制调整幅度，避免极端
    adjustment = max(-0.025, min(0.02, adjustment))

    return base_threshold + adjustment


if __name__ == "__main__":
    # 测试
    test_news = [
        {"title": "半导体板块获国产替代政策催化", "summary": "国产芯片需求爆发"},
        {"title": "北向资金净买入科技股", "summary": "机构加仓半导体"},
    ]
    result = classify_news_sentiment(test_news, sector="半导体")
    print(f"情绪分析: {result}")

    threshold = get_dynamic_low_buy_threshold(
        trend_strength=0.6,
        news_sentiment=result["score"]
    )
    print(f"动态阈值: {threshold * 100:.2f}%")
