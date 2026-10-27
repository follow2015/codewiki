"""邻接索引判据（性能修复的**正确性**一面）。

性能优化最容易犯的错是"快了但结论少了"：把 O(N×E) 换成索引之后，如果
``dependency_edges`` 漏边、``module_of`` 认不出符号归属、置信度过滤失效，
产物会静默缺内容 —— 而图照样画得出来。所以索引的每个查询都要单独钉住。
"""

from __future__ import annotations

from codewiki.graph.index import build_index
from codewiki.models import CONF_DYNAMIC, CONF_STATIC, Edge, EdgeType, Node, NodeType
from codewiki.render.jsonio import packet_from_parts


def _module(path: str) -> Node:
    return Node(id=f"mod::{path}", type=NodeType.MODULE, path=path)


def _symbol(module_path: str, name: str, *, line: int = 1) -> Node:
    return Node(
        id=f"sym::{module_path}::{name}",
        type=NodeType.SYMBOL,
        name=name,
        module_id=f"mod::{module_path}",
        line_start=line,
        kind="function",
    )


def _packet(nodes, edges):
    return packet_from_parts(title="t", layers=[], nodes=nodes, edges=edges, annotations=[], diagnostics=[])


def test_dependency_edges_split_direction():
    packet = _packet(
        [_module("a.py"), _module("b.py"), _module("c.py")],
        [
            Edge(source="mod::b.py", target="mod::a.py", type=EdgeType.IMPORTS),
            Edge(source="mod::a.py", target="mod::c.py", type=EdgeType.IMPORTS),
        ],
    )
    index = build_index(packet)

    assert [o for o, _c, _s in index.dependency_edges("mod::a.py", min_confidence=0.0, direction="down")] == [
        "mod::c.py"
    ], "本模块发出的边 = 下游"
    assert [o for o, _c, _s in index.dependency_edges("mod::a.py", min_confidence=0.0, direction="up")] == [
        "mod::b.py"
    ], "指向本模块的边 = 上游"


def test_dependency_edges_respects_min_confidence():
    packet = _packet(
        [_module("a.py"), _module("b.py"), _module("c.py")],
        [
            Edge(source="mod::a.py", target="mod::b.py", type=EdgeType.CALLS, confidence=CONF_STATIC),
            Edge(
                source="mod::a.py",
                target="mod::c.py",
                type=EdgeType.CALLS,
                confidence=CONF_DYNAMIC,
                static=False,
            ),
        ],
    )
    index = build_index(packet)

    kept = index.dependency_edges("mod::a.py", min_confidence=0.5, direction="down")
    assert [o for o, _c, _s in kept] == ["mod::b.py"], "低置信边不得进图（§0.3）"


def test_symbols_of_is_sorted_by_line():
    packet = _packet(
        [_module("a.py"), _symbol("a.py", "late", line=30), _symbol("a.py", "early", line=2)],
        [],
    )
    index = build_index(packet)

    assert [s.name for s in index.symbols_of("mod::a.py")] == ["early", "late"]


def test_module_of_maps_symbol_to_module():
    packet = _packet([_module("a.py"), _symbol("a.py", "run")], [])
    index = build_index(packet)

    assert index.module_of("sym::a.py::run") == "mod::a.py"
    assert index.module_of("mod::a.py") == "mod::a.py"
    assert index.module_of("sym::unknown::x") is None


def test_neighbors_distinguish_import_and_call():
    packet = _packet(
        [_module("a.py"), _module("b.py"), _symbol("a.py", "run")],
        [
            Edge(source="mod::b.py", target="mod::a.py", type=EdgeType.IMPORTS),
            Edge(source="sym::a.py::run", target="sym::b.py::go", type=EdgeType.CALLS),
        ],
    )
    index = build_index(packet)
    upstream, downstream = index.neighbors("mod::a.py")

    assert upstream.get("mod::b.py") == "import"
    assert downstream.get("sym::b.py::go") == "call", "本模块符号发出的调用 = 下游"


def test_index_is_reusable_across_modules():
    """一次建索引、多次查询：这正是修掉 O(N×E) 的前提。"""
    packet = _packet(
        [_module("a.py"), _module("b.py")],
        [Edge(source="mod::a.py", target="mod::b.py", type=EdgeType.IMPORTS)],
    )
    index = build_index(packet)

    for _ in range(3):
        assert index.dependency_edges("mod::a.py", min_confidence=0.0, direction="down")
    assert index.nodes and len(index.out_edges) == 1
