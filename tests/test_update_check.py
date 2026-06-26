"""每日自更新检查测试（版本文件比对）。stdlib unittest，全程 mock 网络/git。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import update_check as U


class UpdateCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="upd-")) / ".update_check"
        self._orig_marker = U.MARKER
        U.MARKER = self.tmp

    def tearDown(self):
        U.MARKER = self._orig_marker

    def test_due_today_when_no_marker(self):
        self.assertTrue(U.due_today())

    def test_not_due_after_mark(self):
        U.mark_checked()
        self.assertFalse(U.due_today())

    def test_no_update_when_versions_match(self):
        with mock.patch.object(U, "local_version", return_value="1.1.0"), \
             mock.patch.object(U, "remote_version", return_value="1.1.0"):
            res = U.check(apply=True, force=True)
        self.assertFalse(res["update_available"])
        self.assertFalse(res["applied"])
        self.assertIn("最新", res["message"])

    def test_notify_only_when_update_and_no_apply(self):
        with mock.patch.object(U, "local_version", return_value="1.1.0"), \
             mock.patch.object(U, "remote_version", return_value="1.2.0"):
            res = U.check(apply=False, force=True)
        self.assertTrue(res["update_available"])
        self.assertFalse(res["applied"])
        self.assertIn("1.2.0", res["message"])

    def test_apply_pulls_when_clean(self):
        with mock.patch.object(U, "local_version", side_effect=["1.1.0", "1.2.0"]), \
             mock.patch.object(U, "remote_version", return_value="1.2.0"), \
             mock.patch.object(U, "_is_clean_git", return_value=True), \
             mock.patch.object(U, "_git", return_value=(True, "Updated")) as g:
            res = U.check(apply=True, force=True)
        self.assertTrue(res["applied"])
        self.assertIn("已更新", res["message"])
        g.assert_called_with("pull", "--ff-only", U.GIT_REMOTE, U.GIT_BRANCH)

    def test_apply_refuses_when_dirty(self):
        with mock.patch.object(U, "local_version", return_value="1.1.0"), \
             mock.patch.object(U, "remote_version", return_value="1.2.0"), \
             mock.patch.object(U, "_is_clean_git", return_value=False):
            res = U.check(apply=True, force=True)
        self.assertTrue(res["update_available"])
        self.assertFalse(res["applied"])
        self.assertIn("未提交改动", res["message"])

    def test_skips_when_already_checked_today(self):
        U.mark_checked()
        with mock.patch.object(U, "remote_version") as rv:
            res = U.check(apply=True, force=False)
        self.assertFalse(res["checked"])
        rv.assert_not_called()           # 当天不再打网络

    def test_empty_remote_is_graceful(self):
        with mock.patch.object(U, "local_version", return_value="1.1.0"), \
             mock.patch.object(U, "remote_version", return_value=""):
            res = U.check(apply=True, force=True)
        self.assertFalse(res["update_available"])
        self.assertIn("无法获取", res["message"])


if __name__ == "__main__":
    unittest.main()
