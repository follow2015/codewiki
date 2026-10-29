"""仓库上下文（基础设施层，设计文稿 §一）。

职责：文件遍历、语言判定、内容读取、仓库提交号探测。

- **只读**：对业务代码绝不写入（§一 副作用收敛）。
- **零硬编码**：跳过目录清单可由 ``codewiki.toml`` 的 ``[analysis] exclude`` 追加，
  默认值只是一个合理起点，不是业务规则。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from codewiki.subproc import run

DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".codewiki",
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    "site",
    ".next",
    "coverage",
)

EXT_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
}


@dataclass
class SourceFile:
    """一个待分析的源文件（相对路径 + 内容）。"""

    path: str
    content: str
    language: str


def language_of(path: str) -> str:
    """按扩展名判定语言；不在表内一律 ``other``（仅登记文件清单，不建符号）。"""
    suffix = Path(path).suffix.lower()
    return EXT_LANGUAGE.get(suffix, "other")


def is_excluded(rel_path: str, extra: tuple[str, ...] = ()) -> bool:
    """是否跳过（默认清单 + 配置追加的 exclude + **任意隐藏目录**）。

    隐藏目录单独一条规则，是因为固定清单永远追不上：实测仓库里 `.husky`、
    `.hypothesis`、`.github`、`.gitea`、`.agent`、`.vscode`、`.superpowers`
    挨个漏进来，各自被当成一个"架构层"或"模块"——CI 配置和 agent 提示词不是产品
    代码，把它们画进架构图只会让人第一眼就不信这张图。

    只对**父路径段**生效：文件名以点开头是另一回事（`.eslintrc.js` 是文件不是
    目录）。
    """
    parts = rel_path.split("/")
    if any(part.startswith(".") for part in parts[:-1]):
        return True
    blocked = set(DEFAULT_EXCLUDES) | set(extra)
    return any(part in blocked for part in parts)


def iter_source_files(
    root: Path, *, extra_excludes: tuple[str, ...] = (), languages: tuple[str, ...] | None = None
) -> list[SourceFile]:
    """遍历仓库内的源文件（跳过清单内目录，按语言过滤可选）。"""
    files: list[SourceFile] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if is_excluded(rel, extra_excludes):
            continue
        language = language_of(rel)
        if languages and language not in languages:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files.append(SourceFile(path=rel, content=content, language=language))
    return files


def repo_commit(root: Path) -> str | None:
    """尽力取 HEAD 提交号（取不到返回 None，不阻断）。"""
    try:
        return run(["git", "rev-parse", "HEAD"], cwd=root, timeout=5).stdout.strip() or None
    except Exception:  # noqa: BLE001 - 取不到提交号只是少一条元信息，不该中断分析
        return None


__all__ = ["SourceFile", "iter_source_files", "language_of", "repo_commit", "DEFAULT_EXCLUDES"]
