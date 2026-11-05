"""``merge-rules`` —— 把规则草案合并进 ``codewiki.toml``。

与 ``merge-layers`` 同一姿势（建议 → 人工审阅 → **显式**合并），三条规则：

1. **绝不覆盖人工规则**：同名且内容相同 ⇒ 跳过（重复合并是安全的）；同名但内容
   不同 ⇒ **一个字节都不写**并退出 1 —— 人工改过的严重度/理由被草案顶掉，是最难
   事后发现的那类损失（配置里看不出"刚才发生过覆盖"）。
2. **只追加**：草案里的规则排在既有规则之后，人工规则保持原位置。
3. **每条带来源注释**：``# source: suggested``，便于日后回溯哪些是算法给的。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from codewiki.config import RuleSpec

SOURCE_MARK = "# source: suggested"


@dataclass
class RuleMergeOutcome:
    added: list[RuleSpec] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    changed: bool = False


def load_suggested_rules(path: Path) -> list[RuleSpec]:
    """读草案文件里的 ``[[rules]]``（与配置同一个模型，校验规则也就同一份）。"""
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return [RuleSpec.model_validate(item) for item in raw.get("rules", [])]


def plan_merge_rules(suggested: list[RuleSpec], existing: list[RuleSpec]) -> RuleMergeOutcome:
    """算出合并计划（不写文件）。"""
    outcome = RuleMergeOutcome()
    by_id = {rule.id: rule for rule in existing}
    seen: set[str] = set()
    for rule in suggested:
        if rule.id in seen:
            outcome.conflicts.append({"id": rule.id, "fields": ["(草案内重复)"]})
            continue
        seen.add(rule.id)
        current = by_id.get(rule.id)
        if current is None:
            outcome.added.append(rule)
            continue
        diff = _diff_fields(current, rule)
        if diff:
            outcome.conflicts.append({"id": rule.id, "fields": diff})
        else:
            outcome.skipped.append(rule.id)
    return outcome


def _diff_fields(existing: RuleSpec, suggested: RuleSpec) -> list[str]:
    left, right = existing.model_dump(), suggested.model_dump()
    return sorted(key for key in left if left[key] != right[key])


def render_rule_block(rule: RuleSpec) -> str:
    """渲染一个 ``[[rules]]`` 段（带来源注释）。"""
    lines = [SOURCE_MARK, "[[rules]]", f'id = "{_escape(rule.id)}"', f'kind = "{rule.kind}"']
    if rule.from_:
        lines.append(f'from = "{_escape(rule.from_)}"')
    if rule.to:
        lines.append(f'to = "{_escape(rule.to)}"')
    if rule.scope:
        lines.append(f'scope = "{_escape(rule.scope)}"')
    if rule.edges_explicit:
        lines.append("edges = [" + ", ".join(f'"{_escape(edge)}"' for edge in rule.edges) + "]")
    lines.append(f'severity = "{rule.severity}"')
    if rule.reason:
        lines.append(f'reason = "{_escape(rule.reason)}"')
    return "\n".join(lines)


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def apply_merge_rules(config_path: Path, outcome: RuleMergeOutcome) -> bool:
    """把新规则追加到配置末尾（人工规则原样不动）。"""
    if not outcome.added:
        return False
    original = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    body = original.rstrip("\n")
    if body:
        body += "\n\n"
    body += "\n\n".join(render_rule_block(rule) for rule in outcome.added) + "\n"

    tmp = config_path.with_suffix(".toml.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(config_path)
    outcome.changed = True
    return True


def merge_rules(config_path: Path, suggest_path: Path, *, dry_run: bool = False) -> RuleMergeOutcome:
    """读草案 → 计划 → 落盘（有冲突就只计划、不落盘）。"""
    from codewiki.config import load_config

    cfg = load_config(config_path)
    suggested = load_suggested_rules(suggest_path)
    outcome = plan_merge_rules(suggested, cfg.rules)
    if dry_run or outcome.conflicts:
        return outcome
    apply_merge_rules(config_path, outcome)
    return outcome


def render_rule_merge_report(outcome: RuleMergeOutcome) -> str:
    lines = [
        f"新增 {len(outcome.added)} 条 / 跳过 {len(outcome.skipped)} 条 / 冲突 {len(outcome.conflicts)} 处"
    ]
    for item in outcome.conflicts:
        fields = "、".join(str(name) for name in item["fields"])
        lines.append(f"  ⚠ 规则「{item['id']}」与既有同名规则不同（字段：{fields}）")
        lines.append("     未写入配置：需要人工裁决后再合并")
    for name in outcome.skipped:
        lines.append(f"  ⏭ 跳过「{name}」：同名且内容相同")
    return "\n".join(lines)


__all__ = [
    "SOURCE_MARK",
    "RuleMergeOutcome",
    "apply_merge_rules",
    "load_suggested_rules",
    "merge_rules",
    "plan_merge_rules",
    "render_rule_block",
    "render_rule_merge_report",
]
