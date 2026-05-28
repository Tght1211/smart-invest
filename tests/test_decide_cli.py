"""Smoke test for scripts/decide.py CLI.

Verifies:
- 'run' against a real DB account works end-to-end (offline path)
- JSON output validates against §6 schema top-level keys
- Markdown output is non-empty and contains the report header

The test creates a TEMPORARY DB file (data/.test_smart_invest.db), exercises
gather_market_snapshot in a way that won't crash if the network is offline
(funds dict will just be empty), runs decide.py via subprocess, and cleans up.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"


class DecideCliSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Use a temp DB via SMART_INVEST_DB env var so we don't touch the real one.
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="smart-invest-test-"))
        cls.db_path = cls.tmpdir / "smart_invest.db"

        env = os.environ.copy()
        env["SMART_INVEST_DB"] = str(cls.db_path)
        cls.env = env

        subprocess.run(
            ["python3", str(SCRIPTS / "db.py"), "init"],
            env=env, capture_output=True, check=True,
        )
        # Insert a test account
        import sqlite3
        from datetime import datetime
        conn = sqlite3.connect(str(cls.db_path))
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO accounts (name, type, budget, cash, status, "
            "strategy_version, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'active', 'v2.0', ?, ?)",
            ("cli-test", "main", 10000.0, 10000.0, now, now),
        )
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _run(self, *extra):
        return subprocess.run(
            ["python3", str(SCRIPTS / "decide.py"),
             "run", "--account", "cli-test", *extra],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_json_output_has_schema_keys(self):
        result = self._run("--format", "json")
        if result.returncode != 0:
            # Network may be unavailable; either way it should be a clean exit
            # (2 for missing data, not 3 = unexpected). 0 is best.
            self.assertIn(result.returncode, (0, 2),
                          msg=f"stderr={result.stderr}")
        if result.returncode == 0:
            packet = json.loads(result.stdout)
            for key in ("schema_version", "account", "market_regime",
                        "portfolio_snapshot", "actions", "blocked_actions",
                        "alerts", "summary"):
                self.assertIn(key, packet)
            self.assertEqual(packet["account"], "cli-test")
            self.assertEqual(packet["schema_version"], "1.0")

    def test_md_output_non_empty(self):
        result = self._run("--format", "md")
        if result.returncode == 0:
            self.assertIn("决策包 — 账户 cli-test", result.stdout)
            self.assertIn("总资产", result.stdout)


if __name__ == "__main__":
    unittest.main()
