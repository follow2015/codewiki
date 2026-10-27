"""依赖方向闸（设计文稿 §AI 层安全模型 · 依赖方向用测试守住）。

规则不是"画一张好看的图"，而是**防止迁移期悄悄长出反向依赖**：重构一旦允许
``render`` 反向 import ``ai``，"AI 层安全边界"就只是文档上的一句话 —— 下次有人
在渲染层顺手调用 ``read_file``，Jail 与脱敏就绕过去了。

这里用 ``ast`` 扫全部源码的 import（不执行、不导入），按组比对白名单。白名单
等于**当前**的依赖图，新增一条越级依赖即失败；要放宽必须改这张表并在提交信息里
说明理由。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "codewiki"

ALLOWED: dict[str, frozenset[str]] = {
    "subproc": frozenset(),
    "guard": frozenset({"subproc"}),
    "core": frozenset({"subproc"}),
    "analyzers": frozenset({"core", "subproc"}),
    "graph": frozenset({"core", "subproc"}),
    "render": frozenset({"core", "subproc", "graph"}),
    "pipeline": frozenset({"core", "subproc", "analyzers", "graph", "render"}),
    "ai": frozenset({"core", "subproc", "guard", "render"}),
    "cli": frozenset({"*"}),
}


def _group_of_file(rel: Path) -> str:
    parts = rel.parts
    if rel.as_posix() == "subproc.py":
        return "subproc"
    if parts[0] == "ai":
        return "guard" if len(parts) > 1 and parts[1] == "guard" else "ai"
    if parts[0] in {"analyzers", "render", "graph", "pipeline"}:
        return parts[0]
    if parts[0] in {"cli", "cli.py", "doctor.py"}:
        return "cli"
    return "core"


def _group_of_module(name: str) -> str | None:
    """``codewiki.xxx`` → 组名；非本项目的模块返回 ``None``。"""
    parts = name.split(".")
    if parts[0] != "codewiki" or len(parts) == 1:
        return None
    if parts[1] == "subproc":
        return "subproc"
    if parts[1] == "ai":
        return "guard" if len(parts) > 2 and parts[2] == "guard" else "ai"
    if parts[1] in {"analyzers", "render", "graph", "pipeline"}:
        return parts[1]
    if parts[1] in {"cli", "doctor"}:
        return "cli"
    return "core"


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
        elif isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
    return names


def _collect() -> list[tuple[str, str, str, str]]:
    """返回 ``(源文件, 源组, 目标组, 目标模块)`` 的跨组依赖列表。"""
    edges: list[tuple[str, str, str, str]] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        rel = path.relative_to(PACKAGE_ROOT)
        source_group = _group_of_file(rel)
        for module in _imported_modules(path):
            target_group = _group_of_module(module)
            if target_group is None or target_group == source_group:
                continue
            edges.append((rel.as_posix(), source_group, target_group, module))
    return edges


def test_no_unexpected_cross_package_dependency():
    violations = [
        f"{path}: {source} → {target}（{module}）"
        for path, source, target, module in _collect()
        if "*" not in ALLOWED[source] and target not in ALLOWED[source]
    ]
    assert not violations, "出现反向/越级依赖：\n" + "\n".join(violations)


def test_guard_depends_only_on_stdlib_and_subproc():
    """``ai/guard`` 是最底层：它只允许标准库 + subproc，不允许任何业务模块。

    这条比 ALLOWED 更严 —— 白名单只写 ``subproc`` 一个组，但组名比对挡不住
    ``import requests`` 这类第三方依赖（那会让"安全边界"引入外部代码）。
    """
    stdlib = set(sys.stdlib_module_names)
    offenders: list[str] = []
    for path in sorted((PACKAGE_ROOT / "ai" / "guard").glob("*.py")):
        for module in _imported_modules(path):
            top = module.split(".")[0]
            if top == "codewiki":
                if module.startswith("codewiki.ai.guard"):
                    continue
                if _group_of_module(module) != "subproc":
                    offenders.append(f"{path.name}: {module}")
                continue
            if top == "__future__":
                continue
            if top not in stdlib:
                offenders.append(f"{path.name}: 非标准库 {module}")
    assert not offenders, "ai/guard 只允许标准库与 codewiki.subproc：\n" + "\n".join(offenders)


def test_subprocess_is_imported_only_in_subproc_module():
    """与 ruff 的 banned-api 互为双保险：即便有人给某个文件加了 per-file-ignore，
    这条也会红 —— 子进程的隔离参数只能有一处。"""
    offenders = [
        path.relative_to(PACKAGE_ROOT).as_posix()
        for path in PACKAGE_ROOT.rglob("*.py")
        if path.name != "subproc.py"
        and any(m == "subprocess" or m.startswith("subprocess.") for m in _imported_modules(path))
    ]
    assert not offenders, f"这些模块绕过了 codewiki/subproc.py：{offenders}"


def test_ai_layer_does_not_reach_into_cli():
    """``ai`` 不许 import ``cli``：那会让"库调用者"被迫带上 argparse 与终端输出。"""
    offenders = [
        f"{path}: {module}"
        for path, source, target, module in _collect()
        if source in {"ai", "guard", "render", "graph", "analyzers", "pipeline", "core"} and target == "cli"
    ]
    assert not offenders, "低层反向依赖 cli：\n" + "\n".join(offenders)


def test_graph_does_not_depend_on_render():
    """``graph`` 与 ``render`` 之间的环必须保持断开。

    这条与上一条白名单是重复的，但它值得单独存在：白名单是"允许清单"，
    将来有人为了让某条新依赖通过而放宽 ``ALLOWED``，环会静默复活；这条只问
    "依赖在不在"，放宽白名单也绕不过去。
    """
    offenders = [
        f"{path}: {module}"
        for path, source, target, module in _collect()
        if source == "graph" and target == "render"
    ]
    assert not offenders, "graph 又依赖 render 了（环会复活，渲染层改动会牵连导入层）：\n" + "\n".join(
        offenders
    )


def test_packet_constructor_has_a_single_home():
    """``packet_from_parts`` 只定义在 ``codewiki.packetio``。

    它是解开 ``graph → render`` 环的关键：构造器留在渲染层，导入适配器就只能
    反向依赖渲染。搬运时容易"两边都留一份"，那样环会以副本的形式回来，
    而白名单只扫 import、看不出来 —— 所以这里断言两者是同一个函数对象。
    """
    import codewiki.packetio as packetio
    import codewiki.render.jsonio as jsonio

    assert packetio.packet_from_parts.__module__ == "codewiki.packetio"
    assert jsonio.packet_from_parts is packetio.packet_from_parts, (
        "render.jsonio 里的 packet_from_parts 不再是 packetio 的那个 —— 可能被复制成了第二份实现"
    )
