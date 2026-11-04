"""层内按目录二级分组（图表优化 P1-6）。

系统图只画层间之后，每层一张模块级图。层内还有几十个模块时，平铺出来仍是一团线 ——
目录是这里唯一现成的二级结构：一个子目录一个框，跨目录的依赖收成一条带次数的箭头。

判据：
* 只有一个目录时**不**分组（一个框等于没分）；
* 分组后节点标签去掉层前缀和框名（框标题已经写明，重复三遍纯属噪声）；
* 跨组的边聚合成一条、标次数；组内的边逐条画（"这个包里谁依赖谁"是细节）；
* 目录太多时不分组（30 个目录各 1 个模块，框本身成了噪声）；
* 层内图只画本层，不留其它层的空框；
* 同源同宿的平行 import 只画一条箭头（否则 ``import x`` + ``from x import y``
  会画成两行一模一样的箭头，图注还把它们都算进"边"）；
* 层图里的注解只留一句提示 —— 整段 AI 说明会把框撑高到超过尺寸阈值。
"""

from __future__ import annotations

from codewiki.models import Edge, EdgeType, Node, NodeType
from codewiki.packetio import packet_from_parts
from codewiki.render.mermaid_macro import (
    LAYER_NOTE_LIMIT,
    MAX_DIR_GROUPS,
    node_id,
    render_layer_detail,
)
from codewiki.schemas import Annotation


def _module(path: str, layer: str = "v1") -> Node:
    return Node(id=f"mod::{path}", type=NodeType.MODULE, path=path, layer_id=layer)


def _edge(source: str, target: str, **kwargs) -> Edge:
    return Edge(source=f"mod::{source}", target=f"mod::{target}", type=EdgeType.IMPORTS, **kwargs)


def _packet(nodes: list[Node], edges: list[Edge], annotations: list[Annotation] | None = None):
    return packet_from_parts(
        title="t",
        layers=[{"id": "v1", "name": "v1", "order": 1}],
        nodes=nodes,
        edges=edges,
        annotations=annotations,
    )


def _render(nodes: list[Node], edges: list[Edge], annotations: list[Annotation] | None = None) -> str:
    return render_layer_detail(
        _packet(nodes, edges, annotations),
        layer_name="v1",
        members=nodes,
        min_confidence=0.5,
    )


def _boxes(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if "subgraph" in line]


def test_single_directory_layer_has_no_boxes():
    """整层都在同一个目录下时不分组：一个框等于没分。"""
    nodes = [_module("v1/a.py"), _module("v1/b.py")]
    text = _render(nodes, [_edge("v1/a.py", "v1/b.py")])

    assert not _boxes(text), f"单目录层不该套框:\n{text}"
    assert '["a.py"]' in text, f"层前缀仍要去掉（层名在页面标题上）:\n{text}"


def test_modules_are_grouped_by_subdirectory():
    """两个子目录 → 两个框，框标题写目录名和模块数。"""
    nodes = [
        _module("pkg/core/a.py"),
        _module("pkg/core/b.py"),
        _module("pkg/util/c.py"),
    ]
    text = _render(nodes, [])

    boxes = _boxes(text)
    assert len(boxes) == 2, f"两个子目录应各一个框:\n{text}"
    assert any("core" in box and "2" in box for box in boxes), text
    assert any("util" in box and "1" in box for box in boxes), text


def test_node_labels_drop_layer_prefix_and_box_name():
    """标签去掉层前缀**和**框名：框标题已经写明 ``core``，节点再叫 ``core/a.py`` 是重复。"""
    nodes = [_module("pkg/core/a.py"), _module("pkg/util/c.py")]
    text = _render(nodes, [])

    assert '["a.py"]' in text, text
    assert '["c.py"]' in text, text
    assert "pkg/" not in text and "core/a.py" not in text, f"标签里不该再有目录前缀:\n{text}"


def test_intra_group_edges_stay_individual():
    """组内的边逐条画 —— 它们是"这个包里谁依赖谁"的细节，不能被聚合吃掉。"""
    nodes = [_module("pkg/core/a.py"), _module("pkg/core/b.py"), _module("pkg/util/c.py")]
    text = _render(nodes, [_edge("pkg/core/a.py", "pkg/core/b.py")])

    a, b = node_id("mod::pkg/core/a.py"), node_id("mod::pkg/core/b.py")
    assert f"{a} --> {b}" in text, f"组内边要保留具体端点:\n{text}"


def test_cross_group_edges_are_aggregated_with_a_count():
    """跨目录的多条依赖收成一条，箭头标次数 —— 组间只关心"依赖多深"。"""
    nodes = [
        _module("pkg/core/a.py"),
        _module("pkg/core/b.py"),
        _module("pkg/util/c.py"),
        _module("pkg/util/d.py"),
    ]
    edges = [
        _edge("pkg/util/c.py", "pkg/core/a.py"),
        _edge("pkg/util/d.py", "pkg/core/b.py"),
        _edge("pkg/core/a.py", "pkg/util/c.py"),
    ]
    text = _render(nodes, edges)

    util_box = node_id("dir::v1::util")
    core_box = node_id("dir::v1::core")
    assert f"{util_box} -->|2| {core_box}" in text, f"同方向的跨组边要聚合成一条:\n{text}"
    assert f"{core_box} -->|1| {util_box}" in text, f"反方向要单独一条:\n{text}"
    assert f"{node_id('mod::pkg/util/c.py')} --> {node_id('mod::pkg/core/a.py')}" not in text


def test_too_many_directories_fall_back_to_flat():
    """目录太多时平铺：几十个只有一两个模块的框比平铺更乱。"""
    nodes = [_module(f"pkg/d{index:02d}/m.py") for index in range(MAX_DIR_GROUPS + 1)]
    text = _render(nodes, [])

    assert not _boxes(text), f"超过 {MAX_DIR_GROUPS} 个目录时应平铺:\n{text[:400]}"


def test_all_type_only_group_edges_keep_the_dashed_style():
    """整组跨目录依赖都来自 ``TYPE_CHECKING`` 时保留虚线与标签，不能伪装成运行时依赖。"""
    nodes = [_module("pkg/core/a.py"), _module("pkg/util/c.py")]
    text = _render(nodes, [_edge("pkg/util/c.py", "pkg/core/a.py", type_only=True, static=False)])

    assert "仅类型 1" in text, f"仅类型的聚合边要带标签:\n{text}"


def test_parallel_imports_draw_a_single_arrow():
    """``import x`` 与 ``from x import y`` 是两条边，模块粒度上只有一条依赖。

    旧归约把这样一对同时判成可删，重复行一直没露过面；改成缩点归约后 IPIP 实测
    3 组画成两行一模一样的箭头。图注也不该把它们算成两条边。
    """
    nodes = [_module("pkg/core/a.py"), _module("pkg/core/b.py")]
    edges = [
        _edge("pkg/core/a.py", "pkg/core/b.py"),
        _edge("pkg/core/a.py", "pkg/core/b.py", alias="b"),
    ]
    text = _render(nodes, edges)

    a, b = node_id("mod::pkg/core/a.py"), node_id("mod::pkg/core/b.py")
    assert text.count(f"{a} --> {b}") == 1, f"平行 import 只能画一条箭头:\n{text}"
    assert "边 1 ·" in text, f"图注要按去重后的条数记:\n{text}"


def test_parallel_imports_with_different_rendering_both_stay():
    """渲染出来不一样的两条边（权重/仅类型）不能合并 —— 它们是真实差异。"""
    nodes = [_module("pkg/core/a.py"), _module("pkg/core/b.py")]
    edges = [
        _edge("pkg/core/a.py", "pkg/core/b.py"),
        _edge("pkg/core/a.py", "pkg/core/b.py", type_only=True),
    ]
    text = _render(nodes, edges)

    a, b = node_id("mod::pkg/core/a.py"), node_id("mod::pkg/core/b.py")
    assert f"{a} --> {b}" in text, text
    assert "仅类型" in text, f"仅类型那条要保留:\n{text}"


def test_long_annotation_is_clipped_in_layer_diagram():
    """层图是鸟瞰图：整段 AI 说明会把框撑到超过尺寸阈值，只留一句提示。"""
    nodes = [_module("pkg/core/a.py"), _module("pkg/util/c.py")]
    note = "这个模块负责把请求转成内部调用，并在失败时回退到本地缓存。" * 5
    text = _render(nodes, [], [Annotation(target="mod::pkg/core/a.py", note=note, source="ai")])

    assert note not in text, f"完整注解不该进层图:\n{text}"
    assert note[:LAYER_NOTE_LIMIT] in text, f"要留前 {LAYER_NOTE_LIMIT} 个字做提示:\n{text}"
    assert "…" in text, f"截断要有省略号:\n{text}"


def test_short_annotation_survives_intact():
    """短注解本来就一屏放得下，不该被截。"""
    nodes = [_module("pkg/core/a.py"), _module("pkg/util/c.py")]
    text = _render(nodes, [], [Annotation(target="mod::pkg/core/a.py", note="下单主链路。", source="human")])

    assert "下单主链路。" in text, text
    assert "…" not in text, f"没超长就不该加省略号:\n{text}"


__all__: list[str] = []
