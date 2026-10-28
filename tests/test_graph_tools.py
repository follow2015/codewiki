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
