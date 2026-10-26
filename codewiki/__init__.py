"""codewiki —— 配置驱动、可离线交付的代码架构知识图谱与文档生成器。

分层（设计文稿 §一）：Infrastructure → Core Engine → Render & Export → AI（可选）。
一期先落前三层（纯静态、零网络依赖），AI 层在阶段二以独立命令接入。
"""
from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _resolve_version() -> str:
    """版本号**只在 pyproject.toml 定义**，这里只负责读出来。

    此前 ``pyproject`` 写 1.0.0、``__init__`` 与数据包 ``tool_version`` 各写 0.1.0，
    于是「发了 1.0 的数据包却自称 0.1」这种事只能靠人眼发现（设计文稿 §数据包一节）。

    两条读取路径都要：已安装时 ``importlib.metadata`` 是权威（含 wheel 里的元数据）；
    源码树直接跑（测试、CI 的 ``python -m pytest``）时包不一定装过，回落解析
    pyproject —— 仍然只有一处定义，不存在第二份版本号。
    """
    try:
        return version("codewiki")
    except PackageNotFoundError:
        pass

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        resolved = declared.get("project", {}).get("version")
        if isinstance(resolved, str) and resolved:
            return resolved
    except (OSError, tomllib.TOMLDecodeError):
        pass
    return "0.0.0+unknown"


__version__ = _resolve_version()

__all__ = ["__version__"]
