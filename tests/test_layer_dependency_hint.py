"""``NO_LAYER_DEPENDENCY`` 的排障提示必须按**实际语言**分派。

这条诊断的语义是"跨文件 import 整体没解析出来"，但成因与解法逐语言完全不同：
Python 多半是 ``--src`` 指进了包内（绝对导入 ``from pkg.x import Y`` 落空），
TS/JS 多半是 ``@/x`` 别名没进 ``[analysis] ts_aliases``，Go 则连解析路径都还没接上。
给一句写死 ``from <root_package>.x import Y`` 的提示，TS/Go 用户看到的是"报了问题
却不给下一步"——比不报更难查，因为他们会去调 ``--src``，而那不是原因。
"""

from __future__ import annotations

import pytest

from codewiki.graph import build_graph
from tests.conftest import parse_all

TWO_LAYERS = [
    {"id": "layer::adapters", "name": "适配层", "paths": ["adapters"]},
    {"id": "layer::core", "name": "核心层", "paths": ["core"]},
]


def _message(result) -> str:
    """取出唯一的 NO_LAYER_DEPENDENCY 文案（重复即失败：同一事实不该报两遍）。"""
    hits = [d for d in result.diagnostics if d.code == "NO_LAYER_DEPENDENCY"]
    assert len(hits) == 1, [d.code for d in result.diagnostics]
    return hits[0].message


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_python_hint_still_points_at_the_package_root(tmp_path):
    """既有行为不许退化：配了 root_package 时提示要指名它。"""
    root = tmp_path / "proj"
    _write(root / "adapters" / "a.py", "from app.core.s import S\n")
    _write(root / "core" / "s.py", "S = 1\n")

    result = build_graph(parse_all(root), layers=TWO_LAYERS, root_package="proj")
    message = _message(result)

    assert "from proj.x import Y" in message, message
    assert "ts_aliases" not in message


def test_python_hint_mentions_root_package_config_when_unset(tmp_path):
    """没配 root_package 时旧实现给的是**空提示**（message 以句号草草收尾）。"""
    root = tmp_path / "proj"
    _write(root / "adapters" / "a.py", "from app.core.s import S\n")
    _write(root / "core" / "s.py", "S = 1\n")

    result = build_graph(parse_all(root), layers=TWO_LAYERS)
    message = _message(result)

    assert "root_package" in message, message
    assert "[analysis]" in message, message
    assert "from " in message, message


def test_typescript_hint_names_ts_aliases_not_python_syntax(tmp_path):
    """``@/core/s`` 落空是别名没配，不是 ``--src`` 选错层。"""
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_typescript")

    root = tmp_path / "web"
    _write(root / "src" / "adapters" / "a.ts", 'import { S } from "@/core/s";\nexport const A = () => S;\n')
    _write(root / "src" / "core" / "s.ts", "export const S = 1;\n")

    layers = [
        {"id": "layer::adapters", "name": "适配层", "paths": ["src/adapters"]},
        {"id": "layer::core", "name": "核心层", "paths": ["src/core"]},
    ]
    result = build_graph(parse_all(root), layers=layers)
    message = _message(result)

    assert "ts_aliases" in message, message
    assert "from " not in message, message
    assert "root_package" not in message, message


def test_go_hint_does_not_offer_a_workaround_that_cannot_work(tmp_path):
    """Go 的 import 是模块全路径，且引擎至今不按 ``.go`` 补后缀 —— 配别名也没用。

    所以这里断言的重点是"**不许**建议用户去配 ts_aliases / 改 --src"：那是让他
    花时间验证一个我们明知不通的路径。提示只陈述已知缺口。
    """
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_go")

    root = tmp_path / "svc"
    _write(root / "go.mod", "module example.com/demo\n\ngo 1.22\n")
    _write(
        root / "pkg" / "adapters" / "a.go",
        'package adapters\n\nimport "example.com/demo/pkg/core"\n\nfunc A() int { return core.S() }\n',
    )
    _write(root / "pkg" / "core" / "s.go", "package core\n\nfunc S() int { return 1 }\n")

    layers = [
        {"id": "layer::adapters", "name": "适配层", "paths": ["pkg/adapters"]},
        {"id": "layer::core", "name": "核心层", "paths": ["pkg/core"]},
    ]
    result = build_graph(parse_all(root), layers=layers)
    message = _message(result)

    assert "go.mod" in message, message
    assert "ts_aliases" not in message, message
    assert "from " not in message, message


def test_mixed_repo_gets_both_hints(tmp_path):
    """混语言仓（后端 Python + 前端 TS）：两句提示都要在，不能只挑一种。"""
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_typescript")

    root = tmp_path / "mono"
    _write(root / "adapters" / "a.py", "from app.core.s import S\n")
    _write(root / "core" / "s.py", "S = 1\n")
    _write(root / "src" / "adapters" / "a.ts", 'import { S } from "@/core/s";\nexport const A = () => S;\n')
    _write(root / "src" / "core" / "s.ts", "export const S = 1;\n")

    layers = [
        {"id": "layer::adapters", "name": "适配层", "paths": ["adapters", "src/adapters"]},
        {"id": "layer::core", "name": "核心层", "paths": ["core", "src/core"]},
    ]
    result = build_graph(parse_all(root), layers=layers, root_package="mono")
    message = _message(result)

    assert "from mono.x import Y" in message, message
    assert "ts_aliases" in message, message
