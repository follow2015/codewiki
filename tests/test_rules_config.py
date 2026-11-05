"""``[[rules]]`` 配置段（阶段一 1.1）的加载与校验。

这一层是"规则"功能的入口：规则写错时的表现必须是**拒绝加载**，而不是静默忽略
一半条件 —— 一条只生效了一半的规则会让人以为架构被守住了。

两个易错点单独钉住：

1. ``from`` 是 Python 关键字。不用 ``alias="from"`` + ``populate_by_name``，
   TOML 里 ``from = "layer:ui"`` 会被判"未知字段"拒掉 —— 而这是本段最主要的写法。
2. ``forbid-cycle`` 上写 ``edges`` 是无效配置（环看的是全类型边）。静默忽略它，
   用户会以为"我限定了只看 IMPORTS 的环"，实际没限定。
"""

from __future__ import annotations

import json

import pytest

from codewiki.config import ConfigError, RuleSpec, config_schema_json, load_config

BASE = """
[architecture]
[[architecture.layers]]
name = "ui"
paths = ["web"]

[[architecture.layers]]
name = "infra"
paths = ["db"]
"""


def _load(tmp_path, rules: str):
    path = tmp_path / "codewiki.toml"
    path.write_text(BASE + rules, encoding="utf-8")
    return load_config(path)


def test_from_keyword_is_accepted(tmp_path):
    """``from = "layer:ui"`` 必须能读进来 —— 关键字别名是这一段的地基。"""
    cfg = _load(
        tmp_path,
        """
[[rules]]
id = "no-ui-to-infra"
kind = "forbid-dependency"
from = "layer:ui"
to = "layer:infra"
reason = "界面层不得直连基础设施"
""",
    )
    (rule,) = cfg.rules
    assert rule.from_ == "layer:ui"
    assert rule.to == "layer:infra"
    assert rule.reason.startswith("界面层")
    assert rule.edges == ["IMPORTS"]
    assert rule.type_only == "warn"
    assert rule.severity == "error"


def test_rule_spec_can_be_constructed_by_field_name():
    """``populate_by_name`` 让 Python 侧也能用 ``from_=`` 构造（不用记 alias）。"""
    rule = RuleSpec(id="x", kind="forbid-dependency", from_="layer:a", to="layer:b")
    assert rule.from_ == "layer:a"


def test_duplicate_ids_are_rejected_and_name_the_offender(tmp_path):
    with pytest.raises(ConfigError) as exc:
        _load(
            tmp_path,
            """
[[rules]]
id = "dup"
kind = "forbid-dependency"
from = "layer:ui"
to = "layer:infra"

[[rules]]
id = "dup"
kind = "forbid-dependency"
from = "layer:infra"
to = "layer:ui"
""",
        )
    assert "dup" in str(exc.value)


def test_forbid_dependency_without_to_is_rejected(tmp_path):
    with pytest.raises(ConfigError) as exc:
        _load(
            tmp_path,
            """
[[rules]]
id = "half"
kind = "forbid-dependency"
from = "layer:ui"
""",
        )
    assert "half" in str(exc.value)


def test_forbid_cycle_without_scope_is_rejected(tmp_path):
    with pytest.raises(ConfigError) as exc:
        _load(
            tmp_path,
            """
[[rules]]
id = "cyc"
kind = "forbid-cycle"
severity = "warning"
""",
        )
    assert "cyc" in str(exc.value)


def test_forbid_cycle_rejects_explicit_edges(tmp_path):
    """环规则看的是全类型边，``edges`` 在这里是无效配置 —— 静默忽略等于骗人。"""
    with pytest.raises(ConfigError) as exc:
        _load(
            tmp_path,
            """
[[rules]]
id = "cyc"
kind = "forbid-cycle"
scope = "layer:ui"
edges = ["IMPORTS"]
""",
        )
    assert "cyc" in str(exc.value)


def test_empty_edges_list_is_rejected(tmp_path):
    with pytest.raises(ConfigError) as exc:
        _load(
            tmp_path,
            """
[[rules]]
id = "blind"
kind = "forbid-dependency"
from = "layer:ui"
to = "layer:infra"
edges = []
""",
        )
    assert "blind" in str(exc.value)


def test_unknown_layer_reference_lists_available_layers(tmp_path):
    """层名写错一律报错（warning 会被忽略到规则形同虚设），并把可用层名列出来。"""
    with pytest.raises(ConfigError) as exc:
        _load(
            tmp_path,
            """
[[rules]]
id = "typo"
kind = "forbid-dependency"
from = "layer:ui"
to = "layer:infraa"
""",
        )
    message = str(exc.value)
    assert "typo" in message
    assert "infra" in message
    assert "infraa" in message


def test_unknown_target_prefix_is_rejected(tmp_path):
    with pytest.raises(ConfigError) as exc:
        _load(
            tmp_path,
            """
[[rules]]
id = "prefix"
kind = "forbid-dependency"
from = "layerz:ui"
to = "layer:infra"
""",
        )
    assert "layerz" in str(exc.value)


def test_dir_and_module_targets_skip_layer_lookup(tmp_path):
    """``dir:`` / ``module:`` 不指向层，不该被层名校验拦下。"""
    cfg = _load(
        tmp_path,
        """
[[rules]]
id = "dir-rule"
kind = "forbid-dependency"
from = "dir:web"
to = "module:db/session.py"
""",
    )
    assert cfg.rules[0].to == "module:db/session.py"


def test_schema_exposes_rules_section():
    """编辑器补全靠这份 schema：``rules`` 段必须出现，否则写配置全靠猜。"""
    schema = json.loads(config_schema_json())
    assert "rules" in schema["properties"]
    rule_schema = schema["$defs"]["RuleSpec"]["properties"]
    assert {"id", "kind", "from", "to", "scope", "edges", "type_only", "severity"} <= set(rule_schema)
