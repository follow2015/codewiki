"""``codewiki import-graph``：导入外部图谱（§4.6.4）：graphify → 统一数据包。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from codewiki.graph.importers.graphify import GraphifyFormatError, load_graphify
from codewiki.render.jsonio import write_packet
from codewiki.schemas import Diagnostic


def import_graph(args: argparse.Namespace) -> int:
    source = Path(args.input).resolve()
    out_dir = Path(args.out).resolve()
    try:
        packet, diagnostics = load_graphify(source)
    except GraphifyFormatError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    diagnostics.append(
        Diagnostic(level="info", code="GRAPHIFY_IMPORTED", message=f"已从 {source} 导入", where=str(source))
    )

    path = write_packet(packet, out_dir)
    print(f"导入完成：{len(packet.nodes)} 节点 / {len(packet.edges)} 边")
    for diag in diagnostics:
        print(f"  [{diag.level}] {diag.code}: {diag.message}")
    print(f"数据包：{path}")
    return 0
