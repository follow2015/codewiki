"""``codewiki rules``：规则草案（纯静态，不调模型）。

只吃**已有数据包**，不自己跑分析：一条命令如果既分析又出建议，它的参数迟早跟
``analyze`` 分叉（``check`` 当初就是为了这件事才只读的）。两步走也更好排障 ——
"草案不对"和"图谱不对"是两个问题。

默认把草案打到 stdout（可管道、可重定向）；``--out`` 才落盘。默认不写文件是有意的：
生成一份文件却没人看，比在终端里看一眼就扔掉更糟。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from codewiki.graph.suggest_rules import render_suggested_rules
from codewiki.graph.suggest_rules import suggest_rules as _suggest_rules

from .context import load_packet_for_cli


def rules(args: argparse.Namespace) -> int:
    if not args.suggest:
        print("error: 目前只支持 --suggest（按现有层间依赖与层内环生成规则草案）", file=sys.stderr)
        return 2

    packet = load_packet_for_cli(Path(args.input))
    if packet is None:
        return 2

    draft = _suggest_rules(packet)
    text = render_suggested_rules(draft, title=packet.meta.title)
    if not args.out:
        print(text, end="")
        return 0

    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)

    errors = sum(1 for item in draft.suggestions if item.rule.severity == "error")
    print(
        f"规则草案 {len(draft.suggestions)} 条（error {errors} / warning {len(draft.suggestions) - errors}）：{path}"
    )
    for target in draft.targets:
        print(f"  待重构：{target.from_layer} → {target.to_layer}，{target.violations} 条现存依赖")
    for members in draft.cycles:
        print(f"  层间环：{' ⇄ '.join(members)}")
    print(f"审阅后合并：codewiki merge-rules {path}")
    return 0
