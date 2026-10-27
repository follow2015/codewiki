"""``codewiki export``：把产物目录打成单个 Zip。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from codewiki.render.site import write_zip


def export(args: argparse.Namespace) -> int:
    out_dir = Path(args.dir).resolve()
    target = Path(args.zip_path).resolve() if args.zip_path else out_dir.with_suffix(".zip")
    if not out_dir.is_dir():
        print(f"error: 产物目录不存在: {out_dir}", file=sys.stderr)
        return 2
    path = write_zip(out_dir, target)
    print(f"已打包：{path}")
    return 0
