"""答案里的 ``file:line`` 引用核对。

提示词要求"结论必须引用 file:line"，但此前没有任何代码去核对 —— 模型写
``pkg/core.py:999`` 或一个根本不存在的文件，答案照样原样交给用户，引用形同装饰。
这里只做**存在性**核对（文件在仓库内、行号落在文件范围内）；它能挡住编造与数错，
挡不住"引用真实但不支持结论"，后者需要人读，不在这里假装能判断。

路径解析复用 :func:`codewiki.ai.guard.paths.resolve_in_root`（仓库边界只有一处
定义），密钥文件的引用一律判为无效 —— 答案里出现 ``.env:3`` 本身就说明模型读到了
不该读的东西。

**什么算"引用"**：``file:line`` 与 ``file:a-b`` 两种写法，token 由
:func:`looks_like_path` 二次判定。正则分不清 ``v1.2:3``（版本号）与 ``a.py:3``，
所以判定必须落在代码里而不是正则里：含 ``/``、点开头（``.env``）、已知无后缀文件名
（``Makefile``）或**代码/文本/密钥类后缀**才算引用。名单外的后缀（``foo.xyz:12``）
不核对 —— 这是有意的边界：宁可漏核一个怪后缀文件，也不把版本号、模块名误判成文件
而白烧一次纠错轮。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from codewiki.ai.guard import PathDenied, is_secret_file, resolve_in_root

_CANDIDATE = re.compile(
    r"(?<![\w/.\-])([A-Za-z_.][\w.\-]*(?:/[\w.\-]+)*)[:：](\d{1,6})(?:[-–](\d{1,6}))?(?!\w)"
)

_EXTENSIONLESS = frozenset(
    {
        "makefile",
        "gnumakefile",
        "dockerfile",
        "containerfile",
        "procfile",
        "gemfile",
        "rakefile",
        "jenkinsfile",
        "vagrantfile",
        "justfile",
        "brewfile",
        "license",
        "licence",
        "notice",
        "readme",
        "changelog",
        "contributing",
        "authors",
        "todo",
        "requirements",
    }
)

_TEXT_SUFFIXES = frozenset(
    {
        "py",
        "pyi",
        "ts",
        "tsx",
        "js",
        "jsx",
        "mjs",
        "cjs",
        "go",
        "rs",
        "java",
        "kt",
        "kts",
        "c",
        "h",
        "cc",
        "cpp",
        "hpp",
        "cs",
        "rb",
        "php",
        "swift",
        "scala",
        "sh",
        "bash",
        "zsh",
        "fish",
        "ps1",
        "bat",
        "sql",
        "lua",
        "pl",
        "r",
        "jl",
        "dart",
        "ex",
        "exs",
        "erl",
        "hs",
        "clj",
        "vue",
        "svelte",
        "astro",
        "proto",
        "thrift",
        "graphql",
        "gql",
        "toml",
        "yaml",
        "yml",
        "json",
        "json5",
        "ini",
        "cfg",
        "conf",
        "env",
        "properties",
        "xml",
        "gradle",
        "lock",
        "txt",
        "csv",
        "tsv",
        "ndjson",
        "md",
        "rst",
        "adoc",
        "html",
        "htm",
        "css",
        "scss",
        "sass",
        "less",
        "svg",
        "j2",
        "jinja",
        "tmpl",
        "tpl",
        "pem",
        "key",
        "crt",
        "cer",
        "p12",
        "pfx",
        "jks",
        "keystore",
        "gpg",
        "pgp",
        "kdbx",
        "asc",
    }
)

MAX_CITATIONS_CHECKED = 40


@dataclass(frozen=True)
class Citation:
    path: str
    start: int
    end: int

    def __str__(self) -> str:
        return f"{self.path}:{self.start}" + (f"-{self.end}" if self.end != self.start else "")


@dataclass(frozen=True)
class CitationProblem:
    citation: Citation
    reason: str

    def __str__(self) -> str:
        return f"{self.citation}（{self.reason}）"


@dataclass(frozen=True)
class CitationReport:
    """一次核对的全貌：核了几条、错在哪、**有多少条没核**。"""

    checked: int = 0
    problems: list[CitationProblem] = field(default_factory=list)
    skipped: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems


def looks_like_path(token: str) -> bool:
    """token 是否像一个文件路径（用于把版本号、模块名挡在外面）。"""
    if not token:
        return False
    if "/" in token:
        return True
    if token.startswith("."):
        return True
    if token.lower() in _EXTENSIONLESS:
        return True
    if "." not in token:
        return False
    suffix = token.rsplit(".", 1)[1].lower()
    if suffix.isdigit():
        return False
    return suffix in _TEXT_SUFFIXES


def extract_citations(text: str) -> list[Citation]:
    """抽取 ``file:line`` / ``file:a-b`` 引用（去重、保序、不截断）。"""
    seen: set[Citation] = set()
    out: list[Citation] = []
    for match in _CANDIDATE.finditer(text or ""):
        token = match.group(1)
        if not looks_like_path(token):
            continue
        start = int(match.group(2))
        end = int(match.group(3)) if match.group(3) else start
        cit = Citation(token, start, max(start, end))
        if cit in seen:
            continue
        seen.add(cit)
        out.append(cit)
    return out


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8", errors="replace").splitlines())


def _check(citations: list[Citation], repo_root: Path) -> list[CitationProblem]:
    problems: list[CitationProblem] = []
    counts: dict[Path, int] = {}
    for cit in citations:
        try:
            target = resolve_in_root(cit.path, root=repo_root)
        except PathDenied:
            problems.append(CitationProblem(cit, "路径越出仓库"))
            continue
        if is_secret_file(target):
            problems.append(CitationProblem(cit, "密钥文件不应被引用"))
            continue
        if not target.is_file():
            problems.append(CitationProblem(cit, "文件不存在"))
            continue
        if target not in counts:
            try:
                counts[target] = _line_count(target)
            except OSError:
                problems.append(CitationProblem(cit, "文件不可读"))
                continue
        total = counts[target]
        if cit.start < 1 or cit.end > total:
            problems.append(CitationProblem(cit, f"行号越界，文件只有 {total} 行"))
    return problems


def scan_citations(text: str, *, repo_root: Path, limit: int = MAX_CITATIONS_CHECKED) -> CitationReport:
    """核对并返回完整报告（含未核对数量）。"""
    found = extract_citations(text)
    checked = found[: max(0, limit)]
    return CitationReport(
        checked=len(checked),
        problems=_check(checked, repo_root),
        skipped=len(found) - len(checked),
    )


def verify_citations(
    text: str, *, repo_root: Path, limit: int = MAX_CITATIONS_CHECKED
) -> list[CitationProblem]:
    """返回**无效**引用（空列表 = 全部核对通过，或答案里没有引用）。

    只看问题、不看截断时用这个；需要知道"还有多少条没核"请用
    :func:`scan_citations`。
    """
    return scan_citations(text, repo_root=repo_root, limit=limit).problems


__all__ = [
    "Citation",
    "CitationProblem",
    "CitationReport",
    "MAX_CITATIONS_CHECKED",
    "extract_citations",
    "looks_like_path",
    "scan_citations",
    "verify_citations",
]
