"""依赖环图（§4.2「依赖环图」）。

要求：每个 SCC 单独出图，环上的边**标红加粗**，并附断环建议（基于"被依赖最多
的节点"给出候选下沉层）。

断环建议为什么选"环内入度最大"的节点：环之所以成环，是因为边上每个节点都在
等别人；把**被依赖最多**的那个下沉到更底层，其余节点对它的依赖就变成"上层依赖
下层"的合法方向，环自然断开。这是启发式，不是证明 —— 所以它写进注释，
不写进诊断结论（不冒充算法保证）。
"""

from __future__ import annotations

from codewiki.graph.analysis import Cycle
from codewiki.render.mermaid_macro import escape_label, node_id


def render_cycle_diagram(cycle: Cycle, *, index: int = 1) -> str:
    """渲染单个环的 Mermaid（flowchart LR）。"""
    lines = ["flowchart LR"]
    labels = {member: node_id(member) for member in cycle.members}

    for member in cycle.members:
        name = member.removeprefix("mod::").removeprefix("layer::")
        lines.append(f'  {labels[member]}["{escape_label(name)}"]')

    highlighted = {(u, v) for u, v in cycle.edges}
    for source, target in sorted(highlighted):
        lines.append(f"  {labels[source]} ==> {labels[target]}")

    for member in cycle.members:
        lines.append(f"  style {labels[member]} fill:#FFCDD2,stroke:#C62828,stroke-width:2px")

    candidate = _break_candidate(cycle)
    if candidate:
        name = candidate.removeprefix("mod::").removeprefix("layer::")
        lines.append(f"  %% 断环建议：优先下沉 `{name}`（环内被依赖最多），使其余依赖变为上层→下层")
    return "\n".join(lines) + "\n"


def _break_candidate(cycle: Cycle) -> str | None:
    """环内入度最大的节点（没有边信息时退化为 None，不硬猜）。"""
    if not cycle.edges:
        return None
    indegree: dict[str, int] = {}
    for source, target in cycle.edges:
        if source == target:
            continue
        indegree[target] = indegree.get(target, 0) + 1
    if not indegree:
        return None
    return max(sorted(indegree), key=lambda k: (indegree[k], k))


def render_all_cycles(cycles: list[Cycle], out_dir) -> list[str]:
    """把每个环写成一个 .mmd，返回文件名清单。"""
    from pathlib import Path

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for index, cycle in enumerate(cycles, start=1):
        name = f"cycle-{index}-{cycle.kind}.mmd"
        (out / name).write_text(render_cycle_diagram(cycle, index=index), encoding="utf-8")
        written.append(name)
    return written


__all__ = ["render_all_cycles", "render_cycle_diagram"]
