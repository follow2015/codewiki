"""WP7 第二半：analyze 落历史 / 首页趋势 / ``diff --architecture``。

三条互相钉住的约束：

1. **历史不进产物目录** —— ``test_analyze_output_is_unaffected_by_history`` 用
   "同一 commit 两次 analyze 的产物目录逐字节相同"来钉。历史一旦进 ``out/``，
   这条必红（两次跑的历史行数不同）。这是本文件里最要紧的一条：它把"历史属于
   运行记录、不属于归档物"这个设计判断变成了可执行的门禁。
2. **只有 1 行历史时首页不画趋势** —— 一个点画不出趋势，画出来只会让人以为
   "架构一直没变"。
3. **没有可比较的上一轮要明说** —— ``diff --architecture`` 在只有 1 行历史时
   退出 0 但必须说清楚原因；留白会被读成"没有变化"。
"""

from __future__ import annotations

from pathlib import Path

from codewiki.cli import main
from codewiki.graph.history import build_entry, default_path, read
from codewiki.models import CONF_STATIC, Edge, EdgeType, Node, NodeType
from codewiki.render.i18n import t
from codewiki.render.jsonio import packet_from_parts
from codewiki.render.site import render_index_html
from codewiki.schemas import Diagnostic

FIXED_EPOCH = "1767225600"


def _build_repo(root: Path, *, extra_modules: int = 0) -> None:
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "pkg" / "__init__.py").write_text("#\n", encoding="utf-8")
    (root / "pkg" / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
    for index in range(extra_modules):
        (root / "pkg" / f"m{index}.py").write_text(
            "from pkg.core import VALUE\n\n\ndef use() -> int:\n    return VALUE\n", encoding="utf-8"
        )


def _analyze(repo: Path, out: Path, *extra: str) -> int:
    return main(["analyze", "--src", str(repo), "--out", str(out), *extra])


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_analyze_appends_one_line_per_run(tmp_path):
    repo = tmp_path / "repo"
    _build_repo(repo)
    out = tmp_path / "out"

    assert _analyze(repo, out) == 0
    assert _analyze(repo, out) == 0

    entries, skipped = read(default_path(repo))
    assert skipped == 0
    assert len(entries) == 2, "每轮一行：流水记的是「跑过几轮」，去重会把这件事删掉"


def test_history_rows_differ_only_in_the_timestamp(tmp_path):
    """计划 §9 的判据：同一 commit 跑两次，两行除 ``generated_at`` 外逐字段相同。"""
    repo = tmp_path / "repo"
    _build_repo(repo)
    out = tmp_path / "out"
    _analyze(repo, out)
    _analyze(repo, out)

    first, second = read(default_path(repo))[0]
    left, right = first.to_dict(), second.to_dict()
    left.pop("generated_at")
    right.pop("generated_at")

    assert left == right


def test_no_history_switch_writes_nothing(tmp_path):
    """给"绝不能碰被分析仓库"的场景留出口（只读挂载、别人的仓库）。

    只断言历史文件不存在，**不**断言 ``.codewiki/`` 整个目录不存在：FileModel 缓存
    本来就会建它（``.codewiki/cache/``），把"不写历史"写成"不碰目录"是过强的断言。
    """
    repo = tmp_path / "repo"
    _build_repo(repo)

    assert _analyze(repo, tmp_path / "out", "--no-history") == 0

    assert not default_path(repo).exists()


def test_history_flag_redirects_the_file(tmp_path):
    """指向仓库内的受版本控制路径（团队共享趋势），而不是默认的忽略目录。"""
    repo = tmp_path / "repo"
    _build_repo(repo)
    target = tmp_path / "shared" / "architecture-history.jsonl"

    assert _analyze(repo, tmp_path / "out", "--history", str(target)) == 0

    entries, _ = read(target)
    assert len(entries) == 1
    assert not default_path(repo).exists(), "显式给了路径就不该再往默认位置写一份"


def test_history_never_lands_in_the_output_dir(tmp_path):
    repo = tmp_path / "repo"
    _build_repo(repo)
    out = tmp_path / "out"

    _analyze(repo, out)
    _analyze(repo, out)

    assert not (out / "history.jsonl").exists()
    assert not (out / "data" / "history.jsonl").exists()
    assert not (out / ".codewiki").exists()


def test_analyze_output_is_unaffected_by_history(tmp_path, monkeypatch):
    """同一 commit 两次 analyze ⇒ 产物逐字节相同。历史一旦进 out/ 或进首页，这条必红。

    这是**默认行为**的守卫：趋势记录照写，但首页不画 —— 归档物可复现是硬约束，
    趋势是运行视图（要图的人加 ``--trend``，代价写在它的 help 里）。
    """
    monkeypatch.setenv("SOURCE_DATE_EPOCH", FIXED_EPOCH)
    repo = tmp_path / "repo"
    _build_repo(repo, extra_modules=3)

    first = tmp_path / "first"
    second = tmp_path / "second"
    _analyze(repo, first)
    _analyze(repo, second)

    assert _snapshot(first) == _snapshot(second)


def test_trend_switch_draws_the_chart_on_the_index(tmp_path):
    """``--trend`` 才画。第一次跑只有 1 行 ⇒ 仍然不画（一个点不成趋势）。"""
    repo = tmp_path / "repo"
    _build_repo(repo)
    out = tmp_path / "out"

    _analyze(repo, out, "--trend")
    first_html = (out / "index.html").read_text(encoding="utf-8")
    assert t("site.trend") not in first_html

    _analyze(repo, out, "--trend")
    second_html = (out / "index.html").read_text(encoding="utf-8")
    assert t("site.trend") in second_html


def _packet():
    layers = [{"id": "layer::ui", "name": "ui", "order": 1}]
    nodes = [Node(id="mod::ui/a.py", type=NodeType.MODULE, path="ui/a.py", layer_id="layer::ui")]
    return packet_from_parts(
        title="t",
        layers=layers,
        nodes=nodes,
        edges=[
            Edge(
                source="mod::ui/a.py",
                target="mod::ui/a.py",
                type=EdgeType.IMPORTS,
                evidence="ui/a.py:1",
                confidence=CONF_STATIC,
            )
        ],
        annotations=[],
        diagnostics=[],
        generated_at="2026-01-01T00:00:00Z",
        repo_commit="deadbeef",
    )


def _entry(files: int):
    return build_entry(_packet(), files=files)


def test_a_single_history_row_draws_no_trend():
    """一个点画不出趋势。画出来会被读成"架构一直没变"，而真相是"还不知道"。"""
    html = render_index_html(_packet(), history=[_entry(1)])

    assert t("site.trend") not in html


def test_no_history_at_all_draws_no_trend():
    assert t("site.trend") not in render_index_html(_packet())


def test_two_rows_draw_a_trend_with_the_current_value_and_the_delta():
    html = render_index_html(_packet(), history=[_entry(10), _entry(13)])

    assert t("site.trend") in html
    assert t("site.trend_modules") in html
    assert "13" in html, "最新一轮的值必须标出来"
    assert "+3" in html, "相对上一轮的差值（这是这张图唯一的读法）"


def test_trend_block_keeps_the_newest_rows_only():
    """历史会无限长，图只画最近这一段；截断要说出来，否则等于谎报"这就是全部"。"""
    from codewiki.render.site import TREND_POINTS

    history = [_entry(index) for index in range(TREND_POINTS + 20)]
    html = render_index_html(_packet(), history=history)

    assert t("site.trend") in html
    assert str(TREND_POINTS + 19) in html, "最后一轮必须在图里"


def _raw_entry(*, modules: int, layer_edges: int, day: int = 1):
    from codewiki.graph.history import HISTORY_SCHEMA_VERSION, HistoryEntry

    return HistoryEntry(
        schema=HISTORY_SCHEMA_VERSION,
        generated_at=f"2026-01-{day:02d}T00:00:00Z",
        repo_commit=None,
        tool_version="1.2.0",
        counts={
            "files": modules,
            "modules": modules,
            "edges": 0,
            "layers": 1,
            "layer_edges": layer_edges,
            "layer_cycles": 0,
            "module_cycles": 0,
        },
        diagnostics={"error": 0, "warning": 0, "info": 0},
        violations={"error": 0, "warning": 0},
        layer_names=("ui",),
    )


def test_both_lines_share_one_vertical_axis():
    """模块 10→100、层间依赖恒为 2：共用轴下层间依赖必须是**贴底的水平线**。

    各自归一化时恒定的那条会画在中线（"看起来稳定在中间"）、模块那条铺满全高 ——
    两条线同时失真，而读者看不出哪个数字是真的。
    """
    import re

    history = [
        _raw_entry(modules=10, layer_edges=2, day=1),
        _raw_entry(modules=10, layer_edges=2, day=2),
        _raw_entry(modules=100, layer_edges=2, day=3),
    ]
    html = render_index_html(_packet(), history=history)

    points = re.search(r'class="trend-line-b" points="([^"]+)"', html)
    assert points, "层间依赖那条线没画出来"
    ys = [float(pair.split(",")[1]) for pair in points.group(1).split()]
    assert len(set(ys)) == 1, f"恒为 2 的序列不该有起伏：{ys}"
    assert ys[0] > 120, f"共用轴下它应当贴底（最大值 100 撑满高度），实际 y={ys[0]}"


def _write_history(repo: Path, entries) -> None:
    from codewiki.graph.history import append

    for entry in entries:
        append(default_path(repo), entry)


def test_diff_architecture_reports_deltas(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_history(repo, [_entry(10), _entry(13)])

    code = main(["diff", "--architecture", "--src", str(repo)])

    out = capsys.readouterr().out
    assert code == 0
    assert "文件" in out
    assert "+3" in out


def test_diff_architecture_says_so_when_there_is_only_one_row(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_history(repo, [_entry(10)])

    code = main(["diff", "--architecture", "--src", str(repo)])

    out = capsys.readouterr().out
    assert code == 0
    assert "1" in out and "没有" in out, f"必须说清「只有一条、无从比较」：{out}"


def test_diff_architecture_without_history_exits_2(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()

    code = main(["diff", "--architecture", "--src", str(repo)])

    captured = capsys.readouterr()
    assert code == 2
    assert "history" in (captured.out + captured.err).lower()


def test_diff_architecture_names_added_and_removed_layers(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    old = build_entry(
        packet_from_parts(
            title="t",
            layers=[{"id": "layer::a", "name": "a", "order": 1}, {"id": "layer::b", "name": "b", "order": 2}],
            nodes=[],
            edges=[],
            annotations=[],
            diagnostics=[],
            generated_at="2026-01-01T00:00:00Z",
            repo_commit="aaa",
        ),
        files=0,
    )
    new = build_entry(
        packet_from_parts(
            title="t",
            layers=[{"id": "layer::a", "name": "a", "order": 1}, {"id": "layer::c", "name": "c", "order": 2}],
            nodes=[],
            edges=[],
            annotations=[],
            diagnostics=[],
            generated_at="2026-01-02T00:00:00Z",
            repo_commit="bbb",
        ),
        files=0,
    )
    _write_history(repo, [old, new])

    main(["diff", "--architecture", "--src", str(repo)])

    out = capsys.readouterr().out
    assert "c" in out and "b" in out, f"层的增删必须点名到具体名字：{out}"


def test_diff_architecture_says_unchanged_instead_of_leaving_it_blank(tmp_path, capsys):
    """某轮只改了代码、没改结构 ⇒ 六个数字全等。留白会被读成"没比"。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_history(repo, [_entry(10), _entry(10)])

    main(["diff", "--architecture", "--src", str(repo)])

    out = capsys.readouterr().out
    assert "未变" in out


def test_diff_architecture_reads_an_explicit_history_path(tmp_path, capsys):
    target = tmp_path / "shared.jsonl"
    from codewiki.graph.history import append

    append(target, _entry(1))
    append(target, _entry(2))

    code = main(["diff", "--architecture", "--history", str(target)])

    assert code == 0
    assert "+1" in capsys.readouterr().out


def test_rule_violation_growth_is_reported_separately(tmp_path, capsys):
    """诊断计数与规则违例要分开报：前者是解析质量，后者是架构债。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    clean = build_entry(_packet(), files=1)
    dirty_packet = _packet()
    dirty_packet.diagnostics.append(
        Diagnostic(level="error", code="RULE_VIOLATION", message="x", where="ui/a.py:1", rule_id="r")
    )
    _write_history(repo, [clean, build_entry(dirty_packet, files=1)])

    main(["diff", "--architecture", "--src", str(repo)])

    out = capsys.readouterr().out
    assert "规则违例" in out
    assert "+1" in out
