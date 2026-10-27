"""外部评审第二轮（2026-10-23）的回归用例。

覆盖 7 项实测面，每条都按"攻击者视角"写：不是断言函数返回了某个值，而是
断言**攻击面真的被堵死**（外部服务器收不到 key、仓库外内容读不到、回溯被掐断）。

1. ``doctor --probe`` 曾用 ``cfg.ai.base_url`` 直连 —— 仓库写个 base_url 就能收走 key；
2. ``search_codebase`` 用 ``root / rel`` 读文件 —— 符号链接能读到仓库外；
3. ``search_codebase`` 返回绝对路径 —— 本机目录结构进上下文；
4. ReDoS 静态检查只认单层括号 —— ``((a+))+$`` 能绕过；
5. 脱敏漏 ``.env`` 命名（``\b`` 在下划线前不成立）且误伤 ``token: str``；
6. ``ls -R dirlink`` 能列出仓库外目录名（非选项参数没做 resolve）；
7. 剥离注释后残留双空行 —— 部署仓被 ruff 判 I001。

scan-secrets: fixtures-allowed —— 本文件含故意写出的假密钥样本，用途是验证扫描器与脱敏。
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import codewiki.ai.tools as tools
import codewiki.doctor as doctor
from codewiki.ai.audit import redact
from codewiki.ai.guard import redact_model
from codewiki.ai.sandbox import ShellDenied, check_command
from codewiki.ai.tools import ToolError, read_file, search_codebase


class _Recorder(BaseHTTPRequestHandler):
    """记下收到的 Authorization 头（模拟被劫持的网关）。"""

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        self.server.received.append(self.headers.get("Authorization", ""))  # type: ignore[attr-defined]
        body = json.dumps(
            {"choices": [{"message": {"content": "pong"}}], "model": "probe", "usage": {}}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return


def _serve() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    server.received = []  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_doctor_probe_ignores_config_base_url(tmp_path, monkeypatch):
    """仓库里写 base_url 收不到 key：probe 只认 CLI / 环境变量 / 默认端点。"""
    evil = _serve()
    good = _serve()
    try:
        (tmp_path / "codewiki.toml").write_text(
            f'[ai]\nmodel = "m"\nbase_url = "http://127.0.0.1:{evil.server_port}/v1"\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-TESTKEY-do-not-leak")
        monkeypatch.setenv("OPENAI_MODEL", "m")
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        import codewiki.ai.client as client_mod

        monkeypatch.setattr(client_mod, "DEFAULT_BASE_URL", f"http://127.0.0.1:{good.server_port}/v1")

        check = doctor._check_ai(probe=True)
    finally:
        for server in (evil, good):
            server.shutdown()
            server.server_close()

    assert evil.received == [], "仓库配置里的 base_url 绝不能收到任何请求（否则 key 已泄露）"
    assert good.received, "默认端点应当收到一次探测请求"
    assert "codewiki.toml" in check.message, "配置里的 base_url 被忽略时必须在报告里说清楚"


def test_resolve_base_url_never_returns_config_value():
    from codewiki.ai.client import resolve_base_url

    url, warning = resolve_base_url(
        cli_value=None, config_value="http://evil.example/v1", env={"OPENAI_BASE_URL": "http://ok/v1"}
    )
    assert url == "http://ok/v1" and warning


def test_search_codebase_does_not_follow_symlink_outside_repo(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("TOPSECRET_PAYLOAD\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "clean.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "evil.py").symlink_to(outside / "secret.txt")

    hits = search_codebase("TOPSECRET_PAYLOAD", repo_root=repo)

    assert hits == [], "符号链接指向仓库外的文件必须被跳过"


def test_search_codebase_returns_paths_relative_to_root(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "mod.py").write_text("needle = 1\n", encoding="utf-8")

    hits = search_codebase("needle", repo_root=repo)

    assert hits and hits[0]["path"] == "pkg/mod.py"
    assert not hits[0]["path"].startswith("/"), "不能把本机绝对路径带进模型上下文"


def test_search_codebase_redacts_secret_lines(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "config.yaml").write_text("DB_PASSWORD=hunter2\nneedle: yes\n", encoding="utf-8")

    hits = search_codebase("DB_PASSWORD", repo_root=repo)

    assert hits and "hunter2" not in hits[0]["text"]


def test_search_codebase_still_works_through_worker(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")

    hits = search_codebase(r"def\s+foo", repo_root=repo)

    assert len(hits) == 1 and hits[0]["line"] == 1


def test_search_codebase_aborts_catastrophic_backtracking(tmp_path, monkeypatch):
    """``((a+))+$`` 能绕过静态检查（只认单层括号），由子进程超时兜底。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "evil.txt").write_text("a" * 26 + "!\n", encoding="utf-8")
    monkeypatch.setattr(tools, "REGEX_TIMEOUT_S", 0.5)

    with pytest.raises(ToolError) as excinfo:
        search_codebase("((a+))+$", repo_root=repo)

    assert "中止" in str(excinfo.value)


@pytest.mark.parametrize(
    "line",
    [
        "DB_PASSWORD=hunter2",
        "AWS_SECRET_ACCESS_KEY=abcd1234",
        "SECRET_KEY=xyz",
        "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "AKIAIOSFODNN7EXAMPLE",
        "postgres://appuser:s3cr3t@db.internal:5432/prod",
    ],
)
def test_redact_covers_env_style_secrets(line):
    out = redact(line)
    for leaked in (
        "hunter2",
        "abcd1234",
        "xyz",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "AKIAIOSFODNN7EXAMPLE",
        "s3cr3t",
    ):
        assert leaked not in out, f"{line} 里的密钥没抹掉: {out}"


def test_redact_covers_pem_block():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"
    assert "MIIEow" not in redact(pem)


def test_redact_keeps_source_annotations_intact():
    """类型标注本身不是值，两个档位都不该动它。

    ⚠️ 分档断言，不能混：``api_key: Optional[str] = None`` 带赋值，
    audit 档抹 ``None`` 是口径内行为（"宁可多抹"，同 ``MAX_TOKENS = 4096``）；
    但 model 档**必须**保留 —— 抹了就是给模型一段残缺代码。
    """
    for snippet in ("token: str", "def f(token: str) -> None:"):
        assert redact(snippet) == snippet, f"源码注解被误伤: {snippet}"

    annotated = "api_key: Optional[str] = None"
    assert redact_model(annotated) == annotated, "model 档把默认值 None 抹了"


def test_ls_cannot_list_symlinked_directory_outside_repo(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.txt").write_text("x", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "dirlink").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ShellDenied):
        check_command("ls -R dirlink", repo_root=repo)


def test_ordinary_shell_args_are_not_false_denied(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("needle = 1\n", encoding="utf-8")

    assert check_command("ls -R .", repo_root=repo)
    assert check_command('find . -name "*.py"', repo_root=repo)
    assert check_command("grep needle src", repo_root=repo)


def test_strip_comments_does_not_leave_double_blank_lines():
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent / "scripts" / "strip-comments.py"
    spec = importlib.util.spec_from_file_location("strip_comments", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    source = "import os\n\n#: 第一段\n\n#: 第二段\nMAX = 1\n"
    assert module.strip_comments(source) == "import os\n\nMAX = 1\n"


def test_ci_matrix_covers_requires_python_lower_bound():
    """CI 矩阵必须覆盖 ``requires-python`` 的下界，否则那句声明没人验证。

    这条守卫原先是"矩阵里必须有 3.11/3.12/3.13/3.14 四项"。但矩阵该守的是
    **声明的下界有没有被验证**，不是"版本列了几个"：中间版本对
    "依赖在某解释器上装不上"这类问题没有检出能力 —— 实测 networkx 3.7 要
    ``>=3.12``，3.12/3.13 两条腿全绿，只有 3.11 直接
    ``No matching distribution``。所以矩阵收成两端（下界 + 开发基线）是够的。

    断言改为从 pyproject 解析下界：以后谁把 ``requires-python`` 降到 3.10
    却没在矩阵里加 3.10，这条照样会红。
    """
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    meta = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    spec = meta["project"]["requires-python"]
    lower = spec.split(">=")[-1].strip()
    assert f'"{lower}"' in ci, f"requires-python 声明下界 {lower}，但 CI 矩阵里没有它 —— 那句承诺等于没人验证"

    versions = set(re.findall(r'"(\d+\.\d+)"', ci))
    assert len(versions) >= 2, f"CI 矩阵只剩 {versions or '一个版本'}，等于没覆盖"
    assert "ruff" in ci, "CI 必须跑 ruff"


def test_doctor_probe_reports_ignored_config_even_without_probe(tmp_path, monkeypatch):
    (tmp_path / "codewiki.toml").write_text(
        '[ai]\nmodel = "m"\nbase_url = "http://evil.example/v1"\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    check = doctor._check_ai(probe=False)

    assert check.level == "warning" and "codewiki.toml" in check.message


def test_read_file_refuses_env_but_allows_example(tmp_path):
    (tmp_path / ".env").write_text("A=1\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("A=changeme\n", encoding="utf-8")

    with pytest.raises(ToolError):
        read_file(".env", repo_root=tmp_path)
    assert "changeme" in read_file(".env.example", repo_root=tmp_path)


def test_ci_never_calls_hashfiles_from_a_job_level_if():
    """`hashFiles` 只能出现在**步骤级** `if` 里。

    job 级 `if` 由服务端评估，那一刻还没 checkout、没有工作区，`hashFiles`
    读不到任何东西 —— GitHub 不会"返回空"，而是把**整份工作流**判为非法：
    推上去 0 秒失败、工作流名退回文件路径、所有 job 一个都不跑。

    这类错误本地任何 YAML 解析器都看不出来（语法完全合法，错的是上下文），
    只能靠这条守卫。实测代价：一整轮 CI 全红，且报错信息只说"workflow file
    issue"，看不到是哪一行。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    lines = ci.splitlines()

    job_level = [line for line in lines if re.match(r"^ {4}if:.*hashFiles", line)]
    assert job_level == [], f"job 级 if 不许调 hashFiles：{job_level}"

    assert re.search(r"^    runs-on: ", ci, re.M), "找不到 4 空格缩进的 runs-on，缩进假设已过时"
    assert re.search(r"^  [a-zA-Z_-]+:\s*$", ci, re.M), "找不到 job 定义"
