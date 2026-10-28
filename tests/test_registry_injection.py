"""注册表按轮注入，不再走进程级单例（设计文稿 §4.1 后端注册表）。

迁移前 ``pipeline/analyze.py`` 直接用进程级的 ``REGISTRY.get(...)``：那份单例被谁
注册过、被哪个用例污染过，在"跑完发现图谱不对"之前看不出来 —— 而"哪个后端解析了
这个文件"恰恰决定了产物长什么样。

现在每轮现造一份（``build_registry()``），请求对象里还能显式注入一份。三条断言
各挡一种走回头路的方式：

1. 模块里再出现 ``REGISTRY`` 这个**名字**（AST 判定 —— 注释里提到它不算）；
2. ``run()`` 忽略注入进来的注册表；
3. ``run()`` 偷偷回头读进程级单例。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from codewiki.analyzers import build_registry
from codewiki.pipeline.analyze import AnalyzeRequest, run

PIPELINE_SOURCE = Path(__file__).resolve().parent.parent / "codewiki" / "pipeline" / "analyze.py"


def _imported_names(tree: ast.AST) -> set[str]:
    """``import`` / ``from ... import`` 引入的名字（含 ``as`` 别名两侧）。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom | ast.Import):
            for alias in node.names:
                names.add(alias.name)
                names.add(alias.asname or alias.name)
    return names


def _loaded_names(tree: ast.AST) -> set[str]:
    """代码里真正被引用的标识符（注释、字符串、文档串都不算）。"""
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


@pytest.mark.source_tree
def test_pipeline_does_not_reach_for_the_process_registry() -> None:
    """跑到哪个后端是产物的一部分，不能取决于"进程里那份单例当时是什么状态"。

    用 AST 而不是文本包含：写一句"这里不再用 REGISTRY"的注释就能把文本断言满足了，
    而代码其实还在用。
    """
    tree = ast.parse(PIPELINE_SOURCE.read_text(encoding="utf-8"))

    assert "REGISTRY" not in _imported_names(tree), (
        "pipeline.analyze 又导入了进程级 REGISTRY —— 请改用 build_registry() 或注入"
    )
    assert "REGISTRY" not in _loaded_names(tree), (
        "pipeline.analyze 又引用了 REGISTRY 这个全局名（哪怕是从别处 import * 来的）"
    )


def test_analyze_request_defaults_to_no_shared_registry() -> None:
    """``None`` 是"每轮现造"的哨兵；写死成一个实例就等于又回到共享状态。"""
    request = AnalyzeRequest(src=".", out="out")

    assert request.registry is None, "默认值必须是 None（每轮 build_registry()），不是某个实例"


def test_run_uses_the_injected_registry(tmp_path, mini_py):
    """注入的注册表必须真的被用上 —— 否则这个字段只是个装饰。

    ``no_cache=True``：缓存命中时会直接吐出上一次的 FileModel，解析器根本不被调用
    （实测"命中 4 / 重新解析 0"），这条断言就会因为缓存而假红 / 假绿。
    """
    registry = build_registry()
    real = registry.get("python", "ast")

    class Recording:
        language = "python"

        def __init__(self) -> None:
            self.seen: list[str] = []

        def parse(self, path, content):
            self.seen.append(str(path))
            return real.parse(path, content)

    recording = Recording()
    registry.register("python", "ast", lambda: recording)

    out = tmp_path / "out"
    code = run(AnalyzeRequest(src=mini_py, out=out, timestamp="0", no_cache=True, registry=registry))

    assert code == 0
    assert recording.seen, "注入的注册表一次都没被调用，run() 用的是别的注册表"
    assert (out / "data" / "codewiki.json").is_file()


def test_run_ignores_the_process_registry(tmp_path, mini_py, monkeypatch):
    """把进程级单例换成一颗雷：run() 若不自己造表，当场炸给它看。"""
    from codewiki import analyzers

    def poisoned() -> None:
        raise AssertionError("run() 读了进程级 REGISTRY")

    monkeypatch.setitem(analyzers.REGISTRY._factories, ("python", "ast"), poisoned)  # noqa: SLF001

    out = tmp_path / "out"

    assert run(AnalyzeRequest(src=mini_py, out=out, timestamp="0", no_cache=True)) == 0
    assert (out / "data" / "codewiki.json").is_file()


def test_two_runs_do_not_share_a_registry(tmp_path, mini_py):
    """一轮里注册的后端不该泄漏到下一轮（两轮之间没有任何显式清理）。"""
    seen: list[int] = []

    first = build_registry()
    real = first.get("python", "ast")

    class Marked:
        language = "python"

        def parse(self, path, content):
            seen.append(1)
            return real.parse(path, content)

    first.register("python", "ast", lambda: Marked())
    assert (
        run(AnalyzeRequest(src=mini_py, out=tmp_path / "a", timestamp="0", no_cache=True, registry=first))
        == 0
    )
    assert seen, "第一轮就该命中注入的后端"

    seen.clear()
    assert run(AnalyzeRequest(src=mini_py, out=tmp_path / "b", timestamp="0", no_cache=True)) == 0
    assert seen == [], "第二轮默认注册表里不该有第一轮注册的 Marked"
