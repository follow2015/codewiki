"""包根下的「标准库同名模块」：绝对导入必须落标准库，不能连到包内同文件。

场景（pydantic 实测）：分析根**就是包的内容目录**，包里有 ``typing.py`` /
``json.py`` / ``dataclasses.py`` / ``warnings.py`` / ``types.py``。这些文件的全名是
``<包>.typing``，而代码里的 ``import typing`` 指向**顶层** ``typing`` —— 标准库。

按"仓库根优先"的老规则会命中根下的 ``typing.py``，于是 187 条边把 100 多个模块
连成一个假枢纽（图上最粗的那几束线），环检测与热点排名一起被带偏。

判据分两半，缺一就退化成两种相反的错误：

* 绝对导入 + 首段是标准库 + **分析根本身是包** ⇒ 标准库（本文件前四个用例）；
* 带包名前缀的绝对导入 / 相对导入 ⇒ 仍然指向包内文件（否则本地依赖整片消失）；
* 分析根**不是**包（根下无 ``__init__.py``）⇒ 保持"根下同名文件优先"，
  因为那时 ``json.py`` 确实是顶层模块，``import json`` 会加载它（Python 语义）。
"""

from __future__ import annotations

from codewiki.graph import build_graph
from codewiki.graph.builder import external_id, module_id
from codewiki.models import EdgeType, FileModel, ImportFact, SymbolFact


def _fm(path, *, imports=(), symbols=()):
    """构造一个最小 FileModel；``imports`` 形如 ``(module, name, lineno)``。"""
    return FileModel(
        path=path,
        language="python",
        hash=path,
        imports=[
            ImportFact(module=m, name=n, lineno=ln, kind="import" if n is None else "from")
            for m, n, ln in imports
        ],
        symbols=[
            SymbolFact(name=n, qualname=q, kind="function", lineno=ln, endlineno=ln + 1)
            for n, q, ln in symbols
        ],
        calls=[],
    )


def _import_targets(result, source: str) -> set[str]:
    return {
        e.target
        for e in result.edges
        if e.type == EdgeType.IMPORTS and e.source == module_id(source)
    }


def test_bare_stdlib_import_in_package_root_goes_to_stdlib():
    """包根 + 包内有 ``typing.py``：``import typing`` 是标准库，不是本地文件。"""
    files = [
        _fm("__init__.py"),
        _fm("a.py", imports=[("typing", None, 1)]),
        _fm("typing.py", symbols=[("SpecialForm", "SpecialForm", 1)]),
    ]
    result = build_graph(files)

    targets = _import_targets(result, "a.py")
    assert external_id("typing") in targets, f"应落 External(typing)，实际边: {targets}"
    assert module_id("typing.py") not in targets, (
        "``import typing`` 被包内 typing.py 劫持 —— 该文件的全名是 <包>.typing，"
        "顶层名 typing 只可能是标准库"
    )


def test_from_stdlib_import_in_package_root_goes_to_stdlib():
    """``from dataclasses import dataclass`` 同理（包内有 ``dataclasses.py``）。"""
    files = [
        _fm("__init__.py"),
        _fm("_internal.py", imports=[("dataclasses", "dataclass", 3)]),
        _fm("dataclasses.py", symbols=[("dataclass", "dataclass", 1)]),
    ]
    result = build_graph(files)

    targets = _import_targets(result, "_internal.py")
    assert external_id("dataclasses") in targets, f"应落 External(dataclasses)，实际: {targets}"
    assert module_id("dataclasses.py") not in targets, "标准库 from-import 不得连到包内同名文件"


def test_ignored_local_twin_is_reported_as_diagnostic():
    """忽略包内同名文件要留痕：否则"为什么这条边没了"只能靠猜。"""
    files = [
        _fm("__init__.py"),
        _fm("a.py", imports=[("warnings", None, 1)]),
        _fm("warnings.py"),
    ]
    result = build_graph(files)

    hits = [d for d in result.diagnostics if d.code == "IMPORT_SHADOWED" and d.where == "a.py"]
    assert hits, "包内同名文件被当作标准库忽略时必须报 IMPORT_SHADOWED"
    assert hits[0].level == "info", (
        "这里选标准库是**正确**行为，不是可疑选择；级别应为 info，"
        f"否则会和真正的「仓根劫持包内」警告混在一起。实际: {hits[0].level}"
    )


def test_package_prefixed_import_still_hits_local_file():
    """带包名前缀 ``from pkg.typing import X``：明确指向包内文件。"""
    files = [
        _fm("__init__.py"),
        _fm("a.py", imports=[("pkg.typing", "X", 1)]),
        _fm("typing.py", symbols=[("X", "X", 1)]),
    ]
    result = build_graph(files, root_package="pkg")

    targets = _import_targets(result, "a.py")
    assert module_id("typing.py") in targets, (
        f"带包名前缀的绝对导入必须命中包内文件（否则本地依赖整片消失），实际: {targets}"
    )


def test_relative_import_still_hits_local_file():
    """相对导入 ``from .typing import X``：明确指向包内文件。"""
    files = [
        _fm("__init__.py"),
        _fm("a.py", imports=[(".typing", "X", 1)]),
        _fm("typing.py", symbols=[("X", "X", 1)]),
    ]
    result = build_graph(files)

    targets = _import_targets(result, "a.py")
    assert module_id("typing.py") in targets, (
        f"相对导入不得被标准库规则抢走，实际: {targets}"
    )


def test_repo_root_without_init_keeps_shadowing_stdlib():
    """分析根**不是**包（根下无 ``__init__.py``）：根下 ``json.py`` 是顶层模块，
    ``import json`` 确实会加载它 —— 此时连本地才是 Python 的真实语义。
    """
    files = [
        _fm("app.py", imports=[("json", None, 1)]),
        _fm("json.py", symbols=[("loads", "loads", 1)]),
    ]
    result = build_graph(files)

    targets = _import_targets(result, "app.py")
    assert module_id("json.py") in targets, (
        f"非包根场景下根目录同名文件会遮蔽标准库，应连本地，实际: {targets}"
    )


def test_non_stdlib_bare_import_keeps_existing_resolution():
    """护栏：新规则**只**作用于标准库同名文件，不顺手改普通绝对导入的解析。

    包内 ``helpers.py`` 与 ``from helpers import f`` 的关系不属于本次范围
    （要动它得先定 package-relative 还是 top-level 的口径）。这条用例锁住现状，
    防止"修 stdlib 误连"时把别的边一起改了却没人发现。
    """
    files = [
        _fm("__init__.py"),
        _fm("a.py", imports=[("helpers", "f", 1)]),
        _fm("helpers.py", symbols=[("f", "f", 1)]),
    ]
    result = build_graph(files)

    targets = _import_targets(result, "a.py")
    assert module_id("helpers.py") in targets, (
        f"非标准库的绝对导入应保持原有解析结果，实际: {targets}"
    )


__all__: list[str] = []
