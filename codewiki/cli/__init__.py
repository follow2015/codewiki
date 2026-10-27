"""CLI 入口：只做参数解析与分发（设计文稿 §目标架构）。

这个文件刻意只有四十几行。它以前是 1052 行、13 个命令处理函数同住一处 ——
后果不是"难读"，而是**规则只能靠端到端验证**：想确认"增量为什么退化成全量"，
只能造一个仓库跑完整条流水线再看产物。

现在每个子命令在自己的模块里，流水线主体在 :mod:`codewiki.pipeline.analyze`，
这里只剩 ``make_stdio_forgiving() → parse_args() → args.func(args)``。

唯一对外暴露的符号就是 :func:`main`（外加两个可测的构造器）。**不 re-export
实现细节**：``from codewiki.pipeline.analyze import _analyze`` 这类转发会让
``monkeypatch.setattr(cli, "render_all", ...)`` 打在影子上，测试静默失去牙齿。
"""

from __future__ import annotations

from .parser import build_parser
from .stdio import make_stdio_forgiving

__all__ = ["build_parser", "main", "make_stdio_forgiving"]


def main(argv: list[str] | None = None) -> int:
    make_stdio_forgiving()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
