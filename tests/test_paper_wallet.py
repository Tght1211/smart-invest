#!/usr/bin/env python3
"""虚拟钱包（type=paper）测试：多账户架构下的实战钱包，区别于梦境回测。

Run: python3 -m unittest tests.test_paper_wallet -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from tests._helpers import make_in_memory_db, add_test_account


class TestPaperWallet(unittest.TestCase):
    def setUp(self):
        self.db = make_in_memory_db()

    def tearDown(self):
        self.db.close()

    def test_create_and_isolate(self):
        main_id = add_test_account(self.db, "主线", "main", 50000)
        p1 = self.db.create_account("实战-A", "paper", 80000)
        p2 = self.db.create_account("实战-B", "paper", 120000)
        self.assertNotIn(p1, (main_id, p2))

        papers = [a for a in self.db.list_accounts() if a["type"] == "paper"]
        self.assertEqual({a["name"] for a in papers}, {"实战-A", "实战-B"})
        # 现金初始化 = 预算
        a = self.db.get_account(name="实战-B")
        self.assertEqual(a["budget"], 120000)
        self.assertEqual(a["cash"], 120000)
        self.assertEqual(a["type"], "paper")

    def test_paper_separate_from_dream_and_main(self):
        add_test_account(self.db, "主线", "main", 50000)
        add_test_account(self.db, "梦境-x", "dream", 50000)
        self.db.create_account("实战-A", "paper", 80000)
        by_type = {}
        for a in self.db.list_accounts():
            by_type.setdefault(a["type"], []).append(a["name"])
        self.assertEqual(by_type["main"], ["主线"])
        self.assertEqual(by_type["dream"], ["梦境-x"])
        self.assertEqual(by_type["paper"], ["实战-A"])

    def test_positions_scoped_per_wallet(self):
        # 同一只基金在两个钱包里独立记账，互不影响
        p1 = self.db.create_account("实战-A", "paper", 80000)
        p2 = self.db.create_account("实战-B", "paper", 80000)
        self.db.set_position(p1, "110011", "易方达中小盘", 1000, 2.0, sector="科技")
        rows1 = self.db.conn.execute(
            "SELECT shares FROM positions WHERE account_id=?", (p1,)).fetchall()
        rows2 = self.db.conn.execute(
            "SELECT shares FROM positions WHERE account_id=?", (p2,)).fetchall()
        self.assertEqual(len(rows1), 1)
        self.assertEqual(len(rows2), 0)


if __name__ == "__main__":
    unittest.main()
