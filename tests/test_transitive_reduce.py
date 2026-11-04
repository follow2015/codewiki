"""层内图的传递归约：**环内边一条都不能删**。

旧实现逐边问"去掉它之后，其余全部边里起点还能到终点吗"。判据用的是"其余全部边"
而不是"实际保留的边"，于是环里成对的出边互为备选路径、同时被判可删 —— 环上节点的
入边被清空，图上只剩一个孤立方框，图注还写着"省略 N 条"。只在"层内有环 + 边数超
阈值"时触发，恰好是最需要看清依赖的场景，所以必须锁住。

缩点后再归约（``nx.transitive_reduction`` 只吃 DAG）能同时满足两条：环内边全留、
分量间仍然去重。
"""

from __future__ import annotations

import random
import re
import time
from types import SimpleNamespace

import networkx as nx

from codewiki.graph.builder import module_id
from codewiki.models import Edge, EdgeType, Node, NodeType
from codewiki.packetio import packet_from_parts
from codewiki.render.mermaid_macro import (
    MAX_VISIBLE_EDGES,
    _transitive_reduce,
    node_id,
    render_layer_detail,
)


def _e(a: str, b: str) -> SimpleNamespace:
    return SimpleNamespace(source=a, target=b)


def _pairs(edges) -> set[tuple[str, str]]:
    return {(e.source, e.target) for e in edges}


def _reach(edges) -> dict[str, set[str]]:
    graph = nx.DiGraph(list(_pairs(edges)))
    return {node: set(nx.descendants(graph, node)) for node in graph}


def test_cycle_edges_are_never_trimmed():
    """环里 ``A→B`` / ``C→B`` 都有"备选路径"，逐边判断会两条都删，B 的入边被清空。"""
    edges = [_e("A", "B"), _e("C", "B"), _e("A", "C"), _e("C", "A")]
    kept, trimmed = _transitive_reduce(edges)

    assert {"B"} <= {e.target for e in kept}, "B 的入边不能全被删掉"
    assert trimmed == 0


def test_redundant_edges_between_components_are_still_trimmed():
    """缩点归约不是"什么都不删"：分量间被传递覆盖的边照旧省略。"""
    edges = [_e("A", "B"), _e("B", "C"), _e("A", "C")]
    kept, trimmed = _transitive_reduce(edges)

    assert _pairs(kept) == {("A", "B"), ("B", "C")}
    assert trimmed == 1


def test_reachability_is_preserved_on_random_graphs():
    """归约只能删"绕得过去"的边：随机图上的可达关系必须逐点守恒。"""
    rng = random.Random(7)
    for _ in range(40):
        n = rng.randint(4, 14)
        edges = [_e(f"m{rng.randrange(n)}", f"m{rng.randrange(n)}") for _ in range(rng.randint(3, 40))]
        edges = [e for e in edges if e.source != e.target]
        if not edges:
            continue

        kept, _ = _transitive_reduce(edges)

        assert _reach(kept) == _reach(edges)


def test_reduction_is_not_quadratic():
    """旧实现 O(E²)：1.5 万条边在开发机上要 20 秒级；缩点后实测 0.2 秒级。"""
    rng = random.Random(3)
    edges = []
    for _ in range(15000):
        low, high = sorted(rng.sample(range(3000), 2))
        edges.append(_e(f"m{low}", f"m{high}"))

    start = time.perf_counter()
    _transitive_reduce(edges)
    elapsed = time.perf_counter() - start

    assert elapsed < 5, f"归约退化成 O(E²) 了：1.5 万条边用了 {elapsed:.1f}s"


def test_cyclic_layer_draws_every_cycle_edge():
    """端到端：超阈值的环层里，环上节点的入边必须真的画出来。

    ``_transitive_reduce`` 单独测只是第一层 —— 这条盯的是"阈值 → 归约 → 渲染"整条
    链路：旧实现在这里会画出一个没有入边的孤立方框，图注还写"已折叠 4"。
    """
    paths = [f"l1/m{index}.py" for index in range(MAX_VISIBLE_EDGES + 8)]
    nodes = [Node(id=module_id(path), type=NodeType.MODULE, path=path, layer_id="l1") for path in paths]
    edges = [
        Edge(source=module_id(paths[index]), target=module_id(paths[index + 1]), type=EdgeType.IMPORTS)
        for index in range(len(paths) - 1)
    ]
    cycle = [
        ("l1/m0.py", "l1/m1.py"),
        ("l1/m2.py", "l1/m1.py"),
        ("l1/m0.py", "l1/m2.py"),
        ("l1/m2.py", "l1/m0.py"),
    ]
    edges += [Edge(source=module_id(a), target=module_id(b), type=EdgeType.IMPORTS) for a, b in cycle]

    packet = packet_from_parts(
        title="t", layers=[{"id": "l1", "name": "L1", "order": 1}], nodes=nodes, edges=edges
    )
    text = render_layer_detail(packet, layer_name="L1", members=nodes, min_confidence=0.5)

    drawn = {
        (src, dst)
        for line in text.splitlines()
        for src, dst in re.findall(
            r"(n[0-9a-f]{8})\s*(?:-->|-\.->)\s*(?:\(\(|\|\"[^\"]*\"\|)?\s*(n[0-9a-f]{8})", line
        )
    }
    for a, b in cycle:
        assert (node_id(module_id(a)), node_id(module_id(b))) in drawn, f"环边 {a} → {b} 被归约掉了"
    assert "已折叠 0" in text, "环内边被删过（图注的折叠数不为 0）"
