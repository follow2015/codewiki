"""``scripts/check_release_tag.py`` 的行为契约。

这道闸守的是「产物自相矛盾」：tag 说 ``v1.2.0``、装上去的是 1.1.0。它此前是
release.yml 里一段内联 ``python -c``，**只有打 tag 才会执行** —— 写错了要到发版
当天才发现。抽成脚本后平时就有人跑（本文件），本地也能预先验。

断言都打在**退出码 + stderr 文案**上：退出码是流水线的判据，文案是给人看的；
只断言其中一个，另一种退化（码对而理由错、理由对而码错）就漏掉了。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parents[1]
SCRIPT = REPO / "scripts" / "check_release_tag.py"

EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_USAGE = 2


def _run(*args: str, cwd: Path, tag_env: str | None = None) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if key != "GITHUB_REF_NAME"}
    if tag_env is not None:
        env["GITHUB_REF_NAME"] = tag_env
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


@pytest.fixture
def project(tmp_path: Path, request) -> Path:
    """最小 pyproject；需要时用 ``indirect`` 传 version 字符串（``None`` = 不写）。"""
    version = getattr(request, "param", "1.0.0")
    body = '[project]\nname = "demo"\n'
    if version is not None:
        body += f'version = "{version}"\n'
    (tmp_path / "pyproject.toml").write_text(body, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("tag", ["v1.0.0", "1.0.0"])
def test_matching_tag_passes_with_or_without_v_prefix(tag: str, project: Path):
    """``v`` 前缀是 tag 的写法习惯，不是版本的一部分 —— 两种都必须放行。"""
    result = _run("--tag", tag, cwd=project)
    assert result.returncode == EXIT_OK, result.stderr
    assert "版本一致" in result.stdout


def test_tag_is_stripped_before_comparison(project: Path):
    """tag 来自 shell/环境变量，带空白是常事；空白不该被当成"版本不同"。"""
    result = _run("--tag", "  v1.0.0  ", cwd=project)
    assert result.returncode == EXIT_OK, result.stderr


def test_mismatch_fails_and_says_what_is_wrong(project: Path):
    result = _run("--tag", "v1.1.0", cwd=project)
    assert result.returncode == EXIT_MISMATCH
    assert "不一致" in result.stderr
    assert "v1.1.0" in result.stderr
    assert "1.0.0" in result.stderr


def test_pre_release_tag_is_not_silently_normalized(project: Path):
    """刻意不做 PEP 440 正规化：``1.0.0-rc1`` 与 ``1.0.0`` 是两回事。

    如果这里放行，就等于允许"预发布 tag 发出正式版本"——那是最难事后发现的一类
    错发。要发预发布版，pyproject 里也写同一个串。
    """
    result = _run("--tag", "v1.0.0-rc1", cwd=project)
    assert result.returncode == EXIT_MISMATCH, "预发布 tag 不得被正规化成正式版"


def test_uppercase_v_is_not_treated_as_a_prefix(project: Path):
    """只认小写 ``v``：``V1.0.0`` 去掉前缀等于没去，必须判不一致。"""
    result = _run("--tag", "V1.0.0", cwd=project)
    assert result.returncode == EXIT_MISMATCH


def test_empty_tag_is_an_input_error_not_a_mismatch(project: Path):
    """空 tag 是"输入没给"，不是"版本不一致" —— 退出码要区分，否则排查方向跑偏。"""
    result = _run("--tag", "", cwd=project)
    assert result.returncode == EXIT_USAGE
    assert "没有 tag" in result.stderr


def test_tag_comes_from_the_environment_by_default(tmp_path: Path, project: Path):
    """release.yml 传的是 ``$GITHUB_REF_NAME``；不传 ``--tag`` 时必须读环境变量。"""
    result = _run(cwd=project, tag_env="v1.0.0")
    assert result.returncode == EXIT_OK, result.stderr


def test_missing_environment_tag_is_rejected(project: Path):
    result = _run(cwd=project, tag_env=None)
    assert result.returncode == EXIT_USAGE


@pytest.mark.parametrize("project", [None], indirect=True)
def test_missing_version_in_pyproject_is_an_input_error(project: Path):
    """没有 version 就无从比较；返回 0 会让"两边都空"糊过去。"""
    result = _run("--tag", "v1.0.0", cwd=project)
    assert result.returncode == EXIT_USAGE
    assert "project.version" in result.stderr


def test_missing_pyproject_is_reported_clearly(tmp_path: Path):
    result = _run("--tag", "v1.0.0", "--pyproject", "nope.toml", cwd=tmp_path)
    assert result.returncode == EXIT_USAGE
    assert "找不到" in result.stderr


def test_script_agrees_with_the_real_pyproject():
    """对真仓库自洽：用真 pyproject 的 version 造 tag，必须放行。

    没有这条，上面所有用例都可以在一个"永远返回 0"的实现上全绿。
    """
    import tomllib

    version = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    result = _run("--tag", f"v{version}", cwd=REPO)
    assert result.returncode == EXIT_OK, result.stderr
