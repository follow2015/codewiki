"""命令层共用的小工具（三个 AI 命令与只读命令都要用）。

只放"每个子命令都会用、但放进 pipeline 又不合适"的东西：读数据包（失败给
用法错误码 2）、信任开关。真正的业务规则不在这里 —— 那正是这次拆分要分开的
东西，别把 ``cli.py`` 的杂物换个位置继续堆。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from codewiki.packetio import PacketError, load_packet
from codewiki.schemas import DataPacket
from codewiki.trust import is_trusted


def trusted(args: argparse.Namespace) -> bool:
    """``--trust-config`` 或 ``CODEWIKI_TRUST_CONFIG=1`` 才算"我信任这份配置"。"""
    return bool(getattr(args, "trust_config", False)) or is_trusted()


def load_packet_for_cli(path: Path) -> DataPacket | None:
    """读数据包；不存在 / 损坏 / 版本不兼容返回 ``None`` 并由调用方给 2。

    刻意不抛 :class:`SystemExit`：带字符串的 SystemExit 退出码是 1，与"运行失败"
    混在一起，而"输入文件不存在"是**用法错误**（应给 2）。
    """
    if not path.is_file():
        print(f"error: 数据包不存在: {path}（先跑 analyze）", file=sys.stderr)
        return None
    try:
        return load_packet(path)
    except PacketError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None
