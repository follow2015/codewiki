"""check-diagram-size.py 的单测（P2-9）。

量测本身需要浏览器，只在真实产物上手测；这里盯的是纯逻辑：
超限判定、失败结果不计入、阈值是「超过」而非「达到」。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check-diagram-size.py"
spec = importlib.util.spec_from_file_location("check_diagram_size", SCRIPT)
mod = importlib.util.module_from_spec(spec)
sys.modules["check_diagram_size"] = mod
spec.loader.exec_module(mod)


def test_height_above_limit_is_reported():
    rows = [(Path("a.mmd"), 800, 4001), (Path("b.mmd"), 800, 1200)]
    large = mod.collect_too_large(rows, limit=4000)
    assert [p for p, _, _ in large] == [Path("a.mmd")]


def test_height_equal_to_limit_is_not_reported():
    """阈值是「超过」：正好 4000px 不告警。"""
    rows = [(Path("a.mmd"), 800, 4000)]
    assert mod.collect_too_large(rows, limit=4000) == []


def test_failed_measurements_are_skipped_not_counted():
    rows = [(Path("a.mmd"), 800, 9000), "渲染超时（180s）"]
    large = mod.collect_too_large(rows, limit=4000)
    assert len(large) == 1


def test_default_limit_is_4000():
    assert mod.DEFAULT_LIMIT == 4000


def test_cli_help_mentions_diagram_too_large():
    """--help 要说清楚这事是干嘛的（CI 里只有报错可看）。"""
    import subprocess

    proc = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0
    assert "DIAGRAM_TOO_LARGE" in proc.stdout


__all__: list[str] = []
