"""包根下的「标准库同名模块」：绝对导入必须落标准库，不能连到包内同文件。

场景（pydantic 实测）：分析根**就是包的内容目录**，包里有 ``typing.py`` /
``json.py`` / ``dataclasses.py`` / ``warnings.py`` / ``types.py``。这些文件的全名是
``<包>.typing``，而代码里的 ``import typing`` 指向**顶层** ``typing`` —— 标准库。

按"仓库根优先"的老规则会命中根下的 ``typing.py``，于是 187 条边把 100 多个模块
连成一个假枢纽（图上最粗的那几束线），环检测与热点排名一起被带偏。

判据分两半，缺一就退化成两种相反的错误：

* 绝对导入 + 首段是标准库 + **本地同名文件在包内** ⇒ 标准库（本文件前四个用例）。
  "在包内"有两种形态：分析根本身是包（根下有 ``__init__.py``），或导入方位于子目录
  **且目录链上有 ``__init__.py``**（显式包与 PEP 420 命名空间包一样，其同名文件的
  全名都带包前缀）；
* 带包名前缀的绝对导入 / 相对导入 ⇒ 仍然指向包内文件（否则本地依赖整片消失）；
* 分析根**不是**包（根下无 ``__init__.py``）⇒ 根下同名文件保持优先，因为那时
  ``json.py`` 就在 ``sys.path[0]`` 上、确实是顶层模块，``import json`` 会加载它；
* **整条目录链都没有 ``__init__.py``**（``scripts/a.py`` + ``scripts/json.py``）⇒
  这是脚本目录：``python scripts/a.py`` 时 ``sys.path[0]`` 就是 ``scripts/``，
  ``import json`` 真的加载本地文件。PEP 420 只在"作为包被导入"时成立，不覆盖脚本
  场景。这里保留本地依赖，并留一条 ``STDLIB_NAME_IN_SCRIPT_DIR`` 的 info。
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
    return {e.target for e in result.edges if e.type == EdgeType.IMPORTS and e.source == module_id(source)}


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
        "``import typing`` 被包内 typing.py 劫持 —— 该文件的全名是 <包>.typing，顶层名 typing 只可能是标准库"
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
    assert module_id("typing.py") in targets, f"相对导入不得被标准库规则抢走，实际: {targets}"


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
    assert module_id("helpers.py") in targets, f"非标准库的绝对导入应保持原有解析结果，实际: {targets}"


def test_stdlib_wins_when_importer_sits_in_a_subpackage():
    """仓库根不是包、但导入方在包内：``pkg/typing.py`` 不是顶层 ``typing``。

    这是 src-layout / monorepo 的常见形态（``--src .`` 而代码在 ``pkg/`` 里）。
    旧判据只看"根是不是包"，于是漏判，随后落进"同目录兜底"连到 ``pkg/typing.py``。
    """
    files = [
        _fm("pkg/__init__.py"),
        _fm("pkg/a.py", imports=[("typing", None, 1)]),
        _fm("pkg/typing.py"),
    ]
    result = build_graph(files)

    targets = _import_targets(result, "pkg/a.py")
    assert external_id("typing") in targets, f"应落 External(typing)，实际边: {targets}"
    assert module_id("pkg/typing.py") not in targets, "包内同名文件的全名是 pkg.typing，不是顶层 typing"


def test_root_level_same_named_file_still_wins_when_importer_is_in_a_package():
    """反方向的守卫：根不是包时，根下的同名文件就在 ``sys.path[0]`` 上。

    它**不是**包内文件 —— ``pkg/a.py: import typing`` 真的会加载 ``<根>/typing.py``。
    把"导入方在包内"当成"一律判标准库"会丢掉这条真实依赖，还会报一条假诊断。
    """
    files = [
        _fm("typing.py"),
        _fm("pkg/__init__.py"),
        _fm("pkg/a.py", imports=[("typing", None, 1)]),
    ]
    result = build_graph(files)

    targets = _import_targets(result, "pkg/a.py")
    assert module_id("typing.py") in targets, f"根下同名文件是顶层模块，应连本地，实际: {targets}"
    assert external_id("typing") not in targets
    assert not [d for d in result.diagnostics if d.code == "IMPORT_SHADOWED"], (
        "并没有文件被忽略，不该报 IMPORT_SHADOWED"
    )


def test_namespace_subpackage_does_not_shadow_stdlib():
    """祖先目录是包、自身目录没有 ``__init__.py``（PEP 420 命名空间子包）同样在包内。"""
    files = [
        _fm("pkg/__init__.py"),
        _fm("pkg/sub/a.py", imports=[("typing", None, 1)]),
        _fm("pkg/sub/typing.py"),
    ]
    result = build_graph(files)

    targets = _import_targets(result, "pkg/sub/a.py")
    assert external_id("typing") in targets, f"应落 External(typing)，实际: {targets}"
    assert module_id("pkg/sub/typing.py") not in targets


def test_root_package_subdir_without_init_does_not_shadow_stdlib():
    """根是包、子目录没有 ``__init__.py``：``sub/typing.py`` 的全名仍是 ``<包>.sub.typing``。"""
    files = [
        _fm("__init__.py"),
        _fm("sub/a.py", imports=[("typing", None, 1)]),
        _fm("sub/typing.py"),
    ]
    result = build_graph(files)

    targets = _import_targets(result, "sub/a.py")
    assert external_id("typing") in targets, f"应落 External(typing)，实际: {targets}"
    assert module_id("sub/typing.py") not in targets


def test_script_dir_without_init_keeps_local_stdlib_twin():
    """脚本目录（整条目录链都没有 ``__init__.py``）的同名文件是**顶层模块**。

    ``python scripts/a.py`` 时 ``sys.path[0]`` 就是 ``scripts/``，``import json``
    真的加载 ``scripts/json.py``；pytest 的 rootdir 导入模式同理。PEP 420 命名空间
    包只在「作为包被导入」时成立 —— 那时 ``scripts`` 前面得有个包。旧判据只看
    ``base != ""``，把脚本目录也当成包内，于是丢掉真实依赖还报一条假 IMPORT_SHADOWED。
    """
    files = [
        _fm("scripts/a.py", imports=[("json", None, 1)]),
        _fm("scripts/json.py", symbols=[("loads", "loads", 1)]),
    ]
    result = build_graph(files)

    targets = _import_targets(result, "scripts/a.py")
    assert module_id("scripts/json.py") in targets, f"应连本地同名文件，实际: {targets}"
    assert external_id("json") not in targets, "不该把脚本目录里的顶层模块判成标准库"
    assert not [d for d in result.diagnostics if d.code == "IMPORT_SHADOWED"], (
        "没有文件被忽略，不该报 IMPORT_SHADOWED"
    )
    info = [d for d in result.diagnostics if d.code == "STDLIB_NAME_IN_SCRIPT_DIR"]
    assert info and info[0].level == "info", "要留一条 info：包导入模式下它会被标准库遮蔽"


def test_ancestor_init_still_makes_namespace_subdir_a_package():
    """守卫：`pkg/__init__.py` 存在时，`pkg/sub/` 即使无 init 也仍在包内。

    回归修复不能把 PEP 420 命名空间子包一起放开 —— 那会让 pydantic 那批
    ``import typing`` 重新连回包内同名文件。
    """
    files = [
        _fm("pkg/__init__.py"),
        _fm("pkg/sub/a.py", imports=[("typing", None, 1)]),
        _fm("pkg/sub/typing.py"),
    ]
    result = build_graph(files)

    targets = _import_targets(result, "pkg/sub/a.py")
    assert external_id("typing") in targets, f"仍是包内同名文件，应落标准库，实际: {targets}"
    assert not [d for d in result.diagnostics if d.code == "STDLIB_NAME_IN_SCRIPT_DIR"]


__all__: list[str] = []
