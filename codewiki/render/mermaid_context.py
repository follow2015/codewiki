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

环上的边也**不**在这里标红：环是全局信息，而这张图按模块粒度做增量复用 —— 标了之后，
删掉一个文件让环消失时，未受影响模块的图会被原样复用，图上留着已经不存在的环
（``test_incremental_equals_full`` 实测红在 ``pkg_m1.mmd``）。环的可视化放在系统图与
层图，它们每轮都全量重渲。
"""

from __future__ import annotations

import sys

from codewiki.graph.index import GraphIndex
from codewiki.models import NodeType
from codewiki.render.i18n import t
from codewiki.render.mermaid_macro import (
    DYNAMIC_EDGE,
    TYPE_ONLY_LABEL,
    EdgeLine,
    emit_edges,
    escape_label,
    node_id,
)
from codewiki.schemas import DataPacket

DEFAULT_MAX_SYMBOLS = 12

DEFAULT_MAX_NEIGHBORS = 12

MAX_COLUMN_BOXES = 20

MIN_VISIBLE_SYMBOLS = 4

GROUP_NODE_ID = {
    "up": {"stdlib": "XU_STD", "third": "XU_3P"},
    "down": {"stdlib": "XD_STD", "third": "XD_3P"},
}
GROUP_LABEL_KEY = {"stdlib": "diagrams.ext_stdlib", "third": "diagrams.ext_third"}
GROUP_ORDER = ("stdlib", "third")

MORE_SYMBOLS_ID = "XMORE"

MORE_NEIGHBOURS_ID = {"up": "XU_MORE", "down": "XD_MORE"}
MORE_NEIGHBOURS_KEY = {"up": "diagrams.upstream_more", "down": "diagrams.downstream_more"}

EXTERNAL_PREFIX = "ext::"


def render_module_context(
    packet: DataPacket,
    module_id: str,
    *,
    min_confidence: float = 0.5,
    index: GraphIndex | None = None,
    max_symbols: int | None = DEFAULT_MAX_SYMBOLS,
    max_neighbors: int | None = DEFAULT_MAX_NEIGHBORS,
) -> str:
    """渲染某个模块的上下文图（返回 Mermaid 文本）。

    ``index`` 为空时临时建一份：**逐个模块渲染时必须复用同一个索引**，否则
    每个模块都扫描一遍全量边 ⇒ O(N × E)（5000 模块实测占到 40 秒）。

    ``max_symbols`` / ``max_neighbors`` 传 ``None`` 关闭折叠（小图或调试时想看全量）。
    """
    nodes = (index or GraphIndex(packet)).nodes
    if module_id not in nodes:
        raise KeyError(f"模块不存在于数据包: {module_id}")

    idx = index or GraphIndex(packet)
    upstream = idx.dependency_edges(module_id, min_confidence=min_confidence, direction="up")
    downstream = idx.dependency_edges(module_id, min_confidence=min_confidence, direction="down")

    up_internal, up_groups = _partition_external(upstream)
    down_internal, down_groups = _partition_external(downstream)
    up_internal, up_hidden = _cap_neighbors(up_internal, nodes, max_neighbors)
    down_internal, down_hidden = _cap_neighbors(down_internal, nodes, max_neighbors)

    lines = ["flowchart LR"]

    lines.extend(_declare_internal(nodes, up_internal, side="U"))
    lines.extend(_declare_groups(up_groups, side="up"))

    center = nodes[module_id]
    center_label = escape_label(_shorten(center.path or center.id))
    lines.append(f'  M["{center_label}"]')

    lines.extend(_declare_internal(nodes, down_internal, side="D"))
    lines.extend(_declare_groups(down_groups, side="down"))

    edge_entries: list[EdgeLine] = []
    edge_entries.extend(_internal_edge_entries(up_internal, side="up"))
    edge_entries.extend(_group_edge_entries(up_groups, side="up"))
    edge_entries.extend(_internal_edge_entries(down_internal, side="down"))
    edge_entries.extend(_group_edge_entries(down_groups, side="down"))
    lines.extend(emit_edges(edge_entries))

    for side, hidden in (("up", up_hidden), ("down", down_hidden)):
        if not hidden:
            continue
        label = escape_label(t(MORE_NEIGHBOURS_KEY[side], count=hidden))
        lines.append(f'  {MORE_NEIGHBOURS_ID[side]}["{label}"]')
        lines.append(f"  M --- {MORE_NEIGHBOURS_ID[side]}")

    if center.type == NodeType.MODULE:
        symbols = [
            n
            for n in packet.nodes
            if n.type == NodeType.SYMBOL and n.module_id == module_id and not (n.name or "").startswith("_")
        ]
        symbols.sort(key=lambda s: s.line_start or 0)
        budget = MAX_COLUMN_BOXES - _column_boxes(down_internal, down_groups, down_hidden)
        cap = max_symbols if max_symbols is None else min(max_symbols, max(budget, MIN_VISIBLE_SYMBOLS))
        shown = symbols if cap is None else symbols[:cap]
        for symbol in shown:
            lines.append(f'  {node_id(symbol.id)}("{escape_label(symbol.name or symbol.id)}")')
            lines.append(f"  M --- {node_id(symbol.id)}")
        hidden = len(symbols) - len(shown)
        if hidden > 0:
            label = escape_label(t("diagrams.symbols_more", count=hidden))
            lines.append(f'  {MORE_SYMBOLS_ID}["{label}"]')
            lines.append(f"  M --- {MORE_SYMBOLS_ID}")

    return "\n".join(lines) + "\n"


def _column_boxes(internal: list[tuple[str, float, bool, bool]], groups: dict[str, list], hidden: int) -> int:
    """右列里下游侧已占掉的盒子数（含外部组与被折掉的说明框）。"""
    return len(internal) + sum(1 for items in groups.values() if items) + (1 if hidden else 0)


def _cap_neighbors(entries: list[tuple[str, float, bool, bool]], nodes: dict, limit: int | None):
    """按重要性取前 ``limit`` 个，返回 ``(保留的, 折掉的条数)``。

    置信度高的先留 —— 低置信边本来就是推断出来的，先折它们比先折实边合理；
    同置信度按路径排序，保证产物可复现。
    """

    def key(entry: tuple[str, float, bool, bool]) -> tuple[float, str]:
        node = nodes.get(entry[0])
        return (-entry[1], (node.path if node is not None else entry[0]) or entry[0])

    if limit is None or len(entries) <= limit:
        return entries, 0
    ordered = sorted(entries, key=key)
    return ordered[:limit], len(entries) - limit


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


def _internal_edge_entries(entries, *, side: str) -> list[EdgeLine]:
    out: list[EdgeLine] = []
    for position, (_other, confidence, static, type_only) in enumerate(entries):
        if side == "up":
            head, tail = f"U{position}", "M"
        else:
            head, tail = "M", f"D{position}"
        styles: list[str] = []
        if type_only:
            out.append(EdgeLine(f"  {head} {DYNAMIC_EDGE}|{t(TYPE_ONLY_LABEL)}| {tail}"))
            continue
        if not static:
            styles.append("dynamic")
        arrow = DYNAMIC_EDGE if not static else "-->"
        tag = "" if static else f"|{confidence:.2f}?|"
        out.append(EdgeLine(f"  {head} {arrow}{tag} {tail}", tuple(styles)))
    return out


def _group_edge_entries(groups: dict[str, list], *, side: str) -> list[EdgeLine]:
    """分组边：整组都是仅类型依赖时用虚线，否则实线。

    组内混合时按实线画（只要有一条是运行时依赖，这个包就真的被加载）；仅类型的
    数量不再单列 —— 节点标签已经写明总数，再拆一遍会把图重新撑开。
    """
    out: list[EdgeLine] = []
    for key in GROUP_ORDER:
        entries = groups[key]
        if not entries:
            continue
        node = GROUP_NODE_ID[side][key]
        all_type_only = all(entry[3] for entry in entries)
        edge = f"{DYNAMIC_EDGE}|{t(TYPE_ONLY_LABEL)}|" if all_type_only else "-->"
        out.append(EdgeLine(f"  {node} {edge} M" if side == "up" else f"  M {edge} {node}"))
    return out


def _shorten(path: str) -> str:
    """长路径只保留末两段（图里放得下，完整路径在 Wiki 页）。"""
    parts = path.split("/")
    return "/".join(parts[-2:]) if len(parts) > 2 else path


__all__ = [
    "DEFAULT_MAX_NEIGHBORS",
    "DEFAULT_MAX_SYMBOLS",
    "MAX_COLUMN_BOXES",
    "MIN_VISIBLE_SYMBOLS",
    "render_module_context",
]
