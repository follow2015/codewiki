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
import os
import tomllib
from pathlib import Path

import pytest

import codewiki
from codewiki import __version__
from codewiki.analyzers import FORMAT_VERSIONS, build_registry, format_version_of
from codewiki.cli import main
from codewiki.file_cache import FileModelCache
from codewiki.render.jsonio import packet_from_parts
from codewiki.schemas import DataPacket, Meta
from tests.conftest import GOLDEN

PROJECT_ROOT = Path(__file__).resolve().parent.parent
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


FORMAT_GOLDEN_DATA = json.loads(FORMAT_GOLDEN.read_text(encoding="utf-8"))
GOLDEN_BACKENDS: dict[str, dict[str, dict[str, object]]] = FORMAT_GOLDEN_DATA["backends"]
GOLDEN_SAMPLES: dict[str, str] = FORMAT_GOLDEN_DATA["samples"]


def _golden_cases() -> list:
    return [
        pytest.param(language, backend, id=f"{language}/{backend}")
        for language, backends in sorted(GOLDEN_BACKENDS.items())
        for backend in sorted(backends)
    ]


GOLDEN_CASES = _golden_cases()


@pytest.fixture(scope="module")
def registry():
    """一份**独立**注册表：不碰进程级 ``REGISTRY``，也不受别的用例污染。"""
    return build_registry()


def _parse_sample(registry, language: str, backend: str):
    factory = registry._factories.get((language, backend))  # noqa: SLF001 - 契约测试
    assert factory is not None, f"{language}/{backend} 没有注册 —— 黄金文件里有它，默认注册表就应当提供它"
    analyzer = factory()
    if analyzer is None:
        message = f"{language}/{backend} 需要可选依赖（[treesitter] extra），本环境未安装"
        if os.environ.get("CODEWIKI_REQUIRE_TREESITTER"):
            pytest.fail(f"{message}；本环境声明了 [treesitter] extra，黄金用例必须真跑而不是跳过")
        pytest.skip(message)
    sample = GOLDEN / GOLDEN_SAMPLES[language]
    return analyzer.parse(Path(sample.name), sample.read_text(encoding="utf-8"))


def _digest(model) -> str:
    payload = json.dumps(model.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("language,backend", GOLDEN_CASES)
def test_parser_output_matches_the_golden_digest(registry, language, backend):
    """输出变了而 FORMAT_VERSIONS 没动 ⇒ 历史缓存会把旧结果当成新结果。

    看到这条红，两条路选一条：**有意**改了解析行为 ⇒ 同时升 ``FORMAT_VERSIONS``
    与 ``tests/golden/parser_format.json``；**无意**改坏了 ⇒ 修代码。

    每种语言各有一份样本：只锁 Python 的话，TS/Go 后端改了输出而版本没动，
    缓存里那批文件会一直给出旧图谱，且没有任何用例会红。
    """
    expected = GOLDEN_BACKENDS[language][backend]
    model = _parse_sample(registry, language, backend)

    assert _digest(model) == expected["digest"], (
        f"{language}/{backend} 的解析输出变了，但 FORMAT_VERSIONS 仍是 {FORMAT_VERSIONS[backend]}。"
        f"确实要改就 +1 并更新 {FORMAT_GOLDEN.name}，否则修掉回归。"
    )


@pytest.mark.parametrize("language,backend", GOLDEN_CASES)
def test_parser_fact_counts_match_the_golden(registry, language, backend):
    """digest 只说"变了"，这条说"变在哪" —— 失败时不用自己去 diff 整份 FileModel。"""
    expected = GOLDEN_BACKENDS[language][backend]
    model = _parse_sample(registry, language, backend)

    assert {
        "imports": len(model.imports),
        "symbols": len(model.symbols),
        "calls": len(model.calls),
        "inherits": len(model.inherits),
    } == {
        "imports": expected["imports"],
        "symbols": expected["symbols"],
        "calls": expected["calls"],
        "inherits": expected["inherits"],
    }


@pytest.mark.parametrize("language,backend", GOLDEN_CASES)
def test_golden_records_the_declared_format_version(language, backend):
    expected = GOLDEN_BACKENDS[language][backend]

    assert expected["format_version"] == FORMAT_VERSIONS[backend], (
        f"{language}/{backend}: 黄金文件记的是 v{expected['format_version']}，"
        f"代码声明 v{FORMAT_VERSIONS[backend]}"
    )


def test_golden_samples_exist_for_every_locked_language():
    """黄金文件点名了样本却不存在 ⇒ 用例会 skip 成"通过"，等于把锁拆了。"""
    declared = {language for language in GOLDEN_BACKENDS}
    assert declared == set(GOLDEN_SAMPLES), (
        f"这些语言有黄金摘要但没登记样本: {sorted(declared - set(GOLDEN_SAMPLES))}"
    )
    missing = sorted(name for name in GOLDEN_SAMPLES.values() if not (GOLDEN / name).is_file())
    assert not missing, f"黄金样本文件不存在: {missing}"


def test_degraded_backend_declaration_names_the_regex_backend():
    """声明表被清空/删键时，参数化会退化成"零个用例"——静默通过，锁等于没有。"""
    declared = FORMAT_GOLDEN_DATA["must_not_emit_calls_or_inherits"]

    assert isinstance(declared, list) and declared, "降级后端声明不能为空"
    assert "regex" in declared, "regex 是正则降级后端，必须在这份声明里"
    unknown = sorted(set(declared) - set(FORMAT_VERSIONS))
    assert not unknown, f"声明里有未登记的后端: {unknown}"


@pytest.mark.parametrize("backend", FORMAT_GOLDEN_DATA["must_not_emit_calls_or_inherits"])
def test_degraded_backend_emits_no_calls_and_no_inherits(registry, backend):
    """正则后端是**降级路径**：只给 import 与符号，不声称调用与继承。

    它按行匹配，说不出"谁调用了谁"、也不认识继承语法。凭空产出这两类事实的
    后果不是"多一点信息"，而是图里多出一批无法追溯到语法的边 —— 与
    §0.3「宁缺毋滥」相反。这条断言把"不应该有"写成可执行声明。
    """
    languages = [lang for lang, backends in GOLDEN_BACKENDS.items() if backend in backends]
    assert languages, f"{backend} 被声明为降级后端，但黄金文件里没有任何它的记录"

    for language in languages:
        expected = GOLDEN_BACKENDS[language][backend]
        assert expected["calls"] == 0 and expected["inherits"] == 0, (
            f"黄金文件把 {language}/{backend} 记成了有调用或有继承，与降级声明冲突"
        )
        model = _parse_sample(registry, language, backend)
        assert model.calls == [], f"{language}/{backend} 不应产出调用: {model.calls}"
        assert model.inherits == [], f"{language}/{backend} 不应产出继承: {model.inherits}"


def test_every_registered_backend_declares_a_format_version():
    """新增后端而不登记版本 ⇒ 它的缓存永远不受解析逻辑变化影响。"""
    registered = {backend for (_language, backend) in build_registry()._factories}  # noqa: SLF001
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
    model = build_registry().get("python", "ast").parse(Path("a.py"), "x = 1\n")
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
