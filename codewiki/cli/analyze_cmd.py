"""``codewiki analyze`` 的接线层：命令行参数 → 流水线请求对象。

这层刻意只做映射，不做判断。判断留在 :func:`codewiki.pipeline.analyze.run` 里，
好处是它可以脱离 argparse 单测（构造一个 ``AnalyzeRequest`` 即可），也就不用为
"某个开关有没有被正确传下去"写端到端用例。
"""

from __future__ import annotations

import argparse

from codewiki.pipeline.analyze import AnalyzeRequest, run

from .context import trusted


def analyze(args: argparse.Namespace) -> int:
    request = AnalyzeRequest(
        src=args.src,
        out=args.out,
        backend=args.backend,
        exclude=tuple(args.exclude or ()),
        config=args.config,
        trust_config=trusted(args),
        offline_asset=args.offline_asset,
        no_cache=args.no_cache,
        incremental=args.incremental,
        min_confidence=args.min_confidence,
        strict=args.strict,
        timestamp=args.timestamp,
    )
    return run(request)
