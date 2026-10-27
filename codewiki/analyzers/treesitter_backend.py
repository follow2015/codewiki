"""tree-sitter 后端（TS/Go，§4.6）。

与 Python AST 后端同纪律：**只产本文件事实**，不做跨文件解析。

两条工程约束：

1. **语言包是可选依赖**（``[treesitter]`` extra）：导入失败时本模块必须能被
   ``register_defaults()`` 安全地跳过（正则降级），不能让"没装 tree-sitter"
   变成启动崩溃；
2. **版本锁死**：tree-sitter 与语言包三者作为一组联动升级（``==``），
   语法节点名会随 grammar 版本变化，混版本会静默少解析。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from codewiki.models import (
    CallFact,
    FileModel,
    ImportFact,
    InheritanceFact,
    Provenance,
    SymbolFact,
)

BACKEND = "treesitter"


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]


@dataclass(frozen=True)
class LanguageSpec:
    """一个 tree-sitter 语言的解析配置（节点名 → 事实的映射表）。"""

    language: str
    class_types: tuple[str, ...]
    function_types: tuple[str, ...]
    method_types: tuple[str, ...]
    import_types: tuple[str, ...]
    call_types: tuple[str, ...]
    inherits_types: tuple[str, ...] = ()


class TreeSitterAnalyzer:
    """通用 tree-sitter 后端：按 :class:`LanguageSpec` 遍历语法树取事实。"""

    def __init__(self, spec: LanguageSpec, grammar, *, version: str = "") -> None:
        from tree_sitter import Language, Parser

        self.spec = spec
        self.version = version
        self._parser = Parser(Language(grammar))

    def parse(self, path: Path, content: str) -> FileModel:
        data = content.encode("utf-8", errors="replace")
        tree = self._parser.parse(data)
        root = tree.root_node

        imports: list[ImportFact] = []
        symbols: list[SymbolFact] = []
        calls: list[CallFact] = []
        inherits: list[InheritanceFact] = []
        exports: list[str] = []

        self._walk(root, data, imports, symbols, calls, inherits, exports, class_name=None)

        return FileModel(
            path=str(path),
            language=self.spec.language,
            hash=_content_hash(content),
            imports=imports,
            symbols=symbols,
            calls=calls,
            inherits=inherits,
            exports=exports,
            provenance=Provenance(backend=BACKEND, version=self.version or None),
        )

    def _walk(self, node, data, imports, symbols, calls, inherits, exports, *, class_name):
        kind = node.type
        if kind in self.spec.class_types:
            name = self._child_text(node, "name", data)
            if name:
                symbols.append(
                    SymbolFact(
                        name=name,
                        qualname=name,
                        kind="class",
                        lineno=node.start_point[0] + 1,
                        endlineno=node.end_point[0] + 1,
                        docstring=self._leading_comment(node, data),
                    )
                )
                class_name = name
        elif kind in self.spec.function_types or kind in self.spec.method_types:
            name = self._child_text(node, "name", data)
            if name:
                is_method = kind in self.spec.method_types
                symbols.append(
                    SymbolFact(
                        name=name,
                        qualname=f"{class_name}.{name}" if (is_method and class_name) else name,
                        kind="method" if is_method else "function",
                        lineno=node.start_point[0] + 1,
                        endlineno=node.end_point[0] + 1,
                        parent=class_name if is_method else None,
                        signature=self._signature(node, data),
                        docstring=self._leading_comment(node, data),
                    )
                )
        elif kind in self.spec.import_types:
            imports.extend(self._import_facts(node, data))
        elif kind in self.spec.call_types:
            callee = self._callee(node, data)
            if callee:
                calls.append(CallFact(callee=callee, lineno=node.start_point[0] + 1))
        elif kind in self.spec.inherits_types:
            inherits.extend(self._inherits_facts(node, data, class_name))

        for child in node.children:
            self._walk(child, data, imports, symbols, calls, inherits, exports, class_name=class_name)

    def _child_text(self, node, field: str, data: bytes) -> str | None:
        child = node.child_by_field_name(field)
        return self._text(child, data) if child else None

    def _import_facts(self, node, data: bytes) -> list[ImportFact]:
        raise NotImplementedError

    def _callee(self, node, data: bytes) -> str | None:
        func = node.child_by_field_name("function")
        if func is None:
            return None
        if func.type in ("identifier", "type_identifier", "property_identifier"):
            return self._text(func, data)
        for field in ("property", "attribute", "name", "field_identifier"):
            member = func.child_by_field_name(field)
            if member is not None:
                return self._text(member, data)
        return None

    def _inherits_facts(self, node, data: bytes, class_name: str | None) -> list[InheritanceFact]:
        return []

    def _signature(self, node, data: bytes) -> str | None:
        params = node.child_by_field_name("parameters") or node.child_by_field_name("parameters")
        return self._text(params, data) if params else None

    @staticmethod
    def _text(node, data: bytes) -> str:
        return data[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

    @staticmethod
    def _leading_comment(node, data: bytes) -> str | None:
        """取紧邻定义上方的注释（JSDoc / Go doc）。

        **要向上看一层**：TS 里注释挂在 ``export_statement`` 之外，Go 里挂在
        ``type_declaration`` 之外 —— 只看定义节点本身的前兄弟会拿到 `export`
        / `type` 关键字，docstring 就永远为空。
        """
        current = node
        for _ in range(2):
            prev = current.prev_sibling
            if prev is not None and prev.type == "comment":
                return data[prev.start_byte : prev.end_byte].decode("utf-8", errors="replace").strip()
            parent = current.parent
            if parent is None or parent.type == "program":
                return None
            current = parent
        return None
