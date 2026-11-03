"""宏观系统架构图（System Context，§4.2.1）。

目标：展示**层级依赖**与核心组件的数据流向，按配置顺序自上而下排列。

生成规则（逐条对应 §4.2.1）：

1. 遍历 ``architecture.layers`` 生成 ``subgraph``（层 ID 作子图 ID）；
2. 模块按 paths 匹配归入子图，层内按调用权重排序；
3. 有注解时把多行说明用 ``<br/>`` 拼到节点 Label；
4. 层间 ``LAYER_DEPENDS`` 边聚合成一条带调用次数的边；
5. 低于 ``min_confidence`` 的边**不渲染**；动态边虚线 + 置信度标签。

节点 ID 一律用 ``n<hash>``（Mermaid 的 ID 只允许字母数字下划线），真实名字放
Label —— 这样中文路径、点号、斜杠都不会破坏语法。
"""

from __future__ import annotations

import hashlib

from codewiki.layering import UNLAYERED_LAYER_ID
from codewiki.models import EdgeType, NodeType
from codewiki.render.i18n import t
from codewiki.schemas import DataPacket

DYNAMIC_EDGE = "-.->"

TYPE_ONLY_LABEL = "diagrams.edge_type_only"

UNLAYERED_LABEL = "common.unlayered"

BACKWARD_EDGE_STYLE = "stroke:#d33,stroke-width:2px,stroke-dasharray:4 2"

_UNRANKED_LAYER = 10**6


def node_id(seed: str) -> str:
    """由任意字符串生成合法的 Mermaid 节点 ID。"""
    return "n" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]


def escape_label(text: str) -> str:
    """转义 Label 里的特殊字符（§4.3：未转义的 ``()[]"`` 是坏图特征）。"""
    return (
        text.replace("\\", "\\\\")
        .replace('"', "#quot;")
        .replace("(", "#40;")
        .replace(")", "#41;")
        .replace("[", "#91;")
        .replace("]", "#93;")
        .replace("{", "#123;")
        .replace("}", "#125;")
    )


def _annotation_label(packet: DataPacket, target: str, base: str) -> str:
    """把人工/AI 注解拼进 Label（多行用 ``<br/>``，§4.2.1 第 3 步）。"""
    notes = [a for a in packet.annotations if a.target == target and a.note]
    if not notes:
        return base
    extra = "<br/>".join(escape_label(a.note) for a in notes)
    return f"{base}<br/>{extra}"


def render_system_context(
    packet: DataPacket,
    *,
    min_confidence: float = 0.5,
    subset: set[str] | None = None,
    include_modules: bool = True,
) -> str:
    """渲染宏观架构图（返回 Mermaid 文本，不含代码围栏）。

    ``subset`` 用于**大图分包**：只画给定模块（跨层边仍按全局事实画，但只保留
    两端都在本页的边 —— 半条边比没有边更误导）。

    ``include_modules=False`` 切换到**层间概览**：每层一个节点（标签写明模块数），
    只画 ``LAYER_DEPENDS``。系统图默认用这个粒度 —— 模块级的系统图在 100 个模块
    规模上就是毛线球（pydantic 实测 474 行、渲染 1600×2626），层间关系反而被淹没；
    层内细节由每层单独的图承担。
    """
    layers = sorted(packet.layers, key=lambda layer: layer.order if layer.order is not None else 10_000)
    modules = [n for n in packet.nodes if n.type == NodeType.MODULE]
    if subset is not None:
        modules = [m for m in modules if m.id in subset]

    lines = ["flowchart TD"]
    if not layers:
        for module in sorted(modules, key=lambda m: m.path or ""):
            label = escape_label(module.path or module.id)
            lines.append(f'  {node_id(module.id)}["{_annotation_label(packet, module.id, label)}"]')
        lines.extend(_module_edges(packet, min_confidence, subset={m.id for m in modules}))
        return "\n".join(lines) + "\n"

    if not include_modules:
        return _render_layer_overview(packet, layers, modules, min_confidence=min_confidence)

    for layer in layers:
        lines.append(f'  subgraph {node_id(layer.id)}["{escape_label(layer.name)}"]')
        members = [m for m in modules if m.layer_id == layer.id]
        for module in _sorted_by_weight(packet, members):
            label = escape_label(module.path or module.id)
            lines.append(f'    {node_id(module.id)}["{_annotation_label(packet, module.id, label)}"]')
        lines.append("  end")

    unlayered = [m for m in modules if not m.layer_id or m.layer_id == UNLAYERED_LAYER_ID]
    if unlayered:
        lines.append(f'  subgraph {node_id(UNLAYERED_LAYER_ID)}["{t(UNLAYERED_LABEL)}"]')
        for module in _sorted_by_weight(packet, unlayered):
            label = escape_label(module.path or module.id)
            lines.append(f'    {node_id(module.id)}["{_annotation_label(packet, module.id, label)}"]')
        lines.append("  end")

    layer_edges, backward_positions = _layer_edge_lines(packet, min_confidence, layers)
    lines.extend(layer_edges)

    lines.extend(_module_edges(packet, min_confidence, subset={m.id for m in modules}))

    if backward_positions:
        lines.append(
            "  linkStyle "
            + ",".join(str(position) for position in backward_positions)
            + f" {BACKWARD_EDGE_STYLE}"
        )

    return "\n".join(lines) + "\n"


def _render_layer_overview(
    packet: DataPacket,
    layers: list,
    modules: list,
    *,
    min_confidence: float,
) -> str:
    """层间概览：每层一个节点，只画 ``LAYER_DEPENDS``。

    节点标签写明模块数 —— 读者一眼能判断"这层值不值得下钻"，而不是先点进去
    才知道里面有多少东西。未分层兜底组同样呈现（不能吞掉没归类的模块）。
    """
    lines = ["flowchart TD"]
    for layer in layers:
        members = [m for m in modules if m.layer_id == layer.id]
        label = escape_label(t("diagrams.layer_node", name=layer.name, count=len(members)))
        lines.append(f'  {node_id(layer.id)}["{label}"]')

    unlayered = [m for m in modules if not m.layer_id or m.layer_id == UNLAYERED_LAYER_ID]
    if unlayered:
        label = escape_label(t("diagrams.layer_node", name=t(UNLAYERED_LABEL), count=len(unlayered)))
        lines.append(f'  {node_id(UNLAYERED_LAYER_ID)}["{label}"]')

    layer_edges, backward_positions = _layer_edge_lines(packet, min_confidence, layers)
    lines.extend(layer_edges)
    if backward_positions:
        lines.append(
            "  linkStyle "
            + ",".join(str(position) for position in backward_positions)
            + f" {BACKWARD_EDGE_STYLE}"
        )
    return "\n".join(lines) + "\n"


def _layer_edge_lines(packet: DataPacket, min_confidence: float, layers: list) -> tuple[list[str], list[int]]:
    """层间边 + 反向边的位置索引。

    **反向依赖**（下层反过来依赖上层）单独标红：分层倒置是图上最该先看见的东西，
    而它与正常依赖长得一模一样（都是一条带次数的箭头）。Mermaid 只能按**边的
    定义顺序**上色（linkStyle 用索引），所以边要收集完再统一输出，索引才对得上。
    """
    layer_rank = {layer.id: position for position, layer in enumerate(layers)}
    entries: list[tuple[str, bool]] = []
    for edge in packet.edges:
        if edge.type != EdgeType.LAYER_DEPENDS:
            continue
        if edge.confidence < min_confidence:
            continue
        backward = layer_rank.get(edge.target, _UNRANKED_LAYER) < layer_rank.get(edge.source, _UNRANKED_LAYER)
        if edge.type_only:
            shown = t(TYPE_ONLY_LABEL)
            tag = f"|{shown} {edge.weight}|" if edge.weight and edge.weight > 1 else f"|{shown}|"
            entries.append((f"  {node_id(edge.source)} {DYNAMIC_EDGE}{tag} {node_id(edge.target)}", backward))
            continue
        arrow = DYNAMIC_EDGE if not edge.static else "-->"
        tag = f"|{edge.weight}|" if edge.weight and edge.weight > 1 else ""
        entries.append((f"  {node_id(edge.source)} {arrow}{tag} {node_id(edge.target)}", backward))

    return (
        [line for line, _backward in entries],
        [position for position, (_line, flag) in enumerate(entries) if flag],
    )


def _module_edges(packet: DataPacket, min_confidence: float, *, subset: set[str]) -> list[str]:
    """模块级 IMPORTS 边（**只画同层内**，跨层已由 LAYER_DEPENDS 表达）。

    跨层的那几十条模块 import 如果照画，会与 LAYER_DEPENDS 重复表达同一件事；
    更糟的是它们把两个 subgraph 直接连成一片，Mermaid 的 dagre 就失去"层与层
    上下分层"的依据，整张图塌成一条横向长带（25 个节点排一行、5588px 宽）。
    所以这里必须按层过滤 —— 跨层只留聚合边，图才读得出方向。
    """
    layer_of = {n.id: n.layer_id for n in packet.nodes}
    out: list[str] = []
    for edge in packet.edges:
        if edge.type != EdgeType.IMPORTS or edge.confidence < min_confidence:
            continue
        if edge.source not in subset or edge.target not in subset:
            continue
        if layer_of.get(edge.source) != layer_of.get(edge.target):
            continue
        if edge.type_only:
            out.append(
                f"  {node_id(edge.source)} {DYNAMIC_EDGE}|{t(TYPE_ONLY_LABEL)}| {node_id(edge.target)}"
            )
            continue
        arrow = DYNAMIC_EDGE if not edge.static else "-->"
        label = ""
        if not edge.static:
            label = f"|{edge.confidence:.2f}?|"
        out.append(f"  {node_id(edge.source)} {arrow}{label} {node_id(edge.target)}")
    return out


def _sorted_by_weight(packet: DataPacket, members: list) -> list:
    """层内按调用权重（出入度之和）降序，权重相同的按路径，保证产物稳定。"""
    degree: dict[str, int] = {}
    for edge in packet.edges:
        if edge.type not in (EdgeType.IMPORTS, EdgeType.CALLS):
            continue
        degree[edge.source] = degree.get(edge.source, 0) + 1
        degree[edge.target] = degree.get(edge.target, 0) + 1
    return sorted(members, key=lambda m: (-degree.get(m.id, 0), m.path or m.id))


__all__ = ["DYNAMIC_EDGE", "escape_label", "node_id", "render_system_context"]
