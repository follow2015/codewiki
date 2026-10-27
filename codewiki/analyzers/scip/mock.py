"""SCIP 的**占位实现**（阶段一要求的 Mock，§4.6.2）。

它不读索引器、不发网络请求、不解析二进制 —— 只返回一份固定样本 FileModel。
用途有两个：

1. 让"SCIP 后端"这条链路在**没有索引器**的环境下也能被端到端跑通（CI 友好）；
2. 给上层（注册表 / CLI / 测试）一个稳定的形状参照。

真实的解析走 :func:`codewiki.analyzers.scip.mapper.map_index`，它以固定样本
映射测试全绿为**准入口径**，不靠 Mock 蒙混。
"""

from __future__ import annotations

from pathlib import Path

from codewiki.models import CallFact, FileModel, ImportFact, Provenance, SymbolFact

BACKEND = "scip-mock"

SAMPLE = FileModel(
    path="pkg/core.py",
    language="python",
    hash="scipmock0000001",
    imports=[ImportFact(module="pkg.util", lineno=3, kind="import")],
    symbols=[
        SymbolFact(
            name="Service",
            qualname="pkg/core.py.Service",
            kind="class",
            lineno=6,
            endlineno=20,
            docstring="核心服务（SCIP 样本）。",
        ),
        SymbolFact(
            name="Service.run",
            qualname="pkg/core.py.Service.run",
            kind="method",
            lineno=9,
            endlineno=18,
            parent="pkg/core.py.Service",
        ),
    ],
    calls=[CallFact(callee="helper", receiver="pkg/core.py.Service.run", lineno=12)],
    provenance=Provenance(backend=BACKEND, version="mock-1"),
)


def sample_models() -> list[FileModel]:
    """返回固定样本（每次调用都是新对象，避免调用方改到共享状态）。"""
    return [SAMPLE.model_copy(deep=True)]


def parse(_path: Path, _content: str) -> FileModel:
    """Analyzer 协议兼容的占位实现：**忽略输入**，只回样本。

    刻意标注"忽略输入"：把它接到真实流程里会得到与源码无关的结果 —— 这正是它
    只能用于链路自检、不能用于产物交付的原因。
    """
    return SAMPLE.model_copy(deep=True)


__all__ = ["BACKEND", "SAMPLE", "parse", "sample_models"]
