"""``from pkg import sub``：``sub`` 是子模块文件时，依赖边必须落到子模块本身。

此前落到 ``pkg/__init__.py``（有 ``__init__`` 时），或整条退化成 ``ext::pkg``
（命名空间包、没有 ``__init__`` 时）—— 图上少一条真实依赖，度数与 AI 语境跟着失真。
"""

from __future__ import annotations

from codewiki.graph import build_graph
from tests.conftest import parse_all


def _imports(root):
    result = build_graph(parse_all(root))
    return {(e.source, e.target) for e in result.edges if e.type.value == "IMPORTS"}, result


def _write(root, files: dict[str, str]):
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_from_package_import_submodule_with_init(tmp_path):
    _write(
        tmp_path,
        {
            "app/__init__.py": "",
            "app/a.py": "def run():\n    return 1\n",
            "app/b.py": "from app import a\n\ndef main():\n    return a.run()\n",
        },
    )
    edges, _ = _imports(tmp_path)
    assert ("mod::app/b.py", "mod::app/a.py") in edges
    assert ("mod::app/b.py", "mod::app/__init__.py") not in edges


def test_from_namespace_package_import_submodule(tmp_path):
    _write(
        tmp_path,
        {
            "app/a.py": "def run():\n    return 1\n",
            "app/b.py": "from app import a\n\ndef main():\n    return a.run()\n",
        },
    )
    edges, _ = _imports(tmp_path)
    assert ("mod::app/b.py", "mod::app/a.py") in edges
    assert ("mod::app/b.py", "ext::app") not in edges


def test_relative_from_dot_import_submodule(tmp_path):
    _write(
        tmp_path,
        {
            "app/__init__.py": "",
            "app/a.py": "def run():\n    return 1\n",
            "app/b.py": "from . import a\n\ndef main():\n    return a.run()\n",
        },
    )
    edges, _ = _imports(tmp_path)
    assert ("mod::app/b.py", "mod::app/a.py") in edges


def test_symbol_wins_over_submodule(tmp_path):
    """包的 ``__init__`` 里有同名符号时，Python 取属性、不是子模块 —— 边指向定义处。"""
    _write(
        tmp_path,
        {
            "app/__init__.py": "def a():\n    return 0\n",
            "app/a.py": "def run():\n    return 1\n",
            "app/b.py": "from app import a\n\ndef main():\n    return a()\n",
        },
    )
    edges, _ = _imports(tmp_path)
    assert ("mod::app/b.py", "mod::app/__init__.py") in edges
    assert ("mod::app/b.py", "mod::app/a.py") not in edges


def test_unknown_name_still_degrades_to_package(tmp_path):
    """既不是符号也不是子模块：保持原有退化（包级边 + 诊断），不凭空造边。"""
    _write(
        tmp_path,
        {
            "app/__init__.py": "",
            "app/b.py": "from app import ghost\n",
        },
    )
    edges, result = _imports(tmp_path)
    assert ("mod::app/b.py", "mod::app/__init__.py") in edges
    assert any(d.code == "IMPORT_NAME_UNRESOLVED" for d in result.diagnostics)
