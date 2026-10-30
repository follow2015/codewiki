"""``codewiki ai-diagram``：基于真实调用链生成时序图（§4.2 时序图 / §4.3 失败处理）。

为什么是独立命令而不是 ``analyze`` 的一个开关：``analyze`` 是**纯静态**链路，
不联网、不需要密钥、离线机器上要能跑出全部产物 —— 这是它存在的理由。把 AI
塞进去，等于让"只想看依赖图"的人也得配模型。

与 ``ai-annotate`` 也刻意分开：读的是同一份数据包，但要的东西不同 —— 标注要
"这个模块为什么重要"（按热点逐个问），时序图要"这条调用链按什么顺序走"
（按链问一次）。混在一起会让 ``--top-n`` 一个参数同时管两种完全不同的取舍。

产物落在 ``<产物根>/diagrams/sequences/<slug>.mmd``；**坏图零落盘**由
:func:`codewiki.ai.diagram.generate_sequence_diagram` 保证（它返回 ``text=None``
时我们什么都不写，只留诊断）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from codewiki.ai.audit import AUDIT_FILENAME, AuditLog
from codewiki.ai.client import OpenAICompatClient, resolve_base_url, resolve_model
from codewiki.ai.deferred import DEFAULT_MODEL, DeferredClient, render_pending_report
from codewiki.ai.diagram import generate_sequence_diagram
from codewiki.ai.sequences import DEFAULT_DEPTH, pick_call_chains
from codewiki.config import CONFIG_FILENAME, ConfigError
from codewiki.packetio import PacketError, load_packet
from codewiki.render.diagrams import build_slug_map
from codewiki.trust import ai_denied, load_trusted_config

from .context import force_ai, trusted

EXIT_PENDING = 3

DEFAULT_CHAINS = 3

SEQUENCES_SUBDIR = Path("diagrams") / "sequences"


def diagram(args: argparse.Namespace) -> int:
    input_path = Path(args.input).resolve()
    if not input_path.is_file():
        print(f"error: 数据包不存在: {input_path}（先跑 analyze）", file=sys.stderr)
        return 2
    try:
        packet = load_packet(input_path)
    except PacketError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    config_path = Path(args.config).resolve() if args.config else Path.cwd() / CONFIG_FILENAME
    try:
        cfg = load_trusted_config(config_path, trusted=trusted(args))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    denied = ai_denied(cfg, force=force_ai(args))
    if denied:
        print(denied, file=sys.stderr)
        return 2

    model = resolve_model(cli_value=args.model, config_value=cfg.ai.model)
    deferred_root = Path(args.deferred).resolve() if args.deferred else None
    if not model and deferred_root is not None:
        model = DEFAULT_MODEL
    if not model:
        print(
            "error: 未指定模型 —— 用 --model 或环境变量 OPENAI_MODEL 指定"
            "（codewiki.toml 里的 [ai] model 默认被忽略：该文件来自被分析仓库；"
            "用 --deferred 则无需指定）",
            file=sys.stderr,
        )
        return 2

    if deferred_root is not None:
        client: OpenAICompatClient | DeferredClient = DeferredClient(
            deferred_root, model=model, purpose="diagram"
        )
    else:
        base_url, base_url_warning = resolve_base_url(cli_value=args.base_url, config_value=cfg.ai.base_url)
        if base_url_warning:
            print(f"warning: {base_url_warning}", file=sys.stderr)
        client = OpenAICompatClient(
            model=model,
            base_url=base_url,
            timeout_s=cfg.ai.timeout_s,
            max_retries=cfg.ai.max_retries,
        )

    use_l2 = bool(args.l2_validate or cfg.ai.l2_validate)
    chains = pick_call_chains(
        packet,
        top_n=args.top_n or DEFAULT_CHAINS,
        depth=args.depth or DEFAULT_DEPTH,
    )
    if not chains:
        print("没有可用的调用链（数据包里没有 CALLS 边），未生成时序图", file=sys.stderr)
        return 0

    out_root = Path(args.out).resolve() if args.out else input_path.parent.parent
    target_dir = out_root / SEQUENCES_SUBDIR
    target_dir.mkdir(parents=True, exist_ok=True)
    slug_map = build_slug_map([chain.entry for chain in chains])

    written: list[Path] = []
    diagnostics = []
    with AuditLog(out_root / AUDIT_FILENAME) as audit:
        for chain in chains:
            result = generate_sequence_diagram(chain.steps, client=client, use_l2=use_l2, where=chain.entry)
            diagnostics.extend(result.diagnostics)
            audit.record(
                tool="ai_diagram",
                args={"entry": chain.entry, "steps": len(chain.steps), "l2": use_l2},
                outcome="ok" if result.text else ("skipped" if result.skipped else "error"),
                detail=result.text or "",
            )
            if not result.text:
                continue
            path = target_dir / f"{slug_map[chain.entry]}.mmd"
            path.write_text(result.text, encoding="utf-8")
            written.append(path)

    for path in written:
        print(f"时序图：{path}")
    for diag in diagnostics:
        print(f"  [{diag.level}] {diag.code}: {diag.message}")
    if not written:
        print("未生成任何可用时序图（见上面的诊断）", file=sys.stderr)

    if isinstance(client, DeferredClient) and client.misses:
        report = render_pending_report(client.pending(), root=deferred_root or Path.cwd())
        if report:
            print(report, file=sys.stderr)
        return EXIT_PENDING
    return 0
