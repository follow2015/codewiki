"""``merge-layers`` —— 合并**建议分层**（§4.6.3）。

为什么放在 ``graph/`` 而不是文档目录结构里写的 ``ai/merge.py``：分层建议是
**纯静态能力**（社区发现，不需要模型），放 ``ai/`` 会让"跑一次分层建议"这件事
依赖 AI 模块，与「AI 整段缺失等同关闭」的设计冲突。文档目录结构那条是早期
划分，这里按依赖方向修正。

三条规则（与注解合并同款体验：建议 → 人工审阅 → 显式合并）：

1. **绝不自动覆盖人工分层**：人工已声明的层原样保留，建议层追加到其后；
2. **paths 重叠要显式化**：与既有层 paths 重叠时给 warning 并要求人工选择，
   不静默追加（否则同一个模块被两层同时匹配，最长前缀规则会让结果难预测）；
3. **建议层带来源注释**：``# source: suggested``，便于日后回溯哪些是人定的、
   哪些是算法给的。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from codewiki.config import LayerSpec

SOURCE_MARK = "# source: suggested"


@dataclass
class LayerMergeOutcome:
    added: list[LayerSpec] = field(default_factory=list)
    overlaps: list[dict] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    changed: bool = False


def load_suggested_layers(path: Path) -> list[LayerSpec]:
    """读建议文件里的 ``[[architecture.layers]]``。"""
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return [LayerSpec.model_validate(item) for item in raw.get("architecture", {}).get("layers", [])]


def plan_merge_layers(suggested: list[LayerSpec], existing: list[LayerSpec]) -> LayerMergeOutcome:
    """算出合并计划（不写文件）。"""
    outcome = LayerMergeOutcome()
    existing_names = {layer.name for layer in existing}
    for layer in suggested:
        if layer.name in existing_names:
            outcome.skipped.append(layer.name)
            continue
        for current in existing:
            overlap = _path_overlap(layer.paths, current.paths)
            if overlap:
                outcome.overlaps.append({"name": layer.name, "with": current.name, "paths": sorted(overlap)})
        outcome.added.append(layer)
    return outcome


def _path_overlap(a: list[str], b: list[str]) -> set[str]:
    """paths 重叠判定：互为前缀、相等，或任一方是**仓库根**（``""`` / ``"."``）即重叠。

    根必须算重叠：配置里写 ``paths = ["."]`` 是常见写法，它和任何子目录都撞，
    不认这条会把"整仓一层"的既有配置当成与新建议毫无冲突。
    """
    out: set[str] = set()
    for raw_left in a:
        for raw_right in b:
            left = _normalize_path(raw_left)
            right = _normalize_path(raw_right)
            if (
                left == right
                or not left
                or not right
                or left.startswith(right + "/")
                or right.startswith(left + "/")
            ):
                out.add(raw_left)
    return out


def _normalize_path(path: str) -> str:
    """``"."`` / ``"./"`` → ``""``（仓库根）。"""
    stripped = path.strip().rstrip("/")
    return "" if stripped == "." else stripped


def render_layer_block(layer: LayerSpec) -> str:
    """渲染一个 ``[[architecture.layers]]`` 段（带来源注释）。"""
    lines = [SOURCE_MARK, "[[architecture.layers]]", f'name = "{_escape(layer.name)}"']
    lines.append("paths = [" + ", ".join(f'"{_escape(p)}"' for p in layer.paths) + "]")
    if layer.color:
        lines.append(f'color = "{_escape(layer.color)}"')
    return "\n".join(lines)


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def apply_merge_layers(config_path: Path, outcome: LayerMergeOutcome) -> bool:
    """把建议层追加到配置末尾（保留人工层不动）。"""
    if not outcome.added:
        return False
    original = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    body = original.rstrip("\n")
    if body:
        body += "\n\n"
    body += "\n\n".join(render_layer_block(layer) for layer in outcome.added) + "\n"

    tmp = config_path.with_suffix(".toml.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(config_path)
    outcome.changed = True
    return True


def merge_layers(config_path: Path, suggest_path: Path, *, dry_run: bool = False) -> LayerMergeOutcome:
    """读建议 → 计划 → 落盘。"""
    from codewiki.config import load_config

    cfg = load_config(config_path)
    suggested = load_suggested_layers(suggest_path)
    outcome = plan_merge_layers(suggested, cfg.architecture.layers)
    if not dry_run:
        if outcome.overlaps:
            outcome.changed = False
            return outcome
        apply_merge_layers(config_path, outcome)
    return outcome


def render_layer_merge_report(outcome: LayerMergeOutcome) -> str:
    lines = [
        f"新增 {len(outcome.added)} 层 / 跳过 {len(outcome.skipped)} 层 / 重叠 {len(outcome.overlaps)} 处"
    ]
    for item in outcome.overlaps:
        lines.append(
            f"  ⚠ 「{item['name']}」与既有层「{item['with']}」paths 重叠：{', '.join(item['paths'])}"
        )
        lines.append("     未写入配置：请人工决定归属后再合并")
    for name in outcome.skipped:
        lines.append(f"  ⏭ 跳过「{name}」：已存在同名层（人工层优先）")
    return "\n".join(lines)


__all__ = [
    "LayerMergeOutcome",
    "SOURCE_MARK",
    "apply_merge_layers",
    "load_suggested_layers",
    "merge_layers",
    "plan_merge_layers",
    "render_layer_block",
    "render_layer_merge_report",
]
