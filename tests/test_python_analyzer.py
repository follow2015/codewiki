"""Python AST 后端判据（§4.1）。

钉四件事：
1. 符号（类/方法/函数/常量）与归属（方法挂到类上，qualname 为 ``Class.method``）；
2. 调用事实只记**语法名字**，不在此处跨文件消歧；
3. 继承事实记录基类名（消歧在引擎层）；
4. ``__all__`` 只影响公开面，不影响对未列入名字的解析（§2.4）。
"""
from __future__ import annotations

from pathlib import Path

from codewiki.analyzers.python_analyzer import PythonAstAnalyzer


def test_symbols_and_ownership():
    content = (Path(__file__).parent / "fixtures" / "mini_py" / "pkg" / "core.py").read_text(
        encoding="utf-8"
    )
    model = PythonAstAnalyzer().parse(Path("pkg/core.py"), content)

    names = {s.qualname: s for s in model.symbols}
    assert "Service" in names and "Base" in names
    assert "Service.run" in names, "方法应归属到类（qualname = Class.method）"
    assert names["Service.run"].kind == "method"
    assert names["helper"].kind == "function"
    assert names["MAX_RETRY"].kind == "constant"
    assert names["Service"].docstring == "业务服务。"
    assert names["helper"].docstring == "被内部调用的工具函数。"


def test_signature_and_docstring_are_captured():
    content = "def f(a, b=1, *args, **kw) -> int:\n    'doc'\n    return 1\n"
    model = PythonAstAnalyzer().parse(Path("m.py"), content)

    symbol = next(s for s in model.symbols if s.name == "f")
    assert symbol.signature == "(a, b=1, *args, **kw) -> int"
    assert symbol.docstring == "doc"


def test_calls_keep_syntactic_names_only():
    content = "from pkg import Svc\n\ndef main() -> int:\n    return Svc().run()\n"
    model = PythonAstAnalyzer().parse(Path("app.py"), content)

    callees = {(c.callee, c.receiver) for c in model.calls}
    assert ("run", "Svc()") in callees, "属性调用应保留接收者表达式"
    assert all(c.scope is None or isinstance(c.scope, str) for c in model.calls)


def test_inheritance_records_base_name():
    content = "class A: pass\n\nclass B(A): pass\n"
    model = PythonAstAnalyzer().parse(Path("m.py"), content)

    assert [(i.child, i.base) for i in model.inherits] == [("B", "A")]


def test_all_defines_public_surface_only():
    content = "__all__ = ['Public']\n\nclass Public: pass\n\nclass Hidden: pass\n"
    model = PythonAstAnalyzer().parse(Path("pkg/__init__.py"), content)

    assert model.exports == ["Public"]
    assert {s.name for s in model.symbols} == {"Public", "Hidden"}, "未列入 __all__ 也要建符号"


def test_syntax_error_yields_a_bare_file_model():
    model = PythonAstAnalyzer().parse(Path("broken.py"), "def (:\n")

    assert model.path == "broken.py"
    assert model.hash and not model.symbols, "解析失败：只留文件事实，诊断交给引擎层"


def test_content_hash_is_stable():
    content = "x = 1\n"
    a = PythonAstAnalyzer().parse(Path("m.py"), content)
    b = PythonAstAnalyzer().parse(Path("m.py"), content)

    assert a.hash == b.hash and len(a.hash) == 16


def test_relative_import_level_encoded_as_leading_dots():
    """``from ..services.user import User`` 的 level=2 必须保留（编码为前导点），
    否则引擎层会把它当绝对模块名解析，误绑同名路径或落 External。"""
    model = PythonAstAnalyzer().parse(
        Path("app/pkg/a.py"), "from ..services.user import User\n"
    )
    assert model.imports[0].module == "..services.user"


def test_single_level_relative_import_keeps_one_dot():
    """``from . import x`` → module='.'（与既有行为一致）；``from .x import y`` → '.x'。"""
    analyzer = PythonAstAnalyzer()
    assert analyzer.parse(Path("m/a.py"), "from . import x\n").imports[0].module == "."
    assert analyzer.parse(Path("m/a.py"), "from .x import y\n").imports[0].module == ".x"


def test_nested_class_keeps_full_qualname():
    """嵌套类的符号真身是 `Outer.Inner`，不是 `Inner`。

    用父的 name 而不是 qualname 拼接时，`class Outer: class Inner(Root): def m`
    会产出 `Inner.m` ⇒ CONTAINS/INHERITS 指向不存在的符号，被当悬空边丢掉
    （层级与继承在图上凭空消失）。
    """
    source = (
        "class Root:\n"
        "    pass\n\n"
        "class Outer:\n"
        "    class Inner(Root):\n"
        "        def m(self):\n"
        "            return 1\n"
    )
    model = PythonAstAnalyzer().parse(Path("nested.py"), source)

    qualnames = {s.qualname for s in model.symbols}
    assert "Outer.Inner" in qualnames and "Outer.Inner.m" in qualnames, qualnames
    assert [(i.child, i.base) for i in model.inherits] == [("Outer.Inner", "Root")]


def test_nested_class_edges_survive_graph_build():
    from codewiki.graph import build_graph
    from codewiki.models import EdgeType

    source = (
        "class Root:\n"
        "    pass\n\n"
        "class Outer:\n"
        "    class Inner(Root):\n"
        "        def m(self):\n"
        "            return 1\n"
    )
    result = build_graph([PythonAstAnalyzer().parse(Path("nested.py"), source)])

    inherits = [
        (e.source, e.target) for e in result.edges if e.type == EdgeType.INHERITS
    ]
    assert ("sym::nested.py::Outer.Inner", "sym::nested.py::Root") in inherits
    assert not [d for d in result.diagnostics if d.code == "DANGLING_EDGE_DROPPED"]
