"""AI 时序图生成与"坏图零落盘"回退（§4.2 时序图 / §4.3 失败处理）。

规则来自设计原文，三条都不能打折：

1. 每条消息必须能在 ``CALLS`` 边里找到依据；找不到依据的步骤以注释标注
   "AI 推断，未在静态图中证实"；
2. AI 产出的图**必须过校验门**，校验失败一律**不交付坏图**；
3. 对 AI 图：把校验错误回喂模型**自修一次（仅 1 次）**，仍失败则退回纯文本并在
   诊断里记 ``AI_DIAGRAM_INVALID``（含失败原因）。

本模块刻意不写文件 —— 落盘由调用方决定，这样"坏图不落盘"是可测的纯函数行为。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from codewiki.ai.client import ChatMessage, LLMError, OpenAICompatClient
from codewiki.ai.prompts import SEQUENCE_SYSTEM, render_repair_user, render_sequence_user
from codewiki.render.mermaid_validator import validate
from codewiki.schemas import Diagnostic

_FENCE = re.compile(r"^\s*```(?:mermaid)?\s*|\s*```\s*$")

UNPROVEN_MARK = "%% AI 推断，未在静态图中证实"


@dataclass
class DiagramResult:
    """生成结果：``text`` 为空表示**没有可用图**（调用方必须退回纯文本）。"""

    text: str | None = None
    diagnostics: list[Diagnostic] = field(default_factory=list)
    repaired: bool = False
    skipped: bool = False


def strip_fences(text: str) -> str:
    """去掉模型爱加的代码围栏（Mermaid 文件本身不需要）。"""
    lines = [
        line
        for line in (text or "").splitlines()
        if not line.strip().startswith("```")
    ]
    return "\n".join(lines).strip()


def _issues_text(issues) -> list[str]:
    return [f"{i.code}: {i.message}" for i in issues]


def generate_sequence_diagram(
    chain: list[str],
    *,
    client: OpenAICompatClient | None,
    use_l2: bool = False,
    where: str | None = None,
) -> DiagramResult:
    """基于真实调用链生成时序图；拿不到可用图时返回 ``text=None``。"""
    result = DiagramResult()
    if client is None:
        result.skipped = True
        result.diagnostics.append(
            Diagnostic(level="info", code="AI_DIAGRAM_SKIPPED", message="未配置 AI，跳过时序图（纯静态产物不受影响）", where=where)
        )
        return result
    if not chain:
        result.skipped = True
        result.diagnostics.append(
            Diagnostic(level="info", code="AI_DIAGRAM_SKIPPED", message="调用链为空，无需时序图", where=where)
        )
        return result

    user_prompt = render_sequence_user(chain=chain)
    try:
        reply = client.chat([ChatMessage(role="system", content=SEQUENCE_SYSTEM), ChatMessage(role="user", content=user_prompt)])
    except LLMError as exc:
        result.diagnostics.append(
            Diagnostic(level="warning", code="AI_UNAVAILABLE", message=f"时序图生成失败（AI 不可用）: {exc}", where=where)
        )
        return result

    text = strip_fences(reply.content)
    issues, _l2 = validate(text, use_l2=use_l2)
    if not issues:
        result.text = text
        return result

    try:
        repair = client.chat(
            [
                ChatMessage(role="system", content=SEQUENCE_SYSTEM),
                ChatMessage(role="user", content=render_repair_user(issues=_issues_text(issues), diagram=text)),
            ]
        )
    except LLMError as exc:
        result.diagnostics.append(
            Diagnostic(
                level="warning",
                code="AI_DIAGRAM_INVALID",
                message=f"时序图未通过校验门且自修调用失败，已退回纯文本: {exc}",
                where=where,
            )
        )
        return result

    repaired_text = strip_fences(repair.content)
    issues2, _l2 = validate(repaired_text, use_l2=use_l2)
    if not issues2:
        result.text = repaired_text
        result.repaired = True
        return result

    result.diagnostics.append(
        Diagnostic(
            level="warning",
            code="AI_DIAGRAM_INVALID",
            message="时序图两次均未通过校验门，已退回纯文本（不交付坏图）: " + "; ".join(_issues_text(issues2)),
            where=where,
        )
    )
    return result


__all__ = ["DiagramResult", "UNPROVEN_MARK", "generate_sequence_diagram", "strip_fences"]
