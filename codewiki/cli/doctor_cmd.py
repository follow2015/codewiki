"""``codewiki doctor``：环境自检。

探测逻辑在同目录的 :mod:`codewiki.cli.doctor` 里（它要起子进程、读 Node 版本），
这里只负责把报告打出来并决定退出码 —— doctor **只报告不阻断**，所以恒为 0。
"""

from __future__ import annotations

import argparse

from .doctor import render_report, run_checks


def doctor(args: argparse.Namespace) -> int:
    print(render_report(run_checks(out_dir=args.dir, probe_ai=args.probe)), end="")
    return 0
