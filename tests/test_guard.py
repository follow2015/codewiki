"""AI 层安全边界（``codewiki/ai/guard/``）的回归用例。

这组用例对应《codewiki 重构与优化设计文档》§AI 层安全模型里的"一道结构性的闸"：
造一个**恶意仓库**（金丝雀密钥、指向仓库外的符号链接、仓库根的 ``json.py``、
被改写就暴露的普通代码），对每条"模型能读到什么"的路径调用一遍，断言：

1. 金丝雀从未出现在输出里；
2. 仓库外的路径读不到；
3. 被分析仓库里的代码**没有被执行**；
4. 普通代码没有被脱敏改坏。

每条用例都对应一次实测过的越权：它们应当先在旧实现上变红，再在这里变绿。

scan-secrets: fixtures-allowed —— 本文件含故意写出的假密钥样本，用途是验证扫描器与脱敏。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from codewiki.ai.guard import (
    PathDenied,
    is_secret_file,
    redact_audit,
    redact_model,
    relative_to_root,
    resolve_in_root,
)
from codewiki.ai.tools import ToolError, read_file, run_shell, search_codebase

CANARY = "canary-MUST-NOT-LEAK-9f3a"
CANARY_ENV = f"DB_PASSWORD={CANARY}\nSTRIPE=sk_live_canarytest0123456789\n"
OUTSIDE_SECRET = "outside-secret-MUST-NOT-LEAK-77b1"


@pytest.fixture
def evil_repo(tmp_path: Path) -> Path:
    """恶意仓库：金丝雀、外部符号链接、仓库根的同名标准库模块、普通代码。"""
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / ".env").write_text(CANARY_ENV, encoding="utf-8")
    (root / "a.txt").write_text("hello\n", encoding="utf-8")
    (root / "src" / "cfg.py").write_text(
        "MAX_TOKENS = 4096\ntoken_count = 0\npassword = get_password()\n", encoding="utf-8"
    )

    outside = tmp_path / "outside.txt"
    outside.write_text(f"{OUTSIDE_SECRET}\n", encoding="utf-8")
    (root / "leak.txt").symlink_to(outside)
    (root / "leakdir").symlink_to(tmp_path, target_is_directory=True)
    return root


def test_resolve_rejects_escape_and_accepts_inside(evil_repo):
    inside = resolve_in_root("src/cfg.py", root=evil_repo)
    assert inside == (evil_repo / "src" / "cfg.py").resolve()
    for bad in ("../outside.txt", "/etc/hosts", "leak.txt", "leakdir/outside.txt"):
        with pytest.raises(PathDenied):
            resolve_in_root(bad, root=evil_repo)


def test_relative_path_form_never_leaks_absolute_path(evil_repo):
    target = resolve_in_root("src/cfg.py", root=evil_repo)
    assert relative_to_root(target, root=evil_repo) == "src/cfg.py"


def test_regex_worker_ignores_planted_stdlib_shadow(evil_repo, tmp_path, monkeypatch):
    """``python -m`` 会把 cwd 顶到 sys.path 首位 —— 仓库根放个 json.py 就能执行。"""
    marker = tmp_path / "PWNED"
    (evil_repo / "json.py").write_text(f"open({str(marker)!r}, 'w').write('ran')\n", encoding="utf-8")
    monkeypatch.chdir(evil_repo)

    hits = search_codebase("hello", repo_root=evil_repo)

    assert not marker.exists(), "worker 导入并执行了被分析仓库里的 json.py"
    assert [h["path"] for h in hits] == ["a.txt"]


def test_read_file_denies_secret_file(evil_repo):
    with pytest.raises(ToolError):
        read_file(".env", repo_root=evil_repo)


def test_search_never_returns_secret_file_hits(evil_repo):
    assert search_codebase("MUST-NOT-LEAK", repo_root=evil_repo) == []
    assert search_codebase("DB_PASSWORD", repo_root=evil_repo) == []


@pytest.mark.parametrize("command", ["cat .env", "head -n 2 .env", "wc -l .env", "ls .env", "tail .env"])
def test_shell_denies_secret_file_arguments(evil_repo, command):
    with pytest.raises(ToolError):
        run_shell(command, repo_root=evil_repo)


@pytest.mark.parametrize("command", ["grep -r MUST-NOT-LEAK .", "grep -rn DB_PASSWORD .", "grep -rh MUST-NOT-LEAK ."])
def test_grep_never_reads_secret_files(evil_repo, command):
    out = run_shell(command, repo_root=evil_repo)
    assert CANARY not in out
    assert "sk_live" not in out


def test_symlink_outside_repo_is_not_readable(evil_repo):
    with pytest.raises(ToolError):
        read_file("leak.txt", repo_root=evil_repo)
    assert search_codebase("outside-secret", repo_root=evil_repo) == []


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")
def test_git_cannot_leak_secret_files(evil_repo):
    _git(evil_repo, "init", "-q")
    _git(evil_repo, "add", "-A")
    _git(evil_repo, "commit", "-qm", "x")

    with pytest.raises(ToolError):
        run_shell("git show HEAD:.env", repo_root=evil_repo)
    with pytest.raises(ToolError):
        run_shell("git grep -h MUST-NOT-LEAK", repo_root=evil_repo)
    with pytest.raises(ToolError):
        run_shell("git grep --no-filename MUST-NOT-LEAK", repo_root=evil_repo)

    for command in ("git grep MUST-NOT-LEAK", "git log -p", "git show HEAD"):
        out = run_shell(command, repo_root=evil_repo)
        assert CANARY not in out, command
        assert "sk_live" not in out, command
    assert "hello" in run_shell("git show HEAD", repo_root=evil_repo)


@pytest.mark.parametrize(
    "text,must_hide",
    [
        (f"DB_PASSWORD={CANARY}", CANARY),
        (f"STRIPE=sk_live_{'x' * 16}", "sk_live_"),
        (f"DATABASE_URL=postgres://u:{CANARY}@h/db", CANARY),
        (f"password = '{CANARY}'", CANARY),
        (f"password = {CANARY}", CANARY),
        (f"GITHUB_TOKEN=ghp_{'a' * 24}", "ghp_"),
        ("-----BEGIN RSA PRIVATE KEY-----", "BEGIN RSA"),
        (f"password: str = '{CANARY}'", CANARY),
        (f"api_key: SecretStr = '{CANARY}'", CANARY),
        (f'def connect(password: str = "{CANARY}"):', CANARY),
        (f"DB_PASSWORD: str = '{CANARY}'", CANARY),
        (f"token: str = '{CANARY}',", CANARY),
    ],
)
def test_model_profile_masks_literal_secrets(text, must_hide):
    assert must_hide not in redact_model(text)


@pytest.mark.parametrize(
    "line",
    [
        "MAX_TOKENS = 4096",
        "token_count = 0",
        "self.token = token",
        "password = get_password()",
        "api_key = os.environ.get('K')",
        "token: str",
        "token_url = 'https://x.example/token'",
        "token = os.environ['X']",
        "api_key = cfg.SECRETS['db']",
        "password = settings.db.password",
        "api_key: str | None = None,",
        'budget_tokens: int | None = None,',
        'api_key: Optional[str] = None"',
        "token: dict[str, str] = {}",
        "token: list[str] = []",
        "token_ttl: int = 3600",
        "expires: int = 3600",
    ],
)
def test_model_profile_keeps_ordinary_code(line):
    """model 档误伤普通源码 = 让模型读残缺代码下结论（评审实测面）。"""
    assert redact_model(line) == line


@pytest.mark.parametrize("line", ["MAX_TOKENS = 4096", "token_count = 0", "password = get_password()"])
def test_audit_profile_still_redacts_aggressively(line):
    """审计侧口径不变：宁可多抹。"""
    assert "***" in redact_audit(line)


_RULE_POSITIVE_CONTROLS = [
    ("_ASSIGN", "password = 'hunter2'"),
    ("_ANNOTATED_ASSIGN", "password: str = 'hunter2'"),
    ("_COLON", "password: hunter2"),
]
_SHAPE_POSITIVE_CONTROLS = [
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----",
    f"ghp_{'a' * 24}",
    f"github_pat_{'a' * 30}",
    "AKIAIOSFODNN7EXAMPLE",
    f"xoxb-{'a' * 12}",
    f"AIza{'a' * 30}",
    f"sk-{'a' * 12}",
    f"sk_live_{'a' * 16}",
    f"Bearer {'a' * 16}",
    "postgres://u:hunter2@h/db",
]


def test_every_redaction_rule_has_a_positive_control():
    """规则级正控：每条正则都必须在已知样本上真的命中。

    只测"redact_model 抹掉了 canary"是不够的 —— 同一个 canary 可能被**别的**
    规则抹掉，被改坏的那条规则照样绿。所以逐个规则点名验证。
    """
    from codewiki.ai.guard import secrets as mod

    assert all(mod._SHAPE_PATTERNS), "形状规则表空了"
    for pattern, _ in mod._SHAPE_PATTERNS:
        assert any(pattern.search(s) for s in _SHAPE_POSITIVE_CONTROLS), (
            f"形状规则从未命中任何样本：{pattern.pattern}"
        )
    for name, sample in _RULE_POSITIVE_CONTROLS:
        pattern = getattr(mod, name)
        assert pattern.search(sample), f"{name} 从未命中 {sample!r}（正则已静默失效）"
        assert redact_model(sample) != sample, f"{name} 命中了但没抹掉 {sample!r}"


def test_canary_never_reaches_model_through_any_tool(evil_repo):
    outputs: list[str] = []
    for command in ("cat .env", "grep -r MUST-NOT-LEAK .", "ls -R .", "tree ."):
        try:
            outputs.append(run_shell(command, repo_root=evil_repo))
        except ToolError:
            outputs.append("")
    for path in (".env", "leak.txt", "src/cfg.py"):
        try:
            outputs.append(read_file(path, repo_root=evil_repo))
        except ToolError:
            outputs.append("")
    for keyword in ("MUST-NOT-LEAK", "DB_PASSWORD", "outside-secret"):
        outputs.extend(hit["text"] for hit in search_codebase(keyword, repo_root=evil_repo))

    blob = "\n".join(outputs)
    assert CANARY not in blob
    assert OUTSIDE_SECRET not in blob
    assert "MAX_TOKENS = 4096" in blob


@pytest.mark.parametrize(
    "name,expected",
    [
        (".env", True),
        (".env.production", True),
        ("id_rsa", True),
        ("id_ed25519.pub", True),
        ("server.pem", True),
        ("client.key", True),
        ("credentials.json", True),
        (".env.example", False),
        ("main.py", False),
        ("environment.py", False),
    ],
)
def test_secret_file_detection(name, expected):
    assert is_secret_file(Path(name)) is expected
