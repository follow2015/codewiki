"""增量渲染的契约（设计文稿 §数据包与增量分析）。

增量要成立，必须先回答三个问题，而且答案得是**代码**不是注释：

1. 什么算"同一份输入" —— 缓存键（见 :mod:`codewiki.file_cache`）；
2. 什么变化必须**全量重渲** —— 本模块的 ``render_inputs`` / ``changed_components``；
3. 什么时候才算"上一轮已完成" —— 本模块的 ``.incomplete`` 标记。

三个都是纯函数/纯文件操作，脱离 argparse 与文件系统都能单测 —— 这是把规则从
``_analyze`` 里搬出来的唯一理由：嵌在 200 行的命令函数里，它就只能靠端到端跑。

**判据保守**：多算一项的代价是偶尔多渲几页，漏算一项的代价是页面上留着旧结论
而没人发现 —— 后者不可接受，所以只要"渲染时读过"就进表。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from codewiki import __version__
from codewiki.config import CodewikiConfig
from codewiki.models import SCHEMA_VERSION
from codewiki.schemas import DataPacket

INCOMPLETE_MARKER = ".incomplete"
META_FIELD = "render_inputs"


def _sig(payload: object) -> str:
    """任意 JSON 可序列化结构 → 短哈希（分量签名，便于"点名哪一项变了"）。"""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def render_inputs(
    *,
    cfg: CodewikiConfig,
    packet: DataPacket,
    min_confidence: float | None = None,
    max_nodes: int | None = None,
    backends: Mapping[str, object] | None = None,
) -> dict[str, str]:
    """渲染相关的**全部**输入 → 每个分量的签名。

    曾经只有"分层 + 注解"两项，于是改了 ``min_confidence`` / ``max_nodes`` /
    工具版本后，未改动模块的页面仍被复用 —— 图上还是旧阈值的结果。

    ``min_confidence`` / ``max_nodes`` 显式传入的是**生效值**：CLI 的
    ``--min-confidence`` 只覆盖了建图、没覆盖出图与指纹，于是"参数传了、图没变、
    增量也没察觉"三者同时成立（实测：加 `--min-confidence 0.95` 后增量仍报
    "复用 8 个"，产物里还是旧阈值的图）。默认取配置值，调用方应传生效值。

    ``backends`` 是"这次实际用了哪些解析后端"：换后端（ast → regex）时模块内容
    hash 不变（hash 是**文件内容**的），但结构事实全变了 —— 不把后端组合纳入
    指纹，换后端会被判成"没有变化"，续用旧页面。

    分量粒度是刻意的：诊断要能说"是 min_confidence 从 0.5 变成 0.9"，只说
    "输入变了"等于让用户自己猜。
    """
    return {
        "tool_version": __version__,
        "schema_version": str(SCHEMA_VERSION),
        "project": _sig([cfg.project.name, cfg.project.lang]),
        "languages": _sig(
            sorted((name, tuple(spec.ext), spec.analyzer) for name, spec in cfg.languages.items())
        ),
        "layers": _sig([(layer.id, layer.name, layer.color, layer.order) for layer in packet.layers]),
        "annotations": _sig(
            [(a.target, a.label, a.note, a.source, a.reviewed) for a in packet.annotations]
        ),
        "min_confidence": f"{cfg.analysis.min_confidence if min_confidence is None else min_confidence:g}",
        "max_nodes": str(cfg.analysis.max_nodes if max_nodes is None else max_nodes),
        "backends": _sig(sorted(backends or ())),
    }


def describe_input(component: str) -> str:
    """分量名 → 人话（诊断与告警都用它，避免两处文案漂移）。"""
    return {
        "tool_version": "工具版本",
        "schema_version": "数据包格式版本",
        "project": "项目名/产物语言",
        "languages": "语言白名单",
        "layers": "分层定义",
        "annotations": "注解",
        "min_confidence": "置信度下限",
        "max_nodes": "单图节点上限",
        "backends": "解析后端",
    }.get(component, component)


def changed_components(previous: dict[str, str] | None, current: dict[str, str]) -> list[str]:
    """返回"变了的分量"的人话描述；``previous`` 为空表示没有可比对象。"""
    if not previous:
        return []

    labels: list[str] = []
    for name in sorted(current):
        before, after = previous.get(name), current[name]
        if before == after:
            continue
        readable = describe_input(name)
        if _looks_readable(before) and _looks_readable(after):
            labels.append(f"{readable} {before} → {after}")
        else:
            labels.append(f"{readable}已变化")
    for name in sorted(set(previous) - set(current)):
        labels.append(f"{describe_input(name)}已移除")
    return labels


def _looks_readable(value: str | None) -> bool:
    """短且不含哈希气味（16 位十六进制）的值可以直接展示。"""
    if value is None or len(value) > 24:
        return False
    return not (len(value) == 16 and all(c in "0123456789abcdef" for c in value))


def write_incomplete_marker(out_dir: Path) -> Path:
    """渲染前落标记：中途被杀/抛异常时它留在盘上，下一轮据此全量重渲。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    marker = out_dir / INCOMPLETE_MARKER
    marker.write_text("渲染未完成（成功结束后本文件会被删除）\n", encoding="utf-8")
    return marker


def clear_incomplete_marker(out_dir: Path) -> None:
    """全部成功后才清标记；``missing_ok`` 让重复清理不成问题。"""
    (out_dir / INCOMPLETE_MARKER).unlink(missing_ok=True)


def has_incomplete_marker(out_dir: Path) -> bool:
    return (out_dir / INCOMPLETE_MARKER).is_file()


__all__ = [
    "INCOMPLETE_MARKER",
    "META_FIELD",
    "changed_components",
    "clear_incomplete_marker",
    "describe_input",
    "has_incomplete_marker",
    "render_inputs",
    "write_incomplete_marker",
]
