"""``ai-diagram``：把"AI 时序图 + L2 渲染校验"这条路真正接上。

此前 ``ai/diagram.py`` 是个孤儿模块：``generate_sequence_diagram`` 只有测试在调，
``use_l2`` 参数没有任何调用方传过 True，而 ``[ai] l2_validate`` 更是一个读取点都
没有 —— 配置里写着"L2 真实渲染校验（需 mermaid-cli）"，实际永远是关的。

这一组用例锁：图能落盘、坏图零落盘、L2 开关真的传到了校验器、以及和其它 AI
命令一样受 ``[ai] enabled`` 管。
"""

from __future__ import annotations

from codewiki.cli import main
from tests.conftest import FakeClient

GOOD = "sequenceDiagram\n  Alice->>Bob: hi\n"
BAD = "sequenceDiagram\n  Alice-!!->Bob: broken [\n"


def _analyzed(tmp_path, mini_py):
    out = tmp_path / "wiki-output"
    assert main(["analyze", "--src", str(mini_py), "--out", str(out), "--no-cache"]) == 0
    return out, out / "data" / "codewiki.json"


def _patch_client(monkeypatch, replies):
    monkeypatch.setattr(
        "codewiki.cli.diagram_cmd.OpenAICompatClient", lambda **_kwargs: FakeClient(list(replies))
    )


def _run(packet, out, *extra):
    return main(
        [
            "ai-diagram",
            "--input",
            str(packet),
            "--out",
            str(out),
            "--model",
            "fake-model",
            "--top-n",
            "1",
            *extra,
        ]
    )


def test_diagram_is_written(tmp_path, mini_py, monkeypatch):
    out, packet = _analyzed(tmp_path, mini_py)
    _patch_client(monkeypatch, [GOOD])

    code = _run(packet, out)

    assert code == 0
    files = sorted((out / "diagrams" / "sequences").glob("*.mmd"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == GOOD.strip()


def test_broken_diagram_is_not_written(tmp_path, mini_py, monkeypatch, capsys):
    """坏图零落盘：宁可没有图，也不交付一张渲染不了的图（§4.3）。"""
    out, packet = _analyzed(tmp_path, mini_py)
    _patch_client(monkeypatch, [BAD, BAD])

    code = _run(packet, out)

    assert code == 0
    assert not list((out / "diagrams" / "sequences").glob("*.mmd"))
    assert "AI_DIAGRAM_INVALID" in capsys.readouterr().out


def test_l2_switch_from_config_is_passed(tmp_path, mini_py, monkeypatch):
    """``[ai] l2_validate = true`` 必须真的传到校验器 —— 这是它存在的唯一意义。"""
    out, packet = _analyzed(tmp_path, mini_py)
    _patch_client(monkeypatch, [GOOD])
    seen: list[bool] = []

    import codewiki.ai.diagram as diagram_module

    original = diagram_module.validate

    def spy(text, *, use_l2=False):
        seen.append(use_l2)
        return original(text, use_l2=False)

    monkeypatch.setattr(diagram_module, "validate", spy)
    cfg = tmp_path / "codewiki.toml"
    cfg.write_text("[ai]\nl2_validate = true\n", encoding="utf-8")

    assert _run(packet, out, "--config", str(cfg)) == 0

    assert seen == [True], "配置开了 L2，校验器却没收到"


def test_l2_cli_flag_overrides(tmp_path, mini_py, monkeypatch):
    out, packet = _analyzed(tmp_path, mini_py)
    _patch_client(monkeypatch, [GOOD])
    seen: list[bool] = []

    import codewiki.ai.diagram as diagram_module

    original = diagram_module.validate

    def spy(text, *, use_l2=False):
        seen.append(use_l2)
        return original(text, use_l2=False)

    monkeypatch.setattr(diagram_module, "validate", spy)

    assert _run(packet, out, "--l2-validate") == 0

    assert seen == [True]


def test_l2_is_off_by_default(tmp_path, mini_py, monkeypatch):
    """默认必须关：L2 要 Node + mermaid-cli，多数环境没有；默认开会让人一上手
    就看见"降级"的诊断，还以为是自己配错了。"""
    out, packet = _analyzed(tmp_path, mini_py)
    _patch_client(monkeypatch, [GOOD])
    seen: list[bool] = []

    import codewiki.ai.diagram as diagram_module

    original = diagram_module.validate

    def spy(text, *, use_l2=False):
        seen.append(use_l2)
        return original(text, use_l2=False)

    monkeypatch.setattr(diagram_module, "validate", spy)

    assert _run(packet, out) == 0

    assert seen == [False]


def test_disabled_ai_is_denied(tmp_path, mini_py, monkeypatch, capsys):
    """与其它 AI 命令同一条规矩：项目方声明不要 AI 就直接拒绝。"""
    out, packet = _analyzed(tmp_path, mini_py)
    _patch_client(monkeypatch, [GOOD])
    cfg = tmp_path / "codewiki.toml"
    cfg.write_text("[ai]\nenabled = false\n", encoding="utf-8")

    code = _run(packet, out, "--config", str(cfg))

    assert code == 2
    assert "enabled = false" in capsys.readouterr().err


def test_force_ai_bypasses_the_denial(tmp_path, mini_py, monkeypatch):
    out, packet = _analyzed(tmp_path, mini_py)
    _patch_client(monkeypatch, [GOOD])
    cfg = tmp_path / "codewiki.toml"
    cfg.write_text("[ai]\nenabled = false\n", encoding="utf-8")

    assert _run(packet, out, "--config", str(cfg), "--force-ai") == 0


def test_no_calls_edge_is_reported_not_silent(tmp_path, monkeypatch, capsys):
    """没有 CALLS 边时要说清楚"是仓库没有调用链"，而不是让人以为命令坏了。"""
    import json

    from codewiki.models import Node, NodeType
    from codewiki.render.jsonio import packet_from_parts

    packet_path = tmp_path / "codewiki.json"
    packet = packet_from_parts(
        title="t",
        layers=[],
        nodes=[Node(id="mod::a.py", type=NodeType.MODULE, path="a.py")],
        edges=[],
        annotations=[],
        diagnostics=[],
    )
    packet_path.write_text(json.dumps(packet.model_dump(mode="json")), encoding="utf-8")
    _patch_client(monkeypatch, [GOOD])

    code = main(["ai-diagram", "--input", str(packet_path), "--out", str(tmp_path), "--model", "m"])

    assert code == 0
    assert "没有可用的调用链" in capsys.readouterr().err
