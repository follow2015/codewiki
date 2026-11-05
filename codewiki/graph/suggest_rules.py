"""``rules --suggest``：规则草案（**纯静态**，不调模型）。

新用户打开一个陌生仓库，五分钟内要能拿到一组合乎现状的规则，而不是从空文件开始。
现有事实已经够用：层间依赖矩阵 + 层内环。三档策略（路线图 1.2）：

| 该方向现存依赖 | 生成什么 | 理由 |
|---|---|---|
| 0 条 | ``forbid-dependency`` + ``error`` | 现在没人违反，立成硬约束等于白赚一道闸 |
| 1–3 条 | ``forbid-dependency`` + ``warning`` | 可见、可讨论，但不阻断别人 |
| 更多 | **不出规则**，列为「待重构目标」 | 几十条依赖不是配置能解决的；出一条必红规则只会被塞进基线永久豁免 |

两条本实现的取舍，都写在这里而不是只写在提交说明里：

1. **只为"至少一个方向有依赖"的层对出规则**。字面执行"0 条 ⇒ error"的话，15 层就是
   210 条规则，绝大多数在两个从不相往来的层之间 —— 那描述的是目录树，不是架构，
   而且正是"规则过严导致被整体无视"的起点。收窄之后，0 条的那个方向恰好是**回边守卫**：
   A→B 已经存在，锁住 B→A。
2. **环只出到"层内"**。``forbid-cycle`` 的作用域是单个选择器，层与层之间的环（A⇄B）
   根本表达不了 —— 与其造一条永远命不中的规则，不如把它作为事实写进草案（人能读懂，
   配置读不懂就别假装能读）。

"现存多少条"这个数字用**同一个求值器**量（:func:`codewiki.graph.rules.evaluate_rules`），
不另写一套数边的口径：两套口径迟早漂移，而漂移后"草案说 0 条"与"check 报 3 条"
会同时为真。代价是每个候选层对跑一次求值，换来的是"草案自洽"这件事不需要靠人复核。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

import networkx as nx

from codewiki.config import RuleSpec
from codewiki.graph.rules import evaluate_rules
from codewiki.models import NodeType
from codewiki.schemas import DataPacket

SUGGESTED_RULES_FILENAME = "rules.suggest.toml"

ERROR_MAX_VIOLATIONS = 0
WARNING_MAX_VIOLATIONS = 3


@dataclass(frozen=True)
class RuleSuggestion:
    """一条草案规则 + 它对应的现存违例数（同一次求值的产物）。"""

    rule: RuleSpec
    violations: int


@dataclass(frozen=True)
class RefactorTarget:
    """依赖太多、出规则也没人守的层对（层名，不是 id）。"""

    from_layer: str
    to_layer: str
    violations: int


@dataclass
class RulesDraft:
    suggestions: list[RuleSuggestion] = field(default_factory=list)
    targets: list[RefactorTarget] = field(default_factory=list)
    cycles: list[tuple[str, ...]] = field(default_factory=list)
    unrelated_pairs: int = 0


def suggest_rules(packet: DataPacket) -> RulesDraft:
    """数据包 → 规则草案。确定性：同一份数据包两次跑出逐字节相同的文件。

    档位由**求值器的结果**决定，不是由数边的数字决定：先按粗略计数给一个候选
    严重度，再用同一次求值得到的真实违例数定档，两者不一致时以求值器为准
    （并顺手把规则重建成正确的严重度）。这样"草案说 0 条"与 "check 报 N 条"
    不可能同时为真。
    """
    names = _layer_names(packet)
    counts = dependency_counts(packet, names)
    cyclic_group = _cyclic_groups(counts)

    draft = RulesDraft()
    ordered_pairs = len(names) * (len(names) - 1)
    related = {pair for pair in counts} | {(dst, src) for src, dst in counts}
    related = {(src, dst) for src, dst in related if src != dst}
    draft.unrelated_pairs = max(ordered_pairs - len(related), 0)

    used_ids: dict[str, int] = {}
    for src, dst in sorted(related, key=lambda pair: (names[pair[0]], names[pair[1]])):
        provisional = counts.get((src, dst), 0)
        severity = _tier(provisional) or "warning"
        rule = RuleSpec(
            id=_unique_id(used_ids, _dependency_rule_id(names[src], names[dst])),
            kind="forbid-dependency",
            from_=_layer_selector(names[src]),
            to=_layer_selector(names[dst]),
            severity=severity,
            reason=_reason(severity, provisional),
        )
        violations = len(evaluate_rules(packet, [rule]).violations)
        tier = _tier(violations)
        if tier is None:
            draft.targets.append(RefactorTarget(names[src], names[dst], violations))
            continue
        in_cycle = cyclic_group.get(src) is not None and cyclic_group.get(src) == cyclic_group.get(dst)
        if in_cycle and tier == "error":
            tier = "warning"
        reason = _reason(tier, violations, cyclic_with=names[dst] if in_cycle else None)
        if tier != rule.severity or reason != rule.reason:
            rule = rule.model_copy(update={"severity": tier, "reason": reason})
        draft.suggestions.append(RuleSuggestion(rule, violations))

    draft.suggestions.extend(_cycle_suggestions(packet, names, used_ids))
    draft.cycles = _layer_cycles(counts, names)
    draft.targets.sort(key=lambda target: (-target.violations, target.from_layer, target.to_layer))
    return draft


def _tier(violations: int) -> str | None:
    """现存违例数 → 严重度；``None`` 表示"这个数字不该出规则"。"""
    if violations <= ERROR_MAX_VIOLATIONS:
        return "error"
    if violations <= WARNING_MAX_VIOLATIONS:
        return "warning"
    return None


def _layer_names(packet: DataPacket) -> dict[str, str]:
    """只认 ``packet.layers`` 里声明过的层：未分层不是"层"，不该有规则。"""
    return {layer.id: layer.name for layer in packet.layers}


def _layer_selector(name: str) -> str:
    """层名 → 规则里的选择器写法（``layer:<name>``）。"""
    return f"layer:{name}"


def dependency_counts(packet: DataPacket, names: dict[str, str]) -> dict[tuple[str, str], int]:
    """层对 → 现存 ``IMPORTS`` 条数（符号端点先升到模块）。

    与 :func:`codewiki.graph.rules.evaluate_rules` 的口径刻意相同：``IMPORTS``、
    非自环、两端都成模块、两端都在已声明的层里。**仅类型依赖也算**：规则默认
    ``type_only = "warn"`` 时它们仍会被报出来（只是不进退出码），把"现存 0 条"
    说成干净就名不副实。这条口径差异由 :func:`suggest_rules` 里那次"用真求值器
    再量一遍"兜住。
    """
    index = {node.id: node for node in packet.nodes}

    def module_of(node_id: str):  # noqa: ANN202
        node = index.get(node_id)
        if node is None:
            return None
        if node.type == NodeType.MODULE:
            return node
        return index.get(node.module_id) if node.module_id else None

    counts: dict[tuple[str, str], int] = {}
    for edge in packet.edges:
        if str(edge.type) != "IMPORTS":
            continue
        src = module_of(edge.source)
        dst = module_of(edge.target)
        if src is None or dst is None or src.id == dst.id:
            continue
        if src.layer_id not in names or dst.layer_id not in names:
            continue
        if src.layer_id == dst.layer_id:
            continue
        counts[(src.layer_id, dst.layer_id)] = counts.get((src.layer_id, dst.layer_id), 0) + 1
    return counts


def _cycle_suggestions(
    packet: DataPacket, names: dict[str, str], used_ids: dict[str, int]
) -> list[RuleSuggestion]:
    """层内模块环 → ``forbid-cycle`` + ``warning``（单个层的作用域能表达的那种环）。"""
    per_layer: dict[str, int] = {}
    for node in packet.nodes:
        if node.type == NodeType.MODULE and node.layer_id in names:
            per_layer[node.layer_id] = per_layer.get(node.layer_id, 0) + 1

    out: list[RuleSuggestion] = []
    for layer_id in sorted(per_layer, key=lambda lid: names[lid]):
        if per_layer[layer_id] < 2:
            continue
        rule = RuleSpec(
            id=_unique_id(used_ids, f"no-cycle-in-{_slug(names[layer_id])}"),
            kind="forbid-cycle",
            scope=_layer_selector(names[layer_id]),
            severity="warning",
            reason="层内存在模块级环：拆环是设计工作，先把它标出来",
        )
        report = evaluate_rules(packet, [rule])
        if report.violations:
            out.append(RuleSuggestion(rule, len(report.violations)))
    return out


def _layer_cycles(counts: dict[tuple[str, str], int], names: dict[str, str]) -> list[tuple[str, ...]]:
    graph = nx.DiGraph()
    graph.add_edges_from(counts)
    cycles = [
        tuple(sorted(names[node] for node in members))
        for members in nx.strongly_connected_components(graph)
        if len(members) > 1
    ]
    return sorted(cycles)


def _cyclic_groups(counts: dict[tuple[str, str], int]) -> dict[str, int]:
    """层 → 所属强连通分量编号（只登记成环的分量，即成员 ≥ 2 的）。

    只认"直接双向"是不够的：甲→乙→丙→甲 里，甲→丙 的直接依赖是 0，
    但这两层明明绕一圈就能互相到达 —— 给它们出 error 规则等于假装这是
    "免费收益"，而真相是"环里的每一步都得人来裁"。
    """
    graph = nx.DiGraph()
    graph.add_edges_from(counts)
    groups: dict[str, int] = {}
    for index, members in enumerate(nx.strongly_connected_components(graph)):
        if len(members) > 1:
            for node in members:
                groups[node] = index
    return groups


def _slug(text: str) -> str:
    """规则 id 里的人话部分：``app.services`` → ``app-services``（保留中文）。"""
    slug = re.sub(r"[^\w-]+", "-", text.strip()).strip("-").lower()
    return re.sub(r"-{2,}", "-", slug) or "layer"


def _dependency_rule_id(from_name: str, to_name: str) -> str:
    return f"no-{_slug(from_name)}-to-{_slug(to_name)}"


def _unique_id(used: dict[str, int], candidate: str) -> str:
    """同 slug 的层名（``a.b`` 与 ``a-b``）会撞车；规则 id 重复会让配置加载直接失败。"""
    used[candidate] = used.get(candidate, 0) + 1
    return candidate if used[candidate] == 1 else f"{candidate}-{used[candidate]}"


def _reason(severity: str, violations: int, *, cyclic_with: str | None = None) -> str:
    if cyclic_with:
        return (
            f"与 {cyclic_with} 同处一个层环：拆环是设计工作，只给 warning、不阻断"
            f"（现存 {violations} 条直接依赖）"
        )
    if severity == "error":
        return "现存 0 条依赖：这个方向现在是干净的，锁住它（防一步回边成环）"
    return f"现存 {violations} 条依赖：先让它可见、可讨论，暂不阻断"


def render_suggested_rules(draft: RulesDraft, *, title: str = "codewiki") -> str:
    """渲染草案文件（纯 TOML：只有 ``[[rules]]`` 与注释，可直接被 merge-rules 读回）。"""
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    error = sum(1 for item in draft.suggestions if item.rule.severity == "error")
    lines = [
        "# codewiki 规则建议 —— 纯静态：层间依赖矩阵 + 层内环，不调模型",
        f"# 项目：{title}",
        f"# 生成时间：{now}",
        "#",
        "# 三档策略（每条都能用数据包复核）：",
        '#   * 该方向现存 0 条依赖 ⇒ severity = "error"：现在没人违反，立成硬约束等于白赚一道闸',
        '#   * 现存 1–3 条       ⇒ severity = "warning"：可见、可讨论，但不阻断别人',
        "#   * 更多              ⇒ 不出规则，见文件末尾「待重构目标」：几十条依赖不是配置能解决的",
        "#",
        "# 只为「至少一个方向有依赖」的层对出规则：两个从不相往来的层之间写规则，",
        f"# 描述的是目录树不是架构。本轮因此跳过 {draft.unrelated_pairs} 个这样的层对。",
        "#",
        "# 用法：挑取合意的段落，执行 `codewiki merge-rules <本文件>`（加 --dry-run 先看计划）。",
        "# 注意：merge-rules 不会覆盖同名规则；同名但内容不同 ⇒ 退出 1，要求人工裁决。",
        "",
    ]
    if not draft.suggestions:
        lines.append("# （没有可用的层间依赖事实，未产出规则）")
    else:
        lines.append(
            f"# 共 {len(draft.suggestions)} 条：error {error} / warning {len(draft.suggestions) - error}"
        )
        lines.append("")
        for item in draft.suggestions:
            lines.extend(_render_rule(item))
            lines.append("")

    if draft.targets:
        lines.append("# ── 待重构目标（依赖太多，不出规则）─────────────────────────────")
        for target in draft.targets:
            lines.append(
                f"# {target.from_layer} → {target.to_layer}：{target.violations} 条现存依赖"
                " —— 先拆依赖，再谈规则"
            )
        lines.append("")
    if draft.cycles:
        lines.append("# ── 层间环（forbid-cycle 只作用于单个层，表达不了层与层之间的环）──")
        for members in draft.cycles:
            lines.append(f"# {' ⇄ '.join(members)}：互相依赖，分层在此不清晰")
        lines.append("")
    return "\n".join(lines) + "\n"


def _render_rule(item: RuleSuggestion) -> list[str]:
    rule = item.rule
    if rule.kind == "forbid-cycle":
        label = f"层内环 {(rule.scope or '').removeprefix('layer:')}"
    else:
        label = f"{(rule.from_ or '').removeprefix('layer:')} → {(rule.to or '').removeprefix('layer:')}"
    lines = [
        "# source: suggested",
        f"# 现存违例 {item.violations} 条（{label}）",
        "[[rules]]",
        f'id = "{_escape(rule.id)}"',
        f'kind = "{rule.kind}"',
    ]
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
    return lines


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


__all__ = [
    "ERROR_MAX_VIOLATIONS",
    "SUGGESTED_RULES_FILENAME",
    "WARNING_MAX_VIOLATIONS",
    "RefactorTarget",
    "RuleSuggestion",
    "RulesDraft",
    "dependency_counts",
    "render_suggested_rules",
    "suggest_rules",
]
