"""AI 调用审计（§5.4 第 5 条）。

每次工具调用都记一行 JSONL，随产物可追溯（`wiki-output/ai-audit.jsonl`）。
两条纪律：

1. **输出摘要要脱敏**：模型输出里可能含代码里的密钥、也可能含提示词注入文本，
   ``detail`` 只留前若干字符并抹掉疑似密钥（与 §5.3「密钥不落盘」一致）。
   但**摘要会让证据断链**——模型常把 ``file:line`` 放在末尾，正好被截掉，复核时
   只剩"谁、何时、成败"可查。需要完整原文的调用点传 ``full_detail=True``，额外
   落 ``detail_full``（同样脱敏、不截断）与 ``detail_chars``，两个字段只在超限时
   出现，短内容不额外占空间；
2. **审计失败不得阻断主流程**：审计是留痕，不是业务。写不进去就降级为 stderr
   一行，绝不让它把一次标注搞挂。
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from codewiki.ai.guard.secrets import redact_audit

AUDIT_FILENAME = "ai-audit.jsonl"

_SUMMARY_LIMIT = 240


def redact(text: str) -> str:
    """脱敏（**审计档**：宁可多抹，保留名字与分隔符便于读懂上下文）。

    喂模型请用 :func:`codewiki.ai.guard.secrets.redact_model` —— 那一档不会把
    ``MAX_TOKENS = 4096`` 这类普通代码抹掉。
    """
    return redact_audit(text)


def summarize(text: str, *, limit: int = _SUMMARY_LIMIT) -> str:
    """取一段可用于审计的摘要（脱敏 + 截断）。

    .. note:: :meth:`AuditLog.record` 已把同一逻辑内联 —— 它要同时产出"单行摘要"
       与"保留原换行的完整版"两态，套一层不带 ``full_detail`` 的摘要函数反而不
       清楚。本函数保留给需要单独摘要的调用方（含既有外部脚本）。
    """
    cleaned = redact(text).replace("\n", "\\n")
    return cleaned[:limit] + ("…" if len(cleaned) > limit else "")


class AuditLog:
    """JSONL 审计写入器（追加写，逐行落盘）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    def __enter__(self) -> AuditLog:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")
        return self

    def __exit__(self, *_exc) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def record(
        self,
        *,
        tool: str,
        args: dict,
        outcome: str,
        detail: str = "",
        full_detail: bool = False,
        **extra,
    ) -> None:
        """记一条调用。

        ``outcome`` 取 ``ok`` / ``denied`` / ``error`` / ``skipped`` / ``rejected`` /
        ``hit``。``rejected`` 专指"拿到了答案但被门禁丢弃"（证据不支持结论、target
        串台）—— 它与 ``error`` 的区别是**模型正常返回了**，不给它单独留痕的话，
        事后只能靠比对两轮 detail 的差异反推丢弃了什么。

        ``full_detail=True``：detail 超长时额外落 ``detail_full``（完整脱敏文本）。
        """
        raw = redact(detail)
        flat = raw.replace("\n", "\\n")
        entry = {
            "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "tool": tool,
            "args": {k: redact(str(v))[:120] for k, v in args.items()},
            "outcome": outcome,
            "detail": flat[:_SUMMARY_LIMIT] + ("…" if len(flat) > _SUMMARY_LIMIT else ""),
            **extra,
        }
        if full_detail and len(flat) > _SUMMARY_LIMIT:
            entry["detail_chars"] = len(flat)
            entry["detail_full"] = raw
        line = json.dumps(entry, ensure_ascii=False)
        if self._fh is not None:
            self._fh.write(line + "\n")
            self._fh.flush()
            return
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            print(f"[audit] 写入失败: {exc}", file=sys.stderr)


class NullAudit:
    """空审计（不需要留痕时的占位，接口与 :class:`AuditLog` 一致）。"""

    def __enter__(self) -> NullAudit:
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def record(self, **_kwargs) -> None:
        return None


__all__ = ["AUDIT_FILENAME", "AuditLog", "NullAudit", "redact", "summarize"]
