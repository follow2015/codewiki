"""层顺序对齐（图表优化 P0-4b）。

Mermaid 的 dagre 默认按**边**分配 rank，而不是按层的 ``order``：只要存在一条
"下层 → 上层"的回边，下层就会被提到图顶，整张图的上下方向感消失。

实测（三节点最小复现）：``A --> B``、``C -.-> A`` 的图渲染出来是 ``db / api / core``，
即 db 被拉到了 api 之上；加一条 ``A ~~~ B ~~~ C`` 之后顺序变回 ``api / core / db``。
``~~~`` 不画线但参与 rank 分配，所以能钉死层序，而反向边只影响自己的箭头方向。
"""

from __future__ import annotations

from codewiki.layering import UNLAYERED_LAYER_ID
from codewiki.models import Edge, EdgeType, Node, NodeType
from codewiki.packetio import packet_from_parts
from codewiki.render.mermaid_macro import node_id, render_system_context


def _module(path: str, layer: str) -> Node:
    return Node(id=f"mod::{path}", type=NodeType.MODULE, path=path, layer_id=layer)


def _layer(layer_id: str, name: str, order: int) -> dict:
    return {"id": layer_id, "name": name, "order": order}


def _packet(layers: list[dict], nodes: list[Node], edges: list[Edge] | None = None):
    return packet_from_parts(title="t", layers=layers, nodes=nodes, edges=edges or [])


def _chains(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if "~~~" in line]


def test_chain_follows_order_not_declaration():
    """配置里层的**声明顺序**与 ``order`` 不一致时，链必须按 order 排。"""
    layers = [_layer("b", "基础层", 2), _layer("a", "上层", 1)]
    nodes = [_module("a/x.py", "a"), _module("b/y.py", "b")]
    text = render_system_context(_packet(layers, nodes), include_modules=False)

    assert _chains(text) == [f"{node_id('a')} ~~~ {node_id('b')}"], f"链顺序必须等于 order 顺序:\n{text}"


def test_module_level_system_diagram_also_chains_layers():
    """模块级系统图（subgraph 粒度）同样要钉住层序，否则回边一样会把层拉翻。"""
    layers = [_layer("top", "上层", 1), _layer("base", "基础层", 2)]
    nodes = [_module("top/a.py", "top"), _module("base/b.py", "base")]
    text = render_system_context(_packet(layers, nodes))

    assert _chains(text), f"模块级系统图也要有层链:\n{text}"
    chain = _chains(text)[0]
    assert chain.index(node_id("top")) < chain.index(node_id("base")), chain


def test_single_layer_needs_no_chain():
    """只有一层时没有"顺序"可言，别在图上多塞一条看不见的链路。"""
    layers = [_layer("only", "唯一层", 1)]
    nodes = [_module("only/a.py", "only")]
    text = render_system_context(_packet(layers, nodes), include_modules=False)

    assert not _chains(text), f"单层不该出现链:\n{text}"


def test_unlayered_group_sits_at_the_end_of_chain():
    """未分层兜底组排在最后 —— 它不该被当作"最上层"。"""
    layers = [_layer("top", "上层", 1), _layer("base", "基础层", 2)]
    nodes = [
        _module("top/a.py", "top"),
        _module("base/b.py", "base"),
        _module("stray/c.py", UNLAYERED_LAYER_ID),
    ]
    text = render_system_context(_packet(layers, nodes), include_modules=False)

    chain = _chains(text)[0]
    assert chain.endswith(node_id(UNLAYERED_LAYER_ID)), f"未分层组应在链尾:\n{text}"


def test_chain_does_not_change_edge_direction():
    """链只影响布局，不能把反向边"修正"成正向 —— 倒置必须仍然标红。"""
    layers = [_layer("top", "上层", 1), _layer("base", "基础层", 2)]
    nodes = [_module("top/a.py", "top"), _module("base/b.py", "base")]
    backward = Edge(source="base", target="top", type=EdgeType.LAYER_DEPENDS, weight=1)
    text = render_system_context(_packet(layers, nodes, [backward]), include_modules=False)

    assert f"  {node_id('base')} --> {node_id('top')}" in text, f"反向边的箭头方向不能变:\n{text}"
    assert "linkStyle 1" in text, f"反向边仍要标红:\n{text}"


__all__: list[str] = []
