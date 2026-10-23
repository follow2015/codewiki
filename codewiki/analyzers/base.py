"""Analyzer 协议（**冻结接口**，设计文稿 §4.1 / §十一 第一阶段）。

冻结内容：

- ``Analyzer.parse(path, content) -> FileModel`` 的输入输出签名；
- 职责边界：**只做单文件事实提取**，不做跨文件解析、不做符号消歧、不建图。

冻结之后：新增语言 = 新增一个实现该协议的模块；新增后端（正则/AST/tree-sitter/
SCIP）= 平行插件，不改主干（文稿 §十三 决策 5）。

[WARN] ``FileModel`` 的字段变更属于**契约变更**，必须同步
``tests/golden/packet_schema.json`` 与 ``codewiki/models.py`` 的注释。
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from codewiki.models import FileModel


@runtime_checkable
class Analyzer(Protocol):
    """单语言解析器协议。"""

    language: str

    def parse(self, path: Path, content: str) -> FileModel:
        """解析单个文件，返回该文件的事实（不跨文件解析）。

        Args:
            path: 相对仓库根的路径（ID 与展示都基于它）。
            content: 文件文本内容（由调用方读取，Analyzer 不碰文件系统，
                便于测试直接喂字符串）。

        Returns:
            FileModel: 该文件可见的导入/符号/调用/继承/导出事实。
        """
        ...


class AnalyzerRegistry:
    """语言 + 后端 → Analyzer 工厂的注册表。

    一期只注册 Python 的 AST 后端；正则后端与 tree-sitter 后端随阶段推进注册，
    SCIP 在阶段一以 Mock 实现占位（返回固定样本），均不改本文件以外的主干。
    """

    def __init__(self) -> None:
        self._factories: dict[tuple[str, str], type[Analyzer] | object] = {}

    def register(self, language: str, backend: str, factory) -> None:  # noqa: ANN001
        self._factories[(language, backend)] = factory

    def get(self, language: str, backend: str = "auto"):  # noqa: ANN001
        """取一个 Analyzer 实例；``auto`` 按 ast → treesitter → regex 探测降级。"""
        if backend != "auto":
            factory = self._factories.get((language, backend))
            if factory is None:
                raise KeyError(f"未注册的分析后端: language={language} backend={backend}")
            return factory()

        for candidate in ("ast", "treesitter", "regex"):
            factory = self._factories.get((language, candidate))
            if factory is not None:
                return factory()
        raise KeyError(f"该语言没有任何可用后端: {language}")


REGISTRY = AnalyzerRegistry()


def register_defaults() -> None:
    """注册内置后端（Python AST + 正则降级 + 可选 tree-sitter）。"""
    from codewiki.analyzers.python_analyzer import PythonAstAnalyzer
    from codewiki.analyzers.regex_backend import RegexAnalyzer

    REGISTRY.register("python", "ast", PythonAstAnalyzer)
    REGISTRY.register("python", "regex", RegexAnalyzer)
    REGISTRY.register("other", "regex", RegexAnalyzer)
    _register_optional_treesitter()


def _register_optional_treesitter() -> None:
    """注册 TS/Go（tree-sitter 是**可选**依赖：没装就静默跳过，用正则降级）。

    静默不等于无声：``doctor`` 会报"未安装（可选）"。这里刻意不抛异常 ——
    装不上 tree-sitter 的用户仍应能用 Python/AST 与正则跑通全流程。
    """
    try:
        from codewiki.analyzers.go_analyzer import GoAnalyzer
        from codewiki.analyzers.ts_analyzer import TypeScriptAnalyzer

        ts = TypeScriptAnalyzer()
        go = GoAnalyzer()
    except ImportError:
        return
    for language in ("typescript", "tsx", "javascript", "jsx"):
        REGISTRY.register(language, "treesitter", lambda _ts=ts: _ts)
    REGISTRY.register("go", "treesitter", lambda _go=go: _go)
