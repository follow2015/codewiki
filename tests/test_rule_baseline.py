"""基线（存量豁免）：让规则可以先落地、再逐步收缩。

基线的全部价值建立在**身份稳定**上：``(rule_id, src, dst, edge_type)``，不含行号
也不含层名。行号会随无关改动漂移 —— 一旦进了身份，改一行注释就能让整本基线失效，
那和大家干脆不用基线是同一种结局。所以核心用例是"把代码整体上移 10 行 ⇒ 仍退出 0"，
变异点也正是"把行号塞进身份"。
"""

from __future__ import annotations

import json

import pytest

from codewiki.graph.rule_baseline import (
    BASELINE_SCHEMA_VERSION,
    BaselineError,
    load_baseline,
    split_by_baseline,
    write_baseline,
)
from codewiki.graph.rules import Violation


def _violation(*, rule="r", src="ui/a.py", dst="infra/db.py", edge="IMPORTS", where="ui/a.py:3"):
    return Violation(
        rule_id=rule,
        severity="error",
        src=src,
        dst=dst,
        edge_type=edge,
        where=where,
        message="m",
    )


def test_identity_ignores_the_line_number():
    """两个只有行号不同的违例是**同一条**：这是基线能用的前提。"""
    assert _violation(where="ui/a.py:3").identity == _violation(where="ui/a.py:13").identity


def test_write_baseline_sorts_for_diffable_files(tmp_path):
    path = tmp_path / "baseline.json"
    write_baseline(
        path,
        [_violation(src="z.py"), _violation(src="a.py"), _violation(rule="a-rule")],
        created_at="2026-01-01T00:00:00Z",
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == BASELINE_SCHEMA_VERSION
    assert payload["created_at"] == "2026-01-01T00:00:00Z"
    assert [(entry["rule_id"], entry["src"]) for entry in payload["violations"]] == [
        ("a-rule", "ui/a.py"),
        ("r", "a.py"),
        ("r", "z.py"),
    ]


def test_write_baseline_reports_new_and_disappeared(tmp_path):
    """写基线前要说清这一笔到底记了什么：记 N 条 / 新增 M / 已消失 K。"""
    path = tmp_path / "baseline.json"
    write_baseline(path, [_violation(src="a.py")], created_at="2026-01-01T00:00:00Z")

    result = write_baseline(
        path, [_violation(src="a.py"), _violation(src="b.py")], created_at="2026-01-02T00:00:00Z"
    )
    assert (result.total, result.added, result.removed) == (2, 1, 0)

    result = write_baseline(path, [_violation(src="b.py")], created_at="2026-01-03T00:00:00Z")
    assert (result.total, result.added, result.removed) == (1, 0, 1)


def test_split_marks_baselined_violations_and_stale_entries(tmp_path):
    path = tmp_path / "baseline.json"
    write_baseline(
        path,
        [_violation(src="a.py"), _violation(src="gone.py")],
        created_at="2026-01-01T00:00:00Z",
    )
    baseline = load_baseline(path)

    split = split_by_baseline([_violation(src="a.py"), _violation(src="b.py", where="ui/b.py:7")], baseline)
    assert [v.src for v in split.new] == ["b.py"]
    assert split.skipped == 1
    assert [entry.src for entry in split.stale] == ["gone.py"]


def test_missing_baseline_file_is_an_empty_baseline(tmp_path):
    """第一次用：文件不存在等于空基线，不该报错。"""
    baseline = load_baseline(tmp_path / "nope.json")
    assert baseline.entries == []
    assert split_by_baseline([_violation()], baseline).new


def test_malformed_baseline_is_an_error_not_an_empty_one(tmp_path):
    """坏基线**不能**当成空基线：那会把全部存量当成新增，一次刷出几百条。"""
    path = tmp_path / "baseline.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(BaselineError):
        load_baseline(path)


def test_baseline_from_a_newer_schema_is_rejected(tmp_path):
    path = tmp_path / "baseline.json"
    path.write_text(
        json.dumps({"schema_version": BASELINE_SCHEMA_VERSION + 1, "created_at": "x", "violations": []}),
        encoding="utf-8",
    )
    with pytest.raises(BaselineError):
        load_baseline(path)


def test_cycle_violations_use_their_own_edge_type():
    """环违例的身份 edge 是 CYCLE，不会与同模块对的 IMPORTS 违例撞车。"""
    cycle = Violation(
        rule_id="cyc", severity="error", src="a.py", dst="b.py", edge_type="CYCLE", where="a.py", message=""
    )
    dependency = _violation(rule="cyc", src="a.py", dst="b.py")
    assert cycle.identity != dependency.identity
