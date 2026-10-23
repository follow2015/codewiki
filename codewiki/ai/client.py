"""OpenAI 兼容客户端（§5.3）。

三条硬约束：

1. **零新增依赖**：用标准库 ``urllib`` 直连 `/chat/completions`，不为「调一次
   HTTP」引入 httpx/openai SDK（那会把离线安装与 wheel 矩阵一起拖下水）；
2. **密钥不落盘、不写日志、不进产物**（§5.3）：只从环境变量读，任何异常消息
   都要过 :func:`codewiki.ai.audit.redact`；
3. **失败要可分类**：认证明败 / 不可达 / 响应不可用是三种后果 —— 认证错继续
   重试是浪费，不可达直接放弃会让一次抖动毁掉整轮。

重试只针对 429 / 5xx，指数退避；其它 4xx 直接失败（§5.5）。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from codewiki.ai.audit import redact

ENV_API_KEY = "OPENAI_API_KEY"
ENV_BASE_URL = "OPENAI_BASE_URL"
DEFAULT_BASE_URL = "https://api.openai.com/v1"


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

    @property
    def total_tokens(self) -> int:
        return (self.prompt_tokens or 0) + (self.completion_tokens or 0)


class OpenAICompatClient:
    """OpenAI 兼容 Chat Completions 客户端（可指向 Ollama / vLLM 内网网关）。"""

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
        self.base_url = (base_url or os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key or os.environ.get(ENV_API_KEY) or ""
        self.model = model
        self.timeout_s = timeout_s
        self.max_retries = max(0, max_retries)
        self.backoff_base_s = backoff_base_s
        self._transport = transport

    def chat(self, messages: list[ChatMessage], *, temperature: float = 0.2) -> ChatResult:
        """发一次对话；失败抛 :class:`LLMError` 的子类。"""
        payload = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
        }
        attempt = 0
        while True:
            try:
                data = self._post(payload)
                break
            except LLMUnavailable:
                if attempt >= self.max_retries:
                    raise
                attempt += 1
                time.sleep(self.backoff_base_s * (2 ** (attempt - 1)))
        return self._parse(data)

    def _post(self, payload: dict) -> dict:
        if self._transport is not None:
            return self._transport(payload)
        request = urllib.request.Request(  # noqa: S310 - 仅 https/http，base_url 由配置给定
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
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMBadResponse(f"响应缺少 choices[0].message.content: {redact(str(data))[:200]}") from exc
        usage = data.get("usage") or {}
        return ChatResult(
            content=content or "",
            model=data.get("model", self.model),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            raw=data,
        )


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
    "LLMBadResponse",
    "LLMError",
    "LLMUnavailable",
    "OpenAICompatClient",
    "probe_connectivity",
]
