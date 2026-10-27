"""Analyzer 后端集合（§4.1）。

对外只暴露协议与注册表：``from codewiki.analyzers import build_registry``。
新增语言 = 新增模块 + 在 ``base._populate()`` 里注册一行。
"""

from __future__ import annotations

from codewiki.analyzers.base import (
    FORMAT_VERSIONS,
    REGISTRY,
    Analyzer,
    AnalyzerRegistry,
    build_registry,
    format_version_of,
    register_defaults,
)

__all__ = [
    "FORMAT_VERSIONS",
    "REGISTRY",
    "Analyzer",
    "AnalyzerRegistry",
    "build_registry",
    "format_version_of",
    "register_defaults",
]
