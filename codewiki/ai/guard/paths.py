"""路径策略：仓库边界只有一处实现（文档 §AI 层安全模型 · PathPolicy）。

被分析的仓库是**不可信输入**：它的符号链接可以指向 ``/etc/shadow``，它的
``..`` 可以爬到任意位置。所以"这个路径是否还在仓库内"只在这里回答一次，
``read_file`` / ``search_codebase`` / 沙箱 / 导出都只能调用它。

为什么必须用 ``resolve()``：``Path`` 的字符串拼接不做任何归一化，
``root / "evil"``（``evil -> /etc/x``）看起来像在仓库内，读出来却是仓库外的
文件 —— 评审中 ``search_codebase`` 正是这样读到外部内容的。
"""

from __future__ import annotations

from pathlib import Path


class PathDenied(Exception):
    """路径越出仓库根目录（``..``、绝对路径越界、符号链接逃逸）。"""


def resolve_in_root(path: str | Path, *, root: Path) -> Path:
    """把 ``path`` 解析到 ``root`` 内；越界抛 :class:`PathDenied`。

    绝对路径按"必须在 root 内"处理，而不是直接信任 —— 模型给出的
    ``/Users/<me>/.ssh/id_rsa`` 与 ``../../etc/passwd`` 是同一类越权尝试。
    """
    base = root.resolve()
    candidate = Path(path)
    target = (candidate if candidate.is_absolute() else base / candidate).resolve()
    if target != base and base not in target.parents:
        raise PathDenied(f"路径越出仓库根目录: {path}")
    return target


def relative_to_root(path: Path, *, root: Path) -> str:
    """返回相对仓库根的 POSIX 形式（模型的引用约定是 ``file:line``）。

    绝对路径会把本机目录结构带进上下文，既不必要也不安全。
    """
    base = root.resolve()
    try:
        return path.resolve().relative_to(base).as_posix()
    except ValueError as exc:
        raise PathDenied(f"路径越出仓库根目录: {path}") from exc


__all__ = ["PathDenied", "relative_to_root", "resolve_in_root"]
