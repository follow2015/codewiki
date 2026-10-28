"""cycles / diff / suggest-layers / merge-layers 端到端（§六 命令表）。

端到端这一层只钉"命令能跑通且结论正确"，不重复单元层已覆盖的判定细节：

* cycles：成环仓库报环、无环仓库不报；
* diff：两个快照之间改动了什么、影响到谁；
* suggest-layers → merge-layers：建议能产出并合回配置，且**人工层还在**。
"""

from __future__ import annotations

import json

import pytest

from codewiki.cli import main
from codewiki.config import load_config

FIXTURES = "tests/fixtures"

NO_CACHE = ["--no-cache"]


@pytest.fixture
def cyclic_py():
    from pathlib import Path

    return Path(FIXTURES) / "cyclic_py"


@pytest.fixture
def prepared(tmp_path, mini_py):
    out = tmp_path / "wiki-output"
    assert main(["analyze", "--src", str(mini_py), "--out", str(out), *NO_CACHE]) == 0
    return out


def test_cycles_reports_the_cycle_and_writes_diagram(cyclic_py, tmp_path, capsys):
    out = tmp_path / "out"
    assert main(["analyze", "--src", str(cyclic_py), "--out", str(out), *NO_CACHE]) == 0

    code = main(["cycles", "--input", str(out / "data" / "codewiki.json"), "--out", str(out)])
    captured = capsys.readouterr()

    assert code == 0
    assert "MODULE_CYCLE" in captured.out
    assert "a.py" in captured.out and "b.py" in captured.out
    assert list((out / "diagrams" / "cycles").glob("*.mmd")), "应产出环图"


def test_cycles_on_acyclic_repo_reports_none(prepared, capsys):
    assert main(["cycles", "--input", str(prepared / "data" / "codewiki.json")]) == 0
    assert "环 0 个" in capsys.readouterr().out


def test_missing_packet_is_user_error(tmp_path, capsys):
    assert main(["cycles", "--input", str(tmp_path / "nope.json")]) == 2


def test_diff_between_two_snapshots(cyclic_py, tmp_path, capsys):
    base = tmp_path / "base"
    head = tmp_path / "head"
    assert main(["analyze", "--src", str(cyclic_py), "--out", str(base), *NO_CACHE]) == 0
    assert main(["analyze", "--src", str(cyclic_py), "--out", str(head), *NO_CACHE]) == 0

    packet_path = head / "data" / "codewiki.json"
    data = json.loads(packet_path.read_text(encoding="utf-8"))
    for node in data["nodes"]:
        if node.get("path") == "c.py":
            node["hash"] = "changed-hash"
    packet_path.write_text(json.dumps(data), encoding="utf-8")

    code = main(["diff", "--base", str(base / "data" / "codewiki.json"), "--head", str(packet_path)])
    captured = capsys.readouterr()

    assert code == 0
    assert "c.py" in captured.out
    assert "影响半径" in captured.out


def test_suggest_layers_then_merge_layers(prepared, tmp_path, capsys):
    config = tmp_path / "codewiki.toml"
    config.write_text('[[architecture.layers]]\nname = "手工层"\npaths = ["hand"]\n', encoding="utf-8")

    assert (
        main(["suggest-layers", "--input", str(prepared / "data" / "codewiki.json"), "--out", str(prepared)])
        == 0
    )
    suggest = prepared / "suggestions" / "suggested-layers.toml"
    assert suggest.exists()

    code = main(["merge-layers", str(suggest), "--config", str(config), "--dry-run"])
    assert code == 0

    assert main(["merge-layers", str(suggest), "--config", str(config)]) == 0
    cfg = load_config(config)
    names = [layer.name for layer in cfg.architecture.layers]
    assert names[0] == "手工层", "人工层必须保留在最前"
    assert len(names) > 1


def test_full_pipeline_smoke(mini_py, tmp_path):
    """analyze → cycles → suggest-layers → export 全链路不炸（端到端冒烟）。"""
    out = tmp_path / "wiki-output"
    assert main(["analyze", "--src", str(mini_py), "--out", str(out), *NO_CACHE]) == 0
    assert main(["cycles", "--input", str(out / "data" / "codewiki.json")]) == 0
    assert main(["suggest-layers", "--input", str(out / "data" / "codewiki.json"), "--out", str(out)]) == 0
    assert main(["export", "--dir", str(out), "--zip", str(tmp_path / "bundle.zip")]) == 0
    assert (tmp_path / "bundle.zip").exists()


def test_large_repo_pages_instead_of_crashing(mini_py, tmp_path):
    """节点超限时首页仍要能出：改为分层架构地图，不依赖任何 mermaid 宏图。"""
    config = tmp_path / "codewiki.toml"
    config.write_text(
        "[analysis]\nmax_nodes = 2\n\n"
        "[architecture]\n\n"
        '[[architecture.layers]]\nname = "接口层"\npaths = ["app"]\norder = 1\n\n'
        '[[architecture.layers]]\nname = "服务层"\npaths = ["pkg"]\norder = 2\n',
        encoding="utf-8",
    )
    out = tmp_path / "wiki-output"

    assert (
        main(["analyze", "--src", str(mini_py), "--out", str(out), "--config", str(config), *NO_CACHE]) == 0
    )

    assert not (out / "diagrams" / "system-context.mmd").exists()
    assert list((out / "diagrams").glob("system-context-part-*.mmd"))
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "<svg" in html and "layer-map" in html, "首页应展示分层架构地图，而不是空白"
    assert "接口层" in html, "层带要显示层名"
    assert 'class="mermaid"' not in html, "首页不再嵌 mermaid 巨图"
