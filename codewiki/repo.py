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


def nested_repo_dirs(root: Path) -> list[str]:
    """``root`` 直属子目录里**自带 ``.git``** 的那些，按名字排序。

    只看事实，不猜用途：子目录里有 ``.git``（目录，或 submodule/worktree 的
    ``gitdir:`` 文件）就说明它是**一份独立仓库** —— 部署副本、另一份克隆、submodule
    都算。把它扫进来等于同一份代码数两遍：实测某个仓库因此凭空多出 1090 个模块，
    还单独占掉一个"层"。

    只取**顶层名**：``[analysis] exclude`` 是按目录名（任意路径段）匹配的
    （见 :func:`is_excluded`），多段路径写进去永远匹配不上；只取顶层名，语义正好与
    匹配规则对齐，也不会误伤 ``app/vendor`` 这种深层同名目录。
    """
    found: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.is_symlink():
            continue
        try:
            if (child / ".git").exists():
                found.append(child.name)
        except OSError:
            continue
    return found


def repo_commit(root: Path) -> str | None:
    """尽力取 HEAD 提交号（取不到返回 None，不阻断）。"""
    try:
        return run(["git", "rev-parse", "HEAD"], cwd=root, timeout=5).stdout.strip() or None
    except Exception:  # noqa: BLE001 - 取不到提交号只是少一条元信息，不该中断分析
        return None


__all__ = [
    "DEFAULT_EXCLUDES",
    "SourceFile",
    "is_excluded",
    "iter_source_files",
    "language_of",
    "nested_repo_dirs",
    "repo_commit",
]
