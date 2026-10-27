"""``git_log`` / ``git_show``：取代通用 shell 的两个有类型工具（设计文稿 §4.3）。

这里与 ``test_agent_tools_contract.py`` 分工不同：
契约测试问"**会不会漏**"（金丝雀、仓库外、代码执行），跑的是一个刻意恶意但
不是 git 仓库的目录；本文件问"**正常路径对不对**"，所以造一个真仓库，测
"能看到历史 / 能看某版本内容"，以及三条拒绝线：密钥文件、选项注入、越界路径。

scan-secrets: fixtures-allowed —— 本文件含故意写出的假密钥样本，用途是验证扫描器与脱敏。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codewiki.ai.registry import default_registry
from codewiki.ai.tools import MAX_GIT_LOG_LIMIT, ToolError, git_log, git_show


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("A = 1\n", encoding="utf-8")
    (root / ".env").write_text("DB_PASSWORD=NEVER_SHOW_THIS_9f2a\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "a.py", "-f")
    _git(root, "commit", "-q", "-m", "first commit")
    _git(root, "add", "-f", ".env")
    _git(root, "commit", "-q", "-m", "add env by mistake")
    return root


def test_git_log_lists_commits_for_a_path(repo):
    out = git_log(repo_root=repo, path="a.py")
    assert "first commit" in out
    assert "add env by mistake" not in out, "只给了 a.py，不该带出别的文件的提交"


def test_git_log_without_path_covers_whole_repo(repo):
    out = git_log(repo_root=repo)
    assert "first commit" in out and "add env by mistake" in out


def test_git_log_limit_is_bounded(repo):
    for bad in (0, -1, MAX_GIT_LOG_LIMIT + 1):
        with pytest.raises(ToolError, match="limit"):
            git_log(repo_root=repo, limit=bad)
    assert len(git_log(repo_root=repo, limit=1).splitlines()) == 1


def test_git_show_returns_file_content(repo):
    assert git_show(repo_root=repo, rev="HEAD", path="a.py").strip() == "A = 1"


def test_git_show_refuses_secret_file_before_reading(repo):
    """密钥文件按**名字**拒 —— 这条必须在读之前，否则明文已经进了返回值。

    外部评审实测过：``read_file`` 已拒读 ``.env``，而 ``git show HEAD:.env``
    照样把内容交出去，同一个规则两处实现就必然有一处漏。
    """
    with pytest.raises(ToolError, match="密钥"):
        git_show(repo_root=repo, rev="HEAD", path=".env")


@pytest.mark.parametrize("rev", ["--output=/tmp/pwned", "-p", "HEAD; rm -rf /", "HEAD --all", ""])
def test_git_show_refuses_option_injection_in_rev(repo, rev):
    """rev 是模型给的字符串，``--output=`` 在 git 里真能写文件（评审实测面）。"""
    with pytest.raises(ToolError, match="非法的版本引用"):
        git_show(repo_root=repo, rev=rev, path="a.py")


def test_git_show_refuses_path_outside_repo(repo, tmp_path):
    (tmp_path / "outside.txt").write_text("OUTSIDE\n", encoding="utf-8")
    for bad in ("../outside.txt", str(tmp_path / "outside.txt")):
        with pytest.raises(ToolError, match="越出仓库根目录"):
            git_show(repo_root=repo, rev="HEAD", path=bad)


def test_git_show_refuses_symlink_escape(repo, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("OUTSIDE\n", encoding="utf-8")
    (repo / "evil.txt").symlink_to(outside)
    with pytest.raises(ToolError, match="越出仓库根目录"):
        git_show(repo_root=repo, rev="HEAD", path="evil.txt")


def test_git_show_reports_missing_path_clearly(repo):
    with pytest.raises(ToolError, match="git show 失败"):
        git_show(repo_root=repo, rev="HEAD", path="nope.py")


def test_registry_wraps_git_output_as_untrusted(repo):
    """走注册表时输出要被包裹成不可信数据（工具自身只负责内容，不负责边界）。"""
    registry = default_registry(repo_root=repo)
    body = registry.call("git_show", {"rev": "HEAD", "path": "a.py"})
    assert "CODEWIKI_UNTRUSTED_DATA" in body
    assert "A = 1" in body


def test_git_tools_are_in_the_default_closed_set():
    """两个工具必须在默认闭集里 —— 否则 ``--allow-shell`` 关掉之后没有替代品，
    模型会为了看提交历史去求助于一个默认不存在的工具。"""
    names = default_registry().names
    assert {"git_log", "git_show"} <= names
