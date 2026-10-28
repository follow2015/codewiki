"""OpenAI 兼容客户端（§5.3）。

四条硬约束：

1. **零新增依赖**：用标准库 ``urllib`` 直连 `/chat/completions`，不为「调一次
   HTTP」引入 httpx/openai SDK（那会把离线安装与 wheel 矩阵一起拖下水）；
2. **密钥不落盘、不写日志、不进产物**（§5.3）：只从环境变量读，任何异常消息
   都要过 :func:`codewiki.ai.audit.redact`；
3. **失败要可分类**：认证明败 / 不可达 / 响应不可用是三种后果 —— 认证错继续
   重试是浪费，不可达直接放弃会让一次抖动毁掉整轮；
4. **扩展能力要可降级**：``tools`` / ``response_format`` 是可选增强，自建网关
   （Ollama / vLLM / 各家代理）支持程度参差。被拒时去掉扩展重发一次，并把
   "降级过"记在 :attr:`ChatResult.degraded` 上 —— 静默降级会让上层的"要求 JSON
   输出"变成一句空话而无人知晓。

重试只针对 429 / 5xx，指数退避；4xx 里的 400/422 归 :class:`LLMBadRequest`
（可能是网关不认扩展字段），其它 4xx 直接失败（§5.5）。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace

from codewiki.ai.audit import redact

ENV_API_KEY = "OPENAI_API_KEY"
ENV_BASE_URL = "OPENAI_BASE_URL"
ENV_MODEL = "OPENAI_MODEL"
DEFAULT_BASE_URL = "https://api.openai.com/v1"


def validate_base_url(base_url: str) -> str:
    """base_url 必须是带 netloc 的 http/https URL（构造期 fail-closed）。

    urllib 默认接受 ``file://`` 等协议 —— 被分析仓库是不可信输入，任何能控制
    base_url 的一方都不得把请求（连同 Bearer 密钥）引去非 HTTP 端点。
    """
    parsed = urllib.parse.urlparse(base_url or "")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise LLMError(f"base_url 必须是 http/https URL: {base_url!r}（拒绝 file://、ftp:// 等协议）")
    return base_url.rstrip("/")


def resolve_base_url(
    *,
    cli_value: str | None,
    config_value: str | None,
    env: dict[str, str] | None = None,
) -> tuple[str, str | None]:
    """解析 base_url：**只认 CLI 与环境变量，被分析仓库的配置一律忽略**。

    ``codewiki.toml`` 来自被分析仓库 —— hostile 仓库在配置里写自己的 base_url，
    就能把环境变量里的 API Key 以 Bearer 头送走。返回 ``(base_url, warning)``；
    配置里配了 base_url 时给出 warning（提示改用 CLI / 环境变量）。
    """
    environment = env if env is not None else os.environ
    if cli_value:
        return validate_base_url(cli_value), None
    if config_value:
        return validate_base_url(environment.get(ENV_BASE_URL) or DEFAULT_BASE_URL), (
            f"忽略 codewiki.toml [ai] base_url（被分析仓库的配置是不可信输入，"
            f"曾可借此把 API Key 发往任意服务器）：请改用 --base-url 或环境变量 {ENV_BASE_URL}"
        )
    return validate_base_url(environment.get(ENV_BASE_URL) or DEFAULT_BASE_URL), None


def resolve_model(
    *,
    cli_value: str | None,
    config_value: str | None,
    env: dict[str, str] | None = None,
) -> str | None:
    """解析模型名：``--model`` > ``OPENAI_MODEL`` > 配置（若已被信任）。

    与 :func:`resolve_base_url` 的分工：**信任判断不在这里** —— 哪些字段"仓库说了
    不算"由 :mod:`codewiki.trust` 统一裁决，到这里 ``config_value`` 通常已经是
    None（被忽略时那条 warning 也由 trust 给出，避免同一个字段报两遍）。
    """
    if cli_value:
        return cli_value
    environment = env if env is not None else os.environ
    from_env = environment.get(ENV_MODEL)
    if from_env:
        return from_env
    return config_value


class LLMError(Exception):
    """AI 调用失败的基类。"""

    def __init__(self, message: str) -> None:
        super().__init__(redact(message))


class LLMAuthError(LLMError):
    """认证/授权失败（401/403）—— 重试无意义。"""


class LLMUnavailable(LLMError):
    """不可达 / 限流 / 服务端错误（可重试，重试耗尽后仍属此类）。"""


class LLMBadResponse(LLMError):
    """响应结构不符合预期（无法解析出内容）。"""


class LLMBadRequest(LLMError):
    """请求本身不被接受（400 / 422）。

    与 :class:`LLMAuthError` 分开的理由很实际：**它可能是网关不认扩展字段**
    （``tools`` / ``response_format``）。这种情况值得去掉扩展再试一次，
    而认证失败重试只会浪费配额。
    """


@dataclass(frozen=True)
class ToolCall:
    """模型请求的一次原生函数调用。"""

    id: str
    name: str
    arguments: str

    def args(self) -> dict:
        """尽力解析成 dict；解析不了返回空 dict（工具层会按缺参数拒绝）。"""
        try:
            data = json.loads(self.arguments or "{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True)
class ChatResult:
    content: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    raw: dict = field(default_factory=dict)
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None
    degraded: tuple[str, ...] = ()

    @property
    def total_tokens(self) -> int:
        return (self.prompt_tokens or 0) + (self.completion_tokens or 0)


class OpenAICompatClient:
    """OpenAI 兼容 Chat Completions 客户端（可指向 Ollama / vLLM 内网网关）。"""

    supports_native_tools = True
    supports_json_mode = True

    def __init__(
        self,
        *,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_s: float = 60.0,
        max_retries: int = 3,
        backoff_base_s: float = 1.0,
        transport=None,
    ) -> None:
        self.base_url = validate_base_url(base_url or os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL)
        self.api_key = api_key or os.environ.get(ENV_API_KEY) or ""
        self.model = model
        self.timeout_s = timeout_s
        self.max_retries = max(0, max_retries)
        self.backoff_base_s = backoff_base_s
        self._transport = transport
        self.unsupported_fields: frozenset[str] = frozenset()

    def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.2,
        tools: list[dict] | None = None,
        response_format: dict | None = None,
        tool_choice: str | dict | None = None,
    ) -> ChatResult:
        """发一次对话；失败抛 :class:`LLMError` 的子类。

        ``tools`` / ``response_format`` 是可选增强：网关不认时去掉重发一次，
        并在 :attr:`ChatResult.degraded` 里如实标注降级过哪些字段。
        """
        payload = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
        }
        extensions: dict = {}
        if tools:
            extensions["tools"] = list(tools)
        if response_format:
            extensions["response_format"] = response_format
        if tool_choice is not None:
            extensions["tool_choice"] = tool_choice
        extensions = {
            name: value for name, value in extensions.items() if name not in self.unsupported_fields
        }

        if not extensions:
            return self._parse(self._send(payload))

        try:
            data = self._send({**payload, **extensions})
        except LLMBadRequest as rejection:
            try:
                data = self._send(payload)
            except LLMError as fallback_failure:
                raise LLMBadRequest(
                    f"扩展参数（{', '.join(sorted(extensions))}）被网关拒绝（{rejection}）后回退仍失败：{fallback_failure}"
                ) from fallback_failure
            self.unsupported_fields = self.unsupported_fields | frozenset(extensions)
            result = self._parse(data)
            return replace(result, degraded=result.degraded + tuple(sorted(extensions)))
        return self._parse(data)

    def _send(self, payload: dict) -> dict:
        """发送并按"可重试/不可重试"分类；只重试 :class:`LLMUnavailable`。"""
        attempt = 0
        while True:
            try:
                return self._post(payload)
            except LLMUnavailable:
                if attempt >= self.max_retries:
                    raise
                attempt += 1
                time.sleep(self.backoff_base_s * (2 ** (attempt - 1)))

    def _post(self, payload: dict) -> dict:
        if self._transport is not None:
            return self._transport(payload)
        request = urllib.request.Request(  # noqa: S310 - validate_base_url 已钉死 http/https
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:  # noqa: S310
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise LLMAuthError(f"认证失败（HTTP {exc.code}）：检查 {ENV_API_KEY} 与网关鉴权") from exc
            if exc.code in (400, 422):
                raise LLMBadRequest(f"请求被拒绝（HTTP {exc.code}）") from exc
            if exc.code == 429 or 500 <= exc.code < 600:
                raise LLMUnavailable(f"服务端不可用/限流（HTTP {exc.code}）") from exc
            raise LLMError(f"请求失败（HTTP {exc.code}）") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMUnavailable(f"无法连接模型服务 {self.base_url}: {exc}") from exc

        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise LLMBadResponse("响应不是合法 JSON") from exc

    def _parse(self, data: dict) -> ChatResult:
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMBadResponse(f"响应缺少 choices[0].message: {redact(str(data))[:200]}") from exc
        if not isinstance(message, dict):
            raise LLMBadResponse(f"choices[0].message 不是对象: {redact(str(message))[:120]}")

        content = _flatten_content(message.get("content"))
        tool_calls = _parse_tool_calls(message.get("tool_calls"))
        if not content.strip() and not tool_calls:
            raise LLMBadResponse("响应既没有 content 也没有 tool_calls")
        usage = data.get("usage") or {}
        return ChatResult(
            content=content,
            model=data.get("model", self.model),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            raw=data,
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason") if isinstance(choice, dict) else None,
        )


def _flatten_content(content) -> str:
    """把 content 归一化成字符串。

    部分网关返回 ``[{"type":"text","text":"..."}]`` 的分段形式；以前这种响应
    会被判成"缺少 content"，把一次成功的调用变成失败。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return str(content)


def _parse_tool_calls(raw) -> tuple[ToolCall, ...]:
    """解析 tool_calls；形状不对的条目**跳过**（模型输出不受我们控制）。"""
    if not isinstance(raw, list):
        return ()
    calls: list[ToolCall] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments or {}, ensure_ascii=False)
        calls.append(ToolCall(id=str(item.get("id") or ""), name=name.strip(), arguments=arguments))
    return tuple(calls)


def probe_connectivity(client: OpenAICompatClient) -> tuple[bool, str]:
    """连通性探测（供 ``doctor`` 用）：不发真实业务请求，只打一个极短对话。

    失败返回 ``(False, 原因)`` —— 原因已脱敏，可以直接进产物与终端。
    """
    try:
        client.chat([ChatMessage(role="user", content="ping")], temperature=0.0)
    except LLMAuthError as exc:
        return False, f"认证失败：{exc}"
    except LLMUnavailable as exc:
        return False, f"服务不可达：{exc}"
    except LLMError as exc:
        return False, f"调用失败：{exc}"
    return True, f"可达（model={client.model}）"


__all__ = [
    "ChatMessage",
    "ChatResult",
    "ENV_API_KEY",
    "LLMAuthError",
    "LLMBadRequest",
    "LLMBadResponse",
    "LLMError",
    "LLMUnavailable",
    "OpenAICompatClient",
    "ToolCall",
    "probe_connectivity",
    "resolve_base_url",
    "validate_base_url",
]
