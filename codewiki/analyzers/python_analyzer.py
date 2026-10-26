"""Python AST 后端（默认解析器，设计文稿 §4.1）。

实现要点：

- **零依赖**：只用标准库 ``ast``，不需要 tree-sitter，也不需要安装被测项目。
- **不反射注解**：全部信息来自 AST，不触碰 ``__annotations__`` —— 因此
  Python 3.14 的 PEP 649 延迟注解对本后端无影响（与 9.1 的回归要求一致）。
- **不做跨文件解析**：只产出本文件可见的事实，消歧交给 ``graph/builder.py``。
"""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path

from codewiki.models import (
    CallFact,
    DynamicFact,
    FileModel,
    ImportFact,
    InheritanceFact,
    Provenance,
    SymbolFact,
)

LANGUAGE = "python"
BACKEND = "ast"


def _content_hash(content: str) -> str:
    """内容哈希（增量分析用；只取前 16 位，够用且短）。"""
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]


def _unparse(node: ast.AST | None) -> str | None:
    """把 AST 片段回写成源码文本（参数串/基类名/装饰器等）。"""
    if node is None:
        return None
    try:
        return ast.unparse(node).strip()
    except Exception:
        return None


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """函数签名：``(a, b=1, *args, **kw) -> Ret``（不含注解以外的装饰）。"""
    args = _unparse(node.args) or ""
    returns = f" -> {_unparse(node.returns)}" if node.returns is not None else ""
    return f"({args}){returns}"


_DYNAMIC_IMPORT_FUNCS = {"import_module", "__import__"}


def _dynamic_facts(tree: ast.AST) -> list[DynamicFact]:
    """扫 `importlib.import_module("x")` / `__import__("x")` 的字面目标。"""
    facts: list[DynamicFact] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _callee_name(node)
        if name not in _DYNAMIC_IMPORT_FUNCS or not node.args:
            continue
        target = node.args[0]
        if isinstance(target, ast.Constant) and isinstance(target.value, str) and target.value.strip():
            facts.append(
                DynamicFact(target=target.value.strip(), lineno=node.lineno, reason=f"{name}() 动态导入")
            )
    return facts


def _callee_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _callee_of(node: ast.Call) -> tuple[str, str | None]:
    """调用目标：``Name`` → 名字；``Attribute`` → 属性名 + 接收者表达式。"""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id, None
    if isinstance(func, ast.Attribute):
        return func.attr, _unparse(func.value)
    return _unparse(func) or "<unknown>", None


def _decorators(node: ast.AST) -> list[str]:
    raw = getattr(node, "decorator_list", []) or []
    return [d for d in (_unparse(x) for x in raw) if d]


def _all_exports(tree: ast.Module) -> list[str]:
    """``__all__`` 的公开面（影响文档展示，不影响对未列入名字的解析，§2.4）。"""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "__all__":
                try:
                    value = ast.literal_eval(node.value)
                except Exception:
                    return []
                if isinstance(value, (list, tuple)):
                    return [str(v) for v in value]
    return []


class PythonAstAnalyzer:
    """按 ``Analyzer`` 协议实现的 Python 后端。"""

    language = LANGUAGE

    def parse(self, path: Path, content: str) -> FileModel:
        rel = str(path).replace("\\", "/")
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return FileModel(
                path=rel,
                language=LANGUAGE,
                hash=_content_hash(content),
                provenance=Provenance(backend=BACKEND),
            )

        imports: list[ImportFact] = []
        symbols: list[SymbolFact] = []
        calls: list[CallFact] = []
        inherits: list[InheritanceFact] = []

        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(
                        ImportFact(module=alias.name, alias=alias.asname, lineno=node.lineno)
                    )
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    is_star = alias.name == "*"
                    module = ("." * node.level) + (node.module or "")
                    imports.append(
                        ImportFact(
                            module=module or ".",
                            name=None if is_star else alias.name,
                            alias=alias.asname,
                            is_star=is_star,
                            lineno=node.lineno,
                            kind="from",
                        )
                    )

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(
                    SymbolFact(
                        name=node.name,
                        qualname=node.name,
                        kind="function",
                        lineno=node.lineno,
                        endlineno=node.end_lineno or node.lineno,
                        signature=_signature(node),
                        docstring=ast.get_docstring(node),
                        decorators=_decorators(node),
                    )
                )
            elif isinstance(node, ast.ClassDef):
                symbols.append(
                    SymbolFact(
                        name=node.name,
                        qualname=node.name,
                        kind="class",
                        lineno=node.lineno,
                        endlineno=node.end_lineno or node.lineno,
                        docstring=ast.get_docstring(node),
                        decorators=_decorators(node),
                    )
                )
                for base in node.bases:
                    base_name = _unparse(base)
                    if base_name:
                        inherits.append(InheritanceFact(child=node.name, base=base_name))
            elif isinstance(node, ast.Call):
                callee, receiver = _callee_of(node)
                calls.append(CallFact(callee=callee, receiver=receiver, lineno=node.lineno))

        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        symbols.append(
                            SymbolFact(
                                name=target.id,
                                qualname=target.id,
                                kind="constant",
                                lineno=node.lineno,
                                endlineno=node.end_lineno or node.lineno,
                            )
                        )

        for symbol in symbols:
            if symbol.kind not in ("function", "class"):
                continue
            parents = [
                other
                for other in symbols
                if other.kind == "class"
                and other is not symbol
                and other.lineno <= symbol.lineno
                and (other.endlineno or 0) >= (symbol.endlineno or 0)
            ]
            if parents:
                parent = max(parents, key=lambda p: p.lineno)
                parent_qualname = parent.qualname or parent.name
                symbol.parent = parent_qualname
                symbol.qualname = f"{parent_qualname}.{symbol.name}"
                if symbol.kind == "function":
                    symbol.kind = "method"

        qualname_by_name: dict[str, list[str]] = {}
        for symbol in symbols:
            if symbol.kind == "class":
                qualname_by_name.setdefault(symbol.name, []).append(symbol.qualname)
        for item in inherits:
            candidates = qualname_by_name.get(item.child, [])
            if len(candidates) == 1:
                item.child = candidates[0]

        return FileModel(
            path=rel,
            language=LANGUAGE,
            hash=_content_hash(content),
            imports=imports,
            symbols=symbols,
            calls=calls,
            inherits=inherits,
            dynamic=_dynamic_facts(tree),
            exports=_all_exports(tree),
            provenance=Provenance(backend=BACKEND),
        )
