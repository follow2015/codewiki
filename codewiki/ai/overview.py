"""项目概览：Agent 的第一条上下文。

数据包里已经有分层、热点模块和人工注解，但此前 Agent 开局只收到一句"问题：…"，
等于让模型对着一个陌生仓库从零 grep。把这些**已经算好**的事实在第一轮就给它，
能省掉头几轮"先摸一摸仓库长什么样"的工具调用，答案也更贴合项目术语。

全部来自数据包（不读文件系统）。内容里的模块名、层名、注解来自被分析仓库与它的
``codewiki.toml``，所以**同样是不可信数据**：截断后用同一份 ``wrap_untrusted``
包裹（含分隔符中性化），并过 model 档脱敏。
"""

from __future__ import annotations

from typing import Any

from codewiki.ai.graph_tools import module_degrees, ranked_modules
from codewiki.ai.guard import redact_model
from codewiki.ai.tools import wrap_untrusted

DEFAULT_TOP_N = 12
DEFAULT_MAX_CHARS = 3000
MAX_ANNOTATIONS = 12
MAX_NOTE_CHARS = 160


def build_overview(
    packet: Any,
    *,
    top_n: int = DEFAULT_TOP_N,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """生成项目概览文本（已包裹为不可信数据；空数据包返回空串）。"""
    modules = ranked_modules(packet)
    if not modules and not packet.annotations:
        return ""

    degrees = module_degrees(packet)
    layer_names = {layer.id: layer.name for layer in packet.layers}

    lines: list[str] = [f"项目：{packet.meta.title}", f"模块数：{len(modules)}"]

    counts: dict[str, int] = {}
    for node in modules:
        key = layer_names.get(node.layer_id or "", "（未分层）")
        counts[key] = counts.get(key, 0) + 1
    if counts:
        ordered_layers = [layer.name for layer in packet.layers if layer.name in counts]
        ordered_layers += [name for name in counts if name not in ordered_layers]
        lines.append("分层（模块数）：" + "；".join(f"{name} {counts[name]}" for name in ordered_layers))

    if modules:
        lines.append(f"热点模块（按依赖度数，前 {min(top_n, len(modules))}）：")
        for node in modules[:top_n]:
            layer = layer_names.get(node.layer_id or "", "（未分层）")
            lines.append(f"- {node.id} | {layer} | 度数 {degrees.get(node.id, 0)}")

    trusted = [a for a in packet.annotations if a.source == "human" or a.reviewed]
    if trusted:
        lines.append("已有注解（沿用其术语）：")
        for ann in trusted[:MAX_ANNOTATIONS]:
            note = " ".join(ann.note.split())
            if len(note) > MAX_NOTE_CHARS:
                note = note[:MAX_NOTE_CHARS] + "…"
            label = f"［{ann.label}］" if ann.label else ""
            lines.append(f"- {ann.target} {label}{note}")

    lines.append(
        "提示：用 list_modules / find_symbol 发现节点，再用 query_graph 看上下游，"
        "最后 read_file 取证；结论要引用 file:line。"
    )

    body = redact_model("\n".join(lines))
    if len(body) > max_chars:
        body = body[:max_chars] + "\n…[概览已截断]"
    return wrap_untrusted(body, origin="project_overview")


__all__ = ["DEFAULT_MAX_CHARS", "DEFAULT_TOP_N", "build_overview"]
