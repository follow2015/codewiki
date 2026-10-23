"""Analyzer 后端集合（§4.1）。

对外只暴露协议与注册表：``from codewiki.analyzers import REGISTRY, register_defaults``。
新增语言 = 新增模块 + 在 ``base.register_defaults()`` 里注册一行。
"""
from __future__ import annotations

from codewiki.analyzers.base import REGISTRY, Analyzer, AnalyzerRegistry, register_defaults

__all__ = ["Analyzer", "AnalyzerRegistry", "REGISTRY", "register_defaults"]
