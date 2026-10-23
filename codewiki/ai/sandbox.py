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
import re
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


@dataclass(frozen=True)
class _OptionPolicy:
    """一个命令的选项白名单。

    ``plain``：无值长选项；``value``：带值长选项（值可为模式/文本，**不是路径**，
    不参与 jail —— 如 ``-name`` 的 glob）；``short_plain`` / ``short_value``：
    短旗标字母集合（``-rn`` 拆簇逐个验；``short_value`` 的字母后面可粘数字，
    如 ``-A2`` / ``-U3``）；``viewer``：查看类命令（ls/cat/wc/head/tail/tree）
    —— 其短旗标无论怎么簇都不可能写文件或外读，整簇放行，未知长选项仅当
    带值且值过 jail 时才放行。
    """

    plain: frozenset[str] = frozenset()
    value: frozenset[str] = frozenset()
    short_plain: str = ""
    short_value: str = ""
    viewer: bool = False


_GIT_PLAIN = frozenset(
    {
        "--patch", "--stat", "--shortstat", "--numstat", "--name-only", "--name-status",
        "--oneline", "--no-color", "--color", "--no-ext-diff", "--no-textconv", "--full-index",
        "--no-merges", "--first-parent", "--all", "--graph", "--reverse", "--ignore-all-space",
        "--ignore-blank-lines", "--no-renames", "--cached", "--staged", "--exit-code", "--quiet",
        "--follow", "--no-decorate", "--short", "--porcelain", "--others", "--full-name",
        "--directory", "--error-unmatch", "--exclude-standard", "--line-number",
        "--files-with-matches", "--no-pager", "--invert-match", "--fixed-strings",
        "--extended-regexp", "--perl-regexp", "--ignore-case", "--word-regexp", "--summary",
        "--check", "--text", "--no-commit-id", "--binary", "--break-rewrites",
    }
)
_GIT_VALUE = frozenset(
    {
        "--abbrev", "--decorate", "--unified", "--pretty", "--format", "--date",
        "--max-count", "--skip", "--since", "--until", "--author", "--committer",
        "--grep", "--src-prefix", "--dst-prefix", "--diff-filter", "--max-depth",
    }
)
_GIT_SHORT_PLAIN = "psnliwvEFhHabRg"
_GIT_SHORT_VALUE = "Um"

_GREP_PLAIN = frozenset({"--color", "--no-color", "--null", "--count", "--files-with-matches", "--line-number"})
_GREP_VALUE = frozenset({"--include", "--exclude", "--exclude-dir", "--max-count", "--label"})
_GREP_SHORT_PLAIN = "rnivwEFlchHoqsabPx"
_GREP_SHORT_VALUE = "ABCme"

_FIND_PLAIN = frozenset(
    {"-print", "-print0", "-depth", "-L", "-P", "-H", "-not", "-a", "-o", "-empty", "-prune", "-xdev", "-quit"}
)
_FIND_VALUE = frozenset(
    {
        "-name", "-iname", "-lname", "-type", "-path", "-ipath", "-maxdepth", "-mindepth",
        "-size", "-mtime", "-mmin", "-ctime", "-atime", "-newermt", "-perm", "-user", "-group",
        "-links", "-samefile-name",
    }
)

_VIEWER_PLAIN = frozenset(
    {
        "--color", "--no-color", "--noreport", "--no-report", "--dirsfirst", "--all",
        "--almost-all", "--ignore-backups", "--escape", "--literal", "--quote-name",
        "--reverse", "--recursive", "--human-readable", "--si", "--inode", "--identity",
    }
)

_OPTION_POLICY: dict[str, _OptionPolicy] = {
    "git": _OptionPolicy(_GIT_PLAIN, _GIT_VALUE, _GIT_SHORT_PLAIN, _GIT_SHORT_VALUE),
    "grep": _OptionPolicy(_GREP_PLAIN, _GREP_VALUE, _GREP_SHORT_PLAIN, _GREP_SHORT_VALUE),
    "find": _OptionPolicy(_FIND_PLAIN, _FIND_VALUE),
    "ls": _OptionPolicy(viewer=True),
    "cat": _OptionPolicy(viewer=True),
    "wc": _OptionPolicy(viewer=True),
    "head": _OptionPolicy(viewer=True),
    "tail": _OptionPolicy(viewer=True),
    "tree": _OptionPolicy(viewer=True),
}


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


def _jail_check(arg: str, *, root: Path) -> None:
    """路径参数越界即拒绝（``realpath`` 展开 ``..`` 与符号链接）。

    **所有**非选项参数都要过这道检查 —— 曾经按 :func:`_is_path_arg`
    （「含 ``/`` 或 ``.``」）筛选，于是 ``ls -R dirlink`` 里指向仓库外的符号
    链接目录整条漏检，能列出仓库外的目录名（外部评审实测面）。不含分隔符的
    取值（``grep`` 的模式、``find`` 的谓词取值）解析后仍落在 root 内，不会被误拒。
    """
    if arg.startswith("-") or not arg:
        return
    candidate = (Path(arg) if Path(arg).is_absolute() else root / arg).resolve()
    if candidate != root and root not in candidate.parents:
        raise ShellDenied(f"路径越出仓库根目录: {arg!r}")


def _check_option(arg: str, policy: _OptionPolicy, *, root: Path, program: str) -> bool:
    """校验一个选项；返回是否消耗下一个参数（带值选项的分离取值形式）。

    长形式（含 find 的单杠风格 ``-name`` / ``-maxdepth``）先查 plain/value 表；
    再按短旗标簇处理（``-rn`` 拆簇逐个验，``-A2`` 的粘数字是取值）。
    """
    if arg == "-" or re.fullmatch(r"-\d+", arg):
        return False

    name, eq, embedded = arg.partition("=")

    if len(name) > 2 and name in (policy.plain | policy.value):
        is_value_opt = name in policy.value
        if is_value_opt:
            return not eq
        if eq:
            _jail_check(embedded, root=root)
        return False

    if name.startswith("--"):
        if policy.viewer:
            if name in _VIEWER_PLAIN:
                return False
            if eq:
                _jail_check(embedded, root=root)
                return False
            raise ShellDenied(f"{program} 长选项不在白名单: {arg!r}")
        raise ShellDenied(
            f"{program} 长选项不在白名单: {arg!r}（选项内嵌路径曾是越狱通道，一律走白名单）"
        )

    letters = arg[1:]
    if policy.viewer:
        return False
    if not re.fullmatch(r"[A-Za-z0-9]+", letters):
        raise ShellDenied(f"{program} 短旗标形状非法: {arg!r}")
    consumes_next = False
    i = 0
    while i < len(letters):
        ch = letters[i]
        if ch in policy.short_plain:
            i += 1
            continue
        if ch in policy.short_value:
            j = i + 1
            while j < len(letters) and letters[j].isdigit():
                j += 1
            if j == i + 1:
                consumes_next = True
            i = j
            continue
        raise ShellDenied(f"{program} 短旗标不在白名单: {arg!r}")
    return consumes_next


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

    policy = _OPTION_POLICY[program]
    allowed_sub = ALLOWED_COMMANDS[program]

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

    for arg in argv:
        for token in _FORBIDDEN_TOKENS:
            if token in arg:
                raise ShellDenied(f"参数含危险元字符 {token!r}: {arg!r}（禁止管道/重定向/命令替换）")

    root = repo_root.resolve()
    subcommand_seen = False
    index = 1
    while index < len(argv):
        arg = argv[index]
        index += 1
        if not arg.startswith("-"):
            if program == "git" and not subcommand_seen:
                if arg not in (allowed_sub or set()):
                    raise ShellDenied(f"{program} 子命令不在白名单: {arg!r}（允许: {sorted(allowed_sub or set())}）")
                subcommand_seen = True
                continue
            _jail_check(arg, root=root)
            continue
        if program == "git" and not subcommand_seen:
            raise ShellDenied(f"git 全局选项被禁止: {arg!r}（--git-dir/-C 可把 jail 换成任意目录）")
        if _check_option(arg, policy, root=root, program=program):
            if index < len(argv):
                index += 1
            else:
                raise ShellDenied(f"{arg} 缺少取值")

    if program == "git" and not subcommand_seen:
        raise ShellDenied(f"git 缺少子命令（允许: {sorted(allowed_sub or set())}）")
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

    if argv[0] == "git" and next(
        (a for a in argv[1:] if not a.startswith("-")), None
    ) in _GIT_FORCE_NO_DIFF_SUBCOMMANDS:
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
