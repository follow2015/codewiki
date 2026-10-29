"""TS/JS 模块解析判据（前端 import 的四种形态）。

前端 specifier 与 Python 完全不是一套语法，这也是它此前**整片解析不出来**的根因：

1. **相对**是 ``./x`` / ``../x``（斜杠），不是 ``.x`` / ``..x``（点）；
2. **别名** ``@/theme/dark`` 由 tsconfig ``paths`` 决定，既不是包名也不是相对路径；
3. **省略扩展名**：``./Widget`` → ``Widget.tsx``，``@/components`` → ``components/index.ts``；
4. **裸包**（``react`` / ``@tanstack/react-query``）必须留在 External，绝不能被别名规则吞掉。

实测背景（ipip arch-e0）：前端 610 个模块、3411 条 import 全部落 ``ext::``，
模块间 IMPORTS 边 **0 条** —— 每加一条这里的判据，就修回一批真实失真的边。
"""

from __future__ import annotations

from codewiki.graph import build_graph
from codewiki.graph.builder import external_id, module_id
from codewiki.models import EdgeType, FileModel, ImportFact

ALIASES = {"@/*": "frontend-new/src/*"}

HOME = "frontend-new/src/pages/Home.tsx"


def _file(path: str, imports: list[ImportFact] | None = None) -> FileModel:
    return FileModel(path=path, language="typescript", hash="0" * 16, imports=imports or [])


def _import_from(module: str, name: str | None = None) -> ImportFact:
    return ImportFact(module=module, name=name, lineno=1, kind="from" if name else "import")


def _import_targets(result, source: str) -> set[str]:
    return {e.target for e in result.edges if e.type == EdgeType.IMPORTS and e.source == module_id(source)}


def test_relative_import_finds_tsx_without_extension():
    """``import {Widget} from './Widget'`` 要连到 ``Widget.tsx``（扩展名省略 + tsx）。"""
    files = [
        _file(HOME, [_import_from("./Widget", "Widget")]),
        _file("frontend-new/src/pages/Widget.tsx"),
    ]
    targets = _import_targets(build_graph(files), HOME)
    assert module_id("frontend-new/src/pages/Widget.tsx") in targets


def test_parent_relative_import_is_normalized():
    """``../theme/dark`` 要折算掉 ``..``，不能拼出 ``pages/../theme`` 这种假路径。"""
    files = [
        _file(HOME, [_import_from("../theme/dark", "dark")]),
        _file("frontend-new/src/theme/dark.ts"),
    ]
    targets = _import_targets(build_graph(files), HOME)
    assert module_id("frontend-new/src/theme/dark.ts") in targets


def test_alias_import_resolves_through_configured_aliases():
    """``@/*`` 由 ``[analysis] ts_aliases`` 映射进仓库，未配置时不许猜。"""
    files = [
        _file(HOME, [_import_from("@/theme/dark", "dark")]),
        _file("frontend-new/src/theme/dark.ts"),
    ]
    targets = _import_targets(build_graph(files, ts_aliases=ALIASES), HOME)
    assert module_id("frontend-new/src/theme/dark.ts") in targets


def test_alias_directory_import_falls_back_to_index_file():
    """``@/components`` 指的是目录，要落到 ``components/index.ts``。"""
    files = [
        _file(HOME, [_import_from("@/components", "Button")]),
        _file("frontend-new/src/components/index.ts"),
    ]
    targets = _import_targets(build_graph(files, ts_aliases=ALIASES), HOME)
    assert module_id("frontend-new/src/components/index.ts") in targets


def test_scoped_package_is_not_mistaken_for_an_alias():
    """``@tanstack/react-query`` 是 scoped 包：配了 ``@/*`` 也不能被吞成内部模块。"""
    files = [
        _file(HOME, [_import_from("@tanstack/react-query", "useQuery")]),
    ]
    targets = _import_targets(build_graph(files, ts_aliases=ALIASES), HOME)
    assert targets == {external_id("@tanstack/react-query")}


def test_exact_alias_does_not_prefix_match_longer_specifier():
    """精确别名 ``@config`` 不该命中 ``@configX``（前缀匹配只属于带 ``*`` 的规则）。"""
    files = [
        _file(HOME, [_import_from("@config", "cfg")]),
        _file("frontend-new/src/config/app.ts"),
    ]
    result = build_graph(files, ts_aliases={"@config": "frontend-new/src/config/app.ts"})
    targets = _import_targets(result, HOME)
    assert targets == {module_id("frontend-new/src/config/app.ts")}

    other = build_graph(
        [
            _file(HOME, [_import_from("@configX", "x")]),
            _file("frontend-new/src/config/app.ts"),  # 目标真实存在，否则"误连"根本不会发生
        ],
        ts_aliases={"@config": "frontend-new/src/config/app.ts"},
    )
    assert _import_targets(other, HOME) == {external_id("@configX")}


def test_named_import_does_not_become_a_dotted_file_or_a_submodule():
    """``import {Foo} from './x'`` 依赖的是 ``x.ts``。

    TS 的文件名允许带点（``x.Foo.ts``），所以"按点拼子模块"在 TS 上有两种误连：
    ``x.Foo.ts``（点号文件）与 ``x/Foo.ts``（子目录）。两者都不能连。
    """
    files = [
        _file(HOME, [_import_from("./x", "Foo")]),
        _file("frontend-new/src/pages/x.ts"),
        _file("frontend-new/src/pages/x.Foo.ts"),
        _file("frontend-new/src/pages/x/Foo.ts"),
    ]
    targets = _import_targets(build_graph(files), HOME)
    assert targets == {module_id("frontend-new/src/pages/x.ts")}


def test_extension_preference_prefers_ts_over_dts():
    """同名 ``types.ts`` 与 ``types.d.ts`` 并存时，运行时依赖是 ``types.ts``。"""
    files = [
        _file(HOME, [_import_from("./types", "T")]),
        _file("frontend-new/src/pages/types.ts"),
        _file("frontend-new/src/pages/types.d.ts"),
    ]
    targets = _import_targets(build_graph(files), HOME)
    assert module_id("frontend-new/src/pages/types.ts") in targets
    assert module_id("frontend-new/src/pages/types.d.ts") not in targets


def test_alias_without_config_stays_external_not_a_guess():
    """没配 ``ts_aliases`` 就不解析 —— 宁可不连，也不猜（§0.3 非目标 3）。"""
    files = [_file(HOME, [_import_from("@/theme/dark", "dark")])]
    targets = _import_targets(build_graph(files), HOME)
    assert targets == {external_id("@/theme/dark")}


def test_unresolved_relative_import_keeps_a_usable_external_name():
    """解析不到也要留可辨认的名字：按点切会把 ``./missing.js`` 截成空串 ``ext::``。"""
    files = [_file(HOME, [_import_from("./missing.js", "x")])]
    targets = _import_targets(build_graph(files), HOME)
    assert targets == {external_id("./missing.js")}
