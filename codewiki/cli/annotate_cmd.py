"""``codewiki ai-annotate``：独立 AI 标注命令（§5.2 工作流 A）。

刻意与 ``analyze`` 分离：无网环境出静态产物，另一台带模型的机器读数据包 +
源码出标注，再拷回合并。因此本命令**从不改写已产出的静态产物**。

``write_back`` 的四档策略（review-file / dry-run / confirm / auto）在这里，
因为它决定的是"人怎么参与审阅"，是交互问题而不是流水线规则。``auto`` 也仍然
走正规合并（受保护注解不会被动），不是"AI 直写配置"。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from codewiki.ai.annotate import annotate_modules, write_suggestions
from codewiki.ai.audit import AUDIT_FILENAME, AuditLog
from codewiki.ai.cache import CACHE_DIRNAME, default_cache_dir
from codewiki.ai.client import OpenAICompatClient, resolve_base_url, resolve_model
from codewiki.ai.deferred import DEFAULT_MODEL, DeferredClient, render_pending_report
from codewiki.ai.merge import merge_annotations, render_merge_report
from codewiki.config import CONFIG_FILENAME, ConfigError
from codewiki.packetio import PacketError, load_packet
from codewiki.render.diagnostics_md import write_diagnostics_md
from codewiki.schemas import Diagnostic
from codewiki.trust import load_trusted_config

from .context import trusted

WRITE_BACK_POLICIES = frozenset({"review-file", "dry-run", "confirm", "auto"})

EXIT_PENDING = 3


def resolve_cache_dir(cli_value: str | None) -> Path:
    """``--cache-dir`` 是**根**（缓存落在其下 ``.codewiki/ai-cache``）；缺省走用户级目录。

    默认值刻意不再是 ``--src``：那等于把缓存写进被审仓库，而仓库里的缓存文件
    是可以预置的（键可自行复算），命中即被当模型答案采信。
    """
    if cli_value:
        return Path(cli_value).resolve() / CACHE_DIRNAME
    return default_cache_dir()


def annotate(args: argparse.Namespace) -> int:
    input_path = Path(args.input).resolve()
    if not input_path.is_file():
        print(f"error: 数据包不存在: {input_path}（先跑 analyze）", file=sys.stderr)
        return 2

    config_path = Path(args.config).resolve() if args.config else Path.cwd() / CONFIG_FILENAME
    try:
        cfg = load_trusted_config(config_path, trusted=trusted(args))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    model = resolve_model(cli_value=args.model, config_value=cfg.ai.model)
    deferred_root = Path(args.deferred).resolve() if getattr(args, "deferred", None) else None
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

    src_root = Path(args.src).resolve() if args.src else None
    if src_root is not None and not src_root.is_dir():
        print(f"error: --src 不是目录: {src_root}", file=sys.stderr)
        return 2

    try:
        packet = load_packet(input_path)
    except PacketError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    out_dir = Path(args.out).resolve() if args.out else input_path.parent.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = resolve_cache_dir(args.cache_dir)

    if deferred_root is not None:
        if args.base_url:
            print("warning: --deferred 模式不发网络请求，--base-url 已忽略", file=sys.stderr)
        client = DeferredClient(deferred_root, model=model)
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
    write_back = args.write_back or cfg.ai.write_back
    if write_back not in WRITE_BACK_POLICIES:
        print(
            f"error: 未知 write-back 策略 {write_back!r}（可选 {sorted(WRITE_BACK_POLICIES)}）",
            file=sys.stderr,
        )
        return 2

    diagnostics: list[Diagnostic] = []
    with AuditLog(out_dir / AUDIT_FILENAME) as audit:
        result = annotate_modules(
            packet,
            cfg,
            src_root=src_root,
            client=client,
            cache_dir=cache_dir,
            budget_tokens=args.budget if args.budget is not None else cfg.ai.budget_tokens,
            top_n=args.top_n or cfg.ai.top_n,
            audit=audit,
        )
    diagnostics.extend(result.diagnostics)

    suggest_path = write_suggestions(result.suggestions, out_dir)
    write_diagnostics_md(out_dir / "suggestions", diagnostics)

    if result.pending and write_back in ("confirm", "auto"):
        print(
            f"warning: {len(result.pending)} 个模块待作答，本轮降级为 review-file（不自动合并）",
            file=sys.stderr,
        )
        write_back = "review-file"

    print(f"AI 建议 {len(result.suggestions)} 条（跳过 {len(result.skipped)} 个模块）")
    if write_back == "dry-run":
        print("（dry-run：未写文件）")
        print(suggest_path.read_text(encoding="utf-8"))
    elif write_back == "confirm":
        kept = [s for s in result.suggestions if _confirm(s)]
        suggest_path = write_suggestions(kept, out_dir)
        print(f"审阅后保留 {len(kept)} 条")
    elif write_back == "auto":
        outcome = merge_annotations(config_path, suggest_path, dry_run=False)
        print(render_merge_report(outcome))
    else:
        print(f"建议文件：{suggest_path}")
        print(f"审阅后合并：codewiki merge-annotations {suggest_path}")

    for diag in diagnostics:
        print(f"  [{diag.level}] {diag.code}: {diag.message}")

    if result.degraded:
        print("AI 未完成（部分结果已保留，静态产物不受影响）", file=sys.stderr)
        return 1

    if result.pending:
        items = client.pending() if isinstance(client, DeferredClient) else []
        report = render_pending_report(items, root=deferred_root or Path.cwd())
        if report:
            print(report, file=sys.stderr)
        return EXIT_PENDING
    return 0


def _confirm(suggestion) -> bool:
    """逐条确认（少量模块时的交互策略）。"""
    label = suggestion.label or suggestion.target
    print(f"\n[{suggestion.target}] {label}")
    print(suggestion.note)
    try:
        return input("采纳？(y/N) ").strip().lower() == "y"
    except EOFError:
        return False
