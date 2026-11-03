"""边的视觉层次（图表优化 P1-7）。

四类边必须在图上一眼能分开，否则"虚线"同时代表三种含义：

* 运行时 import —— 实线 ``-->``；
* ``if TYPE_CHECKING`` 依赖 —— 虚线 ``-.->`` + 「仅类型」标签；
* 动态/低置信推断 —— 点线（``stroke-dasharray`` 更碎）+ 置信度标签；
* 环上的边 —— 加粗标红。

**Mermaid 的 ``linkStyle`` 按边在图中定义的顺序编号，且不可见链路 ``~~~`` 也占一个
槽位**（实测：把 ``~~~`` 放在第 2 条时，``linkStyle 1`` 落在它那条 ``dasharray: 0 0 …``
的隐形 path 上）。所以索引必须按"图中全部 link 的序号"算，不能只数可见边。
"""

from __future__ import annotations

from codewiki.graph import build_graph
from codewiki.graph.builder import module_id
from codewiki.models import Edge, EdgeType, FileModel, ImportFact, Node, NodeType
from codewiki.packetio import packet_from_parts
from codewiki.render.mermaid_macro import render_system_context

CYCLE_STYLE = "stroke:#d33,stroke-width:3px"
DYNAMIC_STYLE = "stroke-dasharray:1 3"
BACKWARD_STYLE = "stroke:#d33,stroke-width:2px,stroke-dasharray:4 2"


def _fm(path, *, imports=()):
    return FileModel(
        path=path,
        language="python",
        hash=path,
        imports=[
            ImportFact(module=m, name=None, lineno=ln, kind="import", type_only=type_only)
            for m, ln, type_only in imports
        ],
        symbols=[],
        calls=[],
    )


def _module(path: str, layer: str) -> Node:
    return Node(id=f"mod::{path}", type=NodeType.MODULE, path=path, layer_id=layer)


def _layer(layer_id: str, order: int) -> dict:
    return {"id": layer_id, "name": layer_id, "order": order}


def _styles(text: str) -> list[tuple[list[int], str]]:
    """把 ``linkStyle a,b,c <style>`` 解析成 ``([a, b, c], style)``。"""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("linkStyle "):
            continue
        _, indexes, style = stripped.split(" ", 2)
        out.append(([int(item) for item in indexes.split(",")], style))
    return out


def _style_of(text: str, index: int) -> str:
    for indexes, style in _styles(text):
        if index in indexes:
            return style
    return ""


def test_builder_flags_import_edges_inside_a_cycle():
    """互相 import 的两条边都在环上；只被单向依赖的边不在。"""
    files = [
        _fm("__init__.py"),
        _fm("a.py", imports=[("b", 1, False)]),
        _fm("b.py", imports=[("a", 1, False)]),
        _fm("c.py", imports=[("a", 1, False)]),
    ]
    result = build_graph(files)
    flags = {
        (edge.source, edge.target): edge.in_cycle for edge in result.edges if edge.type == EdgeType.IMPORTS
    }

    assert flags[(module_id("a.py"), module_id("b.py"))] is True
    assert flags[(module_id("b.py"), module_id("a.py"))] is True
    assert flags[(module_id("c.py"), module_id("a.py"))] is False


def test_type_only_edges_are_never_flagged_as_cycle():
    """``if TYPE_CHECKING`` 的互相依赖在运行时不存在，不算环。"""
    files = [
        _fm("__init__.py"),
        _fm("a.py", imports=[("b", 1, True)]),
        _fm("b.py", imports=[("a", 1, True)]),
    ]
    result = build_graph(files)
    flags = {edge.in_cycle for edge in result.edges if edge.type == EdgeType.IMPORTS}

    assert flags == {False}, flags


def test_layer_cycle_edges_are_flagged():
    """层间环上的 ``LAYER_DEPENDS`` 边同样要标 —— 系统图靠它加粗。"""
    layers = [
        {"id": "a", "name": "A", "order": 1, "paths": ["a/"]},
        {"id": "b", "name": "B", "order": 2, "paths": ["b/"]},
    ]
    files = [
        _fm("__init__.py"),
        _fm("a/x.py", imports=[("b.y", 1, False)]),
        _fm("b/y.py", imports=[("a.x", 1, False)]),
    ]
    result = build_graph(files, layers=layers)
    layer_edges = [edge for edge in result.edges if edge.type == EdgeType.LAYER_DEPENDS]

    assert layer_edges, "两个层互相 import 应产出层间边"
    assert all(edge.in_cycle for edge in layer_edges), [(e.source, e.target) for e in layer_edges]


def test_in_cycle_defaults_to_false():
    """加法字段：老数据包读进来仍然是 False，不改变既有语义。"""
    assert Edge(source="a", target="b", type=EdgeType.IMPORTS).in_cycle is False


def _layered_packet(extra_edges: list[Edge]):
    """两层；链占 1 个 linkStyle 槽位，层间边从索引 1 开始。"""
    layers = [_layer("top", 1), _layer("base", 2)]
    nodes = [_module("top/a.py", "top"), _module("top/b.py", "top"), _module("base/c.py", "base")]
    return packet_from_parts(title="t", layers=layers, nodes=nodes, edges=extra_edges)


def test_backward_edge_index_accounts_for_the_layer_chain():
    """层链也占 linkStyle 槽位 —— 索引必须按图中全部 link 编号。"""
    edges = [
        Edge(source="top", target="base", type=EdgeType.LAYER_DEPENDS, weight=2),
        Edge(source="base", target="top", type=EdgeType.LAYER_DEPENDS, weight=1),
    ]
    text = render_system_context(_layered_packet(edges))

    assert len([line for line in text.splitlines() if "~~~" in line]) == 1
    assert _style_of(text, 2) == BACKWARD_STYLE, f"反向边的索引要含层链偏移:\n{text}"
    assert _style_of(text, 1) == "", "正向边不该被着色"


def test_link_style_offset_counts_every_chain_link():
    """层链是**一行**里的多条 ``~~~``：偏移要数 link，不能数行。

    两层时「一行链 = 一条 link」恰好相等，所以这个缺陷在两层用例里看不出来；
    六层时按行数算偏移会让所有 ``linkStyle`` 前移 4 位，颜色标到链上那条隐形
    path 和自己的邻边上（pydantic 实测：反向边应标 6,9,10,14，实际标在 2,5,6,10）。
    """
    layers = [_layer("top", 1), _layer("mid", 2), _layer("base", 3)]
    nodes = [
        _module("top/a.py", "top"),
        _module("mid/b.py", "mid"),
        _module("base/c.py", "base"),
    ]
    edges = [
        Edge(source="top", target="base", type=EdgeType.LAYER_DEPENDS, weight=2),
        Edge(source="base", target="top", type=EdgeType.LAYER_DEPENDS, weight=1),
    ]
    text = render_system_context(packet_from_parts(title="t", layers=layers, nodes=nodes, edges=edges))

    chain = [line for line in text.splitlines() if "~~~" in line]
    assert len(chain) == 1, "层链应该是一行"
    assert chain[0].count("~~~") == 2, "三层 = 两条不可见 link"
    assert _style_of(text, 3) == BACKWARD_STYLE, f"反向边的索引要按 link 数偏移:\n{text}"
    assert _style_of(text, 2) == "", f"正向边不该被着色:\n{text}"


def test_cycle_edges_are_bold_and_red():
    """同层内互相 import 的两条边都加粗标红。"""
    a, b = module_id("top/a.py"), module_id("top/b.py")
    edges = [
        Edge(source=a, target=b, type=EdgeType.IMPORTS, in_cycle=True),
        Edge(source=b, target=a, type=EdgeType.IMPORTS, in_cycle=True),
    ]
    text = render_system_context(_layered_packet(edges))

    assert _style_of(text, 1) == CYCLE_STYLE, text
    assert _style_of(text, 2) == CYCLE_STYLE, text


def test_dynamic_edges_are_dotted():
    """低置信推断边用更碎的点线，与「仅类型」的虚线分开。"""
    a, b = module_id("top/a.py"), module_id("top/b.py")
    edges = [Edge(source=a, target=b, type=EdgeType.IMPORTS, static=False, confidence=0.6)]
    text = render_system_context(_layered_packet(edges))

    assert _style_of(text, 1) == DYNAMIC_STYLE, text


def test_type_only_edges_keep_dashed_arrow_and_label():
    """仅类型依赖保持虚线 + 标签，不叠点线（标签已经足够区分）。"""
    a, b = module_id("top/a.py"), module_id("top/b.py")
    edges = [Edge(source=a, target=b, type=EdgeType.IMPORTS, type_only=True, static=False)]
    text = render_system_context(_layered_packet(edges))

    assert "-.->|仅类型|" in text, text
    assert _style_of(text, 1) == "", f"仅类型边不该再叠点线样式:\n{text}"


def test_type_only_layer_edge_is_not_dotted():
    """层间仅类型边同样只画虚线 + 标签，不叠点线（层、模块两条路径都要锁）。"""
    edges = [
        Edge(source="top", target="base", type=EdgeType.LAYER_DEPENDS, type_only=True, static=False),
    ]
    text = render_system_context(_layered_packet(edges))

    assert "-.->|仅类型|" in text, text
    assert _style_of(text, 1) == "", f"层间仅类型边不该叠点线:\n{text}"


def test_module_context_type_only_edge_keeps_plain_dash():
    """模块图里的仅类型边同样只画虚线 + 标签 —— 别叠点线，也别丢标签。"""
    from codewiki.render.mermaid_context import render_module_context

    a, b = module_id("pkg/a.py"), module_id("pkg/b.py")
    nodes = [_module("pkg/a.py", "l1"), _module("pkg/b.py", "l1")]
    edges = [Edge(source=a, target=b, type=EdgeType.IMPORTS, type_only=True, static=False)]
    packet = packet_from_parts(title="t", layers=[_layer("l1", 1)], nodes=nodes, edges=edges)
    text = render_module_context(packet, b)

    assert "|仅类型|" in text, text
    assert "linkStyle" not in text, f"仅类型边不该叠点线:\n{text}"


def test_static_runtime_edges_stay_plain():
    """运行时实线边不加任何样式 —— 否则"默认"就不成立了。"""
    a, b = module_id("top/a.py"), module_id("top/b.py")
    edges = [Edge(source=a, target=b, type=EdgeType.IMPORTS)]
    text = render_system_context(_layered_packet(edges))

    assert "--> " in text or "-->" in text
    assert _styles(text) == [], f"没有特殊边时不该输出 linkStyle:\n{text}"


def test_module_context_does_not_mark_cycles():
    """模块图**不**标环 —— 环是全局信息，而模块图按模块粒度做增量复用。

    标了之后，删掉一个文件让环消失时，未受影响模块的图会被原样复用，图上留着已经
    不存在的环（``test_incremental_equals_full`` 当场红在 ``pkg_m1.mmd``）。环的可视化
    放在系统图与层图，它们每轮都全量重渲，不存在陈旧问题。
    """
    from codewiki.render.mermaid_context import render_module_context

    a, b = module_id("pkg/a.py"), module_id("pkg/b.py")
    nodes = [_module("pkg/a.py", "l1"), _module("pkg/b.py", "l1")]
    edges = [
        Edge(source=a, target=b, type=EdgeType.IMPORTS, in_cycle=True),
        Edge(source=b, target=a, type=EdgeType.IMPORTS, in_cycle=True),
    ]
    packet = packet_from_parts(title="t", layers=[_layer("l1", 1)], nodes=nodes, edges=edges)
    text = render_module_context(packet, b)

    assert "linkStyle" not in text, f"模块图不该带全局环标记（会让增量复用陈旧）:\n{text}"


__all__: list[str] = []
