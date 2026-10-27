"""Graphify 导入判据（§4.6.4）。

适配器最容易犯的错是"**静默映射一半**"：字段对不上时按猜的继续跑，产出的图谱
看起来完整，实际缺了半张图 —— 下游会把缺失当成"这个仓库本来就没依赖"。

所以两条硬判据：

1. 结构不符必须**报错失败**（``GraphifyFormatError``），不是跳过；
2. 认不出的 relation 要**列出来**（诊断），不是硬塞成 IMPORTS。
"""

from __future__ import annotations

import json

import pytest

from codewiki.cli import main
from codewiki.graph.importers.graphify import GraphifyFormatError, load_graphify
from codewiki.models import EdgeType, NodeType

GRAPH = {
    "directed": True,
    "multigraph": False,
    "graph": {},
    "built_at_commit": "abc123",
    "nodes": [
        {
            "id": "app_core_enums_billingmode",
            "label": "BillingMode",
            "_callable": True,
            "_callable_class": True,
            "_origin": "ast",
            "source_file": "app/core/enums.py",
            "source_location": "L708",
        },
        {
            "id": "app_services_svc_run",
            "label": "run",
            "_callable": True,
            "_callable_class": False,
            "_origin": "ast",
            "source_file": "app/services/svc.py",
            "source_location": "L12",
        },
    ],
    "links": [
        {
            "source": "app_services_svc_run",
            "target": "app_core_enums_billingmode",
            "relation": "calls",
            "confidence": "EXTRACTED",
            "confidence_score": 1.0,
            "source_file": "app/services/svc.py",
            "source_location": "L40",
            "weight": 1.0,
        },
        {
            "source": "app_services_svc_run",
            "target": "app_core_enums_billingmode",
            "relation": "mystery_relation",
            "confidence": "EXTRACTED",
            "source_location": "L41",
        },
    ],
}


@pytest.fixture
def graph_path(tmp_path):
    path = tmp_path / "graph.json"
    (tmp_path.parent).mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(GRAPH), encoding="utf-8")
    return path


def test_nodes_and_modules_are_mapped(graph_path):
    packet, _diagnostics = load_graphify(graph_path)

    modules = [n for n in packet.nodes if n.type == NodeType.MODULE]
    symbols = [n for n in packet.nodes if n.type == NodeType.SYMBOL]
    assert [m.path for m in modules] == ["app/core/enums.py", "app/services/svc.py"]
    assert [s.name for s in symbols] == ["BillingMode", "run"]
    assert symbols[0].kind == "class" and symbols[1].kind == "function"
    assert symbols[0].module_id == "mod::app/core/enums.py"
    assert symbols[0].line_start == 708


def test_known_relation_is_mapped_and_unknown_is_reported(graph_path):
    packet, diagnostics = load_graphify(graph_path)

    assert [e.type for e in packet.edges] == [EdgeType.CALLS]
    assert [d.code for d in diagnostics] == ["GRAPHIFY_RELATION_SKIPPED"]
    assert "mystery_relation" in diagnostics[0].message


def test_provenance_records_the_source_commit(graph_path):
    packet, _ = load_graphify(graph_path)
    assert packet.meta.provenance["graphify"].version == "abc123"


def test_missing_links_is_a_hard_error(tmp_path):
    """边在 `links` 而不是 `edges`：缺了就报错，不按空图继续。"""
    path = tmp_path / "g.json"
    path.write_text(json.dumps({"nodes": []}), encoding="utf-8")
    with pytest.raises(GraphifyFormatError):
        load_graphify(path)


def test_node_without_id_is_a_hard_error(tmp_path):
    path = tmp_path / "g.json"
    path.write_text(json.dumps({"nodes": [{"label": "x"}], "links": []}), encoding="utf-8")
    with pytest.raises(GraphifyFormatError):
        load_graphify(path)


def test_broken_json_is_a_hard_error(tmp_path):
    path = tmp_path / "g.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(GraphifyFormatError):
        load_graphify(path)


def test_cli_imports_and_writes_packet(graph_path, tmp_path, capsys):
    out = tmp_path / "out"
    assert main(["import-graph", "--from", "graphify", "--input", str(graph_path), "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "导入完成" in text and "GRAPHIFY_IMPORTED" in text
    assert (out / "data" / "codewiki.json").exists()


def test_cli_reports_bad_input_with_exit_code_2(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text("{}", encoding="utf-8")
    assert main(["import-graph", "--input", str(path), "--out", str(tmp_path / "o")]) == 2
    assert "error:" in capsys.readouterr().err


def test_links_not_a_list_is_a_hard_error(tmp_path):
    """links 是对象而非数组也要报错 —— 静默置空会产出"半张图"。"""
    path = tmp_path / "g.json"
    path.write_text(json.dumps({"nodes": [], "links": {"a": 1}}), encoding="utf-8")
    with pytest.raises(GraphifyFormatError):
        load_graphify(path)


def test_symbol_qualname_is_preserved(graph_path):
    """M5：Node 此前没有 qualname 字段，importer 传入的被 pydantic 静默吞掉。"""
    packet, _ = load_graphify(graph_path)
    syms = [n for n in packet.nodes if n.type == NodeType.SYMBOL]
    assert syms and all(n.qualname for n in syms), "qualname 被静默丢弃"
