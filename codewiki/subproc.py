"""子进程入口：项目里**唯一**允许启动进程的地方（文档 §AI 层安全模型 · SubprocessRunner）。

放在包顶层而不是 ``ai/guard/`` 下，是因为**渲染层也要用它**（L2 用 mermaid-cli
真实渲染）：``render`` 不允许导入 ``ai``（见 tests/test_dependency_direction.py 的
import 扫描闸），而"子进程隔离参数只写一处"是比"归属 AI 层"更重要的约束。它只
导入标准库，任何层都可以安全依赖它。

隔离参数（``-I`` / cwd / 环境变量）写在调用点时，下一个新增的子进程调用点就会
重犯同样的错 —— 评审里 ``python -m`` 继承 cwd 导致被分析仓库的 ``json.py``
被导入并执行，就是"分散在调用点"的直接后果。所以这里把三件事一次定死：

1. **没有 shell**：``shell=False`` 恒为真，``argv`` 必须是列表；
2. **环境最小化**：只保留 :data:`ENV_ALLOW` 里的变量，密钥类环境变量不下传；
3. **项目自带脚本用** :func:`run_isolated_python`：``-I``（隔离模式，忽略
   ``PYTHONPATH`` / 用户 site / 脚本目录）+ **空临时目录作 cwd**，被分析仓库里
   的同名模块没有任何被导入的机会。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT_S = 10.0
DEFAULT_MAX_OUTPUT = 64 * 1024

ENV_ALLOW = ("PATH", "LANG", "LC_ALL", "HOME", "TERM", "TZ")


class SubprocessTimeout(Exception):
    """子进程超时（已被 SIGKILL）。"""


class SubprocessFailed(Exception):
    """子进程无法启动（可执行文件不存在 / 无权限）。"""


@dataclass(frozen=True)
class Completed:
    """一次子进程调用结果（stdout / stderr 已按上限截断）。

    ``truncated`` 必须由这里给出：调用方拿到的是已截断的文本，自己比较
    ``len(stdout) > max_output`` 永远为假 —— sandbox 早先就是这么判的，
    于是"输出被截断"这件事从未被上报过。
    """

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    truncated: bool = False


def minimal_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """最小环境：白名单变量 + 调用方显式声明的 ``extra``。"""
    env = {key: os.environ[key] for key in ENV_ALLOW if key in os.environ}
    if extra:
        env.update(extra)
    return env


def run(
    argv: Sequence[str],
    *,
    cwd: Path | str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    input: str | None = None,
    env: Mapping[str, str] | None = None,
    max_output: int = DEFAULT_MAX_OUTPUT,
) -> Completed:
    """跑一条命令，返回 :class:`Completed`。

    Raises:
        SubprocessTimeout: 超时（进程已被杀掉，不会留下后台子进程）。
        SubprocessFailed: 无法启动。
    """
    args = [str(a) for a in argv]
    try:
        done = subprocess.run(  # noqa: S603 - argv 由调用方固定，shell 恒为 False
            args,
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else minimal_env(),
            input=input,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SubprocessTimeout(f"超时（{timeout:g}s）") from exc
    except OSError as exc:
        raise SubprocessFailed(f"无法启动 {args[0]}: {exc}") from exc
    return Completed(
        argv=tuple(args),
        returncode=done.returncode,
        stdout=(done.stdout or "")[:max_output],
        stderr=(done.stderr or "")[:max_output],
        truncated=len(done.stdout or "") > max_output or len(done.stderr or "") > max_output,
    )


def run_isolated_python(
    script: Path,
    *,
    payload: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    max_output: int = DEFAULT_MAX_OUTPUT,
) -> Completed:
    """用**隔离模式**跑项目自带的 Python 脚本（worker），cwd 是一个空临时目录。

    ``-I`` 等价于 ``-E -s -P``：忽略 ``PYTHONPATH``、用户 site-packages，并且
    **不把 cwd / 脚本目录塞进 ``sys.path``**。配合空 cwd，被分析仓库里的
    ``json.py`` / ``re.py`` 无法抢在标准库之前被导入 —— 这是"分析不可信仓库
    等于执行任意代码"那条越权的封堵点。
    """
    env = minimal_env(extra={"PYTHONIOENCODING": "utf-8"})
    with tempfile.TemporaryDirectory(prefix="codewiki-subproc-") as clean_cwd:
        return run(
            [sys.executable, "-I", str(script)],
            cwd=clean_cwd,
            timeout=timeout,
            input=payload,
            env=env,
            max_output=max_output,
        )


__all__ = [
    "DEFAULT_MAX_OUTPUT",
    "DEFAULT_TIMEOUT_S",
    "ENV_ALLOW",
    "Completed",
    "SubprocessFailed",
    "SubprocessTimeout",
    "minimal_env",
    "run",
    "run_isolated_python",
]
