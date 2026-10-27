"""``codewiki merge-layers``：把建议分层合并进 ``codewiki.toml``。

路径重叠时退出码 1：合并本身成功了，但"两层声明了同一批路径"需要人裁决。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from codewiki.config import CONFIG_FILENAME
from codewiki.graph.merge_layers import merge_layers as _merge_layers
from codewiki.graph.merge_layers import render_layer_merge_report


def merge_layers(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve() if args.config else Path.cwd() / CONFIG_FILENAME
    outcome = _merge_layers(config_path, Path(args.file).resolve(), dry_run=args.dry_run)
    print(render_layer_merge_report(outcome))
    return 1 if outcome.overlaps else 0
