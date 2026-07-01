#!/usr/bin/env python3
"""skill ↔ smart-invest-web 双向同步测试（web transport 全 mock，纯 stdlib unittest）。

Run: python3 -m unittest tests.test_web_sync -v
"""
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import web_sync
from tests._helpers import make_in_memory_db


class _Resp:
    """最小 urlopen 上下文管理器返回体。"""
    def __init__(self, obj):
        self._b = json.dumps(obj, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


def make_opener(responses):
    """按请求路径路由的假 opener：/api/login → token；/api/sync → {state}。"""
    def opener(req, timeout=None):
        path = req.full_url
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        for suffix, fn in responses.items():
            if path.endswith(suffix):
                return _Resp(fn(body))
        raise AssertionError("unexpected url " + path)
    return opener


class TestPayloadShaping(unittest.TestCase):
    def test_to_web_payload(self):
        state = {"budget": 1000, "cash": 300,
                 "positions": [{"code": "A", "name": "甲", "shares": 100,
                                "cost_nav": 2.0, "sector": "科技"}],
                 "trades": [{"uid": "u1", "date": "2026-06-01", "code": "A", "name": "甲",
                             "action": "buy", "amount": 200, "nav": 2.0, "shares": 100}]}
        p = web_sync.to_web_payload(state, "主线")
        self.assertEqual(p["wallet"], "主线")
        self.assertEqual(p["holdings"][0]["code"], "A")
        self.assertEqual(p["trades"][0]["uid"], "u1")

    def test_from_web_state(self):
        web_state = {"budget": 1000, "cash": 300,
                     "holdings": [{"code": "B", "name": "乙", "shares": 50, "cost_nav": 1.5}],
                     "trades": [{"uid": "u2", "date": "2026-06-02", "code": "B", "name": "乙",
                                 "action": "buy", "amount": 75, "nav": 1.5, "shares": 50}]}
        s = web_sync.from_web_state(web_state, "主线")
        self.assertEqual(s["account"], "主线")
        self.assertEqual(s["positions"][0]["code"], "B")
        self.assertEqual(s["trades"][0]["uid"], "u2")


class TestSyncRoundTrip(unittest.TestCase):
    def setUp(self):
        self.db = make_in_memory_db()
        self.aid = self.db.create_account("主线", "paper", 50000)
        self.db.set_position(self.aid, "110011", "易方达", 1000, 2.0, sector="科技")
        self.db.add_trade(self.aid, "2026-06-01", "110011", "易方达", "buy",
                          2000, 2.0, 1000)

    def _cfg(self):
        return {"base_url": "http://web", "email": "u@x.com",
                "password": "pw", "account": "主线", "wallet": "主线"}

    def test_not_configured(self):
        res = web_sync.sync_account(self.db, cfg={"base_url": "http://web"})
        self.assertFalse(res["ok"])

    def test_login_fail(self):
        opener = make_opener({"/api/login": lambda b: {"error": "bad"}})  # 无 token
        res = web_sync.sync_account(self.db, cfg=self._cfg(), _opener=opener)
        self.assertFalse(res["ok"])
        self.assertIn("登录", res["error"])

    def test_pulls_web_side_new_trade(self):
        """web 回传里含本地没有的交易 + 新持仓 → 拉回本地 DB。"""
        def sync_resp(body):
            # web 侧「合并后」状态：保留本地推上去的持仓，另加一笔 web 侧新交易 + 新持仓
            return {"ok": True, "state": {
                "wallet": "主线", "budget": 50000, "cash": 12345,
                "holdings": [
                    {"code": "110011", "name": "易方达", "shares": 1000, "cost_nav": 2.0,
                     "sector": "科技"},
                    {"code": "540010", "name": "web新基", "shares": 500, "cost_nav": 3.0,
                     "sector": "AI"}],
                "trades": [
                    {"uid": "web-1", "date": "2026-06-10", "code": "540010", "name": "web新基",
                     "action": "buy", "amount": 1500, "nav": 3.0, "shares": 500,
                     "reason": "web 侧操作"}]}}
        opener = make_opener({"/api/login": lambda b: {"token": "tok123"},
                              "/api/sync": sync_resp})
        res = web_sync.sync_account(self.db, cfg=self._cfg(), _opener=opener)
        self.assertTrue(res["ok"], res)
        # web 侧新持仓被拉回
        codes = {p["code"] for p in self.db.get_positions(self.aid)}
        self.assertIn("540010", codes)
        # web 侧新交易被追加
        trs = self.db.get_trades(self.aid)
        self.assertTrue(any(t["code"] == "540010" for t in trs))
        # 现金按 web 权威值置位
        self.assertEqual(self.db.get_account(name="主线")["cash"], 12345)

    def test_login_sends_credentials(self):
        seen = {}
        def login_resp(body):
            seen.update(body)
            return {"token": "tok"}
        opener = make_opener({"/api/login": login_resp,
                              "/api/sync": lambda b: {"state": {
                                  "wallet": "主线", "budget": 50000, "cash": 1,
                                  "holdings": [], "trades": []}}})
        web_sync.sync_account(self.db, cfg=self._cfg(), _opener=opener)
        self.assertEqual(seen.get("email"), "u@x.com")
        self.assertEqual(seen.get("password"), "pw")


if __name__ == "__main__":
    unittest.main()
