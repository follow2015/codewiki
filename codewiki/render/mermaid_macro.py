"""宏观系统架构图（System Context，§4.2.1）。

目标：展示**层级依赖**与核心组件的数据流向，按配置顺序自上而下排列。

生成规则（逐条对应 §4.2.1）：

1. 遍历 ``architecture.layers`` 生成 ``subgraph``（层 ID 作子图 ID）；
2. 模块按 paths 匹配归入子图，层内按调用权重排序；
3. 有注解时把多行说明用 ``<br/>`` 拼到节点 Label；
4. 层间 ``LAYER_DEPENDS`` 边聚合成一条带调用次数的边；
5. 低于 ``min_confidence`` 的边**不渲染**；动态边虚线 + 置信度标签。

节点 ID 一律用 ``n<hash>``（Mermaid 的 ID 只允许字母数字下划线），真实名字放
Label —— 这样中文路径、点号、斜杠都不会破坏语法。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import networkx as nx

from codewiki.layering import UNLAYERED_LAYER_ID
from codewiki.models import EdgeType, NodeType
from codewiki.render.i18n import t
from codewiki.schemas import DataPacket

DYNAMIC_EDGE = "-.->"

TYPE_ONLY_LABEL = "diagrams.edge_type_only"

UNLAYERED_LABEL = "common.unlayered"

BACKWARD_EDGE_STYLE = "stroke:#d33,stroke-width:2px,stroke-dasharray:4 2"

DYNAMIC_EDGE_STYLE = "stroke-dasharray:1 3"

CYCLE_EDGE_STYLE = "stroke:#d33,stroke-width:3px"

EDGE_STYLES = {
    "dynamic": DYNAMIC_EDGE_STYLE,
    "backward": BACKWARD_EDGE_STYLE,
    "cycle": CYCLE_EDGE_STYLE,
}

MAX_VISIBLE_EDGES = 60

HUB_RATIO = 0.3

MAX_DIR_GROUPS = 12

_UNRANKED_LAYER = 10**6

INVISIBLE_LINK = "~~~"


def _layer_chain(layer_ids: list[str]) -> list[str]:
    """把层按 ``order`` 串成不可见链路，钉死**自上而下**的层序。

    dagre 默认按边分配 rank，而不是按层的配置顺序：只要有一条"下层 → 上层"的
    回边，下层就会被提到图顶（三节点实测：``A --> B`` 加 ``C -.-> A`` 渲染出来是
    ``db / api / core``，方向感全丢）。``~~~`` 不画线却占 rank，顺序因此只由
    ``order`` 决定，反向边只影响它自己那条箭头。

    只有一层时没有"顺序"可言，不输出 —— 免得在图上留一条看不见的链路。
    """
    ids = [node_id(layer_id) for layer_id in layer_ids]
    if len(ids) < 2:
        return []
    return ["  " + f" {INVISIBLE_LINK} ".join(ids)]


def _chain_link_count(chain_lines: list[str]) -> int:
    """层链占用的 ``linkStyle`` 槽位数。

    一条链是**一行**里用 ``~~~`` 串起 N 个节点，所以「行数」不等于「link 数」：
    六层是一行、五条 link。按行数算偏移会让所有 ``linkStyle`` 前移 4 位，颜色落到
    链上那条隐形 path 和自己的邻边上（pydantic 层图实测：反向边应标 6,9,10,14，
    实际标在 2,5,6,10）。两层时两者恰好相等，缺陷在两层用例里看不出来。
    """
    return sum(line.count(INVISIBLE_LINK) for line in chain_lines)


def node_id(seed: str) -> str:
    """由任意字符串生成合法的 Mermaid 节点 ID。"""
    return "n" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]


def escape_label(text: str) -> str:
    """转义 Label 里的特殊字符（§4.3：未转义的 ``()[]"`` 是坏图特征）。"""
    return (
        text.replace("\\", "\\\\")
        .replace('"', "#quot;")
        .replace("(", "#40;")
        .replace(")", "#41;")
        .replace("[", "#91;")
        .replace("]", "#93;")
        .replace("{", "#123;")
        .replace("}", "#125;")
    )


def _annotation_label(packet: DataPacket, target: str, base: str) -> str:
    """把人工/AI 注解拼进 Label（多行用 ``<br/>``，§4.2.1 第 3 步）。"""
    notes = [a for a in packet.annotations if a.target == target and a.note]
    if not notes:
        return base
    extra = "<br/>".join(escape_label(a.note) for a in notes)
    return f"{base}<br/>{extra}"


STATS_STYLE = "fill:#f6f6f6,stroke:#d0d0d0,color:#666,font-size:12px"


def _stats_lines(nodes: int, edges: int, folded: int) -> list[str]:
    """图例统计行：「节点 N · 边 M · 已折叠 K」。

    降噪本身是好事，藏起来就成了误导：读者看到 18 条边，会以为这层只有 18 条
    依赖。所以每张系统图 / 层图 / 层内图底部都写一行账。它**不套 subgraph、
    不参与 linkStyle**（没有连边），样式压成灰底小字 —— 它是图注，不是结构。
    模块图的折叠用行内节点表达（粒度更细），不重复加这行。
    """
    stats_id = node_id("diagram-stats")
    label = escape_label(t("diagrams.stats", nodes=nodes, edges=edges, folded=folded))
    return [
        f'  {stats_id}["{label}"]',
        f"  classDef stats {STATS_STYLE}",
        f"  class {stats_id} stats",
    ]


def render_system_context(
    packet: DataPacket,
    *,
    min_confidence: float = 0.5,
    subset: set[str] | None = None,
    include_modules: bool = True,
) -> str:
    """渲染宏观架构图（返回 Mermaid 文本，不含代码围栏）。

    ``subset`` 用于**大图分包**：只画给定模块（跨层边仍按全局事实画，但只保留
    两端都在本页的边 —— 半条边比没有边更误导）。

    ``include_modules=False`` 切换到**层间概览**：每层一个节点（标签写明模块数），
    只画 ``LAYER_DEPENDS``。系统图默认用这个粒度 —— 模块级的系统图在 100 个模块
    规模上就是毛线球（pydantic 实测 474 行、渲染 1600×2626），层间关系反而被淹没；
    层内细节由每层单独的图承担。
    """
    layers = sorted(packet.layers, key=lambda layer: layer.order if layer.order is not None else 10_000)
    modules = [n for n in packet.nodes if n.type == NodeType.MODULE]
    if subset is not None:
        modules = [m for m in modules if m.id in subset]

    lines = ["flowchart TD"]
    if not layers:
        plan = _plan_module_edges(packet, min_confidence, subset={m.id for m in modules})
        for module in sorted(modules, key=lambda m: m.path or ""):
            lines.append(f'  {node_id(module.id)}["{_module_label(packet, module, plan.hubs)}"]')
        lines.extend(emit_edges(plan.entries))
        lines.extend(_stats_lines(len(modules), len(plan.entries), plan.raw - len(plan.entries)))
        return "\n".join(lines) + "\n"

    if not include_modules:
        return _render_layer_overview(packet, layers, modules, min_confidence=min_confidence)

    plan = _plan_module_edges(packet, min_confidence, subset={m.id for m in modules})

    for layer in layers:
        lines.append(f'  subgraph {node_id(layer.id)}["{escape_label(layer.name)}"]')
        members = [m for m in modules if m.layer_id == layer.id]
        for module in _sorted_by_weight(packet, members):
            lines.append(f'    {node_id(module.id)}["{_module_label(packet, module, plan.hubs)}"]')
        lines.append("  end")

    unlayered = [m for m in modules if not m.layer_id or m.layer_id == UNLAYERED_LAYER_ID]
    if unlayered:
        lines.append(f'  subgraph {node_id(UNLAYERED_LAYER_ID)}["{t(UNLAYERED_LABEL)}"]')
        for module in _sorted_by_weight(packet, unlayered):
            lines.append(f'    {node_id(module.id)}["{_module_label(packet, module, plan.hubs)}"]')
        lines.append("  end")

    chain_ids = [layer.id for layer in layers]
    if unlayered:
        chain_ids.append(UNLAYERED_LAYER_ID)
    chain_lines = _layer_chain(chain_ids)
    lines.extend(chain_lines)

    entries = _layer_edge_entries(packet, min_confidence, layers)
    entries.extend(plan.entries)
    lines.extend(emit_edges(entries, offset=_chain_link_count(chain_lines)))
    lines.extend(_stats_lines(len(modules), len(entries), plan.raw - len(plan.entries)))

    return "\n".join(lines) + "\n"


def _render_layer_overview(
    packet: DataPacket,
    layers: list,
    modules: list,
    *,
    min_confidence: float,
) -> str:
    """层间概览：每层一个节点，只画 ``LAYER_DEPENDS``。

    节点标签写明模块数 —— 读者一眼能判断"这层值不值得下钻"，而不是先点进去
    才知道里面有多少东西。未分层兜底组同样呈现（不能吞掉没归类的模块）。
    """
    lines = ["flowchart TD"]
    for layer in layers:
        members = [m for m in modules if m.layer_id == layer.id]
        label = escape_label(t("diagrams.layer_node", name=layer.name, count=len(members)))
        lines.append(f'  {node_id(layer.id)}["{label}"]')

    unlayered = [m for m in modules if not m.layer_id or m.layer_id == UNLAYERED_LAYER_ID]
    if unlayered:
        label = escape_label(t("diagrams.layer_node", name=t(UNLAYERED_LABEL), count=len(unlayered)))
        lines.append(f'  {node_id(UNLAYERED_LAYER_ID)}["{label}"]')

    chain_ids = [layer.id for layer in layers]
    if unlayered:
        chain_ids.append(UNLAYERED_LAYER_ID)
    chain_lines = _layer_chain(chain_ids)
    lines.extend(chain_lines)

    entries = _layer_edge_entries(packet, min_confidence, layers)
    lines.extend(emit_edges(entries, offset=_chain_link_count(chain_lines)))
    lines.extend(_stats_lines(len(chain_ids), len(entries), 0))
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class EdgeLine:
    """一条已渲染的边，以及它需要的样式键。"""

    text: str
    styles: tuple[str, ...] = ()


@dataclass(frozen=True)
class EdgePlan:
    """一页图里模块级边的取舍结果。"""

    entries: list[EdgeLine]
    hubs: dict[str, int] = field(default_factory=dict)
    trimmed: int = 0
    raw: int = 0
    edges: list = field(default_factory=list)


def emit_edges(entries: list[EdgeLine], *, offset: int = 0) -> list[str]:
    """输出边，并按类归组生成 ``linkStyle``。

    ``offset`` 是**本批边之前已定义的 link 数**。Mermaid 的 linkStyle 按全图 link
    顺序编号，而不可见链路 ``~~~`` 也占一个槽位（实测：``~~~`` 排第 2 条时，
    ``linkStyle 1`` 落在它那条 ``stroke-dasharray: 0 0 …`` 的隐形 path 上，颜色
    实际标不到任何可见边）。少算这一条，所有颜色都会前移一位、标到邻边上。
    """
    lines = [entry.text for entry in entries]
    for key, style in EDGE_STYLES.items():
        positions = [offset + index for index, entry in enumerate(entries) if key in entry.styles]
        if positions:
            lines.append(f"  linkStyle {','.join(str(position) for position in positions)} {style}")
    return lines


def _layer_edge_entries(packet: DataPacket, min_confidence: float, layers: list) -> list[EdgeLine]:
    """层间边及其样式键。

    **反向依赖**（下层反过来依赖上层）单独标红：分层倒置是图上最该先看见的东西，
    而它与正常依赖长得一模一样（都是一条带次数的箭头）。
    """
    layer_rank = {layer.id: position for position, layer in enumerate(layers)}
    entries: list[EdgeLine] = []
    for edge in packet.edges:
        if edge.type != EdgeType.LAYER_DEPENDS:
            continue
        if edge.confidence < min_confidence:
            continue
        styles: list[str] = []
        if layer_rank.get(edge.target, _UNRANKED_LAYER) < layer_rank.get(edge.source, _UNRANKED_LAYER):
            styles.append("backward")
        if edge.in_cycle:
            styles.append("cycle")
        if edge.type_only:
            shown = t(TYPE_ONLY_LABEL)
            tag = f"|{shown} {edge.weight}|" if edge.weight and edge.weight > 1 else f"|{shown}|"
            entries.append(
                EdgeLine(
                    f"  {node_id(edge.source)} {DYNAMIC_EDGE}{tag} {node_id(edge.target)}", tuple(styles)
                )
            )
            continue
        if not edge.static:
            styles.append("dynamic")
        arrow = DYNAMIC_EDGE if not edge.static else "-->"
        tag = f"|{edge.weight}|" if edge.weight and edge.weight > 1 else ""
        entries.append(
            EdgeLine(f"  {node_id(edge.source)} {arrow}{tag} {node_id(edge.target)}", tuple(styles))
        )
    return entries


def _plan_module_edges(packet: DataPacket, min_confidence: float, *, subset: set[str]) -> EdgePlan:
    """模块级边的取舍：**只有超阈值才降噪**。

    小图不降噪是有意的：`A → C` 与 `A → B → C` 在依赖图里是两条不同的声明，
    小图上省掉它只会让读者以为没有直接依赖。
    """
    candidates = _module_edges(packet, min_confidence, subset=subset)
    if len(candidates) <= MAX_VISIBLE_EDGES:
        return EdgePlan(
            [_module_entry(edge) for edge in candidates], edges=list(candidates), raw=len(candidates)
        )

    hubs = _hub_counts(candidates, module_count=len(subset))
    kept, trimmed = _transitive_reduce(candidates)
    kept = [edge for edge in kept if edge.target not in hubs]
    return EdgePlan([_module_entry(edge) for edge in kept], hubs, trimmed, edges=kept, raw=len(candidates))


def _hub_counts(edges: list, *, module_count: int) -> dict[str, int]:
    """被同层超过 ``HUB_RATIO`` 的模块依赖的节点 → 依赖它的模块数（去重）。"""
    if not module_count:
        return {}
    sources: dict[str, set[str]] = {}
    for edge in edges:
        sources.setdefault(edge.target, set()).add(edge.source)
    return {target: len(users) for target, users in sources.items() if len(users) / module_count > HUB_RATIO}


def _transitive_reduce(edges: list) -> tuple[list, int]:
    """去掉"已有另一条路径可达"的直连边，返回 ``(保留的边, 省略条数)``。

    **先缩点再归约**：环内的边一条都不动，只对强连通分量之间的边做传递归约。

    为什么不能逐边判断"去掉它后还可达吗"：判据是"其余**全部**边"，而不是"实际
    保留的边"。环里 ``A→B``、``C→B``、``A↔C`` 这几条边，``A→B`` 有备选路径
    ``A→C→B``、``C→B`` 有备选路径 ``C→A→B``，两条都被判可删 —— B 的入边被清空，
    图上只剩一个孤立方框，图注还写着"省略 2 条"。而且只在层内有环、边数超阈值时
    才触发，恰好是最需要看清依赖的场景。

    缩点后分量图是 DAG，``nx.transitive_reduction`` 的结果与遍历顺序无关，可达性
    有保证；同一对分量之间的平行边整体保留或整体省略。复杂度 O(V·E)，不再是 O(E²)。
    """
    graph = nx.DiGraph()
    for edge in edges:
        graph.add_edge(edge.source, edge.target)
    condensed = nx.condensation(graph)
    member_of: dict[str, int] = {}
    for comp, data in condensed.nodes(data=True):
        for node in data["members"]:
            member_of[node] = comp
    reduced = nx.transitive_reduction(condensed)

    kept = []
    for edge in edges:
        src, dst = member_of[edge.source], member_of[edge.target]
        if src == dst or reduced.has_edge(src, dst):
            kept.append(edge)
    return kept, len(edges) - len(kept)


def _module_edges(packet: DataPacket, min_confidence: float, *, subset: set[str]) -> list:
    """同层内的 ``IMPORTS`` 边（跨层已由 ``LAYER_DEPENDS`` 表达）。

    跨层的那几十条模块 import 如果照画，会与 LAYER_DEPENDS 重复表达同一件事；
    更糟的是它们把两个 subgraph 直接连成一片，Mermaid 的 dagre 就失去"层与层
    上下分层"的依据，整张图塌成一条横向长带（25 个节点排一行、5588px 宽）。
    所以这里必须按层过滤 —— 跨层只留聚合边，图才读得出方向。
    """
    layer_of = {n.id: n.layer_id for n in packet.nodes}
    out = []
    for edge in packet.edges:
        if edge.type != EdgeType.IMPORTS or edge.confidence < min_confidence:
            continue
        if edge.source not in subset or edge.target not in subset:
            continue
        if layer_of.get(edge.source) != layer_of.get(edge.target):
            continue
        out.append(edge)
    return out


def _module_entry(edge) -> EdgeLine:
    """一条模块边的行文本与样式键。"""
    styles: list[str] = []
    if edge.in_cycle:
        styles.append("cycle")
    if edge.type_only:
        return EdgeLine(
            f"  {node_id(edge.source)} {DYNAMIC_EDGE}|{t(TYPE_ONLY_LABEL)}| {node_id(edge.target)}",
            tuple(styles),
        )
    if not edge.static:
        styles.append("dynamic")
    arrow = DYNAMIC_EDGE if not edge.static else "-->"
    label = f"|{edge.confidence:.2f}?|" if not edge.static else ""
    return EdgeLine(f"  {node_id(edge.source)} {arrow}{label} {node_id(edge.target)}", tuple(styles))


def _module_label(packet: DataPacket, module, hubs: dict[str, int], *, display: str | None = None) -> str:
    """模块节点标签：路径 +（枢纽时）徽标 + 人工注解。

    ``display`` 用于层内图：整个层都在 ``v1/`` 下时，每个节点再重复一遍 ``v1/``
    只是噪声（层名在页面标题和框标题里已经有了）。

    徽标前的空格由这里补：i18n 目录禁止值首尾带空白（``test_no_empty_or_padded_entries``），
    而 ``errors.py· 被 12 个模块使用`` 挤在一起读不断句。
    """
    label = escape_label(display or module.path or module.id)
    count = hubs.get(module.id)
    if count:
        label += " " + escape_label(t("diagrams.hub_badge", count=count))
    return _annotation_label(packet, module.id, label)


def _sorted_by_weight(packet: DataPacket, members: list) -> list:
    """层内按调用权重（出入度之和）降序，权重相同的按路径，保证产物稳定。"""
    degree: dict[str, int] = {}
    for edge in packet.edges:
        if edge.type not in (EdgeType.IMPORTS, EdgeType.CALLS):
            continue
        degree[edge.source] = degree.get(edge.source, 0) + 1
        degree[edge.target] = degree.get(edge.target, 0) + 1
    return sorted(members, key=lambda m: (-degree.get(m.id, 0), m.path or m.id))


def _common_dir_prefix(paths: list[str]) -> str:
    """所有模块共同的**目录**前缀（含结尾斜杠）；没有共同目录时返回空串。

    ``root`` 层是 ``main.py``/``fields.py`` 这种裸文件名，前缀为空；``v1`` 层全是
    ``v1/...``，前缀就是 ``v1/`` —— 去掉它，框里只留 ``core/a.py`` 这种有用信息，
    而不是每个节点都重复一遍层名。
    """
    dirs = [path.rsplit("/", 1)[0] if "/" in path else "" for path in paths]
    if not dirs or not all(dirs):
        return ""
    common: list[str] = []
    for column in zip(*(dir.split("/") for dir in dirs), strict=False):
        if len(set(column)) != 1:
            break
        common.append(column[0])
    return "".join(part + "/" for part in common)


def _dir_groups(members: list, prefix: str) -> dict[str, list]:
    """按**层内相对目录**把模块分组：键是相对目录（层根为 ``""``）。"""
    groups: dict[str, list] = {}
    for module in members:
        path = module.path or module.id
        relative = path[len(prefix) :] if prefix and path.startswith(prefix) else path
        key = relative.rsplit("/", 1)[0] if "/" in relative else ""
        groups.setdefault(key, []).append(module)
    return groups


def render_layer_detail(
    packet: DataPacket,
    *,
    layer_name: str,
    members: list,
    min_confidence: float = 0.5,
) -> str:
    """单层的模块级图：层内再按子目录分一层框，框之间只连**聚合边**。

    系统图只画层间之后，这张图承担"这一层里有什么"。层内还有几十个模块、上百条
    同层 import 时，平铺出来仍是一团线（pydantic `v1` 27 个模块、130 条边）。
    目录是这里唯一现成的二级结构：一个子目录一个框，跨目录的依赖收成一条带次数的
    箭头 —— 读者先看到"哪几个包在互相依赖"，需要时再点进模块页。

    只有分组数在 ``[2, MAX_DIR_GROUPS]`` 之间才真的分组：一个组等于没分，
    组太多（30 个目录各 1 个模块）比平铺更乱。
    """
    subset = {module.id for module in members}
    plan = _plan_module_edges(packet, min_confidence, subset=subset)

    paths = [module.path or module.id for module in members]
    prefix = _common_dir_prefix(paths)
    groups = _dir_groups(members, prefix)
    grouped = 2 <= len(groups) <= MAX_DIR_GROUPS
    group_of = {module.id: key for key, mods in groups.items() for module in mods}

    def _shown(module) -> str:
        """节点标签：去掉层前缀；分组时再去掉框自己的目录名（框标题已经写明）。"""
        relative = (module.path or module.id)[len(prefix) :] if prefix else (module.path or module.id)
        key = group_of.get(module.id, "")
        if grouped and key and relative.startswith(key + "/"):
            return relative[len(key) + 1 :]
        return relative

    lines = ["flowchart TD"]
    ordered = sorted(groups.items(), key=lambda item: (item[0] != "", item[0]))
    for key, mods in ordered:
        if grouped:
            title = key or layer_name
            label = escape_label(t("diagrams.dir_group", name=title, count=len(mods)))
            lines.append(f'  subgraph {node_id(f"dir::{layer_name}::{key}")}["{label}"]')
        indent = "    " if grouped else "  "
        for module in _sorted_by_weight(packet, mods):
            text = _module_label(packet, module, plan.hubs, display=_shown(module))
            lines.append(f'{indent}{node_id(module.id)}["{text}"]')
        if grouped:
            lines.append("  end")

    if not grouped:
        lines.extend(emit_edges(plan.entries))
        lines.extend(_stats_lines(len(members), len(plan.entries), plan.raw - len(plan.entries)))
        return "\n".join(lines) + "\n"

    intra: list[EdgeLine] = []
    aggregate: dict[tuple[str, str], list] = {}
    for edge, entry in zip(plan.edges, plan.entries, strict=True):
        source_group = group_of.get(edge.source, "")
        target_group = group_of.get(edge.target, "")
        if source_group == target_group:
            intra.append(entry)
        else:
            aggregate.setdefault((source_group, target_group), []).append(edge)

    lines.extend(emit_edges(intra))
    for (source_group, target_group), edges in sorted(aggregate.items()):
        label = t(TYPE_ONLY_LABEL) if all(edge.type_only for edge in edges) else ""
        arrow = DYNAMIC_EDGE if label else "-->"
        tag = f"|{label} {len(edges)}|" if label else f"|{len(edges)}|"
        lines.append(
            f"  {node_id(f'dir::{layer_name}::{source_group}')}"
            f" {arrow}{tag} {node_id(f'dir::{layer_name}::{target_group}')}"
        )
    drawn = len(intra) + len(aggregate)
    lines.extend(_stats_lines(len(members), drawn, plan.raw - drawn))
    return "\n".join(lines) + "\n"


__all__ = [
    "CYCLE_EDGE_STYLE",
    "DYNAMIC_EDGE",
    "DYNAMIC_EDGE_STYLE",
    "EDGE_STYLES",
    "EdgeLine",
    "STATS_STYLE",
    "TYPE_ONLY_LABEL",
    "emit_edges",
    "escape_label",
    "node_id",
    "render_layer_detail",
    "render_system_context",
]
