"""TYPE_CHECKING-only import 与包根名推断（round 12）。

* ``if TYPE_CHECKING:`` 下的 import 此前**根本没被读进来**（分析器只扫模块顶层），
  所以不会造成假环，而是丢了依赖边。现在读进来并标 ``type_only``：边保留（变更影响
  要用），但不参与环检测。
* 分析根目录名与包名不一致时（``--src src`` + ``from app.x import Y``），按 import 统计
  推断包根，而不是整仓绝对导入静默落成 external。
"""

from __future__ import annotations

from pathlib import Path

from codewiki.analyzers.python_analyzer import PythonAstAnalyzer
from codewiki.graph import build_graph
from codewiki.graph.analysis import find_module_cycles, impact_of
from codewiki.graph.builder import module_id
from codewiki.models import EdgeType, FileModel, ImportFact
from codewiki.render.jsonio import packet_from_parts
from codewiki.render.mermaid_context import render_module_context
from codewiki.render.mermaid_macro import render_system_context
from codewiki.schemas import LayerNode


def _model(path: str, src: str) -> FileModel:
    return PythonAstAnalyzer().parse(Path(path), src)


def _edge(result, a: str, b: str):
    return next(
        (
            e
            for e in result.edges
            if e.type == EdgeType.IMPORTS and e.source == module_id(a) and e.target == module_id(b)
        ),
        None,
    )


def test_type_checking_imports_are_collected_and_flagged():
    model = _model(
        "a.py",
        "from __future__ import annotations\n"
        "import typing\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from b import B\n"
        "    import c\n"
        "else:\n"
        "    import d\n"
        "if typing.TYPE_CHECKING:\n"
        "    from e import E\n"
        "def f():\n"
        "    import lazy\n",
    )
    flags = {(i.module, i.type_only) for i in model.imports}
    assert ("b", True) in flags and ("c", True) in flags and ("e", True) in flags
    assert ("typing", False) in flags
    assert not any(i.module == "d" and i.type_only for i in model.imports)
    assert all(i.module != "lazy" for i in model.imports)


def test_type_only_edges_do_not_form_cycles_but_runtime_ones_do():
    a = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from b import B\n"
    b = "from a import A\n"
    result = build_graph([_model("a.py", a), _model("b.py", b)])

    edge = _edge(result, "a.py", "b.py")
    assert edge is not None and edge.type_only, "type-only 依赖要保留（变更影响分析需要）"
    assert _edge(result, "b.py", "a.py").type_only is False

    assert not any(d.code == "MODULE_CYCLE" for d in result.diagnostics)

    runtime = build_graph([_model("a.py", "from b import B\n"), _model("b.py", b)])
    assert any(d.code == "MODULE_CYCLE" for d in runtime.diagnostics), "对照组：运行时 import 必须仍报环"


def test_runtime_import_wins_over_type_only_for_the_same_dependency():
    a = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import b\nimport b\n"
    result = build_graph([_model("a.py", a), _model("b.py", "x = 1\n")])
    assert _edge(result, "a.py", "b.py").type_only is False


def test_layer_dependency_from_type_checking_only_is_not_a_layer_cycle():
    layers = [
        {"id": "L1", "name": "L1", "paths": ["lo"]},
        {"id": "L2", "name": "L2", "paths": ["hi"]},
    ]
    files = [
        _model("hi/a.py", "from lo.b import B\n"),
        _model(
            "lo/b.py",
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from hi.a import A\n",
        ),
    ]
    result = build_graph(files, layers=layers)
    deps = [e for e in result.edges if e.type == EdgeType.LAYER_DEPENDS]
    assert {(e.source, e.target) for e in deps} == {("L2", "L1"), ("L1", "L2")}
    assert any(e.type_only for e in deps), "反向那条全部来自 TYPE_CHECKING，应标 type_only"
    assert not any(d.code == "LAYER_CYCLE" for d in result.diagnostics)

    runtime = build_graph(
        [_model("hi/a.py", "from lo.b import B\n"), _model("lo/b.py", "from hi.a import A\n")],
        layers=layers,
    )
    assert any(d.code == "LAYER_CYCLE" for d in runtime.diagnostics), "对照组：运行时反向依赖必须仍报层级环"


def _pkg_files(prefix: str = "app") -> list[FileModel]:
    return [
        _model("main.py", f"from {prefix}.core import run\nfrom {prefix}.util import helper\n"),
        _model("core.py", f"from {prefix}.util import helper\nimport os\n"),
        _model("util.py", f"from {prefix}.core import run\nimport json\n"),
        _model("svc/api.py", f"from {prefix}.core import run\n"),
    ]


def test_root_package_is_inferred_when_dir_name_differs():
    result = build_graph(_pkg_files(), root_package="src")
    assert _edge(result, "main.py", "core.py") is not None
    assert _edge(result, "svc/api.py", "core.py") is not None
    info = [d for d in result.diagnostics if d.code == "ROOT_PACKAGE_INFERRED"]
    assert len(info) == 1 and "'app'" in info[0].message


def test_inference_ignores_real_third_party_and_stdlib():
    files = [
        _model("a.py", "import requests.adapters\nfrom requests.auth import X\nfrom requests.api import Y\n"),
        _model("b.py", "import os.path\n"),
    ]
    result = build_graph(files, root_package="src")
    assert not [d for d in result.diagnostics if d.code == "ROOT_PACKAGE_INFERRED"]


def test_inference_needs_enough_evidence():
    files = [_model("a.py", "from app.core import x\n"), _model("core.py", "x = 1\n")]
    result = build_graph(files, root_package="src")
    assert not [d for d in result.diagnostics if d.code == "ROOT_PACKAGE_INFERRED"]


def test_local_top_level_package_is_never_treated_as_a_prefix():
    files = [
        _model("app/__init__.py", ""),
        _model("app/core.py", ""),
        *[_model(f"m{i}.py", "from app.core import x\n") for i in range(4)],
    ]
    result = build_graph(files, root_package="src")
    assert not [d for d in result.diagnostics if d.code == "ROOT_PACKAGE_INFERRED"]


def test_explicit_root_package_from_config_is_honored():
    result = build_graph(_pkg_files("mypkg"), root_package="mypkg")
    assert _edge(result, "main.py", "core.py") is not None
    assert not [d for d in result.diagnostics if d.code == "ROOT_PACKAGE_INFERRED"]


def test_warns_when_almost_nothing_resolves_internally():
    files = [_model(f"m{i}.py", "import numpy.linalg\nimport pandas.io\n") for i in range(30)]
    result = build_graph(files, root_package="src")
    assert any(d.code == "ROOT_PACKAGE_UNKNOWN" for d in result.diagnostics)


def test_no_unknown_warning_for_a_normal_project():
    result = build_graph(_pkg_files("src"), root_package="src")
    assert not any(d.code == "ROOT_PACKAGE_UNKNOWN" for d in result.diagnostics)


def _lang_model(path: str, language: str, module: str) -> FileModel:
    """造一条"只知道 import、不知道落点"的事实（不经分析器，避免依赖 tree-sitter）。"""
    return FileModel(
        path=path,
        language=language,
        hash="",
        imports=[ImportFact(module=module, lineno=1)],
    )


def test_go_only_repo_never_gets_the_python_src_advice():
    """Go 的 import 天生就是模块全路径、几乎全部落 external。

    此时"只有 0 条解析到仓库内文件 ⇒ 多半是 --src 选错"是**错误归因**：用户可以
    把 --src 试遍全仓也不会好。纯 Go 仓不该出这条诊断。
    """
    files = [_lang_model(f"pkg/m{i}.go", "go", "example.com/demo/internal/core") for i in range(60)]
    result = build_graph(files)
    assert not [d for d in result.diagnostics if d.code == "ROOT_PACKAGE_UNKNOWN"], result.diagnostics


def test_typescript_repo_gets_alias_advice_instead_of_root_package():
    """前端整片落 external 是别名没进配置，不是 Python 的包根问题。"""
    files = [_lang_model(f"src/m{i}.ts", "typescript", "@/core/s") for i in range(60)]
    result = build_graph(files)

    hits = [d for d in result.diagnostics if d.code == "ROOT_PACKAGE_UNKNOWN"]
    assert len(hits) == 1, result.diagnostics
    assert "ts_aliases" in hits[0].message, hits[0].message
    assert "root_package" not in hits[0].message, hits[0].message


def test_type_only_edge_is_visible_to_impact_analysis():
    """「边保留（变更影响分析要用）」必须真的看得见 —— 曾为 `if type_only: continue` 打脸。

    语义是**两个方向同时成立**：改 `b.py` 会波及只在 TYPE_CHECKING 下 import 它的
    `a.py`（类型注解跟着坏），但两者不构成运行时循环。
    """
    a = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from b import B\n"
    result = build_graph([_model("a.py", a), _model("b.py", "class B: pass\n")])
    packet = packet_from_parts(title="t", layers=[], nodes=result.nodes, edges=result.edges)

    impact = impact_of(packet, [module_id("b.py")])
    assert module_id("a.py") in impact.direct, "仅类型依赖也是依赖：改 b 会弄坏 a 的类型检查"
    assert find_module_cycles(packet) == [], "但运行时不存在，不能算循环依赖"


def test_mermaid_draws_type_only_dependency_dashed():
    """图上要能看出「这条不是运行时依赖」，否则会画出诊断里说"不存在"的环。"""
    a = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from b import B\n"
    result = build_graph([_model("a.py", a), _model("b.py", "class B: pass\n")])
    packet = packet_from_parts(
        title="t", layers=[LayerNode(id="L", name="L", order=0)], nodes=result.nodes, edges=result.edges
    )

    lines = [ln for ln in render_system_context(packet, min_confidence=0.0).splitlines() if "n" in ln]
    edge_lines = [ln for ln in lines if "-->" in ln or "-.->" in ln]
    assert edge_lines, "同层内的模块依赖应该被画出来"
    assert all("-.->" in ln for ln in edge_lines), "仅类型依赖画虚线"
    assert any("仅类型" in ln for ln in edge_lines), "虚线还要带文字标签，否则与动态边无法区分"


def test_module_context_draws_type_only_dependency_dashed():
    """模块上下文图走另一条取数路径（``GraphIndex.dependency_edges``），别只修一张图。"""
    a = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from b import B\n"
    result = build_graph([_model("a.py", a), _model("b.py", "class B: pass\n")])
    packet = packet_from_parts(title="t", layers=[], nodes=result.nodes, edges=result.edges)

    out = render_module_context(packet, module_id("b.py"), min_confidence=0.0)
    upstream = [ln for ln in out.splitlines() if " M" in ln and "U" in ln]
    assert upstream, "b.py 的上游应该被画出来"
    assert all("-.->|仅类型|" in ln for ln in upstream), "仅类型上游画虚线 + 标签"
