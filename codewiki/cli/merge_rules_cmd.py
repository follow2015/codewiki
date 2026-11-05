"""``codewiki merge-rules``：把规则草案合并进 ``codewiki.toml``。

有冲突（同名不同内容）时退出码 1：合并没有发生，需要人看一眼哪一份才是想要的。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from codewiki.config import CONFIG_FILENAME
from codewiki.graph.merge_rules import merge_rules as _merge_rules
from codewiki.graph.merge_rules import render_rule_merge_report


def merge_rules(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve() if args.config else Path.cwd() / CONFIG_FILENAME
    outcome = _merge_rules(config_path, Path(args.file).resolve(), dry_run=args.dry_run)
    print(render_rule_merge_report(outcome))
    return 1 if outcome.conflicts else 0
