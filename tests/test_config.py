"""配置层判据（§3）。

钉四件事：

1. **零硬编码**：没有 `codewiki.toml` 也能跑（默认值），但要有明确诊断；
2. **最长前缀优先**：`app/services/…` 必须落到"服务层"，不能被 `app/` 抢走；
3. **错误可修**：语法错误带行号，字段错误带键名 + 可执行建议；
4. **注解与层**能从配置读进数据包（AI 建议合入后也走同一条路径）。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from codewiki.config import (
    CONFIG_FILENAME,
    ArchitectureSection,
    CodewikiConfig,
    ConfigError,
    LayerSpec,
    config_schema_json,
    load_config,
)

EXAMPLE = Path(__file__).parent.parent / "codewiki.toml.example"


def test_example_config_is_valid_and_matches_layers_by_longest_prefix():
    cfg = load_config(EXAMPLE)

    assert cfg.title == "我的项目"
    assert cfg.layer_of("app/api/user.py") == "layer::接口层"
    assert cfg.layer_of("app/services/order.py") == "layer::服务层"
    assert cfg.layer_of("app/models/device.py") == "layer::数据层"
    assert cfg.layer_of("app/other/x.py") == "layer::_unlayered"
    # 最长前缀：services 下的深层路径仍属服务层
    assert cfg.layer_of("app/services/deep/nested.py") == "layer::服务层"


def test_missing_config_falls_back_to_defaults(tmp_path):
    cfg = load_config(tmp_path / CONFIG_FILENAME)

    assert isinstance(cfg, CodewikiConfig)
    assert cfg.analysis.min_confidence == 0.5
    assert cfg.layer_of("anything.py") == "layer::_unlayered"
    assert cfg.ai.enabled is False


def test_syntax_error_reports_line_number(tmp_path):
    bad = tmp_path / CONFIG_FILENAME
    bad.write_text('[project\nname = "x"\n', encoding="utf-8")

    with pytest.raises(ConfigError) as info:
        load_config(bad)

    message = str(info.value)
    assert "TOML 语法错误" in message
    assert "codewiki.toml:1" in message, "必须给出行列号，否则用户无从下手"


def _emulate_old_tomllib(monkeypatch) -> None:
    """把 ``tomllib.loads`` 抛出的异常抹成 3.11–3.13 的真实形态。

    老版本 ``TOMLDecodeError`` 没有 ``lineno / pos / msg``，位置只写在消息尾巴
    里。3.14 上置空等价于"属性不存在"，于是本机也能复现老版本的代码路径。
    """
    real_loads = tomllib.loads

    def loads_without_lineno(text: str) -> dict:
        try:
            real_loads(text)
        except tomllib.TOMLDecodeError as exc:
            exc.lineno = None
            raise
        raise AssertionError("样本必须是语法错误，否则这条用例什么都没测")

    monkeypatch.setattr(tomllib, "loads", loads_without_lineno)


def test_line_number_survives_python_without_lineno_attr(tmp_path, monkeypatch):
    """3.11–3.13 的 ``TOMLDecodeError`` 没有 ``lineno``，位置只写在消息里。

    CI 的 ``test (3.11)`` 就是在这里红的：只读属性恒为 ``None``，报错里连行号
    都没有。这条用例把"必须留消息兜底"钉死——删掉兜底它就会红。
    """
    bad = tmp_path / CONFIG_FILENAME
    bad.write_text('[project\nname = "x"\n', encoding="utf-8")

    _emulate_old_tomllib(monkeypatch)

    with pytest.raises(ConfigError) as info:
        load_config(bad)

    assert f"{CONFIG_FILENAME}:1" in str(info.value), "属性缺失时必须从消息里抠行号"


@pytest.mark.parametrize(
    ("bad_text", "expected_line"),
    [
        ("a = 1\nb", 2),  # 缺等号，解析器跑到第 2 行行尾才发现
        ("a = 'unterminated\n", 2),  # 引号没闭合，同理
    ],
)
def test_end_of_document_line_matches_across_python_versions(tmp_path, monkeypatch, bad_text, expected_line):
    """``at end of document`` 类错误：3.14 报最后一行，老版本不能报空。

    两个版本给出的**整条消息必须一字不差**，否则同一个坏配置在 3.11 用户和
    3.14 用户那里收到的提示不一样，测试矩阵也就失去意义。
    """
    bad = tmp_path / CONFIG_FILENAME
    bad.write_text(bad_text, encoding="utf-8")

    with pytest.raises(ConfigError) as new_python:
        load_config(bad)
    assert f"{CONFIG_FILENAME}:{expected_line}" in str(new_python.value)

    _emulate_old_tomllib(monkeypatch)
    with pytest.raises(ConfigError) as old_python:
        load_config(bad)

    assert str(old_python.value) == str(new_python.value), "跨版本口径必须一致"


def test_type_error_reports_key_and_hint(tmp_path):
    bad = tmp_path / CONFIG_FILENAME
    bad.write_text('[analysis]\nmin_confidence = "high"\n', encoding="utf-8")

    with pytest.raises(ConfigError) as info:
        load_config(bad)

    message = str(info.value)
    assert "analysis.min_confidence" in message
    assert "数字" in message, "建议要可执行：这里应提示不要加引号"


def test_out_of_range_value_is_rejected(tmp_path):
    bad = tmp_path / CONFIG_FILENAME
    bad.write_text("[analysis]\nmin_confidence = 2.5\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(bad)


def test_layers_and_annotations_export_for_the_packet():
    cfg = load_config(EXAMPLE)

    layers = cfg.layers_as_nodes()
    assert [layer["name"] for layer in layers] == ["接口层", "服务层", "数据层"]
    assert layers[0]["id"] == "layer::接口层"
    assert cfg.annotations[0].target == "mod::app/services/order.py"


def test_config_schema_is_exportable():
    schema = json_loads(config_schema_json())

    assert "properties" in schema
    assert "architecture" in schema["properties"]


def json_loads(text: str) -> dict:
    import json

    return json.loads(text)


def test_root_path_pattern_matches_everything_but_loses_to_finer_layers():
    """根路径 `paths = ["."]` 是常见写法：必须命中，且要输给更细的层。

    症状很隐蔽：不修的话这一层的 subgraph **是空的**，只有真正出图才看得出来
    （看 SVG 时发现的）。
    """
    cfg = CodewikiConfig(
        architecture=ArchitectureSection(
            layers=[
                LayerSpec(name="适配层", paths=["adapters"]),
                LayerSpec(name="服务层", paths=["."]),
            ]
        )
    )

    assert cfg.layer_of("adapters/login.py") == "layer::适配层"
    assert cfg.layer_of("client_error_service.py") == "layer::服务层"
    assert cfg.layer_of("retention.py") == "layer::服务层"


def test_root_path_variants_are_equivalent():
    for pattern in (".", "./", ""):
        cfg = CodewikiConfig(
            architecture=ArchitectureSection(layers=[LayerSpec(name="全部", paths=[pattern])])
        )
        assert cfg.layer_of("a/b.py") == "layer::全部", pattern


# ── M4 修复回归（2026-10-23 评审）：未知配置键必须报错，不得静默忽略 ────────


def test_unknown_top_level_key_is_rejected(tmp_path):
    """拼错段名（如 [architect]）必须显式失败，静默忽略等于配置白写。"""
    from codewiki.config import ConfigError, load_config

    p = tmp_path / "codewiki.toml"
    p.write_text("[architect]\nlayers = []\n", encoding="utf-8")
    try:
        load_config(p)
    except ConfigError as exc:
        assert "architect" in str(exc)
    else:
        raise AssertionError("未知顶层段被静默接受")


def test_unknown_section_key_is_rejected(tmp_path):
    from codewiki.config import ConfigError, load_config

    p = tmp_path / "codewiki.toml"
    p.write_text("[analysis]\nmin_confidnce = 0.6\n", encoding="utf-8")  # 拼错
    try:
        load_config(p)
    except ConfigError as exc:
        assert "min_confidnce" in str(exc)
    else:
        raise AssertionError("段内未知键被静默接受")


def test_longest_prefix_wins_even_when_the_fine_layer_comes_last():
    """细层**写在后面**也必须赢 —— 这是"最长前缀"的全部意义。

    `longest_prefix_match` 的注释明写"别改成首个命中即返回"，但此前没有任何
    用例在守：把 `len(prefix) > best[0]` 换成 `best is None`，全量 448 条仍全绿。
    现有用例都把细层写在前面，两种语义因此区分不出来。
    """
    cfg = CodewikiConfig(
        architecture=ArchitectureSection(
            layers=[
                LayerSpec(name="全部", paths=["."]),  # 兜底层写在前面
                LayerSpec(name="适配层", paths=["adapters"]),
            ]
        )
    )

    assert cfg.layer_of("adapters/login.py") == "layer::适配层"
    assert cfg.layer_of("retention.py") == "layer::全部"


def test_longer_prefix_beats_shorter_one_between_two_real_layers():
    """两个非根层之间也按最长前缀取，不是谁先声明谁赢。"""
    cfg = CodewikiConfig(
        architecture=ArchitectureSection(
            layers=[
                LayerSpec(name="外层", paths=["app"]),
                LayerSpec(name="内层", paths=["app/log_center"]),
            ]
        )
    )

    assert cfg.layer_of("app/log_center/retention.py") == "layer::内层"
    assert cfg.layer_of("app/other.py") == "layer::外层"


def test_ts_aliases_are_parsed_from_config(tmp_path):
    """``[analysis] ts_aliases`` 要能写进配置 —— 前端别名只能由配置声明（不猜 tsconfig）。"""
    path = tmp_path / CONFIG_FILENAME
    path.write_text(
        '[analysis]\nts_aliases = { "@/*" = "frontend-new/src/*", "@config" = "frontend-new/src/config/app.ts" }\n',
        encoding="utf-8",
    )

    cfg = load_config(path)

    assert cfg.analysis.ts_aliases == {
        "@/*": "frontend-new/src/*",
        "@config": "frontend-new/src/config/app.ts",
    }


def test_ts_aliases_default_to_empty_not_none():
    """缺省是空表（而不是 None）：调用方不必为"没配别名"写分支。"""
    assert CodewikiConfig().analysis.ts_aliases == {}
