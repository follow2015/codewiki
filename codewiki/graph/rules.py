"""架构规则求值（阶段一 1.1）：数据包 + 规则 → 违例。

设计上的四条硬约束，每条都是为了"规则不能安静地失效"：

1. **层归属只认 ``Node.layer_id``**：``builder`` 已经算过一遍。这里再按 ``paths``
   前缀匹配一次就是第二套口径 —— 两套口径迟早漂移，而漂移后没人知道该信哪个。
2. **端点先升到模块**：``IMPORTS`` 连模块节点，``CALLS`` 连**符号**节点。不先经
   ``Node.module_id`` 上升，``CALLS`` 会整片落进"未分层"，于是规则恒不命中 ——
   这种"恒假"比报错难发现得多。
3. **默认只看 ``IMPORTS``**：Python 里调用必然先 import，两条都算就是"一处违规
   报两次"。要看绕经 re-export 的调用，规则里显式写 ``edges = ["IMPORTS","CALLS"]``。
4. **违例身份不含行号**：身份是 ``(rule_id, 源模块, 宿模块, 边类型)``，``where``
   只用于展示。行号会随无关改动漂移，拿它当身份会让基线天天失效。

另外两条与"诚实的输出"有关：

- ``evidence`` 为空时 ``where`` 降级为模块路径，并在 ``message`` 里注明"仅模块级"，
  **不编行号**；
- 规则指向的层 / 目录 / 模块在数据包里一个都没命中时，记一条 ``problems`` ——
  此时"零违例"其实是"规则没生效"，把它当成绿灯是最危险的一种误判。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import networkx as nx

from codewiki.config import RuleSpec
from codewiki.models import NodeType
from codewiki.schemas import DataPacket, Diagnostic, Edge, Node

CYCLE_EDGE_TYPE = "CYCLE"

RULE_VIOLATION = "RULE_VIOLATION"
RULE_NOT_EFFECTIVE = "RULE_NOT_EFFECTIVE"
RULES_EVALUATED = "RULES_EVALUATED"


@dataclass(frozen=True)
class Violation:
    """一条违例。

    身份 = ``(rule_id, src, dst, edge_type)``；``where`` 只是展示用的位置，
    **不参与身份**（见模块文档第 4 条）。
    """

    rule_id: str
    severity: str
    src: str
    dst: str
    edge_type: str
    where: str
    message: str
    type_only: bool = False

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return (self.rule_id, self.src, self.dst, self.edge_type)


@dataclass
class RuleReport:
    """求值结果：违例、被忽略的仅类型违例计数、以及"规则没生效"的问题。"""

    violations: list[Violation] = field(default_factory=list)
    ignored_type_only: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return sum(1 for violation in self.violations if violation.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for violation in self.violations if violation.severity == "warning")


def evaluate_rules(packet: DataPacket, rules: Sequence[RuleSpec]) -> RuleReport:
    """按规则扫描数据包。纯函数：不读盘、不调模型、不修改 ``packet``。"""
    report = RuleReport()
    if not rules:
        return report

    index = {node.id: node for node in packet.nodes}
    available_layers = {node.layer_id for node in packet.nodes if node.layer_id}

    for rule in rules:
        if rule.kind == "forbid-cycle":
            _check_cycle_rule(packet, rule, index, available_layers, report)
        else:
            _check_dependency_rule(packet, rule, index, available_layers, report)

    report.violations.sort(key=lambda v: (v.rule_id, v.src, v.dst, v.edge_type))
    return report


def _selector(spec: str) -> Callable[[Node], bool]:
    """``layer:<名>`` / ``dir:<前缀>`` / ``module:<路径>`` → 模块节点判定函数。

    前缀合法性在配置加载时已校验；这里只按前缀分派，不做二次猜测。
    """
    prefix, _, body = spec.partition(":")
    if prefix == "layer":
        layer_id = f"layer::{body}"
        return lambda node: node.layer_id == layer_id
    if prefix == "dir":
        root = body.rstrip("/")
        return lambda node: node.path == root or (node.path or "").startswith(root + "/")
    return lambda node: node.path == body


def _target_label(spec: str) -> str:
    prefix, _, body = spec.partition(":")
    return {"layer": "层", "dir": "目录", "module": "模块"}.get(prefix, "目标") + f" {body}"


def _matched(spec: str, packet: DataPacket) -> int:
    selector = _selector(spec)
    return sum(1 for node in packet.nodes if node.type == NodeType.MODULE and selector(node))


def _require_match(rule: RuleSpec, spec: str, packet: DataPacket, report: RuleReport) -> bool:
    """规则目标在数据包里必须至少命中一个模块，否则记问题并跳过这条规则。

    "命中 0 个"最常见的两个成因：层名改了没重跑 analyze、``dir:`` 前缀写错。
    两种情况下规则都是恒不命中 —— 报出来，而不是给出一片干净的绿灯。
    """
    if _matched(spec, packet):
        return True
    report.problems.append(
        f"规则 {rule.id!r} 的 {_target_label(spec)} 在数据包里没有命中任何模块"
        "（层名/路径写错，或产物由旧配置生成）—— 这条规则本轮不会生效"
    )
    return False


def _module_node(node_id: str, index: dict[str, Node]) -> Node | None:
    """任意节点 id → 它所属的模块节点（符号经 ``module_id`` 上升）。"""
    node = index.get(node_id)
    if node is None:
        return None
    if node.type == NodeType.MODULE:
        return node
    return index.get(node.module_id) if node.module_id else None


def _check_dependency_rule(
    packet: DataPacket,
    rule: RuleSpec,
    index: dict[str, Node],
    available_layers: set[str],
    report: RuleReport,
) -> None:
    if not _layer_exists(rule.from_, available_layers, rule, packet, report):
        return
    if not _layer_exists(rule.to or "", available_layers, rule, packet, report):
        return
    if not _require_match(rule, rule.from_ or "", packet, report):
        return
    if not _require_match(rule, rule.to or "", packet, report):
        return

    source_ok = _selector(rule.from_ or "")
    target_ok = _selector(rule.to or "")
    wanted = set(rule.edges)

    for edge in packet.edges:
        if str(edge.type) not in wanted:
            continue
        src = _module_node(edge.source, index)
        dst = _module_node(edge.target, index)
        if src is None or dst is None or src.id == dst.id:
            continue
        if not (source_ok(src) and target_ok(dst)):
            continue
        if edge.type_only and rule.type_only == "ignore":
            report.ignored_type_only[rule.id] = report.ignored_type_only.get(rule.id, 0) + 1
            continue
        report.violations.append(_dependency_violation(rule, edge, src, dst))


def _layer_exists(
    spec: str,
    available_layers: set[str],
    rule: RuleSpec,
    packet: DataPacket,
    report: RuleReport,
) -> bool:
    """``layer:`` 目标在数据包里必须真出现过（其他前缀交给 :func:`_require_match`）。"""
    if not spec.startswith("layer:"):
        return True
    layer_id = f"layer::{spec[len('layer:') :]}"
    if layer_id in available_layers:
        return True
    known = "、".join(sorted(name[len("layer::") :] for name in available_layers)) or "（无）"
    report.problems.append(
        f"规则 {rule.id!r} 引用的层 {spec[len('layer:') :]!r} 不在数据包里"
        f"（现有层：{known}）—— 多半是改了分层没重跑 analyze，这条规则本轮不会生效"
    )
    return False


def _dependency_violation(rule: RuleSpec, edge: Edge, src: Node, dst: Node) -> Violation:
    notes: list[str] = []
    if edge.type_only:
        notes.append("仅类型依赖")
    if not edge.evidence:
        notes.append("仅模块级：该边没有位置信息")
    detail = str(edge.type) + " 边" + (f"，{'；'.join(notes)}" if notes else "")
    reason = f"；{rule.reason}" if rule.reason else ""
    severity = "warning" if (edge.type_only and rule.type_only == "warn") else rule.severity
    return Violation(
        rule_id=rule.id,
        severity=severity,
        src=src.path or src.id,
        dst=dst.path or dst.id,
        edge_type=str(edge.type),
        where=edge.evidence or (src.path or src.id),
        message=f"{src.path} 依赖了 {dst.path}（{detail}），违反规则 {rule.id}{reason}",
        type_only=edge.type_only,
    )


def _check_cycle_rule(
    packet: DataPacket,
    rule: RuleSpec,
    index: dict[str, Node],
    available_layers: set[str],
    report: RuleReport,
) -> None:
    """scope 内找模块级环。

    用 ``IMPORTS``（且非 ``type_only``）建图：环是"运行时互相加载"，类型注解不构成
    运行时环 —— 与 ``analysis.find_module_cycles`` 同口径。``edges`` 字段在这里无效
    （配置期已拒绝显式写法）。
    """
    if not _layer_exists(rule.scope or "", available_layers, rule, packet, report):
        return
    if not _require_match(rule, rule.scope or "", packet, report):
        return

    scope_ok = _selector(rule.scope or "")
    inside: dict[str, Node] = {
        node.id: node
        for node in packet.nodes
        if node.type == NodeType.MODULE and node.path and scope_ok(node)
    }
    graph = nx.DiGraph()
    graph.add_nodes_from(sorted(inside))
    evidence: dict[tuple[str, str], str | None] = {}
    for edge in packet.edges:
        if str(edge.type) != "IMPORTS" or edge.type_only:
            continue
        src = _module_node(edge.source, index)
        dst = _module_node(edge.target, index)
        if src is None or dst is None or src.id == dst.id:
            continue
        if src.id in inside and dst.id in inside:
            graph.add_edge(src.id, dst.id)
            evidence.setdefault((src.id, dst.id), edge.evidence)

    for members in nx.strongly_connected_components(graph):
        if len(members) < 2:
            continue
        ordered = sorted(members, key=lambda mid: inside[mid].path or mid)
        first, second = inside[ordered[0]], inside[ordered[1]]
        where = evidence.get((ordered[0], ordered[1])) or first.path or first.id
        preview = " → ".join(inside[mid].path or mid for mid in ordered[:4])
        if len(ordered) > 4:
            preview += f" → …（共 {len(ordered)} 个模块）"
        report.violations.append(
            Violation(
                rule_id=rule.id,
                severity=rule.severity,
                src=first.path or first.id,
                dst=second.path or second.id,
                edge_type=CYCLE_EDGE_TYPE,
                where=where,
                message=f"{len(ordered)} 个模块互相 import 成环：{preview}（违反规则 {rule.id}）"
                + (f"；{rule.reason}" if rule.reason else ""),
            )
        )


def to_diagnostics(report: RuleReport, *, declared: int) -> list[Diagnostic]:
    """求值结果 → 数据包诊断（``analyze`` 用它把违例写进产物）。

    第一条永远是 ``RULES_EVALUATED``（info）：**"零违例"与"没检查"在产物里必须
    长得不一样**。少了这条，下游只能凭"有没有 RULE_VIOLATION"猜本轮到底跑没跑
    规则，而这两种情形在站点上都是"一片安静"。

    ``declared`` 由调用方给（配置里写了几条规则）：``RuleReport`` 只记录求值结果，
    凭空从里面数规则数会漏掉"两端都没命中、直接跳过"的那些。
    """
    out = [
        Diagnostic(level="info", code=RULES_EVALUATED, message=_evaluated_message(report, declared)),
    ]
    for violation in report.violations:
        out.append(
            Diagnostic(
                level=violation.severity,
                code=RULE_VIOLATION,
                message=violation.message,
                where=violation.where,
                rule_id=violation.rule_id,
            )
        )
    for problem in report.problems:
        out.append(Diagnostic(level="warning", code=RULE_NOT_EFFECTIVE, message=problem))
    return out


def _evaluated_message(report: RuleReport, declared: int) -> str:
    text = f"已按 {declared} 条规则检查：error {report.error_count} / warning {report.warning_count}"
    if report.ignored_type_only:
        skipped = sum(report.ignored_type_only.values())
        text += f"；另有 {skipped} 条仅类型依赖按 type_only 跳过"
    if report.problems:
        text += f"；{len(report.problems)} 条规则本轮没有生效"
    return text


__all__ = [
    "CYCLE_EDGE_TYPE",
    "RULE_NOT_EFFECTIVE",
    "RULE_VIOLATION",
    "RULES_EVALUATED",
    "RuleReport",
    "Violation",
    "evaluate_rules",
    "to_diagnostics",
]
