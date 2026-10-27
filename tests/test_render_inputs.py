"""渲染指纹与"上一轮是否完成"的契约（``pipeline.incremental``）。

这里有两条**曾经出过事**的规则：

* 指纹只含分层与注解 —— 改了 ``min_confidence`` / ``max_nodes`` / 工具版本后，
  未改动模块的页面仍被复用，图上还是旧阈值的结果；
* 快照先于渲染落盘 —— 中途被杀，下一轮会把残缺产物当成"上一轮已完成"来复用。

两条都不是靠读代码能发现的，所以钉成用例。
"""

from __future__ import annotations

import pytest

from codewiki.config import CodewikiConfig
from codewiki.pipeline.incremental import (
    INCOMPLETE_MARKER,
    changed_components,
    clear_incomplete_marker,
    describe_input,
    has_incomplete_marker,
    render_inputs,
    write_incomplete_marker,
)
from codewiki.schemas import DataPacket, LayerNode


def _packet(layers: list[LayerNode] | None = None) -> DataPacket:
    return DataPacket(
        meta=DataPacket.new_meta("t"),
        layers=layers if layers is not None else [LayerNode(id="layer::L1", name="L1", order=0)],
    )


def _cfg(**analysis) -> CodewikiConfig:
    return CodewikiConfig.model_validate({"analysis": analysis} if analysis else {})


@pytest.mark.parametrize(
    ("kwargs", "component"),
    [
        ({"min_confidence": 0.9}, "min_confidence"),
        ({"max_nodes": 999}, "max_nodes"),
    ],
)
def test_analysis_settings_are_part_of_the_fingerprint(kwargs, component):
    """这些参数决定"图上有什么"，改了却复用页面 = 页面与图不同源。"""
    before = render_inputs(cfg=_cfg(), packet=_packet())
    after = render_inputs(cfg=_cfg(**kwargs), packet=_packet())

    assert before[component] != after[component]
    assert changed_components(before, after), "分量变了却没被比较出来"


def test_layers_and_annotations_are_part_of_the_fingerprint():
    before = render_inputs(cfg=_cfg(), packet=_packet())
    after = render_inputs(
        cfg=_cfg(),
        packet=_packet([LayerNode(id="layer::L1", name="改名了", order=0)]),
    )

    assert before["layers"] != after["layers"]
    assert "分层定义" in changed_components(before, after)[0]


def test_effective_cli_override_beats_the_config_value_in_the_fingerprint():
    """``--min-confidence`` 必须进指纹。

    曾经的半生效状态：CLI 覆盖只用在建图，出图与指纹仍读配置值 —— 于是用户传了
    ``--min-confidence 0.95``，图上没变、增量还报"复用 N 个"，产物里是旧阈值的图。
    """
    configured = render_inputs(cfg=_cfg(min_confidence=0.5), packet=_packet())
    overridden = render_inputs(cfg=_cfg(min_confidence=0.5), packet=_packet(), min_confidence=0.95)

    assert configured["min_confidence"] == "0.5"
    assert overridden["min_confidence"] == "0.95"
    assert "置信度下限" in changed_components(configured, overridden)[0]


def test_parsed_backends_are_part_of_the_fingerprint():
    """换后端要全量：模块 hash 是**文件内容**的 hash，换后端时它不变，
    但结构事实全变了（regex 只有 import 骨架）。只看 hash 会判成"没变化"。"""
    ast_only = render_inputs(cfg=_cfg(), packet=_packet(), backends={"ast": object()})
    regex_only = render_inputs(cfg=_cfg(), packet=_packet(), backends={"regex": object()})

    assert ast_only["backends"] != regex_only["backends"]
    assert "解析后端" in changed_components(ast_only, regex_only)[0]


def test_language_whitelist_is_part_of_the_fingerprint():
    base = _cfg()
    changed = CodewikiConfig.model_validate({"languages": {"python": {"ext": [".py"], "analyzer": "regex"}}})

    assert (
        render_inputs(cfg=base, packet=_packet())["languages"]
        != render_inputs(cfg=changed, packet=_packet())["languages"]
    )


def test_tool_version_is_part_of_the_fingerprint(monkeypatch):
    """工具升级往往意味着渲染逻辑变了，旧页面不能当"还新鲜"复用。"""
    import codewiki.pipeline.incremental as incremental

    before = render_inputs(cfg=_cfg(), packet=_packet())
    monkeypatch.setattr(incremental, "__version__", "9.9.9")
    after = render_inputs(cfg=_cfg(), packet=_packet())

    assert after["tool_version"] == "9.9.9"
    assert before["tool_version"] != after["tool_version"]
    assert changed_components(before, after)[0].startswith("工具版本")


def test_changed_components_shows_before_and_after_for_scalars():
    labels = changed_components({"min_confidence": "0.5"}, {"min_confidence": "0.9"})

    assert labels == ["置信度下限 0.5 → 0.9"]


def test_changed_components_says_changed_for_hashed_values():
    labels = changed_components({"layers": "a" * 16}, {"layers": "b" * 16})

    assert labels == ["分层定义已变化"], "哈希看不出值，就不要假装能展示前后值"


def test_changed_components_reports_removed_components():
    assert changed_components({"min_confidence": "0.5"}, {}) == ["置信度下限已移除"]


def test_no_previous_input_means_no_claim():
    """没有上一轮指纹时不能凭空说"变了" —— 该由调用方按"无法判断"处理。"""
    assert changed_components({}, {"min_confidence": "0.5"}) == []
    assert changed_components(None, {"min_confidence": "0.5"}) == []


def test_every_component_has_a_readable_name():
    """诊断里露英文键名等于让用户去读源码。"""
    for component in render_inputs(cfg=_cfg(), packet=_packet()):
        assert describe_input(component) != component, f"{component} 没有中文名"


def test_marker_lifecycle(tmp_path):
    assert not has_incomplete_marker(tmp_path)

    marker = write_incomplete_marker(tmp_path)
    assert marker.name == INCOMPLETE_MARKER and has_incomplete_marker(tmp_path)

    clear_incomplete_marker(tmp_path)
    assert not has_incomplete_marker(tmp_path)


def test_clearing_a_missing_marker_is_not_an_error(tmp_path):
    """重复清理必须无害：成功路径与异常清理可能各清一次。"""
    clear_incomplete_marker(tmp_path)
    clear_incomplete_marker(tmp_path)


def test_marker_is_written_into_a_not_yet_existing_out_dir(tmp_path):
    """首次运行的产物目录还不存在，写标记不能因此失败。"""
    target = tmp_path / "not-yet" / "wiki-output"

    write_incomplete_marker(target)

    assert (target / INCOMPLETE_MARKER).is_file()
