"""增量渲染判据（§7）。

判断"复用"不能靠 mtime（同一秒内会相同），所以这里用**哨兵内容**：把某模块页
改写成一个不可能由渲染产生的字符串，跑完增量后 ——

* 未受影响的页：哨兵**还在**（说明真的没重渲）；
* 受影响闭包内的页：哨兵**被覆盖**（说明真的重渲了）。

另外三条"增量该退出"的情形也要钉住（否则"开了增量却没生效/退化成全量"会变成
又一个静默问题）：无上一轮快照、快照损坏、分层或注解有变化。
"""

from __future__ import annotations

import json
from pathlib import Path

from codewiki.cli import main
from codewiki.pipeline.incremental import has_incomplete_marker, write_incomplete_marker

SENTINEL = "SENTINEL-SHOULD-SURVIVE-IF-REUSED"


def _build_repo(root: Path, count: int = 6) -> None:
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "pkg" / "__init__.py").write_text("#\n", encoding="utf-8")
    for i in range(count):
        (root / "pkg" / f"m{i}.py").write_text(
            f"from pkg.m{(i + 1) % count} import NEXT\n\n\nclass C{i}:\n    def run(self) -> int:\n        return NEXT\n",
            encoding="utf-8",
        )
    (root / "pkg" / "iso.py").write_text("VALUE = 1\n", encoding="utf-8")


def _note(capsys) -> str:
    """增量说明走 **stdout**，不进数据包。

    数据包是归档物（描述"仓库长什么样"），而"这一轮重渲了几个模块"是本次运行的
    过程信息。写进 packet.diagnostics 会让同一份输入的增量产物永远比全量产物多
    一条诊断 —— tests/test_incremental_equals_full.py 的逐字节验收就废了。
    """
    return capsys.readouterr().out


def test_incremental_reuses_untouched_pages_and_rerenders_affected(tmp_path, capsys):
    repo = tmp_path / "repo"
    out = tmp_path / "out"
    _build_repo(repo)
    assert main(["analyze", "--src", str(repo), "--out", str(out)]) == 0

    untouched = out / "modules" / "pkg_iso.md"
    affected = out / "modules" / "pkg_m2.md"
    untouched.write_text(SENTINEL, encoding="utf-8")
    affected.write_text(SENTINEL, encoding="utf-8")
    untouched_mmd = out / "diagrams" / "modules" / "pkg_iso.mmd"
    affected_mmd = out / "diagrams" / "modules" / "pkg_m2.mmd"
    untouched_mmd.write_text(SENTINEL, encoding="utf-8")
    affected_mmd.write_text(SENTINEL, encoding="utf-8")

    (repo / "pkg" / "m2.py").write_text(
        (repo / "pkg" / "m2.py").read_text(encoding="utf-8") + "\n# 改动\n", encoding="utf-8"
    )
    assert main(["analyze", "--src", str(repo), "--out", str(out), "--incremental"]) == 0

    assert "增量渲染：重渲" in _note(capsys)
    assert untouched.read_text(encoding="utf-8") == SENTINEL, "未受影响的页不该被重渲"
    assert affected.read_text(encoding="utf-8") != SENTINEL, "受影响的页必须重渲"
    assert untouched_mmd.read_text(encoding="utf-8") == SENTINEL, "未受影响的图不该被重渲"
    assert affected_mmd.read_text(encoding="utf-8") != SENTINEL, "受影响的图必须重渲"


def test_first_run_without_snapshot_falls_back_to_full(tmp_path, capsys):
    repo = tmp_path / "repo"
    out = tmp_path / "out"
    _build_repo(repo)

    assert main(["analyze", "--src", str(repo), "--out", str(out), "--incremental"]) == 0
    assert "无上一轮快照" in _note(capsys), "没有上一轮快照就该全量，并说明原因"
    assert (out / "modules" / "pkg_m0.md").exists()


def test_corrupt_snapshot_falls_back_to_full(tmp_path, capsys):
    repo = tmp_path / "repo"
    out = tmp_path / "out"
    _build_repo(repo)
    assert main(["analyze", "--src", str(repo), "--out", str(out)]) == 0

    (out / "data" / "codewiki.json").write_text("{broken", encoding="utf-8")
    assert main(["analyze", "--src", str(repo), "--out", str(out), "--incremental"]) == 0
    assert "无上一轮快照" in _note(capsys), "读不懂上一轮就当没有（全量），不是崩掉"


def test_config_change_forces_full_rerender(tmp_path, capsys):
    """分层/注解变了 ⇒ 每个模块页都可能变（页里带层名与注解）⇒ 必须全量。"""
    repo = tmp_path / "repo"
    out = tmp_path / "out"
    config = tmp_path / "codewiki.toml"
    config.write_text('[[architecture.layers]]\nname = "L1"\npaths = ["pkg/m0.py"]\n', encoding="utf-8")
    _build_repo(repo)
    assert main(["analyze", "--src", str(repo), "--out", str(out), "--config", str(config)]) == 0

    changed = tmp_path / "codewiki2.toml"
    changed.write_text(
        '[[architecture.layers]]\nname = "L1"\npaths = ["pkg/m0.py"]\n\n'
        '[[architecture.layers]]\nname = "L2"\npaths = ["pkg"]\n',
        encoding="utf-8",
    )
    assert (
        main(["analyze", "--src", str(repo), "--out", str(out), "--config", str(changed), "--incremental"])
        == 0
    )
    assert "分层定义已变化" in _note(capsys)


def test_no_flag_means_full_render(tmp_path, capsys):
    """默认不做增量（显式开关）：不带 --incremental 时不该出现增量说明。"""
    repo = tmp_path / "repo"
    out = tmp_path / "out"
    _build_repo(repo)
    assert main(["analyze", "--src", str(repo), "--out", str(out)]) == 0
    (repo / "pkg" / "m1.py").write_text(
        (repo / "pkg" / "m1.py").read_text(encoding="utf-8") + "\n# 改\n", encoding="utf-8"
    )
    assert main(["analyze", "--src", str(repo), "--out", str(out)]) == 0
    assert "增量" not in _note(capsys)


def _with_sentinel(out: Path) -> Path:
    page = out / "modules" / "pkg_iso.md"
    page.write_text(SENTINEL, encoding="utf-8")
    return page


def test_min_confidence_change_forces_full_rerender(tmp_path, capsys):
    """源码一字未改，但阈值变了 ⇒ 图变了 ⇒ 页面必须重渲（指纹只含分层就漏这里）。"""
    repo, out = tmp_path / "repo", tmp_path / "out"
    _build_repo(repo)
    loose = tmp_path / "loose.toml"
    loose.write_text("[analysis]\nmin_confidence = 0.1\n", encoding="utf-8")
    strict = tmp_path / "strict.toml"
    strict.write_text("[analysis]\nmin_confidence = 0.95\n", encoding="utf-8")

    assert main(["analyze", "--src", str(repo), "--out", str(out), "--config", str(loose)]) == 0
    page = _with_sentinel(out)

    assert (
        main(["analyze", "--src", str(repo), "--out", str(out), "--config", str(strict), "--incremental"])
        == 0
    )

    note = _note(capsys)
    assert "全量渲染" in note
    assert "置信度下限" in note, "要能说清是哪个分量变了"
    assert page.read_text(encoding="utf-8") != SENTINEL, "阈值变了就不能复用旧页面"


def test_tool_version_change_forces_full_rerender(tmp_path, monkeypatch, capsys):
    """工具升级（渲染逻辑大概率也变了）⇒ 不复用上一轮页面。"""
    import codewiki.pipeline.incremental as incremental

    repo, out = tmp_path / "repo", tmp_path / "out"
    _build_repo(repo)
    assert main(["analyze", "--src", str(repo), "--out", str(out)]) == 0
    page = _with_sentinel(out)

    monkeypatch.setattr(incremental, "__version__", "9.9.9")
    assert main(["analyze", "--src", str(repo), "--out", str(out), "--incremental"]) == 0

    assert "工具版本" in _note(capsys)
    assert page.read_text(encoding="utf-8") != SENTINEL


def test_successful_run_leaves_no_incomplete_marker(tmp_path):
    repo, out = tmp_path / "repo", tmp_path / "out"
    _build_repo(repo)

    assert main(["analyze", "--src", str(repo), "--out", str(out)]) == 0

    assert not has_incomplete_marker(out), "全部产物齐全后必须清标记"


def test_leftover_marker_forces_full_rerender(tmp_path, capsys):
    """模拟"上一轮渲染跑到一半被杀"：快照在，但页面可能残缺。"""
    repo, out = tmp_path / "repo", tmp_path / "out"
    _build_repo(repo)
    assert main(["analyze", "--src", str(repo), "--out", str(out)]) == 0
    page = _with_sentinel(out)
    write_incomplete_marker(out)

    assert main(["analyze", "--src", str(repo), "--out", str(out), "--incremental"]) == 0

    assert "未完成" in _note(capsys)
    assert page.read_text(encoding="utf-8") != SENTINEL, "残缺产物不得被当成可复用"
    assert not has_incomplete_marker(out), "这一轮成功了，标记要清掉"


def test_old_snapshot_without_fingerprint_is_treated_as_full(tmp_path, capsys):
    """旧版本产物没有 render_inputs ⇒ 无法判断 ⇒ 全量（宁可多渲，不可复用旧结论）。"""
    repo, out = tmp_path / "repo", tmp_path / "out"
    _build_repo(repo)
    assert main(["analyze", "--src", str(repo), "--out", str(out)]) == 0

    packet_path = out / "data" / "codewiki.json"
    data = json.loads(packet_path.read_text(encoding="utf-8"))
    data["meta"].pop("render_inputs", None)
    packet_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    assert main(["analyze", "--src", str(repo), "--out", str(out), "--incremental"]) == 0

    assert "没有渲染指纹" in _note(capsys)
