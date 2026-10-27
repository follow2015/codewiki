"""图谱邻接索引（性能基线的关键）。

为什么需要它：**每个模块渲染一次**是天然 O(N) 的，如果每次渲染都线性扫描全量
边去求上下游，整体就是 O(N × E)。5000 模块 / 3 万边时实测 97 秒（出图 44s +
Wiki 75s 里绝大部分都在"找邻居"），而解析与建图加起来不到 1 秒。

索引把"找邻居"降为 O(degree)：

- ``out_edges`` / ``in_edges``：按端点分桶，一次建表多次查；
- ``symbols_of(module)``：符号按所属模块分桶（模块页要列符号）；
- ``module_of(symbol)``：符号 → 模块（O(1)，替代反复的 ``startswith`` 前缀比对，
  实测那一行被调用过 5000 万次）。

索引是**只读快照**：建完之后不得随 packet 变化（packet 在渲染期不变）。
"""

from __future__ import annotations

from codewiki.models import Edge, EdgeType, Node, NodeType
from codewiki.schemas import DataPacket


class GraphIndex:
    """一份数据包的邻接与归属索引（构建 O(N+E)，查询 O(deg)）。"""

    def __init__(self, packet: DataPacket) -> None:
        self.packet = packet
        self.nodes: dict[str, Node] = {n.id: n for n in packet.nodes}
        self.out_edges: dict[str, list[Edge]] = {}
        self.in_edges: dict[str, list[Edge]] = {}
        self.symbols_by_module: dict[str, list[Node]] = {}
        self._symbol_owner: dict[str, str] = {}

        for edge in packet.edges:
            self.out_edges.setdefault(edge.source, []).append(edge)
            self.in_edges.setdefault(edge.target, []).append(edge)

        for node in packet.nodes:
            if node.type == NodeType.SYMBOL and node.module_id:
                self.symbols_by_module.setdefault(node.module_id, []).append(node)
                self._symbol_owner[node.id] = node.module_id

        for group in self.symbols_by_module.values():
            group.sort(key=lambda s: s.line_start or 0)

    def module_of(self, node_id: str) -> str | None:
        """符号 → 所属模块；模块 id 原样返回。"""
        if node_id.startswith("mod::"):
            return node_id
        return self._symbol_owner.get(node_id)

    def symbols_of(self, module_id: str) -> list[Node]:
        return self.symbols_by_module.get(module_id, [])

    def dependency_edges(
        self, module_id: str, *, min_confidence: float, direction: str
    ) -> list[tuple[str, float, bool]]:
        """模块的依赖边（``direction`` = ``up`` / ``down``），已按置信度过滤。"""
        edges = self.in_edges.get(module_id, ()) if direction == "up" else self.out_edges.get(module_id, ())
        out: list[tuple[str, float, bool]] = []
        for edge in edges:
            if edge.type not in (EdgeType.IMPORTS, EdgeType.CALLS):
                continue
            if edge.confidence < min_confidence:
                continue
            other = edge.source if direction == "up" else edge.target
            out.append((other, edge.confidence, edge.static))
        return out

    def neighbors(self, module_id: str) -> tuple[dict[str, str], dict[str, str]]:
        """模块级上下游（IMPORTS + 本模块符号参与的 CALLS），去重。"""
        upstream: dict[str, str] = {}
        downstream: dict[str, str] = {}
        prefix = module_id.removeprefix("mod::")

        for edge in self.in_edges.get(module_id, ()):
            if edge.type == EdgeType.IMPORTS:
                upstream.setdefault(edge.source, "import")
            elif edge.type == EdgeType.CALLS and self._in_module(edge.target, prefix):
                upstream.setdefault(edge.source, "call")

        for edge in self.out_edges.get(module_id, ()):
            if edge.type == EdgeType.IMPORTS:
                downstream.setdefault(edge.target, "import")
            elif edge.type == EdgeType.CALLS and self._in_module(edge.source, prefix):
                downstream.setdefault(edge.target, "call")

        for symbol in self.symbols_of(module_id):
            for edge in self.in_edges.get(symbol.id, ()):
                if edge.type == EdgeType.CALLS:
                    upstream.setdefault(edge.source, "call")
            for edge in self.out_edges.get(symbol.id, ()):
                if edge.type == EdgeType.CALLS:
                    downstream.setdefault(edge.target, "call")
        return upstream, downstream

    def _in_module(self, symbol_id: str, module_prefix: str) -> bool:
        """符号是否属于某模块（O(1)，不做字符串前缀扫描）。"""
        owner = self._symbol_owner.get(symbol_id)
        return owner is not None and owner.removeprefix("mod::") == module_prefix


def build_index(packet: DataPacket) -> GraphIndex:
    """构建索引（渲染前调一次，之后所有模块复用）。"""
    return GraphIndex(packet)


__all__ = ["GraphIndex", "build_index"]
