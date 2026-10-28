"""CLI 拆分的契约（设计文稿「CLI 拆成按子命令的模块」）。

拆分的失败模式不是"文件没拆开"，而是**拆完还留着旧入口**：
``cli/__init__.py`` 继续 re-export ``_analyze`` / ``render_all`` / ``REGISTRY``，
于是测试里 ``monkeypatch.setattr(cli_mod, "render_all", ...)`` 依然"成功"，
真正的调用却已经搬进了 ``pipeline.analyze`` —— 补丁打在影子上，用例静默失去
牙齿。这类假绿在迁移里最贵：它看起来是绿的，而且还"刚刚验证过"。

所以这里全部用 AST 判断"模块暴露了哪些名字"，不做文本包含（注释里出现一个
名字就能让文本断言通过，之前 CI 守卫踩过这个坑）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.source_tree

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLI_DIR = PROJECT_ROOT / "codewiki" / "cli"
CLI_LEGACY_MODULE = PROJECT_ROOT / "codewiki" / "cli.py"
PIPELINE_ANALYZE = PROJECT_ROOT / "codewiki" / "pipeline" / "analyze.py"

CLI_PUBLIC = frozenset({"main", "build_parser", "make_stdio_forgiving"})

_NOT_EXPORTS = frozenset({"annotations", "__all__"})

COMMAND_MODULES: tuple[tuple[str, str, str], ...] = (
    ("init", "init_cmd", "init"),
    ("analyze", "analyze_cmd", "analyze"),
    ("export", "export_cmd", "export"),
    ("doctor", "doctor_cmd", "doctor"),
    ("ai-annotate", "annotate_cmd", "annotate"),
    ("merge-annotations", "merge_annotations_cmd", "merge_annotations"),
    ("cycles", "cycles_cmd", "cycles"),
    ("diff", "diff_cmd", "diff"),
    ("suggest-layers", "suggest_layers_cmd", "suggest_layers"),
    ("merge-layers", "merge_layers_cmd", "merge_layers"),
    ("serve", "serve_cmd", "serve"),
    ("import-graph", "import_graph_cmd", "import_graph"),
    ("agent", "agent_cmd", "agent"),
)


def _module_level_names(path: Path) -> set[str]:
    """模块顶层导入了/定义了哪些名字（AST，不看文本）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom | ast.Import):
            for alias in node.names:
                if alias.name == "*":
                    names.add("*")
                    continue
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def _top_level_function_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.name for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}


def _wired_command_functions(parser_path: Path) -> dict[str, str]:
    """从 ``set_defaults(func=NAME)`` 反查"这个 func 从哪个模块 import 的"。

    只认 AST，所以注释里写一句 ``set_defaults(func=_analyze)`` 不会被当成接线。
    """
    tree = ast.parse(parser_path.read_text(encoding="utf-8"))
    imported: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imported[alias.asname or alias.name] = node.module
    wired: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "set_defaults":
            continue
        for keyword in node.keywords:
            if keyword.arg == "func" and isinstance(keyword.value, ast.Name):
                wired[keyword.value.id] = imported.get(keyword.value.id, "<未导入>")
    return wired


def test_cli_is_a_package_not_a_module() -> None:
    """拆完就不该再有 ``codewiki/cli.py``：两个入口并存时，导入哪个看运气。"""
    assert not CLI_LEGACY_MODULE.exists(), "旧的单体 cli.py 必须删掉，否则它是第二份真身"
    assert (CLI_DIR / "__init__.py").is_file(), "cli 应当是一个包"


def test_cli_entry_stays_within_150_lines() -> None:
    """设计文稿的验收标准：cli.py 不超过 150 行，只做参数解析与分发。"""
    entry = CLI_DIR / "__init__.py"
    if not entry.is_file():
        raise AssertionError("cli/__init__.py 尚未创建")
    lines = len(entry.read_text(encoding="utf-8").splitlines())
    assert lines <= 150, f"cli 入口 {lines} 行，超出 150 行的验收线（业务逻辑该在 pipeline 或子命令模块里）"


def test_cli_exposes_only_the_public_surface() -> None:
    """cli 只能暴露三个入口，其余都是"影子入口"。

    ``from codewiki.pipeline.analyze import _analyze`` 这类 re-export 会让
    ``monkeypatch.setattr(cli_mod, "render_all", ...)`` 依然成功，而真正的调用
    早已搬到 pipeline —— 补丁失效但测试全绿。
    """
    exported = _module_level_names(CLI_DIR / "__init__.py")
    leaked = {n for n in exported - CLI_PUBLIC - _NOT_EXPORTS if not n.startswith("_")}
    assert not leaked, (
        f"cli 暴露了 {sorted(leaked)}；除 {sorted(CLI_PUBLIC)} 外的符号都属于实现细节，"
        "留在自己的模块里，别在入口 re-export（会让测试补丁打在影子上）"
    )
    assert "*" not in exported, "cli 不该 `import *`：暴露面不可枚举就没法守"


def test_every_subcommand_has_its_own_module() -> None:
    missing = [mod for _, mod, _ in COMMAND_MODULES if not (CLI_DIR / f"{mod}.py").is_file()]
    assert not missing, f"缺子命令模块：{missing}（设计文稿：每个子命令一个文件）"


def test_each_command_module_defines_its_entry_function() -> None:
    for command, module, entry in COMMAND_MODULES:
        path = CLI_DIR / f"{module}.py"
        if not path.is_file():
            continue
        names = _top_level_function_names(path)
        assert entry in names, f"{module}.py 缺入口函数 {entry}()（子命令 {command}）"


def test_parser_wires_each_command_from_its_own_module() -> None:
    """参数解析层只做接线：**每个** ``func=`` 都必须来自对应的子命令模块。

    这条挡的是"拆了文件但 parser 还指向一个转发的壳"：那种壳会让真正实现
    与补丁点之间多一层，改动实现时测试不会红。
    """
    wired = _wired_command_functions(CLI_DIR / "parser.py")
    assert wired, "parser.py 里没有找到任何 set_defaults(func=...)"

    for command, module, entry in COMMAND_MODULES:
        assert entry in wired, f"子命令 {command} 没有接到 {module}.{entry}()"
        source = wired[entry]
        assert source.endswith(module), f"{entry} 来自 {source}，应当是 .{module}"
        if source == "<未导入>":
            raise AssertionError(f"{entry} 是 parser.py 里就地定义的，不在 {module} 模块里")


def test_patch_points_are_not_shadowed_by_a_local_copy() -> None:
    """补丁点必须是**同一个对象**，不是同名副本。

    测试通过 ``monkeypatch.setattr(codewiki.pipeline.analyze, "render_all", ...)``
    劫持出图中断路径。如果 pipeline 里写了个同名局部函数（或包了一层），补丁
    依然"成功"，但被调用的不是它 —— 中断用例永远不再触发。
    """
    from codewiki import analyzers
    from codewiki.pipeline import analyze as pipeline_analyze
    from codewiki.render import diagrams

    assert pipeline_analyze.render_all is diagrams.render_all, (
        "pipeline.analyze.render_all 不是 render.diagrams.render_all（被影子覆盖了）"
    )
    assert pipeline_analyze.build_registry is analyzers.build_registry, (
        "pipeline.analyze.build_registry 不是 analyzers.build_registry（被影子覆盖了）"
    )


def test_pipeline_entry_is_callable_without_argparse() -> None:
    """``pipeline.analyze.run`` 必须能脱离 argparse 单测（吃请求对象）。"""
    from codewiki.pipeline.analyze import AnalyzeRequest, run

    request = AnalyzeRequest(src=PROJECT_ROOT / "tests", out=PROJECT_ROOT / "wiki-output")
    assert request.backend == "auto", "默认后端应为 auto"
    assert callable(run)


def test_cli_runs_as_a_module() -> None:
    """``python -m codewiki.cli`` 仍要能跑（bench.py 与 CI 都是这个姿势）。"""
    assert (CLI_DIR / "__main__.py").is_file(), "包形式的 cli 需要 __main__.py，否则 -m 会报错"
