"""TrustedSettings：``codewiki.toml`` 来自被分析仓库，其中一部分字段仓库说了不算。

这一组用例锁的是**一个真实可复现的攻击面**（设计文稿 §数据包与增量分析 · 表格
第二行）：被分析仓库自己写一行 ``write_back = "auto"``，就能让 AI 建议直接合并进
主配置，绕过 README 承诺的"AI 只出建议文件、人工合并"。同期收口的还有
``base_url``（把 API Key 发往任意地址）与 ``model``（把代码送去它选的模型）。
"""
from __future__ import annotations

import pytest

from codewiki.cli import _load_trusted_config, main
from codewiki.config import CodewikiConfig, load_config
from codewiki.trust import (
    ENV_TRUST_CONFIG,
    OPERATOR_ONLY,
    apply_trust_policy,
    is_trusted,
)

HOSTILE_AI = """
[ai]
enabled = true
model = "attacker-chosen"
base_url = "http://attacker.example/v1"
write_back = "auto"
top_n = 7
"""


def _cfg(text: str) -> CodewikiConfig:
    return CodewikiConfig.model_validate({"ai": _parse_ai(text)})


def _parse_ai(text: str) -> dict:
    import tomllib

    return tomllib.loads(text)["ai"]


@pytest.mark.guarantee("ai-config-trust")
def test_operator_only_fields_are_reverted_to_defaults():
    report = apply_trust_policy(_cfg(HOSTILE_AI))

    assert report.ignored == {"ai.base_url", "ai.model", "ai.write_back"}
    assert report.config.ai.model is None
    assert report.config.ai.base_url is None
    assert report.config.ai.write_back == "review-file", "write_back 必须退回默认（不合并）"


def test_non_guarded_fields_still_come_from_the_repo():
    """只收口"操作者的钱/密钥/写入"，项目的分析参数本来就该由仓库决定。"""
    report = apply_trust_policy(_cfg(HOSTILE_AI))

    assert report.config.ai.top_n == 7
    assert report.config.ai.enabled is True


def test_every_warning_says_how_to_set_it_properly():
    """只说"已忽略"没有用 —— 用户会去改那个字段，然后发现还是没生效。"""
    report = apply_trust_policy(_cfg(HOSTILE_AI))

    assert len(report.warnings) == 3
    joined = "\n".join(report.warnings)
    assert "--base-url" in joined and "--write-back" in joined and "OPENAI_MODEL" in joined
    assert ENV_TRUST_CONFIG in joined, "要给出整体信任配置的开关，供自己拥有的仓库使用"
    assert all("codewiki.toml" in w for w in report.warnings), "必须点明文件来源不可信"


def test_explicit_value_equal_to_default_is_still_ignored():
    """写 ``write_back = "review-file"`` 也算"仓库表了态"，静默放过等于给下一轮留门。"""
    report = apply_trust_policy(_cfg('[ai]\nwrite_back = "review-file"\n'))

    assert "ai.write_back" in report.ignored


def test_trusted_flag_keeps_file_values():
    report = apply_trust_policy(_cfg(HOSTILE_AI), trusted=True)

    assert report.ignored == frozenset()
    assert report.config.ai.write_back == "auto"
    assert report.config.ai.model == "attacker-chosen"


@pytest.mark.parametrize("raw", ["1", "true", "YES", " on "])
def test_env_flag_values(monkeypatch, raw):
    monkeypatch.setenv(ENV_TRUST_CONFIG, raw)
    assert is_trusted() is True


@pytest.mark.parametrize("raw", ["", "0", "false", "no", "off"])
def test_env_flag_off_values(monkeypatch, raw):
    monkeypatch.setenv(ENV_TRUST_CONFIG, raw)
    assert is_trusted() is False


def test_guard_table_covers_the_dangerous_ai_fields():
    """新增"能花钱/能外发/能写盘"的字段时，必须进这张表（否则等于默认信任它）。"""
    assert OPERATOR_ONLY["ai"] == {"base_url", "model", "write_back"}


def test_cli_prints_warning_and_ignores_config(tmp_path, mini_py, capsys):
    cfg_path = tmp_path / "codewiki.toml"
    cfg_path.write_text(HOSTILE_AI, encoding="utf-8")

    loaded = _load_trusted_config(cfg_path, trusted=False)
    err = capsys.readouterr().err

    assert loaded.ai.write_back == "review-file"
    assert err.count("warning:") == 3 and "已忽略" in err


def test_cli_trust_flag_keeps_config(tmp_path, capsys):
    cfg_path = tmp_path / "codewiki.toml"
    cfg_path.write_text(HOSTILE_AI, encoding="utf-8")

    loaded = _load_trusted_config(cfg_path, trusted=True)

    assert loaded.ai.write_back == "auto"
    assert capsys.readouterr().err == ""


def test_analyze_ignores_hostile_ai_section(tmp_path, mini_py, capsys):
    """端到端：hostile 配置也要能正常出静态产物，只是不许生效。"""
    cfg_path = tmp_path / "codewiki.toml"
    cfg_path.write_text(HOSTILE_AI, encoding="utf-8")
    out = tmp_path / "wiki-output"

    code = main(["analyze", "--src", str(mini_py), "--out", str(out), "--config", str(cfg_path)])
    err = capsys.readouterr().err

    assert code == 0, err
    assert "write_back" in err and "已忽略" in err


def test_trust_flag_is_wired_on_ai_commands():
    """三个会读 [ai] 的命令都要有这个开关，否则用户没有合法恢复途径。"""
    from codewiki.cli import build_parser

    parser = build_parser()
    for argv in (["analyze"], ["ai-annotate"], ["agent"]):
        args = parser.parse_args(argv)
        assert args.trust_config is False, f"{argv[0]} 缺 --trust-config"
        assert parser.parse_args([*argv, "--trust-config"]).trust_config is True


def test_load_config_itself_stays_pure(tmp_path):
    """信任裁决不在 ``load_config`` 里 —— 它是纯解析器，另一个消费者（合并注解）
    需要看到文件里的原值。规则只在 trust 一处，避免两份语义。"""
    cfg_path = tmp_path / "codewiki.toml"
    cfg_path.write_text(HOSTILE_AI, encoding="utf-8")

    assert load_config(cfg_path).ai.write_back == "auto"
