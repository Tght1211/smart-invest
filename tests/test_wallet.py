"""Tests for the wallet-ops features ported onto the baseline:
  relevant_news / portfolio_return_series / calibrate_costs (fetch_fund),
  email retry+outbox / operation-report trade-notify (send_email),
  card_wallet (daily_report), web_panel render.

Overlapping concepts already on baseline (cash debit/credit, T+1 pending,
snapshots) are covered by their own tests — not here. All stdlib unittest,
no network/SMTP (mocked). Run: python3 -m unittest tests.test_wallet -v
"""
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from tests._helpers import make_in_memory_db, add_test_account  # noqa: E402


# ===================== relevant_news =====================

class RelevantNewsTest(unittest.TestCase):
    def setUp(self):
        import fetch_fund
        self.ff = fetch_fund
        self.news = [
            {"title": "半导体板块大涨，集成电路领涨", "summary": "国产替代加速"},
            {"title": "白酒消费回暖", "summary": "茅台批价上行"},
            {"title": "央行宣布降准0.5个百分点", "summary": "释放流动性"},
        ]

    def test_theme_match_embedded_in_name(self):
        hits = self.ff.relevant_news(self.news, name="国联安半导体ETF联接A", sector="A股科技")
        self.assertIn("半导体板块大涨，集成电路领涨", hits)

    def test_sector_match(self):
        hits = self.ff.relevant_news(self.news, name="招商中证白酒指数A", sector="消费")
        self.assertEqual(hits, ["白酒消费回暖"])

    def test_no_match_falls_back_to_top(self):
        hits = self.ff.relevant_news(self.news, name="某宽基", sector="宽基", limit=2)
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0], self.news[0]["title"])

    def test_empty_news(self):
        self.assertEqual(self.ff.relevant_news([], name="x"), [])


# ===================== portfolio_return_series =====================

class PortfolioReturnSeriesTest(unittest.TestCase):
    def test_aligned_series(self):
        import fetch_fund
        holdings = [
            {"code": "A", "shares": 100, "cost_nav": 1.0, "buy_date": "2026-01-01"},
            {"code": "B", "shares": 50, "cost_nav": 2.0, "buy_date": "2026-01-01"},
        ]
        nav = {
            "A": [("2026-06-01", 1.0), ("2026-06-02", 1.1), ("2026-06-03", 1.2)],
            "B": [("2026-06-01", 2.0), ("2026-06-02", 2.0), ("2026-06-03", 2.2)],
        }
        with mock.patch.object(fetch_fund, "_load_portfolio", return_value=holdings), \
             mock.patch.object(fetch_fund, "fetch_nav_series",
                               side_effect=lambda code, days=30: nav[code]):
            series = fetch_fund.portfolio_return_series("主线", days=30)
        self.assertEqual([d for d, _ in series],
                         ["2026-06-01", "2026-06-02", "2026-06-03"])
        self.assertAlmostEqual(series[1][1], 0.05, places=6)
        self.assertAlmostEqual(series[2][1], 0.15, places=6)

    def test_excludes_today_buy(self):
        import fetch_fund
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        holdings = [
            {"code": "OLD", "shares": 100, "cost_nav": 1.0, "buy_date": "2026-01-01"},
            {"code": "NEW", "shares": 50, "cost_nav": 2.0, "buy_date": today},
        ]
        navs = {"OLD": [("2026-06-01", 1.0), ("2026-06-02", 1.1)]}
        fetched = []
        with mock.patch.object(fetch_fund, "_load_portfolio", return_value=holdings), \
             mock.patch.object(fetch_fund, "fetch_nav_series",
                               side_effect=lambda c, days=30: fetched.append(c) or navs[c]):
            fetch_fund.portfolio_return_series("主线", days=30)
        self.assertEqual(fetched, ["OLD"])   # 当天买入的 NEW 未参与


# ===================== email retry + outbox =====================

class OutboxRetryTest(unittest.TestCase):
    def setUp(self):
        import send_email
        self.se = send_email
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_outbox = send_email.OUTBOX_DIR
        send_email.OUTBOX_DIR = Path(self.tmp.name) / "outbox"
        self.cfg = {"enabled": True, "smtp": {
            "server": "smtp.x", "port": 465, "sender": "a@x.com",
            "password": "p", "receiver": ["b@x.com"], "use_ssl": True}}

    def tearDown(self):
        self.se.OUTBOX_DIR = self._orig_outbox
        self.tmp.cleanup()

    def _queued(self):
        d = self.se.OUTBOX_DIR
        return sorted(d.glob("*.json")) if d.exists() else []

    def test_send_failure_enqueues(self):
        with mock.patch.object(self.se, "load_config", return_value=self.cfg), \
             mock.patch.object(self.se, "_deliver", return_value=False), \
             redirect_stderr(io.StringIO()):
            ok = self.se.send_email("S1", "body", "<h>")
        self.assertFalse(ok)
        self.assertEqual(len(self._queued()), 1)

    def test_flush_sends_and_removes(self):
        self.se._enqueue_outbox("S", "body", "<h>", ["b@x.com"])
        with mock.patch.object(self.se, "_deliver", return_value=True), \
             redirect_stdout(io.StringIO()):
            n = self.se.flush_outbox(self.cfg)
        self.assertEqual(n, 1)
        self.assertEqual(self._queued(), [])

    def test_flush_keeps_on_failure(self):
        self.se._enqueue_outbox("S", "body", None, ["b@x.com"])
        with mock.patch.object(self.se, "_deliver", return_value=False):
            n = self.se.flush_outbox(self.cfg)
        self.assertEqual(n, 0)
        self.assertEqual(len(self._queued()), 1)

    def test_smtp_retry_then_success(self):
        attempts = {"n": 0}
        class FakeSrv:
            def login(self, *a): pass
            def sendmail(self, *a): pass
            def quit(self): pass
        def flaky(server, port, timeout=30):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise OSError("refused")
            return FakeSrv()
        msg = self.se._build_msg("s", "b", None, "a@x.com", ["b@x.com"])
        with mock.patch.object(self.se.smtplib, "SMTP_SSL", side_effect=flaky), \
             mock.patch.object(self.se.time, "sleep", lambda s: None):
            ok = self.se._smtp_send(self.cfg["smtp"], ["b@x.com"], msg, retries=3)
        self.assertTrue(ok)
        self.assertEqual(attempts["n"], 3)

    def test_smtp_all_fail_returns_false(self):
        def fail(*a, **k):
            raise OSError("down")
        msg = self.se._build_msg("s", "b", None, "a@x.com", ["b@x.com"])
        with mock.patch.object(self.se.smtplib, "SMTP_SSL", side_effect=fail), \
             mock.patch.object(self.se.time, "sleep", lambda s: None), \
             redirect_stderr(io.StringIO()):
            ok = self.se._smtp_send(self.cfg["smtp"], ["b@x.com"], msg, retries=3)
        self.assertFalse(ok)


# ===================== trade-notify operation report =====================

class TradeNotifyReportTest(unittest.TestCase):
    def test_report_contains_reason_news_wallet(self):
        import send_email
        cap = {}
        args = SimpleNamespace(
            action="buy", code="660011", name="农银中证500指数A",
            amount="2000", nav="2.1633", shares="924.5", note="低吸-中证500",
            reason="中证500近5日回调3%，未追高，分散A股敞口",
            news=["中证500今日回调，估值低位", "央行宣布降准"],
            wallet="总钱包 ¥33,500 ｜ 现金 ¥31,500")
        with mock.patch.object(send_email, "send_email",
                               side_effect=lambda s, b, h: cap.update(subject=s, body=b, html=h)):
            send_email.cmd_trade_notify(args)
        self.assertIn("操作报告", cap["subject"])
        self.assertIn("操作依据", cap["body"])
        self.assertIn("分散A股敞口", cap["body"])
        self.assertIn("降准", cap["body"])
        self.assertIn("操作后钱包", cap["body"])
        self.assertIn("<", cap["html"])

    def test_report_falls_back_to_note(self):
        import send_email
        cap = {}
        with mock.patch.object(send_email, "send_email",
                               side_effect=lambda s, b, h: cap.update(body=b)):
            send_email.cmd_trade_notify(SimpleNamespace(
                action="sell", code="006479", name="纳指100C", amount="100",
                nav="8.0", shares="12.5", note="紧急止损",
                reason=None, news=[], wallet=None))
        self.assertIn("紧急止损", cap["body"])


# ===================== card_wallet =====================

class CardWalletTest(unittest.TestCase):
    def _ctx(self, with_pending=False):
        funds = {"006479": {"current_nav": 8.00, "day_return": -0.03}}
        positions = [
            {"code": "006479", "name": "广发纳指100C", "shares": 1000,
             "cost_nav": 6.50, "sector": "美股QDII", "is_pending": False},
        ]
        if with_pending:
            funds["540010"] = {"current_nav": 6.96, "day_return": 0.017}
            positions.append({"code": "540010", "name": "汇丰晋信", "shares": 200,
                              "cost_nav": 6.84, "sector": "A股科技", "is_pending": True})
        return {"funds": funds, "positions": positions, "cash": 30000.0,
                "account": "主线", "dca_plans": [], "budget": 36500.0}

    def test_basic_content_and_math(self):
        import daily_report, fetch_fund
        with mock.patch.object(fetch_fund, "portfolio_return_series",
                               return_value=[("d1", 0.10), ("d2", 0.231)]):
            md = "\n".join(daily_report.card_wallet(self._ctx()))
        self.assertIn("总钱包（持仓市值 + 现金）", md)
        self.assertIn("可用现金", md)
        self.assertIn("现金储备线(10%)", md)
        self.assertIn("总收益走势", md)
        self.assertIn("持仓收益", md)
        self.assertIn("持仓成本", md)
        self.assertIn("本金", md)
        # 持仓 8000, 现金 30000 → 总钱包 38000；持仓收益 = 8000-6500 = 1500 (+23.08%)
        self.assertIn("¥38,000", md)
        self.assertIn("1,500.00", md)
        # 现金 30000 = 本金 36500 − 持仓成本 6500 → 标注公式
        self.assertIn("= 本金 − 持仓成本", md)

    def test_pending_excluded_from_pnl_valued_at_cost(self):
        import daily_report, fetch_fund
        with mock.patch.object(fetch_fund, "portfolio_return_series", return_value=[]):
            md = "\n".join(daily_report.card_wallet(self._ctx(with_pending=True)))
        # 540010 待确认按成本 200*6.84=1368 计入市值，但不计入总收益
        # 持仓 = 8000(006479) + 1368(540010@cost) = 9368；现金 30000 → 总钱包 39368
        self.assertIn("¥39,368", md)
        self.assertIn("1,500.00", md)        # 总收益仍只含已确认的 006479

    def test_dca_quota_line_when_plans_present(self):
        import daily_report, fetch_fund
        ctx = self._ctx()
        ctx["dca_plans"] = [{"code": "006479", "name": "广发纳指100C",
                             "amount": 10.0, "frequency": "daily"}]
        with mock.patch.object(fetch_fund, "portfolio_return_series", return_value=[]):
            md = "\n".join(daily_report.card_wallet(ctx))
        self.assertIn("定投额度", md)


# ===================== calibrate_costs =====================

class CalibrateCostsTest(unittest.TestCase):
    def setUp(self):
        import fetch_fund
        self.ff = fetch_fund
        self.db = make_in_memory_db()
        self.aid = add_test_account(self.db, name="主线", budget=50000.0)
        self._orig_DB = fetch_fund.Database
        self._orig_avail = fetch_fund.DB_AVAILABLE
        fetch_fund.DB_AVAILABLE = True
        self.db.close = lambda: None          # in-memory：别被 finally 关掉
        fetch_fund.Database = lambda *a, **k: self.db

    def tearDown(self):
        self.ff.Database = self._orig_DB
        self.ff.DB_AVAILABLE = self._orig_avail

    def _series(self, navmap):
        return lambda code, days=30: navmap.get(code, [])

    def test_single_lot_calibrated(self):
        self.db.set_position(self.aid, "540010", "汇丰晋信", 237.47, 6.8429,
                             buy_date="2026-06-23", sector="A股科技")
        self.db.add_trade(self.aid, "2026-06-23", "540010", "汇丰晋信",
                          "buy", 1625.0, 6.8429, 237.47)
        with mock.patch.object(self.ff, "fetch_nav_series",
                               side_effect=self._series({"540010": [("2026-06-23", 6.90)]})), \
             mock.patch.object(self.ff, "_buy_unconfirmed", return_value=False):
            rows = self.ff.calibrate_costs("主线", apply=True)
        self.assertEqual(rows[0]["status"], "applied")
        pos = self.db.get_positions(self.aid)[0]
        self.assertAlmostEqual(pos["cost_nav"], 6.90, places=4)
        self.assertAlmostEqual(pos["shares"], round(1625.0 / 6.90, 2), places=2)

    def test_idempotent(self):
        self.db.set_position(self.aid, "540010", "汇丰晋信", 237.47, 6.8429,
                             buy_date="2026-06-23", sector="A股科技")
        self.db.add_trade(self.aid, "2026-06-23", "540010", "汇丰晋信",
                          "buy", 1625.0, 6.8429, 237.47)
        with mock.patch.object(self.ff, "fetch_nav_series",
                               side_effect=self._series({"540010": [("2026-06-23", 6.90)]})), \
             mock.patch.object(self.ff, "_buy_unconfirmed", return_value=False):
            self.ff.calibrate_costs("主线", apply=True)
            rows2 = self.ff.calibrate_costs("主线", apply=True)
        self.assertEqual(rows2, [])

    def test_accumulated_skipped(self):
        self.db.set_position(self.aid, "006479", "广发纳指100C", 2427.86, 6.5449,
                             buy_date="2026-03-01", sector="美股QDII")
        self.db.add_trade(self.aid, "2026-06-23", "006479", "广发纳指100C",
                          "buy", 10.0, 6.80, 1.47)
        with mock.patch.object(self.ff, "fetch_nav_series",
                               side_effect=self._series({"006479": [("2026-06-23", 6.95)]})), \
             mock.patch.object(self.ff, "_buy_unconfirmed", return_value=False):
            rows = self.ff.calibrate_costs("主线", apply=True)
        self.assertEqual(rows[0]["status"], "skip_accumulated")
        pos = [p for p in self.db.get_positions(self.aid) if p["code"] == "006479"][0]
        self.assertAlmostEqual(pos["cost_nav"], 6.5449, places=4)

    def test_today_not_calibrated(self):
        self.db.set_position(self.aid, "540010", "汇丰晋信", 237.47, 6.8429,
                             buy_date="2026-06-24", sector="A股科技")
        self.db.add_trade(self.aid, "2026-06-24", "540010", "汇丰晋信",
                          "buy", 1625.0, 6.8429, 237.47)
        with mock.patch.object(self.ff, "fetch_nav_series", side_effect=self._series({})), \
             mock.patch.object(self.ff, "_buy_unconfirmed", return_value=True):
            rows = self.ff.calibrate_costs("主线", apply=True)
        self.assertEqual(rows, [])


# ===================== web_panel =====================

class WebPanelTest(unittest.TestCase):
    """v2 面板：JSON API + 自包含前端。测后端数据层 + 页面外壳。"""

    def setUp(self):
        import web_panel
        web_panel._OVERVIEW_CACHE.clear()
        web_panel._KLINE_CACHE.clear()

    def test_page_html_is_self_contained(self):
        import web_panel
        html = web_panel.PAGE_HTML
        self.assertIn("Smart Invest", html)
        self.assertIn("/api/overview", html)   # 前端走 JSON API
        self.assertIn("echarts", html)          # K线/走势图

    def test_overview_error_into_json(self):
        # build_context 报错 → 进 error 字段，不抛异常、不 500
        import web_panel
        with mock.patch.object(web_panel.daily_report, "build_context",
                               return_value=(None, "boom")), \
             mock.patch.object(web_panel, "_list_accounts", return_value=["主线"]), \
             mock.patch.object(web_panel.fetch_fund, "market_clock", return_value={}):
            data = web_panel._overview_data("主线", "close")
        self.assertEqual(data["error"], "boom")
        self.assertEqual(data["account"], "主线")

    def test_overview_shape(self):
        import web_panel
        clock = {"time": "14:00", "session": "盘尾"}
        ctx = {
            "funds": {"000001": {"name": "测试基金", "current_nav": 1.1,
                                 "day_return": 0.02, "fund_5d_return": 0.01,
                                 "fund_20d_return": 0.03, "fund_60d_return": 0.05,
                                 "sector": "科技", "signals": {"rsi_14": 55}}},
            "positions": [{"code": "000001", "name": "测试基金", "shares": 1000,
                           "cost_nav": 1.0, "sector": "科技", "hold_days": 10,
                           "is_pending": False}],
            "cash": 5000.0, "budget": 10000.0,
            "packet": {"actions": [], "alerts": [], "market_regime": {}},
            "news": [{"title": "利好", "summary": "s", "time": "t", "url": "u"}],
            "discovered": [], "dca_plans": [],
        }
        with mock.patch.object(web_panel.daily_report, "build_context",
                               return_value=(ctx, None)), \
             mock.patch.object(web_panel, "_list_accounts", return_value=["主线"]), \
             mock.patch.object(web_panel.fetch_fund, "market_clock", return_value=clock), \
             mock.patch.object(web_panel.fetch_fund, "portfolio_return_series",
                               return_value=[("2026-06-01", 0.1)]):
            data = web_panel._overview_data("主线", "close")
        self.assertNotIn("error", data)
        self.assertEqual(len(data["holdings"]), 1)
        h = data["holdings"][0]
        self.assertAlmostEqual(h["market_value"], 1100.0)
        self.assertAlmostEqual(h["hold_pnl"], 100.0)
        self.assertAlmostEqual(data["wallet"]["return_pct"], 0.1)   # 100/1000，不含现金
        self.assertEqual(len(data["news"]), 1)

    def test_kline_fund_vs_index(self):
        import web_panel
        with mock.patch.object(web_panel.fetch_fund, "_resolve_chart_target",
                               return_value=("fund", "000001", None)), \
             mock.patch.object(web_panel.fetch_fund, "fetch_nav_series",
                               return_value=[("2026-06-01", 1.0), ("2026-06-02", 1.1)]):
            d = web_panel._kline_data("000001", 60)
        self.assertEqual(d["type"], "nav")
        self.assertEqual(len(d["points"]), 2)

        web_panel._KLINE_CACHE.clear()
        with mock.patch.object(web_panel.fetch_fund, "_resolve_chart_target",
                               return_value=("index", "1.000300", "沪深300")), \
             mock.patch.object(web_panel.fetch_fund, "fetch_index_kline",
                               return_value={"name": "沪深300",
                                             "points": [{"date": "2026-06-01", "open": 1,
                                                         "close": 2, "high": 3, "low": 1,
                                                         "volume": 10}]}):
            d = web_panel._kline_data("沪深300", 120)
        self.assertEqual(d["type"], "ohlc")
        self.assertEqual(d["name"], "沪深300")

    def test_kline_unknown_target(self):
        import web_panel
        with mock.patch.object(web_panel.fetch_fund, "_resolve_chart_target",
                               return_value=None):
            d = web_panel._kline_data("???", 60)
        self.assertIn("error", d)

    def test_fetch_index_kline_parse(self):
        # 解析东财 klines「日期,开,收,高,低,量」CSV 行 → 结构化 OHLC（升序）
        import fetch_fund
        import json as _json
        blob = _json.dumps({"data": {"name": "沪深300", "klines": [
            "2026-06-01,4000.0,4050.5,4060,3990,1234",
            "2026-06-02,4050.5,4020.0,4070,4010,2345",
        ]}})
        with mock.patch.object(fetch_fund, "_get", return_value=blob):
            res = fetch_fund.fetch_index_kline("1.000300", days=120)
        self.assertEqual(res["name"], "沪深300")
        self.assertEqual(len(res["points"]), 2)
        self.assertAlmostEqual(res["points"][0]["close"], 4050.5)
        self.assertAlmostEqual(res["points"][1]["high"], 4070.0)

    def test_fetch_index_kline_failure(self):
        import fetch_fund
        with mock.patch.object(fetch_fund, "_get", return_value=None):
            res = fetch_fund.fetch_index_kline("1.000300", days=60)
        self.assertEqual(res["points"], [])


if __name__ == "__main__":
    unittest.main()
