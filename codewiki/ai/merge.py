"""``merge-annotations`` —— 把 AI 建议合并进 ``codewiki.toml``（§5.2 第 6 条）。

合并规则（逐条对应设计，顺序即优先级）：

1. **只追加不覆盖**：AI 永远不直接改主配置（§5.4 第 7 条 人在回路）；
2. **受保护注解不可动**：``human`` 或 ``reviewed`` 的注解被建议命中时记为
   冲突/跳过，``--force`` 也不例外；
3. **同 target 同 label 同内容去重**：重复合入会让配置越滚越大；
4. **同 target 不同内容 = 冲突**：输出清单并**拒绝静默覆盖**；``--force`` 才
   覆盖，且仍保留受保护段。

写入策略：只做**文本级追加/删块**，不整体重写用户的 TOML —— 整体重写会丢注释、
重排字段、把用户手写的排版弄乱，那比冲突更让人恼火。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from codewiki.schemas import Annotation

ANNOTATIONS_HEADER = "[[annotations]]"


class MergeInputError(ValueError):
    """建议文件/主配置读不懂 —— 用户输入问题，CLI 要报人话而不是甩堆栈。"""


def _brief(exc: ValidationError) -> str:
    """pydantic 的报错面向开发者，这里只取第一条并翻译成"哪个字段错了"。"""
    first = (exc.errors() or [{}])[0]
    loc = ".".join(str(part) for part in first.get("loc", ())) or "字段"
    return f"{loc}: {first.get('msg', '校验失败')}"


def _read_toml(path: Path, *, what: str) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MergeInputError(f"{what}不存在: {path}") from exc
    except UnicodeDecodeError as exc:
        raise MergeInputError(f"{what}不是 UTF-8 文本: {path}") from exc
    except OSError as exc:
        raise MergeInputError(f"{what}读不了: {path}（{exc.strerror or exc}）") from exc
    except tomllib.TOMLDecodeError as exc:
        raise MergeInputError(f"{what}不是合法 TOML: {path} —— {exc}") from exc


def _annotations_in(raw: dict, path: Path, *, what: str) -> list[Annotation]:
    items = raw.get("annotations") or []
    if not isinstance(items, list):
        raise MergeInputError(f"{what}的 annotations 必须是数组: {path}")
    parsed: list[Annotation] = []
    for index, item in enumerate(items, start=1):
        try:
            parsed.append(Annotation.model_validate(item))
        except ValidationError as exc:
            raise MergeInputError(f"{what}第 {index} 条注解不合法: {path} —— {_brief(exc)}") from exc
    return parsed


@dataclass
class MergeOutcome:
    """合并结果（供 CLI 打印与测试断言）。"""

    added: list[Annotation] = field(default_factory=list)
    deduped: list[str] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)
    changed: bool = False


def load_suggestions(path: Path) -> list[Annotation]:
    """读建议文件里的 ``[[annotations]]``。

    畸形 TOML / 缺必填字段都收敛成 ``MergeInputError``：这是用户输入问题，
    不该让 tomllib 或 pydantic 的堆栈直接出现在终端上。
    """
    return _annotations_in(_read_toml(path, what="建议文件"), path, what="建议文件")


def _split_blocks(text: str) -> tuple[str, list[str]]:
    """把配置文本拆成（非注解前缀, 各 ``[[annotations]]`` 块）。"""
    parts = text.split(ANNOTATIONS_HEADER)
    return parts[0], [ANNOTATIONS_HEADER + part for part in parts[1:]]


def _block_target(block: str) -> str | None:
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("target"):
            value = stripped.split("=", 1)[1].strip().strip('"')
            return value
    return None


def existing_annotations(config_path: Path) -> list[Annotation]:
    """读主配置里已有的注解（文件不存在则为空）。"""
    if not config_path.exists():
        return []
    return _annotations_in(_read_toml(config_path, what="主配置"), config_path, what="主配置")


def plan_merge(
    suggestions: list[Annotation],
    existing: list[Annotation],
    *,
    force: bool = False,
) -> MergeOutcome:
    """算出合并计划（**不写文件**，便于 dry-run 与测试）。"""
    outcome = MergeOutcome()
    by_target: dict[str, list[Annotation]] = {}
    for item in existing:
        by_target.setdefault(item.target, []).append(item)

    for suggestion in suggestions:
        current = by_target.get(suggestion.target)
        if not current:
            outcome.added.append(suggestion)
            by_target[suggestion.target] = [suggestion]
            continue

        protected = [a for a in current if a.source == "human" or a.reviewed]
        if protected:
            # 规则 2：受保护注解 --force 也不动
            outcome.protected.append(suggestion.target)
            continue

        same = [
            a
            for a in current
            if (a.label or "") == (suggestion.label or "") and a.note.strip() == suggestion.note.strip()
        ]
        if same:
            outcome.deduped.append(suggestion.target)
            continue

        if not force:
            outcome.conflicts.append(
                {
                    "target": suggestion.target,
                    "existing_note": current[0].note,
                    "incoming_note": suggestion.note,
                }
            )
            continue

        outcome.added.append(suggestion)  # force：以建议为准
        outcome.conflicts.append(
            {
                "target": suggestion.target,
                "existing_note": current[0].note,
                "incoming_note": suggestion.note,
                "resolved": "force",
            }
        )
    return outcome


def apply_merge(config_path: Path, outcome: MergeOutcome) -> bool:
    """把计划落到 ``codewiki.toml``（原子化）；返回是否改动。

    ``--force`` 覆盖掉的旧块会被**整块删除**再追加新块（避免同一 target 两条）。
    """
    if not outcome.added:
        return False

    original = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    prefix, blocks = _split_blocks(original)
    overridden = {item.target for item in outcome.added}
    kept = [block for block in blocks if _block_target(block) not in overridden]

    renderer = _render_annotation
    body = prefix.rstrip("\n")
    if body and not body.endswith("\n\n"):
        body += "\n"
    body += "".join(kept).rstrip("\n")
    if body:
        body += "\n\n"
    body += "\n\n".join(renderer(item) for item in outcome.added) + "\n"

    tmp = config_path.with_suffix(".toml.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(config_path)
    outcome.changed = True
    return True


def _render_annotation(item: Annotation) -> str:
    from codewiki.ai.annotate import _toml_string

    lines = [ANNOTATIONS_HEADER, f"target = {_toml_string(item.target)}"]
    if item.label:
        lines.append(f"label = {_toml_string(item.label)}")
    lines.append(f"note = {_toml_string(item.note)}")
    lines.append(f'source = "{item.source}"')
    lines.append(f"reviewed = {'true' if item.reviewed else 'false'}")
    return "\n".join(lines)


def merge_annotations(
    config_path: Path,
    suggest_path: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> MergeOutcome:
    """读建议 → 计划 → 落盘（``dry_run`` 时不写）。"""
    suggestions = load_suggestions(suggest_path)
    existing = existing_annotations(config_path)
    outcome = plan_merge(suggestions, existing, force=force)
    if not dry_run:
        apply_merge(config_path, outcome)
    return outcome


def render_merge_report(outcome: MergeOutcome) -> str:
    """把合并结果渲染成人能直接读的摘要（冲突必须**看得见**）。"""
    lines = [
        f"新增 {len(outcome.added)} 条 / 去重 {len(outcome.deduped)} 条 / "
        f"跳过(受保护) {len(outcome.protected)} 条 / 冲突 {len(outcome.conflicts)} 条"
    ]
    for item in outcome.conflicts:
        lines.append(
            f"  ⚠ 冲突 {item['target']}：已存在「{item['existing_note'][:40]}…」，建议为「{item['incoming_note'][:40]}…」"
        )
        lines.append("     未自动覆盖；确认无误后加 --force 重跑")
    for target in outcome.protected:
        lines.append(f"  ⏭ 跳过 {target}：已有人工/已审阅注解（受保护，--force 也不动）")
    return "\n".join(lines)


__all__ = [
    "MergeInputError",
    "MergeOutcome",
    "apply_merge",
    "existing_annotations",
    "load_suggestions",
    "merge_annotations",
    "plan_merge",
    "render_merge_report",
]
