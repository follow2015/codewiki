"""模块上下文交互图（Module Context，§4.2.2）。

以单模块为中心：

* **上游（Left）**：以本模块为目标的 ``IMPORTS`` / ``CALLS`` 起点；
* **自身（Middle）**：本模块 + 其对外可见符号；
* **下游（Right）**：本模块发出的 ``IMPORTS`` / ``CALLS`` 目标。

节点 ID 与转义规则复用 :mod:`mermaid_macro`（同一套，避免两张图风格分叉）。

**两处规模闸门**（pydantic 实测：``json_schema`` 的图渲染成 1600×8622，约 5 屏高）：

1. 外部依赖按「标准库 / 第三方」各合成一个节点。它们与仓内依赖等权逐条画，会让
   ``__future__`` / ``os`` / ``re`` 这些和真正的上下游抢同样的视觉重量；
2. 符号叶子封顶（默认 12），其余折成一个"另有 N 个符号"节点 —— 数量必须写出来，
   读者才知道自己没看到什么。

刻意**不**折叠仓内依赖：那是这张图要回答的问题（"我依赖谁"），折了就没内容了。
"""

from __future__ import annotations

import sys

from codewiki.graph.index import GraphIndex
from codewiki.models import NodeType
from codewiki.render.i18n import t
from codewiki.render.mermaid_macro import DYNAMIC_EDGE, TYPE_ONLY_LABEL, escape_label, node_id
from codewiki.schemas import DataPacket

DEFAULT_MAX_SYMBOLS = 12

GROUP_NODE_ID = {
    "up": {"stdlib": "XU_STD", "third": "XU_3P"},
    "down": {"stdlib": "XD_STD", "third": "XD_3P"},
}
GROUP_LABEL_KEY = {"stdlib": "diagrams.ext_stdlib", "third": "diagrams.ext_third"}
GROUP_ORDER = ("stdlib", "third")

MORE_SYMBOLS_ID = "XMORE"

EXTERNAL_PREFIX = "ext::"


def render_module_context(
    packet: DataPacket,
    module_id: str,
    *,
    min_confidence: float = 0.5,
    index: GraphIndex | None = None,
    max_symbols: int | None = DEFAULT_MAX_SYMBOLS,
) -> str:
    """渲染某个模块的上下文图（返回 Mermaid 文本）。

    ``index`` 为空时临时建一份：**逐个模块渲染时必须复用同一个索引**，否则
    每个模块都扫描一遍全量边 ⇒ O(N × E)（5000 模块实测占到 40 秒）。

    ``max_symbols=None`` 关闭折叠（小图或调试时想看全量）。
    """
    nodes = (index or GraphIndex(packet)).nodes
    if module_id not in nodes:
        raise KeyError(f"模块不存在于数据包: {module_id}")

    idx = index or GraphIndex(packet)
    upstream = idx.dependency_edges(module_id, min_confidence=min_confidence, direction="up")
    downstream = idx.dependency_edges(module_id, min_confidence=min_confidence, direction="down")

    up_internal, up_groups = _partition_external(upstream)
    down_internal, down_groups = _partition_external(downstream)

    lines = ["flowchart LR"]

    lines.extend(_declare_internal(nodes, up_internal, side="U"))
    lines.extend(_declare_groups(up_groups, side="up"))

    center = nodes[module_id]
    center_label = escape_label(_shorten(center.path or center.id))
    lines.append(f'  M["{center_label}"]')

    lines.extend(_declare_internal(nodes, down_internal, side="D"))
    lines.extend(_declare_groups(down_groups, side="down"))

    lines.extend(_emit_internal_edges(up_internal, side="up"))
    lines.extend(_emit_group_edges(up_groups, side="up"))
    lines.extend(_emit_internal_edges(down_internal, side="down"))
    lines.extend(_emit_group_edges(down_groups, side="down"))

    if center.type == NodeType.MODULE:
        symbols = [
            n
            for n in packet.nodes
            if n.type == NodeType.SYMBOL and n.module_id == module_id and not (n.name or "").startswith("_")
        ]
        symbols.sort(key=lambda s: s.line_start or 0)
        shown = symbols if max_symbols is None else symbols[:max_symbols]
        for symbol in shown:
            lines.append(f'  {node_id(symbol.id)}("{escape_label(symbol.name or symbol.id)}")')
            lines.append(f"  M --- {node_id(symbol.id)}")
        hidden = len(symbols) - len(shown)
        if hidden > 0:
            label = escape_label(t("diagrams.symbols_more", count=hidden))
            lines.append(f'  {MORE_SYMBOLS_ID}["{label}"]')
            lines.append(f"  M --- {MORE_SYMBOLS_ID}")

    return "\n".join(lines) + "\n"


def _partition_external(entries: list[tuple[str, float, bool, bool]]):
    """把依赖拆成「仓内」与「外部两组」。

    外部只会出现在**下游**侧（"以我为目标"的边不可能来自一个外部模块），但上游
    也走同一套代码 —— 分组逻辑不该依赖调用方向这个隐含前提。
    """
    internal: list[tuple[str, float, bool, bool]] = []
    groups: dict[str, list[tuple[str, float, bool, bool]]] = {key: [] for key in GROUP_ORDER}
    for entry in entries:
        other = entry[0]
        if other.startswith(EXTERNAL_PREFIX):
            groups[_group_of(other)].append(entry)
        else:
            internal.append(entry)
    return internal, groups


def _group_of(external: str) -> str:
    """外部依赖归组：首段在标准库名单里算 stdlib，其余算第三方。

    这是**展示层**判断，不改数据模型：``sys.stdlib_module_names`` 对 TS/Go 的
    外部名永远不会命中，那些自然落到第三方组 —— 与"语言无关"的期望一致。
    """
    name = external[len(EXTERNAL_PREFIX) :]
    head = name.split(".")[0]
    return "stdlib" if head in sys.stdlib_module_names else "third"


def _declare_internal(nodes, entries, *, side: str) -> list[str]:
    out = []
    for position, (other, *_detail) in enumerate(entries):
        node = nodes.get(other)
        name = (node.path or node.name or other) if node else other
        out.append(f'  {side}{position}["{escape_label(_shorten(name))}"]')
    return out


def _declare_groups(groups: dict[str, list], *, side: str) -> list[str]:
    out = []
    for key in GROUP_ORDER:
        entries = groups[key]
        if not entries:
            continue
        label = escape_label(t(GROUP_LABEL_KEY[key], count=len(entries)))
        out.append(f'  {GROUP_NODE_ID[side][key]}["{label}"]')
    return out


def _emit_internal_edges(entries, *, side: str) -> list[str]:
    out = []
    for position, (_other, confidence, static, type_only) in enumerate(entries):
        if side == "up":
            head, tail = f"U{position}", "M"
        else:
            head, tail = "M", f"D{position}"
        if type_only:
            out.append(f"  {head} {DYNAMIC_EDGE}|{t(TYPE_ONLY_LABEL)}| {tail}")
            continue
        arrow = DYNAMIC_EDGE if not static else "-->"
        tag = "" if static else f"|{confidence:.2f}?|"
        out.append(f"  {head} {arrow}{tag} {tail}")
    return out


def _emit_group_edges(groups: dict[str, list], *, side: str) -> list[str]:
    """分组边：整组都是仅类型依赖时用虚线，否则实线。

    组内混合时按实线画（只要有一条是运行时依赖，这个包就真的被加载）；仅类型的
    数量不再单列 —— 节点标签已经写明总数，再拆一遍会把图重新撑开。
    """
    out = []
    for key in GROUP_ORDER:
        entries = groups[key]
        if not entries:
            continue
        node = GROUP_NODE_ID[side][key]
        if all(entry[3] for entry in entries):
            edge = f"{DYNAMIC_EDGE}|{t(TYPE_ONLY_LABEL)}|"
        else:
            edge = "-->"
        out.append(f"  {node} {edge} M" if side == "up" else f"  M {edge} {node}")
    return out


def _shorten(path: str) -> str:
    """长路径只保留末两段（图里放得下，完整路径在 Wiki 页）。"""
    parts = path.split("/")
    return "/".join(parts[-2:]) if len(parts) > 2 else path


__all__ = ["DEFAULT_MAX_SYMBOLS", "render_module_context"]
