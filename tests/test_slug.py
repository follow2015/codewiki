"""slug 唯一性判据（2026-10-23 评审 M3 修复回归）。

``_slug`` 此前对点文件产生空串（``.env`` / ``.gitignore`` / ``.nvmrc`` 共写一个
页面），且 ``a-b.py`` 与 ``a/b.py`` 都映射成 ``a_b``（页面互覆）。修复后：

1. 点文件保留完整名（前缀 ``_`` 防隐藏文件）；
2. 残余碰撞由 ``build_slug_map`` 确定性消歧（首轮保留简单名，后续追加
   ``~<sha1_8>``；同样的输入集合两轮渲染结果一致 —— 增量渲染依赖这一点）；
3. 页面文件名、index 链接、页内互链必须同源（同一个 map）。
"""
from __future__ import annotations

from codewiki.cli import main
from codewiki.render.diagrams import _slug, build_slug_map


def test_dotfiles_keep_distinct_slugs():
    assert _slug(".env") != _slug(".gitignore") != _slug(".nvmrc")
    assert _slug(".env") and not _slug(".env").startswith("."), "页面名不能以点开头（隐藏文件）"


def test_slug_map_disambiguates_collisions_deterministically():
    paths = ["a/b.py", "a-b.py", "a_b.py", ".env", "x/.env"]
    mapping = build_slug_map(paths)

    assert len(set(mapping.values())) == len(paths), f"仍有碰撞: {mapping}"
    assert build_slug_map(list(reversed(paths))) == mapping
    assert build_slug_map(["app/a.py", "app/b.py"]) == {"app/a.py": "app_a", "app/b.py": "app_b"}


def test_dotfile_pages_do_not_overwrite_each_other(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app").mkdir()
    (root / "app" / "a.py").write_text("A = 1\n", encoding="utf-8")
    (root / ".env").write_text("X=1\n", encoding="utf-8")
    (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (root / ".nvmrc").write_text("20\n", encoding="utf-8")
    out = tmp_path / "out"

    assert main(["analyze", "--src", str(root), "--out", str(out)]) == 0

    pages = sorted(p.name for p in (out / "modules").glob("*.md"))
    dot_pages = [p for p in pages if "env" in p or "gitignore" in p or "nvmrc" in p]
    assert len(dot_pages) == 3, f"三个点文件应各自成页，实际: {pages}"


def test_index_links_point_to_existing_pages(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a-b.py").write_text("A = 1\n", encoding="utf-8")
    (root / "b").mkdir()
    (root / "b" / "a_b.py").write_text("B = 2\n", encoding="utf-8")
    out = tmp_path / "out"

    assert main(["analyze", "--src", str(root), "--out", str(out)]) == 0

    index_text = (out / "index.md").read_text(encoding="utf-8")
    for page in (out / "modules").glob("*.md"):
        name = page.name[: -len(".md")]
        assert f"{name}.md" in index_text, f"index 链接缺页 {name}"
