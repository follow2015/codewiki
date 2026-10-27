"""TypeScript / TSX 后端（tree-sitter，§4.6）。

取三类事实：import / re-export、类与方法与函数、**能说出名字的调用**。

刻意不做的两件事：

1. **类型级依赖**（``import type``、泛型约束）不建边 —— 它们不构成运行时耦合，
   建了会让架构图被"类型体操"淹没；真需要时再作为低置信边另开；
2. **不解析别名/路径映射**（tsconfig paths）：那需要读 tsconfig 并做前缀替换，
   属于跨文件配置解析，交给引擎层的模块解析（与 Python 的相对导入同理）。
"""

from __future__ import annotations

from pathlib import Path

from codewiki.analyzers.treesitter_backend import LanguageSpec, TreeSitterAnalyzer
from codewiki.models import ImportFact, InheritanceFact

LANGUAGE = "typescript"


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

    def _import_facts(self, node, data: bytes) -> list[ImportFact]:
        lineno = node.start_point[0] + 1
        source = node.child_by_field_name("source")
        if source is None:
            return []
        module = self._text(source, data).strip().strip("'\"")

        facts: list[ImportFact] = []
        if node.type == "export_statement":
            for child in node.children:
                if child.type == "export_clause":
                    for spec in child.children:
                        if spec.type == "export_specifier":
                            name = self._child_text(spec, "name", data)
                            alias = self._child_text(spec, "alias", data)
                            facts.append(
                                ImportFact(module=module, name=name, alias=alias, lineno=lineno, kind="from")
                            )
                elif child.type == "namespace_export" or child.type == "asterisk":
                    facts.append(ImportFact(module=module, is_star=True, lineno=lineno, kind="from"))
            if not facts:
                facts.append(ImportFact(module=module, is_star=True, lineno=lineno, kind="from"))
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
                                        module=module, name=name, alias=alias, lineno=lineno, kind="from"
                                    )
                                )
                    elif part.type == "namespace_import":
                        facts.append(ImportFact(module=module, name="*", lineno=lineno, kind="from"))
        if default_name:
            facts.append(ImportFact(module=module, name=default_name, lineno=lineno, kind="from"))
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
