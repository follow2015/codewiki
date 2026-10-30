"""SCIP 映射判据（§4.6.2 准入口径：**以固定样本映射全绿为准入**）。

样本已入仓（`tests/samples/scip/minimal.scip`），
覆盖三条映射规则：定义 / import / 读访问。

钉住的三件事都是"看起来能跑但结果是错的"类型：

1. ``range`` 是 **packed repeated int32**（没有字段键）—— 当成嵌套消息编码会让
   所有 occurrence 的行号都变成 9；
2. ``descriptor`` 的标点编码层级（``:``/``#``/``.``/``()``）—— 不规范化会产出
   两条同名符号（符号表一条、定义位置一条）；
3. kind 判定顺序 —— ``Service#run().`` 同时以 ``()`` 和 ``.`` 结尾，先判后者
   会把它当成字段。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codewiki.analyzers.scip import mock, pb
from codewiki.analyzers.scip.mapper import kind_of, map_index, module_like, symbol_name

SAMPLE = Path("tests/samples/scip/minimal.scip")


@pytest.fixture
def sample_bytes() -> bytes:
    if not SAMPLE.is_file():
        pytest.skip("缺少 SCIP 样本 tests/samples/scip/minimal.scip")
    return SAMPLE.read_bytes()


def test_roundtrip_varint_and_len():
    raw = pb.encode_message([(1, pb.WIRE_VARINT, 300), (2, pb.WIRE_LEN, "你好")])
    decoded = pb.decode_message(raw)
    assert decoded[1][0].value == 300
    assert pb.text(pb.first(decoded, 2)) == "你好"


def test_repeated_fields_keep_order():
    raw = pb.encode_message([(3, pb.WIRE_LEN, "a"), (3, pb.WIRE_LEN, "b"), (3, pb.WIRE_LEN, "c")])
    assert [pb.text(f) for f in pb.decode_message(raw)[3]] == ["a", "b", "c"]


def test_packed_ints_roundtrip():
    raw = pb.encode_packed_ints([0, 9, 300])
    field = pb.Field(1, pb.WIRE_LEN, raw)
    assert pb.packed_ints(field) == [0, 9, 300]


def test_truncated_varint_raises():
    with pytest.raises(ValueError):
        pb.decode_message(b"\x08\x80\x80")


def test_sample_maps_to_one_file(sample_bytes):
    models = map_index(sample_bytes)
    assert len(models) == 1
    assert models[0].path == "pkg/core.py" and models[0].language == "python"
    assert models[0].provenance is not None and models[0].provenance.backend == "scip"


def test_definitions_are_merged_not_duplicated(sample_bytes):
    """符号信息表 + 定义 occurrence 必须合成一条（不得出现两条同名符号）。"""
    symbols = map_index(sample_bytes)[0].symbols
    names = [s.name for s in symbols]
    assert names == ["Service", "Service.run"], names
    assert [s.kind for s in symbols] == ["class", "method"]
    assert [s.lineno for s in symbols] == [6, 9], "行号来自 occurrence（不是全 9）"


def test_docstring_takes_first_line_only(sample_bytes):
    cls = map_index(sample_bytes)[0].symbols[0]
    assert cls.docstring == "核心服务。"


def test_import_and_reference_are_mapped(sample_bytes):
    model = map_index(sample_bytes)[0]
    assert [(i.module, i.lineno) for i in model.imports] == [("pkg.util", 3)]
    calls = [(c.callee, c.lineno) for c in model.calls]
    assert calls == [("helper", 12)], calls


def test_reference_has_no_invented_receiver(sample_bytes):
    """enclosing_range 只给位置 ⇒ 放进 scope，不猜调用者（receiver 留空）。"""
    call = map_index(sample_bytes)[0].calls[0]
    assert call.receiver is None and call.scope == "line9"


def test_symbol_name_normalizes_descriptor():
    assert symbol_name("scip-python python demo 1.0 pkg/core.py:Service#run().") == "Service.run"
    assert symbol_name("scip-python python demo 1.0 pkg/core.py:Service:") == "Service"


def test_kind_detection_order():
    assert kind_of("scip-python python demo 1.0 pkg/core.py:Service#run().") == "method"
    assert kind_of("scip-python python demo 1.0 pkg/core.py:Service:") == "class"
    assert kind_of("scip-python python demo 1.0 pkg/core.py:Service#field.") == "field"
    assert kind_of("scip-python python demo 1.0 pkg/util.py:helper().") == "function"


def test_module_like_strips_extension():
    assert module_like("scip-python python demo 1.0 pkg/util.py:") == "pkg.util"


def test_mock_returns_fresh_copies():
    first, second = mock.sample_models()[0], mock.sample_models()[0]
    first.symbols.clear()
    assert second.symbols, "样本必须每次深拷贝（改一个不能影响另一个）"


def test_mock_parse_follows_the_protocol(tmp_path):
    model = mock.parse(tmp_path / "whatever.py", "content")
    assert model.path == "pkg/core.py" and model.provenance.backend == "scip-mock"
