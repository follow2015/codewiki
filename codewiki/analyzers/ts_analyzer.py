"""TypeScript / TSX 后端（tree-sitter，§4.6）。

取四类事实：import / re-export、符号（类、方法、函数、**箭头函数/包装出来的组件**、
接口 / 类型别名 / 枚举、导出的常量）、**能说出名字的调用**、**JSX 组件使用**。

关于类型级依赖：``import type`` / ``import { type X }`` / ``export type {…} from`` 照常读入，
但标 ``type_only`` —— 保留依赖事实（变更影响要用），不参与环检测。此前它们和运行时
import 混在一起，TS 里类型互相引用极常见，会刷出大量运行时根本不存在的"环"。

刻意不做的一件事：**不解析别名/路径映射**（tsconfig paths）：那需要读 tsconfig 并做前缀替换，
属于跨文件配置解析，交给引擎层的模块解析（与 Python 的相对导入同理）。
"""

from __future__ import annotations

from pathlib import Path

from codewiki.analyzers.treesitter_backend import LanguageSpec, TreeSitterAnalyzer
from codewiki.models import CallFact, ImportFact, InheritanceFact, SymbolFact

LANGUAGE = "typescript"

_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression", "function", "generator_function"})
_LOCAL_SCOPES = frozenset(
    {
        "function_declaration",
        "generator_function_declaration",
        "arrow_function",
        "function_expression",
        "function",
        "generator_function",
        "method_definition",
        "class_body",
        "statement_block",
    }
)
_TYPE_DECLARATIONS = {
    "interface_declaration": "interface",
    "type_alias_declaration": "type",
    "enum_declaration": "enum",
}


class TypeScriptAnalyzer(TreeSitterAnalyzer):
    """TS/TSX 解析（tsx grammar 覆盖 .ts / .tsx）。"""

    def __init__(self) -> None:
        import tree_sitter_typescript as ts

        super().__init__(
            LanguageSpec(
                language=LANGUAGE,
                class_types=("class_declaration", "abstract_class_declaration"),
                function_types=("function_declaration", "generator_function_declaration"),
                method_types=("method_definition", "public_field_definition"),
                import_types=("import_statement", "export_statement"),
                call_types=("call_expression",),
                inherits_types=("class_heritage",),
            ),
            ts.language_tsx(),
            version="tree-sitter-typescript==0.23.2",
        )

    def _walk(self, node, data, imports, symbols, calls, inherits, exports, *, class_name):
        kind = node.type
        if kind in _TYPE_DECLARATIONS:
            self._type_symbol(node, data, symbols)
        elif kind == "variable_declarator":
            self._declarator_symbol(node, data, symbols)
        elif kind in ("jsx_self_closing_element", "jsx_opening_element"):
            self._jsx_use(node, data, calls)
        super()._walk(node, data, imports, symbols, calls, inherits, exports, class_name=class_name)

    @staticmethod
    def _is_module_level(node) -> bool:
        current = node.parent
        while current is not None:
            if current.type in _LOCAL_SCOPES:
                return False
            current = current.parent
        return True

    def _type_symbol(self, node, data: bytes, symbols: list[SymbolFact]) -> None:
        name = self._child_text(node, "name", data)
        if not name or not self._is_module_level(node):
            return
        symbols.append(
            SymbolFact(
                name=name,
                qualname=name,
                kind=_TYPE_DECLARATIONS[node.type],
                lineno=node.start_point[0] + 1,
                endlineno=node.end_point[0] + 1,
                docstring=self._leading_comment(node, data),
            )
        )

    def _declarator_symbol(self, node, data: bytes, symbols: list[SymbolFact]) -> None:
        """模块级 ``const X = …``：函数值 → 函数；其余导出的 → 常量；非导出的只认全大写。

        React 里组件大多不是 ``function`` 声明，而是 ``const Button = forwardRef(() => …)`` /
        ``export const Page = () => …``；只认 ``function_declaration`` 时一个组件库的符号表近乎空白。
        """
        name_node = node.child_by_field_name("name")
        if name_node is None or name_node.type != "identifier" or not self._is_module_level(node):
            return
        name = self._text(name_node, data)
        declaration = node.parent
        exported = (
            declaration is not None
            and declaration.parent is not None
            and (declaration.parent.type == "export_statement")
        )
        function = self._function_value(node.child_by_field_name("value"))
        if function is not None:
            kind, signature = (
                "function",
                self._signature(function, data) or self._single_param(function, data),
            )
        elif exported or name.isupper():
            kind, signature = "constant", None
        else:
            return
        symbols.append(
            SymbolFact(
                name=name,
                qualname=name,
                kind=kind,
                lineno=node.start_point[0] + 1,
                endlineno=node.end_point[0] + 1,
                signature=signature,
                docstring=self._leading_comment(declaration or node, data),
            )
        )

    @staticmethod
    def _function_value(value):
        """值本身是函数，或是 ``forwardRef(() => …)`` / ``memo(…)`` / ``observer(…)`` 这类包装。"""
        if value is None:
            return None
        if value.type in _FUNCTION_VALUES:
            return value
        if value.type == "call_expression":
            args = value.child_by_field_name("arguments")
            for child in args.named_children if args is not None else ():
                if child.type in _FUNCTION_VALUES:
                    return child
        return None

    def _single_param(self, function, data: bytes) -> str | None:
        param = function.child_by_field_name("parameter")
        return self._text(param, data) if param is not None else None

    def _jsx_use(self, node, data: bytes, calls: list[CallFact]) -> None:
        """``<Button />`` 是对 ``Button`` 的使用：记成调用，才有"组件依赖组件"的边。

        只认首字母大写（``<div>`` 这类内建标签不是符号）；``<Foo.Bar />`` 记成 ``Bar`` 带 receiver。
        """
        name = node.child_by_field_name("name")
        if name is None:
            return
        text = self._text(name, data)
        lineno = node.start_point[0] + 1
        if name.type == "identifier" and text[:1].isupper():
            calls.append(CallFact(callee=text, lineno=lineno))
        elif name.type in ("member_expression", "nested_identifier"):
            receiver, _, member = text.rpartition(".")
            if member[:1].isupper() or receiver[:1].isupper():
                calls.append(CallFact(callee=member, receiver=receiver or None, lineno=lineno))

    @staticmethod
    def _has_type_keyword(node) -> bool:
        """``import type`` / ``export type`` / ``{ type X }`` 里的 ``type`` 关键字（匿名节点）。"""
        return any(child.type == "type" and not child.is_named for child in node.children)

    def _import_facts(self, node, data: bytes) -> list[ImportFact]:
        lineno = node.start_point[0] + 1
        source = node.child_by_field_name("source")
        if source is None:
            return []
        module = self._text(source, data).strip().strip("'\"")
        statement_type_only = self._has_type_keyword(node)

        facts: list[ImportFact] = []
        if node.type == "export_statement":
            for child in node.children:
                if child.type == "export_clause":
                    for spec in child.children:
                        if spec.type == "export_specifier":
                            name = self._child_text(spec, "name", data)
                            alias = self._child_text(spec, "alias", data)
                            facts.append(
                                ImportFact(
                                    module=module,
                                    name=name,
                                    alias=alias,
                                    lineno=lineno,
                                    kind="from",
                                    type_only=statement_type_only or self._has_type_keyword(spec),
                                )
                            )
                elif child.type == "namespace_export" or child.type == "asterisk":
                    facts.append(
                        ImportFact(
                            module=module,
                            is_star=True,
                            lineno=lineno,
                            kind="from",
                            type_only=statement_type_only,
                        )
                    )
            if not facts:
                facts.append(
                    ImportFact(
                        module=module, is_star=True, lineno=lineno, kind="from", type_only=statement_type_only
                    )
                )
            return facts

        default_name = None
        for child in node.children:
            if child.type == "import_clause":
                for part in child.children:
                    if part.type == "identifier":
                        default_name = self._text(part, data)
                    elif part.type == "named_imports":
                        for spec in part.children:
                            if spec.type == "import_specifier":
                                name = self._child_text(spec, "name", data)
                                alias = self._child_text(spec, "alias", data)
                                facts.append(
                                    ImportFact(
                                        module=module,
                                        name=name,
                                        alias=alias,
                                        lineno=lineno,
                                        kind="from",
                                        type_only=statement_type_only or self._has_type_keyword(spec),
                                    )
                                )
                    elif part.type == "namespace_import":
                        facts.append(
                            ImportFact(
                                module=module,
                                name="*",
                                lineno=lineno,
                                kind="from",
                                type_only=statement_type_only,
                            )
                        )
        if default_name:
            facts.append(
                ImportFact(
                    module=module,
                    name=default_name,
                    lineno=lineno,
                    kind="from",
                    type_only=statement_type_only,
                )
            )
        if not facts:
            facts.append(ImportFact(module=module, lineno=lineno, kind="import"))
        return facts

    def _inherits_facts(self, node, data: bytes, class_name: str | None) -> list[InheritanceFact]:
        if not class_name:
            return []
        out: list[InheritanceFact] = []
        for child in node.children:
            if child.type in ("extends_clause", "implements_clause"):
                inner = (
                    child.child_by_field_name("value") or child.named_children[0]
                    if child.named_children
                    else None
                )
                if inner is not None:
                    base = self._text(inner, data).strip()
                    out.append(InheritanceFact(child=class_name, base=base))
        return out

    def parse(self, path: Path, content: str):
        model = super().parse(path, content)
        if str(path).endswith(".tsx"):
            model.language = "tsx"
        return model


__all__ = ["LANGUAGE", "TypeScriptAnalyzer"]
