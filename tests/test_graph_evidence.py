"""``IMPORTS`` 边必须带 ``evidence``（``文件:行``）。

为什么单独钉：``Edge.evidence`` 字段一直在，但只有 ``CALLS`` / ``USES_DYNAMIC`` 真的传了它，
**IMPORTS 的 7 处构造点全都没传**。位置信息在单文件事实（``ImportFact.lineno``）里本来就有，
只是没写进边 —— 于是任何"报出违规在哪一行"的功能（规则、PR 注释、报告）都拿不到行号，
只能退化成模块级，而模块级定位在实践中等于没定位。

7 处构造点对应 5 种 import 形态（星号 / ``import pkg.sub`` / re-export 命中 /
``from pkg import sub`` 子模块 / 普通 ``from`` + 落 external），下面逐形态钉死。
"""

from __future__ import annotations

from pathlib import Path

from codewiki.analyzers.python_analyzer import PythonAstAnalyzer
from codewiki.graph import build_graph
from codewiki.graph.builder import external_id, module_id
from codewiki.models import EdgeType, FileModel


def _model(path: str, src: str) -> FileModel:
    return PythonAstAnalyzer().parse(Path(path), src)


def _import_edge(result, src: str, dst: str):
    """取 ``src → dst`` 的那条 IMPORTS 边（不存在即失败，避免断言落到 None 上）。"""
    hits = [
        e
        for e in result.edges
        if e.type == EdgeType.IMPORTS and e.source == module_id(src) and e.target == module_id(dst)
    ]
    assert len(hits) == 1, [e.target for e in result.edges if e.type == EdgeType.IMPORTS]
    return hits[0]


def test_from_import_records_the_line_it_appears_on():
    files = [
        _model("a.py", "import os\nfrom b import X\n"),
        _model("b.py", "class X: pass\n"),
    ]
    edge = _import_edge(build_graph(files), "a.py", "b.py")
    assert edge.evidence == "a.py:2"


def test_star_import_records_the_line():
    files = [
        _model("a.py", "from b import *\n"),
        _model("b.py", "X = 1\n"),
    ]
    edge = _import_edge(build_graph(files), "a.py", "b.py")
    assert edge.evidence == "a.py:1"


def test_dotted_import_records_the_line():
    """``import pkg.sub`` 走的是另一处构造点（alias 分支）。"""
    files = [
        _model("a.py", "\nimport pkg.sub\n"),
        _model("pkg/__init__.py", ""),
        _model("pkg/sub.py", "Y = 2\n"),
    ]
    edge = _import_edge(build_graph(files), "a.py", "pkg/sub.py")
    assert edge.evidence == "a.py:2"


def test_submodule_from_import_records_the_line():
    """``from pkg import sub``：``sub`` 是子模块，连的是 ``pkg/sub.py`` 而不是包。"""
    files = [
        _model("a.py", "# 注释\n\nfrom pkg import sub\n"),
        _model("pkg/__init__.py", ""),
        _model("pkg/sub.py", "Y = 2\n"),
    ]
    edge = _import_edge(build_graph(files), "a.py", "pkg/sub.py")
    assert edge.evidence == "a.py:3"


def test_reexport_hit_records_the_import_line():
    """名字经 re-export 解析到具体符号时，边指向符号所属模块（第 4 处构造点）。"""
    files = [
        _model("a.py", "from pkg import Service\n"),
        _model("pkg/__init__.py", "from pkg.core import Service\n"),
        _model("pkg/core.py", "class Service: pass\n"),
    ]
    edge = _import_edge(build_graph(files), "a.py", "pkg/core.py")
    assert edge.evidence == "a.py:1"


def test_external_import_also_records_the_line():
    """落 external 也是 IMPORTS 边：规则/报告同样要能说清"哪一行引了这个外部包"。"""
    files = [_model("a.py", "import numpy.linalg\n")]
    result = build_graph(files)
    hits = [e for e in result.edges if e.type == EdgeType.IMPORTS and e.target == external_id("numpy")]
    assert len(hits) == 1
    assert hits[0].evidence == "a.py:1"


def test_unresolved_dotted_import_records_the_line():
    """``import 未安装的包`` 走的是"import 分支 + 落 external"这处构造点。"""
    files = [_model("a.py", "# 头注释\nimport not_here.sub\n")]
    result = build_graph(files)
    hits = [e for e in result.edges if e.type == EdgeType.IMPORTS and e.target == external_id("not_here")]
    assert len(hits) == 1
    assert hits[0].evidence == "a.py:2"


def test_unresolved_from_import_records_the_line():
    """``from 未安装的包 import X`` 走的是函数末尾那处构造点 —— 与上一条不是同一处。"""
    files = [_model("a.py", "\nfrom missing_pkg import Y\n")]
    result = build_graph(files)
    hits = [e for e in result.edges if e.type == EdgeType.IMPORTS and e.target == external_id("missing_pkg")]
    assert len(hits) == 1
    assert hits[0].evidence == "a.py:2"


def test_name_missing_from_existing_module_records_the_line():
    """``from b import 不存在的名字``：模块解析成功、名字没解析到，是第 6 处构造点。

    与"包整个不存在"（落 external）不是同一个分支，两侧都得带行号。
    """
    files = [_model("a.py", "from b import nosuch\n"), _model("b.py", "X = 1\n")]
    edge = _import_edge(build_graph(files), "a.py", "b.py")
    assert edge.evidence == "a.py:1"
