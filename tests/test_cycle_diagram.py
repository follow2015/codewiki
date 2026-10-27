"""依赖环图判据（§4.2「依赖环图」）。

图要能直接回答两个问题：**谁和谁成环**、**先动哪个能断环**。所以判据钉的是
"环上边被标红加粗"与"断环建议指向环内被依赖最多的节点"。
"""

from __future__ import annotations

from codewiki.graph.analysis import Cycle
from codewiki.render.mermaid_cycle import render_all_cycles, render_cycle_diagram


def test_cycle_diagram_highlights_edges_and_nodes():
    cycle = Cycle(
        kind="module",
        members=["mod::a.py", "mod::b.py"],
        edges=[("mod::a.py", "mod::b.py"), ("mod::b.py", "mod::a.py")],
    )
    text = render_cycle_diagram(cycle)

    assert text.startswith("flowchart LR")
    assert text.count(" ==> ") == 2, "环上边必须加粗"
    assert text.count("stroke:#C62828") == 2, "环上节点必须标红"


def test_break_suggestion_targets_most_depended_node():
    """断环建议 = 环内入度最大者（下沉它可让其余依赖变成上层→下层）。"""
    cycle = Cycle(
        kind="module",
        members=["mod::a.py", "mod::b.py", "mod::c.py"],
        edges=[("mod::a.py", "mod::b.py"), ("mod::c.py", "mod::b.py"), ("mod::b.py", "mod::a.py")],
    )
    text = render_cycle_diagram(cycle)

    assert "断环建议" in text and "`b.py`" in text, "b 被依赖 2 次，应作为候选"


def test_diagram_without_edges_has_no_fake_suggestion():
    cycle = Cycle(kind="module", members=["mod::a.py"], edges=[])
    assert "断环建议" not in render_cycle_diagram(cycle), "没有边信息就不该硬猜"


def test_render_all_cycles_writes_one_file_per_cycle(tmp_path):
    cycles = [
        Cycle(kind="module", members=["mod::a.py", "mod::b.py"], edges=[("mod::a.py", "mod::b.py")]),
        Cycle(kind="layer", members=["layer::x", "layer::y"], edges=[("layer::x", "layer::y")]),
    ]
    written = render_all_cycles(cycles, tmp_path)

    assert len(written) == 2 and all((tmp_path / name).exists() for name in written)
    assert any("module" in name for name in written) and any("layer" in name for name in written)
