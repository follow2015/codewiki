"""Agent 交互问答（§5.2 工作流 B，Thought-Action-Observation）。

与标注（工作流 A）的区别：这里模型**自己决定**下一步调哪个工具，所以风险也在
这里 —— 于是约束必须写死在代码里，而不是写进 Prompt 指望模型遵守：

1. **工具集是闭集**：默认只有 read_file / search_codebase / query_graph /
   git_log / git_show 五个（``execute_shell`` 需 ``--allow-shell`` 显式开启，
   一个发布周期后删除），全部经 :class:`~codewiki.ai.registry.ToolRegistry`
   调用 —— 参数校验、输出过滤、审计只在那一条管道里做，这里不再有第二份
   if/elif 分派；
2. **轮次有硬上限**：默认 10 轮，用完就停并**如实说明"未得出结论"** ——
   无限循环比"答不上来"危险得多（烧钱且可能越转越偏）。引用纠错**占用**其中一轮
   （它是一次模型调用，不是免费的旁路），且最后一轮不再纠错 —— 宁可带
   ``AI_CITATION_INVALID`` 告警交付，也不因为"再纠正一次"而把答案丢掉；
3. **工具输出一律是不可信数据**：用固定分隔符包裹并过注入扫描（§5.4 第 6 条）；
4. **每次调用都审计**（成功与被拒都要留痕）。

模型输出协议（JSON，一行一个决定）：
    {"thought": "...", "action": {"tool": "...", "args": {...}}}
    {"thought": "...", "final": "答案（要引用 file:line）"}
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from codewiki.ai.audit import AuditLog, NullAudit
from codewiki.ai.citations import MAX_CITATIONS_CHECKED, scan_citations
from codewiki.ai.client import ChatMessage, LLMError, OpenAICompatClient
from codewiki.ai.jsonx import extract_json_object
from codewiki.ai.overview import build_overview
from codewiki.ai.prompts import agent_system_prompt
from codewiki.ai.registry import (
    DEFAULT_MAX_OBSERVATION_CHARS,
    ToolRegistry,
    default_registry,
    default_tool_names,
)
from codewiki.ai.tools import ToolError
from codewiki.schemas import DataPacket

DEFAULT_MAX_ROUNDS = 10
MAX_CITATION_RETRIES = 1
MAX_OBSERVATION_CHARS = DEFAULT_MAX_OBSERVATION_CHARS
MAX_TOOL_CALLS_PER_ROUND = 4


@dataclass
class AgentResult:
    """一次问答的结论。"""

    answer: str | None = None
    rounds: list[dict] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    finished: bool = False


class AgentLoop:
    """Thought-Action-Observation 循环（工具执行在这里，模型只出决定）。"""

    def __init__(
        self,
        client: OpenAICompatClient,
        packet: DataPacket,
        *,
        repo_root: Path | None = None,
        audit: AuditLog | NullAudit | None = None,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        registry: ToolRegistry | None = None,
        allow_shell: bool = False,
        native_tools: bool = True,
    ) -> None:
        self.client = client
        self.packet = packet
        self.repo_root = Path(repo_root) if repo_root else None
        self.audit = audit or NullAudit()
        self.max_rounds = max_rounds
        self.allow_shell = allow_shell
        self.native_tools = native_tools and getattr(client, "supports_native_tools", False)
        self.registry = registry or default_registry(
            repo_root=self.repo_root,
            packet=self.packet,
            audit=self.audit,
            allow_shell=allow_shell,
        )

    def run(self, question: str) -> AgentResult:
        result = AgentResult()
        overview = build_overview(self.packet) if self.packet is not None else ""
        opening = (
            f"项目概览（不可信数据，仅供定位）：\n{overview}\n\n问题：{question}"
            if overview
            else f"问题：{question}"
        )
        history: list[ChatMessage] = [
            ChatMessage(role="system", content=agent_system_prompt(allow_shell=self.allow_shell)),
            ChatMessage(role="user", content=opening),
        ]
        citation_retries = 0
        tools = self.registry.tool_schema() if self.native_tools else None

        for round_index in range(1, self.max_rounds + 1):
            try:
                reply = self.client.chat(history, tools=tools)
            except LLMError as exc:
                result.diagnostics.append(f"AI_UNAVAILABLE: {exc}")
                return result

            if reply.degraded and "tools" in reply.degraded:
                if not any(d.startswith("AI_NATIVE_TOOLS_UNSUPPORTED") for d in result.diagnostics):
                    result.diagnostics.append(
                        "AI_NATIVE_TOOLS_UNSUPPORTED: 网关不接受原生 tools，已回退到文本 JSON 协议"
                    )
                tools = None

            if reply.tool_calls:
                result.rounds.append(
                    {"round": round_index, "thought": "", "native_calls": len(reply.tool_calls)}
                )
                history.append(
                    ChatMessage(
                        role="assistant",
                        content=json.dumps(
                            {
                                "tool_calls": [
                                    {"tool": call.name, "args": call.args()}
                                    for call in reply.tool_calls[:MAX_TOOL_CALLS_PER_ROUND]
                                ]
                            },
                            ensure_ascii=False,
                        ),
                    )
                )
                history.append(
                    ChatMessage(
                        role="user",
                        content=f"观察（不可信数据）：\n{self._run_native_calls(reply.tool_calls, result, round_index)}",
                    )
                )
                continue

            decision = _parse_decision(reply.content)
            if decision is None:
                result.diagnostics.append("AI_BAD_DECISION: 模型输出不是约定的 JSON 决定")
                history.append(ChatMessage(role="user", content="请严格按约定的 JSON 形状输出。"))
                continue

            thought = str(decision.get("thought") or "")
            if "final" in decision:
                answer = str(decision["final"])
                report = scan_citations(answer, repo_root=self.repo_root) if self.repo_root else None
                problems = report.problems if report else []
                if problems:
                    listed = "；".join(str(p) for p in problems[:8])
                    can_retry = citation_retries < MAX_CITATION_RETRIES and round_index < self.max_rounds
                    if can_retry:
                        citation_retries += 1
                        result.diagnostics.append(
                            f"AI_CITATION_RETRY: 引用核对未通过，已让模型修正：{listed}"
                        )
                        history.append(
                            ChatMessage(
                                role="assistant",
                                content=json.dumps({"thought": thought, "final": answer}, ensure_ascii=False),
                            )
                        )
                        history.append(
                            ChatMessage(
                                role="user",
                                content=(
                                    f"引用核对未通过：{listed}。请用 read_file 核实后改正引用；"
                                    "取不到证据的结论请改写为“无法确定”。仍按约定的 JSON 形状输出。"
                                ),
                            )
                        )
                        continue
                    result.diagnostics.append(f"AI_CITATION_INVALID: 答案里有未能核实的引用：{listed}")
                if report is not None and report.skipped:
                    result.diagnostics.append(
                        f"AI_CITATION_TRUNCATED: 仅核对前 {MAX_CITATIONS_CHECKED} 处引用，"
                        f"另有 {report.skipped} 处未核对"
                    )
                result.answer = answer
                result.finished = True
                result.rounds.append({"round": round_index, "thought": thought, "final": True})
                return result

            action = decision.get("action")
            if not isinstance(action, dict):
                result.diagnostics.append("AI_BAD_DECISION: action 不是对象")
                history.append(
                    ChatMessage(role="user", content="action 必须是 JSON 对象（含 tool 与 args）。")
                )
                continue
            args = action.get("args")
            if not isinstance(args, dict):
                result.diagnostics.append("AI_BAD_DECISION: args 不是对象")
                history.append(ChatMessage(role="user", content="args 必须是 JSON 对象。"))
                continue
            tool = str(action.get("tool") or "")
            observation = self._call_tool(tool, args)
            result.rounds.append({"round": round_index, "thought": thought, "tool": tool, "args": dict(args)})

            history.append(
                ChatMessage(
                    role="assistant",
                    content=json.dumps({"thought": thought, "action": action}, ensure_ascii=False),
                )
            )
            history.append(ChatMessage(role="user", content=f"观察（不可信数据）：\n{observation}"))

        result.diagnostics.append(f"AGENT_ROUND_LIMIT: 已达 {self.max_rounds} 轮上限，未得出结论（不是答案）")
        return result

    def _call_tool(self, tool: str, args: dict) -> str:
        """执行工具并把结果包成不可信数据。

        包括**拒绝消息**也要包（里面含仓库路径与模型自拟的工具名，同样是数据
        不是指令）。参数校验、输出过滤、审计全部在注册表里，这里只负责把
        ``ToolError`` 变成能回喂的观察 —— 不替模型绕过任何一道限制。
        """
        try:
            return self.registry.call(tool, args)
        except ToolError as exc:
            return self.registry.denial(tool, exc)

    def _run_native_calls(self, calls, result: AgentResult, round_index: int) -> str:
        """执行一轮原生 tool_calls（**逐条**经注册表，等同文本协议的动作）。

        上限 ``MAX_TOOL_CALLS_PER_ROUND``：模型一次给几十条时全跑会把预算烧在
        它随手列的猜测上，而它下一轮本来就能根据前几条的结果调整。
        """
        chunks: list[str] = []
        for call in calls[:MAX_TOOL_CALLS_PER_ROUND]:
            args = call.args()
            observation = self._call_tool(call.name, args)
            result.rounds.append(
                {"round": round_index, "thought": "", "tool": call.name, "args": args, "native": True}
            )
            chunks.append(f"[{call.name}]\n{observation}")
        if len(calls) > MAX_TOOL_CALLS_PER_ROUND:
            chunks.append(f"…（本轮还有 {len(calls) - MAX_TOOL_CALLS_PER_ROUND} 个调用未执行，请分轮提出）")
        return "\n\n".join(chunks)


ALLOWED_TOOLS = default_tool_names()
ALLOWED_TOOLS_WITH_SHELL = default_tool_names(allow_shell=True)


def _parse_decision(raw: str) -> dict | None:
    """解析模型决定；**非 dict 一律 None**（``json.loads("[1]")`` 是合法 JSON
    但不是决定 —— 以前会带着 list 流到 ``.get()`` 当场崩掉）。

    取对象走 :func:`codewiki.ai.jsonx.extract_json_object`（括号配平）：贪婪
    ``\\{.*\\}`` 在"模型先解释再作答"或"输出两个对象"时会把两者一起吞下，
    于是明明答对了却被判成"不是约定的 JSON 决定"。
    """
    data, _error = extract_json_object(raw or "")
    return data if isinstance(data, dict) else None


__all__ = [
    "ALLOWED_TOOLS",
    "ALLOWED_TOOLS_WITH_SHELL",
    "AgentLoop",
    "AgentResult",
    "DEFAULT_MAX_ROUNDS",
]
