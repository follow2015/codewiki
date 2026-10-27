"""环 / God Node / 变更影响判据（§2.5、§4.6.3）。

两条反假阳性（都是实跑真实代码时踩出来的，故钉成用例）：

1. **外部引用节点不算跨层**：``mod::ref::*`` 不属于任何层，把它算进分母会让
   "导入很多标准库"看起来像跨层耦合（曾出现"全部模块同层却报 100% 跨层"）；
2. **外部引用节点本身不是 God Node**：它没层，"跨层"无从谈起（曾出现
   "`__future__` 是上帝模块"这种荒谬结论）。

结论一律要能落到具体节点：环给成员、God Node 给度数与占比、影响给半径。
"""

from __future__ import annotations

import pytest

from codewiki.graph.analysis import (
    cycles_to_diagnostics,
    diff_packets,
    find_layer_cycles,
    find_module_cycles,
    god_node_diagnostics,
    hotspots,
    impact_of,
    module_graph,
)
from codewiki.models import CONF_STATIC, Edge, EdgeType, Node, NodeType
from codewiki.render.jsonio import packet_from_parts


def _module(path: str, *, layer: str = "layer::a") -> Node:
    return Node(id=f"mod::{path}", type=NodeType.MODULE, path=path, layer_id=layer)


def _external(name: str) -> Node:
    """外部引用占位节点（无 layer_id，与真实产物一致）。"""
    return Node(id=f"mod::ref::{name}", type=NodeType.MODULE, path=None)


def _edge(source: str, target: str, *, kind: EdgeType = EdgeType.IMPORTS) -> Edge:
    return Edge(source=f"mod::{source}", target=f"mod::{target}", type=kind, confidence=CONF_STATIC)


def _packet(nodes, edges):
    return packet_from_parts(title="t", layers=[], nodes=nodes, edges=edges, annotations=[], diagnostics=[])


def test_two_node_cycle_is_found():
    packet = _packet([_module("a.py"), _module("b.py")], [_edge("a.py", "b.py"), _edge("b.py", "a.py")])
    cycles = find_module_cycles(packet)

    assert len(cycles) == 1 and set(cycles[0].members) == {"mod::a.py", "mod::b.py"}


def test_self_loop_is_a_cycle():
    packet = _packet([_module("a.py")], [_edge("a.py", "a.py")])
    assert len(find_module_cycles(packet)) == 1


def test_acyclic_graph_has_no_cycles():
    packet = _packet([_module("a.py"), _module("b.py")], [_edge("a.py", "b.py")])
    assert find_module_cycles(packet) == []


def test_layer_cycle_is_error_level():
    packet = _packet(
        [_module("a.py")],
        [
            Edge(source="layer::x", target="layer::y", type=EdgeType.LAYER_DEPENDS),
            Edge(source="layer::y", target="layer::x", type=EdgeType.LAYER_DEPENDS),
        ],
    )
    cycles = find_layer_cycles(packet)
    diagnostics = cycles_to_diagnostics(cycles)

    assert len(cycles) == 1 and cycles[0].kind == "layer"
    assert [d.code for d in diagnostics] == ["LAYER_CYCLE"]
    assert diagnostics[0].level == "error", "分层失效是严重诊断（§2.5）"


def test_module_cycle_diagnostic_is_warning():
    packet = _packet([_module("a.py"), _module("b.py")], [_edge("a.py", "b.py"), _edge("b.py", "a.py")])
    diagnostics = cycles_to_diagnostics(find_module_cycles(packet))

    assert [d.code for d in diagnostics] == ["MODULE_CYCLE"]
    assert diagnostics[0].level == "warning"


def test_module_graph_ignores_symbol_edges():
    """符号级边会把一个模块对放大成几十条边，干扰 SCC 判定。"""
    packet = _packet(
        [_module("a.py"), _module("b.py")],
        [Edge(source="sym::a.py::A.run", target="sym::b.py::B", type=EdgeType.CALLS)],
    )
    assert module_graph(packet).number_of_edges() == 0


def test_god_node_requires_cross_layer_coupling():
    """高扇入扇出 + 高跨层占比才报（单看度数会把公共工具模块误报）。"""
    nodes = [
        _module("hub.py", layer="layer::a"),
        _module("x.py", layer="layer::b"),
        _module("y.py", layer="layer::c"),
        _module("z.py", layer="layer::d"),
    ]
    edges = [_edge("hub.py", "x.py"), _edge("hub.py", "y.py"), _edge("hub.py", "z.py")]
    diagnostics = god_node_diagnostics(_packet(nodes, edges), cross_layer_ratio=0.5)

    assert [d.code for d in diagnostics] == ["GOD_NODE"]
    assert "hub.py" in diagnostics[0].where


def test_same_layer_hub_is_not_a_god_node():
    nodes = [_module("hub.py"), _module("x.py"), _module("y.py")]
    edges = [_edge("hub.py", "x.py"), _edge("hub.py", "y.py")]
    assert god_node_diagnostics(_packet(nodes, edges)) == []


def test_external_nodes_never_become_god_nodes():
    """`__future__` 被 15 个模块引用 ≠ 它是上帝模块。"""
    nodes = [_module("a.py"), _external("typing"), _external("os")]
    edges = [_edge("a.py", "typing"), _edge("a.py", "os")]
    assert god_node_diagnostics(_packet(nodes, edges)) == []
    ranked = hotspots(_packet(nodes, edges), top_n=10)
    assert all(not h.node_id.startswith("mod::ref::") for h in ranked), (
        "外部引用节点不该进入热点列表（它不是项目模块）"
    )
    hot = ranked[0]
    assert hot.node_id == "mod::a.py"
    assert hot.cross_layer_ratio == 0.0, f"外部引用被算成跨层了：{hot}"


def test_impact_radius_direct_and_indirect():
    nodes = [_module("core.py"), _module("mid.py"), _module("app.py")]
    edges = [_edge("mid.py", "core.py"), _edge("app.py", "mid.py")]
    impact = impact_of(_packet(nodes, edges), ["mod::core.py"])

    assert impact.changed == ["mod::core.py"]
    assert impact.direct == ["mod::mid.py"]
    assert impact.indirect == ["mod::app.py"], "app 经 mid 间接依赖 core"
    assert impact.radius == 2


def test_unknown_changed_module_counts_as_added():
    packet = _packet([_module("a.py")], [])
    impact = impact_of(packet, ["mod::new.py"])
    assert impact.added == ["mod::new.py"] and impact.changed == []


def test_diff_reports_added_removed_and_changed():
    base = _packet([_module("a.py"), _module("gone.py")], [])
    head = _packet([_module("a.py"), _module("b.py")], [])
    for node in head.nodes:
        if node.path == "a.py":
            node.hash = "changed"
    impact = diff_packets(base, head)

    assert impact.removed == ["mod::gone.py"]
    assert "mod::b.py" in impact.added
    assert impact.changed == ["mod::a.py"]


@pytest.mark.parametrize("seed", [1, 42])
def test_hotspots_are_sorted_by_fan(seed):
    """排序必须确定（同 fan 按 id），否则诊断顺序每次都变。"""
    nodes = [_module("a.py"), _module("b.py")]
    edges = [_edge("a.py", "b.py")]
    first = [h.node_id for h in hotspots(_packet(nodes, edges))]
    second = [h.node_id for h in hotspots(_packet(nodes, edges))]
    assert first == second == ["mod::a.py", "mod::b.py"]


def test_removed_modules_dependents_are_in_scope():
    """删掉一个模块，引用它的模块必须进影响集合。

    删除之后 head 图里已无指向它的边（`impact_of` 只能在新图上跑），引用方自身
    hash 又没变 —— 不回 base 反查就没人重渲：页面继续写着"下游 y.py"并链到已
    删除的 y.md（实测一处删除留 6 条死链，直到下次全量才自愈）。
    """
    base = _packet(
        [_module("x.py"), _module("y.py")],
        [_edge("x.py", "y.py")],
    )
    head = _packet([_module("x.py")], [])

    impact = diff_packets(base, head)

    assert impact.removed == ["mod::y.py"]
    assert "mod::x.py" in impact.direct, "引用方必须进 scope，否则页面不重渲并留死链"


def test_removed_module_without_dependents_changes_nothing():
    base = _packet([_module("x.py"), _module("lonely.py")], [])
    head = _packet([_module("x.py")], [])

    impact = diff_packets(base, head)

    assert impact.direct == [], "没人引用的模块被删 ⇒ 无影响"


def test_god_node_is_not_cut_off_by_the_truncation_window():
    """真枢纽不能被 `top_n` 截断挤掉。

    先按 fan 取前 top_n 再过滤的话，一大群"同层互连、零跨层"的普通模块会把
    fan 一般却跨层占比高的真枢纽挤出候选 ⇒ 诊断直接返回空（实测漏报）。
    """
    nodes = [_module("hub.py", layer="layer::A")]
    edges = []
    peer_count = 12
    peers = [f"peer{i}.py" for i in range(peer_count)]
    nodes += [_module(p, layer="layer::A") for p in peers]
    for i, a in enumerate(peers):
        for b in peers[i + 1 :]:
            edges.append(_edge(a, b))
            edges.append(_edge(b, a))
    nodes.append(_module("o1.py", layer="layer::B"))
    nodes.append(_module("o2.py", layer="layer::B"))
    nodes.append(_module("o3.py", layer="layer::B"))
    for o in ("o1.py", "o2.py", "o3.py"):
        edges.append(_edge("hub.py", o))
    edges.append(_edge("hub.py", peers[0]))

    diags = god_node_diagnostics(_packet(nodes, edges), top_n=3, cross_layer_ratio=0.5)

    assert diags, "真枢纽被截断窗口挤掉了（先截断后过滤的后果）"
    assert diags[0].where == "mod::hub.py"
