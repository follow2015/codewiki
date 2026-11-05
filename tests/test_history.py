"""WP7：架构趋势 ``history.jsonl`` —— 每轮 analyze 追加一行确定性快照。

计划 §9 给的判据是"同一 commit 跑两次，两行除 ``generated_at`` 外逐字段相同"。
这条判据逼出来的不是"大致稳定"，而是：**历史行里不许有任何运行期噪声**。缓存
命中数、耗时、集合迭代序、路径绝对化 —— 任一样进去，趋势图就会在架构根本没变的
时候报"变了"，而那正是这个功能唯一的用途。

落点选在被分析仓库的 ``.codewiki/``（与 FileModel 缓存同一个父目录），**不进产物
目录**，理由与 ``CACHE_STATS`` 那条同源：``data/codewiki.json`` 描述"仓库长什么样"，
历史描述"这些轮次各长什么样"——后者是运行记录，混进归档物会让
``test_incremental_equals_full`` 那条逐字节验收永远做不成（增量目录跑过 8 轮、
全新目录只跑 1 轮，行数天然不同）。
"""

from __future__ import annotations

import json

from codewiki.graph.history import (
    HISTORY_FILENAME,
    HISTORY_SCHEMA_VERSION,
    append,
    build_entry,
    default_path,
    read,
)
from codewiki.models import CONF_STATIC, Edge, EdgeType, Node, NodeType
from codewiki.render.jsonio import packet_from_parts
from codewiki.schemas import Diagnostic


def _packet(*, layer_cycle: bool = False, rule_violation: bool = False):
    layers = [
        {"id": "layer::ui", "name": "ui", "order": 1},
        {"id": "layer::infra", "name": "infra", "order": 2},
    ]
    nodes = [
        Node(id="mod::ui/a.py", type=NodeType.MODULE, path="ui/a.py", layer_id="layer::ui"),
        Node(id="mod::infra/db.py", type=NodeType.MODULE, path="infra/db.py", layer_id="layer::infra"),
    ]
    edges = [
        Edge(
            source="mod::ui/a.py",
            target="mod::infra/db.py",
            type=EdgeType.IMPORTS,
            evidence="ui/a.py:3",
            confidence=CONF_STATIC,
        ),
        Edge(
            source="layer::ui",
            target="layer::infra",
            type=EdgeType.LAYER_DEPENDS,
            evidence="2 条跨层 import",
            confidence=CONF_STATIC,
            weight=2,
        ),
    ]
    if layer_cycle:
        edges.append(
            Edge(
                source="layer::infra",
                target="layer::ui",
                type=EdgeType.LAYER_DEPENDS,
                evidence="1 条跨层 import",
                confidence=CONF_STATIC,
                weight=1,
            )
        )
    diagnostics = [Diagnostic(level="info", code="RULES_EVALUATED", message="已按 1 条规则检查")]
    if rule_violation:
        diagnostics.append(
            Diagnostic(
                level="error",
                code="RULE_VIOLATION",
                message="ui 不许依赖 infra",
                where="ui/a.py:3",
                rule_id="no-ui-infra",
            )
        )
    return packet_from_parts(
        title="t",
        layers=layers,
        nodes=nodes,
        edges=edges,
        annotations=[],
        diagnostics=diagnostics,
        generated_at="2026-01-01T00:00:00Z",
        repo_commit="deadbeef",
    )


def test_two_runs_of_the_same_packet_give_identical_entries():
    """同一份数据包两次 ⇒ 逐字段相同。历史行的每一个字段都必须是产物的函数。"""
    first = build_entry(_packet(), files=2)
    second = build_entry(_packet(), files=2)

    assert first.to_dict() == second.to_dict()
    assert first.to_line() == second.to_line()


def test_entry_records_the_counts_a_maintainer_would_ask_about():
    """``files`` 必须与模块数**不等**才算数：解析失败/后端缺失的文件不进 nodes，
    用模块数顶替会把这类退化瞒报掉 —— 那正是这个字段存在的理由。"""
    entry = build_entry(_packet(rule_violation=True), files=3)
    data = entry.to_dict()

    counts = data["counts"]
    assert counts["files"] == 3
    assert counts["modules"] == 2, "夹具要能让 files 与模块数分得开"
    assert counts["layers"] == 2
    assert counts["layer_edges"] == 1
    assert counts["module_cycles"] == 0
    assert data["diagnostics"] == {"error": 1, "warning": 0, "info": 1}
    assert data["violations"] == {"error": 1, "warning": 0}
    assert data["schema"] == HISTORY_SCHEMA_VERSION
    assert data["repo_commit"] == "deadbeef"


def test_layer_names_are_sorted_so_a_reordered_config_is_not_a_change():
    """层声明顺序换了但层集合没变，不该在趋势里显示成"层变了"。

    层序（``order``）确实影响层图，但它已经体现在 ``order`` 字段里；这里存的是
    "有哪些层"，用于 diff 时点名消失/新增的层，所以必须与声明顺序无关。
    """
    packet = _packet()
    shuffled = packet.model_copy(update={"layers": list(reversed(packet.layers))})

    assert build_entry(packet, files=2).to_dict()["layer_names"] == ["infra", "ui"]
    assert build_entry(shuffled, files=2).to_dict()["layer_names"] == ["infra", "ui"]


def test_layer_cycle_is_counted_not_just_flagged():
    with_cycle = build_entry(_packet(layer_cycle=True), files=2).to_dict()
    without = build_entry(_packet(), files=2).to_dict()

    assert without["counts"]["layer_cycles"] == 0
    assert with_cycle["counts"]["layer_cycles"] == 1


def test_append_then_read_roundtrip(tmp_path):
    path = tmp_path / "history.jsonl"
    first = build_entry(_packet(), files=2)
    second = build_entry(_packet(rule_violation=True), files=3)

    count = append(path, first)
    append(path, second)

    assert count == 1, "append 返回的是文件里累计的行数"
    entries, skipped = read(path)
    assert skipped == 0
    assert [entry.to_dict() for entry in entries] == [first.to_dict(), second.to_dict()]


def test_history_lives_under_the_repo_and_ignores_itself(tmp_path):
    """写在被分析仓库里 ⇒ 必须自带忽略规则，否则一次 ``git add -A`` 就把流水提交了。

    与 ``FileModelCache._ensure_dir`` 同一个坑、同一句理由。
    """
    path = default_path(tmp_path)
    append(path, build_entry(_packet(), files=2))

    assert path.parent.name == ".codewiki"
    assert path.name == HISTORY_FILENAME
    assert (path.parent / ".gitignore").read_text(encoding="utf-8").strip() == "*"


def test_a_corrupt_line_is_skipped_not_fatal(tmp_path):
    """坏行跳过并计数，**不**像基线那样报错。

    这两处的取舍相反是有意的：基线是裁决的输入（坏了就当作空基线会一次把存量刷成
    几百条新增），历史只是展示（一行写坏不该让整条趋势消失）。skip 必须返回出去，
    静默丢行会让"趋势断了一截"看起来像"架构一直没变"。
    """
    path = tmp_path / "history.jsonl"
    good = build_entry(_packet(), files=2).to_line()
    path.write_text(f"{good}\n{{ this is not json\n{good}\n", encoding="utf-8")

    entries, skipped = read(path)

    assert len(entries) == 2
    assert skipped == 1


def test_read_of_a_missing_file_is_an_empty_history(tmp_path):
    entries, skipped = read(tmp_path / "nope.jsonl")

    assert entries == []
    assert skipped == 0


def test_read_limit_returns_the_newest_rows_in_order(tmp_path):
    path = tmp_path / "history.jsonl"
    for index in range(5):
        append(path, build_entry(_packet(), files=index))

    entries, _ = read(path, limit=2)

    assert [entry.to_dict()["counts"]["files"] for entry in entries] == [3, 4]


def test_line_has_no_newline_so_the_file_stays_one_record_per_line():
    """一行一条是 ``.jsonl`` 的全部约定：值里带换行会把下一条顶到新行。"""
    line = build_entry(_packet(), files=2).to_line()

    assert "\n" not in line
    assert json.loads(line)["schema"] == HISTORY_SCHEMA_VERSION
