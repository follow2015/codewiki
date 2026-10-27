"""验收：``--incremental`` 的产物必须与同一输入的**全量**产物逐字节一致。

这是设计文稿 §数据包与增量分析 的验收闸 —— 它逼出的不是"大概差不多"，而是
"增量复用**没有留下任何痕迹**"：多一个残留页面、少一条诊断、某个模块页停在上一
轮的旧图上，都算失败。

八种变更覆盖了增量逻辑的全部分支：内容变更（改 / 增 / 删 / 改名）、渲染输入变更
（分层 / 阈值 / 后端 / 工具版本）。前四种走"影响闭包"，后四种走"直接全量"。

之所以能做逐字节比对，靠的是可注入时钟（``SOURCE_DATE_EPOCH``）与"运行统计不进
归档物"两条前提 —— 缺一个，比对就只能退化成"排除某几个字段"，而排除式比对迟早
漏掉真正该比的东西。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codewiki.cli import main
from codewiki.pipeline.incremental import has_incomplete_marker

FIXED_EPOCH = "1767225600"


def _build_repo(root: Path, count: int = 5) -> None:
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "pkg" / "__init__.py").write_text("#\n", encoding="utf-8")
    for i in range(count):
        (root / "pkg" / f"m{i}.py").write_text(
            f"from pkg.m{(i + 1) % count} import NEXT\n\n\n"
            f"class C{i}:\n    def run(self) -> int:\n        return NEXT\n",
            encoding="utf-8",
        )
    (root / "pkg" / "iso.py").write_text("VALUE = 1\n", encoding="utf-8")


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _analyze(repo: Path, out: Path, *extra: str) -> None:
    assert main(["analyze", "--src", str(repo), "--out", str(out), *extra]) == 0


def _edit_one_file(repo: Path, flags: list[str]) -> None:
    target = repo / "pkg" / "iso.py"
    target.write_text(target.read_text(encoding="utf-8") + "\nVALUE2 = 2\n", encoding="utf-8")


def _add_file(repo: Path, flags: list[str]) -> None:
    (repo / "pkg" / "fresh.py").write_text(
        "from pkg.iso import VALUE\n\n\ndef use() -> int:\n    return VALUE\n", encoding="utf-8"
    )


def _delete_file(repo: Path, flags: list[str]) -> None:
    (repo / "pkg" / "m4.py").unlink()


def _rename_file(repo: Path, flags: list[str]) -> None:
    (repo / "pkg" / "iso.py").rename(repo / "pkg" / "iso_renamed.py")


def _change_layers(repo: Path, flags: list[str]) -> None:
    (repo / "codewiki.toml").write_text(
        '[architecture]\n[[architecture.layers]]\nname = "核心层"\npaths = ["pkg"]\ncolor = "#C8E6C9"\n',
        encoding="utf-8",
    )


def _change_min_confidence(repo: Path, flags: list[str]) -> None:
    flags.extend(["--min-confidence", "0.95"])


def _switch_backend(repo: Path, flags: list[str]) -> None:
    flags.extend(["--backend", "regex"])


def _bump_tool_version(repo: Path, flags: list[str], monkeypatch) -> None:
    """工具版本变了 ⇒ 指纹变 ⇒ 本轮必须全量（否则旧页面会带着旧版本的渲染结论
    继续存在，而产物里写着新版本号）。"""
    import codewiki.packetio as packetio
    import codewiki.pipeline.incremental as incremental

    monkeypatch.setattr(incremental, "__version__", "9.9.9")
    monkeypatch.setattr(packetio, "__version__", "9.9.9")


MUTATIONS = [
    ("改一个文件", _edit_one_file),
    ("新增文件", _add_file),
    ("删除文件", _delete_file),
    ("重命名文件", _rename_file),
    ("改分层", _change_layers),
    ("改 min_confidence", _change_min_confidence),
    ("换后端", _switch_backend),
    ("升工具版本", _bump_tool_version),
]


@pytest.mark.guarantee("incremental-equals-full")
def test_incremental_output_matches_full_output_byte_for_byte(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", FIXED_EPOCH)
    repo = tmp_path / "repo"
    _build_repo(repo)
    incremental_out = tmp_path / "incremental"
    _analyze(repo, incremental_out, "--incremental")

    for label, mutate in MUTATIONS:
        flags: list[str] = []
        if mutate is _bump_tool_version:
            mutate(repo, flags, monkeypatch)  # type: ignore[call-arg]
        else:
            mutate(repo, flags)
        _analyze(repo, incremental_out, "--incremental", *flags)

        full_out = tmp_path / f"full-{label}"
        _analyze(repo, full_out, *flags)

        incremental_files = _snapshot(incremental_out)
        full_files = _snapshot(full_out)
        missing = sorted(set(full_files) - set(incremental_files))
        extra = sorted(set(incremental_files) - set(full_files))
        differing = sorted(
            name
            for name in set(incremental_files) & set(full_files)
            if incremental_files[name] != full_files[name]
        )
        assert not missing, f"[{label}] 增量产物缺少文件：{missing}"
        assert not extra, f"[{label}] 增量产物多出残留文件（上一轮没清干净）：{extra}"
        assert not differing, f"[{label}] 增量与全量内容不同：{differing}"


def test_incremental_really_is_incremental(tmp_path, monkeypatch, capsys):
    """验收的另一半：不能为了"一致"就每轮全量 —— 那样比对恒成立却毫无意义。

    判据是 stdout 里的增量说明（它不进归档物）：改一个独立模块时复用数必须 > 0。
    """
    monkeypatch.setenv("SOURCE_DATE_EPOCH", FIXED_EPOCH)
    repo = tmp_path / "repo"
    _build_repo(repo)
    out = tmp_path / "out"
    _analyze(repo, out, "--incremental")
    _edit_one_file(repo, [])
    capsys.readouterr()
    _analyze(repo, out, "--incremental")

    note = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("增量渲染："))
    reused = int(note.split("复用 ")[1].split(" ")[0])
    assert reused > 0, f"这轮退化成全量了（复用 0）：{note}"


def test_interrupted_render_does_not_reuse_partial_output(tmp_path, monkeypatch):
    """渲染中途崩溃 ⇒ 留下 ``.incomplete`` ⇒ 下一轮必须全量重渲，且结果仍与全量一致。

    不用"临时目录整体切换"来换取一致性：那会让被复用的页面也要整份拷贝，
    增量的速度优势就没了（设计文稿明确排除）。
    """
    import codewiki.cli as cli_mod

    monkeypatch.setenv("SOURCE_DATE_EPOCH", FIXED_EPOCH)
    repo = tmp_path / "repo"
    _build_repo(repo)
    out = tmp_path / "out"
    _analyze(repo, out, "--incremental")

    _edit_one_file(repo, [])
    original_render_all = cli_mod.render_all

    def _boom(*_a, **_k):
        raise RuntimeError("模拟渲染中途崩溃")

    monkeypatch.setattr(cli_mod, "render_all", _boom)
    with pytest.raises(RuntimeError):
        main(["analyze", "--src", str(repo), "--out", str(out), "--incremental"])

    assert has_incomplete_marker(out), "崩溃后必须留下中断标记"
    monkeypatch.setattr(cli_mod, "render_all", original_render_all)

    _analyze(repo, out, "--incremental")
    assert not has_incomplete_marker(out)

    full_out = tmp_path / "full"
    _analyze(repo, full_out)
    assert _snapshot(out) == _snapshot(full_out), "中断后仍有残缺产物被当成最新复用"
