"""图谱引擎判据（§2.4）—— 静态分析最易翻车的三处。

1. **re-export 消歧**：``from pkg import Svc`` 必须解析到 ``pkg/core.py::Service``，
   而不是停在 ``pkg/__init__.py``（这是"符号错连"的典型来源）；
2. **调用解析**：``Service.run`` → ``helper`` 落在同一模块的真实定义上；
3. **不猜测**：解析不到就落 ``External``，绝不随机绑定（§0.3 非目标 3）。
"""

from __future__ import annotations

from pathlib import Path

from codewiki.graph import build_graph
from codewiki.graph.builder import external_id, module_id, symbol_id
from codewiki.models import EdgeType
from tests.conftest import parse_all


def test_reexport_is_resolved_to_the_real_definition(mini_py):
    result = build_graph(parse_all(mini_py))

    real = symbol_id("pkg/core.py", "Service")
    assert real in {n.id for n in result.nodes}
    imports = {
        (e.source, e.target)
        for e in result.edges
        if e.type == EdgeType.IMPORTS and e.source == module_id("app.py")
    }
    assert (module_id("app.py"), module_id("pkg/core.py")) in imports, (
        "re-export 未穿透：边指向了 pkg/__init__.py 而不是 pkg/core.py"
    )


def test_call_is_bound_to_the_real_symbol(mini_py):
    result = build_graph(parse_all(mini_py))

    calls = {(e.source, e.target) for e in result.edges if e.type == EdgeType.CALLS}
    assert (symbol_id("pkg/core.py", "Service.run"), symbol_id("pkg/core.py", "helper")) in calls


def test_contains_edges_link_modules_and_classes(mini_py):
    result = build_graph(parse_all(mini_py))

    contains = {(e.source, e.target) for e in result.edges if e.type == EdgeType.CONTAINS}
    assert (module_id("pkg/core.py"), symbol_id("pkg/core.py", "Service")) in contains
    assert (symbol_id("pkg/core.py", "Service"), symbol_id("pkg/core.py", "Service.run")) in contains


def test_inheritance_is_resolved(mini_py):
    result = build_graph(parse_all(mini_py))

    inherits = {(e.source, e.target) for e in result.edges if e.type == EdgeType.INHERITS}
    assert (symbol_id("pkg/core.py", "Service"), symbol_id("pkg/core.py", "Base")) in inherits


def test_unknown_module_becomes_external_not_a_random_module(mini_py):
    """解析不到 → External，绝不随机绑定（§2.4 第 1 条）。"""
    from codewiki.models import FileModel, ImportFact

    models = parse_all(mini_py)
    models.append(
        FileModel(
            path="orphan.py",
            language="python",
            hash="0" * 16,
            imports=[ImportFact(module="totally_unknown_pkg", lineno=1)],
        )
    )
    result = build_graph(models)

    targets = {
        e.target for e in result.edges if e.type == EdgeType.IMPORTS and e.source == module_id("orphan.py")
    }
    assert targets == {external_id("totally_unknown_pkg")}
    assert external_id("totally_unknown_pkg") in {n.id for n in result.nodes}


def test_unlayered_modules_are_reported(mini_py):
    """未命中分层 → 虚拟层 + 诊断（§2.1）。"""
    result = build_graph(parse_all(mini_py))

    codes = {d.code for d in result.diagnostics}
    assert "UNLAYERED_MODULE" in codes


def test_layer_cycle_is_an_error_level_diagnostic():
    files = [
        _model("a/x.py", modules=["b.y"]),
        _model("b/y.py", modules=["a.x"]),
    ]
    layers = [
        {"id": "layer::A", "name": "A", "paths": ["a/"]},
        {"id": "layer::B", "name": "B", "paths": ["b/"]},
    ]
    result = build_graph(files, layers=layers)

    cycles = [d for d in result.diagnostics if d.code == "LAYER_CYCLE"]
    assert cycles and cycles[0].level == "error", "分层成环 = 分层失效，必须是 error"


def test_stdlib_is_classified_as_stdlib():
    from codewiki.models import FileModel, ImportFact

    files = [
        FileModel(
            path="m.py", language="python", hash="0" * 16, imports=[ImportFact(module="json", lineno=1)]
        )
    ]
    result = build_graph(files)

    node = next(n for n in result.nodes if n.id == external_id("json"))
    assert node.kind == "stdlib"


def _model(path: str, *, modules: list[str] | None = None):
    """构造一个最小 FileModel（只带 ``import <module>`` 事实）。"""
    from codewiki.models import FileModel, ImportFact

    return FileModel(
        path=path,
        language="python",
        hash="0" * 16,
        imports=[ImportFact(module=m, lineno=1, kind="import") for m in (modules or [])],
    )


def test_dynamic_import_becomes_low_confidence_edge():
    """`importlib.import_module("x")` 是确定的字面目标 ⇒ 收，但必须低置信。"""
    from codewiki.analyzers.python_analyzer import PythonAstAnalyzer
    from codewiki.models import CONF_DYNAMIC, EdgeType

    source = 'import importlib\n\n\ndef load():\n    return importlib.import_module("pkg.core")\n'
    model = PythonAstAnalyzer().parse(Path("loader.py"), source)

    assert [d.target for d in model.dynamic] == ["pkg.core"], model.dynamic

    result = build_graph(
        [
            model,
            _model("pkg/core.py"),
            _model("pkg/__init__.py"),
        ]
    )
    dynamic = [e for e in result.edges if e.type == EdgeType.USES_DYNAMIC]
    assert dynamic, "动态引用应产出边（否则边类型定义了却无人产出）"
    assert dynamic[0].confidence == CONF_DYNAMIC and dynamic[0].static is False
    assert dynamic[0].target == "mod::pkg/core.py"


def test_dynamic_target_outside_repo_is_external_not_guessed():
    """解析不到的动态目标落 External，**不猜**成同名模块。"""
    from codewiki.analyzers.python_analyzer import PythonAstAnalyzer
    from codewiki.models import EdgeType

    source = "mod = __import__('some_third_party')\n"
    model = PythonAstAnalyzer().parse(Path("loader.py"), source)
    result = build_graph([model])

    dynamic = [e for e in result.edges if e.type == EdgeType.USES_DYNAMIC]
    assert dynamic and dynamic[0].target.startswith("ext::"), dynamic


def test_unknowable_dynamic_access_is_not_recorded():
    """`getattr(obj, name)` 目标不可知 ⇒ 宁缺毋滥，不收。"""
    from codewiki.analyzers.python_analyzer import PythonAstAnalyzer

    source = "def f(obj, name):\n    return getattr(obj, name)\n"
    model = PythonAstAnalyzer().parse(Path("f.py"), source)
    assert model.dynamic == []


def test_absolute_import_with_extra_package_prefix_is_resolved(tmp_path):
    """``--src app`` 下 ``from app.services.x import Y`` 必须解析到本地文件。

    IPIP 实测：不剥离包根前缀时 5934 条 IMPORTS 里只有 130 条解析到仓库内模块
    （97.8% 落 ``mod::ref::*``），层间依赖因此为 0，架构图各层并列互不相连。
    只在首段**恰等于分析根目录名**时剥离，避免 ``import os.path`` 误判成
    ``path.py``。
    """
    root = tmp_path / "app"
    (root / "services").mkdir(parents=True)
    (root / "adapters").mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "services" / "__init__.py").write_text("", encoding="utf-8")
    (root / "services" / "svc.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "adapters" / "__init__.py").write_text("", encoding="utf-8")
    (root / "adapters" / "a.py").write_text("from app.services.svc import VALUE\n", encoding="utf-8")

    models = parse_all(root)
    with_root = build_graph(models, root_package="app")
    without = build_graph(models)
    target = module_id("services/svc.py")
    src = module_id("adapters/a.py")

    assert (src, target) in {(e.source, e.target) for e in with_root.edges if e.type == EdgeType.IMPORTS}
    assert (src, target) not in {(e.source, e.target) for e in without.edges if e.type == EdgeType.IMPORTS}, (
        "不传 root_package 时不该凭空剥离 —— 剥离只在首段确为分析根包名时发生"
    )


def test_root_package_strip_does_not_hijack_stdlib(tmp_path):
    """``import os.path`` 不能被剥离成 ``path.py``（误连本地同名文件）。"""
    root = tmp_path / "os"
    root.mkdir()
    (root / "app.py").write_text("import os.path\nimport os\n", encoding="utf-8")
    (root / "path.py").write_text("X = 1\n", encoding="utf-8")

    result = build_graph(parse_all(root), root_package="os")
    imports = {(e.source, e.target) for e in result.edges if e.type == EdgeType.IMPORTS}

    assert (module_id("app.py"), module_id("path.py")) not in imports, (
        "stdlib 名恰好等于根包名时仍按 stdlib 处理，不剥离"
    )


def test_multi_layer_without_any_layer_dependency_warns(tmp_path):
    """配了多层却零层间边 ⇒ 必须出声（图看着完整，实则依赖全丢）。"""
    root = tmp_path / "proj"
    (root / "adapters").mkdir(parents=True)
    (root / "core").mkdir()
    (root / "adapters" / "a.py").write_text("from app.core.s import S\n", encoding="utf-8")
    (root / "core" / "s.py").write_text("S = 1\n", encoding="utf-8")

    layers = [
        {"id": "layer::adapters", "name": "适配层", "paths": ["adapters"]},
        {"id": "layer::core", "name": "核心层", "paths": ["core"]},
    ]
    result = build_graph(parse_all(root), layers=layers, root_package="proj")

    codes = {d.code for d in result.diagnostics}
    assert "NO_LAYER_DEPENDENCY" in codes, result.diagnostics


def test_layer_dependency_present_does_not_warn(tmp_path):
    root = tmp_path / "app"
    (root / "adapters").mkdir(parents=True)
    (root / "core").mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "adapters" / "__init__.py").write_text("", encoding="utf-8")
    (root / "core" / "__init__.py").write_text("", encoding="utf-8")
    (root / "adapters" / "a.py").write_text("from app.core.s import S\n", encoding="utf-8")
    (root / "core" / "s.py").write_text("S = 1\n", encoding="utf-8")

    layers = [
        {"id": "layer::adapters", "name": "适配层", "paths": ["adapters"]},
        {"id": "layer::core", "name": "核心层", "paths": ["core"]},
    ]
    result = build_graph(parse_all(root), layers=layers, root_package="app")

    assert "NO_LAYER_DEPENDENCY" not in {d.code for d in result.diagnostics}, result.diagnostics
    assert any(e.type == EdgeType.LAYER_DEPENDS for e in result.edges)


def test_unlayered_layer_is_excluded_from_layer_cycle(tmp_path):
    """``_unlayered`` 是兜底桶、不是配置的层，不该参与层级环判定。

    它天然与每个真实层双向依赖（未分层模块被 A 层 import、又 import 回 A），
    照算会把一次"部分分层"的配置刷成一串 LAYER_CYCLE error。
    """
    root = tmp_path / "app"
    (root / "layered").mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "layered" / "__init__.py").write_text("", encoding="utf-8")
    (root / "loose.py").write_text("from app.layered.m import M\n", encoding="utf-8")
    (root / "layered" / "m.py").write_text("import app.loose\n\nM = 1\n", encoding="utf-8")

    layers = [{"id": "layer::L", "name": "分层", "paths": ["layered"]}]
    result = build_graph(parse_all(root), layers=layers, root_package="app")

    codes = [d.code for d in result.diagnostics]
    assert "LAYER_CYCLE" not in codes, result.diagnostics


def _fm(path, *, imports=(), symbols=(), calls=()):
    from codewiki.models import CallFact, FileModel, ImportFact, SymbolFact

    return FileModel(
        path=path,
        language="python",
        hash=path,
        imports=[ImportFact(module=m, name=n, lineno=ln, kind="from") for m, n, ln in imports],
        symbols=[
            SymbolFact(name=n, qualname=q, kind="function", lineno=ln, endlineno=ln + 1)
            for n, q, ln in symbols
        ],
        calls=[CallFact(callee=c, receiver=r, lineno=ln) for c, r, ln in calls],
    )


def test_unresolved_reexport_import_falls_to_external_not_dangling_ref():
    """H4：from-import 的 re-export 解析失败时，边必须落 External，不得指向
    不存在的 ``mod::ref::*`` 节点（悬空边违反引用完整性）。"""
    from codewiki.graph.builder import external_id

    fm = _fm(
        "pkg/a.py",
        imports=[("pkg.missing", "Y", 1)],
        symbols=[("f", "f", 2)],
    )
    result = build_graph([fm])

    ids = {n.id for n in result.nodes}
    dangling = [e for e in result.edges if e.source not in ids or e.target not in ids]
    assert dangling == [], f"悬空边: {[(e.source, e.type, e.target) for e in dangling]}"
    assert not any(str(e.target).startswith("mod::ref::") for e in result.edges)
    assert any(n.id == external_id("pkg") for n in result.nodes), "应退化为 External"


def test_call_via_unresolved_from_import_does_not_dangle():
    """H4 补充：CALLS 目标来自未解析的 re-export 别名（ref::）时不得成边。"""
    fm = _fm(
        "m/a.py",
        imports=[("pkg.missing", "Y", 1)],
        symbols=[("f", "f", 2)],
        calls=[("Y", "", 3)],
    )
    result = build_graph([fm])
    ids = {n.id for n in result.nodes}
    dangling = [e for e in result.edges if e.source not in ids or e.target not in ids]
    assert dangling == []
    assert not any("ref::" in str(e.target) for e in result.edges)


def test_guess_symbol_never_binds_ambiguous_name():
    """H3：两个同名候选时绝不随机绑定（README 红线），必须零边 + 歧义诊断。"""
    fa = _fm("m/a.py", symbols=[("run", "run", 1)])
    fb = _fm("m/b.py", symbols=[("run", "run", 1)])
    fc = _fm("m/c.py", symbols=[("g", "g", 1)], calls=[("run", "obj", 2)])
    result = build_graph([fa, fb, fc])

    calls = [e for e in result.edges if e.type == EdgeType.CALLS]
    assert calls == [], f"同名多候选被随机绑定: {[(e.source, e.target) for e in calls]}"
    assert any(d.code == "CALL_AMBIGUOUS" for d in result.diagnostics), result.diagnostics


def test_guess_symbol_binds_unique_candidate_still():
    """H3 正例：唯一候选时同名猜测照常工作（窄而不死）。"""
    from codewiki.models import CONF_NAME_GUESS

    fa = _fm("m/a.py", symbols=[("run", "run", 1)])
    fc = _fm("m/c.py", symbols=[("g", "g", 1)], calls=[("run", "obj", 2)])
    result = build_graph([fa, fc])

    calls = [e for e in result.edges if e.type == EdgeType.CALLS]
    assert len(calls) == 1 and calls[0].confidence == CONF_NAME_GUESS


def test_no_edge_endpoint_is_ever_absent():
    """引用完整性兜底：任何路径下产出的边端点都必须存在于节点表。"""
    files = [
        _fm(
            "m/a.py",
            imports=[("no.such.mod", "X", 1)],
            symbols=[("run", "run", 2)],
            calls=[("run", "o", 3), ("X", "o", 4)],
        ),
        _fm(
            "m/b.py",
            imports=[("no.such.mod", "run", 1)],
            symbols=[("run", "run", 2)],
            calls=[("run", "o", 3)],
        ),
    ]
    result = build_graph(files)
    ids = {n.id for n in result.nodes}
    dangling = [e for e in result.edges if e.source not in ids or e.target not in ids]
    assert dangling == []


def test_relative_from_import_resolves_across_levels():
    """M2：二级相对导入按调用方包路径解析（``from ..services.user import User``）。"""
    files = [
        _fm("app/pkg/a.py", imports=[("..services.user", "User", 1)], symbols=[("f", "f", 2)]),
        _fm("app/services/user.py", symbols=[("User", "User", 1)]),
    ]
    result = build_graph(files)
    imports = {(e.source, e.target) for e in result.edges if e.type == EdgeType.IMPORTS}
    assert (module_id("app/pkg/a.py"), module_id("app/services/user.py")) in imports, (
        f"二级相对导入未解析: {imports}"
    )


def test_single_level_relative_import_resolves_to_sibling_package():
    """M2：一级相对导入（``from .sib import S``）解析到同包兄弟模块。"""
    files = [
        _fm("pkg/a.py", imports=[(".sib", "S", 1)], symbols=[("f", "f", 2)]),
        _fm("pkg/sib.py", symbols=[("S", "S", 1)]),
    ]
    result = build_graph(files)
    imports = {(e.source, e.target) for e in result.edges if e.type == EdgeType.IMPORTS}
    assert (module_id("pkg/a.py"), module_id("pkg/sib.py")) in imports, imports


def test_duplicate_call_edges_are_merged_with_weight():
    """低危收尾：同一 (source,target,type) 的重复边合并为一条、weight 累加，
    而不是靠 append 堆量（IPIP 实测 4 万条边里大量重复）。"""
    fm = _fm(
        "m/a.py", symbols=[("run", "run", 1), ("g", "g", 5)], calls=[("run", "obj", 6), ("run", "obj", 6)]
    )
    result = build_graph([fm])
    calls = [e for e in result.edges if e.type == EdgeType.CALLS and e.target.endswith("::run")]
    assert len(calls) == 1, calls
    assert calls[0].weight == 2


def test_layer_cycle_enumeration_is_bounded():
    """层图两两互连时 `simple_cycles` 是组合级的。

    实测：10 层互连 ⇒ 111 万条环，光构造诊断就让整轮分析几分钟不出结果
    （8 层 1.6 万条、9 层 12.6 万条、10 层 111 万条）。诊断要的是"哪几层互相
    绕"，不是把所有绕法列全 ⇒ 按强连通分量限量，并给出截断提示。
    """
    import time

    from codewiki.graph.builder import MAX_CYCLES_PER_COMPONENT, GraphBuilder
    from codewiki.models import Edge, Node, NodeType

    n = 10
    builder = GraphBuilder()
    builder.nodes = {
        f"layer::L{i}": Node(id=f"layer::L{i}", type=NodeType.LAYER, name=f"L{i}") for i in range(n)
    }
    builder.edges = [
        Edge(source=f"layer::L{i}", target=f"layer::L{j}", type=EdgeType.LAYER_DEPENDS)
        for i in range(n)
        for j in range(n)
        if i != j
    ]

    started = time.monotonic()
    builder._detect_cycles()
    assert time.monotonic() - started < 2.0, "分层环检测不该把分析拖死"

    codes = [d.code for d in builder.diagnostics]
    assert codes.count("LAYER_CYCLE") <= MAX_CYCLES_PER_COMPONENT
    assert "LAYER_CYCLE_TRUNCATED" in codes, "环多到截断时要说清楚，不能假装只有这几条"
