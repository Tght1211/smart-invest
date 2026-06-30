#!/usr/bin/env python3
"""基金基本面 + 红旗清单测试（借鉴 jiafei 五层质量分析）。

纯函数 + 网络 mock，stdlib unittest。
Run: python3 -m unittest tests.test_fundamentals -v
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import fetch_fund as ff


class TestParsers(unittest.TestCase):
    def test_work_years(self):
        self.assertAlmostEqual(ff._parse_work_years("10年又343天"), 10.94, places=2)
        self.assertAlmostEqual(ff._parse_work_years("343天"), 0.94, places=2)
        self.assertEqual(ff._parse_work_years("5年"), 5.0)
        self.assertIsNone(ff._parse_work_years(""))
        self.assertIsNone(ff._parse_work_years(None))

    def test_pct_str(self):
        self.assertEqual(ff._pct_str("35.29%"), 35.29)
        self.assertEqual(ff._pct_str("-3.5%"), -3.5)
        self.assertEqual(ff._pct_str(12.0), 12.0)
        self.assertIsNone(ff._pct_str(""))
        self.assertIsNone(ff._pct_str(None))


class TestRedFlags(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(ff.evaluate_red_flags({}), [])
        self.assertEqual(ff.evaluate_red_flags(None), [])

    def test_clean_fund_no_flags(self):
        f = {"scale": 50, "inst_pct": 30, "manager": {"work_years": 8},
             "asset": {"bond": 4}, "top10_concentration": 40,
             "abilities": {"抗风险": 80, "稳定性": 85}, "scale_series": [3, 4, 5]}
        self.assertEqual(ff.evaluate_red_flags(f), [])

    def test_critical_flags(self):
        keys = {x["key"]: x["level"] for x in ff.evaluate_red_flags(
            {"scale": 1.2, "inst_pct": 95, "asset": {"bond": 130}})}
        self.assertEqual(keys["scale_tiny"], "critical")
        self.assertEqual(keys["inst_heavy"], "critical")
        self.assertEqual(keys["leverage"], "critical")
        self.assertTrue(ff.has_critical(ff.evaluate_red_flags({"scale": 1.0})))

    def test_warn_flags(self):
        flags = ff.evaluate_red_flags(
            {"scale": 8, "scale_mom": 80, "scale_series": [5, 4, 3],
             "manager": {"work_years": 2.0}, "top10_concentration": 72,
             "abilities": {"抗风险": 50}})
        keys = {x["key"] for x in flags}
        self.assertIn("scale_surge", keys)
        self.assertIn("scale_shrink", keys)
        self.assertIn("mgr_green", keys)
        self.assertIn("concentration", keys)
        self.assertIn("ability_low", keys)
        self.assertFalse(ff.has_critical(flags))  # 都是 warn

    def test_equity_vs_bond_scale_huge(self):
        big = {"scale": 600}
        self.assertTrue(any(x["key"] == "scale_huge"
                            for x in ff.evaluate_red_flags(big, equity=True)))
        # 债基不按权益规模口径，600亿 不算红旗
        self.assertFalse(any(x["key"] == "scale_huge"
                             for x in ff.evaluate_red_flags(big, equity=False)))


# 一个最小可解析的 pingzhongdata 片段（仅含我们解析的变量）
FAKE_PINGZHONG = (
    'var fS_name = "测试科技基金";'
    'var Data_fluctuationScale = {"categories":["a","b","c"],'
    '"series":[{"y":3.0,"mom":"5%"},{"y":4.0,"mom":"33%"},{"y":8.0,"mom":"100%"}]};'
    'var Data_holderStructure = {"series":[{"name":"机构持有比例","data":[10,20,30]},'
    '{"name":"个人持有比例","data":[90,80,70]}],"categories":["x","y","z"]};'
    'var Data_currentFundManager = [{"name":"张三","workTime":"6年又100天",'
    '"power":{"avr":"82.5"}}];'
    'var Data_assetAllocation = {"series":[{"name":"股票占净比","data":[90,92,94]},'
    '{"name":"债券占净比","data":[5,4,3]},{"name":"现金占净比","data":[5,4,3]}]};'
    'var Data_performanceEvaluation = {"avr":"80","categories":["选证能力","抗风险"],'
    '"data":[85.0,55.0]};'
    'var fund_Rate = "0.15";'
)
FAKE_HOLDINGS = "".join(f"<td>{p}%</td>" for p in
                        [9.9, 9.0, 8.5, 8.0, 7.5, 7.0, 6.5, 6.0, 5.5, 5.0])


class TestFetchFundamentals(unittest.TestCase):
    def _fake_get(self, url, headers=None, retries=2):
        if "pingzhongdata" in url:
            return FAKE_PINGZHONG
        if "FundArchivesDatas" in url:
            return FAKE_HOLDINGS
        return None

    def test_parse_all_fields(self):
        with mock.patch.object(ff, "_get", side_effect=self._fake_get):
            f = ff.fetch_fundamentals("000001")
        self.assertEqual(f["name"], "测试科技基金")
        self.assertEqual(f["scale"], 8.0)
        self.assertEqual(f["scale_mom"], 100.0)
        self.assertEqual(f["inst_pct"], 30)
        self.assertEqual(f["manager"]["name"], "张三")
        self.assertAlmostEqual(f["manager"]["work_years"], 6.27, places=1)
        self.assertEqual(f["manager"]["ability"], 82.5)
        self.assertEqual(f["asset"], {"stock": 94, "bond": 3, "cash": 3})
        self.assertEqual(f["abilities"]["抗风险"], 55.0)
        self.assertEqual(f["mgmt_rate"], 0.15)
        self.assertAlmostEqual(f["top10_concentration"], 72.9, places=1)

    def test_offline_returns_empty(self):
        with mock.patch.object(ff, "_get", return_value=None):
            self.assertEqual(ff.fetch_fundamentals("000001"), {})

    def test_red_flags_off_parsed(self):
        with mock.patch.object(ff, "_get", side_effect=self._fake_get):
            f = ff.fetch_fundamentals("000001")
        flags = ff.evaluate_red_flags(f)
        keys = {x["key"] for x in flags}
        self.assertIn("scale_surge", keys)        # mom 100% > 50
        self.assertIn("ability_low", keys)        # 抗风险 55 < 60
        self.assertIn("concentration", keys)      # 72.9% > 60
        self.assertFalse(ff.has_critical(flags))  # 规模8亿/机构30%/无杠杆


class TestDiscoverQualityGate(unittest.TestCase):
    def test_quality_drops_critical(self):
        cands = [
            {"code": "A", "name": "好基金", "w_6m": 30, "w_3m": 10, "w_1m": 5,
             "w_1w": 2, "w_1d": 1},
            {"code": "B", "name": "迷你基金", "w_6m": 40, "w_3m": 12, "w_1m": 6,
             "w_1w": 3, "w_1d": 1},
        ]
        def fake_rank(ft="gp", period="6n", top=60, otc_only=True):
            return cands if ft == "gp" else []

        def fake_fund(code):
            return {"scale": 50} if code == "A" else {"scale": 1.0}  # B 清盘风险

        with mock.patch.object(ff, "fetch_fund_rank", side_effect=fake_rank), \
             mock.patch.object(ff, "fetch_fundamentals", side_effect=fake_fund), \
             mock.patch.object(ff, "_infer_sector", return_value="科技"):
            picked = ff.discover_candidates(limit=5, quality=True)
        codes = {c["code"] for c in picked}
        self.assertIn("A", codes)
        self.assertNotIn("B", codes)        # critical 被剔除
        self.assertIn("red_flags", picked[0])


if __name__ == "__main__":
    unittest.main()
