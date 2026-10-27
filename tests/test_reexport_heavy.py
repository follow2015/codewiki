"""重度 re-export 专项夹具（§2.4 第 2 条：作为 Analyzer 的**准入门槛**）。

静态分析最易翻车的就是 re-export。本夹具覆盖四种形态，逐条断言：

1. **一跳**：``from pkg.a import A`` → ``pkg/a.py::A``；
2. **别名**：``from pkg.b import B as AliasB`` → 对外名 AliasB 指向 ``pkg/b.py::B``；
3. **二跳（多跳链）**：``pkg/__init__`` 的 ``C`` 来自 ``pkg/c.py``，而 c 里的 ``C``
   又是 ``pkg/d.py::D`` 的别名 ⇒ 必须**穿透到 d**，不能停在 c；
4. **成环**：``cyc/__init__ ← cyc/x ← cyc/__init__`` ⇒ 不许死循环、不许崩，
   且要留下可观测的诊断（或降级为模块级边），**绝不猜测**。
"""
from __future__ import annotations

from codewiki.graph import build_graph
from codewiki.graph.builder import module_id, symbol_id
from codewiki.models import EdgeType
from tests.conftest import FIXTURES, parse_all

HEAVY = FIXTURES / "reexport_py"


def _edges(result, etype: EdgeType):
    return {(e.source, e.target) for e in result.edges if e.type == etype}


def test_single_hop_reexport():
    result = build_graph(parse_all(HEAVY))

    assert (_exports_target(result, "entry.py", "pkg/a.py")) is True, "一跳 re-export 未解析"


def test_aliased_reexport_resolves_to_real_symbol():
    result = build_graph(parse_all(HEAVY))
    node_ids = {n.id for n in result.nodes}

    assert symbol_id("pkg/b.py", "B") in node_ids
    assert (_exports_target(result, "entry.py", "pkg/b.py")) is True, "别名 re-export 未解析"


def test_multi_hop_chain_is_penetrated_to_the_leaf():
    """``pkg.C`` → ``pkg/c.py.C`` → ``pkg/d.py.D``：必须穿透两级到 d。"""
    result = build_graph(parse_all(HEAVY))

    assert (_exports_target(result, "entry.py", "pkg/d.py")) is True, (
        "多跳 re-export 未穿透：停在 pkg/c.py 而不是 pkg/d.py"
    )


def test_cycle_does_not_hang_and_is_observable():
    """成环链：不死循环、不崩溃，并有可观测结论（诊断或降级边）。"""
    result = build_graph(parse_all(HEAVY))

    codes = {d.code for d in result.diagnostics}
    pairs = _edges(result, EdgeType.IMPORTS)
    cycle_visible = (
        module_id("cyc/x.py"),
        module_id("cyc/__init__.py"),
    ) in pairs or (
        module_id("cyc/__init__.py"),
        module_id("cyc/x.py"),
    ) in pairs
    assert cycle_visible or "REEXPORT_HOP_LIMIT" in codes, (
        f"成环 re-export 既无诊断也无降级边：{sorted(codes)}"
    )


def test_all_public_names_of_the_package_are_resolvable():
    """``__all__`` 里的三个名字都必须能落到真实定义，不能有'悬空'的对外名。"""
    result = build_graph(parse_all(HEAVY))
    entry_imports = _edges(result, EdgeType.IMPORTS)
    targets = {t for s, t in entry_imports if s == module_id("entry.py")}

    for path in ("pkg/a.py", "pkg/b.py", "pkg/d.py"):
        assert module_id(path) in targets, f"entry 未解析到 {path}"


def _exports_target(result, source_path: str, target_path: str) -> bool:
    return (module_id(source_path), module_id(target_path)) in _edges(result, EdgeType.IMPORTS)
