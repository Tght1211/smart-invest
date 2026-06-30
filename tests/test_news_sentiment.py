#!/usr/bin/env python3
"""新闻情绪 + 历史新闻缓存单元测试（stdlib unittest，无网络）。"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from news_sentiment import (
    classify_news_sentiment,
    get_dynamic_low_buy_threshold,
    load_news_cache,
    cached_news_sentiment,
    _label_for_score,
)


class TestClassify(unittest.TestCase):
    def test_empty(self):
        r = classify_news_sentiment([])
        self.assertEqual(r["score"], 0)
        self.assertEqual(r["label"], "中性")

    def test_bullish(self):
        items = [
            {"title": "半导体获国产替代政策支持", "summary": "需求爆发"},
            {"title": "北向资金净买入，机构加仓", "summary": ""},
        ]
        r = classify_news_sentiment(items, sector="半导体")
        self.assertGreater(r["score"], 0)
        self.assertIn(r["label"], ("弱利好", "强利好"))
        self.assertGreater(r["bullish_count"], 0)

    def test_bearish(self):
        items = [{"title": "公司业绩下滑，遭制裁，资金流出", "summary": "暴雷风险"}]
        r = classify_news_sentiment(items)
        self.assertLess(r["score"], 0)
        self.assertIn(r["label"], ("弱利空", "强利空"))

    def test_label_boundaries(self):
        self.assertEqual(_label_for_score(2.0), "强利好")
        self.assertEqual(_label_for_score(0.5), "弱利好")
        self.assertEqual(_label_for_score(0.0), "中性")
        self.assertEqual(_label_for_score(-0.5), "弱利空")
        self.assertEqual(_label_for_score(-2.0), "强利空")


class TestDynamicThreshold(unittest.TestCase):
    def test_neutral_returns_base(self):
        self.assertAlmostEqual(
            get_dynamic_low_buy_threshold(0.0, 0.0, base_threshold=-0.03), -0.03)

    def test_strong_trend_and_good_news_loosens(self):
        t = get_dynamic_low_buy_threshold(0.6, 2.5, base_threshold=-0.03)
        self.assertGreater(t, -0.03)  # 放宽（更接近0）

    def test_weak_trend_bad_news_tightens(self):
        t = get_dynamic_low_buy_threshold(-0.6, -2.5, base_threshold=-0.03)
        self.assertLess(t, -0.03)  # 收紧（更负）

    def test_clamped(self):
        t_hi = get_dynamic_low_buy_threshold(1.0, 3.0, base_threshold=-0.03)
        t_lo = get_dynamic_low_buy_threshold(-1.0, -3.0, base_threshold=-0.03)
        self.assertLessEqual(t_hi, -0.03 + 0.02 + 1e-9)
        self.assertGreaterEqual(t_lo, -0.03 - 0.025 - 1e-9)


class TestNewsCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8")
        json.dump({
            "2025-06": {
                "科技": ["半导体获国产替代政策支持，需求爆发", "北向资金净买入科技股"],
                "_market": ["大盘强势，市场情绪乐观，资金流入"],
            },
            "2025-07": {
                "_market": ["美联储加息，市场担忧，资金流出"],
            },
        }, self.tmp, ensure_ascii=False)
        self.tmp.close()
        self.path = self.tmp.name

    def tearDown(self):
        os.unlink(self.path)

    def test_load_missing(self):
        self.assertEqual(load_news_cache("/no/such/cache.json"), {})

    def test_load_valid(self):
        cache = load_news_cache(self.path)
        self.assertIn("2025-06", cache)

    def test_month_miss_returns_none(self):
        self.assertIsNone(cached_news_sentiment("2026-01-15", sector="科技", cache_path=self.path))

    def test_sector_hit_blends_market(self):
        r = cached_news_sentiment("2025-06-10", sector="科技", cache_path=self.path)
        self.assertIsNotNone(r)
        self.assertEqual(r["source"], "cache")
        self.assertGreater(r["score"], 0)

    def test_sector_miss_uses_market_only(self):
        # 2025-07 无 科技 赛道，只有 _market（利空）
        r = cached_news_sentiment("2025-07-20", sector="科技", cache_path=self.path)
        self.assertIsNotNone(r)
        self.assertLess(r["score"], 0)

    def test_empty_month_returns_none(self):
        # 月份存在但 sector 和 _market 都没命中内容
        empty = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8")
        json.dump({"2025-08": {"医药": []}}, empty, ensure_ascii=False)
        empty.close()
        try:
            self.assertIsNone(
                cached_news_sentiment("2025-08-01", sector="科技", cache_path=empty.name))
        finally:
            os.unlink(empty.name)


if __name__ == "__main__":
    unittest.main()
