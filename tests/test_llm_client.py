#!/usr/bin/env python3
"""app_config + llm_client 测试（纯 stdlib，无网络；urlopen 全部 mock）。

Run: python3 -m unittest tests.test_llm_client -v
"""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import app_config
import llm_client


class TestAppConfig(unittest.TestCase):
    def setUp(self):
        app_config.reset_cache()

    def tearDown(self):
        app_config.reset_cache()

    def test_missing_file_defaults_offline(self):
        cfg = app_config.load_config(path="/no/such/config.json")
        self.assertEqual(cfg["mode"], "offline")
        self.assertEqual(cfg["llm"], {})
        self.assertEqual(cfg["sync"], {})

    def test_file_parsed(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as f:
            json.dump({"mode": "online",
                       "llm": {"base_url": "https://gw/v1", "api_key": "k",
                               "model": "claude-x"}}, f)
            path = f.name
        try:
            cfg = app_config.load_config(path=path)
            self.assertEqual(cfg["mode"], "online")
            self.assertEqual(cfg["llm"]["model"], "claude-x")
        finally:
            os.unlink(path)

    def test_bad_mode_falls_back(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as f:
            json.dump({"mode": "garbage"}, f)
            path = f.name
        try:
            self.assertEqual(app_config.load_config(path=path)["mode"], "offline")
        finally:
            os.unlink(path)

    def test_env_override(self):
        with mock.patch.dict(os.environ, {
            "SMART_INVEST_MODE": "online",
            "SMART_INVEST_LLM_BASE_URL": "https://env/v1",
            "SMART_INVEST_LLM_KEY": "envkey",
            "SMART_INVEST_LLM_MODEL": "m",
        }):
            cfg = app_config.load_config(path="/no/such.json")
        self.assertEqual(cfg["mode"], "online")
        self.assertEqual(cfg["llm"]["base_url"], "https://env/v1")

    def test_corrupt_json_safe(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as f:
            f.write("{not json")
            path = f.name
        try:
            self.assertEqual(app_config.load_config(path=path)["mode"], "offline")
        finally:
            os.unlink(path)


# 假 urlopen：返回一个带 .read() 和上下文管理器的对象
class _FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestLLMClient(unittest.TestCase):
    CFG = {"base_url": "https://gw/v1", "api_key": "k", "model": "claude-x"}

    def test_not_configured(self):
        self.assertFalse(llm_client.is_configured({}))
        self.assertFalse(llm_client.is_configured({"base_url": "x"}))
        self.assertTrue(llm_client.is_configured(self.CFG))

    def test_unconfigured_returns_none(self):
        self.assertIsNone(llm_client.chat([{"role": "user", "content": "hi"}], cfg={}))

    def test_chat_success(self):
        captured = {}

        def fake_open(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResp({"content": [{"type": "text", "text": "你好"},
                                          {"type": "text", "text": "世界"}]})

        out = llm_client.chat([{"role": "user", "content": "hi"}],
                              system="sys", cfg=self.CFG, _opener=fake_open)
        self.assertEqual(out, "你好世界")
        self.assertEqual(captured["url"], "https://gw/v1/messages")  # /v1 不重复
        self.assertEqual(captured["headers"].get("x-api-key"), "k")
        self.assertEqual(captured["body"]["system"], "sys")
        self.assertEqual(captured["body"]["model"], "claude-x")

    def test_bearer_auth_style(self):
        captured = {}

        def fake_open(req, timeout=None):
            captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
            return _FakeResp({"content": [{"type": "text", "text": "ok"}]})

        cfg = dict(self.CFG, auth_style="bearer")
        llm_client.chat([{"role": "user", "content": "x"}], cfg=cfg, _opener=fake_open)
        self.assertEqual(captured["headers"].get("authorization"), "Bearer k")
        self.assertNotIn("x-api-key", captured["headers"])

    def test_network_error_returns_none(self):
        def boom(req, timeout=None):
            raise OSError("connection reset")
        self.assertIsNone(llm_client.chat([{"role": "user", "content": "x"}],
                                          cfg=self.CFG, _opener=boom))

    def test_url_when_base_lacks_v1(self):
        captured = {}

        def fake_open(req, timeout=None):
            captured["url"] = req.full_url
            return _FakeResp({"content": [{"type": "text", "text": "ok"}]})

        cfg = dict(self.CFG, base_url="https://gw")  # 无 /v1
        llm_client.chat([{"role": "user", "content": "x"}], cfg=cfg, _opener=fake_open)
        self.assertEqual(captured["url"], "https://gw/v1/messages")

    def test_narrate(self):
        def fake_open(req, timeout=None):
            return _FakeResp({"content": [{"type": "text", "text": "点评"}]})
        self.assertEqual(llm_client.narrate("评价", cfg=self.CFG, _opener=fake_open), "点评")


if __name__ == "__main__":
    unittest.main()
