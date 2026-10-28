"""FileModel 增量缓存判据（§7）。

三条全都要钉住，因为失效方式各不相同：

1. **键含三个成分**（路径 / mtime / 内容 hash）—— 少一个就会出现"改了内容却读到
   旧事实"或"没改却白白重解析"；
2. **坏缓存当未命中**：缓存是加速手段，不是真源；
3. **自污染**：缓存写在仓库内（`.codewiki/`），必须被扫描排除 —— 否则第二轮
   开始图谱里会冒出几千个缓存文件"模块"（第一轮永远测不出来）；
4. **不能进版本库**：缓存目录自带 ``.gitignore``（内容 ``*``）—— 否则一次
   ``git add -A`` 就把上一轮的解析结果提交了，之后 clone 下来命中旧 FileModel。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from codewiki.analyzers.python_analyzer import PythonAstAnalyzer
from codewiki.file_cache import FileModelCache, content_hash_of, mtime_ns_of
from codewiki.models import FileModel
from codewiki.repo import DEFAULT_EXCLUDES, is_excluded, iter_source_files

SOURCE = "class A:\n    pass\n"


def _model(path: str = "a.py", source: str = SOURCE) -> FileModel:
    return PythonAstAnalyzer().parse(Path(path), source)


def test_roundtrip(tmp_path):
    cache = FileModelCache(tmp_path)
    model = _model()
    key = FileModelCache.key("a.py", 123, content_hash_of(SOURCE), "ast", parser_version=1)

    assert cache.get(key) is None
    cache.set(key, model)
    got = cache.get(key)
    assert got is not None and [s.name for s in got.symbols] == ["A"]


def test_key_covers_path_mtime_content_backend_and_parser_version():
    """五个维度缺一不可 —— 少任何一个都会出现"代码改了、产物没变"或"换了后端却命中旧缓存"。"""

    def key(path="a.py", mtime=123, digest="h1", backend="ast", parser_version=1):
        return FileModelCache.key(path, mtime, digest, backend, parser_version=parser_version)

    assert key() != key(path="b.py")
    assert key() != key(mtime=456)
    assert key() != key(digest="h2")
    assert key() != key(backend="regex")
    assert key() != key(parser_version=2), "解析逻辑改了（版本 +1）必须让旧缓存全部失效"
    assert key() == key()


def test_corrupt_cache_is_a_miss(tmp_path):
    cache = FileModelCache(tmp_path)
    key = FileModelCache.key("a.py", 1, "h", "ast", parser_version=1)
    cache.set(key, _model())
    path = cache._path_of(key)
    path.write_text("{broken", encoding="utf-8")
    assert cache.get(key) is None


def test_unknown_fields_are_a_miss(tmp_path):
    """模型演进来后旧缓存不该把整轮拖垮（字段不符 = 未命中）。"""
    cache = FileModelCache(tmp_path)
    key = FileModelCache.key("a.py", 1, "h", "ast", parser_version=1)
    cache.set(key, _model())
    cache._path_of(key).write_text('{"path": "a.py"}', encoding="utf-8")
    assert cache.get(key) is None


def test_unwritable_cache_degrades_silently(tmp_path, monkeypatch):
    cache = FileModelCache(tmp_path)
    monkeypatch.setattr(Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
    cache.set("k", _model())
    assert cache.enabled is False
    assert cache.get("k") is None


def test_cache_dir_is_never_scanned_as_source():
    """自污染防线：`.codewiki/**` 必须被排除（第二轮才会暴露的问题）。"""
    assert ".codewiki" in DEFAULT_EXCLUDES
    assert is_excluded(".codewiki/cache/01/abc.json")
    assert is_excluded("wiki-output/data/x.json") is False


def test_second_scan_after_cache_write_stays_clean(tmp_path, monkeypatch):
    """端到端自污染：写完缓存再扫一遍，不能多出文件。"""
    (tmp_path / "a.py").write_text(SOURCE, encoding="utf-8")
    first = list(iter_source_files(tmp_path))
    FileModelCache(tmp_path).set("k", _model())
    second = list(iter_source_files(tmp_path))

    assert [f.path for f in first] == [f.path for f in second] == ["a.py"]


@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")
def test_cache_dir_carries_its_own_gitignore(tmp_path):
    """缓存目录自带 ``.gitignore``（内容 ``*``）：``git add -A`` 不该把缓存收进仓库。

    这条来自实测事故：``tests/fixtures/cyclic_py/.codewiki/cache/`` 的 9 个 JSON
    被 ``git add -A`` 提交进库（5cb83df）。后果不只是"脏" —— 缓存跟着 clone 走之后，
    解析器改了行为但 ``parser_version`` 忘了 bump 时，下一轮照样命中旧 FileModel，
    测试全绿而产物是旧的。ai-cache 一直自带忽略文件，FileModel 缓存当时漏了。
    """
    FileModelCache(tmp_path).set("k", _model())

    ignore = tmp_path / ".codewiki" / "cache" / ".gitignore"
    assert ignore.is_file(), "缓存目录必须自带 .gitignore"
    assert ignore.read_text(encoding="utf-8").strip() == "*"

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    listed = subprocess.run(
        ["git", "-C", str(tmp_path), "status", "--porcelain", "--untracked-files=all"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert listed.stdout.strip() == "", f"缓存不能被 git 看见，实际: {listed.stdout!r}"


def test_missing_stat_does_not_block(tmp_path):
    """stat 不到就用 0（键里退化为常量），绝不因拿不到 mtime 而中断整轮。"""
    assert content_hash_of(SOURCE)
    assert mtime_ns_of(tmp_path / "nope.py") == 0


def test_key_covers_backend():
    """同一份文件不同后端 ⇒ 不同键。

    键里没有 backend 时，`--backend ast` 之后再跑 `--backend regex` 会全部命中
    ast 的缓存：用户点名要 regex，拿到的是 ast 的事实，provenance 也跟着说谎
    （实测第二轮 provenance 仍报 ast）。
    """
    assert FileModelCache.key("a.py", 123, "h1", "ast", parser_version=1) != FileModelCache.key(
        "a.py", 123, "h1", "regex", parser_version=1
    )
