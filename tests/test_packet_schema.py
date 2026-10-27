"""数据包 schema 冻结判据（§4.4 / §十一 第一阶段第 1 项）。

这条用例是"接口先行"的落点：schema 一旦被改（加字段除外），golden 快照先红 ——
不依赖评审时靠人眼发现，也不需要"记得同步渲染层"。

- ``tests/golden/packet_schema.json`` 是**冻结版本**；
- 修改 schema 必须同批改 golden，并在提交说明里写清影响面（渲染层 / AI 层 /
  导入适配器都是它的消费者）。
"""
from __future__ import annotations

import json

from codewiki.models import SCHEMA_VERSION
from codewiki.schemas import DataPacket
from tests.conftest import GOLDEN

GOLDEN_PATH = GOLDEN / "packet_schema.json"


def test_packet_schema_matches_the_frozen_golden():
    current = DataPacket.model_json_schema()
    if not GOLDEN_PATH.exists():
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(
            json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    frozen = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

    def props(schema: dict) -> dict:
        defs = schema.get("$defs", {})
        out = dict(schema.get("properties", {}))
        for name, body in defs.items():
            for key, value in (body.get("properties") or {}).items():
                out[f"{name}.{key}"] = value
        return out

    current_props, frozen_props = props(current), props(frozen)

    missing = sorted(set(frozen_props) - set(current_props))
    assert not missing, f"schema 少了字段（破坏下游消费者）: {missing}"

    added = sorted(set(current_props) - set(frozen_props))
    for name in added:
        assert not frozen_props.get(name, {}).get("required"), f"新增字段 {name} 不应影响既有必填性"


def test_packet_top_level_sections_are_stable():
    """六段结构（§4.4）：meta / layers / nodes / edges / annotations / diagnostics。"""
    sections = set(DataPacket.model_json_schema().get("properties", {}))
    assert {"meta", "layers", "nodes", "edges", "annotations", "diagnostics"} <= sections


def test_schema_version_is_pinned():
    assert SCHEMA_VERSION == 1
    meta_props = DataPacket.model_json_schema()["$defs"]["Meta"]["properties"]
    assert "schema_version" in meta_props
    assert "provenance" in meta_props


def test_models_reject_unknown_fields():
    """M4/M5 同根：pydantic 默认 extra=ignore 会静默吞掉拼错的字段名。
    模型层一律 extra=forbid —— 拼错字段名必须当场炸，而不是悄悄丢数据。"""
    import pytest
    from pydantic import ValidationError

    from codewiki.models import Node, NodeType

    with pytest.raises(ValidationError):
        Node(id="x", type=NodeType.SYMBOL, name="X", quaalname="typo")
