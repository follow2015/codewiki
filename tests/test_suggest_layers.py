"""建议分层与分层合并判据（§4.6.3）。

核心是"**建议**"二字的分量：

* 建议文件必须能被 ``tomllib`` 读回来（否则用户只能手抄）；
* 结果必须**确定**（同一份图谱两次跑出同样建议，用户才能 diff 审阅）；
* ``merge-layers`` **绝不自动覆盖人工分层**：同名层跳过、paths 重叠直接不写
  并要求人工决定。
"""

from __future__ import annotations

import tomllib

import pytest

from codewiki.config import LayerSpec, load_config
from codewiki.graph.merge_layers import (
    LayerMergeOutcome,
    apply_merge_layers,
    load_suggested_layers,
    merge_layers,
    plan_merge_layers,
    render_layer_merge_report,
)
from codewiki.graph.suggest_layers import SuggestedLayer, render_suggested_layers, suggest_layers
from codewiki.models import CONF_STATIC, Edge, EdgeType, Node, NodeType
from codewiki.render.jsonio import packet_from_parts


def _module(path: str) -> Node:
    return Node(id=f"mod::{path}", type=NodeType.MODULE, path=path, layer_id="layer::a")


def _packet(paths: list[str], edges: list[tuple[str, str]]):
    nodes = [_module(p) for p in paths]
    return packet_from_parts(
        title="demo",
        layers=[],
        nodes=nodes,
        edges=[
            Edge(source=f"mod::{a}", target=f"mod::{b}", type=EdgeType.IMPORTS, confidence=CONF_STATIC)
            for a, b in edges
        ],
        annotations=[],
        diagnostics=[],
    )


CLUSTERED = _packet(
    ["api/a.py", "api/b.py", "svc/c.py", "svc/d.py"],
    [("api/a.py", "api/b.py"), ("svc/c.py", "svc/d.py"), ("api/a.py", "svc/c.py")],
)


def test_suggestions_are_parseable():
    text = render_suggested_layers(suggest_layers(CLUSTERED), title="demo")
    data = tomllib.loads(text)
    assert data["architecture"]["layers"], "建议文件必须能被 tomllib 读回"


def test_layer_names_come_from_directories():
    names = {layer.name for layer in suggest_layers(CLUSTERED)}
    assert names & {"api", "svc"}, f"层名应取公共目录（实际 {names}）"


def test_suggestion_is_deterministic():
    """确定性：不同 seed 无关，同一份图谱必须产出同一建议。"""
    first = [(layer.name, tuple(layer.paths)) for layer in suggest_layers(CLUSTERED)]
    second = [(layer.name, tuple(layer.paths)) for layer in suggest_layers(CLUSTERED)]
    assert first == second


def test_reason_is_attached():
    text = render_suggested_layers(suggest_layers(CLUSTERED))
    assert "内聚度" in text and "# source: suggested" in text


def test_empty_graph_yields_no_suggestion():
    assert suggest_layers(_packet([], [])) == []
    assert "未产出建议" in render_suggested_layers([])


def test_same_name_layer_is_skipped():
    outcome = plan_merge_layers(
        [LayerSpec(name="api", paths=["api"])],
        [LayerSpec(name="api", paths=["api"], color="#fff")],
    )
    assert outcome.skipped == ["api"] and outcome.added == []


def test_overlap_is_reported_and_blocks_writing(tmp_path):
    config = tmp_path / "codewiki.toml"
    config.write_text('[[architecture.layers]]\nname = "旧"\npaths = ["."]\n', encoding="utf-8")
    suggest = tmp_path / "suggested-layers.toml"
    suggest.write_text(
        render_suggested_layers(
            [
                SuggestedLayer(
                    name="api", paths=["api"], modules=["mod::api/a.py"], internal_edges=1, external_edges=0
                )
            ]
        ),
        encoding="utf-8",
    )

    outcome = merge_layers(config, suggest)

    assert outcome.overlaps, "与既有层 paths 重叠必须报出来"
    assert not outcome.changed, "有重叠时不得写入（要求人工先选择）"
    assert "重叠" in render_layer_merge_report(outcome)
    assert load_config(config).architecture.layers[0].name == "旧"


def test_clean_merge_appends_after_existing(tmp_path):
    config = tmp_path / "codewiki.toml"
    config.write_text('[[architecture.layers]]\nname = "手工层"\npaths = ["hand"]\n', encoding="utf-8")
    suggest = tmp_path / "suggested-layers.toml"
    suggest.write_text('[[architecture.layers]]\nname = "api"\npaths = ["api"]\n', encoding="utf-8")

    outcome = merge_layers(config, suggest)
    cfg = load_config(config)

    assert [layer.name for layer in cfg.architecture.layers] == ["手工层", "api"], (
        "人工层必须还在，建议层追加在后"
    )
    assert outcome.changed


def test_dry_run_writes_nothing(tmp_path):
    config = tmp_path / "codewiki.toml"
    original = '[project]\nname = "x"\n'
    config.write_text(original, encoding="utf-8")
    suggest = tmp_path / "s.toml"
    suggest.write_text('[[architecture.layers]]\nname = "api"\npaths = ["api"]\n', encoding="utf-8")

    merge_layers(config, suggest, dry_run=True)
    assert config.read_text(encoding="utf-8") == original


def test_load_suggested_layers_reads_only_architecture(tmp_path):
    path = tmp_path / "s.toml"
    path.write_text('[[annotations]]\ntarget = "mod::a.py"\nnote = "x"\n', encoding="utf-8")
    assert load_suggested_layers(path) == []


def test_apply_marks_changed(tmp_path):
    config = tmp_path / "codewiki.toml"
    outcome = LayerMergeOutcome(added=[LayerSpec(name="api", paths=["api"])])
    assert apply_merge_layers(config, outcome) is True and outcome.changed
    assert "api" in config.read_text(encoding="utf-8")


def test_apply_with_nothing_added_is_noop(tmp_path):
    config = tmp_path / "codewiki.toml"
    assert apply_merge_layers(config, LayerMergeOutcome()) is False


@pytest.mark.parametrize(
    ("left", "right", "overlap"),
    [(["api"], ["api"], True), (["api/sub"], ["api"], True), (["api"], ["svc"], False)],
)
def test_path_overlap_rules(left, right, overlap):
    outcome = plan_merge_layers([LayerSpec(name="new", paths=left)], [LayerSpec(name="old", paths=right)])
    assert bool(outcome.overlaps) is overlap
