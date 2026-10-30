"""从数据包提取模块级调用链 —— ``ai-diagram`` 的输入。

时序图要有"一条真实的调用链"才有意义：模型拿到的证据是 ``source -> target``
列表，看不见链就没法判断顺序与参与方。这份证据只能从静态图里取，不能让模型
自己编（§4.2 第 1 条：找不到依据的步骤必须显式标注为推断）。

两个刻意的选择：

* **折回模块级**。``CALLS`` 边是符号级的（``sym::app.py::run_order``），逐符号
  画会把同一对模块放大成几十条边 —— 时序图的价值是"谁按什么顺序调了谁"，
  不是把符号表重新排一遍。
* **每步只看最热的几个邻居**。找"最长路径"在有环图上是指数级的；每步只展开
  出度最大的 ``branching`` 个目标，深度再限死，代价就从"仓库多大算多久"变成
  "常数级"。这条链是**代表性样本**，不是"最长路径"的精确解 —— 名字上不假装
  它是。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from codewiki.models import EdgeType, NodeType
from codewiki.schemas import DataPacket

DEFAULT_BRANCHING = 3

DEFAULT_DEPTH = 5


@dataclass(frozen=True)
class CallChain:
    """一条调用链。``steps`` 是给人看的 ``a.py -> b.py``；``entry`` 是起点模块路径。"""

    entry: str
    steps: list[str]
    modules: list[str]

    @property
    def digest_source(self) -> str:
        return "\n".join(self.steps)


def _module_of(node_id: str, symbol_to_module: dict[str, str]) -> str | None:
    """把节点 id 折到所属模块；``ext::`` 之类返回 ``None``（第三方不在时序图里）。"""
    if node_id.startswith("mod::"):
        return node_id[5:]
    return symbol_to_module.get(node_id)


def module_call_graph(packet: DataPacket) -> dict[str, set[str]]:
    """模块级 CALLS 邻接表（``模块路径 -> 被调模块路径集合``）。

    自环（模块内部符号互调）直接丢掉：它在时序图上没有任何表达。
    """
    symbol_to_module = {
        node.id: node.module_id for node in packet.nodes if node.type == NodeType.SYMBOL and node.module_id
    }
    symbol_to_module = {
        key: value[5:] if value.startswith("mod::") else value for key, value in symbol_to_module.items()
    }
    by_source: dict[str, set[str]] = defaultdict(set)
    for edge in packet.edges:
        if edge.type != EdgeType.CALLS:
            continue
        source = _module_of(edge.source, symbol_to_module)
        target = _module_of(edge.target, symbol_to_module)
        if source and target and source != target:
            by_source[source].add(target)
    return dict(by_source)


def _longest_from(graph: dict[str, set[str]], start: str, *, depth: int, branching: int) -> list[str]:
    """从 ``start`` 出发贪心找一条尽量长的链（不回头、限深、限分支）。"""
    best: list[str] = [start]

    def walk(node: str, path: list[str], seen: set[str]) -> None:
        nonlocal best
        if len(path) > len(best):
            best = list(path)
        if len(path) >= depth:
            return
        for nxt in sorted(graph.get(node, ()))[:branching]:
            if nxt in seen:
                continue
            seen.add(nxt)
            path.append(nxt)
            walk(nxt, path, seen)
            path.pop()
            seen.discard(nxt)

    walk(start, [start], {start})
    return best


def pick_call_chains(
    packet: DataPacket,
    *,
    top_n: int = 3,
    depth: int = DEFAULT_DEPTH,
    branching: int = DEFAULT_BRANCHING,
) -> list[CallChain]:
    """挑出 ``top_n`` 条调用链。

    起点优先取**没有入边的模块**（真正的入口：CLI、路由注册、``main``），
    出度大的排前面。全仓都是环（没有入口）时退化为"出度最大的几个"，
    而不是返回空 —— 一个互相调用的子图里同样存在值得看的顺序。
    """
    if top_n <= 0:
        return []
    graph = module_call_graph(packet)
    if not graph:
        return []

    called = {target for targets in graph.values() for target in targets}
    entries = [node for node in graph if node not in called]
    pool = entries or list(graph)
    pool.sort(key=lambda node: (-len(graph.get(node, ())), node))

    chains: list[CallChain] = []
    used_entries: set[str] = set()
    for entry in pool:
        if len(chains) >= top_n:
            break
        if entry in used_entries:
            continue
        used_entries.add(entry)
        modules = _longest_from(graph, entry, depth=depth, branching=branching)
        if len(modules) < 2:
            continue
        chains.append(
            CallChain(
                entry=entry,
                steps=[f"{a} -> {b}" for a, b in zip(modules, modules[1:], strict=False)],
                modules=modules,
            )
        )
    return chains


__all__ = [
    "DEFAULT_BRANCHING",
    "DEFAULT_DEPTH",
    "CallChain",
    "module_call_graph",
    "pick_call_chains",
]
