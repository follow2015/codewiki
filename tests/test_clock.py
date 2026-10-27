"""可注入时钟（``SOURCE_DATE_EPOCH`` / ``--timestamp``）的契约测试。

"增量产物 == 全量产物逐字节一致"这条验收的前提就是它：时间戳每轮不同，字节比对
只能退化到"排除某个字段"，而排除式比对迟早漏掉真正该比的东西。
"""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import pytest

from codewiki import clock
from codewiki.cli import main
from codewiki.clock import ClockError

FIXED_EPOCH = "1767225600"
FIXED_ISO = "2026-01-01T00:00:00Z"


def test_source_date_epoch_is_honoured(monkeypatch):
    monkeypatch.setenv(clock.ENV_SOURCE_DATE_EPOCH, FIXED_EPOCH)
    assert clock.now_iso() == FIXED_ISO


def test_timestamp_overrides_environment(monkeypatch):
    """显式参数 > 环境变量：--timestamp 是操作者当场的意图。"""
    monkeypatch.setenv(clock.ENV_SOURCE_DATE_EPOCH, FIXED_EPOCH)
    assert clock.resolve("2026-05-06T07:08:09Z") == "2026-05-06T07:08:09Z"


def test_timestamp_accepts_seconds_and_iso():
    assert clock.resolve(FIXED_EPOCH) == FIXED_ISO
    assert clock.resolve("2026-01-01T00:00:00+00:00") == FIXED_ISO
    assert clock.resolve("2026-01-01T00:00:00.000Z") == FIXED_ISO


def test_naive_iso_is_treated_as_utc():
    assert clock.resolve("2026-01-01T00:00:00") == FIXED_ISO


@pytest.mark.parametrize("bad", ["昨天", "2026-13-45", "abc123"])
def test_invalid_timestamp_is_a_loud_error(bad):
    """非法值必须报错，**不能**静默退回真实时间 —— 那会让人以为产物可复现。"""
    with pytest.raises(ClockError):
        clock.resolve(bad)


def test_invalid_source_date_epoch_is_a_loud_error(monkeypatch):
    monkeypatch.setenv(clock.ENV_SOURCE_DATE_EPOCH, "not-a-number")
    with pytest.raises(ClockError, match="整数秒"):
        clock.now_iso()


def test_without_any_override_time_is_real(monkeypatch):
    monkeypatch.delenv(clock.ENV_SOURCE_DATE_EPOCH, raising=False)
    before = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=5)
    parsed = datetime.fromisoformat(clock.now_iso().replace("Z", "+00:00"))
    assert parsed >= before, "没有覆盖时应该取真实时间"
    assert parsed - before < timedelta(minutes=1)


def test_metadata_carries_the_injected_clock(monkeypatch):
    from codewiki.render.jsonio import packet_from_parts

    monkeypatch.setenv(clock.ENV_SOURCE_DATE_EPOCH, FIXED_EPOCH)
    packet = packet_from_parts(title="t", layers=[], nodes=[], edges=[])
    assert packet.meta.generated_at == FIXED_ISO


def test_analyze_records_the_fixed_timestamp(tmp_path, monkeypatch):
    """端到端：产物里的 generated_at 真的被固定（不是一个只在单测里生效的函数）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("X = 1\n", encoding="utf-8")
    monkeypatch.setenv(clock.ENV_SOURCE_DATE_EPOCH, FIXED_EPOCH)

    assert main(["analyze", "--src", str(repo), "--out", str(tmp_path / "out")]) == 0
    packet = json.loads((tmp_path / "out" / "data" / "codewiki.json").read_text(encoding="utf-8"))
    assert packet["meta"]["generated_at"] == FIXED_ISO


def test_analyze_rejects_a_bad_timestamp_before_doing_work(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("X = 1\n", encoding="utf-8")

    assert main(["analyze", "--src", str(repo), "--out", str(tmp_path / "out"), "--timestamp", "昨天"]) == 2
    assert "--timestamp 无法解析" in capsys.readouterr().err
    assert not (tmp_path / "out").exists(), "参数错误不该留下半份产物"


def test_two_runs_with_the_same_clock_produce_identical_bytes(tmp_path, monkeypatch):
    """同一输入 + 同一时钟 ⇒ 逐字节相同。这是后面八种变更验收的地基。"""
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "a.py").write_text("from pkg.b import B\n\n\ndef f():\n    return B\n", encoding="utf-8")
    (repo / "pkg" / "b.py").write_text("B = 1\n", encoding="utf-8")
    monkeypatch.setenv(clock.ENV_SOURCE_DATE_EPOCH, FIXED_EPOCH)

    assert main(["analyze", "--src", str(repo), "--out", str(tmp_path / "o1")]) == 0
    time.sleep(0.01)
    assert main(["analyze", "--src", str(repo), "--out", str(tmp_path / "o2")]) == 0

    def snapshot(root):
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}

    assert snapshot(tmp_path / "o1") == snapshot(tmp_path / "o2")
