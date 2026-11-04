"""round 14：docstring 里的 javascript: 链接不得进入页面；init --ai 要遵守 [ai] enabled=false。"""

from __future__ import annotations

import pytest

from codewiki.cli import main
from codewiki.render.site import md_to_html

TABLE = "| a | b |\n| --- | --- |\n| `x` | {cell} |"


def _cell(cell: str) -> str:
    return md_to_html(TABLE.format(cell=cell))


@pytest.mark.parametrize(
    "target",
    [
        "javascript:alert(1",
        "javascript:location=name",
        "JaVaScRiPt:location=name",
        "\x01javascript:location=name",
        "data:text/html,x",
        "vbscript:x",
        "file:///etc/passwd",
    ],
)
def test_unsafe_link_targets_render_as_plain_text(target):
    html = _cell(f"[点我]({target})")
    assert "<a " not in html and "href" not in html, html
    assert "点我" in html


@pytest.mark.parametrize(
    "target",
    ["other.md", "../x/y.md#sec", "#anchor", "https://example.com/a?b=1", "http://e.org", "mailto:a@b.c"],
)
def test_safe_link_targets_stay_links(target):
    assert "<a href=" in _cell(f"[文档]({target})")


def test_forged_code_placeholder_is_not_expanded():
    html = _cell("\x000\x00 `<img src=x onerror=alert(1)>`")
    assert html.count("<code>") == 2, html
    assert html.count("&lt;img") == 1


def test_end_to_end_docstring_link_does_not_reach_the_html(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "a.py").write_text(
        'def run():\n    """[详见](javascript:location=name) 说明"""\n    return 1\n', encoding="utf-8"
    )
    out = tmp_path / "out"
    assert main(["analyze", "--src", str(pkg), "--out", str(out)]) == 0
    pages = list((out / "modules").glob("*.html"))
    assert pages
    for page in pages:
        assert "javascript:" not in page.read_text(encoding="utf-8").lower()


def _repo(root) -> None:  # noqa: ANN001
    for rel, text in {
        "app/api/h.py": "from app.svc import l\n",
        "app/svc/l.py": "from app.m import r\n",
        "app/m/r.py": "X = 1\n",
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _optout(path) -> None:  # noqa: ANN001
    path.write_text("[project]\nname = 'x'\n\n[ai]\nenabled = false\n", encoding="utf-8")


def test_init_explicit_ai_is_refused_when_repo_opts_out(tmp_path, capsys):
    repo = tmp_path / "repo"
    _repo(repo)
    _optout(repo / "codewiki.toml")
    deferred = tmp_path / "deferred"
    rc = main(
        ["init", "--src", str(repo), "--path", str(tmp_path / "o"), "--ai", "--deferred", str(deferred)]
    )
    assert rc == 2
    assert "enabled = false" in capsys.readouterr().err
    assert not deferred.exists(), "不得导出任何请求"


def test_init_default_falls_back_to_directory_draft_when_repo_opts_out(tmp_path, capsys):
    repo = tmp_path / "repo"
    _repo(repo)
    _optout(repo / "codewiki.toml")
    deferred = tmp_path / "deferred"
    out = tmp_path / "o"
    rc = main(["init", "--src", str(repo), "--path", str(out), "--deferred", str(deferred)])
    assert rc == 0
    assert (out / "codewiki.toml").exists()
    assert not deferred.exists()
    assert "目录起草" in capsys.readouterr().err


def test_init_target_config_that_opts_out_is_also_honored(tmp_path):
    repo = tmp_path / "repo"
    _repo(repo)
    out = tmp_path / "o"
    out.mkdir()
    _optout(out / "codewiki.toml")
    deferred = tmp_path / "deferred"
    rc = main(
        ["init", "--src", str(repo), "--path", str(out), "--force", "--ai", "--deferred", str(deferred)]
    )
    assert rc == 2 and not deferred.exists()


def test_init_force_ai_overrides_the_opt_out(tmp_path):
    repo = tmp_path / "repo"
    _repo(repo)
    _optout(repo / "codewiki.toml")
    deferred = tmp_path / "deferred"
    rc = main(
        [
            "init",
            "--src",
            str(repo),
            "--path",
            str(tmp_path / "o"),
            "--ai",
            "--force-ai",
            "--deferred",
            str(deferred),
        ]
    )
    assert rc == 3, "显式 --force-ai 才能跑；请求已导出（待作答）"


def test_init_unparsable_existing_config_does_not_block_regeneration(tmp_path):
    """目标配置语法错 ⇒ 不算"项目方拒绝"，AI 照跑。

    必须走 ``--ai``：上一版这条用 ``--no-ai``，``use_ai`` 本来就是 False，
    ``_repo_ai_denial`` 根本没被调用 —— 把 ``except ConfigError: continue``
    改成 ``raise`` 它照样绿。变异验证当场戳穿，所以改成真的会读配置的路径。
    """
    repo = tmp_path / "repo"
    _repo(repo)
    out = tmp_path / "o"
    out.mkdir()
    (out / "codewiki.toml").write_text("[[[ not toml", encoding="utf-8")
    deferred = tmp_path / "deferred"

    rc = main(
        ["init", "--src", str(repo), "--path", str(out), "--force", "--ai", "--deferred", str(deferred)]
    )

    assert rc == 3, "坏配置不该拦下 AI 起草（rc=1 说明 ConfigError 漏成了 traceback）"
    assert (deferred / "requests").is_dir(), "请求应已导出"


def test_init_src_subdir_still_honors_repo_root_opt_out(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    _repo(repo)
    _optout(repo / "codewiki.toml")
    monkeypatch.chdir(repo)
    deferred = tmp_path / "deferred"

    rc = main(["init", "--src", "app", "--path", str(tmp_path / "o"), "--ai", "--deferred", str(deferred)])

    assert rc == 2, "站在 opt-out 仓库里 --src 指子目录，仍必须拒绝"
    assert not deferred.exists(), "不得导出任何请求"
    assert "enabled = false" in capsys.readouterr().err


def test_init_denial_names_the_config_file_it_obeyed(tmp_path, monkeypatch, capsys):
    """三份候选文件时，只说"该仓库的 codewiki.toml"等于让人去猜是哪一份。"""
    repo = tmp_path / "repo"
    _repo(repo)
    _optout(repo / "codewiki.toml")
    monkeypatch.chdir(repo)

    rc = main(
        ["init", "--src", "app", "--path", str(tmp_path / "o"), "--ai", "--deferred", str(tmp_path / "d")]
    )

    err = capsys.readouterr().err
    assert rc == 2
    assert str((repo / "codewiki.toml").resolve()) in err, err
