"""``codewiki diff``：变更影响分析（两个快照，或一个 git 范围）。

git 范围走的是 ``ai.tools.run_shell``（**沙箱**里的只读白名单命令），不是自己
拼 subprocess —— "子进程只有一个入口"这条约束对 CLI 自己的命令同样成立。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from codewiki.graph.analysis import diff_packets, impact_of

from .context import load_packet_for_cli


def diff(args: argparse.Namespace) -> int:
    if args.base and args.head:
        base = load_packet_for_cli(Path(args.base))
        head = load_packet_for_cli(Path(args.head))
        if base is None or head is None:
            return 2
        impact = diff_packets(base, head)
    else:
        packet = load_packet_for_cli(Path(args.input))
        if packet is None:
            return 2
        if not args.range:
            print("error: 需要 git 范围（如 v1.2.0..HEAD）或 --base/--head", file=sys.stderr)
            return 2
        changed = _changed_modules_from_git(args.range, repo_root=Path(args.src or "."))
        impact = impact_of(packet, changed)

    print(f"改动 {len(impact.changed)} 个模块 / 新增 {len(impact.added)} / 删除 {len(impact.removed)}")
    for node_id in impact.changed[:20]:
        print(f"  改动：{_short(node_id)}")
    print(f"直接影响 {len(impact.direct)} 个、间接影响 {len(impact.indirect)} 个（影响半径 {impact.radius}）")
    for node_id in impact.direct[:20]:
        print(f"  直接：{_short(node_id)}")
    for node_id in impact.indirect[:20]:
        print(f"  间接：{_short(node_id)}")
    return 0


def _changed_modules_from_git(rev_range: str, *, repo_root: Path):
    """用**沙箱**跑 git 拿改动文件（不自己拼命令，也不给 AI 开口子）。"""
    from codewiki.ai.tools import run_shell

    output = run_shell(f"git diff --name-only {rev_range}", repo_root=repo_root.resolve())
    return [f"mod::{line.strip()}" for line in output.splitlines() if line.strip()]


def _short(node_id: str) -> str:
    return node_id.removeprefix("mod::").removeprefix("layer::")
