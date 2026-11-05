"""``ARCHITECTURE.md``：一页架构报告（贴 PR 描述或架构评审用）。

**纯静态，不调模型、不重算图**：层表从 ``packet.nodes`` 分组，环 / 核心节点 / 规则
违例全部从 ``packet.diagnostics`` 过滤。站点也是从这两处取 —— 与站点同源，才不会
出现"报告说 3 个环、站点说 2 个"。[roadmap §1.5]

内容只由数据包决定（不写耗时、不写缓存命中）：报告是归档物，混进运行历史会让
"同一份输入两次生成相同"作废。
"""

from __future__ import annotations

from pathlib import Path

from codewiki.graph.history import HistoryEntry
from codewiki.graph.rules import RULE_NOT_EFFECTIVE, RULE_VIOLATION, RULES_EVALUATED
from codewiki.models import EdgeType, NodeType
from codewiki.render.diagnostics_md import format_diagnostic_line
from codewiki.render.i18n import t
from codewiki.render.stats import displayed_stats
from codewiki.schemas import DataPacket, Diagnostic

ARCHITECTURE_FILENAME = "ARCHITECTURE.md"

CYCLE_PREVIEW = 8
VIOLATION_PREVIEW = 5
VIOLATION_TOTAL = 15
GOD_NODE_PREVIEW = 8
DIAGNOSTIC_PREVIEW = 8

MESSAGE_CLIP = 140

TREND_ROWS = 10

_CYCLES = ("LAYER_CYCLE", "MODULE_CYCLE", "LAYER_CYCLE_TRUNCATED")
_GOD_NODE = "GOD_NODE"
_OWNED = {RULE_VIOLATION, RULE_NOT_EFFECTIVE, RULES_EVALUATED, *_CYCLES, _GOD_NODE}


def render_architecture_md(packet: DataPacket, *, history: list[HistoryEntry] | None = None) -> str:
    lines: list[str] = [f"# {t('arch.title', title=packet.meta.title)}", ""]
    lines += [f"> {_provenance(packet)}", ""]
    lines += _summary(packet)
    lines += _layers(packet)
    lines += _cycles(packet)
    lines += _violations(packet)
    lines += _god_nodes(packet)
    lines += _trend(history)
    lines += _diagnostics(packet)
    lines += _how_to_read()
    return "\n".join(lines).rstrip() + "\n"


def write_architecture_md(
    out_dir: Path, packet: DataPacket, *, history: list[HistoryEntry] | None = None
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / ARCHITECTURE_FILENAME
    target.write_text(render_architecture_md(packet, history=history), encoding="utf-8")
    return target


def _provenance(packet: DataPacket) -> str:
    commit = (packet.meta.repo_commit or "")[:8] or t("arch.unknown_commit")
    return t(
        "arch.provenance",
        tool=packet.meta.tool_version,
        at=packet.meta.generated_at,
        commit=commit,
    )


def _summary(packet: DataPacket) -> list[str]:
    lines = [
        f"## {t('arch.section_summary')}",
        "",
        f"| {t('arch.col_metric')} | {t('arch.col_count')} |",
        "|---|---|",
    ]
    lines += [f"| {t(key)} | {value} |" for key, value in displayed_stats(packet)]
    return [*lines, ""]


def _layers(packet: DataPacket) -> list[str]:
    """层表：每层模块数与出向跨层依赖数（层间依赖的"从哪出去"）。"""
    groups: dict[str, int] = {}
    for node in packet.nodes:
        if node.type == NodeType.MODULE and node.layer_id:
            groups[node.layer_id] = groups.get(node.layer_id, 0) + 1
    outgoing: dict[str, int] = {}
    for edge in packet.edges:
        if edge.type == EdgeType.LAYER_DEPENDS and not edge.type_only:
            outgoing[edge.source] = outgoing.get(edge.source, 0) + 1

    ordered = sorted(
        packet.layers, key=lambda layer: (layer.order if layer.order is not None else 10_000, layer.name)
    )
    lines = [
        f"## {t('arch.section_layers')}",
        "",
        f"| {t('arch.col_layer')} | {t('arch.col_modules')} | {t('arch.col_outgoing')} |",
        "|---|---|---|",
    ]
    for layer in ordered:
        lines.append(f"| {layer.name} | {groups.get(layer.id, 0)} | {outgoing.get(layer.id, 0)} |")
    return [*lines, ""]


def _cycles(packet: DataPacket) -> list[str]:
    cycles = [item for item in packet.diagnostics if item.code in _CYCLES]
    if not cycles:
        return []
    lines = [f"## {t('arch.section_cycles')}", "", *(_bullet(item) for item in cycles[:CYCLE_PREVIEW])]
    if len(cycles) > CYCLE_PREVIEW:
        lines += ["", t("arch.more_items", count=len(cycles) - CYCLE_PREVIEW)]
    return [*lines, ""]


def _violations(packet: DataPacket) -> list[str]:
    violations = [item for item in packet.diagnostics if item.code == RULE_VIOLATION]
    if not violations:
        return []
    groups: dict[str, list[Diagnostic]] = {}
    for item in violations:
        groups.setdefault(item.rule_id or "?", []).append(item)

    lines = [f"## {t('arch.section_violations')}", ""]
    ordered = sorted(
        groups.items(), key=lambda pair: (0 if any(d.level == "error" for d in pair[1]) else 1, pair[0])
    )
    printed = 0
    for rule_id, items in ordered:
        room = VIOLATION_TOTAL - printed
        if room <= 0:
            break
        level = "error" if any(d.level == "error" for d in items) else "warning"
        shown = items[: min(VIOLATION_PREVIEW, room)]
        printed += len(shown)
        lines += [f"### `{rule_id}`（{level}，{len(items)}）", ""]
        lines += [_bullet(item) for item in shown]
        if len(items) > len(shown):
            lines += ["", t("arch.more_violations", count=len(items) - len(shown))]
        lines.append("")

    remaining = len(violations) - printed
    if remaining > 0:
        lines += [t("arch.more_items", count=remaining), ""]

    ineffective = [item for item in packet.diagnostics if item.code == RULE_NOT_EFFECTIVE]
    if ineffective:
        lines += [f"> {t('arch.rules_incomplete')}", ""]
        lines += [f"> {item.message}" for item in ineffective]
        lines.append("")
    return lines


def _god_nodes(packet: DataPacket) -> list[str]:
    nodes = [item for item in packet.diagnostics if item.code == _GOD_NODE]
    if not nodes:
        return []
    lines = [f"## {t('arch.section_nodes')}", "", *(_bullet(item) for item in nodes[:GOD_NODE_PREVIEW])]
    if len(nodes) > GOD_NODE_PREVIEW:
        lines += ["", t("arch.more_items", count=len(nodes) - GOD_NODE_PREVIEW)]
    return [*lines, ""]


def _trend(history: list[HistoryEntry] | None) -> list[str]:
    rows = (history or [])[-TREND_ROWS:]
    if len(rows) < 2:
        return []
    header = " | ".join(
        [
            t("arch.col_time"),
            t("site.trend_files"),
            t("site.trend_modules"),
            t("site.trend_layer_edges"),
            t("site.trend_layer_cycles"),
            t("site.trend_module_cycles"),
            t("site.trend_violations"),
        ]
    )
    lines = [f"## {t('arch.section_trend')}", "", f"| {header} |", "|" + "---|" * 7]
    for row in rows:
        counts = row.counts
        lines.append(
            f"| {row.generated_at[:10]} | {counts.get('files', 0)} | {counts.get('modules', 0)} | "
            f"{counts.get('layer_edges', 0)} | {counts.get('layer_cycles', 0)} | "
            f"{counts.get('module_cycles', 0)} | {row.violations.get('error', 0)} |"
        )
    return [*lines, ""]


def _diagnostics(packet: DataPacket) -> list[str]:
    """余下诊断里**值得架构评审看的**：只列 error/warning，info 只给条数。

    与环/违例段相反：那两段"空段无信息"，这里"没有诊断"本身就是结论 —— 留白会被
    读成"没检查"。条数有上限（ipip 实测能到 700+ 行），但丢掉的条数必须写出来。

    info 不列：ipip 上 702 条里有几百条 ``CALL_AMBIGUOUS``（同名符号无法绑定），
    那是**解析质量**的细节，不是架构事实。混进一页报告会把真正该看的 error 挤掉。
    """
    rank = {"error": 0, "warning": 1}
    shown = sorted(
        (item for item in packet.diagnostics if item.code not in _OWNED and item.level in rank),
        key=lambda item: (rank[item.level], item.code),
    )
    info_count = sum(1 for item in packet.diagnostics if item.code not in _OWNED and item.level not in rank)

    lines = [f"## {t('arch.section_diagnostics')}", ""]
    if not shown and not info_count:
        lines += [t("diagnostics.empty")]
    else:
        lines += [_bullet(item) for item in shown[:DIAGNOSTIC_PREVIEW]]
        if len(shown) > DIAGNOSTIC_PREVIEW:
            lines += ["", t("arch.more_items", count=len(shown) - DIAGNOSTIC_PREVIEW)]
        if info_count:
            lines += ["", t("arch.info_only", count=info_count)]
    return [*lines, ""]


def _how_to_read() -> list[str]:
    return [f"## {t('arch.section_read')}", "", t("arch.read_hint"), ""]


def _bullet(item: Diagnostic) -> str:
    """一条诊断行；消息过长时截断（完整内容在 ``diagnostics.md``）。

    截断是必须的：模块环的成员列表实测能到 1164 字符（8 个完整模块 id），一行撑满
    屏幕宽度，一页报告立刻变成一屏横滚。

    ``mod::`` / ``layer::`` 前缀一并去掉：那是节点 id 的内部前缀，报告是给人读的
    （``app.exceptions → app.utils`` 比 ``layer::app.exceptions → layer::app.utils``
    清楚得多）。id 的权威形态在数据包里，这里不承担机器可读的职责。
    """
    message = _strip_ids(item.message)
    if len(message) > MESSAGE_CLIP:
        message = message[: MESSAGE_CLIP - 1] + "…"
    where = _strip_ids(item.where) if item.where else None
    return format_diagnostic_line(item.model_copy(update={"message": message, "where": where}))


def _strip_ids(text: str) -> str:
    return text.replace("layer::", "").replace("mod::", "")


__all__ = ["ARCHITECTURE_FILENAME", "render_architecture_md", "write_architecture_md"]
