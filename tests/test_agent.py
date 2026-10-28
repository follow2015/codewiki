"""Agent 问答判据（§5.2 工作流 B）。

这一层模型**自己决定**调什么工具，所以判据盯的是"约束有没有真的生效"：

1. **轮次上限是硬的**：模型一直要工具时，必须在 N 轮停下并如实说未得出结论
   （无限循环比答不上来危险）；
2. **危险命令到不了子进程**：模型要 `rm -rf /`，我们拒绝并把"被拒"作为观察
   回给它（不是崩，也不是放行）；
3. **工具输出是不可信数据**：观察必须被固定分隔符包裹；
4. **每次工具调用都审计**（成功与被拒都留痕）。

用 FakeClient 逐轮喂决定，不需要真模型。
"""

from __future__ import annotations

import json

from codewiki.ai.agent import ALLOWED_TOOLS, ALLOWED_TOOLS_WITH_SHELL, AgentLoop
from codewiki.ai.audit import AuditLog
from codewiki.ai.client import ChatResult, LLMUnavailable
from codewiki.ai.tools import DELIM_BEGIN
from codewiki.graph import build_graph
from codewiki.render.jsonio import packet_from_parts
from tests.conftest import FakeClient


def _packet(mini_py):
    from tests.conftest import parse_all

    result = build_graph(parse_all(mini_py))
    return packet_from_parts(
        title="mini", layers=[], nodes=result.nodes, edges=result.edges, annotations=[], diagnostics=[]
    )


def _act(tool: str, **args) -> str:
    return json.dumps({"thought": f"调用 {tool}", "action": {"tool": tool, "args": args}})


def _final(answer: str) -> str:
    return json.dumps({"thought": "够了", "final": answer})


def test_agent_uses_tools_then_answers(mini_py, tmp_path):
    client = FakeClient(
        [
            _act("search_codebase", keyword="class Service"),
            _act("read_file", path="pkg/core.py"),
            _final("Service.run 见 pkg/core.py:9"),
        ]
    )
    loop = AgentLoop(client, _packet(mini_py), repo_root=mini_py, audit=AuditLog(tmp_path / "a.jsonl"))
    result = loop.run("Service.run 做什么？")

    assert result.finished and result.answer and "pkg/core.py:9" in result.answer
    assert [r["tool"] for r in result.rounds[:2]] == ["search_codebase", "read_file"]
    assert result.rounds[-1]["final"] is True


def test_observation_is_wrapped_as_untrusted_data(mini_py, tmp_path):
    """工具输出必须包进固定分隔符 —— 这是提示词注入防护的第一道。"""
    client = FakeClient([_act("read_file", path="pkg/core.py"), _final("done")])
    AgentLoop(client, _packet(mini_py), repo_root=mini_py, audit=AuditLog(tmp_path / "a.jsonl")).run("q")

    second_user_message = client.calls[1][-1].content
    assert DELIM_BEGIN in second_user_message, "观察必须标明是不可信数据"


def test_dangerous_command_is_refused_and_fed_back(mini_py, tmp_path):
    """模型要 `rm -rf /` ⇒ 拒绝并作为观察回传，不崩也不放行。

    显式 ``allow_shell=True``：这条测的是 **sandbox 白名单**这一层的拒绝。
    默认闭集里根本没有 shell，若不开，测到的只是"未知工具"—— 看起来同样被拒，
    实际 sandbox 那层一次都没跑到。
    """
    client = FakeClient([_act("execute_shell", command="rm -rf /"), _final("放弃")])
    loop = AgentLoop(
        client,
        _packet(mini_py),
        repo_root=mini_py,
        audit=AuditLog(tmp_path / "a.jsonl"),
        allow_shell=True,
    )
    result = loop.run("清一下目录")

    assert result.answer == "放弃"
    feedback = client.calls[1][-1].content
    assert "工具调用被拒绝" in feedback, "拒绝要如实告知模型，让它换路子"


def test_round_limit_stops_and_says_so(mini_py, tmp_path):
    """轮次上限是硬的：一直要工具就停手，并明确"未得出结论"。"""
    client = FakeClient([_act("search_codebase", keyword="x") for _ in range(20)])
    loop = AgentLoop(
        client, _packet(mini_py), repo_root=mini_py, audit=AuditLog(tmp_path / "a.jsonl"), max_rounds=3
    )
    result = loop.run("q")

    assert result.answer is None and not result.finished
    assert len(client.calls) == 3, "不得超过轮次上限"
    assert any("AGENT_ROUND_LIMIT" in d for d in result.diagnostics), "要说清为什么没有答案"
    assert len(result.rounds) == 3


def test_unknown_tool_is_reported_not_executed(mini_py, tmp_path):
    client = FakeClient([_act("write_file", path="x.py"), _final("done")])
    loop = AgentLoop(client, _packet(mini_py), repo_root=mini_py, audit=AuditLog(tmp_path / "a.jsonl"))
    result = loop.run("q")

    assert result.answer == "done"
    assert "未知工具" in client.calls[1][-1].content
    assert set(ALLOWED_TOOLS) == {
        "read_file",
        "search_codebase",
        "query_graph",
        "list_modules",
        "find_symbol",
        "git_log",
        "git_show",
    }
    assert set(ALLOWED_TOOLS_WITH_SHELL) == {
        "read_file",
        "search_codebase",
        "query_graph",
        "list_modules",
        "find_symbol",
        "git_log",
        "git_show",
        "execute_shell",
    }


def test_bad_decision_is_diagnosed(mini_py, tmp_path):
    client = FakeClient(["这不是 JSON", _final("done")])
    result = AgentLoop(client, _packet(mini_py), repo_root=mini_py, audit=AuditLog(tmp_path / "a.jsonl")).run(
        "q"
    )

    assert result.answer == "done"
    assert any("AI_BAD_DECISION" in d for d in result.diagnostics)


def test_ai_unavailable_is_reported(mini_py, tmp_path):
    client = FakeClient([LLMUnavailable("down")])
    result = AgentLoop(client, _packet(mini_py), repo_root=mini_py, audit=AuditLog(tmp_path / "a.jsonl")).run(
        "q"
    )

    assert result.answer is None
    assert any("AI_UNAVAILABLE" in d for d in result.diagnostics)


def test_tool_calls_are_audited(mini_py, tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    client = FakeClient(
        [
            _act("read_file", path="pkg/core.py"),
            _act("execute_shell", command="rm -rf /"),
            _final("done"),
        ]
    )
    with AuditLog(audit_path) as audit:
        AgentLoop(client, _packet(mini_py), repo_root=mini_py, audit=audit, allow_shell=True).run("q")

    outcomes = [json.loads(line)["outcome"] for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert outcomes == ["ok", "denied"], "成功与被拒都要留痕"


def test_query_graph_works_without_src(mini_py, tmp_path):
    """没有 --src 时仍可问答（只能靠图谱），不能崩。"""
    client = FakeClient([_act("query_graph", node_id="mod::pkg/core.py", direction="up"), _final("ok")])
    result = AgentLoop(client, _packet(mini_py), repo_root=None, audit=AuditLog(tmp_path / "a.jsonl")).run(
        "q"
    )

    assert result.answer == "ok"


def test_read_file_without_src_is_refused(mini_py, tmp_path):
    client = FakeClient([_act("read_file", path="pkg/core.py"), _final("ok")])
    result = AgentLoop(client, _packet(mini_py), repo_root=None, audit=AuditLog(tmp_path / "a.jsonl")).run(
        "q"
    )

    assert result.answer == "ok"
    assert "未提供 --src" in client.calls[1][-1].content


def test_denied_tool_output_is_also_wrapped(mini_py, tmp_path):
    """拒绝反馈也含仓库路径/模型自拟内容 —— 同样是不可信数据，必须包裹。

    ``allow_shell=True`` 让这条走到 sandbox 拒绝（而不是"未知工具"）——
    两种拒绝的文案不同，包裹的是哪一条要明确。
    """
    client = FakeClient([_act("execute_shell", command="rm -rf /"), _final("放弃")])
    AgentLoop(
        client, _packet(mini_py), repo_root=mini_py, audit=AuditLog(tmp_path / "a.jsonl"), allow_shell=True
    ).run("q")
    assert DELIM_BEGIN in client.calls[1][-1].content, "拒绝消息也要按不可信数据包裹"


def test_unknown_tool_output_is_also_wrapped(mini_py, tmp_path):
    """未知工具提示里含模型自拟的工具名 —— 也是不可信数据。"""
    client = FakeClient([_act("write_file", path="x.py"), _final("done")])
    AgentLoop(client, _packet(mini_py), repo_root=mini_py, audit=AuditLog(tmp_path / "a.jsonl")).run("q")
    assert DELIM_BEGIN in client.calls[1][-1].content, "未知工具提示也要按不可信数据包裹"


def test_agent_cli_audits_even_without_out(tmp_path, monkeypatch, mini_py):
    """agent 不给 --out 时也要落审计（此前是 NullAudit，全程无痕）。"""
    from pathlib import Path

    import codewiki.cli as cli_mod
    import codewiki.cli.agent_cmd as agent_cmd
    from codewiki.ai.audit import AUDIT_FILENAME
    from tests.conftest import FakeClient

    monkeypatch.setattr(
        agent_cmd,
        "OpenAICompatClient",
        lambda **_kw: FakeClient([_final("答案 A")]),
    )
    out = tmp_path / "wiki-output"
    assert cli_mod.main(["analyze", "--src", str(mini_py), "--out", str(out), "--no-cache"]) == 0
    packet = out / "data" / "codewiki.json"
    code = cli_mod.main(["agent", "--input", str(packet), "--model", "fake", "q"])
    assert code == 0
    assert (out / AUDIT_FILENAME).exists(), (
        f"无 --out 也必须有审计文件: {sorted(p.name for p in Path(out).iterdir())}"
    )


class _NativeClient(FakeClient):
    """支持原生 tools 的假客户端：按顺序回放（``ChatResult`` 原样返回）。"""

    supports_native_tools = True

    def __init__(self, replies):
        super().__init__(replies)
        self.tool_payloads: list = []

    def chat(self, messages, *, temperature=0.2, tools=None, response_format=None, tool_choice=None):
        self.tool_payloads.append(tools)
        self.calls.append(list(messages))
        if not self.replies:
            return ChatResult(content="", model=self.model)
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, ChatResult):
            return item
        return ChatResult(content=str(item), model=self.model, prompt_tokens=10, completion_tokens=20)


def _tool_call(name: str, **args) -> ChatResult:
    from codewiki.ai.client import ToolCall

    return ChatResult(
        content="",
        model="fake-model",
        tool_calls=(ToolCall(id="c1", name=name, arguments=json.dumps(args)),),
        finish_reason="tool_calls",
        prompt_tokens=10,
        completion_tokens=20,
    )


def test_native_tools_are_sent_when_client_supports_them(mini_py, tmp_path):
    client = _NativeClient([_tool_call("read_file", path="pkg/core.py"), _final("见 pkg/core.py:11")])
    loop = AgentLoop(client, _packet(mini_py), repo_root=mini_py)
    result = loop.run("q")

    assert client.tool_payloads[0], "支持原生 tools 的客户端必须收到工具声明"
    names = {item["function"]["name"] for item in client.tool_payloads[0]}
    assert {"read_file", "query_graph", "list_modules"} <= names
    assert result.finished and result.answer


def test_text_protocol_client_is_not_sent_tool_schema(mini_py, tmp_path):
    """客户端声明不支持时一个原生字段都不该发（否则每轮白吃一次 400）。"""
    client = FakeClient([_final("ok")])
    AgentLoop(client, _packet(mini_py), repo_root=mini_py).run("q")
    assert client.extensions[0]["tools"] is None


def test_native_tool_call_observation_is_wrapped(mini_py, tmp_path):
    client = _NativeClient([_tool_call("read_file", path="pkg/core.py"), _final("见 pkg/core.py:11")])
    AgentLoop(client, _packet(mini_py), repo_root=mini_py).run("q")
    assert DELIM_BEGIN in client.calls[1][-1].content, "原生调用的观察同样是不可信数据"


def test_native_round_is_capped(mini_py, tmp_path):
    """一轮给一堆调用时不能全跑：上限之外的要明说"分轮提出"。"""
    from codewiki.ai.agent import MAX_TOOL_CALLS_PER_ROUND
    from codewiki.ai.audit import AuditLog
    from codewiki.ai.client import ToolCall

    many = ChatResult(
        content="",
        model="fake-model",
        tool_calls=tuple(
            ToolCall(id=f"c{i}", name="list_modules", arguments="{}")
            for i in range(MAX_TOOL_CALLS_PER_ROUND + 2)
        ),
    )
    client = _NativeClient([many, _final("ok")])
    loop = AgentLoop(client, _packet(mini_py), repo_root=mini_py, audit=AuditLog(tmp_path / "a.jsonl"))
    result = loop.run("q")

    executed = [r for r in result.rounds if r.get("native")]
    assert len(executed) == MAX_TOOL_CALLS_PER_ROUND
    assert "分轮提出" in client.calls[1][-1].content
