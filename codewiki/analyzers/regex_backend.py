"""正则降级后端（零依赖，设计文稿 §4.1 的降级方案）。

用途：tree-sitter / AST 不可用时**不阻断基础功能**，降级事件由引擎层写入诊断。

能力边界（刻意只做这些，不做伪确定性）：

- 提取 ``import`` / ``from … import`` 的**模块与名字轮廓**；
- 提取 ``def`` / ``class`` 的名字与行号（不推断签名与归属）；
- 不产出调用边 —— 正则分辨不出调用与字符串，宁缺毋滥（§0.3 非目标 3）。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from codewiki.models import FileModel, ImportFact, Provenance, SymbolFact

BACKEND = "regex"

_RE_IMPORT = re.compile(r"^\s*import\s+([\w\.]+)(?:\s+as\s+(\w+))?", re.M)
_RE_FROM = re.compile(r"^\s*from\s+([\w\.]*)\s+import\s+(.+?)\s*$", re.M)
_RE_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(", re.M)
_RE_CLASS = re.compile(r"^\s*class\s+(\w+)[\(:]", re.M)
_RE_ALL = re.compile(r"^__all__\s*=\s*\[([^\]]*)\]", re.M)
_RE_NAME = re.compile(r"[\'\"]([\w\.]+)[\'\"]")


class RegexAnalyzer:
    """按语言名切换正则集的通用后端；TS/JS/Python 都能给出最低限度的轮廓。"""

    def __init__(self, language: str = "other") -> None:
        self.language = language

    def parse(self, path: Path, content: str) -> FileModel:
        rel = str(path).replace("\\", "/")
        digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]

        imports: list[ImportFact] = []
        for lineno, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "//")):
                continue
            m_from = _RE_FROM.match(line)
            if m_from:
                module, names = m_from.group(1), m_from.group(2)
                for name in _RE_NAME.findall(names) or ([names.strip()] if names.strip() else []):
                    if name == "*":
                        imports.append(
                            ImportFact(module=module or ".", is_star=True, lineno=lineno, kind="from")
                        )
                    else:
                        imports.append(
                            ImportFact(module=module or ".", name=name, lineno=lineno, kind="from")
                        )
                continue
            m_import = _RE_IMPORT.match(line)
            if m_import:
                imports.append(
                    ImportFact(
                        module=m_import.group(1), alias=m_import.group(2), lineno=lineno
                    )
                )

        symbols: list[SymbolFact] = []
        lines = content.splitlines()
        for match in _RE_CLASS.finditer(content):
            name = match.group(1)
            symbols.append(
                SymbolFact(name=name, qualname=name, kind="class", lineno=_lineno(match, lines), endlineno=_lineno(match, lines))
            )
        for match in _RE_DEF.finditer(content):
            name = match.group(1)
            symbols.append(
                SymbolFact(name=name, qualname=name, kind="function", lineno=_lineno(match, lines), endlineno=_lineno(match, lines))
            )

        exports: list[str] = []
        m_all = _RE_ALL.search(content)
        if m_all:
            exports = _RE_NAME.findall(m_all.group(1))

        return FileModel(
            path=rel,
            language=self.language,
            hash=digest,
            imports=imports,
            symbols=symbols,
            calls=[],
            exports=exports,
            provenance=Provenance(backend=BACKEND),
        )


def _lineno(match: re.Match[str], lines: list[str]) -> int:
    """由字符偏移换算行号（1 起）。"""
    offset = match.start()
    consumed = 0
    for index, line in enumerate(lines, start=1):
        consumed += len(line) + 1
        if consumed > offset:
            return index
    return len(lines)
