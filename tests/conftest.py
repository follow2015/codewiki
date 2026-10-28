"""测试夹具与共享工具。

scan-secrets: fixtures-allowed —— 本文件含故意写出的假密钥样本，用途是验证扫描器与脱敏。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codewiki.ai.client import ChatResult, OpenAICompatClient

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = Path(__file__).parent / "golden"


@pytest.fixture
def mini_py() -> Path:
    """最小 Python 夹具仓库（含 re-export / 调用 / 继承 / 常量）。"""
    return FIXTURES / "mini_py"


def parse_all(root: Path):
    """解析一个目录下的全部源文件（测试专用便捷入口）。"""
    from codewiki.analyzers import REGISTRY, register_defaults
    from codewiki.repo import iter_source_files

    register_defaults()
    models = []
    for source in iter_source_files(root):
        analyzer = REGISTRY.get(source.language, "auto")
        models.append(analyzer.parse(Path(source.path), source.content))
    return models


class FakeClient(OpenAICompatClient):
    """测试用假客户端：按顺序返回预设回复（``Exception`` 实例则表示抛错）。

    为什么要走"注入客户端"而不是真的起一个 HTTP stub：客户端的失败分类
    （认证 / 不可达 / 响应坏）才是要测的东西，起 stub 只会把断言推到 HTTP 层，
    反而测不到分类。

    :attr:`supports_native_tools` 置 False：agent 的用例都在验**文本协议**
    （一行一个 JSON 决定），原生 tool_calls 的行为由 ``test_ai_client`` 单独锁。
    置 True 会让既有用例的请求形状悄悄变掉，却看不出新能力是否真的被用到。
    """

    supports_native_tools = False

    def __init__(self, replies, *, model: str = "fake-model") -> None:
        super().__init__(model=model, api_key="test-key", base_url="http://localhost/v1")
        self.replies = list(replies)
        self.calls: list[list] = []
        self.extensions: list[dict] = []

    def chat(
        self,
        messages,
        *,
        temperature: float = 0.2,
        tools=None,
        response_format=None,
        tool_choice=None,
    ) -> ChatResult:
        self.calls.append(list(messages))
        self.extensions.append(
            {"tools": tools, "response_format": response_format, "tool_choice": tool_choice}
        )
        if not self.replies:
            return ChatResult(content="", model=self.model)
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return ChatResult(content=str(item), model=self.model, prompt_tokens=10, completion_tokens=20)


@pytest.fixture
def fake_client_factory():
    def _make(replies, **kwargs):
        return FakeClient(replies, **kwargs)

    return _make
