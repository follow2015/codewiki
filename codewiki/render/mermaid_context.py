"""模块上下文交互图（Module Context，§4.2.2）。

以单模块为中心：

* **上游（Left）**：以本模块为目标的 ``IMPORTS`` / ``CALLS`` 起点；
* **自身（Middle）**：本模块 + 其对外可见符号；
* **下游（Right）**：本模块发出的 ``IMPORTS`` / ``CALLS`` 目标。

节点 ID 与转义规则复用 :mod:`mermaid_macro`（同一套，避免两张图风格分叉）。
"""

from __future__ import annotations

from codewiki.graph.index import GraphIndex
from codewiki.models import NodeType
from codewiki.render.mermaid_macro import DYNAMIC_EDGE, escape_label, node_id
from codewiki.schemas import DataPacket


def render_module_context(
    packet: DataPacket,
    module_id: str,
    *,
    min_confidence: float = 0.5,
    index: GraphIndex | None = None,
) -> str:
    """渲染某个模块的上下文图（返回 Mermaid 文本）。

    ``index`` 为空时临时建一份：**逐个模块渲染时必须复用同一个索引**，否则
    每个模块都扫描一遍全量边 ⇒ O(N × E)（5000 模块实测占到 40 秒）。
    """
    nodes = (index or GraphIndex(packet)).nodes
    if module_id not in nodes:
        raise KeyError(f"模块不存在于数据包: {module_id}")

    idx = index or GraphIndex(packet)
    upstream = idx.dependency_edges(module_id, min_confidence=min_confidence, direction="up")
    downstream = idx.dependency_edges(module_id, min_confidence=min_confidence, direction="down")

    lines = ["flowchart LR"]

    def emit(node_ids: list[tuple[str, float, bool]], *, side: str) -> list[str]:
        out = []
        for index, (other, *_detail) in enumerate(node_ids):
            node = nodes.get(other)
            name = (node.path or node.name or other) if node else other
            label = escape_label(_shorten(name))
            out.append(f'  {side}{index}["{label}"]')
        return out

    left = emit(upstream, side="U")
    right = emit(downstream, side="D")
    lines.extend(left)

    center = nodes[module_id]
    center_label = escape_label(_shorten(center.path or center.id))
    lines.append(f'  M["{center_label}"]')

    lines.extend(right)

    for index, (_other, confidence, static) in enumerate(upstream):
        arrow = DYNAMIC_EDGE if not static else "-->"
        tag = "" if static else f"|{confidence:.2f}?|"
        lines.append(f"  U{index} {arrow}{tag} M")

    for index, (_other, confidence, static) in enumerate(downstream):
        arrow = DYNAMIC_EDGE if not static else "-->"
        tag = "" if static else f"|{confidence:.2f}?|"
        lines.append(f"  M {arrow}{tag} D{index}")

    if center.type == NodeType.MODULE:
        symbols = [
            n
            for n in packet.nodes
            if n.type == NodeType.SYMBOL and n.module_id == module_id and not (n.name or "").startswith("_")
        ]
        for symbol in sorted(symbols, key=lambda s: s.line_start or 0):
            lines.append(f'  {node_id(symbol.id)}("{escape_label(symbol.name or symbol.id)}")')
            lines.append(f"  M --- {node_id(symbol.id)}")

    return "\n".join(lines) + "\n"


def _shorten(path: str) -> str:
    """长路径只保留末两段（图里放得下，完整路径在 Wiki 页）。"""
    parts = path.split("/")
    return "/".join(parts[-2:]) if len(parts) > 2 else path


__all__ = ["render_module_context"]
