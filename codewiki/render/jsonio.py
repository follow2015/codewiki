"""标准数据包导出（§4.4 / §4.5）。

渲染层与 AI 层的唯一输入是这份 JSON；本模块只负责**落盘与读数**，不做任何
二次加工（避免"渲染时各算各的"，§十三 决策 1）。

构造器 ``packet_from_parts`` 已下沉到 :mod:`codewiki.packetio` —— 导入适配器
（``graph/importers``）也要造包，而它不该依赖渲染层。这里保留 re-export 只为
不打断既有调用方，新代码请直接 ``from codewiki.packetio import packet_from_parts``。
"""
from __future__ import annotations

import json
from pathlib import Path

from codewiki.packetio import packet_from_parts
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
    """读回数据包（供 ``ai-annotate`` / 导入适配器 / 测试使用）。

    走 :func:`codewiki.packetio.load_packet`：版本过新会明确报错、过旧会迁移 ——
    直接 ``model_validate_json`` 会让旧版工具把新包"静默丢掉不认识的字段"后照常
    渲染，产物看着正常、信息已经缺了。
    """
    from codewiki.packetio import load_packet

    return load_packet(path)


__all__ = [
    "PACKET_FILENAME",
    "PACKET_SUBDIR",
    "SCHEMA_FILENAME",
    "packet_from_parts",
    "read_packet",
    "write_packet",
]
