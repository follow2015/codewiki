"""``codewiki agent``：交互问答（§5.2 工作流 B）。

模型决定调什么工具，我们负责执行与安全。安全那部分全在 ``ai/guard`` 与工具
注册表里，这里只做三件事：读问题、装配上下文、把过程打出来。

审计是**无条件**的（M6）：不给 ``--out`` 只是目录取默认值，绝不能退化成
NullAudit —— "全程无痕"的 AI 调用等于放弃了追责能力。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from codewiki.ai.agent import AgentLoop
from codewiki.ai.audit import AUDIT_FILENAME, AuditLog
from codewiki.ai.client import OpenAICompatClient, resolve_model
from codewiki.config import CONFIG_FILENAME, ConfigError
from codewiki.trust import ai_denied, load_trusted_config

from .context import force_ai, load_packet_for_cli, trusted


def agent(args: argparse.Namespace) -> int:
    packet = load_packet_for_cli(Path(args.input))
    if packet is None:
        return 2
    try:
        cfg = load_trusted_config(
            Path(args.config).resolve() if args.config else Path.cwd() / CONFIG_FILENAME,
            trusted=trusted(args),
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    denied = ai_denied(cfg, force=force_ai(args))
    if denied:
        print(denied, file=sys.stderr)
        return 2
    model = resolve_model(cli_value=args.model, config_value=cfg.ai.model)
    if not model:
        print("error: 未指定模型（--model 或环境变量 OPENAI_MODEL）", file=sys.stderr)
        return 2

    question = args.question or _read_question_from_stdin()
    if not question:
        print("error: 需要提供问题（作为参数，或从 stdin 读入）", file=sys.stderr)
        return 2

    out_dir = Path(args.out).resolve() if args.out else Path(args.input).parent.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    audit = AuditLog(out_dir / AUDIT_FILENAME)

    with audit:
        loop = AgentLoop(
            OpenAICompatClient(model=model, base_url=args.base_url, timeout_s=60.0),
            packet,
            repo_root=Path(args.src).resolve() if args.src else None,
            audit=audit,
            max_rounds=args.max_rounds,
            allow_shell=bool(getattr(args, "allow_shell", False)),
        )
        result = loop.run(question)

    for step in result.rounds:
        tool = step.get("tool")
        suffix = f" → {tool}({step.get('args')})" if tool else " → 结论"
        print(f"[{step['round']}] {step['thought'][:80]}{suffix}")

    if result.answer:
        print("\n答案：")
        print(result.answer)
    else:
        print("\n未得出结论。", file=sys.stderr)
    for note in result.diagnostics:
        print(f"  {note}", file=sys.stderr)
    return 0 if result.answer else 1


def _read_question_from_stdin() -> str:
    """非交互管道场景：从 stdin 读问题（没有输入就返回空串，不阻塞）。"""
    if sys.stdin.isatty():
        return ""
    return sys.stdin.read().strip()
