"""数据包读取入口（``load_packet``）的契约测试。

设计文稿要求的是三件事，缺一件"版本"就只是个躺着的数字：

- 版本**更新**：明确报错，不降级解析（旧版工具读新包会静默丢字段）；
- 版本**更旧**：走 ``migrate_n_to_n+1`` 链，逐级推进版本号；
- 结构不符 / 坏 JSON：给可读原因，调用方转退出码 2。
"""

from __future__ import annotations

import json

import pytest

from codewiki.cli import main
from codewiki.models import SCHEMA_VERSION
from codewiki.packetio import (
    MIGRATIONS,
    PacketError,
    PacketVersionError,
    load_packet,
    load_packet_or_none,
    migrate_packet,
)
from codewiki.render.jsonio import packet_from_parts, write_packet


def _packet_dict(**meta_override) -> dict:
    packet = packet_from_parts(title="t", layers=[], nodes=[], edges=[])
    data = packet.model_dump(mode="json")
    data["meta"].update(meta_override)
    return data


def _write(tmp_path, data: dict):
    path = tmp_path / "codewiki.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_roundtrip_of_a_freshly_written_packet(tmp_path):
    target = write_packet(packet_from_parts(title="t", layers=[], nodes=[], edges=[]), tmp_path)
    packet = load_packet(target)
    assert packet.meta.title == "t"
    assert packet.meta.schema_version == SCHEMA_VERSION


def test_bad_json_is_reported_with_reason(tmp_path):
    path = tmp_path / "codewiki.json"
    path.write_text("{不是 json", encoding="utf-8")
    with pytest.raises(PacketError, match="不是合法 JSON"):
        load_packet(path)


def test_missing_meta_is_rejected(tmp_path):
    path = _write(tmp_path, {"nodes": [], "edges": []})
    with pytest.raises(PacketError, match="缺少 meta"):
        load_packet(path)


def test_missing_schema_version_is_rejected(tmp_path):
    """本工具写的包一定带这一项；缺失说明来源不明，不能猜成"当前版本"。"""
    data = _packet_dict()
    del data["meta"]["schema_version"]
    with pytest.raises(PacketError, match="schema_version"):
        load_packet(_write(tmp_path, data))


def test_newer_schema_is_a_loud_error_not_a_silent_downgrade(tmp_path):
    data = _packet_dict(schema_version=SCHEMA_VERSION + 1)
    with pytest.raises(PacketVersionError, match="比当前工具支持"):
        load_packet(_write(tmp_path, data))


def test_older_schema_without_migration_is_rejected(tmp_path):
    assert SCHEMA_VERSION not in MIGRATIONS
    data = _packet_dict(schema_version=0)
    with pytest.raises(PacketVersionError, match="缺少 0 → 1"):
        load_packet(_write(tmp_path, data))


def test_older_packet_is_migrated_and_version_advanced(tmp_path, monkeypatch):
    """迁移函数只改内容；版本号由 :func:`migrate_packet` 推进（漏推会死循环）。"""
    data = _packet_dict(schema_version=0)
    calls: list[int] = []

    def _from_0_to_1(raw: dict) -> dict:
        calls.append(raw["meta"]["schema_version"])
        raw["diagnostics"].append({"level": "info", "code": "MIGRATED", "message": "来自 schema 0"})
        return raw

    monkeypatch.setitem(MIGRATIONS, 0, _from_0_to_1)
    packet = load_packet(_write(tmp_path, data))

    assert calls == [0]
    assert packet.meta.schema_version == SCHEMA_VERSION
    assert any(d.code == "MIGRATED" for d in packet.diagnostics)


def test_migration_chain_runs_step_by_step(tmp_path, monkeypatch):
    """多级迁移必须逐级调用，不能"直接跳到最新"。"""
    seen: list[int] = []

    def _step(version: int):
        def _migrate(raw: dict) -> dict:
            seen.append(version)
            return raw

        return _migrate

    monkeypatch.setitem(MIGRATIONS, 0, _step(0))
    monkeypatch.setitem(MIGRATIONS, 1, _step(1))
    monkeypatch.setattr("codewiki.packetio.SCHEMA_VERSION", 2)

    raw = migrate_packet(_packet_dict(schema_version=0), 0)
    assert seen == [0, 1]
    assert raw["meta"]["schema_version"] == 2


def test_structural_mismatch_is_reported(tmp_path):
    data = _packet_dict()
    data["nodes"] = "这不是数组"
    with pytest.raises(PacketError, match="结构不符"):
        load_packet(_write(tmp_path, data))


def test_or_none_swallows_packet_errors(tmp_path):
    assert load_packet_or_none(tmp_path / "missing.json") is None
    path = tmp_path / "codewiki.json"
    path.write_text("{", encoding="utf-8")
    assert load_packet_or_none(path) is None


def test_cli_turns_a_too_new_packet_into_exit_code_2(tmp_path, capsys):
    """端到端：用户拿到的是"请升级工具"，不是一屏 pydantic 堆栈。"""
    data = _packet_dict(schema_version=SCHEMA_VERSION + 1)
    path = _write(tmp_path, data)

    assert main(["cycles", "--input", str(path)]) == 2
    err = capsys.readouterr().err
    assert "schema 版本" in err and "升级" in err
