"""架构趋势：``history.jsonl`` —— 每轮 analyze 追加一行架构快照。

**为什么落在被分析仓库的 ``.codewiki/`` 而不是产物目录**：``data/codewiki.json``
描述的是"仓库长什么样"，历史描述的是"这些轮次各长什么样"——后者是**运行记录**，
与缓存命中数同类。混进归档物会立刻打破 ``test_incremental_equals_full`` 那条
逐字节验收：增量目录跑过 N 轮有 N 行、全新目录只跑 1 轮有 1 行，行数天然不同，
而那条验收的前提正是"给定相同输入产生相同产物"。``CACHE_STATS`` 踩过同一个坑。

**每一行必须是产物的函数**（计划 §9 的判据：同一 commit 跑两次，两行除
``generated_at`` 外逐字段相同）。所以这里刻意不记：缓存命中、耗时、绝对路径、
任何 set 的迭代序。层名显式排序 —— 配置里调换层声明顺序不算"层变了"。

**坏行为什么跳过而不是报错**：与 ``rule_baseline`` 的取舍相反，且是有意的 ——
基线是裁决的输入（判成空基线会把存量一次刷成几百条新增），历史只是展示
（一行写坏不该让整条趋势消失）。但跳过数必须返回出去：静默丢行会让"趋势断了
一截"看起来像"架构一直没变"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from codewiki.graph.analysis import find_layer_cycles, find_module_cycles
from codewiki.models import EdgeType, NodeType
from codewiki.schemas import DataPacket

HISTORY_DIRNAME = ".codewiki"
HISTORY_FILENAME = "history.jsonl"
HISTORY_SCHEMA_VERSION = 1

_GITIGNORE = "*\n"

RULE_VIOLATION_CODE = "RULE_VIOLATION"
_LEVELS = ("error", "warning", "info")


@dataclass(frozen=True)
class HistoryEntry:
    """一行历史。字段全部由数据包（与文件数）决定，不含运行期噪声。"""

    schema: int
    generated_at: str
    repo_commit: str | None
    tool_version: str
    counts: dict[str, int]
    diagnostics: dict[str, int]
    violations: dict[str, int]
    layer_names: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "generated_at": self.generated_at,
            "repo_commit": self.repo_commit,
            "tool_version": self.tool_version,
            "counts": dict(self.counts),
            "diagnostics": dict(self.diagnostics),
            "violations": dict(self.violations),
            "layer_names": list(self.layer_names),
        }

    def to_line(self) -> str:
        """一行 JSON，无换行：值里出现换行会把下一条顶到新行。"""
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict) -> HistoryEntry:
        return cls(
            schema=int(data["schema"]),
            generated_at=str(data["generated_at"]),
            repo_commit=data["repo_commit"],
            tool_version=str(data["tool_version"]),
            counts={str(k): int(v) for k, v in data["counts"].items()},
            diagnostics={str(k): int(v) for k, v in data["diagnostics"].items()},
            violations={str(k): int(v) for k, v in data["violations"].items()},
            layer_names=tuple(str(name) for name in data["layer_names"]),
        )


def build_entry(packet: DataPacket, *, files: int) -> HistoryEntry:
    """数据包 → 一行历史。

    ``files`` 由调用方给（``len(models)``）：解析失败或后端缺失的文件不在
    ``packet.nodes`` 里，用模块数代替会**悄悄瞒报**这类退化。
    """
    modules = [node for node in packet.nodes if node.type == NodeType.MODULE]
    layer_edges = [
        edge for edge in packet.edges if edge.type == EdgeType.LAYER_DEPENDS and not edge.type_only
    ]
    counts = {
        "files": files,
        "modules": len(modules),
        "edges": len(packet.edges),
        "layers": len(packet.layers),
        "layer_edges": len(layer_edges),
        "layer_cycles": len(find_layer_cycles(packet)),
        "module_cycles": len(find_module_cycles(packet)),
    }
    diagnostics = {level: 0 for level in _LEVELS}
    violations = {"error": 0, "warning": 0}
    for diagnostic in packet.diagnostics:
        if diagnostic.level in diagnostics:
            diagnostics[diagnostic.level] += 1
        if diagnostic.code == RULE_VIOLATION_CODE and diagnostic.level in violations:
            violations[diagnostic.level] += 1
    return HistoryEntry(
        schema=HISTORY_SCHEMA_VERSION,
        generated_at=packet.meta.generated_at,
        repo_commit=packet.meta.repo_commit,
        tool_version=packet.meta.tool_version,
        counts=counts,
        diagnostics=diagnostics,
        violations=violations,
        layer_names=tuple(sorted(layer.name for layer in packet.layers)),
    )


def default_path(root: str | Path) -> Path:
    """默认历史文件：``<repo>/.codewiki/history.jsonl``（自带忽略规则）。"""
    return Path(root).resolve() / HISTORY_DIRNAME / HISTORY_FILENAME


def append(path: Path, entry: HistoryEntry) -> int:
    """追加一行，返回写完后文件里的**总行数**。

    "这一轮跑过吗"以后要能查，所以重复状态也照写、不做幂等去重 —— 去重等于把
    "跑了几次"这件事从流水里删掉，而流水正是这个文件的全部意义。
    """
    _ensure_dir(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(entry.to_line() + "\n")
    return _count_lines(path)


def read(path: Path, *, limit: int | None = None) -> tuple[list[HistoryEntry], int]:
    """读历史（旧 → 新）。返回 ``(条目, 跳过行数)``；文件不存在 ⇒ 空历史。

    ``limit`` 取**最新**的若干条（趋势图只需要最近这一段）。schema 比本版本新的行
    也跳过：读个半懂的行比没有这一行更危险 —— 趋势会把"我不认识的字段"当成"没变"。
    """
    if not path.exists():
        return [], 0
    entries: list[HistoryEntry] = []
    skipped = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                data = json.loads(text)
                if int(data["schema"]) > HISTORY_SCHEMA_VERSION:
                    skipped += 1
                    continue
                entries.append(HistoryEntry.from_dict(data))
            except (ValueError, KeyError, TypeError):
                skipped += 1
    if limit is not None and limit >= 0:
        entries = entries[-limit:] if limit else []
    return entries, skipped


def _count_lines(path: Path) -> int:
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def _ensure_dir(path: Path) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    ignore = parent / ".gitignore"
    if not ignore.exists():
        ignore.write_text(_GITIGNORE, encoding="utf-8")


__all__ = [
    "HISTORY_DIRNAME",
    "HISTORY_FILENAME",
    "HISTORY_SCHEMA_VERSION",
    "HistoryEntry",
    "append",
    "build_entry",
    "default_path",
    "read",
]
