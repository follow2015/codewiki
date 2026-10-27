"""``suggest-layers`` —— 社区发现给出**分层建议**（§4.6.3）。

定位三条：

1. 这是**建议**不是自动分层：产出独立文件，人工挑选后 ``merge-layers`` 合并，
   **绝不自动覆盖人工分层**；
2. 算法可解释：层名取社区内模块的**最深公共目录**（比 "community-3" 有意义），
   并附"模块数 / 内外边数 / 内聚度"作为启发式理由；
3. 确定性：Louvain 需要固定 ``seed``，否则同一份图谱两次跑出不同建议，
   用户无法 diff 审阅。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import networkx as nx

from codewiki.graph.analysis import module_graph
from codewiki.schemas import DataPacket

SUGGESTED_LAYERS_FILENAME = "suggested-layers.toml"


@dataclass
class SuggestedLayer:
    """一个建议层。"""

    name: str
    paths: list[str]
    modules: list[str]
    internal_edges: int
    external_edges: int

    @property
    def cohesion(self) -> float:
        total = self.internal_edges + self.external_edges
        return self.internal_edges / total if total else 0.0

    def reason(self) -> str:
        return (
            f"{len(self.modules)} 个模块，内部边 {self.internal_edges} / "
            f"外部边 {self.external_edges}（内聚度 {self.cohesion:.0%}）"
        )


def _common_dir(paths: list[str]) -> str:
    """最深公共目录（模块都在 pkg/sub 下 ⇒ "pkg/sub"；散落则退到上一级）。"""
    if not paths:
        return ""
    parts = [p.split("/") for p in paths]
    common: list[str] = []
    for index in range(min(len(p) for p in parts)):
        segment = parts[0][index]
        if all(p[index] == segment for p in parts):
            common.append(segment)
        else:
            break
    if common and "." in common[-1]:
        common.pop()
    return "/".join(common)


def suggest_layers(packet: DataPacket, *, seed: int = 42) -> list[SuggestedLayer]:
    """按模块图的社区结构给出建议分层。"""
    graph = module_graph(packet)
    if not graph.nodes:
        return []

    undirected = graph.to_undirected()
    try:
        communities = list(nx.community.louvain_communities(undirected, seed=seed))
    except Exception:  # noqa: BLE001 - 算法失败不应让命令崩：退到"每个模块一层"
        communities = [{node} for node in graph.nodes]

    paths_by_node = {
        n.id: (n.path or n.id.removeprefix("mod::")) for n in packet.nodes if n.id in graph.nodes
    }
    layers: list[SuggestedLayer] = []
    for members in communities:
        member_set = set(members)
        internal = sum(1 for u, v in graph.edges() if u in member_set and v in member_set)
        external = sum(1 for u, v in graph.edges() if (u in member_set) ^ (v in member_set))
        module_ids = sorted(members)
        module_paths = sorted(paths_by_node.get(m, m.removeprefix("mod::")) for m in module_ids)
        directory = _common_dir(module_paths)
        name = directory.replace("/", ".") if directory else "root"
        paths = [directory] if directory else module_paths
        layers.append(
            SuggestedLayer(
                name=name,
                paths=paths,
                modules=module_ids,
                internal_edges=internal,
                external_edges=external,
            )
        )
    layers.sort(key=lambda layer: (-len(layer.modules), layer.name))
    return layers


def render_suggested_layers(layers: list[SuggestedLayer], *, title: str = "codewiki") -> str:
    """渲染建议文件（每条都带 ``# source: suggested`` 便于回溯）。"""
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    lines = [
        "# codewiki 分层建议 —— 社区发现（Louvain）产出，**纯静态、无需 AI**",
        f"# 项目：{title}",
        f"# 生成时间：{now}",
        "#",
        "# 用法：挑取合意的部分，执行 `codewiki merge-layers <本文件>`；",
        "#       或直接复制 [[architecture.layers]] 段到 codewiki.toml。",
        "# 注意：merge-layers 不会覆盖人工已声明的层；paths 重叠时会报 warning 要求人工选择。",
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
        lines.append("")
    return "\n".join(lines)


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


__all__ = ["SUGGESTED_LAYERS_FILENAME", "SuggestedLayer", "render_suggested_layers", "suggest_layers"]
