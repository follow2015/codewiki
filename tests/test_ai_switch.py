"""``[ai] enabled`` 三态：项目方的"不要 AI"声明必须真的拦得住。

此前这个字段**一个读取点都没有** —— ``codewiki.toml.example`` 写着"整段缺失等同
enabled = false"，而实际行为是"有 ``--model`` 就跑"，仓库作者写 ``enabled = false``
完全无效。一个"看起来能关、实际关不掉"的开关比没有开关更糟：审计第三方代码时，
人会以为项目方已经拒绝了 AI 分析。

三态语义（缺省 / false / true）以及它与 :mod:`codewiki.trust` 的分工：

* 缺省 ``None``：不干预，由命令行与环境变量决定；
* ``false``：**拒绝**，三个 AI 命令一律 exit 2（``merge-annotations`` 不受管 ——
  它不调模型，只是把建议文件合进配置，是人工环节）；
* ``true``：**允许但不强制**。端点与模型仍只认操作者：让配置文件里的 true 能开启
  AI，等于把 ``OPENAI_API_KEY`` 的去向交给被分析仓库。

覆盖途径只有操作者按得到的那两个：``--force-ai`` 与 ``CODEWIKI_FORCE_AI=1``。
"""

from __future__ import annotations

import pytest

from codewiki.cli import build_parser, main
from codewiki.config import load_config
from codewiki.trust import ENV_FORCE_AI, ai_denied


def _cfg_text(enabled: str | None) -> str:
    body = "" if enabled is None else f"enabled = {enabled}\n"
    return f"[ai]\n{body}"


@pytest.mark.parametrize(
    ("enabled", "denied"),
    [
        ("false", True),
        ("true", False),
        (None, False),
    ],
)
def test_three_states(tmp_path, enabled, denied):
    path = tmp_path / "codewiki.toml"
    path.write_text(_cfg_text(enabled), encoding="utf-8")

    assert (ai_denied(load_config(path)) is not None) is denied


def test_default_is_not_false(tmp_path):
    """缺省必须是 None 而不是 False：默认 False 会让所有老配置突然变成"拒绝"，
    而它们的作者只是没写这一行。"""
    path = tmp_path / "codewiki.toml"
    path.write_text('[project]\nname = "x"\n', encoding="utf-8")

    assert load_config(path).ai.enabled is None


def test_force_overrides_false(tmp_path):
    path = tmp_path / "codewiki.toml"
    path.write_text(_cfg_text("false"), encoding="utf-8")

    assert ai_denied(load_config(path), force=True) is None


def test_denial_message_tells_how_to_override(tmp_path):
    """只说"被拒绝"而不说怎么绕过，用户只能去删那一行 —— 而那一行是项目方的声明。"""
    path = tmp_path / "codewiki.toml"
    path.write_text(_cfg_text("false"), encoding="utf-8")

    message = ai_denied(load_config(path))

    assert message is not None
    assert "enabled = false" in message
    assert "--force-ai" in message


def test_env_force_ai_is_recognised(monkeypatch):
    from codewiki.trust import is_force_ai

    assert is_force_ai({}) is False
    assert is_force_ai({ENV_FORCE_AI: "1"}) is True
    assert is_force_ai({ENV_FORCE_AI: "YES"}) is True
    assert is_force_ai({ENV_FORCE_AI: "0"}) is False


def _packet(tmp_path):
    """命令在 enabled 判定前不解析数据包，所以一个存在的占位文件就够。"""
    path = tmp_path / "codewiki.json"
    path.write_text("{}", encoding="utf-8")
    return path


def test_ai_annotate_is_denied(tmp_path, capsys):
    cfg = tmp_path / "codewiki.toml"
    cfg.write_text(_cfg_text("false"), encoding="utf-8")

    code = main(["ai-annotate", "--input", str(_packet(tmp_path)), "--config", str(cfg), "--model", "m"])
    err = capsys.readouterr().err

    assert code == 2
    assert "enabled = false" in err


def test_ai_annotate_force_ai_passes_the_gate(tmp_path, capsys):
    """``--force-ai`` 之后不再因 enabled 被拒（此处仍会因数据包为空而失败，那是另一回事）。"""
    cfg = tmp_path / "codewiki.toml"
    cfg.write_text(_cfg_text("false"), encoding="utf-8")

    code = main(
        [
            "ai-annotate",
            "--input",
            str(_packet(tmp_path)),
            "--config",
            str(cfg),
            "--model",
            "m",
            "--force-ai",
        ]
    )
    err = capsys.readouterr().err

    assert "enabled = false" not in err
    assert code != 0


def test_ai_annotate_env_force_ai(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv(ENV_FORCE_AI, "1")
    cfg = tmp_path / "codewiki.toml"
    cfg.write_text(_cfg_text("false"), encoding="utf-8")

    main(["ai-annotate", "--input", str(_packet(tmp_path)), "--config", str(cfg), "--model", "m"])

    assert "enabled = false" not in capsys.readouterr().err


def test_agent_is_denied(tmp_path, mini_py, capsys):
    out = tmp_path / "wiki-output"
    assert main(["analyze", "--src", str(mini_py), "--out", str(out), "--no-cache"]) == 0
    cfg = tmp_path / "codewiki.toml"
    cfg.write_text(_cfg_text("false"), encoding="utf-8")

    code = main(
        [
            "agent",
            "--input",
            str(out / "data" / "codewiki.json"),
            "--config",
            str(cfg),
            "--model",
            "m",
            "问题",
        ]
    )

    assert code == 2
    assert "enabled = false" in capsys.readouterr().err


def test_merge_annotations_is_not_gated(tmp_path, capsys):
    """合并是**人工环节**，不调模型。用 enabled = false 拦住它，等于让"不要 AI"
    的项目方连人工合并都做不了 —— 而那条路径上根本没有模型参与。"""
    cfg = tmp_path / "codewiki.toml"
    cfg.write_text(_cfg_text("false"), encoding="utf-8")
    suggest = tmp_path / "annotations.suggest.toml"
    suggest.write_text('[[annotations]]\ntarget = "mod::a.py"\nnote = "x"\n', encoding="utf-8")

    code = main(["merge-annotations", str(suggest), "--config", str(cfg), "--dry-run"])
    err = capsys.readouterr().err

    assert code == 0, err


def test_force_ai_wired_on_ai_commands():
    parser = build_parser()
    for argv in (["ai-annotate"], ["agent"]):
        args = parser.parse_args(argv)
        assert args.force_ai is False, f"{argv[0]} 缺 --force-ai"
        assert parser.parse_args([*argv, "--force-ai"]).force_ai is True
