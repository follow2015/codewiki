"""HTML 转义的**单一出口**守卫（设计文稿 §P3-②）。

为什么需要这道闸：转义散落在各渲染模块时，漏掉一处就是一处 XSS；而"漏掉"在
代码评审里几乎看不出来（多写一个 f-string 就忘了）。所以把"只有一个地方能调
``html.escape``"变成可执行的断言，而不是一条口头约定。

三条断言：

1. 全仓（``codewiki/``）除 ``render/escape.py`` 外，不得出现 ``import html``
   或任何 ``html.escape(...)`` 调用；
2. ``escape_text`` / ``escape_attr`` 必须覆盖五类 HTML 元字符 —— 两个名字当前
   实现相同，但契约不同（文本语境 vs 属性语境），这里钉住"都不能漏引号"；
3. ``render/escape.py`` 不得反向依赖渲染层（保持它是叶子，谁都能用）。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from codewiki.render.escape import escape_attr, escape_text

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "codewiki"
ESCAPE_MODULE = PACKAGE / "render" / "escape.py"

_BARE_AMP_RE = re.compile(r"&(?!(?:amp|lt|gt|quot|#x27);)")


def _package_sources() -> list[Path]:
    return sorted(p for p in PACKAGE.rglob("*.py") if p.resolve() != ESCAPE_MODULE.resolve())


def _offences(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rel = path.relative_to(ROOT)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "html" for alias in node.names):
                found.append(f"{rel}:{node.lineno} import html")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "html":
                found.append(f"{rel}:{node.lineno} from html import ...")
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "escape"
                and isinstance(func.value, ast.Name)
                and func.value.id == "html"
            ):
                found.append(f"{rel}:{node.lineno} html.escape(...)")
    return found


def test_html_escaping_has_exactly_one_exit() -> None:
    """除 ``render/escape.py`` 外，任何模块都不许自己转义。"""
    offenders: list[str] = []
    for path in _package_sources():
        offenders.extend(_offences(path))
    assert offenders == [], "HTML 转义只能经过 codewiki.render.escape，发现绕过：\n  " + "\n  ".join(
        offenders
    )


def test_both_helpers_escape_the_five_html_characters() -> None:
    """文本语境与属性语境都必须吃掉 ``& < > " '``。"""
    hostile = "&<>\"'"
    for helper in (escape_text, escape_attr):
        out = helper(hostile)
        assert _BARE_AMP_RE.search(out) is None, f"{helper.__name__} 漏了 &：{out!r}"
        for ch in "<>\"'":
            assert ch not in out, f"{helper.__name__} 漏了 {ch!r}：{out!r}"


def test_escape_module_stays_a_leaf() -> None:
    """转义模块不得 import 渲染层的其它模块（否则又会绕回依赖环）。"""
    tree = ast.parse(ESCAPE_MODULE.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
    forbidden = [
        m for m in imported if m.startswith("codewiki.") and not m.startswith("codewiki.render.escape")
    ]
    assert forbidden == [], f"render/escape.py 不该依赖 {forbidden}"
