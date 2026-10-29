"""``codewiki suggest-layers``：目录优先 → 建议分层（纯静态，无需 AI）。"""

from __future__ import annotations

import argparse
from pathlib import Path

from codewiki.graph.suggest_layers import (
    SUGGESTED_LAYERS_FILENAME,
    render_suggested_layers,
)
from codewiki.graph.suggest_layers import suggest_layers as _suggest_layers

from .context import load_packet_for_cli


def suggest_layers(args: argparse.Namespace) -> int:
    packet = load_packet_for_cli(Path(args.input))
    if packet is None:
        return 2
    # 用 alias 调实现：入口函数与本模块同名，直接 `suggest_layers(packet)` 会
    # 递归调自己（AttributeError: 'Namespace' object has no attribute ...）。
    layers = _suggest_layers(packet, max_group=args.max_group)
    out_dir = Path(args.out) if args.out else Path(args.input).parent.parent
    target_dir = out_dir / "suggestions"
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / SUGGESTED_LAYERS_FILENAME
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(render_suggested_layers(layers, title=packet.meta.title), encoding="utf-8")
    tmp.replace(path)

    print(f"建议分层 {len(layers)} 层：{path}")
    for layer in layers:
        print(f"  - {layer.name}: {layer.reason()}")
    print(f"审阅后合并：codewiki merge-layers {path}")
    return 0
