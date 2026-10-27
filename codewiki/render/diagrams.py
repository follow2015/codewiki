"""图的编排与落盘（§4.5 / §4.3）。

流程：渲染 → **L1 校验门** → 落盘。

失败语义（§4.3，两种截然不同）：

- **静态确定性图**（宏观图 / 模块上下文图）校验失败 = **工具缺陷** ⇒ 记 error
  并**中断整轮**，一张坏图都不落盘（防止把渲染不了的图发到用户手里）；
- AI 生成的图才走"回喂自修一次 → 退回纯文本"（阶段二实现）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from codewiki.graph.index import build_index
from codewiki.models import NodeType
from codewiki.render.mermaid_context import render_module_context
from codewiki.render.mermaid_macro import render_system_context
from codewiki.render.mermaid_validator import validate_l1
from codewiki.schemas import DataPacket, Diagnostic

DIAGRAM_SUBDIR = "diagrams"
SYSTEM_DIAGRAM = "system-context.mmd"
SYSTEM_STEM = "system-context"
SYSTEM_INDEX = "system-context-index.md"
MODULE_SUBDIR = "modules"


@dataclass
class DiagramResult:
    """本轮出图的结论。"""

    files: list[Path] = None  # type: ignore[assignment]
    diagnostics: list[Diagnostic] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.files = self.files or []
        self.diagnostics = self.diagnostics or []


def render_all(
    packet: DataPacket,
    out_dir: Path,
    *,
    min_confidence: float = 0.5,
    max_nodes: int = 300,
    only_modules: set[str] | None = None,
) -> DiagramResult:
    """渲染两类图并落盘；任一静态图校验失败即中断（返回已记的 error）。

    ``only_modules`` 用于**增量渲染**：只重渲集合中模块的上下文图，其余**保留
    上一轮已落盘的文件**（缺失时才补渲）。宏观/分包图反映全局，总是重渲。
    """
    result = DiagramResult()
    system_text = render_system_context(packet, min_confidence=min_confidence)

    issues = validate_l1(system_text)
    if issues:
        result.diagnostics.append(
            Diagnostic(
                level="error",
                code="DIAGRAM_INVALID",
                message="宏观架构图未通过 L1 校验，已放弃出图: "
                + "; ".join(f"{i.code}@{i.message}" for i in issues),
                where=SYSTEM_DIAGRAM,
            )
        )
        return result

    diagrams_dir = out_dir / DIAGRAM_SUBDIR
    diagrams_dir.mkdir(parents=True, exist_ok=True)

    module_nodes = [n for n in packet.nodes if n.type == NodeType.MODULE]
    if _count_nodes(system_text) > max_nodes and len(module_nodes) > 1:
        pages = _paginate(packet, max_nodes)
        for index, subset in enumerate(pages, start=1):
            text = render_system_context(packet, min_confidence=min_confidence, subset=subset)
            problems = validate_l1(text)
            if problems:
                result.diagnostics.append(
                    Diagnostic(
                        level="error",
                        code="DIAGRAM_INVALID",
                        message=f"分包图 {index} 未通过 L1 校验，已放弃出图: "
                        + "; ".join(f"{i.code}@{i.message}" for i in problems),
                        where=f"{SYSTEM_STEM}-part-{index}",
                    )
                )
                return result
            target = diagrams_dir / f"{SYSTEM_STEM}-part-{index}.mmd"
            target.write_text(text, encoding="utf-8")
            result.files.append(target)

        index_path = diagrams_dir / SYSTEM_INDEX
        index_path.write_text(render_system_index(packet, pages, max_nodes=max_nodes), encoding="utf-8")
        result.files.append(index_path)
        stale_primary = diagrams_dir / SYSTEM_DIAGRAM
        if stale_primary.exists():
            stale_primary.unlink()
        result.diagnostics.append(
            Diagnostic(
                level="info",
                code="DIAGRAM_PAGED",
                message=f"宏观图节点数超过 max_nodes={max_nodes}：已按层拆成 {len(pages)} 页（见 {SYSTEM_INDEX}）",
                where=SYSTEM_DIAGRAM,
            )
        )
    else:
        system_path = diagrams_dir / SYSTEM_DIAGRAM
        system_path.write_text(system_text, encoding="utf-8")
        result.files.append(system_path)
        for stale in diagrams_dir.glob(f"{SYSTEM_STEM}-part-*.mmd"):
            stale.unlink()
        stale_index = diagrams_dir / SYSTEM_INDEX
        if stale_index.exists():
            stale_index.unlink()

    modules_dir = diagrams_dir / MODULE_SUBDIR
    index = build_index(packet)
    slug_map = build_slug_map([n.path for n in packet.nodes if n.type == NodeType.MODULE and n.path])
    for node in packet.nodes:
        if node.type != NodeType.MODULE or not node.path:
            continue
        target = modules_dir / f"{slug_map[node.path]}.mmd"
        if only_modules is not None and node.id not in only_modules and target.exists():
            continue
        text = render_module_context(packet, node.id, min_confidence=min_confidence, index=index)
        problems = validate_l1(text)
        if problems:
            result.diagnostics.append(
                Diagnostic(
                    level="error",
                    code="DIAGRAM_INVALID",
                    message="模块上下文图未通过 L1 校验，已放弃出图: "
                    + "; ".join(f"{i.code}@{i.message}" for i in problems),
                    where=node.path,
                )
            )
            return result

        modules_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        result.files.append(target)

    return result


def _paginate(packet: DataPacket, max_nodes: int) -> list[set[str]]:
    """按"层顺序 → 层内路径"切成每页不超过 ``max_nodes`` 个模块的页。

    为什么不按边数或度数切：**层**是用户能理解的分块单位，按层切出来的每页
    都是"某几个层的全景"，而不是一堆随机模块。
    """
    order = {layer.id: (layer.order if layer.order is not None else 10_000) for layer in packet.layers}
    modules = sorted(
        (n for n in packet.nodes if n.type == NodeType.MODULE),
        key=lambda n: (order.get(n.layer_id or "", 10_000), n.path or n.id),
    )
    pages: list[set[str]] = []
    current: set[str] = set()
    for module in modules:
        current.add(module.id)
        if len(current) >= max_nodes:
            pages.append(current)
            current = set()
    if current:
        pages.append(current)
    return pages


def render_system_index(packet: DataPacket, pages: list[set[str]], *, max_nodes: int) -> str:
    """分包索引页：每页给出链接与模块清单（上钻到模块上下文图）。"""
    paths = {n.id: (n.path or n.id) for n in packet.nodes}
    slug_map = build_slug_map([n.path for n in packet.nodes if n.type == NodeType.MODULE and n.path])
    lines = [
        "# 宏观架构图（分页）",
        "",
        f"节点数超过单图上限 {max_nodes}，已按层拆成 {len(pages)} 页。",
        "",
    ]
    for index, subset in enumerate(pages, start=1):
        lines.append(f"## 第 {index} 页 — `{SYSTEM_STEM}-part-{index}.mmd`")
        lines.append("")
        for node_id in sorted(subset):
            path = paths.get(node_id, node_id)
            slug = slug_map.get(path, _slug(path))
            lines.append(f"- `{path}`（下钻：[{slug}.mmd](modules/{slug}.mmd)）")
        lines.append("")
    return "\n".join(lines)


_UNSAFE_SLUG = re.compile(r"[\"'<>&\s#]")


def _slug(path: str) -> str:
    """模块路径 → 文件名（斜杠与扩展名都去掉点号，避免 Windows/URL 麻烦）。

    点文件（``.env``）此前会产出空串 stem —— 所有点文件共写一个页面
    （IPIP 实测 3 组 9 页互覆）。stem 为空时改用完整路径并加 ``_`` 前缀
    （防隐藏文件名）；残余碰撞（``a-b.py`` vs ``a/b.py``）由
    :func:`build_slug_map` 统一消歧。
    """
    name = path.rsplit("/", 1)[-1]
    stem = path.rsplit(".", 1)[0] if "." in name and name.rsplit(".", 1)[0] else path
    slug = _UNSAFE_SLUG.sub("_", stem.replace("/", "_").replace("-", "_"))
    if not slug:
        slug = "_" + _UNSAFE_SLUG.sub("_", path.replace("/", "_").replace("-", "_"))
    if slug.startswith("."):
        slug = "_" + slug
    return slug


def build_slug_map(paths: list[str]) -> dict[str, str]:
    """一组模块路径 → 唯一页面名（确定性，增量渲染依赖两轮结果一致）。

    首个碰撞者保留简单 slug，其余追加 ``~<sha1_8>``；同一输入集合（不管遍历
    顺序）产出同一映射。调用方（wiki 页面文件名 / index 链接 / 页内互链 /
    模块上下文图）必须**同源**使用这一个 map，否则链接指到不存在的页。
    """
    import hashlib

    mapping: dict[str, str] = {}
    used: dict[str, str] = {}
    for path in sorted(paths):
        slug = _slug(path)
        owner = used.get(slug)
        if owner is None or owner == path:
            used.setdefault(slug, path)
            mapping[path] = slug
            continue
        digest = hashlib.sha1(path.encode("utf-8")).hexdigest()[:8]
        candidate = f"{slug}~{digest}"
        while candidate in used:
            candidate = f"{candidate}~"
        used[candidate] = path
        mapping[path] = candidate
    return mapping


def _count_nodes(text: str) -> int:
    """粗略统计节点数（用于 max_nodes 提示）。"""
    import re

    return len(set(re.findall(r"\bn[0-9a-f]{8}\b", text)))


__all__ = [
    "DIAGRAM_SUBDIR",
    "DiagramResult",
    "MODULE_SUBDIR",
    "SYSTEM_DIAGRAM",
    "SYSTEM_INDEX",
    "SYSTEM_STEM",
    "render_all",
    "render_system_index",
]
