"""模块级上下游邻居（喂给标注 Prompt 的"它在链路里的位置"）。

只给一个文件的内容，模型只能看出"这个模块自己干什么"，看不出**谁在用它**、
**它在用谁** —— 于是注解容易写成"这是一个工具模块"这类没有信息量的话。把模块级
import 关系一并给它，才有判断依据（例如"被 app 与 api 同时依赖，改动影响面大"）。

三条约束：

1. **只用已落盘的数据包**：不读文件系统，不引入新的路径/密钥攻击面；
2. **确定性**：排序 + 去重，同一份数据包两次渲染必须逐字节相同 —— 否则缓存键
   每次都变，等于缓存从不命中；
3. **截断要显式**：热点模块的上游可能有上百个，截断数量必须写在文本里，
   不能让模型以为"就这几个"。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

MAX_NEIGHBORS_PER_DIRECTION = 12


def _one_line(text: str) -> str:
    """压成单行：路径里若含换行（POSIX 允许），它能伪造出一行"邻居"。"""
    return " ".join(str(text).split())


@dataclass(frozen=True)
class NeighborContext:
    """一个模块的一跳邻居。``digest`` 进缓存键。"""

    module_id: str
    downstream: tuple[str, ...] = ()
    upstream: tuple[str, ...] = ()
    omitted: tuple[tuple[str, int], ...] = ()
    digest: str = ""

    def is_empty(self) -> bool:
        return not self.downstream and not self.upstream


def _collect(packet, module_id: str) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    downstream: dict[str, set[str]] = {}
    upstream: dict[str, set[str]] = {}
    for edge in getattr(packet, "edges", []) or []:
        source = str(getattr(edge, "source", "") or "")
        target = str(getattr(edge, "target", "") or "")
        if not (source.startswith("mod::") and target.startswith("mod::")):
            continue
        edge_type = str(getattr(edge, "type", "") or "")
        if source == module_id and target != module_id:
            downstream.setdefault(target.removeprefix("mod::"), set()).add(edge_type)
        elif target == module_id and source != module_id:
            upstream.setdefault(source.removeprefix("mod::"), set()).add(edge_type)
    return downstream, upstream


def _render_row(path: str, types: set[str]) -> str:
    labels = ", ".join(sorted(t for t in types if t)) or "IMPORTS"
    return f"{_one_line(path)}（{labels}）"


def render_neighbors(context: NeighborContext) -> str:
    """渲染成 Prompt 里的一段（空的方向显式写"无"，不留空白让人猜）。"""
    lines = []
    for label, rows in (
        ("下游（本模块依赖的）", context.downstream),
        ("上游（依赖本模块的）", context.upstream),
    ):
        if rows:
            lines.append(f"{label}：")
            lines.extend(f"- {row}" for row in rows)
        else:
            lines.append(f"{label}：无（没有模块级出边/入边）")
    for direction, count in context.omitted:
        lines.append(
            f"- …（{direction}另有 {count} 个未列出，按路径排序取前 {MAX_NEIGHBORS_PER_DIRECTION} 个）"
        )
    return "\n".join(lines)


def module_neighbors(packet, module_id: str, *, limit: int = MAX_NEIGHBORS_PER_DIRECTION) -> NeighborContext:
    """取一个模块的一跳上下游（排序、去重、带上限）。"""
    downstream, upstream = _collect(packet, module_id)
    omitted: list[tuple[str, int]] = []
    rendered: dict[str, tuple[str, ...]] = {}
    for name, raw in (("下游", downstream), ("上游", upstream)):
        ordered = sorted(raw.items())
        kept = ordered[: max(0, int(limit))]
        rendered[name] = tuple(_render_row(path, types) for path, types in kept)
        if len(ordered) > len(kept):
            omitted.append((name, len(ordered) - len(kept)))

    context = NeighborContext(
        module_id=module_id,
        downstream=rendered["下游"],
        upstream=rendered["上游"],
        omitted=tuple(omitted),
    )
    text = render_neighbors(context)
    digest = hashlib.sha256(f"{module_id}\n{text}".encode()).hexdigest()
    return NeighborContext(
        module_id=context.module_id,
        downstream=context.downstream,
        upstream=context.upstream,
        omitted=context.omitted,
        digest=digest,
    )


__all__ = [
    "MAX_NEIGHBORS_PER_DIRECTION",
    "NeighborContext",
    "module_neighbors",
    "render_neighbors",
]
