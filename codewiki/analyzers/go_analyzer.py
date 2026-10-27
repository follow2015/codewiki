"""Go 后端（tree-sitter，§4.6）。

Go 的"类"是 struct + 方法接收者，所以：

* ``type_declaration → type_spec`` 产出 class 符号；
* 带 receiver 的 ``method_declaration`` 产出 method 符号，**qualname 用
  ``<ReceiverType>.<Name>``** —— 这样引擎层的符号消歧能与其它语言对齐
  （不这么做的话，所有方法都叫同名，跨文件调用边会连错）；
* 函数名首字母大小写即导出语义，据此填 ``exports``（Go 没有 export 关键字）。
"""

from __future__ import annotations

from pathlib import Path

from codewiki.analyzers.treesitter_backend import LanguageSpec, TreeSitterAnalyzer
from codewiki.models import ImportFact

LANGUAGE = "go"


class GoAnalyzer(TreeSitterAnalyzer):
    """Go 解析（tree-sitter-go）。"""

    def __init__(self) -> None:
        import tree_sitter_go as go

        super().__init__(
            LanguageSpec(
                language=LANGUAGE,
                class_types=("type_spec",),
                function_types=("function_declaration",),
                method_types=("method_declaration",),
                import_types=("import_declaration",),
                call_types=("call_expression",),
            ),
            go.language(),
            version="tree-sitter-go==0.25.0",
        )

    def parse(self, path: Path, content: str):
        model = super().parse(path, content)
        model.exports = sorted(
            {s.name for s in model.symbols if s.name[:1].isupper() and s.name[:1].isalpha()}
        )
        return model

    def _import_facts(self, node, data: bytes) -> list[ImportFact]:
        facts: list[ImportFact] = []
        for child in node.children:
            if child.type == "import_spec":
                path_text = self._child_text(child, "path", data) or ""
                module = path_text.strip().strip('"')
                alias = self._child_text(child, "name", data)
                facts.append(
                    ImportFact(
                        module=module, alias=alias or None, lineno=child.start_point[0] + 1, kind="import"
                    )
                )
            elif child.type == "import_spec_list":
                for spec in child.children:
                    if spec.type == "import_spec":
                        path_text = self._child_text(spec, "path", data) or ""
                        alias = self._child_text(spec, "name", data)
                        facts.append(
                            ImportFact(
                                module=path_text.strip().strip('"'),
                                alias=alias or None,
                                lineno=spec.start_point[0] + 1,
                                kind="import",
                            )
                        )
            elif child.type == "interpreted_string_literal":
                facts.append(
                    ImportFact(
                        module=self._text(child, data).strip().strip('"'),
                        lineno=child.start_point[0] + 1,
                        kind="import",
                    )
                )
        return facts

    def _signature(self, node, data: bytes) -> str | None:
        params = node.child_by_field_name("parameters")
        result = node.child_by_field_name("result")
        text = self._text(params, data) if params else "()"
        if result is not None:
            text = f"{text} {self._text(result, data)}"
        return text

    def _walk(self, node, data, imports, symbols, calls, inherits, exports, *, class_name):
        kind = node.type
        if kind == "method_declaration" and class_name is None:
            receiver = node.child_by_field_name("receiver")
            if receiver is not None:
                type_node = _receiver_type(receiver)
                if type_node is not None:
                    class_name = self._text(type_node, data).lstrip("*")
        elif kind == "type_declaration":
            pass
        super()._walk(node, data, imports, symbols, calls, inherits, exports, class_name=class_name)


def _receiver_type(receiver):
    """从 receiver 节点里挖出类型标识符（``(s *Service)`` → ``Service``）。"""
    for child in receiver.children:
        if child.type == "parameter_declaration":
            type_node = child.child_by_field_name("type")
            return type_node
    return None


__all__ = ["LANGUAGE", "GoAnalyzer"]
