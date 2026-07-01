#!/usr/bin/env python3
"""应用配置层 —— 离线/在线模式、LLM 适配、远程同步的统一配置入口。

读取 `data/app_config.json`（可选，gitignored，可含密钥），环境变量同名覆盖。
纯 stdlib，无三方依赖；文件缺失/损坏一律回退到「离线、无 LLM、无同步」的安全默认。

配置文件形态（全部可选）：
{
  "mode": "offline" | "online",          // 默认 offline（向后兼容、单机）
  "llm": {                                // Anthropic Messages 兼容三方 API
    "base_url": "https://your-gateway/v1",
    "api_key": "sk-...",
    "model": "claude-...",
    "auth_style": "x-api-key" | "bearer", // 默认 x-api-key
    "anthropic_version": "2023-06-01"
  },
  "sync": {                               // 在线连通模式下与远程服务器双向同步
    "base_url": "https://your-server",
    "token": "...",
    "device_id": "macbook-1"
  },
  "web": {                                // 与 smart-invest-web 在线平台双向同步（skill 控制 web / web 回流 skill）
    "base_url": "http://43.119.62.71:8090",
    "email": "you@example.com",
    "password": "...",
    "wallet": "主线",                      // 本地账户 ↔ web 钱包 的名字映射（缺省同名）
    "account": "主线"
  }
}
"""
import json
import os
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "data" / "app_config.json"

_CACHE = None


def _load_file(path=None):
    p = Path(path) if path else CONFIG_PATH
    if not p.exists():
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError, ValueError):
        return {}


def load_config(path=None, force=False):
    """合并配置（文件 + 环境变量覆盖）。结果缓存；force=True 或传 path 时重读。"""
    global _CACHE
    if _CACHE is not None and not force and path is None:
        return _CACHE

    cfg = _load_file(path)
    cfg.setdefault("mode", "offline")
    cfg.setdefault("llm", {})
    cfg.setdefault("sync", {})
    cfg.setdefault("web", {})

    # 环境变量覆盖（便于 CI / 容器注入，不落盘密钥）
    env = os.environ
    if env.get("SMART_INVEST_MODE"):
        cfg["mode"] = env["SMART_INVEST_MODE"]
    for ek, (sect, key) in {
        "SMART_INVEST_LLM_BASE_URL": ("llm", "base_url"),
        "SMART_INVEST_LLM_KEY": ("llm", "api_key"),
        "SMART_INVEST_LLM_MODEL": ("llm", "model"),
        "SMART_INVEST_LLM_AUTH": ("llm", "auth_style"),
        "SMART_INVEST_SYNC_URL": ("sync", "base_url"),
        "SMART_INVEST_SYNC_TOKEN": ("sync", "token"),
        "SMART_INVEST_DEVICE_ID": ("sync", "device_id"),
        "SMART_INVEST_WEB_URL": ("web", "base_url"),
        "SMART_INVEST_WEB_EMAIL": ("web", "email"),
        "SMART_INVEST_WEB_PASSWORD": ("web", "password"),
        "SMART_INVEST_WEB_WALLET": ("web", "wallet"),
        "SMART_INVEST_WEB_ACCOUNT": ("web", "account"),
    }.items():
        if env.get(ek):
            cfg[sect][key] = env[ek]

    if cfg["mode"] not in ("offline", "online"):
        cfg["mode"] = "offline"

    if path is None:
        _CACHE = cfg
    return cfg


def reset_cache():
    """测试用：清掉缓存。"""
    global _CACHE
    _CACHE = None


def mode(path=None):
    return load_config(path).get("mode", "offline")


def is_online(path=None):
    return mode(path) == "online"


def llm_config(path=None):
    return load_config(path).get("llm", {}) or {}


def sync_config(path=None):
    return load_config(path).get("sync", {}) or {}


def web_config(path=None):
    return load_config(path).get("web", {}) or {}


if __name__ == "__main__":
    import sys
    cfg = load_config()
    safe = json.loads(json.dumps(cfg))  # 脱敏打印
    if safe.get("llm", {}).get("api_key"):
        safe["llm"]["api_key"] = "***"
    if safe.get("sync", {}).get("token"):
        safe["sync"]["token"] = "***"
    if safe.get("web", {}).get("password"):
        safe["web"]["password"] = "***"
    json.dump(safe, sys.stdout, ensure_ascii=False, indent=2)
    print()
