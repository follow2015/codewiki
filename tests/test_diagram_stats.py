"""图例与统计（图表优化 P2-10）。

降噪本身是好事，藏起来就成了误导：读者看到 18 条边，会以为这层只有 18 条依赖。
所以每张系统图 / 层图 / 层内图底部都写一行「节点 N · 边 M · 已折叠 K」，并且
**K 按实际省略掉的条数算**（传递归约 + 枢纽折叠 + 跨组聚合都算），不是只报其中一项。

模块图的折叠用行内节点表达（"…另有 N 个符号" / "…另有 N 个下游模块"），粒度更细，
所以不重复加这行图注。
"""

from __future__ import annotations

import re

from codewiki.graph.builder import module_id
from codewiki.models import Edge, EdgeType, Node, NodeType
from codewiki.packetio import packet_from_parts
from codewiki.render.mermaid_macro import (
    MAX_VISIBLE_EDGES,
    STATS_STYLE,
    render_layer_detail,
    render_system_context,
)


def _module(path: str, layer: str = "l1") -> Node:
    return Node(id=module_id(path), type=NodeType.MODULE, path=path, layer_id=layer)


def _edge(source: str, target: str, **kwargs) -> Edge:
    return Edge(source=module_id(source), target=module_id(target), type=EdgeType.IMPORTS, **kwargs)


def _packet(nodes: list[Node], edges: list[Edge], layers: list[dict] | None = None):
    return packet_from_parts(
        title="t",
        layers=layers or [{"id": "l1", "name": "L1", "order": 1}],
        nodes=nodes,
        edges=edges,
    )


def _caption(text: str) -> str:
    """取出图注那一行的**标签文本**（不带节点 ID —— ID 里也有数字）。"""
    for line in text.splitlines():
        if "已折叠" not in line:
            continue
        start, end = line.find('["'), line.rfind('"]')
        assert start >= 0 and end > start, line
        return line[start + 2 : end]
    return ""


def _numbers(text: str) -> tuple[int, int, int]:
    line = _caption(text)
    assert line, f"没有找到图注:\n{text[-500:]}"
    digits = [int(part) for part in re.findall(r"\d+", line)]
    assert len(digits) == 3, line
    return digits[0], digits[1], digits[2]


def test_layer_overview_caption_reports_nodes_and_edges():
    """层间概览：节点数 = 层数，边数 = 层间边数，折叠 0（层内细节另有专图）。"""
    layers = [
        {"id": "top", "name": "top", "order": 1},
        {"id": "base", "name": "base", "order": 2},
    ]
    nodes = [_module("top/a.py", "top"), _module("base/b.py", "base")]
    edges = [Edge(source="top", target="base", type=EdgeType.LAYER_DEPENDS, weight=3)]
    text = render_system_context(_packet(nodes, edges, layers), include_modules=False)

    assert _numbers(text) == (2, 1, 0), _caption(text)


def test_stats_caption_is_a_note_not_a_structure_node():
    """图注要像图注：自定义样式、不套 subgraph，也不参与 linkStyle。"""
    text = render_system_context(_packet([_module("l1/a.py")], []), include_modules=False)

    assert "classDef stats " + STATS_STYLE in text, text
    assert "class n" in text and " stats" in text, text
    assert "subgraph" not in text, text


def test_layer_detail_reports_every_folded_edge():
    """层内图折叠数 = 原始同层边数 − 实际画出的边数。

    这里刻意同时造出**三种**折叠：**传递归约**（``m0 → m2`` 已被
    ``m0 → m1 → m2`` 覆盖）、**枢纽折叠**（``hub.py`` 被 31/72 个模块依赖）和
    **跨组聚合**（模块散在 3 个子目录里）。并且路径必须落在**分组**分支上 ——
    夹具若全平铺在一个目录，跨组聚合那条账就没人核。恒等式盯住
    "没画出来的都算过账"。
    """
    count = MAX_VISIBLE_EDGES + 10
    dirs = [f"l1/g{index % 3}/m{index}.py" for index in range(count)]
    nodes = [_module(path) for path in dirs] + [_module("l1/g0/hub.py")]
    edges = [_edge(dirs[index], dirs[index + 1]) for index in range(count - 1)]
    edges += [_edge(dirs[0], dirs[index]) for index in range(2, count - 1)]
    edges += [_edge(dirs[index], "l1/g0/hub.py") for index in range(31)]

    text = render_layer_detail(_packet(nodes, edges), layer_name="L1", members=nodes, min_confidence=0.5)
    shown_nodes, shown_edges, folded = _numbers(text)

    assert shown_nodes == len(nodes)
    assert "subgraph" in text, "3 个子目录必须真的分了组（否则本用例没测到聚合路径）"
    assert "被 31 个模块使用" in text, f"枢纽徽标必须出现（否则本用例没测到枢纽折叠）:\n{text[:300]}"
    assert folded > 0
    assert shown_edges + folded == len(edges), (
        f"画出的边 + 折叠数必须等于原始边数（{len(edges)}），实际 {shown_edges}+{folded}"
    )


def test_small_layer_reports_zero_folded():
    """没折叠就写 0 —— 读者由此知道"这就是全部"。"""
    nodes = [_module("l1/a.py"), _module("l1/b.py")]
    text = render_layer_detail(
        _packet(nodes, [_edge("l1/a.py", "l1/b.py")]),
        layer_name="L1",
        members=nodes,
        min_confidence=0.5,
    )

    assert _numbers(text) == (2, 1, 0), _caption(text)


__all__: list[str] = []
