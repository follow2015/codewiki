"""可选依赖降级契约：treesitter 未安装时 register_defaults 必须静默跳过。

CI 实测回归（ubuntu + 纯运行时依赖）：TypeScriptAnalyzer 的
``import tree_sitter_typescript`` 在 ``__init__`` 内执行，此前 try/except
只护住模块导入 —— 构造函数抛 ImportError 直接炸掉整个 CLI。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codewiki.analyzers import base as analyzer_base

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_register_defaults_survives_missing_treesitter_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模拟未装 [treesitter] extra：构造期 ImportError ⇒ 跳过注册，不抛异常。"""

    def _boom(self) -> None:  # noqa: ANN001
        raise ImportError("No module named 'tree_sitter_typescript'")

    monkeypatch.setattr("codewiki.analyzers.ts_analyzer.TypeScriptAnalyzer.__init__", _boom)

    analyzer_base._register_optional_treesitter()


def test_register_defaults_still_registers_python_and_regex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """可选后端缺失不影响主链路：python/ast、python/regex、other/regex 照常可用。"""

    def _boom(self) -> None:  # noqa: ANN001
        raise ImportError("No module named 'tree_sitter_typescript'")

    monkeypatch.setattr("codewiki.analyzers.ts_analyzer.TypeScriptAnalyzer.__init__", _boom)

    fresh = analyzer_base.AnalyzerRegistry()
    monkeypatch.setattr(analyzer_base, "REGISTRY", fresh)
    analyzer_base.register_defaults()

    for language, backend in (("python", "ast"), ("python", "regex"), ("other", "regex")):
        analyzer = fresh.get(language, backend)
        assert analyzer is not None
    with pytest.raises(KeyError):
        fresh.get("typescript", "treesitter")


def test_build_registry_does_not_construct_optional_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注册阶段一旦构造后端，这里的 AssertionError 就会炸出来。"""

    def _boom(self) -> None:  # noqa: ANN001
        raise AssertionError("注册阶段不该构造可选后端")

    monkeypatch.setattr("codewiki.analyzers.ts_analyzer.TypeScriptAnalyzer.__init__", _boom)
    monkeypatch.setattr("codewiki.analyzers.go_analyzer.GoAnalyzer.__init__", _boom)

    registry = analyzer_base.build_registry()

    assert ("typescript", "treesitter") in registry._factories  # noqa: SLF001 - 契约测试


def test_build_registry_returns_an_isolated_instance() -> None:
    """两份注册表互不串味：往 A 里塞东西不该出现在 B。"""
    first = analyzer_base.build_registry()
    second = analyzer_base.build_registry()

    first.register("probe", "fake", lambda: None)

    assert first is not second
    assert ("probe", "fake") in first._factories  # noqa: SLF001 - 契约测试
    assert ("probe", "fake") not in second._factories  # noqa: SLF001 - 契约测试
    assert second is not analyzer_base.REGISTRY


def test_opt_out_keeps_treesitter_out_of_the_registry() -> None:
    """``allow_treesitter=False`` ⇒ 连工厂都不挂（要确定性降级的调用方用）。"""
    registry = analyzer_base.build_registry(allow_treesitter=False)

    assert {backend for (_lang, backend) in registry._factories} == {"ast", "regex"}  # noqa: SLF001


def test_ci_actually_runs_the_treesitter_suite() -> None:
    """装了 extra 的那台 runner 必须真跑后端用例，否则它只是一份"跳过的绿"。

    ``tests/test_treesitter_analyzers.py`` 是模块级 importorskip：其它 job 装
    ``.[dev]``，整个文件被静默跳过（CI 只显示 "1 skipped"，很容易被当成正常）。
    全仓 11 条用例因此长期没有任何流水线跑过。这条断言把"必须有一个 job 点名跑
    它"钉在配置上 —— 删掉那一步就红。

    ⚠️ 断言前必须**剥掉 YAML 注释**：本仓注释里到处引用测试文件名，不剥的话
    注释自己就能满足断言，删掉那一步依然全绿（实测变异存活过一轮）。
    """
    raw = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    code = "\n".join(line for line in raw.splitlines() if not line.lstrip().startswith("#"))

    assert "tests/test_treesitter_analyzers.py" in code, (
        "没有任何 job 点名跑 treesitter 用例；只靠全量 pytest 的话，未装 extra 的 "
        "job 会把整个文件跳过，等于没跑"
    )
