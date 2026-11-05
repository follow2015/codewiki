"""``codewiki check``：在**已有数据包**上裁决架构规则。

刻意不做 ``--src``（不内联跑 analyze）：check 的价值是"同一份产物上反复裁决"。
一旦它能自己分析，就会长成第二个 ``analyze``，两边参数与缓存迟早分叉。代价是
CI 里两步（analyze → check），换来的是可重复。

三条硬约束：

- **只读**：违例只在内存里用于输出，绝不回写数据包。会改东西的检查命令，
  没人敢在 CI 里跑。
- **必须说清裁决的是哪份快照**：输出头固定打印 ``generated_at`` / ``repo_commit``
  / ``tool_version``。拿旧产物跑出绿灯是这条链路上最容易发生的误判。
- **"没有规则" ≠ "检查通过"**：没声明规则时退出 0，但措辞要让人一眼看出
  "什么都没检查"；规则指向的层在产物里不存在（产物与配置不一致）时退出 2，
  因为这一轮裁决本身无效。
- **存量与新增分账**（``--baseline``）：基线内的违例不报明细，但要把"另有 N 条
  存量"说出来；基线里有、当前已经没有的条目提示收缩（``RULE_BASELINE_STALE``）。
  写基线是**显式**动作（``--write-baseline``），且写之前把记了多少条打出来。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from codewiki.config import CodewikiConfig, ConfigError, load_config
from codewiki.graph.rule_baseline import (
    BaselineEntry,
    BaselineError,
    BaselineSplit,
    load_baseline,
    split_by_baseline,
    write_baseline,
)
from codewiki.graph.rules import RuleReport, Violation, evaluate_rules
from codewiki.schemas import DataPacket

from .context import load_packet_for_cli

DEFAULT_MAX_PER_RULE = 20

DEFAULT_BASELINE_NAME = ".codewiki-baseline.json"


def check(args: argparse.Namespace) -> int:
    packet = load_packet_for_cli(Path(args.input))
    if packet is None:
        return 2

    try:
        config = load_config(Path(args.config))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report = evaluate_rules(packet, config.rules)
    baseline_path = Path(args.baseline) if args.baseline else None

    if args.write_baseline:
        if report.problems:
            print(
                "error: 规则没有真正生效，拒绝写入基线（先修好下面这些问题，否则记下的是一本假账）",
                file=sys.stderr,
            )
            for problem in report.problems:
                print(f"  ! {problem}", file=sys.stderr)
            return 2
        target = baseline_path or Path(DEFAULT_BASELINE_NAME)
        result = write_baseline(target, report.violations)
        print(
            f"已写入基线：{result.path}\n"
            f"  记录 {result.total} 条 / 新增 {result.added} / 已消失 {result.removed}"
        )
        return 0

    split: BaselineSplit | None = None
    if baseline_path is not None:
        try:
            split = split_by_baseline(report.violations, load_baseline(baseline_path))
        except BaselineError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    violations = split.new if split is not None else report.violations
    payload = _payload(
        packet,
        Path(args.input),
        config,
        report,
        violations,
        split,
        baseline_path=baseline_path,
        max_per_rule=args.max_per_rule,
    )

    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif not config.rules:
        _print_no_rules(packet, Path(args.input))
    else:
        _print_text(packet, Path(args.input), config, payload, max_per_rule=args.max_per_rule)

    if config.rules and report.problems:
        return 2
    return 1 if any(v.severity == "error" for v in violations) else 0


def _payload(
    packet: DataPacket,
    path: Path,
    config: CodewikiConfig,
    report: RuleReport,
    violations: list[Violation],
    split: BaselineSplit | None = None,
    *,
    baseline_path: Path | None = None,
    max_per_rule: int,
) -> dict:
    shown, truncated = _truncate(violations, max_per_rule=max_per_rule)
    return {
        "packet": {
            "path": str(path),
            "generated_at": packet.meta.generated_at,
            "repo_commit": packet.meta.repo_commit,
            "tool_version": packet.meta.tool_version,
        },
        "rules": {
            "declared": len(config.rules),
            "ids": [rule.id for rule in config.rules],
        },
        "summary": {
            "error": sum(1 for v in violations if v.severity == "error"),
            "warning": sum(1 for v in violations if v.severity == "warning"),
            "ignored_type_only": sum(report.ignored_type_only.values()),
            "baselined": split.skipped if split is not None else 0,
        },
        "violations": [_violation_json(violation) for violation in shown],
        "truncated": truncated,
        "ignored_type_only": dict(sorted(report.ignored_type_only.items())),
        "baseline": {
            "enabled": split is not None,
            "path": str(baseline_path) if baseline_path is not None else None,
            "skipped": split.skipped if split is not None else 0,
            "stale": [_entry_json(entry) for entry in (split.stale if split is not None else [])],
        },
        "problems": list(report.problems),
    }


def _entry_json(entry: BaselineEntry) -> dict:
    return {"rule_id": entry.rule_id, "src": entry.src, "dst": entry.dst, "edge": entry.edge}


def _truncate(violations: list[Violation], *, max_per_rule: int) -> tuple[list[Violation], dict[str, int]]:
    """每条规则最多列 ``max_per_rule`` 条，其余只计数。

    截断是给终端看的；**丢掉多少条必须写出来**，否则读者会以为"就这么多"。
    摘要里的 error/warning 数的是全部，不是截断后的。
    """
    per_rule: Counter[str] = Counter()
    shown: list[Violation] = []
    truncated: dict[str, int] = {}
    for violation in violations:
        per_rule[violation.rule_id] += 1
        if per_rule[violation.rule_id] <= max_per_rule:
            shown.append(violation)
        else:
            truncated[violation.rule_id] = truncated.get(violation.rule_id, 0) + 1
    return shown, truncated


def _violation_json(violation: Violation) -> dict:
    return {
        "rule_id": violation.rule_id,
        "severity": violation.severity,
        "src": violation.src,
        "dst": violation.dst,
        "edge_type": violation.edge_type,
        "where": violation.where,
        "type_only": violation.type_only,
        "message": violation.message,
    }


def _print_meta(packet: DataPacket, path: Path) -> None:
    """输出头：**这次裁决的是哪份快照**。拿旧产物跑出绿灯是这条链路上最易犯的错。"""
    meta = packet.meta
    print(f"数据包：{path}")
    print(
        "  generated_at={generated_at} · repo_commit={repo_commit} · tool_version={tool_version}".format(
            generated_at=meta.generated_at,
            repo_commit=meta.repo_commit or "-",
            tool_version=meta.tool_version,
        )
    )


def _print_no_rules(packet: DataPacket, path: Path) -> None:
    _print_meta(packet, path)
    print(
        '未声明任何规则：本次**没有任何东西被检查** —— 这不等于"架构没问题"。\n'
        "  在 codewiki.toml 里写一条 [[rules]]（示例见 codewiki.toml.example），\n"
        "  或先跑 `codewiki rules --suggest` 起草。"
    )


def _print_text(
    packet: DataPacket,
    path: Path,
    config: CodewikiConfig,
    payload: dict,
    *,
    max_per_rule: int,
) -> None:
    summary = payload["summary"]
    baseline = payload["baseline"]
    _print_meta(packet, path)
    print(f"规则：{_rule_line(config)}  违例：error {summary['error']} / warning {summary['warning']}")
    if baseline["enabled"]:
        print(f"基线：{baseline['path']}  另有 {summary['baselined']} 条存量（不报明细）")

    for violation in payload["violations"]:
        print(f"\n  [{violation['severity']}] {violation['rule_id']}  {violation['where']}")
        print(f"      {violation['message']}")

    for rule_id, rest in payload["truncated"].items():
        print(f"\n  … {rule_id} 另有 {rest} 条未列出（--max-per-rule 截断）")

    if payload["ignored_type_only"]:
        detail = "、".join(f"{rule}={count}" for rule, count in payload["ignored_type_only"].items())
        print(f'\n  仅类型依赖（type_only = "ignore"，已跳过但计入）：{detail}')

    if baseline["enabled"] and baseline["stale"]:
        _print_stale(baseline["stale"], max_per_rule=max_per_rule)

    for problem in payload["problems"]:
        print(f"\n  ! {problem}")

    if summary["error"]:
        print("\n结论：有 error 级违例（退出码 1）")
    elif summary["warning"]:
        print("\n结论：只有 warning（退出码 0）")
    elif summary["baselined"]:
        print(f"\n结论：无新增违例（{summary['baselined']} 条存量按基线跳过，退出码 0）")
    else:
        print("\n结论：未发现违例")


def _print_stale(entries: list[dict], *, max_per_rule: int) -> None:
    """基线里有、当前产物里已经没有的条目 —— 提示收缩，但不影响退出码。

    为什么不进退出码：这不是"架构出了问题"，而是"账该更新了"。让它拦 CI，
    团队唯一的出路是把基线删掉，正好毁掉基线存在的意义。
    """
    print(
        f"\n  [warning] RULE_BASELINE_STALE  基线里 {len(entries)} 条在当前产物里已不存在"
        "（诊断项，不计入上面的违例数）"
    )
    for entry in entries[:max_per_rule]:
        print(f"      {entry['rule_id']}  {entry['src']} → {entry['dst']}  ({entry['edge']})")
    if len(entries) > max_per_rule:
        print(f"      … 另有 {len(entries) - max_per_rule} 条未列出")
    print("      确认是有意消除后用 --write-baseline 收缩：虚高的基线会掩盖以后的真回归")


def _rule_line(config: CodewikiConfig) -> str:
    """一行摘要："2 条（no-a、no-b）"，让人一眼知道这次到底装了几道闸。"""
    ids = "、".join(rule.id for rule in config.rules)
    return f"{len(config.rules)} 条（{ids}）"


__all__ = ["DEFAULT_BASELINE_NAME", "DEFAULT_MAX_PER_RULE", "check"]
