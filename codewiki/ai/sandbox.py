"""只读 Shell 沙箱（§5.4）。

六层防御，任一层拒绝即终止该次调用：

1. **命令白名单**：仅 ``git``（限子命令）/ ``grep`` / ``find`` / ``ls`` / ``cat`` /
   ``tree`` / ``wc`` / ``head`` / ``tail``；
2. **禁止 ``shell=True``**：``shlex.split`` 成参数数组，从根上消除管道、重定向、
   命令替换；参数里出现元字符一律拒绝；
3. **路径 jail**：所有路径参数 ``realpath`` 后必须落在 ``repo_root`` 内（含符号
   链接逃逸与 ``..`` 越界）；
4. **资源限制**：超时 / 输出截断 / 单子进程 / 清空疑似携带密钥的环境变量；
5. **审计**（由 :mod:`codewiki.ai.audit` 承担）；
6. **提示词注入防护**（由 :mod:`codewiki.ai.tools` 的包裹与扫描承担）。

为什么元字符要**逐个参数**查而不是只看整串：``shlex.split("cat a; rm -rf /")``
会得到 ``["cat", "a;", "rm", "-rf", "/"]`` —— 用参数数组执行时 ``;`` 只是个普通
字符、不会触发第二个命令，但它说明这条命令来自拼接而非白名单意图，且 ``rm``
不在白名单里会被第 1 层拦掉。两道都查是纵深，不是重复。
"""
from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

ALLOWED_COMMANDS: dict[str, set[str] | None] = {
    "git": {"log", "show", "diff", "grep", "rev-parse", "ls-files", "status"},
    "grep": None,
    "find": None,
    "ls": None,
    "cat": None,
    "tree": None,
    "wc": None,
    "head": None,
    "tail": None,
}

_FORBIDDEN_TOKENS = (";", "|", ">", "<", "&", "$(", "`", "\n", "\r")

_FIND_DENIED_PREDICATES = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete"})

_GIT_DENIED_FLAGS = frozenset({"--open-files-in-pager", "--ext-diff", "--textconv", "-c", "--config-env"})

_GIT_FORCE_NO_DIFF_SUBCOMMANDS = frozenset({"diff", "log", "show"})

DEFAULT_TIMEOUT_S = 10.0
DEFAULT_MAX_OUTPUT = 64 * 1024


class ShellDenied(Exception):
    """命令被沙箱拒绝（**未执行**）。"""


@dataclass(frozen=True)
class ShellResult:
    """一次执行的结果（拒绝时抛 :class:`ShellDenied`，不返回本对象）。"""

    command: str
    returncode: int
    stdout: str
    stderr: str
    truncated: bool = False
    timed_out: bool = False
    argv: tuple[str, ...] = field(default_factory=tuple)


def _clean_env() -> dict[str, str]:
    """只保留最必要的环境，并清掉可能携带密钥的变量。"""
    keep = {}
    for key in ("PATH", "LANG", "LC_ALL", "HOME", "TERM", "TZ"):
        value = os.environ.get(key)
        if value:
            keep[key] = value
    keep["GIT_CONFIG_NOSYSTEM"] = "1"
    keep["GIT_PAGER"] = "cat"
    keep["PAGER"] = "cat"
    keep["GIT_TERMINAL_PROMPT"] = "0"
    return keep


def _is_path_arg(arg: str) -> bool:
    """粗判一个参数是否像路径（选项与其取值不算）。"""
    if arg.startswith("-"):
        return False
    if not arg:
        return False
    return "/" in arg or "." in arg or arg in {"*"}


def check_command(command: str, *, repo_root: Path) -> list[str]:
    """静态校验：通过返回 argv，不通过抛 :class:`ShellDenied`。"""
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ShellDenied(f"命令无法解析为参数数组: {exc}") from exc
    if not argv:
        raise ShellDenied("空命令")

    program = argv[0]
    if program not in ALLOWED_COMMANDS:
        raise ShellDenied(f"命令不在白名单: {program!r}（允许: {sorted(ALLOWED_COMMANDS)}）")

    allowed_sub = ALLOWED_COMMANDS[program]
    subcommand = next((a for a in argv[1:] if not a.startswith("-")), None)
    if allowed_sub is not None:
        if subcommand is None:
            raise ShellDenied(f"{program} 缺少子命令（允许: {sorted(allowed_sub)}）")
        if subcommand not in allowed_sub:
            raise ShellDenied(f"{program} 子命令不在白名单: {subcommand!r}（允许: {sorted(allowed_sub)}）")

    if program == "find":
        for arg in argv[1:]:
            if arg in _FIND_DENIED_PREDICATES:
                raise ShellDenied(
                    f"find 动作谓词被禁止: {arg!r}（-exec/-execdir/-ok 可把任意程序挂进 find，-delete 删文件）"
                )

    if program == "git":
        for arg in argv[1:]:
            if arg in _GIT_DENIED_FLAGS or arg.startswith("--open-files-in-pager"):
                raise ShellDenied(
                    f"git 参数被禁止: {arg!r}（pager/diff driver/运行时配置可被仓库本地配置劫持为任意命令）"
                )
        if subcommand == "grep" and any(a == "-O" for a in argv[1:]):
            raise ShellDenied("git grep -O 等价于 --open-files-in-pager（core.pager 劫持面），被禁止")

    for arg in argv:
        for token in _FORBIDDEN_TOKENS:
            if token in arg:
                raise ShellDenied(f"参数含危险元字符 {token!r}: {arg!r}（禁止管道/重定向/命令替换）")

    root = repo_root.resolve()
    for arg in argv[1:]:
        if not _is_path_arg(arg):
            continue
        candidate = (Path(arg) if Path(arg).is_absolute() else root / arg).resolve()
        if candidate != root and root not in candidate.parents:
            raise ShellDenied(f"路径越出仓库根目录: {arg!r}")
    return argv


def execute_shell(
    command: str,
    *,
    repo_root: Path,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_output: int = DEFAULT_MAX_OUTPUT,
) -> ShellResult:
    """在沙箱里执行一条只读命令。

    Raises:
        ShellDenied: 任一防御层拒绝（**命令不会被执行**）。
    """
    argv = check_command(command, repo_root=repo_root)
    root = repo_root.resolve()

    if argv[0] == "git":
        if next((a for a in argv[1:] if not a.startswith("-")), None) in _GIT_FORCE_NO_DIFF_SUBCOMMANDS:
            argv = [*argv, "--no-ext-diff", "--no-textconv"]

    try:
        completed = subprocess.run(  # noqa: S603 - argv 已过白名单，且 shell=False
            argv,
            cwd=root,
            env=_clean_env(),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_s,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return ShellResult(
            command=command,
            returncode=124,
            stdout="",
            stderr=f"超时（{timeout_s}s）",
            timed_out=True,
            argv=tuple(argv),
        )
    except FileNotFoundError as exc:
        raise ShellDenied(f"命令不存在: {argv[0]}") from exc

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    truncated = len(stdout) > max_output or len(stderr) > max_output
    return ShellResult(
        command=command,
        returncode=completed.returncode,
        stdout=stdout[:max_output],
        stderr=stderr[:max_output],
        truncated=truncated,
        argv=tuple(argv),
    )


__all__ = [
    "ALLOWED_COMMANDS",
    "DEFAULT_MAX_OUTPUT",
    "DEFAULT_TIMEOUT_S",
    "ShellDenied",
    "ShellResult",
    "check_command",
    "execute_shell",
]
