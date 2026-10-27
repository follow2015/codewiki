"""``codewiki cycles``：依赖环检测（模块环 warning、层级环 error）。"""

from __future__ import annotations

import argparse
from pathlib import Path

from codewiki.graph.analysis import cycles_to_diagnostics, find_layer_cycles, find_module_cycles
from codewiki.render.mermaid_cycle import render_all_cycles

from .context import load_packet_for_cli


def cycles(args: argparse.Namespace) -> int:
    packet = load_packet_for_cli(Path(args.input))
    if packet is None:
        return 2
    cycles = find_module_cycles(packet) + find_layer_cycles(packet)
    diagnostics = cycles_to_diagnostics(cycles)

    print(
        f"环 {len(cycles)} 个（模块 {sum(1 for c in cycles if c.kind == 'module')} / "
        f"层级 {sum(1 for c in cycles if c.kind == 'layer')}）"
    )
    for diag in diagnostics:
        print(f"  [{diag.level}] {diag.code}: {diag.message}")

    if args.out:
        out_dir = Path(args.out)
        written = render_all_cycles(cycles, out_dir / "diagrams" / "cycles")
        for name in written:
            print(f"环图：{out_dir / 'diagrams' / 'cycles' / name}")

    return 1 if any(d.level == "error" for d in diagnostics) else 0
