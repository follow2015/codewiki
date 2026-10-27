"""``codewiki merge-annotations``：把 AI 建议合并进 ``codewiki.toml``。

冲突时退出码 1：这不是"运行失败"，而是"需要人看一眼"的信号 —— 脚本化用法
要靠它决定停不停。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from codewiki.ai.merge import merge_annotations as _merge
from codewiki.ai.merge import render_merge_report
from codewiki.config import CONFIG_FILENAME


def merge_annotations(args: argparse.Namespace) -> int:
    suggest_path = Path(args.file).resolve()
    if not suggest_path.is_file():
        print(f"error: 建议文件不存在: {suggest_path}", file=sys.stderr)
        return 2
    config_path = Path(args.config).resolve() if args.config else Path.cwd() / CONFIG_FILENAME

    outcome = _merge(config_path, suggest_path, force=args.force, dry_run=args.dry_run)
    print(render_merge_report(outcome))
    return 1 if outcome.conflicts else 0
