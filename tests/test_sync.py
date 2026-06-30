#!/usr/bin/env python3
"""双向同步测试：纯合并逻辑 + DB 序列化/回写 + 真实 client↔server 往返。

Run: python3 -m unittest tests.test_sync -v
"""
import contextlib
import io
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import sync_state
import sync_client
import server as server_mod
from db import Database
from tests._helpers import make_in_memory_db


def _file_db():
    """临时文件 DB（check_same_thread=False，可被同步服务器跨线程共享）。"""
    tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tf.close()
    db = Database(tf.name)
    with contextlib.redirect_stdout(io.StringIO()):
        db.init_tables()
    return db, tf.name


# ---------- 纯合并逻辑 ----------

class TestMergeLogic(unittest.TestCase):
    def test_trade_uid_stable_and_distinct(self):
        a = {"date": "2026-06-01", "code": "110011", "action": "buy",
             "amount": 1000, "nav": 2.0, "shares": 500}
        self.assertEqual(sync_state.trade_uid(a), sync_state.trade_uid(dict(a)))
        b = dict(a, shares=600)
        self.assertNotEqual(sync_state.trade_uid(a), sync_state.trade_uid(b))

    def test_merge_trades_union_dedup(self):
        t1 = {"date": "2026-06-01", "code": "A", "action": "buy", "amount": 1,
              "nav": 1, "shares": 1}
        t2 = {"date": "2026-06-02", "code": "B", "action": "buy", "amount": 1,
              "nav": 1, "shares": 1}
        for t in (t1, t2):
            t["uid"] = sync_state.trade_uid(t)
        merged = sync_state.merge_trades([t1], [t1, t2])  # t1 重复
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["date"], "2026-06-01")  # 排序

    def test_merge_positions_lww(self):
        local = [{"code": "A", "shares": 100, "updated_at": "2026-06-01"}]
        remote = [{"code": "A", "shares": 200, "updated_at": "2026-06-05"},
                  {"code": "B", "shares": 50, "updated_at": "2026-06-03"}]
        out = {p["code"]: p for p in sync_state.merge_positions(local, remote)}
        self.assertEqual(out["A"]["shares"], 200)   # remote 更新 → 胜
        self.assertIn("B", out)                     # 对端独有保留

    def test_merge_positions_drops_zeroed(self):
        out = sync_state.merge_positions(
            [{"code": "A", "shares": 100, "updated_at": "1"}],
            [{"code": "A", "shares": 0, "updated_at": "2"}])
        self.assertEqual(out, [])  # 已清仓

    def test_merge_account_cash_lww(self):
        local = {"account": "w", "cash": 100, "cash_ts": "2026-06-01",
                 "updated_at": "2026-06-01", "positions": [], "trades": []}
        remote = {"account": "w", "cash": 250, "cash_ts": "2026-06-09",
                  "updated_at": "2026-06-09", "positions": [], "trades": []}
        self.assertEqual(sync_state.merge_account(local, remote)["cash"], 250)


# ---------- DB 序列化/回写 ----------

class TestSerializeApply(unittest.TestCase):
    def setUp(self):
        self.db = make_in_memory_db()
        self.aid = self.db.create_account("实战-A", "paper", 50000)

    def tearDown(self):
        self.db.close()

    def test_serialize_then_apply_roundtrip(self):
        self.db.set_position(self.aid, "110011", "易方达", 1000, 2.0, sector="科技")
        self.db.add_trade(self.aid, "2026-06-01", "110011", "易方达", "buy",
                          2000, 2.0, 1000)
        state = sync_client.serialize_account(self.db, "实战-A")
        self.assertEqual(len(state["positions"]), 1)
        self.assertEqual(len(state["trades"]), 1)
        self.assertTrue(state["trades"][0]["uid"])

        # 在第二个库上 apply → 应重建出同样的持仓/交易，且幂等
        db2 = make_in_memory_db()
        try:
            r1 = sync_client.apply_account(db2, state)
            self.assertTrue(r1["created"])
            self.assertEqual(r1["trades_added"], 1)
            r2 = sync_client.apply_account(db2, state)  # 再来一次
            self.assertEqual(r2["trades_added"], 0)     # 幂等不重复
            aid2 = db2.get_account(name="实战-A")["id"]
            self.assertEqual(len(db2.get_trades(aid2)), 1)
            self.assertEqual(len(db2.get_positions(aid2)), 1)
        finally:
            db2.close()


# ---------- 真实 client↔server 往返 ----------

class TestRoundTrip(unittest.TestCase):
    """两台「设备」各自有本地库，都同步到同一台服务器 → 服务器并集 → 两端收敛。"""

    def setUp(self):
        self.server_db, self._dbpath = _file_db()
        self.httpd = server_mod.make_server(
            "127.0.0.1", 0, db=self.server_db, tokens={"tok-1": "alice"})
        self.port = self.httpd.server_address[1]
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.server_db.close()
        os.unlink(self._dbpath)

    def _cfg(self):
        return {"base_url": self.base, "token": "tok-1"}

    def test_two_devices_converge(self):
        # 设备1：建钱包，买 A
        d1 = make_in_memory_db()
        a1 = d1.create_account("实战-X", "paper", 100000)
        d1.set_position(a1, "110011", "基金A", 1000, 2.0, sector="科技")
        d1.add_trade(a1, "2026-06-01", "110011", "基金A", "buy", 2000, 2.0, 1000)
        r1 = sync_client.sync_account(d1, "实战-X", cfg=self._cfg())
        self.assertTrue(r1["ok"], r1)

        # 设备2：同名钱包，买 B（独立交易）
        d2 = make_in_memory_db()
        a2 = d2.create_account("实战-X", "paper", 100000)
        d2.set_position(a2, "161725", "基金B", 500, 1.5, sector="消费")
        d2.add_trade(a2, "2026-06-02", "161725", "基金B", "buy", 750, 1.5, 500)
        r2 = sync_client.sync_account(d2, "实战-X", cfg=self._cfg())
        self.assertTrue(r2["ok"], r2)

        # 设备2 同步后应已拿到设备1 的交易（服务器并集）
        a2 = d2.get_account(name="实战-X")["id"]
        codes2 = {t["code"] for t in d2.get_trades(a2)}
        self.assertEqual(codes2, {"110011", "161725"})

        # 设备1 再同步一次 → 也收敛到两笔
        r1b = sync_client.sync_account(d1, "实战-X", cfg=self._cfg())
        self.assertTrue(r1b["ok"])
        a1 = d1.get_account(name="实战-X")["id"]
        codes1 = {t["code"] for t in d1.get_trades(a1)}
        self.assertEqual(codes1, {"110011", "161725"})
        d1.close()
        d2.close()

    def test_unauthorized_rejected(self):
        d = make_in_memory_db()
        d.create_account("实战-Y", "paper", 1000)
        res = sync_client.sync_account(d, "实战-Y",
                                       cfg={"base_url": self.base, "token": "bad"})
        self.assertFalse(res["ok"])  # 401 → server_state None → 失败
        d.close()

    def test_wallets_isolated_per_user(self):
        # alice 的钱包不应出现在另一 token 用户名下（命名空间隔离）
        self.httpd.tokens["tok-2"] = "bob"
        d = make_in_memory_db()
        d.create_account("仅-Alice", "paper", 1000)
        sync_client.sync_account(d, "仅-Alice", cfg=self._cfg())
        alice = server_mod.list_wallets(self.server_db, "alice")
        bob = server_mod.list_wallets(self.server_db, "bob")
        self.assertIn("仅-Alice", alice)
        self.assertNotIn("仅-Alice", bob)
        d.close()


if __name__ == "__main__":
    unittest.main()
