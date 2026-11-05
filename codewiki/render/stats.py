"""首页 chips 与 ``ARCHITECTURE.md`` 共用的六个数字（单一真相源）。

两处各算一遍就是"口径不一致"的复发点：站点写 670 个模块、报告写 668，读者只会
怀疑两边都不可信。所以数字在这里出，两个渲染器只负责摆版式。

**为什么规则违例要单列**：诊断 error 会把规则违例算进去，但反过来不成立。合成
一个数的最坏后果是"规则违例涨了、解析诊断降了，总数不变"——于是这条最重要的
信号被平均数吃掉了。
"""

from __future__ import annotations

from dataclasses import dataclass

from codewiki.graph.rules import RULE_VIOLATION, RULES_EVALUATED
from codewiki.models import NodeType
from codewiki.schemas import DataPacket

STAT_KEYS = (
    "site.chip_modules",
    "site.chip_nodes",
    "site.chip_edges",
    "site.chip_layers",
    "site.chip_errors",
    "site.chip_violations",
)


@dataclass(frozen=True)
class ArchitectureStats:
    modules: int
    nodes: int
    edges: int
    layers: int
    diagnostics_error: int
    violations_error: int

    def items(self) -> list[tuple[str, int]]:
        """``(文案键, 数量)`` 六项，顺序与 :data:`STAT_KEYS` 一致。"""
        values = (
            self.modules,
            self.nodes,
            self.edges,
            self.layers,
            self.diagnostics_error,
            self.violations_error,
        )
        return list(zip(STAT_KEYS, values, strict=True))


def architecture_stats(packet: DataPacket) -> ArchitectureStats:
    errors = [d for d in packet.diagnostics if d.level == "error"]
    return ArchitectureStats(
        modules=sum(1 for node in packet.nodes if node.type == NodeType.MODULE),
        nodes=len(packet.nodes),
        edges=len(packet.edges),
        layers=len(packet.layers),
        diagnostics_error=len(errors),
        violations_error=sum(1 for item in errors if item.code == RULE_VIOLATION),
    )


def displayed_stats(packet: DataPacket) -> list[tuple[str, int]]:
    """要在界面上显示的 ``(文案键, 数量)`` —— 没跑过规则时**去掉违例那一项**。

    只看数字的话，"规则违例 0" 与"根本没配规则"长得一模一样，而前者是"检查过、
    干净"，后者是"什么都没检查"。``RULES_EVALUATED`` 就是区分二者的那条诊断
    （WP5 引入它就是为了这个）；没有它时这一项不该出现，否则等于替不存在的规则
    背书。
    """
    items = architecture_stats(packet).items()
    if any(item.code == RULES_EVALUATED for item in packet.diagnostics):
        return items
    return [pair for pair in items if pair[0] != "site.chip_violations"]


__all__ = ["STAT_KEYS", "ArchitectureStats", "architecture_stats", "displayed_stats"]
