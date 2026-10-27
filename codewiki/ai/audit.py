"""AI 调用审计（§5.4 第 5 条）。

每次工具调用都记一行 JSONL，随产物可追溯（`wiki-output/ai-audit.jsonl`）。
两条纪律：

1. **输出摘要要脱敏**：模型输出里可能含代码里的密钥、也可能含提示词注入文本，
   摘要只留前若干字符并抹掉疑似密钥（与 §5.3「密钥不落盘」一致）；
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
    """取一段可用于审计的摘要（脱敏 + 截断）。"""
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

    def record(self, *, tool: str, args: dict, outcome: str, detail: str = "", **extra) -> None:
        """记一条调用。``outcome`` 取 ``ok`` / ``denied`` / ``error`` / ``skipped``。"""
        entry = {
            "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "tool": tool,
            "args": {k: redact(str(v))[:120] for k, v in args.items()},
            "outcome": outcome,
            "detail": summarize(detail),
            **extra,
        }
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
