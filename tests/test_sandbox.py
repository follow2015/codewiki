"""沙箱判据（§5.4）—— 危险命令**必须被拒绝且未执行**。

判据写法有一条纪律：断言"被拒绝"是不够的（一个拒绝一切的实现也能全绿），
所以每个拒绝用例都配一组**白名单正例**，证明沙箱只是窄，不是死。

拒绝用例断言的是 :class:`ShellDenied` —— 抛异常意味着命令**根本没进子进程**。
"""
from __future__ import annotations

import os

import pytest

from codewiki.ai.sandbox import (
    ALLOWED_COMMANDS,
    ShellDenied,
    _clean_env,
    check_command,
    execute_shell,
)
from codewiki.subproc import SubprocessTimeout

DANGEROUS = [
    ("rm -rf /", "程序不在白名单"),
    ("sh -c 'ls'", "shell 解释器不在白名单"),
    ("curl http://evil.example", "网络命令不在白名单"),
    ("cat a; rm -rf /", "分号拼接"),
    ("cat a | grep b", "管道"),
    ("cat a > out.txt", "重定向"),
    ("cat a >> out.txt", "追加重定向"),
    ("echo $(whoami)", "命令替换"),
    ("cat `whoami`", "反引号"),
    ("cat a && rm b", "逻辑与"),
    ("cat ../../etc/passwd", ".. 越界"),
    ("cat /etc/passwd", "绝对路径越界"),
    ("git push origin main", "git 子命令不在白名单"),
    ("git", "git 缺子命令"),
    ("find . -name '*.py' -exec rm -rf {} +", "find -exec 挂任意程序"),
    ("find . -name '*.py' -exec wc -l {} +", "find -exec 即便只挂 wc 也必须拒绝"),
    ("find . -name '*.tmp' -delete", "find -delete 删文件"),
    ("find . -execdir wc {} +", "find -execdir"),
    ("git grep --open-files-in-pager foo", "git grep 打开 pager（core.pager 可执行任意命令）"),
    ("git grep -O foo", "git grep -O 是 --open-files-in-pager 短形式"),
    ("git diff --ext-diff a b", "显式启用外部 diff driver"),
    ("git log -p --ext-diff", "log 也接受 --ext-diff"),
    ("git show --textconv HEAD", "textconv driver 可执行任意命令"),
]


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "a.txt").write_text("hello\nworld\n", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "b.txt").write_text("inner\n", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(("command", "reason"), DANGEROUS)
def test_dangerous_commands_are_rejected(repo, command, reason):
    with pytest.raises(ShellDenied) as exc:
        execute_shell(command, repo_root=repo)
    assert reason
    assert exc.value.args


def test_allowed_commands_still_work(repo):
    """窄而不死：白名单命令必须真能跑（否则"全拒绝"也能让上面全绿）。"""
    assert "a.txt" in execute_shell("ls", repo_root=repo).stdout
    assert "hello" in execute_shell("cat a.txt", repo_root=repo).stdout
    assert "inner" in execute_shell("cat pkg/b.txt", repo_root=repo).stdout
    assert execute_shell("git log", repo_root=repo).returncode in (0, 128)


def test_symlink_escape_is_rejected(repo, tmp_path):
    """符号链接逃逸：链接在仓内、目标在仓外 ⇒ realpath 后越界。"""
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (repo / "link.txt").symlink_to(outside)

    with pytest.raises(ShellDenied):
        execute_shell("cat link.txt", repo_root=repo)


def test_output_is_truncated(repo):
    big = repo / "big.txt"
    big.write_text("x" * 200_000, encoding="utf-8")
    result = execute_shell("cat big.txt", repo_root=repo, max_output=1024)
    assert result.truncated and len(result.stdout) == 1024


def test_timeout_is_reported_not_raised(repo, monkeypatch):
    """超时是**结果**不是异常：调用方要能拿到已产出的部分输出并继续。"""

    def _boom(*_a, **_k):
        raise SubprocessTimeout("超时（0.01s）")

    monkeypatch.setattr("codewiki.ai.sandbox.run", _boom)
    result = execute_shell("cat a.txt", repo_root=repo, timeout_s=0.01)
    assert result.timed_out and result.returncode == 124


def test_env_is_scrubbed(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("MY_SECRET", "nope")
    env = _clean_env()
    assert "OPENAI_API_KEY" not in env and "MY_SECRET" not in env
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"


def test_argv_is_stored_for_audit(repo):
    result = execute_shell("cat a.txt", repo_root=repo)
    assert result.argv == ("cat", "a.txt")


def test_check_command_does_not_execute(repo):
    """``check_command`` 是纯校验：给定危险命令只返回/抛错，绝不执行。"""
    with pytest.raises(ShellDenied):
        check_command("rm -rf /", repo_root=repo)
    assert check_command("cat a.txt", repo_root=repo) == ["cat", "a.txt"]


def test_whitelist_is_explicit():
    """白名单是可审计的闭集：新增命令必须同时补用例。"""
    assert set(ALLOWED_COMMANDS) == {"git", "grep", "find", "ls", "cat", "tree", "wc", "head", "tail"}
    assert ALLOWED_COMMANDS["git"] is not None and "push" not in ALLOWED_COMMANDS["git"]


def test_find_without_action_predicates_still_works(repo):
    """H1 正例：不带动作谓词的 find 必须照常可用。"""
    out = execute_shell("find . -name '*.txt'", repo_root=repo).stdout
    assert "a.txt" in out and "b.txt" in out


def _init_git_repo(tmp_path):
    import subprocess as sp

    def run(*args):
        return sp.run(["git", "-C", str(tmp_path), *args], capture_output=True, text=True,
                      env={"PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1",
                           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})

    run("init", "-q")
    (tmp_path / "f.txt").write_text("hello\n", encoding="utf-8")
    run("add", ".")
    run("commit", "-qm", "init")
    return run


def test_git_diff_does_not_run_external_diff_driver(tmp_path):
    """H2 行为级：仓库把 diff.external 指到任意脚本，git diff 也不得执行它。"""
    run = _init_git_repo(tmp_path)
    marker = tmp_path / "PWNED"
    (tmp_path / "fake-diff.sh").write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    fake = tmp_path / "fake-diff.sh"
    fake.chmod(0o755)
    run("config", "diff.external", str(fake))
    (tmp_path / "f.txt").write_text("changed\n", encoding="utf-8")

    result = execute_shell("git diff", repo_root=tmp_path)
    assert not marker.exists(), "外部 diff driver 被执行了 —— 沙箱被仓库配置劫持"
    assert result.returncode in (0, 1)


def test_git_grep_does_not_invoke_pager(tmp_path):
    """H2 行为级：core.pager 指到任意脚本，git grep 也不得执行它。"""
    run = _init_git_repo(tmp_path)
    marker = tmp_path / "PWNED"
    fake = tmp_path / "fake-pager.sh"
    fake.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    fake.chmod(0o755)
    run("config", "core.pager", str(fake))

    result = execute_shell("git grep hello", repo_root=tmp_path)
    assert not marker.exists(), "core.pager 被执行了 —— 沙箱被仓库配置劫持"
    assert "hello" in result.stdout
    assert result.returncode == 0


def test_git_diff_plain_still_works(tmp_path):
    """H2 正例：注入防劫持参数后，普通 git diff 必须照常出结果。"""
    _init_git_repo(tmp_path)
    (tmp_path / "f.txt").write_text("changed\n", encoding="utf-8")
    result = execute_shell("git diff", repo_root=tmp_path)
    assert result.returncode == 0
    assert "changed" in result.stdout


def test_viewer_short_flags_may_not_dereference_symlinks(repo):
    """`ls -L` / `tree -l` 必须被拒 —— jail 只校验路径参数，管不到命令语义。

    仓库内放一个指向仓库外的符号链接，`ls -RL .` 就能把 /etc 列出来：路径
    参数 `.` 完全合法，越界发生在"命令跟着链接走"这一步。
    """
    for cmd in ("ls -RL .", "ls -L .", "tree -l", "tree -al la"):
        with pytest.raises(ShellDenied):
            check_command(cmd, repo_root=repo)
    assert check_command("tree -L 2", repo_root=repo) == ["tree", "-L", "2"]


def test_common_viewer_flag_combinations_still_work(repo):
    """收紧只针对"跟随链接"的那几个字母，日常查看组合不受影响。"""
    for cmd in ("ls -la", "ls -lR .", "ls -1", "cat -n a.txt", "wc -l a.txt"):
        check_command(cmd, repo_root=repo)
