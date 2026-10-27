"""scan-secrets.py 的判据契约（移植自 ipip 同名脚本，按 codewiki 裁剪）。

scan-secrets: fixtures-allowed —— 本文件含故意写出的假密钥样本，用途是验证扫描器与脱敏。
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest  # noqa: F401

_SCRIPT = Path(__file__).parents[1] / "scripts" / "scan-secrets.py"
_spec = importlib.util.spec_from_file_location("scan_secrets", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
scan_file = _mod.scan_file


def _hits(tmp_path: Path, text: str) -> set[str]:
    target = tmp_path / "probe.txt"
    target.write_text(text, encoding="utf-8")
    return {rule for _, rule, _, _ in scan_file(target, text=text)}


def test_detects_private_key(tmp_path):
    assert "PRIVATE_KEY" in _hits(tmp_path, "-----BEGIN RSA PRIVATE KEY-----\nabc\n")


def test_detects_sk_token(tmp_path):
    assert "SK_PREFIX" in _hits(tmp_path, 'TOKEN = "sk-abcdefghijklmn0123456789"\n')


def test_detects_plaintext_password(tmp_path):
    assert "PASSWORD_ASSIGN" in _hits(tmp_path, 'password = "Sup3rS3cret!"\n')


def test_detects_conn_string_with_password(tmp_path):
    assert "CONN_STRING" in _hits(tmp_path, "mysql://root:pass123@db.internal/x\n")


def test_detects_public_ip(tmp_path):
    assert "IP_ADDR" in _hits(tmp_path, "host = 58.49.146.64\n")


def test_allows_private_and_loopback_ips(tmp_path):
    assert not _hits(tmp_path, "bind = 127.0.0.1\ngw = 192.168.1.1\nmask = 10.0.0.5\n")


def test_allows_variable_interpolation_not_literal(tmp_path):
    """值整体是变量引用（shell / f-string）⇒ 运行时取值，放行。"""
    assert not _hits(tmp_path, 'export DB_PWD="${DB_PASSWORD:-}"\n')
    assert not _hits(tmp_path, "f'password={esc_pwd}'\n")


def test_allows_placeholder_values(tmp_path):
    assert not _hits(tmp_path, 'password = "your-password-here"\n')
    assert not _hits(tmp_path, 'api_key = "xxx"\n')


def test_allows_env_var_name_as_value(tmp_path):
    """值整体是 UPPER_SNAKE 的环境变量名（指向取值来源）⇒ 不是明文。

    实测误报：codewiki/ai/client.py 的 `ENV_API_KEY = "OPENAI_API_KEY"`。
    真实密码几乎不会是纯大写下划线形态；该判据放行的是"常量指向取值来源"。
    """
    assert not _hits(tmp_path, 'ENV_API_KEY = "OPENAI_API_KEY"\n')
    assert not _hits(tmp_path, 'PASSWORD_ENV = "CODEWIKI_DB_PASSWORD"\n')
    assert "PASSWORD_ASSIGN" in _hits(tmp_path, 'password = "OpenAI_Key_123"\n')


def test_allows_docs_and_readme_paths(tmp_path):
    target = tmp_path / "docs"
    target.mkdir()
    probe = target / "note.md"
    probe.write_text("示例：host = 58.49.146.64\n", encoding="utf-8")
    assert not scan_file(probe)


def test_skips_binary_content(tmp_path):
    assert not _hits(tmp_path, "\x00\x01\x02 sk-abcdefghijklmn0123456789\n")


def test_detects_unquoted_plaintext_password(tmp_path):
    """值不带引号也是明文 —— 此前要求引号，`password = hunter2` 这类全漏。

    扫描器漏报比误报危险得多：闸门看着在跑，实际什么都没拦住。
    """
    assert "PASSWORD_ASSIGN" in _hits(tmp_path, "password = hunter2\n")
    assert "PASSWORD_ASSIGN" in _hits(tmp_path, "DB_PASSWORD: p@ssw0rd\n")
    assert "PASSWORD_ASSIGN" in _hits(tmp_path, 'password = "hunter2"\n')


def test_unquoted_indirection_is_still_allowed(tmp_path):
    """放开裸值后，间接取值与占位符不能反过来变成误报。"""
    for text in (
        "password = os.environ['PW']\n",
        "password = os.getenv('PW')\n",
        "password = ${PW}\n",
        "password: <your-password>\n",
        "DB_PASSWORD = OPENAI_API_KEY\n",
        "api_key = api_key\n",
    ):
        assert "PASSWORD_ASSIGN" not in _hits(tmp_path, text), text


def test_fixture_marker_skips_tests_tree(tmp_path):
    d = tmp_path / "tests"
    d.mkdir()
    target = d / "test_probe.py"
    text = '# scan-secrets: fixtures-allowed\nTOKEN = "sk-abcdefghijklmn0123456789"\n'
    target.write_text(text, encoding="utf-8")
    assert scan_file(target, text=text) == []


def test_fixture_marker_is_inert_outside_tests(tmp_path):
    target = tmp_path / "codewiki_probe.py"
    text = '# scan-secrets: fixtures-allowed\nTOKEN = "sk-abcdefghijklmn0123456789"\n'
    target.write_text(text, encoding="utf-8")
    rules = {rule for _, rule, _, _ in scan_file(target, text=text)}
    assert "SK_PREFIX" in rules, "标记在 tests/ 之外竟然生效了 —— 这就是后门"


def test_fixture_marker_must_be_near_file_head(tmp_path):
    """标记只认文件头 40 行：允许写在任意位置等于允许事后补标记。"""
    d = tmp_path / "tests"
    d.mkdir()
    target = d / "test_late.py"
    filler = "# padding\n" * 60
    text = filler + '# scan-secrets: fixtures-allowed\nTOKEN = "sk-abcdefghijklmn0123456789"\n'
    target.write_text(text, encoding="utf-8")
    rules = {rule for _, rule, _, _ in scan_file(target, text=text)}
    assert "SK_PREFIX" in rules


def test_main_reports_marker_misuse_outside_tests(tmp_path):
    """越界使用标记要在 CLI 层报出来，且退出码非 0（不能静默放行）。"""
    target = tmp_path / "leak.py"
    text = '# scan-secrets: fixtures-allowed\nTOKEN = "sk-abcdefghijklmn0123456789"\n'
    target.write_text(text, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), str(target)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 1
    assert "不生效" in result.stderr
