"""边数闸门与降噪（图表优化 P1-5）。

层内图在百模块规模上是一团线：pydantic 的 `v1` 层有 27 个模块、130 条同层 import。
但其中大量边是"绕路"—— `A → B → C` 已经说明 A 能到 C，再画一条 `A → C` 只增加交叉。

两道闸门，只在**边数超阈值**时启用（小图保持精确，不为了好看丢掉信息）：

1. 传递归约：去掉"已有另一条路径可达"的直连边；
2. 枢纽折叠：被同层超过 30% 模块依赖的节点（`errors.py` 这类），不逐条连入边，
   改成在节点标签上写「被 N 个模块使用」。

pydantic 实测：`root` 层 97 → 32 → 18 条，`v1` 层 130 → 13 → 7 条。
"""

from __future__ import annotations

from codewiki.graph.builder import module_id
from codewiki.models import Edge, EdgeType, Node, NodeType
from codewiki.packetio import packet_from_parts
from codewiki.render.mermaid_macro import node_id, render_system_context


def _module(path: str) -> Node:
    return Node(id=module_id(path), type=NodeType.MODULE, path=path, layer_id="l1")


def _packet(nodes: list[Node], edges: list[Edge]):
    return packet_from_parts(
        title="t", layers=[{"id": "l1", "name": "L1", "order": 1}], nodes=nodes, edges=edges
    )


def _chain(count: int, *, shortcut: bool = True):
    """``count`` 个模块连成链；``shortcut`` 加一条 ``m0 → m2``（可被归约）。"""
    nodes = [_module(f"l1/m{index}.py") for index in range(count)]
    edges = [
        Edge(
            source=module_id(f"l1/m{index}.py"),
            target=module_id(f"l1/m{index + 1}.py"),
            type=EdgeType.IMPORTS,
        )
        for index in range(count - 1)
    ]
    if shortcut:
        edges.append(Edge(source=module_id("l1/m0.py"), target=module_id("l1/m2.py"), type=EdgeType.IMPORTS))
    return nodes, edges


def test_small_diagram_keeps_every_edge():
    """边数没到阈值就不降噪：A→C 与 A→B→C 是两条不同的声明。"""
    nodes, edges = _chain(20)
    text = render_system_context(_packet(nodes, edges))

    assert f"{node_id(module_id('l1/m0.py'))} --> {node_id(module_id('l1/m2.py'))}" in text


def test_transitive_shortcut_is_dropped_above_threshold():
    """超阈值时，已被 ``m0 → m1 → m2`` 覆盖的直连边不再画。"""
    nodes, edges = _chain(62)
    text = render_system_context(_packet(nodes, edges))

    shortcut = f"{node_id(module_id('l1/m0.py'))} --> {node_id(module_id('l1/m2.py'))}"
    hop = f"{node_id(module_id('l1/m0.py'))} --> {node_id(module_id('l1/m1.py'))}"
    assert shortcut not in text, "传递可归约的直连边应被省略"
    assert hop in text, "链上的边必须保留（它是唯一路径）"


def test_hub_edges_are_folded_into_a_badge():
    """被 30% 以上模块依赖的节点：入边不画，标签写明被多少个模块使用。"""
    hub = module_id("l1/hub.py")
    users = 40
    nodes = [_module("l1/hub.py")] + [_module(f"l1/u{index}.py") for index in range(users)]
    edges = [
        Edge(source=module_id(f"l1/u{index}.py"), target=hub, type=EdgeType.IMPORTS) for index in range(users)
    ]
    edges += [
        Edge(
            source=module_id(f"l1/u{index}.py"),
            target=module_id(f"l1/u{index + 1}.py"),
            type=EdgeType.IMPORTS,
        )
        for index in range(users - 1)
    ]
    text = render_system_context(_packet(nodes, edges))

    assert f"被 {users} 个模块使用" in text, f"枢纽节点标签要带徽标:\n{text[:400]}"
    assert f'["l1/hub.py · 被 {users} 个模块使用"]' in text, "徽标前要有空格（不然挤成 hub.py·）"
    assert f"--> {node_id(hub)}" not in text, "枢纽的入边应被折叠"


def test_hub_badge_only_applies_above_threshold():
    """小图不做折叠：4 个模块都依赖 hub 也不算枢纽（边数远没到阈值）。"""
    hub = module_id("l1/hub.py")
    nodes = [_module("l1/hub.py")] + [_module(f"l1/u{index}.py") for index in range(3)]
    edges = [
        Edge(source=module_id(f"l1/u{index}.py"), target=hub, type=EdgeType.IMPORTS) for index in range(3)
    ]
    text = render_system_context(_packet(nodes, edges))

    assert "个模块使用" not in text
    assert f"--> {node_id(hub)}" in text


def test_below_threshold_boundary_is_exact():
    """阈值是"超过"而不是"达到"：正好 60 条边仍全画。"""
    nodes, edges = _chain(60, shortcut=True)
    text = render_system_context(_packet(nodes, edges))

    assert f"{node_id(module_id('l1/m0.py'))} --> {node_id(module_id('l1/m2.py'))}" in text


__all__: list[str] = []
