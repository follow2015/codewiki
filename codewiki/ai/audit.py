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
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

AUDIT_FILENAME = "ai-audit.jsonl"

_KEYWORD = (
    r"password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|"
    r"private[_-]?key|credential|authorization"
)

_SECRET_PATTERNS = (
    (
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S),
        "***",
    ),
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"), "***"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}"), "***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "***"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "***"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "***"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"), "***"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), "***"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}"), "***"),
    (
        re.compile(r"(\b[a-z][a-z0-9+.\-]*://[^/\s:@]{1,64}):[^/\s@]{1,128}@", re.I),
        r"\1:***@",
    ),
    (
        re.compile(
            rf"\b([A-Za-z0-9_.\-]*?(?:{_KEYWORD})[A-Za-z0-9_.\-]*)(\s*=\s*)"
            r"(\"[^\"]*\"|'[^']*'|[^\s#]+)",
            re.I,
        ),
        r"\1\2***",
    ),
    (
        re.compile(
            rf"\b([A-Za-z0-9_.\-]*?(?:{_KEYWORD})[A-Za-z0-9_.\-]*)(\s*:\s*)"
            r"(\"[^\"]+\"|'[^']+'|[^\s\"']*[0-9_\-./+@:][^\s\"']*)",
            re.I,
        ),
        r"\1\2***",
    ),
)

_SUMMARY_LIMIT = 240


def redact(text: str) -> str:
    """脱敏：把疑似密钥整段替换成 ``***``（保留名字与分隔符，便于读懂上下文）。"""
    out = text
    for pattern, repl in _SECRET_PATTERNS:
        out = pattern.sub(repl, out)
    return out


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
