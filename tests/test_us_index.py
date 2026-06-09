"""US 指数抓取的纯逻辑测试（解析 + QDII 映射）。网络部分不在此测。stdlib unittest。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_fund  # noqa: E402


class TestUsIndexParse(unittest.TestCase):
    SAMPLE = {
        "data": {
            "diff": [
                {"f2": 19234.56, "f3": 1.58, "f4": 299.1, "f12": "NDX", "f14": "纳斯达克100"}
            ]
        }
    }

    def test_parse_ok(self):
        r = fetch_fund._parse_us_index(self.SAMPLE, "纳斯达克100")
        self.assertEqual(r["name"], "纳斯达克100")
        self.assertEqual(r["price"], 19234.56)
        self.assertEqual(r["pct"], 1.58)

    def test_parse_uses_fallback_name(self):
        data = {"data": {"diff": [{"f2": 1.0, "f3": 0.0}]}}
        r = fetch_fund._parse_us_index(data, "标普500")
        self.assertEqual(r["name"], "标普500")

    def test_parse_empty_returns_none(self):
        self.assertIsNone(fetch_fund._parse_us_index({"data": {"diff": []}}, "x"))
        self.assertIsNone(fetch_fund._parse_us_index({}, "x"))
        self.assertIsNone(fetch_fund._parse_us_index(None, "x"))


class TestQdiiMapping(unittest.TestCase):
    def test_ndx_secid(self):
        self.assertEqual(fetch_fund.US_INDICES["纳斯达克100"], "100.NDX")

    def test_006479_maps_to_ndx(self):
        self.assertEqual(fetch_fund.QDII_INDEX_MAP["006479"], "纳斯达克100")

    def test_qdii_signal_unmapped_returns_none(self):
        # 未登记的代码直接返回 None，不触发网络
        self.assertIsNone(fetch_fund.qdii_overnight_signal("999999"))

    def test_fetch_us_index_unknown_name_returns_none(self):
        self.assertIsNone(fetch_fund.fetch_us_index("不存在的指数"))


if __name__ == "__main__":
    unittest.main()
