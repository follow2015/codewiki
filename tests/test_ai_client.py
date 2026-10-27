"""客户端判据（§5.3 / §5.5）。

失败分类是要害：

* 认证错（401/403）**立即失败** —— 重试只会浪费时间与配额；
* 不可达 / 限流（429/5xx）**指数退避重试**，耗尽后才失败；
* 响应结构坏 —— 立即失败，且消息里不能带出密钥。

最后一条对应 §5.3「密钥不落盘、不写日志、不进产物」：所有异常消息都过脱敏。

scan-secrets: fixtures-allowed —— 本文件含故意写出的假密钥样本，用途是验证扫描器与脱敏。
"""

from __future__ import annotations

import json

import pytest

from codewiki.ai.client import (
    ChatMessage,
    LLMBadResponse,
    LLMUnavailable,
    OpenAICompatClient,
    probe_connectivity,
)


def _client(replies=None, *, max_retries: int = 2, transport=None):
    return OpenAICompatClient(
        model="m",
        base_url="http://x/v1",
        api_key="sk-secret-key-abcdef123456",
        max_retries=max_retries,
        backoff_base_s=0.0,
        transport=transport,
    )


def test_ok_response_is_parsed():
    client = _client(
        transport=lambda _p: {
            "choices": [{"message": {"content": "hello"}}],
            "model": "m",
            "usage": {"prompt_tokens": 3, "completion_tokens": 4},
        }
    )
    result = client.chat([ChatMessage(role="user", content="hi")])
    assert result.content == "hello" and result.total_tokens == 7


def test_auth_error_is_not_retried():
    calls = []

    def transport(_p):
        calls.append(1)
        raise LLMUnavailable.__mro__[1]("nope")

    from codewiki.ai.client import LLMAuthError

    def raising(_p):
        calls.append(1)
        raise LLMAuthError("401 sk-secret-key-abcdef123456")

    client = _client(transport=raising)
    with pytest.raises(LLMAuthError):
        client.chat([ChatMessage(role="user", content="hi")])
    assert len(calls) == 1, "认证失败重试是纯浪费"


def test_unavailable_is_retried_then_gives_up():
    calls = []

    def raising(_p):
        calls.append(1)
        raise LLMUnavailable("429 rate limited")

    client = _client(transport=raising, max_retries=2)
    with pytest.raises(LLMUnavailable):
        client.chat([ChatMessage(role="user", content="hi")])
    assert len(calls) == 3, f"首次 + 2 次重试（实际 {len(calls)}）"


def test_bad_response_is_reported():
    client = _client(transport=lambda _p: {"unexpected": True})
    with pytest.raises(LLMBadResponse):
        client.chat([ChatMessage(role="user", content="hi")])


def test_secrets_are_redacted_from_errors():
    """异常消息必须脱敏（§5.3）：密钥不进日志、不进产物。"""
    from codewiki.ai.client import LLMError as Base

    err = Base("failed with sk-secret-key-abcdef123456")
    assert "sk-secret-key-abcdef123456" not in str(err)
    assert "***" in str(err)


def test_payload_shape_is_openai_compatible():
    seen = {}

    def transport(payload):
        seen.update(payload)
        return {"choices": [{"message": {"content": "{}"}}]}

    _client(transport=transport).chat([ChatMessage(role="system", content="s")])
    assert seen["model"] == "m"
    assert seen["messages"][0] == {"role": "system", "content": "s"}
    assert json.dumps(seen)


def test_env_fallback_for_base_url(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://from-env/v1")
    client = OpenAICompatClient(model="m")
    assert client.base_url == "http://from-env/v1"


def test_probe_reports_failure_reason():
    def raising(_p):
        raise LLMUnavailable("connection refused")

    ok, message = probe_connectivity(_client(transport=raising, max_retries=0))
    assert ok is False and "不可达" in message


def test_probe_reports_success():
    ok, message = probe_connectivity(
        _client(transport=lambda _p: {"choices": [{"message": {"content": "pong"}}]})
    )
    assert ok is True and "可达" in message
