"""分层架构地图（首页「架构总览」，2026-10-23）。

**为什么要换掉 mermaid 宏观图**：几百个模块扔给自动布局，产出既读不了也
渲染不动（IPIP 实测：107KB 源超 mermaid maxTextSize 直接报错；分包后单页
仍是几百节点的毛线团）。参考 ipip 新架构 f00 总览图的表达方式 —— 手工
结构的**层带卡片 + 汇总数字**，一眼看清分层与依赖流向：

- 每层一条带：层名、模块数、符号数、子包分布（模块路径前缀 × 数量）；
- 相邻层之间画引用连线并标注条数；
- 右栏列出**跨层引用 TOP**（按条数降序）——分层泄漏（如「接口层→数据层
  60 条直连」）在这里直接可见；
- 层带可点击，跳到首页对应模块分组。

全部内联 SVG，颜色走页面 CSS 变量（明暗主题自动跟随）；层色逻辑与首页
分组色点同源（``layer_color``）。单模块页的 mermaid 上下文图**不受影响**。
"""

from __future__ import annotations

import re

from codewiki.layering import UNLAYERED_LAYER_ID
from codewiki.models import NodeType
from codewiki.render.escape import escape_attr
from codewiki.render.i18n import t
from codewiki.schemas import DataPacket

_LAYER_PALETTE = (
    "#6366f1",
    "#3b82f6",
    "#10b981",
    "#f59e0b",
    "#a855f7",
    "#f43f5e",
    "#14b8a6",
    "#eab308",
)

_LAYER_SEMANTIC = {
    "接口层": "#6366f1",  # i18n-ignore
    "服务层": "#3b82f6",  # i18n-ignore
    "数据层": "#10b981",  # i18n-ignore
    "基础设施": "#f59e0b",  # i18n-ignore
    "契约层": "#a855f7",  # i18n-ignore
    "任务层": "#f43f5e",  # i18n-ignore
}

_UNLAYERED_ID = UNLAYERED_LAYER_ID

_UNSAFE_ANCHOR = re.compile(r"[\"'<>&\s#]")

BAND_H = 56.0
BAND_GAP = 24.0


def layer_color(name: str) -> str:
    if name in _LAYER_SEMANTIC:
        return _LAYER_SEMANTIC[name]
    return _LAYER_PALETTE[sum(name.encode("utf-8")) % len(_LAYER_PALETTE)]


def _layer_buckets(packet: DataPacket):
    """按层归组节点；返回 ``(有序层带, 层名表)``，未分层兜底在最后。"""
    ordered = sorted(
        packet.layers, key=lambda item: (item.order if item.order is not None else 10_000, item.id)
    )
    names: dict[str, str] = {item.id: item.name for item in packet.layers}
    bands: list[tuple[str, str, list]] = []
    remaining = list(packet.nodes)
    for layer in ordered:
        here = [n for n in remaining if n.layer_id == layer.id]
        remaining = [n for n in remaining if n.layer_id != layer.id]
        bands.append((layer.id, layer.name, here))
    if remaining:
        bands.append((_UNLAYERED_ID, t("common.unlayered"), remaining))
    return bands, names


def cross_layer_flows(packet: DataPacket, *, top: int = 10):
    """层与层之间的引用计数（同层边不计）；按条数降序，取前 ``top``。

    返回 ``[((src_lid, dst_lid), count), …]``；weight 累加过的重复边按
    weight 计（与图谱口径一致）。
    """
    node_layer = {n.id: n.layer_id for n in packet.nodes}
    counts: dict[tuple[str, str], int] = {}
    for edge in packet.edges:
        src = node_layer.get(edge.source)
        dst = node_layer.get(edge.target)
        if src and dst and src != dst:
            counts[(src, dst)] = counts.get((src, dst), 0) + max(edge.weight, 1)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[:top], len(ranked) - min(top, len(ranked))


def _esc(text: str) -> str:
    return escape_attr(str(text))


def _display_name(lid: str, names: dict[str, str]) -> str:
    """层 id → 展示名；未分层兜底桶显示「未分层」而不是原始 id。"""
    if lid == _UNLAYERED_ID:
        return t("common.unlayered")
    return names.get(lid, lid)


def layer_anchor(lid: str) -> str:
    """层 → 页内锚点 id（SVG 层卡与首页分组必须用同一张表）。

    未分层桶的 id 含 ``layer::`` 前缀，直接拼进锚点既难看又像泄漏 ——
    统一给它一个干净的固定锚点。

    id 要落进 ``id="..."`` / ``href="#..."``，因此**必须是安全字符集**：层 id 由
    被分析仓库的配置提供（不可信），放行引号就能闭合属性注入。
    """
    if lid == _UNLAYERED_ID:
        return "layer-unlayered"
    return "layer-" + _UNSAFE_ANCHOR.sub("_", lid)


def _layer_flow_counts(packet: DataPacket) -> tuple[dict[str, tuple[int, int]], dict[tuple[str, str], int]]:
    """跨层引用统计：``(每层 (出, 入), {(源层, 宿层): 条数})``。

    同层边不计。出/入按节点累计，一个节点可以既出又入，两条口径互不影响。
    """
    node_layer = {n.id: n.layer_id for n in packet.nodes}
    pairs: dict[tuple[str, str], int] = {}
    sent: dict[str, int] = {}
    received: dict[str, int] = {}
    for edge in packet.edges:
        src = node_layer.get(edge.source)
        dst = node_layer.get(edge.target)
        if not src or not dst or src == dst:
            continue
        weight = max(edge.weight, 1)
        pairs[(src, dst)] = pairs.get((src, dst), 0) + weight
        sent[src] = sent.get(src, 0) + weight
        received[dst] = received.get(dst, 0) + weight
    io = {lid: (sent.get(lid, 0), received.get(lid, 0)) for lid in set(sent) | set(received)}
    return io, pairs


def _band_svg(
    y: float,
    lid: str,
    name: str,
    members: list,
    width: float,
    *,
    outbound: int = 0,
    inbound: int = 0,
) -> str:
    """一条层带：色条 + 层名 + 耦合度 + 计数 + 子包分布，整条可点击跳锚点。

    第一行三段：层名（左）、对外/对内引用条数（右起 140px）、模块计数（最右）。
    第二行是子包分布。符号数为 0 时只写模块数 —— 七层全写「· 0 符号」是纯噪声。
    """
    color = layer_color(name)
    modules = [n for n in members if n.type == NodeType.MODULE and n.path]
    symbols = len(members) - len(modules)

    dirs: dict[str, int] = {}
    for n in modules:
        directory = n.path.rsplit("/", 1)[0] if "/" in (n.path or "") else t("layer_map.root")
        dirs[directory] = dirs.get(directory, 0) + 1
    top_dirs = sorted(dirs.items(), key=lambda kv: (-kv[1], kv[0]))[:4]
    dir_chips = " · ".join(f"{d} ×{c}" for d, c in top_dirs) if top_dirs else "—"

    if symbols:
        stats = t("layer_map.module_stats", modules=len(modules), symbols=symbols)
    else:
        stats = t("layer_map.module_count_only", modules=len(modules))

    parts = [
        f'<a href="#{layer_anchor(lid)}" style="text-decoration:none">',
        f'<rect x="0" y="{y}" width="{width}" height="{BAND_H}" rx="10" '
        'style="fill:var(--panel);stroke:var(--border);stroke-width:1"/>',
        f'<rect x="0" y="{y}" width="5" height="{BAND_H}" rx="2.5" fill="{color}"/>',
        f'<text x="22" y="{y + 22}" font-size="14.5" font-weight="650" '
        f'style="fill:var(--text)">{_esc(name)}</text>',
    ]
    if outbound or inbound:
        parts.append(
            f'<text x="{width - 146}" y="{y + 22}" font-size="10.5" text-anchor="end" '
            f'style="fill:var(--faint)">{t("layer_map.io_stats", sent=outbound, received=inbound)}</text>'
        )
    parts.extend(
        [
            f'<text x="{width - 18}" y="{y + 22}" font-size="12.5" font-weight="600" '
            f'text-anchor="end" style="fill:var(--text)">{stats}</text>',
            f'<text x="22" y="{y + 42}" font-size="10.5" style="fill:var(--faint)">{_esc(dir_chips)}</text>',
            "</a>",
        ]
    )
    return "\n".join(parts)


def render_layer_map_svg(packet: DataPacket, *, max_flows: int = 10) -> str:
    """渲染分层架构地图（内联 SVG；无分层配置时返回空串 —— 无硬画）。

    版式：左列层带自上而下，右列跨层引用榜。两列**同高**——右栏按左列总高
    均分行距，每行带一条按条数归一的横条，既是榜单也是条形图；此前右栏固定
    22px 行距，七层时下半栏留 460px 空白。
    """
    if not packet.layers:
        return ""

    band_w, panel_w, gap_x = 604.0, 300.0, 36.0
    bands, names = _layer_buckets(packet)
    flows, overflow = cross_layer_flows(packet, top=max_flows)
    io, pairs = _layer_flow_counts(packet)

    stack_h = len(bands) * BAND_H + (len(bands) - 1) * BAND_GAP
    total_h = 8.0 + stack_h + 10.0
    total_w = band_w + gap_x + panel_w

    parts = [
        f'<svg class="layer-map" viewBox="0 0 {total_w:g} {total_h:g}" '
        'xmlns="http://www.w3.org/2000/svg" role="img" '
        'style="width:100%;height:auto;display:block;font-family:inherit">',
    ]

    adjacency = [
        pairs.get((bands[i][0], bands[i + 1][0]), 0) + pairs.get((bands[i + 1][0], bands[i][0]), 0)
        for i in range(len(bands) - 1)
    ]
    heaviest = max(adjacency, default=0)

    y = 8.0
    for index, (lid, name, members) in enumerate(bands):
        sent, received = io.get(lid, (0, 0))
        parts.append(_band_svg(y, lid, name, members, band_w, outbound=sent, inbound=received))
        if index < len(bands) - 1:
            next_top = y + BAND_H + BAND_GAP
            mid_x = band_w / 2
            count = adjacency[index]
            weight = 1.2 if not count else 1.2 + 2.4 * count / heaviest
            dash = ";stroke-dasharray:3 4" if not count else ";stroke-linecap:round"
            parts.append(
                f'<line x1="{mid_x}" y1="{y + BAND_H}" x2="{mid_x}" y2="{next_top}" '
                f'style="stroke:var(--border-strong);stroke-width:{weight:.1f}{dash}"/>'
            )
            if count:
                mid_y = (y + BAND_H + next_top) / 2
                parts.append(
                    f'<rect x="{mid_x - 19}" y="{mid_y - 8}" width="38" height="16" rx="8" '
                    'style="fill:var(--panel);stroke:var(--border);stroke-width:1"/>'
                )
                parts.append(
                    f'<text x="{mid_x}" y="{mid_y + 3.5}" font-size="9.5" text-anchor="middle" '
                    f'style="fill:var(--muted)">⇄ {count}</text>'
                )
            y = next_top
        else:
            y += BAND_H

    px = band_w + gap_x
    parts.append(
        f'<rect x="{px}" y="8" width="{panel_w}" height="{stack_h}" rx="10" '
        'style="fill:var(--panel-2);stroke:var(--border);stroke-width:1"/>'
    )
    parts.append(
        f'<text x="{px + 18}" y="{30}" font-size="12" font-weight="650" '
        f'style="fill:var(--muted)">{t("layer_map.cross_layer_top", count=len(flows))}</text>'
    )
    if not flows:
        parts.append(
            f'<text x="{px + 18}" y="{58}" font-size="11" style="fill:var(--faint)">'
            f"{t('layer_map.no_cross_layer')}</text>"
        )
        parts.append("</svg>")
        return "\n".join(parts)

    header, footnote = 40.0, 26.0 if overflow > 0 else 12.0
    row_h = min(46.0, max(20.0, (stack_h - header - footnote) / len(flows)))
    top_count = flows[0][1] or 1
    bar_max = panel_w - 44.0
    row_y = 8.0 + header
    for (src_lid, dst_lid), count in flows:
        src_color = layer_color(names.get(src_lid, src_lid))
        flow_label = f"{_display_name(src_lid, names)} → {_display_name(dst_lid, names)}"
        parts.append(
            f'<text x="{px + 20}" y="{row_y}" font-size="11" '
            f'style="fill:var(--text)">{_esc(flow_label)}</text>'
        )
        parts.append(
            f'<text x="{px + panel_w - 18}" y="{row_y}" font-size="11" '
            f'font-weight="600" text-anchor="end" style="fill:var(--text)">{count}</text>'
        )
        bar = max(2.0, bar_max * count / top_count)
        parts.append(
            f'<rect x="{px + 20}" y="{row_y + 11}" width="{bar_max:.1f}" height="4" rx="2" '
            'style="fill:var(--border)" opacity="0.55"/>'
        )
        parts.append(
            f'<rect x="{px + 20}" y="{row_y + 11}" width="{bar:.1f}" height="4" rx="2" '
            f'fill="{src_color}" opacity="0.9"/>'
        )
        row_y += row_h
    if overflow > 0:
        parts.append(
            f'<text x="{px + panel_w - 18}" y="{8 + stack_h - 10}" font-size="10.5" '
            f'text-anchor="end" style="fill:var(--faint)">'
            f"{t('layer_map.more_flows', count=overflow)}</text>"
        )

    parts.append("</svg>")
    return "\n".join(parts)


__all__ = ["cross_layer_flows", "layer_anchor", "layer_color", "render_layer_map_svg"]
