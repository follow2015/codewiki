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

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from codewiki.models import FileModel

FORMAT_VERSIONS: dict[str, int] = {
    "ast": 1,
    "regex": 1,
    "treesitter": 1,
}


def format_version_of(backend: str) -> int:
    """取后端的解析结果格式版本；未登记的后端按 0 处理（新增后端需在此登记）。"""
    return FORMAT_VERSIONS.get(backend, 0)


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

    注册的是**工厂**而不是实例：可选后端（tree-sitter）的导入与构造都被推迟到
    ``get()`` 那次调用，没装 extra 的机器跑 Python 分析时不会为此付一次 import。

    一个实例就是一份独立注册表 —— 需要干净环境的场景（测试、将来按仓库配置裁剪
    后端）用 :func:`build_registry` 拿新的，不要往进程级 :data:`REGISTRY` 里塞。
    """

    def __init__(self) -> None:
        self._factories: dict[tuple[str, str], Callable[[], Analyzer | None]] = {}

    def register(self, language: str, backend: str, factory: Callable[[], Analyzer | None]) -> None:
        self._factories[(language, backend)] = factory

    def get(self, language: str, backend: str = "auto") -> Analyzer:
        """取一个 Analyzer 实例；``auto`` 按 ast → treesitter → regex 探测降级。

        Raises:
            KeyError: 该语言没有可用后端；或显式点名的后端其可选依赖未安装。
        """
        if backend != "auto":
            factory = self._factories.get((language, backend))
            if factory is None:
                raise KeyError(f"未注册的分析后端: language={language} backend={backend}")
            instance = factory()
            if instance is None:
                raise KeyError(
                    f"分析后端不可用（可选依赖未安装）: language={language} backend={backend}"
                )
            return instance

        for candidate in ("ast", "treesitter", "regex"):
            factory = self._factories.get((language, candidate))
            if factory is None:
                continue
            instance = factory()
            if instance is not None:
                return instance
        raise KeyError(f"该语言没有任何可用后端: {language}")


def _lazy_analyzer(module: str, klass: str) -> Callable[[], Analyzer | None]:
    """构造"首次调用才导入"的工厂；可选依赖缺失返回 ``None``。

    ⚠️ 导入与构造都必须在 try 里：``TypeScriptAnalyzer.__init__`` 里才 import
    ``tree_sitter_typescript``（模块顶层没有），只护导入护不住 —— CI 实测未装
    ``[treesitter]`` extra 时在构造期炸掉整个 CLI。
    """

    def factory() -> Analyzer | None:
        try:
            mod = importlib.import_module(module)
            return getattr(mod, klass)()
        except ImportError:
            return None

    return factory


REGISTRY = AnalyzerRegistry()

_TS_BACKENDS: dict[str, tuple[str, str]] = {
    "typescript": ("codewiki.analyzers.ts_analyzer", "TypeScriptAnalyzer"),
    "tsx": ("codewiki.analyzers.ts_analyzer", "TypeScriptAnalyzer"),
    "javascript": ("codewiki.analyzers.ts_analyzer", "TypeScriptAnalyzer"),
    "jsx": ("codewiki.analyzers.ts_analyzer", "TypeScriptAnalyzer"),
    "go": ("codewiki.analyzers.go_analyzer", "GoAnalyzer"),
}


def _populate(registry: AnalyzerRegistry, *, allow_treesitter: bool = True) -> None:
    """往给定注册表里填内置后端（Python AST + 正则降级 + 可选 tree-sitter）。"""
    from codewiki.analyzers.python_analyzer import PythonAstAnalyzer
    from codewiki.analyzers.regex_backend import RegexAnalyzer

    registry.register("python", "ast", PythonAstAnalyzer)
    registry.register("python", "regex", RegexAnalyzer)
    registry.register("other", "regex", RegexAnalyzer)
    if allow_treesitter:
        _register_optional_treesitter(registry)


def build_registry(*, allow_treesitter: bool = True) -> AnalyzerRegistry:
    """造一份**独立**的注册表（不碰进程级 :data:`REGISTRY`）。

    ``allow_treesitter=False`` 时连工厂都不挂 —— 给"要确定性降级"的测试用。
    """
    registry = AnalyzerRegistry()
    _populate(registry, allow_treesitter=allow_treesitter)
    return registry


def register_defaults() -> None:
    """把内置后端注册进进程级 :data:`REGISTRY`（兼容入口，等价 ``_populate(REGISTRY)``）。"""
    _populate(REGISTRY)


def _register_optional_treesitter(registry: AnalyzerRegistry | None = None) -> None:
    """挂上 TS/Go 的**延迟**工厂（tree-sitter 是可选依赖，没装就降级为正则）。

    静默不等于无声：``doctor`` 会报"未安装（可选）"。这里刻意不抛异常 ——
    装不上 tree-sitter 的用户仍应能用 Python/AST 与正则跑通全流程。
    真正缺依赖的事实由 ``get()`` 在用到的那一刻给出（显式点名 → KeyError）。
    """
    reg = REGISTRY if registry is None else registry
    for language, (module, klass) in _TS_BACKENDS.items():
        reg.register(language, "treesitter", _lazy_analyzer(module, klass))

