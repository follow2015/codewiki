"""系统图的层间方向判据（图表优化 P0-4）。

分层倒置（下层反过来依赖上层）是**架构问题**，但当前图里它和正常依赖长得一模一样：
都是一条带次数的实线箭头。文档里的说法是"反向依赖边用红色虚线标出"。

Mermaid 只能靠 ``linkStyle`` 按**边的定义顺序**着色，所以索引必须与实际输出顺序
一致 —— 这条用例锁的就是"索引对得上"，而不是"有没有那个颜色字符串"。
"""

from __future__ import annotations

from codewiki.models import Edge, EdgeType, Node, NodeType
from codewiki.packetio import packet_from_parts
from codewiki.render.mermaid_macro import render_system_context

BACKWARD_COLOUR = "stroke:#d33"


def _node(path: str, layer: str) -> Node:
    return Node(id=f"mod::{path}", type=NodeType.MODULE, path=path, layer_id=layer)


def _layer_dep(source: str, target: str, weight: int = 1) -> Edge:
    """端点必须是**层 id**（配置文件里的 ``id``），不是模块 id。"""
    return Edge(source=source, target=target, type=EdgeType.LAYER_DEPENDS, weight=weight)


def _packet(edges):
    layers = [
        {"id": "top", "name": "上层", "order": 1},
        {"id": "base", "name": "基础层", "order": 2},
    ]
    nodes = [_node("top/a.py", "top"), _node("base/b.py", "base")]
    return packet_from_parts(title="t", layers=layers, nodes=nodes, edges=edges)


def test_backward_layer_dependency_is_marked_red():
    """基础层 → 上层 是倒置，必须标红；上层 → 基础层 是正常方向。"""
    text = render_system_context(
        _packet([_layer_dep("top", "base"), _layer_dep("base", "top")])
    )

    styles = [ln.strip() for ln in text.splitlines() if "linkStyle" in ln]
    assert styles, f"存在反向依赖时必须输出 linkStyle:\n{text}"
    assert styles[0] == f"linkStyle 1 {BACKWARD_COLOUR},stroke-width:2px,stroke-dasharray:4 2", (
        f"linkStyle 索引必须对应反向边在输出中的位置:\n{text}"
    )


def test_forward_only_dependencies_produce_no_link_style():
    """全是正向依赖时不该出现 linkStyle —— 免得读者以为图里标了什么。"""
    text = render_system_context(_packet([_layer_dep("top", "base")]))

    assert "linkStyle" not in text, f"没有反向边时不应输出 linkStyle:\n{text}"


def test_backward_marking_also_applies_to_type_only_edges():
    """仅类型的反向依赖同样是倒置，不能因为画虚线就漏标。"""
    edge = Edge(
        source="base",
        target="top",
        type=EdgeType.LAYER_DEPENDS,
        weight=1,
        type_only=True,
    )
    text = render_system_context(_packet([edge]))

    assert "linkStyle 0" in text, f"仅类型的反向边也要标红:\n{text}"


def test_layer_overview_has_one_node_per_layer():
    """层间概览：每层一个节点、标签写明模块数，不再出现模块级路径。

    pydantic 实测模块级系统图 474 行、渲染 1600×2626 —— 图上全是交叉线，层间
    那 11 条带次数的关系反而被淹没。概览模式要回答的是"层之间怎么依赖"。
    """
    text = render_system_context(_packet([_layer_dep("top", "base")]), include_modules=False)

    assert "上层" in text and "基础层" in text
    assert "1 个模块" in text, f"标签必须写出层内模块数:\n{text}"
    assert "top/a.py" not in text, f"概览模式不该出现具体模块:\n{text}"
    assert "subgraph" not in text, f"每层一个节点即可，不需要再套子图:\n{text}"


def test_render_all_writes_one_diagram_per_layer(tmp_path):
    """概览模式下层内细节必须有去处：每层一张图落在 diagrams/layers/。"""
    from codewiki.render.diagrams import LAYER_SUBDIR, render_all

    packet = _packet([_layer_dep("top", "base")])
    result = render_all(packet, tmp_path)

    assert not [d for d in result.diagnostics if d.level == "error"], result.diagnostics
    layer_files = sorted((tmp_path / "diagrams" / LAYER_SUBDIR).glob("*.mmd"))
    assert len(layer_files) == 2, f"两个非空层应各有一张图，实际: {layer_files}"
    assert "top/a.py" in layer_files[0].read_text(encoding="utf-8")


__all__: list[str] = []
