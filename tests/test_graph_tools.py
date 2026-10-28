"""图谱发现工具：``list_modules`` / ``find_symbol`` / ``query_graph`` 的相近候选。"""

from __future__ import annotations

import pytest

from codewiki.ai.graph_tools import find_symbol, list_modules, suggest_node_ids
from codewiki.ai.registry import default_registry
from codewiki.ai.tools import ToolError
from codewiki.graph import build_graph
from codewiki.render.jsonio import packet_from_parts
from tests.conftest import parse_all


@pytest.fixture
def packet(mini_py):
    result = build_graph(parse_all(mini_py))
    return packet_from_parts(
        title="mini", layers=[], nodes=result.nodes, edges=result.edges, annotations=[], diagnostics=[]
    )


def test_list_modules_orders_by_degree_and_is_deterministic(packet):
    first = list_modules(packet)
    assert first == list_modules(packet)
    assert first.splitlines()[0].startswith("mod::")
    degrees = [int(line.rsplit("度数 ", 1)[1]) for line in first.splitlines() if "度数 " in line]
    assert degrees == sorted(degrees, reverse=True)


def test_list_modules_limit_is_bounded_and_reports_total(packet):
    out = list_modules(packet, limit=1)
    assert len(out.splitlines()) >= 1
    assert "仅列前 1" in out or len(out.splitlines()) == 1
    assert list_modules(packet, limit=10**9)


def test_list_modules_unknown_layer_says_so(packet):
    assert "没有匹配的模块" in list_modules(packet, layer="no-such-layer")


def test_find_symbol_exact_before_partial(packet):
    symbols = [n for n in packet.nodes if n.type.value == "Symbol" and n.name]
    target = symbols[0]
    out = find_symbol(packet, target.name)
    assert target.id in out
    path = target.module_id.removeprefix("mod::")
    assert f"{path}:{target.line_start}" in out


def test_find_symbol_empty_and_missing(packet):
    assert "不能为空" in find_symbol(packet, "   ")
    assert "没有匹配" in find_symbol(packet, "zzz_no_such_symbol_zzz")


def test_suggest_node_ids_handles_wrong_prefix_and_bare_filename(packet):
    module = next(n for n in packet.nodes if n.type.value == "Module" and n.path)
    assert module.id in suggest_node_ids(packet, module.path)
    bare = module.path.rsplit("/", 1)[-1]
    assert module.id in suggest_node_ids(packet, bare)


def test_query_graph_missing_node_offers_candidates(packet, mini_py):
    module = next(n for n in packet.nodes if n.type.value == "Module" and n.path)
    registry = default_registry(repo_root=mini_py, packet=packet)
    with pytest.raises(ToolError) as exc:
        registry.call("query_graph", {"node_id": module.path})
    assert module.id in str(exc.value)


def test_registry_exposes_new_tools_and_validates_args(packet, mini_py):
    registry = default_registry(repo_root=mini_py, packet=packet)
    assert {"list_modules", "find_symbol"} <= registry.names
    with pytest.raises(ToolError):
        registry.call("list_modules", {"repo_root": "/"})
    with pytest.raises(ToolError):
        registry.call("find_symbol", {})
    assert registry.call("list_modules", {"limit": "3"})


def test_query_graph_reports_edge_types(packet, mini_py):
    import json

    registry = default_registry(repo_root=mini_py, packet=packet)
    raw, _wrapped = registry.call_raw(
        "query_graph", {"node_id": "mod::pkg/core.py", "direction": "upstream", "depth": 1}
    )
    payload = json.loads(raw)

    assert payload["edge_count"] >= 1
    assert payload["edge_types"].get("IMPORTS"), f"缺少边类型统计: {payload}"
    assert payload["edges"], "逐边明细不该是空的"
    assert all(row["type"] for row in payload["edges"]), "逐边明细也要带 type"
    assert {"from", "to", "type"} <= set(payload["edges"][0])


def test_query_graph_edge_rows_are_capped_but_counted(packet, mini_py):
    """明细有上限，被截掉多少要写出来 —— 静默截断会让模型以为"就这么多"。"""
    import json

    from codewiki.ai.registry import MAX_EDGE_ROWS

    registry = default_registry(repo_root=mini_py, packet=packet)
    raw, _wrapped = registry.call_raw("query_graph", {"node_id": "mod::pkg/core.py", "depth": 3})
    payload = json.loads(raw)

    assert len(payload["edges"]) <= MAX_EDGE_ROWS
    assert payload["edges_truncated"] == max(0, payload["edge_count"] - len(payload["edges"]))


def test_query_graph_keeps_legacy_fields(packet, mini_py):
    """``node`` / ``related`` 是老字段：加类型不能把它们挤掉（提示词里在用）。"""
    import json

    registry = default_registry(repo_root=mini_py, packet=packet)
    payload = json.loads(registry.call_raw("query_graph", {"node_id": "mod::pkg/core.py"})[0])
    assert payload["node"]["id"] == "mod::pkg/core.py"
    assert isinstance(payload["related"], list) and payload["related"]
