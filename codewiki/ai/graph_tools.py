"""只读图谱工具：让 Agent 能"发现"节点，而不是猜 ``node_id``。

``query_graph`` 要求精确的节点 id（``mod::pkg/a.py``）。模型在开局并不知道仓库里
有哪些模块、哪个符号在哪 —— 没有发现手段时，它只能先 ``search_codebase`` 盲搜，再
自己拼 id，拼错就浪费一轮。这里补三件事：

* :func:`list_modules` —— 按层列出模块（按度数降序，热点在前）；
* :func:`find_symbol`  —— 按名字找符号，直接给出 ``path:line``；
* :func:`suggest_node_ids` —— ``query_graph`` 找不到节点时给"相近候选"。

三者的输入只有已落盘的数据包，**不碰文件系统**，所以不引入新的路径/密钥攻击面；
输出里的名字来自被分析仓库（不可信），由注册表统一过滤与包裹，这里不另做。
"""

from __future__ import annotations

import difflib
from typing import Any

MAX_LIST_LIMIT = 200
DEFAULT_LIST_LIMIT = 30


def _clamp(limit: int | None, default: int) -> int:
    if limit is None:
        return default
    return max(1, min(int(limit), MAX_LIST_LIMIT))


def module_degrees(packet: Any) -> dict[str, int]:
    """模块级度数（只数 ``mod::`` 之间的边）。"""
    degrees: dict[str, int] = {}
    for edge in packet.edges:
        if edge.source.startswith("mod::") and edge.target.startswith("mod::"):
            degrees[edge.source] = degrees.get(edge.source, 0) + 1
            degrees[edge.target] = degrees.get(edge.target, 0) + 1
    return degrees


def _layer_names(packet: Any) -> dict[str, str]:
    return {layer.id: layer.name for layer in packet.layers}


def ranked_modules(packet: Any) -> list[Any]:
    """模块节点：度数降序，同度按 path 排序（确定性）。"""
    degrees = module_degrees(packet)
    modules = [n for n in packet.nodes if n.type.value == "Module" and n.path]
    return sorted(modules, key=lambda n: (-degrees.get(n.id, 0), n.path or ""))


def list_modules(packet: Any, *, layer: str = "", limit: int | None = None) -> str:
    """列出模块：``id | 层 | 度数``。``layer`` 可给层 id 或层名（大小写不敏感）。"""
    cap = _clamp(limit, DEFAULT_LIST_LIMIT)
    degrees = module_degrees(packet)
    names = _layer_names(packet)
    wanted = layer.strip().lower()

    rows = []
    for node in ranked_modules(packet):
        layer_id = node.layer_id or ""
        layer_name = names.get(layer_id, layer_id)
        if wanted and wanted not in {layer_id.lower(), layer_name.lower()}:
            continue
        rows.append((node.id, layer_name or "（未分层）", degrees.get(node.id, 0)))

    if not rows:
        known = sorted(set(names.values()))
        hint = f"；已有层：{', '.join(known)}" if known else ""
        return f"（没有匹配的模块{hint}）"

    shown = rows[:cap]
    lines = [f"{mid} | {lname} | 度数 {deg}" for mid, lname, deg in shown]
    if len(rows) > cap:
        lines.append(f"…（共 {len(rows)} 个，仅列前 {cap}；用 layer 参数缩小范围）")
    return "\n".join(lines)


def find_symbol(packet: Any, name: str, *, limit: int | None = None) -> str:
    """按名字找符号（子串匹配、大小写不敏感），给出 ``sym id | 种类 | path:line``。

    精确匹配（名字或限定名完全相同）排在前面 —— 找 ``run`` 时，``run`` 比
    ``run_all`` / ``prune`` 更可能是想要的那个。
    """
    needle = name.strip().lower()
    if not needle:
        return "（name 不能为空）"
    cap = _clamp(limit, DEFAULT_LIST_LIMIT)

    exact: list[Any] = []
    partial: list[Any] = []
    for node in packet.nodes:
        if node.type.value != "Symbol":
            continue
        short = (node.name or "").lower()
        qual = (node.qualname or "").lower()
        if needle in (short, qual):
            exact.append(node)
        elif needle in short or needle in qual:
            partial.append(node)

    ordered = sorted(exact, key=lambda n: n.id) + sorted(partial, key=lambda n: n.id)
    if not ordered:
        return "（没有匹配的符号；可先用 list_modules 看模块，或 search_codebase 搜文本）"

    lines = []
    for node in ordered[:cap]:
        where = node.path or (node.module_id or "").removeprefix("mod::")
        if where and node.line_start:
            where = f"{where}:{node.line_start}"
        sig = f" | {node.signature}" if node.signature else ""
        lines.append(f"{node.id} | {node.kind or 'symbol'} | {where}{sig}")
    if len(ordered) > cap:
        lines.append(f"…（共 {len(ordered)} 个，仅列前 {cap}；给更长的名字缩小范围）")
    return "\n".join(lines)


def suggest_node_ids(packet: Any, node_id: str, *, limit: int = 5) -> list[str]:
    """给一个不存在的 id 找相近候选（``query_graph`` 的"你是不是想找"）。

    先按"路径片段/名字包含"，再用 difflib 补足 —— 模型常见的错法是漏了 ``mod::``
    前缀、写成了绝对路径，或只给了文件名。
    """
    raw = node_id.strip()
    tail = raw.split("::")[-1].lower()
    ids = [n.id for n in packet.nodes if n.type.value in ("Module", "Symbol")]

    picked: list[str] = []
    if tail:
        for nid in sorted(ids):
            if tail in nid.lower() and nid not in picked:
                picked.append(nid)
            if len(picked) >= limit:
                return picked
    for nid in difflib.get_close_matches(raw, ids, n=limit, cutoff=0.5):
        if nid not in picked:
            picked.append(nid)
    return picked[:limit]


__all__ = [
    "DEFAULT_LIST_LIMIT",
    "MAX_LIST_LIMIT",
    "find_symbol",
    "list_modules",
    "module_degrees",
    "ranked_modules",
    "suggest_node_ids",
]
