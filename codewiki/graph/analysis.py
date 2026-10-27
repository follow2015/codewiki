"""图谱诊断与影响分析（§2.5 / §4.6.3）：环、God Node、变更影响。

三类结论的共同纪律（§4.6.3「可追溯审计」）：**每条结论都能回溯到依据** ——
环给出环上节点、God Node 给出出入度与跨层占比、diff 给出改动节点与影响半径。
只给"你可能有问题"而不给依据的诊断，等于把排查成本丢给读者。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx

from codewiki.models import EdgeType, NodeType
from codewiki.schemas import DataPacket, Diagnostic

DEFAULT_CROSS_LAYER_RATIO = 0.5
DEFAULT_GOD_TOP_N = 10

_GOD_CANDIDATE_FACTOR = 5
DEFAULT_GOD_MIN_FAN = 3


@dataclass
class Cycle:
    """一个环（文件级 SCC 或层级环）。"""

    kind: str
    members: list[str]
    edges: list[tuple[str, str]] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.members)


def module_graph(packet: DataPacket) -> nx.DiGraph:
    """模块级有向图（只取 IMPORTS/CALLS/USES_DYNAMIC 的 mod::→mod:: 边）。

    符号级边不计入：环检测关心的是**模块之间的循环依赖**，符号边会把同一个
    模块对放大成几十条边，干扰 SCC 的判定（自环尤其容易被误报）。
    """
    graph = nx.DiGraph()
    for node in packet.nodes:
        if node.type == NodeType.MODULE and node.id.startswith("mod::"):
            graph.add_node(node.id, path=node.path, layer=node.layer_id)
    for edge in packet.edges:
        if edge.type not in (EdgeType.IMPORTS, EdgeType.CALLS, EdgeType.USES_DYNAMIC):
            continue
        if edge.source.startswith("mod::") and edge.target.startswith("mod::"):
            graph.add_edge(edge.source, edge.target)
    return graph


def find_module_cycles(packet: DataPacket) -> list[Cycle]:
    """文件级环：Tarjan SCC（``nx.strongly_connected_components``）。

    SCC 节点数 > 1 是真环；单节点 SCC 只有在**存在自环**时才算（模块 import 自己
    通常源于重导出的假象，但确实存在，且是真实问题）。
    """
    graph = module_graph(packet)
    cycles: list[Cycle] = []
    for members in nx.strongly_connected_components(graph):
        if len(members) > 1:
            subgraph = graph.subgraph(members)
            ordered = sorted(members)
            edges = [(u, v) for u, v in subgraph.edges() if u != v]
            cycles.append(Cycle(kind="module", members=ordered, edges=sorted(edges)))
        else:
            only = next(iter(members))
            if graph.has_edge(only, only):
                cycles.append(Cycle(kind="module", members=[only], edges=[(only, only)]))
    return sorted(cycles, key=lambda c: (-c.size, c.members))


def find_layer_cycles(packet: DataPacket) -> list[Cycle]:
    """层级环：``LAYER_DEPENDS`` 上的简单环（分层失效，§2.5 严重诊断）。"""
    graph = nx.DiGraph()
    for edge in packet.edges:
        if edge.type == EdgeType.LAYER_DEPENDS:
            graph.add_edge(edge.source, edge.target)
    cycles: list[Cycle] = []
    for members in nx.simple_cycles(graph):
        subgraph = graph.subgraph(members)
        cycles.append(
            Cycle(
                kind="layer",
                members=sorted(members),
                edges=sorted((u, v) for u, v in subgraph.edges() if u != v),
            )
        )
    return sorted(cycles, key=lambda c: (-c.size, c.members))


def cycles_to_diagnostics(cycles: list[Cycle]) -> list[Diagnostic]:
    """环 → 诊断；层级环是 error（分层失效），模块环是 warning。"""
    out: list[Diagnostic] = []
    for cycle in cycles:
        level = "error" if cycle.kind == "layer" else "warning"
        code = "LAYER_CYCLE" if cycle.kind == "layer" else "MODULE_CYCLE"
        members = " → ".join(_short(m) for m in cycle.members)
        out.append(
            Diagnostic(
                level=level,
                code=code,
                message=f"检测到{('层级' if cycle.kind == 'layer' else '模块')}循环依赖（{cycle.size} 个节点）: {members}",
                where=cycle.members[0],
            )
        )
    return out


def _short(node_id: str) -> str:
    return node_id.removeprefix("mod::").removeprefix("layer::")


@dataclass
class Hotspot:
    """一个热点/上帝模块。"""

    node_id: str
    in_degree: int
    out_degree: int
    cross_layer_ratio: float
    layer_id: str | None = None

    @property
    def fan(self) -> int:
        return self.in_degree + self.out_degree


def hotspots(packet: DataPacket, *, top_n: int = DEFAULT_GOD_TOP_N) -> list[Hotspot]:
    """按扇入扇出与跨层占比排序的热点节点（不出诊断，交给调用方决定是否报）。"""
    graph = module_graph(packet)
    layers = {n.id: n.layer_id for n in packet.nodes}
    out: list[Hotspot] = []
    for node_id in graph.nodes:
        in_degree = graph.in_degree(node_id)
        out_degree = graph.out_degree(node_id)
        if in_degree + out_degree == 0:
            continue
        own = layers.get(node_id)
        if own is None:
            continue
        neighbours = list(graph.predecessors(node_id)) + list(graph.successors(node_id))
        scoped = [other for other in neighbours if layers.get(other) is not None]
        cross = sum(1 for other in scoped if layers.get(other) != own)
        out.append(
            Hotspot(
                node_id=node_id,
                in_degree=in_degree,
                out_degree=out_degree,
                cross_layer_ratio=(cross / len(scoped)) if scoped else 0.0,
                layer_id=own,
            )
        )
    out.sort(key=lambda h: (-h.fan, h.node_id))
    return out[:top_n]


def god_node_diagnostics(
    packet: DataPacket,
    *,
    top_n: int = DEFAULT_GOD_TOP_N,
    cross_layer_ratio: float = DEFAULT_CROSS_LAYER_RATIO,
    min_fan: int = DEFAULT_GOD_MIN_FAN,
) -> list[Diagnostic]:
    """God Node 诊断：**同时**满足高扇入扇出与高跨层占比才算。

    只看度数会把"正常的公共工具模块"误报成上帝模块（度数高但几乎不跨层，
    那是设计好的复用）；只看跨层又会把"只有一个跨层邻居的叶子模块"算成枢纽。

    ``top_n`` 只限制**最终**报几条：先按 fan 截断再过滤会让真正的枢纽被切掉
    （实测：10 个同层高 fan 模块把 fan 一般但跨层 100% 的枢纽挤出前 N 名，
    诊断直接返回空）。故这里取一个更大的候选窗口再过滤。
    """
    out: list[Diagnostic] = []
    for hot in hotspots(packet, top_n=max(top_n * _GOD_CANDIDATE_FACTOR, top_n + 20)):
        if hot.fan < min_fan or hot.cross_layer_ratio < cross_layer_ratio:
            continue
        out.append(
            Diagnostic(
                level="warning",
                code="GOD_NODE",
                message=(
                    f"枢纽模块：入度 {hot.in_degree} / 出度 {hot.out_degree}，"
                    f"跨层依赖占比 {hot.cross_layer_ratio:.0%}（≥ {cross_layer_ratio:.0%}）—— 建议下沉或拆分"
                ),
                where=hot.node_id,
            )
        )
    return out


@dataclass
class Impact:
    """变更影响分析结果。"""

    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    direct: list[str] = field(default_factory=list)
    indirect: list[str] = field(default_factory=list)

    @property
    def radius(self) -> int:
        """影响半径 = 直接 + 间接（不含改动自身）。"""
        return len(self.direct) + len(self.indirect)


def impact_of(packet: DataPacket, changed_ids: list[str], *, max_depth: int = 10) -> Impact:
    """反向遍历 IMPORTS/CALLS 求受影响集合（§2.5 变更影响分析）。

    ``direct`` = 直接依赖改动模块的节点；``indirect`` = 更深层的传递影响。
    """
    graph = module_graph(packet)
    reverse = graph.reverse(copy=True)
    known = {n for n in graph.nodes}
    result = Impact()
    for node_id in changed_ids:
        if node_id in known:
            result.changed.append(node_id)
        else:
            result.added.append(node_id)
    for node_id in changed_ids:
        if node_id not in known:
            continue

    direct: set[str] = set()
    indirect: set[str] = set()
    for node_id in result.changed:
        for other in reverse.successors(node_id):
            if other == node_id or other in result.changed:
                continue
            direct.add(other)
        for other, depth in nx.single_source_shortest_path_length(reverse, node_id, cutoff=max_depth).items():
            if other == node_id or other in result.changed or depth <= 1:
                continue
            indirect.add(other)

    result.direct = sorted(direct)
    result.indirect = sorted(indirect - direct)
    return result


def diff_packets(base: DataPacket, head: DataPacket) -> Impact:
    """两个快照之间的差异 → 影响分析（改动 = 新增 + 删除 + 内容 hash 变化）。"""
    base_nodes = {n.id: n for n in base.nodes if n.type == NodeType.MODULE}
    head_nodes = {n.id: n for n in head.nodes if n.type == NodeType.MODULE}

    added = sorted(set(head_nodes) - set(base_nodes))
    removed = sorted(set(base_nodes) - set(head_nodes))
    changed = sorted(
        node_id
        for node_id in set(base_nodes) & set(head_nodes)
        if (base_nodes[node_id].hash or "") != (head_nodes[node_id].hash or "")
    )
    result = impact_of(head, changed)
    result.added = added
    result.removed = removed
    _absorb_neighbours(base, removed, result)
    _absorb_neighbours(head, added, result)
    return result


def _absorb_neighbours(packet: DataPacket, ids: list[str], result: Impact) -> None:
    """把 ``ids`` 在图里的**双向邻居**并入 ``direct``（比"谁引用它"更宽）。

    模块页同时展示"上游（谁依赖我）"与"下游（我依赖谁）"，所以一条边**任一端**
    新增或被删，另一端的页面内容就变了 —— 而它自身 hash 没变、也不在 changed 里，
    只沿 reverse 传播永远找不到它：

    - 删除模块 y：引用方 x 的页面仍写着"下游 y.py"并链到 y.md（死链）；
    - 新增模块 y：被 y 依赖的 z 的页面没写"下游 y"，而 y 的页面却链到 z，
      两边互相矛盾。

    两种情况都要到下次全量才自愈，`--incremental` 期间产物是错的。实测症状：
    删一个模块报"重渲 0 / 复用 N"，而产物里 6 条链接指向不存在的页。
    """
    if not ids:
        return
    graph = module_graph(packet)
    neighbours: set[str] = set()
    for node_id in ids:
        if node_id not in graph:
            continue
        neighbours.update(graph.successors(node_id))
        neighbours.update(graph.predecessors(node_id))
    skip = set(result.changed) | set(result.added) | set(result.removed)
    neighbours -= skip
    if neighbours:
        result.direct = sorted(set(result.direct) | neighbours)


__all__ = [
    "DEFAULT_CROSS_LAYER_RATIO",
    "DEFAULT_GOD_MIN_FAN",
    "DEFAULT_GOD_TOP_N",
    "Cycle",
    "Hotspot",
    "Impact",
    "cycles_to_diagnostics",
    "diff_packets",
    "find_layer_cycles",
    "find_module_cycles",
    "god_node_diagnostics",
    "hotspots",
    "impact_of",
    "module_graph",
]
