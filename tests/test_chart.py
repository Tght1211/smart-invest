"""chart.py 终端走势图 + 邮件 sparkline 测试。stdlib unittest，纯函数无网络。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import chart  # noqa: E402


class TestDownsample(unittest.TestCase):
    def test_longer_series_reduced_to_target(self):
        vals = list(range(241))
        out = chart.downsample(vals, 60)
        self.assertEqual(len(out), 60)

    def test_shorter_series_unchanged(self):
        vals = [1.0, 2.0, 3.0]
        self.assertEqual(chart.downsample(vals, 60), vals)

    def test_endpoints_preserved(self):
        vals = list(range(100))
        out = chart.downsample(vals, 10)
        self.assertEqual(out[0], vals[0])
        self.assertEqual(out[-1], vals[-1])


class TestRenderChart(unittest.TestCase):
    def test_height_rows_plus_axis(self):
        vals = [10, 12, 11, 15, 13, 14, 9, 10.5]
        out = chart.render_chart(vals, height=8, width=40)
        lines = out.split("\n")
        # height 行图体 + 1 行底轴
        self.assertEqual(len(lines), 9)

    def test_labels_contain_min_max(self):
        vals = [10.0, 20.0, 15.0]
        out = chart.render_chart(vals, height=5, width=20)
        self.assertIn("20.00", out)
        self.assertIn("10.00", out)

    def test_flat_series_no_crash(self):
        out = chart.render_chart([5.0] * 30, height=5, width=20)
        self.assertTrue(out)


class TestSparkHtml(unittest.TestCase):
    def test_up_is_red(self):
        html = chart.spark_html([10, 11, 12, 13], bars=4)
        self.assertIn("#E64340", html)
        self.assertNotIn("#09BB07", html)

    def test_down_is_green(self):
        html = chart.spark_html([13, 12, 11, 10], bars=4)
        self.assertIn("#09BB07", html)
        self.assertNotIn("#E64340", html)

    def test_bar_count(self):
        html = chart.spark_html(list(range(100)), bars=30)
        self.assertEqual(html.count("<td"), 30)


class TestParseTrend(unittest.TestCase):
    def test_parse_eastmoney_trends2(self):
        import fetch_fund
        data = {"data": {
            "name": "纳斯达克100",
            "preClose": 25929.66,
            "trends": ["2026-06-09 21:30,25800.5", "2026-06-09 21:31,25810.0"],
        }}
        out = fetch_fund._parse_trend(data)
        self.assertEqual(out["name"], "纳斯达克100")
        self.assertAlmostEqual(out["pre_close"], 25929.66)
        self.assertEqual(out["points"][0], ("2026-06-09 21:30", 25800.5))
        self.assertEqual(len(out["points"]), 2)

    def test_parse_empty_returns_none(self):
        import fetch_fund
        self.assertIsNone(fetch_fund._parse_trend({"data": None}))


class TestResolveChartTarget(unittest.TestCase):
    def test_us_index_name(self):
        import fetch_fund
        kind, secid, name = fetch_fund._resolve_chart_target("纳斯达克100")
        self.assertEqual((kind, secid), ("index", "100.NDX"))

    def test_us_alias(self):
        import fetch_fund
        kind, secid, name = fetch_fund._resolve_chart_target("ndx")
        self.assertEqual((kind, secid, name), ("index", "100.NDX", "纳斯达克100"))

    def test_a_share_index(self):
        import fetch_fund
        kind, secid, _ = fetch_fund._resolve_chart_target("沪深300")
        self.assertEqual((kind, secid), ("index", "1.000300"))

    def test_fund_code(self):
        import fetch_fund
        self.assertEqual(fetch_fund._resolve_chart_target("006479"),
                         ("fund", "006479", None))

    def test_unknown(self):
        import fetch_fund
        self.assertIsNone(fetch_fund._resolve_chart_target("瞎写的"))


class TestSparkDSL(unittest.TestCase):
    def test_markdown_spark_block_renders_bars(self):
        import send_email
        md = "\n".join([
            ":::spark",
            "纳指100 隔夜走势 | 25,678.82 -0.97%",
            "25800,25700,25600,25650,25678",
            ":::",
        ])
        html = send_email.markdown_to_html(md)
        self.assertIn("纳指100 隔夜走势", html)
        self.assertIn("-0.97%", html)
        # 跌 → 绿色 bar
        self.assertIn("#09BB07", html)

    def test_spark_lines_helper(self):
        import daily_report
        lines = daily_report.spark_lines("纳指100 隔夜走势", 25929.66,
                                         [25800.0, 25700.0, 25678.82])
        self.assertEqual(lines[0], ":::spark")
        self.assertIn("-0.97%", lines[1])     # (25678.82-25929.66)/25929.66 ≈ -0.97%
        self.assertIn("25800.00", lines[2])
        self.assertEqual(lines[3], ":::")


if __name__ == "__main__":
    unittest.main()
