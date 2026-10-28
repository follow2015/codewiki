"""``scripts/verify-wheel.py`` 的回归用例。

这个脚本是"交付形态"的唯一判据：``pip install -e .`` 不读 wheel，所以
打包配置写错（packages.find 漏子包、漏 package-data）时，冲烟和 pytest 都可能
是绿的，只有用户装完之后才炸。这里用构造出来的假 wheel 钉住它的判定方向 ——
**能发现缺**，而不是"永远说通过"。
"""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_verifier():
    spec = importlib.util.spec_from_file_location("verify_wheel", ROOT / "scripts" / "verify-wheel.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


verify_wheel = _load_verifier()


def _make_wheel(path: Path, entries: list[str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name in entries:
            zf.writestr(name, "")
    return path


@pytest.fixture()
def src_tree(tmp_path: Path) -> Path:
    root = tmp_path / "src" / "codewiki"
    (root / "sub").mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "core.py").write_text("", encoding="utf-8")
    (root / "sub" / "__init__.py").write_text("", encoding="utf-8")
    (root / "sub" / "deep.py").write_text("", encoding="utf-8")
    return tmp_path / "src"


FULL = [
    "codewiki/__init__.py",
    "codewiki/core.py",
    "codewiki/sub/__init__.py",
    "codewiki/sub/deep.py",
    "codewiki/assets/mermaid.min.js",
    "codewiki/assets/mermaid.min.js.sha256",
]


def test_complete_wheel_passes(tmp_path, src_tree):
    wheel = _make_wheel(tmp_path / "ok.whl", FULL)
    assert verify_wheel.verify(wheel, src_tree) == []


def test_missing_subpackage_is_reported(tmp_path, src_tree):
    """整块子包漏掉 —— packages.find 写窄了就是这个形态。"""
    entries = [n for n in FULL if not n.startswith("codewiki/sub/")]
    problems = verify_wheel.verify(_make_wheel(tmp_path / "bad.whl", entries), src_tree)
    assert any("漏了" in p and "2" in p for p in problems), problems


def test_extra_module_is_reported(tmp_path, src_tree):
    entries = [*FULL, "codewiki/ghost.py"]
    problems = verify_wheel.verify(_make_wheel(tmp_path / "extra.whl", entries), src_tree)
    assert any("混入源码树没有的模块" in p for p in problems), problems


def test_missing_asset_is_reported(tmp_path, src_tree):
    entries = [n for n in FULL if not n.endswith("mermaid.min.js")]
    problems = verify_wheel.verify(_make_wheel(tmp_path / "noasset.whl", entries), src_tree)
    assert any("缺交付资产" in p for p in problems), problems


def test_deploy_copy_inside_wheel_is_reported(tmp_path, src_tree):
    entries = [*FULL, "codewiki-deploy/codewiki/core.py"]
    problems = verify_wheel.verify(_make_wheel(tmp_path / "leak.whl", entries), src_tree)
    assert any("混进了" in p for p in problems), problems


def test_bad_zip_is_reported_not_raised(tmp_path, src_tree):
    broken = tmp_path / "broken.whl"
    broken.write_bytes(b"not a zip")
    problems = verify_wheel.verify(broken, src_tree)
    assert problems and "不是合法 zip" in problems[0]


def test_missing_wheel_is_reported(tmp_path, src_tree):
    assert verify_wheel.verify(tmp_path / "nope.whl", src_tree)


@pytest.mark.source_tree
def test_real_repository_layout_passes(tmp_path):
    """拿当前仓库的真实源码清单造一个"完整" wheel，必须通过。

    这条防的是脚本与现实脱节：比如有人改了包目录名、或 .py 被挪到别处，
    上面几条用假树的用例照样绿，而真仓库永远过不了。
    """
    expected = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "codewiki").rglob("*.py"))
    assert len(expected) > 20, "源码树模块数异常，检查 src_root"
    entries = [*expected, *verify_wheel.REQUIRED_ASSETS]
    wheel = _make_wheel(tmp_path / "real.whl", entries)
    assert verify_wheel.verify(wheel, ROOT) == []
