"""标准数据包导出（§4.4 / §4.5）。

渲染层与 AI 层的唯一输入是这份 JSON；本模块只负责**落盘与读数**，不做任何
二次加工（避免"渲染时各算各的"，§十三 决策 1）。
"""
from __future__ import annotations

import json
from pathlib import Path

from codewiki.schemas import DataPacket

PACKET_FILENAME = "codewiki.json"
PACKET_SUBDIR = "data"
SCHEMA_FILENAME = "codewiki.schema.json"


def write_packet(packet: DataPacket, out_dir: Path, *, write_schema: bool = True) -> Path:
    """把数据包写入 ``<out>/data/codewiki.json``（并返回该路径）。

    写入用 ``ensure_ascii=False`` + 缩进，便于人工 diff 与 golden 快照比对；
    键序按模型声明顺序（不排序），保证跨版本 diff 稳定。
    """
    data_dir = out_dir / PACKET_SUBDIR
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / PACKET_FILENAME
    target.write_text(
        json.dumps(packet.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if write_schema:
        (data_dir / SCHEMA_FILENAME).write_text(
            json.dumps(DataPacket.model_json_schema(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return target


def read_packet(path: Path) -> DataPacket:
    """读回数据包（供 ``ai-annotate`` / 导入适配器 / 测试使用）。"""
    return DataPacket.model_validate_json(path.read_text(encoding="utf-8"))


def packet_from_parts(
    *,
    title: str,
    layers,
    nodes,
    edges,
    annotations=None,
    diagnostics=None,
    provenance=None,
    repo_root: str | None = None,
    repo_commit: str | None = None,
) -> DataPacket:
    """组装数据包（CLI 与测试共用，避免各写各的 meta）。"""
    meta = DataPacket.new_meta(title, repo_root=repo_root, repo_commit=repo_commit)
    if provenance:
        meta.provenance = provenance
    return DataPacket(
        meta=meta,
        layers=list(layers),
        nodes=list(nodes),
        edges=[e for e in edges if e.confidence >= 0.0],
        annotations=list(annotations or []),
        diagnostics=list(diagnostics or []),
    )


__all__ = [
    "PACKET_FILENAME",
    "PACKET_SUBDIR",
    "SCHEMA_FILENAME",
    "packet_from_parts",
    "read_packet",
    "write_packet",
]
