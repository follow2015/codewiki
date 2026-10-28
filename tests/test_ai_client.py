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
    LLMBadRequest,
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


def _ok(content: str = "hello", **extra) -> dict:
    return {"choices": [{"message": {"content": content, **extra}, "finish_reason": "stop"}]}


def test_response_format_is_sent_when_asked():
    seen: dict = {}

    def transport(payload):
        seen.update(payload)
        return _ok("{}")

    _client(transport=transport).chat(
        [ChatMessage(role="user", content="hi")], response_format={"type": "json_object"}
    )
    assert seen["response_format"] == {"type": "json_object"}


def test_no_extension_fields_when_not_asked():
    """不点名就不要发 —— 平白多出的字段是自找的 400。"""
    seen: dict = {}
    _client(transport=lambda p: seen.update(p) or _ok()).chat([ChatMessage(role="user", content="hi")])
    assert "response_format" not in seen and "tools" not in seen


def test_unsupported_extension_falls_back_to_plain_request():
    payloads: list[dict] = []

    def transport(payload):
        payloads.append(dict(payload))
        if "response_format" in payload:
            raise LLMBadRequest("HTTP 400 unsupported parameter: response_format")
        return _ok("纯文本 JSON 也行")

    client = _client(transport=transport)
    result = client.chat([ChatMessage(role="user", content="hi")], response_format={"type": "json_object"})

    assert result.content == "纯文本 JSON 也行"
    assert result.degraded == ("response_format",), "降级必须被调用方看见"
    assert len(payloads) == 2 and "response_format" not in payloads[1]


def test_unsupported_field_is_remembered_and_not_resent():
    """记住降级结果：每个模块都先吃一次 400 是纯浪费。"""
    payloads: list[dict] = []

    def transport(payload):
        payloads.append(dict(payload))
        if "tools" in payload:
            raise LLMBadRequest("HTTP 400 unknown field tools")
        return _ok("ok")

    client = _client(transport=transport)
    tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
    client.chat([ChatMessage(role="user", content="1")], tools=tools)
    client.chat([ChatMessage(role="user", content="2")], tools=tools)

    assert len(payloads) == 3, f"第二次不该再发 tools（实际 {len(payloads)} 次请求）"
    assert "tools" not in payloads[-1]
    assert client.unsupported_fields == frozenset({"tools"})


def test_bad_request_without_extensions_is_raised_not_retried():
    calls: list[int] = []

    def transport(_payload):
        calls.append(1)
        raise LLMBadRequest("HTTP 400 bad model")

    with pytest.raises(LLMBadRequest):
        _client(transport=transport).chat([ChatMessage(role="user", content="hi")])
    assert len(calls) == 1, "没有扩展可去掉时不该重发"


def test_fallback_that_also_fails_reports_both():
    """回退后仍失败：错误信息要同时说清"扩展被拒"和"去掉后也不行"。"""
    calls: list[int] = []

    def transport(_payload):
        calls.append(1)
        raise LLMBadRequest("HTTP 400 nope")

    client = _client(transport=transport)
    with pytest.raises(LLMBadRequest) as excinfo:
        client.chat([ChatMessage(role="user", content="hi")], response_format={"type": "json_object"})
    assert len(calls) == 2
    assert "回退" in str(excinfo.value)


def test_native_tool_calls_are_parsed():
    data = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    result = _client(transport=lambda _p: data).chat([ChatMessage(role="user", content="hi")])

    assert result.content == ""
    assert result.finish_reason == "tool_calls"
    assert [(c.name, c.arguments) for c in result.tool_calls] == [("read_file", '{"path": "a.py"}')]


def test_malformed_tool_call_is_skipped_not_crashed():
    """模型给的 tool_calls 形状不可控：没有名字的条目跳过，不要让整轮崩掉。"""
    data = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"id": "x"},
                        {"function": {"name": "ok", "arguments": {"a": 1}}},
                    ],
                }
            }
        ]
    }
    result = _client(transport=lambda _p: data).chat([ChatMessage(role="user", content="hi")])
    assert [c.name for c in result.tool_calls] == ["ok"]
    assert result.tool_calls[0].arguments == '{"a": 1}', "dict 形式的 arguments 要序列化成字符串"


def test_empty_content_and_no_tool_calls_is_bad_response():
    client = _client(transport=lambda _p: {"choices": [{"message": {"content": None}}]})
    with pytest.raises(LLMBadResponse):
        client.chat([ChatMessage(role="user", content="hi")])


def test_content_parts_are_flattened():
    """部分网关把 content 返回成 [{type:text,text:...}] —— 不该因此判成坏响应。"""
    data = {
        "choices": [
            {"message": {"content": [{"type": "text", "text": "分段"}, {"type": "text", "text": "内容"}]}}
        ]
    }
    result = _client(transport=lambda _p: data).chat([ChatMessage(role="user", content="hi")])
    assert result.content == "分段内容"
