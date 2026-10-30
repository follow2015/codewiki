"""Markdown Wiki 渲染（§4.5）。

每个模块一页：**符号 / 上下游 / 注解**（对应产物注释里的三项），外加内嵌的
模块上下文图（直接引用 ``diagrams/modules/*.mmd`` 的内容，点与图同源）。

另生成 ``index.md``：分层 → 模块清单，带诊断摘要。
"""

from __future__ import annotations

from codewiki.graph.index import GraphIndex
from codewiki.models import NodeType
from codewiki.render.diagrams import _slug, build_slug_map
from codewiki.render.i18n import t
from codewiki.schemas import DataPacket

MODULES_SUBDIR = "modules"
INDEX_FILENAME = "index.md"


def _packet_slug_map(packet: DataPacket) -> dict[str, str]:
    """全包唯一的 slug 映射（与页面文件名 / 图文件名同源，M3）。"""
    return build_slug_map([n.path for n in packet.nodes if n.type == NodeType.MODULE and n.path])


def render_index(packet: DataPacket) -> str:
    """索引页：分层 → 模块清单 + 诊断摘要。"""
    lines = [f"# {packet.meta.title}", ""]

    if packet.meta.repo_commit:
        lines.append(
            t(
                f"> 提交：{packet.meta.repo_commit} · 生成于 {packet.meta.generated_at}",
                f"> commit: {packet.meta.repo_commit} · generated at {packet.meta.generated_at}",
            )
        )
        lines.append("")

    modules = [n for n in packet.nodes if n.type == NodeType.MODULE and n.path]
    slug_map = _packet_slug_map(packet)
    layers = sorted(packet.layers, key=lambda layer: layer.order if layer.order is not None else 10_000)

    if layers:
        lines.append(t("## 架构分层", "## Architecture layers"))
        lines.append("")
        for layer in layers:
            members = sorted(m.path for m in modules if m.layer_id == layer.id)
            lines.append(
                t(
                    f"- **{layer.name}**（{len(members)} 个模块）",
                    f"- **{layer.name}** ({len(members)} modules)",
                )
            )
            for path in members:
                lines.append(f"  - [{path}]({MODULES_SUBDIR}/{slug_map[path]}.md)")
        lines.append("")

        unlayered = sorted(m.path for m in modules if not m.layer_id or m.layer_id.endswith("_unlayered"))
        if unlayered:
            lines.append(t("## 未分层模块", "## Unlayered modules"))
            lines.append("")
            for path in unlayered:
                lines.append(f"- [{path}]({MODULES_SUBDIR}/{slug_map[path]}.md)")
            lines.append("")
    else:
        lines.append(t("## 模块清单（未配置分层）", "## Modules (no layers configured)"))
        lines.append("")
        for path in sorted(m.path for m in modules):
            lines.append(f"- [{path}]({MODULES_SUBDIR}/{slug_map[path]}.md)")
        lines.append("")

    errors = [d for d in packet.diagnostics if d.level == "error"]
    warnings = [d for d in packet.diagnostics if d.level == "warning"]
    lines.append(t("## 诊断摘要", "## Diagnostics"))
    lines.append("")
    lines.append(
        t(
            f"- error {len(errors)} / warning {len(warnings)} / 共 {len(packet.diagnostics)}",
            f"- error {len(errors)} / warning {len(warnings)} / total {len(packet.diagnostics)}",
        )
    )
    lines.append("")
    for item in errors[:20]:
        where = f"（{item.where}）" if item.where else ""
        lines.append(f"- **error** `{item.code}` {where}：{item.message}")
    lines.append("")
    lines.append(
        t(
            "完整清单见 [diagnostics.md](diagnostics.md)。",
            "See [diagnostics.md](diagnostics.md) for the full list.",
        )
    )
    lines.append("")
    return "\n".join(lines)


def render_module_page(
    packet: DataPacket,
    module_id: str,
    *,
    diagram_text: str | None = None,
    index: GraphIndex | None = None,
) -> str:
    """模块页：符号 / 上下游 / 注解 + 内嵌上下文图。

    ``index`` 同 :func:`~codewiki.render.mermaid_context.render_module_context`：
    批量渲染必须复用，逐个建索引会把 O(N) 拖成 O(N × E)。
    """
    idx = index or GraphIndex(packet)
    nodes = idx.nodes
    module = nodes.get(module_id)
    if module is None or not module.path:
        raise KeyError(f"模块不存在: {module_id}")

    lines = [f"# {module.path}", ""]
    layer_name = _layer_name(packet, module.layer_id)
    meta_bits = [
        t(f"语言 `{module.language or '?'}`", f"language `{module.language or '?'}`"),
        t(f"层 {layer_name}", f"layer {layer_name}"),
    ]
    if module.hash:
        meta_bits.append(f"hash `{module.hash}`")
    lines.append(" · ".join(meta_bits))
    lines.append("")

    notes = [a for a in packet.annotations if a.target == module_id]
    if notes:
        lines.append(t("## 注解", "## Annotations"))
        lines.append("")
        for note in notes:
            tag = (
                t("人工", "human")
                if note.source == "human"
                else t(
                    f"AI（reviewed={str(note.reviewed).lower()}）",
                    f"AI (reviewed={str(note.reviewed).lower()})",
                )
            )
            label = f"【{note.label}】" if note.label else ""
            lines.append(f"- {label}{note.note} —— *{tag}*")
        lines.append("")

    if diagram_text:
        lines.append(t("## 上下文图", "## Context diagram"))
        lines.append("")
        lines.append("```mermaid")
        lines.append(diagram_text.rstrip("\n"))
        lines.append("```")
        lines.append("")

    symbols = idx.symbols_of(module_id)
    if symbols:
        lines.append(t("## 符号", "## Symbols"))
        lines.append("")
        lines.append(t("| 名称 | 类型 | 行 | 签名 | 说明 |", "| Name | Kind | Line | Signature | Docs |"))
        lines.append("| --- | --- | --- | --- | --- |")
        for symbol in symbols:
            signature = (symbol.signature or "").replace("|", "\\|")
            doc = (symbol.docstring or "").splitlines()[0] if symbol.docstring else ""
            doc = doc.replace("|", "\\|")
            lines.append(f"| `{symbol.name}` | {symbol.kind} | {symbol.line_start} | `{signature}` | {doc} |")
        lines.append("")

    upstream_map, downstream_map = idx.neighbors(module_id)
    upstream = sorted(upstream_map.items())
    downstream = sorted(downstream_map.items())
    slug_map = _packet_slug_map(packet)
    if upstream or downstream:
        lines.append(t("## 上下游", "## Neighbors"))
        lines.append("")
        if upstream:
            lines.append(t("**上游**（谁用到我）", "**Upstream** (who uses me)"))
            lines.append("")
            for other, relation in upstream:
                lines.append(f"- {_link(nodes, other, slug_map)} （{relation}）")
            lines.append("")
        if downstream:
            lines.append(t("**下游**（我依赖谁）", "**Downstream** (what I depend on)"))
            lines.append("")
            for other, relation in downstream:
                lines.append(f"- {_link(nodes, other, slug_map)} （{relation}）")
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(t(f"[← 返回索引](../{INDEX_FILENAME})", f"[← Back to index](../{INDEX_FILENAME})"))
    lines.append("")
    return "\n".join(lines)


def _layer_name(packet: DataPacket, layer_id: str | None) -> str:
    if not layer_id:
        return "_unlayered"
    for layer in packet.layers:
        if layer.id == layer_id:
            return layer.name
    return layer_id.replace("layer::", "")


def _module_path(module_id: str) -> str:
    return module_id[len("mod::") :] if module_id.startswith("mod::") else module_id


def _link(nodes: dict, node_id: str, slug_map: dict[str, str] | None = None) -> str:
    node = nodes.get(node_id)
    if node and node.type == NodeType.MODULE and node.path:
        slug = (slug_map or {}).get(node.path) or _slug(node.path)
        return f"[{node.path}]({slug}.md)"
    if node and node.type == NodeType.SYMBOL and node.module_id:
        owner = nodes.get(node.module_id)
        if owner and owner.path:
            return f"`{node.name}`（{owner.path}）"
    if node_id.startswith("ext::"):
        return f"`{node_id[len('ext::') :]}`（外部）"
    return f"`{node_id}`"


__all__ = ["INDEX_FILENAME", "MODULES_SUBDIR", "render_index", "render_module_page"]
