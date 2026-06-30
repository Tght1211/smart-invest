#!/usr/bin/env python3
"""LLM 适配层 —— 调用 Anthropic Messages 兼容的三方 API（纯 stdlib urllib）。

设计目标：base_url / api_key / model 全部来自 `app_config`（文件或环境变量），
因此**任何 Anthropic 格式的三方网关**填上配置即用，无需改代码、无三方依赖。

红线：LLM **不驱动买卖决策**（决策仍归 `decision_engine`）。仅用于报告叙事增强、
复盘点评、自然语言问答等「表达层」。未配置 / 网络失败一律**优雅降级**返回 None，
调用方回退到现有的确定性文案，绝不阻塞主流程。

用法：
    from llm_client import chat, is_configured
    if is_configured():
        text = chat([{"role": "user", "content": "用一句话点评今天的持仓表现"}],
                    system="你是稳健的投资助理")
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import app_config  # noqa: E402

DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 1024
TIMEOUT = 30


def is_configured(cfg=None):
    """是否已配齐可调用 LLM（base_url + api_key + model）。"""
    c = cfg if cfg is not None else app_config.llm_config()
    return bool(c.get("base_url") and c.get("api_key") and c.get("model"))


def _headers(c):
    h = {
        "content-type": "application/json",
        "anthropic-version": c.get("anthropic_version") or DEFAULT_ANTHROPIC_VERSION,
    }
    if (c.get("auth_style") or "x-api-key").lower() == "bearer":
        h["Authorization"] = f"Bearer {c['api_key']}"
    else:
        h["x-api-key"] = c["api_key"]
    return h


def _extract_text(resp_obj):
    """从 Anthropic 响应里抽纯文本（content 是 block 列表）。"""
    blocks = resp_obj.get("content")
    if isinstance(blocks, list):
        parts = [b.get("text", "") for b in blocks
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "".join(parts).strip()
    # 某些网关直接返回 {"text": ...} 或 OpenAI 风格，兜底
    if isinstance(blocks, str):
        return blocks.strip()
    return ""


def chat(messages, system=None, model=None, max_tokens=DEFAULT_MAX_TOKENS,
         temperature=None, cfg=None, _opener=None):
    """调用 Anthropic Messages 兼容端点，返回文本；未配置/失败返回 None（不抛异常）。

    messages: [{"role": "user"|"assistant", "content": "..."}]
    _opener: 测试注入（替代 urllib.request.urlopen），生产留空。
    """
    c = cfg if cfg is not None else app_config.llm_config()
    if not is_configured(c):
        return None

    base = c["base_url"].rstrip("/")
    url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
    payload = {
        "model": model or c["model"],
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        payload["system"] = system
    if temperature is not None:
        payload["temperature"] = temperature

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=_headers(c), method="POST")
    try:
        opener = _opener or urllib.request.urlopen
        with opener(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        return _extract_text(data) or None
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
            json.JSONDecodeError, TimeoutError, OSError):
        return None


def narrate(prompt, system=None, max_tokens=512, cfg=None, _opener=None):
    """便捷封装：单轮叙事/点评。未配置返回 None。"""
    return chat([{"role": "user", "content": prompt}], system=system,
                max_tokens=max_tokens, cfg=cfg, _opener=_opener)


if __name__ == "__main__":
    if not is_configured():
        print("LLM 未配置（缺 base_url/api_key/model）。"
              "在 data/app_config.json 的 llm 段或环境变量中填入即可。")
        raise SystemExit(0)
    q = " ".join(sys.argv[1:]) or "用一句话说明你已接入成功。"
    out = narrate(q, system="你是 Smart Invest 的投资助理，回答简洁、中文。")
    print(out or "（调用失败或无返回）")
