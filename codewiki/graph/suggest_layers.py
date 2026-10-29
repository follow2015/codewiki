"""``suggest-layers`` —— **分层建议**（§4.6.3）。

定位四条：

1. 这是**建议**不是自动分层：产出独立文件，人工挑选后 ``merge-layers`` 合并，
   **绝不自动覆盖人工分层**；
2. **目录优先，社区发现只做补充**：目录结构是最强的分层信号，所以先按目录归组
   （:func:`codewiki.layering.directory_groups`），碎片组并入 ``other``；只有某组
   大到 ``max_group`` 以上，才用 Louvain 把它拆开。此前直接对全图跑社区发现，
   结果是多个同名的 ``root`` 层、一串单模块"层"和一屏幕的文件路径列表；
3. **带依赖方向的层序**：按跨层导入边做强连通分量压缩、再按拓扑深度排出
   ``order``（1 = 最上层，没有别的层依赖它）。成环的层共享同一个 order 并标注 ——
   环本身就是"分层并不清晰"的证据，比任何建议都更值得先看；
4. 确定性：Louvain 固定 ``seed``，同一份图谱两次跑出同一建议，才能 diff 审阅。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import networkx as nx

from codewiki.graph.analysis import module_graph
from codewiki.layering import DEFAULT_MAX_GROUP, DirGroup, directory_groups, rank_layers, unique_names
from codewiki.schemas import DataPacket

#: 产物文件名（§十一：suggestions/suggested-layers.toml）
SUGGESTED_LAYERS_FILENAME = "suggested-layers.toml"

MIN_COMMUNITY = 3


@dataclass
class SuggestedLayer:
    """一个建议层。"""

    name: str
    paths: list[str]
    modules: list[str]
    internal_edges: int
    external_edges: int
    order: int | None = None
    cyclic: bool = False

    @property
    def cohesion(self) -> float:
        total = self.internal_edges + self.external_edges
        return self.internal_edges / total if total else 0.0

    def reason(self) -> str:
        text = (
            f"{len(self.modules)} 个模块，内部边 {self.internal_edges} / "
            f"外部边 {self.external_edges}（内聚度 {self.cohesion:.0%}）"
        )
        if self.order is not None:
            text += f"，层序 {self.order}"
        if self.cyclic:
            text += "，与其他层存在循环依赖（分层在此不清晰）"
        return text


def _split_big_group(group: DirGroup, graph: nx.DiGraph, *, seed: int, max_group: int) -> list[DirGroup]:
    """目录组过大时用社区发现拆开；拆不出有意义的结果就原样保留。"""
    if len(group.modules) <= max_group:
        return [group]
    members = set(group.modules)
    sub = graph.subgraph(members).to_undirected()
    try:
        communities = [set(c) for c in nx.community.louvain_communities(sub, seed=seed)]
    except Exception:  # noqa: BLE001 - 算法失败不应让命令崩：保留目录组
        return [group]
    big = [c for c in communities if len(c) >= MIN_COMMUNITY]
    if len(big) < 2:
        return [group]

    paths_of = {m: graph.nodes[m].get("path") or m.removeprefix("mod::") for m in members}
    big.sort(key=lambda c: (-len(c), min(paths_of[m] for m in c)))
    covered = set().union(*big)
    pieces = [DirGroup(f"{group.name}.part{index}", [], sorted(c)) for index, c in enumerate(big, start=1)]
    leftover = sorted(members - covered)
    if leftover:
        pieces.append(DirGroup(f"{group.name}.misc", [], leftover))
    return pieces


def suggest_layers(
    packet: DataPacket, *, seed: int = 42, max_group: int = DEFAULT_MAX_GROUP
) -> list[SuggestedLayer]:
    """目录归组（超限下钻）→ 仍过大再社区拆分 → 层序，给出建议分层。"""
    graph = module_graph(packet)
    if not graph.nodes:
        return []

    paths_by_node = {
        n.id: (n.path or n.id.removeprefix("mod::")) for n in packet.nodes if n.id in graph.nodes
    }
    id_of_path = {path: node_id for node_id, path in paths_by_node.items()}

    groups: list[DirGroup] = []
    for group in directory_groups(list(paths_by_node.values()), max_group=max_group):
        ids = DirGroup(group.name, group.prefixes, sorted(id_of_path[p] for p in group.modules))
        groups.extend(_split_big_group(ids, graph, seed=seed, max_group=max_group))
    # 拆分出来的 ``X.partN`` 仍可能与某个真实目录同名 ⇒ 拆完再走一次同一份去重
    for group, name in zip(groups, unique_names([g.name for g in groups]), strict=True):
        group.name = name

    assignment = {module: index for index, group in enumerate(groups) for module in group.modules}
    order_of, cyclic = rank_layers(assignment, graph, len(groups))

    layers: list[SuggestedLayer] = []
    for index, group in enumerate(groups):
        member_set = set(group.modules)
        internal = sum(1 for u, v in graph.edges() if u in member_set and v in member_set)
        external = sum(1 for u, v in graph.edges() if (u in member_set) ^ (v in member_set))
        module_paths = sorted(paths_by_node[m] for m in group.modules)
        # 整目录成组时给目录前缀（可读、可维护）；拆分出来的组只能列文件
        paths = group.prefixes if group.prefixes else module_paths
        layers.append(
            SuggestedLayer(
                name=group.name,
                paths=list(paths),
                modules=sorted(group.modules),
                internal_edges=internal,
                external_edges=external,
                order=order_of[index],
                cyclic=index in cyclic,
            )
        )
    layers.sort(key=lambda layer: (layer.order or 0, -len(layer.modules), layer.name))
    return layers


def render_suggested_layers(layers: list[SuggestedLayer], *, title: str = "codewiki") -> str:
    """渲染建议文件（每条都带 ``# source: suggested`` 便于回溯）。"""
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    lines = [
        "# codewiki 分层建议 —— 按目录归组（过大的组再按子目录下钻/社区发现拆分），**纯静态、无需 AI**",
        f"# 项目：{title}",
        f"# 生成时间：{now}",
        "#",
        "# 用法：挑取合意的部分，执行 `codewiki merge-layers <本文件>`；",
        "#       或直接复制 [[architecture.layers]] 段到 codewiki.toml。",
        "# 注意：merge-layers 不会覆盖人工已声明的层；paths 重叠时会报 warning 要求人工选择。",
        "# order = 层序：1 是最上层（没有别的层依赖它），数字越大越靠下；同序的层互不依赖或互相成环。",
        "",
    ]
    if not layers:
        lines.append("# （图谱为空或无模块，未产出建议）")
        return "\n".join(lines) + "\n"

    for index, layer in enumerate(layers, start=1):
        lines.append("# source: suggested")
        lines.append(f"# 建议 {index}：{layer.reason()}")
        lines.append("[[architecture.layers]]")
        lines.append(f'name = "{_escape(layer.name)}"')
        lines.append("paths = [" + ", ".join(f'"{_escape(p)}"' for p in layer.paths) + "]")
        if layer.order is not None:
            lines.append(f"order = {layer.order}")
        lines.append("")
    return "\n".join(lines)


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


__all__ = ["SUGGESTED_LAYERS_FILENAME", "SuggestedLayer", "render_suggested_layers", "suggest_layers"]
