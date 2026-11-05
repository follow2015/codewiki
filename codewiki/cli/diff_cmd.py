"""``codewiki diff``：变更影响分析（两个快照，或一个 git 范围）。

git 范围走的是 ``ai.tools.run_shell``（**沙箱**里的只读白名单命令），不是自己
拼 subprocess —— "子进程只有一个入口"这条约束对 CLI 自己的命令同样成立。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from codewiki.graph.analysis import diff_packets, impact_of
from codewiki.graph.history import default_path, read

from .context import load_packet_for_cli


def diff(args: argparse.Namespace) -> int:
    if getattr(args, "architecture", False):
        return _diff_architecture(args)
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


def _diff_architecture(args: argparse.Namespace) -> int:
    """架构级变化：比 history.jsonl 的最后两行。

    只比**结构计数**，不比"哪些文件改了"——后者是 ``--base/--head`` 的活。这个视图
    回答的是"这一轮让架构变胖了还是变瘦了"，所以六项计数 + 层增删 + 规则违例。
    """
    path = Path(args.history) if getattr(args, "history", None) else default_path(args.src)
    entries, skipped = read(path)
    if skipped:
        print(f"warning: {skipped} 行历史无法解析，已跳过（{path}）", file=sys.stderr)
    if not entries:
        print(
            f"error: 没有架构历史（{path}）：先跑一次 analyze，或用 --history 指定文件",
            file=sys.stderr,
        )
        return 2
    if len(entries) < 2:
        print(f"只有 1 条架构历史（{entries[0].generated_at}），没有可比较的上一轮。")
        print("再跑一次 analyze 之后就有趋势可看了。")
        return 0

    previous, latest = entries[-2], entries[-1]
    print(
        f"架构变化：{previous.generated_at}（{_commit(previous.repo_commit)}）"
        f" → {latest.generated_at}（{_commit(latest.repo_commit)}）"
    )
    rows = (
        ("文件", "files"),
        ("模块", "modules"),
        ("层间依赖", "layer_edges"),
        ("层", "layers"),
        ("层环", "layer_cycles"),
        ("模块环", "module_cycles"),
    )
    changed = 0
    for label, key in rows:
        before, after = previous.counts.get(key, 0), latest.counts.get(key, 0)
        changed += before != after
        print(f"  {label:<10}{_pair(before, after)}")
    for label, mapping in (("诊断", "diagnostics"), ("规则违例", "violations")):
        before = getattr(previous, mapping)
        after = getattr(latest, mapping)
        text = " / ".join(
            f"{level} {before.get(level, 0)} → {after.get(level, 0)}"
            if before.get(level, 0) == after.get(level, 0)
            else f"{level} {before.get(level, 0)} → {after.get(level, 0)}  "
            f"({after.get(level, 0) - before.get(level, 0):+d})"
            for level in sorted(set(before) | set(after))
        )
        changed += before != after
        print(f"  {label:<10}{text}")

    added = sorted(set(latest.layer_names) - set(previous.layer_names))
    removed = sorted(set(previous.layer_names) - set(latest.layer_names))
    if added or removed:
        changed += 1
        print(f"新增层：{'、'.join(added) if added else '（无）'}")
        print(f"移除层：{'、'.join(removed) if removed else '（无）'}")

    print("结论：架构未变（结构计数与上一轮相同）" if not changed else "结论：架构有变化（见上）")
    return 0


def _pair(before: int, after: int) -> str:
    if before == after:
        return f"{before}"
    return f"{before} → {after}  ({after - before:+d})"


def _commit(sha: str | None) -> str:
    return (sha or "未知提交")[:8]


def _changed_modules_from_git(rev_range: str, *, repo_root: Path):
    """用**沙箱**跑 git 拿改动文件（不自己拼命令，也不给 AI 开口子）。"""
    from codewiki.ai.tools import run_shell

    output = run_shell(f"git diff --name-only {rev_range}", repo_root=repo_root.resolve())
    return [f"mod::{line.strip()}" for line in output.splitlines() if line.strip()]


def _short(node_id: str) -> str:
    return node_id.removeprefix("mod::").removeprefix("layer::")
