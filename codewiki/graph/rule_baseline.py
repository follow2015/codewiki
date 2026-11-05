"""规则基线：把"存量违例"记成一本可 diff 的账，好让新违例独立报错。

为什么需要它：一个成熟仓库第一次接规则时，几乎总有一批存量违例。没有基线，
规则一上线就全红，团队只能把规则删掉 —— 基线把"存量"和"新增"分开，让规则
可以**先落地、再收缩**。

三条设计决定：

1. **身份不含行号、不含层名**：``(rule_id, src, dst, edge_type)``。行号随无关改动
   漂移；层名会重命名。任何一项进了身份，基线都会天天失效，结局等于没有基线。
2. **坏基线是错误，不是空基线**：把损坏的文件当空基线，会让全部存量突然变成
   "新增"，一次刷出几百条 —— 用户唯一能做的事就是重写基线，从此不再看它。
3. **写基线是显式动作**，且写之前把"记 N 条 / 新增 M / 已消失 K"打印出来：
   基线是"我现在接受这些"，不该被隐式执行。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from codewiki.clock import now_iso
from codewiki.graph.rules import Violation

BASELINE_SCHEMA_VERSION = 1


class BaselineError(Exception):
    """基线文件不可用（损坏 / 版本过新 / 结构不符）。"""


@dataclass(frozen=True)
class BaselineEntry:
    rule_id: str
    src: str
    dst: str
    edge: str

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return (self.rule_id, self.src, self.dst, self.edge)


@dataclass
class Baseline:
    """一本基线账。``created_at`` 只用于人读，不参与身份。"""

    schema_version: int = BASELINE_SCHEMA_VERSION
    created_at: str = ""
    entries: list[BaselineEntry] = field(default_factory=list)

    def identities(self) -> set[tuple[str, str, str, str]]:
        return {entry.identity for entry in self.entries}


@dataclass(frozen=True)
class BaselineWriteResult:
    total: int
    added: int
    removed: int
    path: Path


@dataclass
class BaselineSplit:
    """当前违例按基线切开的结果。"""

    new: list[Violation] = field(default_factory=list)
    skipped: int = 0
    stale: list[BaselineEntry] = field(default_factory=list)


def load_baseline(path: Path) -> Baseline:
    """读基线。文件不存在 ⇒ 空基线（首次使用）；坏了 ⇒ :class:`BaselineError`。"""
    if not path.is_file():
        return Baseline()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BaselineError(f"基线文件读不出来或不是合法 JSON：{exc}") from exc

    if not isinstance(raw, dict):
        raise BaselineError("基线文件顶层应当是对象")

    version = raw.get("schema_version", 0)
    if not isinstance(version, int) or version > BASELINE_SCHEMA_VERSION:
        raise BaselineError(
            f"基线 schema 版本 {version!r} 比当前工具支持的 {BASELINE_SCHEMA_VERSION} 新，"
            "请升级 codewiki（不降级解析：会静默丢掉不认识的条目）"
        )

    entries: list[BaselineEntry] = []
    for index, item in enumerate(raw.get("violations") or [], start=1):
        try:
            entries.append(
                BaselineEntry(
                    rule_id=str(item["rule_id"]),
                    src=str(item["src"]),
                    dst=str(item["dst"]),
                    edge=str(item["edge"]),
                )
            )
        except (KeyError, TypeError) as exc:
            raise BaselineError(f"基线第 {index} 条缺少字段（需要 rule_id/src/dst/edge）：{exc}") from exc

    return Baseline(
        schema_version=version,
        created_at=str(raw.get("created_at") or ""),
        entries=entries,
    )


def write_baseline(
    path: Path, violations: list[Violation], *, created_at: str | None = None
) -> BaselineWriteResult:
    """把当前**全部**违例写成基线（排序后写，保证文件可 diff）。

    返回的统计是"相对旧基线"的：新增多少、消失了多少 —— 写之前就能看出这次
    收缩/扩张了多少，不必自己拿两个文件做 diff。
    """
    previous = load_baseline(path).identities()
    entries = [BaselineEntry(rule_id=v.rule_id, src=v.src, dst=v.dst, edge=v.edge_type) for v in violations]
    unique: dict[tuple[str, str, str, str], BaselineEntry] = {e.identity: e for e in entries}
    ordered = [unique[key] for key in sorted(unique)]

    payload = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "created_at": created_at or now_iso(),
        "violations": [{"rule_id": e.rule_id, "src": e.src, "dst": e.dst, "edge": e.edge} for e in ordered],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    current = {e.identity for e in ordered}
    return BaselineWriteResult(
        total=len(ordered),
        added=len(current - previous),
        removed=len(previous - current),
        path=path,
    )


def split_by_baseline(violations: list[Violation], baseline: Baseline) -> BaselineSplit:
    """把当前违例切成"新增 / 存量 / 已失效的基线条目"。"""
    known = baseline.identities()
    split = BaselineSplit()
    for violation in violations:
        if violation.identity in known:
            split.skipped += 1
        else:
            split.new.append(violation)

    current = {v.identity for v in violations}
    split.stale = [entry for entry in baseline.entries if entry.identity not in current]
    return split


__all__ = [
    "BASELINE_SCHEMA_VERSION",
    "Baseline",
    "BaselineEntry",
    "BaselineError",
    "BaselineSplit",
    "BaselineWriteResult",
    "load_baseline",
    "split_by_baseline",
    "write_baseline",
]
