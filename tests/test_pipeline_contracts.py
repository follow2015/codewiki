"""数据包与增量分析的**契约**（设计文稿 §数据包与增量分析）。

这一组用例针对的是"三条隐含约定散在 ``_analyze`` 里"的问题：

* 版本号到底以哪为准（``pyproject`` 1.0.0 vs 代码里 0.1.0）；
* 缓存键包含哪些维度（解析逻辑变了会不会命中旧缓存）；
* 解析结果本身是否被黄金文件锁住（输出变了而版本没动，谁能发现）。

约定写成可执行的断言后，改坏了当场红，不靠评审时有人记得。
"""
from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path

import pytest

import codewiki
from codewiki import __version__
from codewiki.analyzers import FORMAT_VERSIONS, REGISTRY, format_version_of, register_defaults
from codewiki.cli import main
from codewiki.file_cache import FileModelCache
from codewiki.render.jsonio import packet_from_parts
from codewiki.schemas import DataPacket, Meta
from tests.conftest import GOLDEN

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE = GOLDEN / "parser_sample.py"
FORMAT_GOLDEN = GOLDEN / "parser_format.json"


@pytest.mark.guarantee("single-version-source")
def test_version_has_exactly_one_source_of_truth():
    """``pyproject.toml`` 是唯一定义处；代码里不得再出现第二份字面量。"""
    declared = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert __version__ == declared["project"]["version"]
    assert codewiki.__version__ == __version__


def test_packet_defaults_never_disagree_with_the_package_version():
    """数据包自报的 ``tool_version`` 曾长期是 0.1.0，而包已经是 1.0.0 —— 归档物说谎。"""
    assert Meta(title="t", generated_at="2026-01-01T00:00:00Z").tool_version == __version__


def test_pipeline_writes_the_current_version_into_the_packet():
    packet = packet_from_parts(title="t", layers=[], nodes=[], edges=[])

    assert packet.meta.tool_version == __version__


def test_analyze_writes_the_current_version(tmp_path, mini_py):
    out = tmp_path / "wiki-output"
    assert main(["analyze", "--src", str(mini_py), "--out", str(out)]) == 0

    meta = json.loads((out / "data" / "codewiki.json").read_text(encoding="utf-8"))["meta"]
    assert meta["tool_version"] == __version__


def test_schema_version_is_independent_from_tool_version():
    """一个是数据格式契约，一个是工具版本 —— 混在一起会让"只升工具"变成破坏性变更。"""
    from codewiki.models import SCHEMA_VERSION

    assert SCHEMA_VERSION == 1
    assert __version__ != str(SCHEMA_VERSION)


def setup_module(module):  # noqa: ANN001 - pytest 钩子
    register_defaults()


def _digest(backend: str) -> str:
    model = REGISTRY.get("python", backend).parse(Path("parser_sample.py"), SAMPLE.read_text(encoding="utf-8"))
    payload = json.dumps(model.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_parser_output_matches_the_golden_digest():
    """输出变了而 FORMAT_VERSIONS 没动 ⇒ 历史缓存会把旧结果当成新结果。

    看到这条红，两条路选一条：**有意**改了解析行为 ⇒ 同时升 ``FORMAT_VERSIONS``
    与 ``tests/golden/parser_format.json``；**无意**改坏了 ⇒ 修代码。
    """
    golden = json.loads(FORMAT_GOLDEN.read_text(encoding="utf-8"))

    for backend, expected in golden.items():
        current = _digest(backend)
        assert current == expected["digest"], (
            f"{backend} 后端的解析输出变了，但 FORMAT_VERSIONS 仍是 {FORMAT_VERSIONS[backend]}。"
            f"确实要改就 +1 并更新 {FORMAT_GOLDEN.name}，否则修掉回归。"
        )


def test_golden_records_the_declared_format_version():
    golden = json.loads(FORMAT_GOLDEN.read_text(encoding="utf-8"))

    for backend, expected in golden.items():
        assert expected["format_version"] == FORMAT_VERSIONS[backend], (
            f"{backend}: 黄金文件记的是 v{expected['format_version']}，代码声明 v{FORMAT_VERSIONS[backend]}"
        )


def test_every_registered_backend_declares_a_format_version():
    """新增后端而不登记版本 ⇒ 它的缓存永远不受解析逻辑变化影响。"""
    register_defaults()
    registered = {backend for (_language, backend) in REGISTRY._factories}  # noqa: SLF001 - 契约测试
    missing = sorted(registered - set(FORMAT_VERSIONS))

    assert not missing, f"这些后端没有 FORMAT_VERSIONS 条目: {missing}"


def test_unknown_backend_gets_version_zero_instead_of_crashing():
    assert format_version_of("scip") == 0


@pytest.mark.guarantee("cache-key-has-format-version")
def test_cache_key_refuses_to_be_built_without_a_parser_version():
    """必需参数是防线：以后新的调用点漏传会当场 TypeError，而不是悄悄少一个维度。"""
    with pytest.raises(TypeError):
        FileModelCache.key("a.py", 1, "h", "ast")  # type: ignore[call-arg]


def test_bumping_the_format_version_invalidates_cache_entries(tmp_path):
    """增量分析的验收口径是"输出与全量一致"，前提是缓存不会跨解析逻辑复用。"""
    cache = FileModelCache(tmp_path)
    model = REGISTRY.get("python", "ast").parse(Path("a.py"), "x = 1\n")
    old_key = FileModelCache.key("a.py", 1, "h", "ast", parser_version=1)
    cache.set(old_key, model)

    new_key = FileModelCache.key("a.py", 1, "h", "ast", parser_version=2)

    assert cache.get(old_key) is not None
    assert cache.get(new_key) is None, "版本 +1 后必须整体未命中"


def test_packet_json_roundtrip_keeps_tool_version(tmp_path):
    packet = packet_from_parts(title="t", layers=[], nodes=[], edges=[])
    path = tmp_path / "p.json"
    path.write_text(json.dumps(packet.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8")

    loaded = DataPacket.model_validate_json(path.read_text(encoding="utf-8"))

    assert loaded.meta.tool_version == packet.meta.tool_version == __version__
