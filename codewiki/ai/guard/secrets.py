"""密钥策略：判定与脱敏各只有一个实现（文档 §AI 层安全模型 · SecretPolicy）。

**两个脱敏档位，因为目标本来就冲突**：

- :func:`redact_audit` —— 写审计日志用，**宁可多抹**。日志给人看，抹掉一个
  普通变量名的代价远小于漏掉一个真密钥。
- :func:`redact_model` —— 喂给模型用，**只抹字面量密钥**。把
  ``MAX_TOKENS = 4096`` 抹成 ``MAX_TOKENS = ***``、把「取密码函数的返回值」
  （``password`` 取 ``get_password()`` 的结果）也抹成 ``***``，等于给模型一段
  残缺代码，它会照着残缺代码下结论（外部评审实测）。

两者共用同一套"形状"模式（PEM 块、``ghp_`` / ``AKIA`` / ``sk-`` / ``sk_live_``、
``Bearer``、URL 内嵌口令）——**形状确定的密钥没有歧义，两个档位都无条件抹**。
差别只在"名字 + 值"这类模糊情形：值像不像代码，只有 model 档在乎。
"""

from __future__ import annotations

import re
from pathlib import Path

_SECRET_FILE_NAMES = frozenset(
    {
        ".netrc",
        ".htpasswd",
        ".npmrc",
        ".pypirc",
        ".git-credentials",
        "credentials.json",
    }
)
_SECRET_KEY_PREFIXES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
_SECRET_FILE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".env")
_SECRET_FILE_ALLOW = frozenset({".env.example", ".env.sample", ".env.template", ".env.dist"})

EXCLUDE_GLOBS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".htpasswd",
    ".git-credentials",
    "credentials.json",
)


def is_secret_file(path: Path) -> bool:
    """判断是否"读都不该读"的密钥载体（``.env`` / 私钥 / 凭据文件）。

    脱敏是**读后**补救，形如 PEM 的多行结构、自定义格式的凭据文件都可能漏网；
    对这几类文件直接拒读，比"读了再抹"稳。模板文件（``.env.example``）放行。
    """
    name = path.name.lower()
    if name in _SECRET_FILE_ALLOW:
        return False
    if name in _SECRET_FILE_NAMES:
        return True
    if name.startswith(_SECRET_KEY_PREFIXES):
        return True
    if name == ".env" or name.startswith(".env."):
        return True
    return name.endswith(_SECRET_FILE_SUFFIXES)


_KEYWORD = (
    r"password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|"
    r"private[_-]?key|credential|authorization"
)

_BENIGN_NAME = re.compile(
    r"(?:^|[_.\-])(?:max|min|num|total|prompt|completion|input|output)[_\-]?tokens?(?:$|[_.\-])"
    r"|tokens?[_\-]?(?:count|limit|len|length|budget|usage|type|ttl|size|per\w*)"
    r"|(?:url|uri|path|file|dir|header|type|name|field|prefix|endpoint)$",
    re.I,
)

_BARE_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_.]*")

_SHAPE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
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
    (re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{8,}"), "***"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}"), "***"),
    (
        re.compile(r"(\b[a-z][a-z0-9+.\-]*://[^/\s:@]{1,64}):[^/\s@]{1,128}@", re.I),
        r"\1:***@",
    ),
)

_ASSIGN = re.compile(
    rf"\b([A-Za-z0-9_.\-]*?(?:{_KEYWORD})[A-Za-z0-9_.\-]*)(\s*=\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s#]+)",
    re.I,
)
_COLON = re.compile(
    rf"\b([A-Za-z0-9_.\-]*?(?:{_KEYWORD})[A-Za-z0-9_.\-]*)(\s*:\s*)"
    r"(\"[^\"]+\"|'[^']+'|[^\s\"']*[0-9_\-./+@:][^\s\"']*)",
    re.I,
)


def _mask_all(match: re.Match[str]) -> str:
    """audit 档：名字与分隔符保留，值一律抹掉。"""
    return f"{match.group(1)}{match.group(2)}***"


def _mask_literal_only(match: re.Match[str]) -> str:
    """model 档：只抹**看起来是字面量密钥**的值。

    - 带引号的非空字面量：抹；
    - ``NAME=value``（全大写名 + 无空格，dotenv / shell 风格）：抹；
    - 调用 / 表达式（含括号）、裸标识符（``self.token = token``）、短纯数字
      （``token_count = 0``）：这是代码不是密钥，原样保留；
    - 其余（含数字或符号的裸值，如 ``password`` 取 ``hunter2``）：抹。
    """
    name, sep, value = match.group(1), match.group(2), match.group(3)
    original = match.group(0)
    if _BENIGN_NAME.search(name):
        return original
    if value[:1] in ("'", '"'):
        return f"{name}{sep}***" if len(value) > 2 else original
    if "(" in value or ")" in value:
        return original
    if sep.strip() == "=" and name.isupper():
        return f"{name}{sep}***"
    if _BARE_IDENTIFIER.fullmatch(value) or (value.isdigit() and len(value) < 6):
        return original
    return f"{name}{sep}***"


def _apply(text: str, repl_keyword) -> str:
    out = text
    for pattern, repl in _SHAPE_PATTERNS:
        out = pattern.sub(repl, out)
    out = _ASSIGN.sub(repl_keyword, out)
    return _COLON.sub(repl_keyword, out)


def redact_audit(text: str) -> str:
    """审计档：宁可多抹（把疑似密钥整段替换成 ``***``）。"""
    return _apply(text, _mask_all)


def redact_model(text: str) -> str:
    """模型档：只抹字面量密钥，不把普通源码改成残缺代码。"""
    return _apply(text, _mask_literal_only)


__all__ = [
    "EXCLUDE_GLOBS",
    "is_secret_file",
    "redact_audit",
    "redact_model",
]
