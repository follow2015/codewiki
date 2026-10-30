"""模块级调用链提取（``ai-diagram`` 的输入）。

时序图必须先有一条**静态分析得到的**调用链，否则模型只能编 —— 而"找不到依据的
步骤必须显式标注"是这套工具的硬规则（§4.2 第 1 条）。这一组用例锁四件事：

1. 符号级 ``CALLS`` 边要折回模块级（逐符号画等于把符号表重排一遍）；
2. 模块内部的调用（自环）不进链；
3. 深度 / 分支 / 条数三个上限都真的生效（否则一个有环的大仓库能算到天荒地老）；
4. 同样的图给同样的链（增量渲染依赖确定性）。
"""

from __future__ import annotations

from codewiki.ai.sequences import module_call_graph, pick_call_chains
from codewiki.models import Edge, EdgeType, Node, NodeType
from codewiki.render.jsonio import packet_from_parts


def _mod(path: str) -> Node:
    return Node(id=f"mod::{path}", type=NodeType.MODULE, path=path)


def _sym(module_path: str, qualname: str) -> Node:
    return Node(
        id=f"sym::{module_path}::{qualname}",
        type=NodeType.SYMBOL,
        name=qualname,
        module_id=f"mod::{module_path}",
        path=module_path,
    )


def _call(src: tuple[str, str], dst: tuple[str, str]) -> Edge:
    return Edge(source=f"sym::{src[0]}::{src[1]}", target=f"sym::{dst[0]}::{dst[1]}", type=EdgeType.CALLS)


def _packet(nodes, edges):
    return packet_from_parts(title="t", layers=[], nodes=nodes, edges=edges, annotations=[], diagnostics=[])


def _chain_graph():
    """a -> b -> c，且 a 无入边（是入口）。"""
    nodes = [_mod("a.py"), _mod("b.py"), _mod("c.py")]
    nodes += [_sym("a.py", "main"), _sym("b.py", "work"), _sym("c.py", "leaf")]
    edges = [_call(("a.py", "main"), ("b.py", "work")), _call(("b.py", "work"), ("c.py", "leaf"))]
    return _packet(nodes, edges)


def test_symbols_are_folded_to_modules():
    graph = module_call_graph(_chain_graph())

    assert graph == {"a.py": {"b.py"}, "b.py": {"c.py"}}


def test_intra_module_call_is_dropped():
    """模块内部符号互调在时序图上没有表达 —— 自己调自己画不出消息。"""
    nodes = [_mod("a.py"), _sym("a.py", "x"), _sym("a.py", "y")]
    packet = _packet(nodes, [_call(("a.py", "x"), ("a.py", "y"))])

    assert module_call_graph(packet) == {}


def test_chain_starts_from_entry_and_lists_steps():
    chains = pick_call_chains(_chain_graph(), top_n=3)

    assert len(chains) == 1
    assert chains[0].entry == "a.py"
    assert chains[0].steps == ["a.py -> b.py", "b.py -> c.py"]


def test_depth_limit_is_respected():
    chains = pick_call_chains(_chain_graph(), top_n=3, depth=2)

    assert chains[0].modules == ["a.py", "b.py"]


def test_branching_limit_is_respected():
    """每步只展开最热的几个：分支不设上限时，一个"到处都调"的模块会让走链爆炸。"""
    nodes = [_mod("hub.py")] + [_mod(f"leaf{i}.py") for i in range(10)]
    nodes += [_sym("hub.py", "run")] + [_sym(f"leaf{i}.py", f"f{i}") for i in range(10)]
    edges = [_call(("hub.py", "run"), (f"leaf{i}.py", f"f{i}")) for i in range(10)]

    chains = pick_call_chains(_packet(nodes, edges), top_n=1, depth=3, branching=2)

    assert len(chains[0].modules) == 2


def test_top_n_limits_number_of_chains():
    nodes = [_mod("a1.py"), _mod("a2.py"), _mod("b.py")]
    nodes += [_sym("a1.py", "f"), _sym("a2.py", "f"), _sym("b.py", "f")]
    edges = [_call(("a1.py", "f"), ("b.py", "f")), _call(("a2.py", "f"), ("b.py", "f"))]

    chains = pick_call_chains(_packet(nodes, edges), top_n=1)

    assert len(chains) == 1


def test_no_calls_means_no_chains():
    assert pick_call_chains(_packet([_mod("a.py")], []), top_n=3) == []


def test_cycles_without_entry_still_yield_chains():
    """全仓成环（没有入度为 0 的模块）时退化为按出度挑，而不是返回空 ——
    一个互相调用的子图里同样存在值得看的顺序。"""
    nodes = [_mod("a.py"), _mod("b.py")]
    nodes += [_sym("a.py", "f"), _sym("b.py", "g")]
    edges = [_call(("a.py", "f"), ("b.py", "g")), _call(("b.py", "g"), ("a.py", "f"))]

    chains = pick_call_chains(_packet(nodes, edges), top_n=1, depth=4)

    assert chains, "成环时不应退化成空"
    assert len(chains[0].modules) >= 2


def test_result_is_deterministic():
    """同一份图两轮必须给同样的链：否则增量渲染会反复重生成同一张图。"""
    packet = _chain_graph()

    first = pick_call_chains(packet, top_n=3)
    second = pick_call_chains(packet, top_n=3)

    assert [c.steps for c in first] == [c.steps for c in second]
