"""Graphify 图谱导入适配器（§4.6.4 / §9.0）。

定位：**只读、单向** —— 读 IPIP 仓 ``graphify-out/<日期>/graph.json``，映射成
我们的统一数据包；本仓不引入 graphify 代码，也不把产物写回去。

两条纪律：

1. **版本探测 + 不兼容即报错**：graphify 的节点/边字段会随版本变化，遇到不认识
   的形状必须明确失败（``GraphifyFormatError``），绝不能"按猜的字段接着跑" ——
   静默映射一半的图谱比报错危险得多（下游会把缺失当成"这个仓库没有依赖"）；
2. **只导入能确定的部分**：``relation`` 映射到我们已定义的边类型；
   认不出的 relation 记诊断并跳过（不硬塞成 IMPORTS）。

关于"外部图谱不依赖其运行时"：本模块只依赖 JSON 文本，**不 import graphify**，
因此 codewiki 可以独立安装与运行。
"""
from __future__ import annotations

import json
from pathlib import Path

from codewiki.models import CONF_DYNAMIC, CONF_STATIC, Edge, EdgeType, Node, NodeType, Provenance
from codewiki.packetio import packet_from_parts
from codewiki.schemas import DataPacket, Diagnostic

BACKEND = "graphify"

RELATION_MAP = {
    "calls": EdgeType.CALLS,
    "imports": EdgeType.IMPORTS,
    "imports_from": EdgeType.IMPORTS,
    "re_exports": EdgeType.IMPORTS,
    "inherits": EdgeType.INHERITS,
    "instantiates": EdgeType.CALLS,
    "defines": EdgeType.CONTAINS,
    "contains": EdgeType.CONTAINS,
    "uses": EdgeType.USES_DYNAMIC,
}

LOW_CONFIDENCE_RELATIONS = {
    "references": EdgeType.USES_DYNAMIC,
    "indirect_call": EdgeType.USES_DYNAMIC,
    "dynamic_import": EdgeType.USES_DYNAMIC,
}

CONFIDENCE_MAP = {
    "EXTRACTED": CONF_STATIC,
    "RESOLVED": CONF_STATIC,
    "INFERRED": 0.5,
    "GUESSED": 0.4,
}


class GraphifyFormatError(Exception):
    """图谱文件不是能识别的 graphify 格式（含原因，便于用户处理）。"""


def _line_of(location: str | None) -> int | None:
    """``"L708"`` → 708。"""
    if not location:
        return None
    try:
        return int(str(location).lstrip("L"))
    except ValueError:
        return None


def _require(payload: dict, key: str, *, where: str) -> list:
    value = payload.get(key)
    if not isinstance(value, list):
        raise GraphifyFormatError(f"{where}：缺少 `{key}` 数组（不是 graphify 图谱？）")
    return value


def load_graphify(path: Path) -> tuple[DataPacket, list[Diagnostic]]:
    """读 graphify 图谱 →（数据包, 诊断）。

    Raises:
        GraphifyFormatError: 顶层结构不符（缺 nodes / links，或节点缺 id）。
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GraphifyFormatError(f"无法读取图谱 {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise GraphifyFormatError(f"{path}：顶层不是对象")

    raw_nodes = _require(payload, "nodes", where=str(path))
    raw_links = payload.get("links")
    if raw_links is None:
        raise GraphifyFormatError(f"{path}：缺少 `links` 数组（graphify 的边在 links 而不是 edges）")
    if not isinstance(raw_links, list):
        raise GraphifyFormatError(f"{path}：`links` 不是数组")

    diagnostics: list[Diagnostic] = []
    nodes: list[Node] = []
    modules: dict[str, Node] = {}

    for raw in raw_nodes:
        if not isinstance(raw, dict) or "id" not in raw:
            raise GraphifyFormatError(f"{path}：存在缺 `id` 的节点（格式不符，拒绝猜测）")
        source_file = raw.get("source_file")
        module_id: str | None = None
        if source_file:
            module_id = f"mod::{source_file}"
            if module_id not in modules:
                modules[module_id] = Node(
                    id=module_id,
                    type=NodeType.MODULE,
                    path=str(source_file),
                    name=str(source_file),
                    language=_language_of(str(source_file)),
                )
                nodes.append(modules[module_id])

        label = str(raw.get("label") or raw.get("id"))
        nodes.append(
            Node(
                id=f"sym::{raw['id']}",
                type=NodeType.SYMBOL,
                name=label,
                qualname=f"{source_file}::{label}" if source_file else label,
                kind=_kind_of(raw),
                module_id=module_id,
                line_start=_line_of(raw.get("source_location")),
                language=_language_of(str(source_file)) if source_file else None,
            )
        )

    edges: list[Edge] = []
    skipped: set[str] = set()
    known = {n.id for n in nodes}
    for raw in raw_links:
        if not isinstance(raw, dict):
            continue
        relation = str(raw.get("relation") or "")
        edge_type = RELATION_MAP.get(relation)
        low_confidence = False
        if edge_type is None:
            edge_type = LOW_CONFIDENCE_RELATIONS.get(relation)
            low_confidence = edge_type is not None
        if edge_type is None:
            skipped.add(relation or "<空>")
            continue
        source = f"sym::{raw.get('source')}"
        target = f"sym::{raw.get('target')}"
        if source not in known or target not in known:
            continue
        confidence = CONFIDENCE_MAP.get(str(raw.get("confidence") or ""), CONF_STATIC)
        if low_confidence:
            confidence = min(float(confidence), CONF_DYNAMIC)
        edges.append(
            Edge(
                source=source,
                target=target,
                type=edge_type,
                confidence=float(confidence),
                weight=int(raw.get("weight") or 1),
                evidence=str(raw.get("source_location") or "") or None,
            )
        )

    if skipped:
        diagnostics.append(
            Diagnostic(
                level="info",
                code="GRAPHIFY_RELATION_SKIPPED",
                message=f"以下 relation 未映射（已跳过，未硬塞成 IMPORTS）: {', '.join(sorted(skipped))}",
                where=str(path),
            )
        )

    packet = packet_from_parts(
        title=f"graphify:{Path(path).parent.name or 'graph'}",
        layers=[],
        nodes=nodes,
        edges=edges,
        annotations=[],
        diagnostics=[],
        provenance={BACKEND: Provenance(backend=BACKEND, version=str(payload.get("built_at_commit") or "unknown"))},
    )
    return packet, diagnostics


def _kind_of(raw: dict) -> str:
    if raw.get("_callable_class"):
        return "class"
    if raw.get("_callable"):
        return "function"
    return "symbol"


def _language_of(path: str) -> str | None:
    from codewiki.repo import language_of

    return language_of(path)


__all__ = ["BACKEND", "GraphifyFormatError", "load_graphify"]
