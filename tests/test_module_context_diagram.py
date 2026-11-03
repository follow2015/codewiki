"""模块上下文图的可读性闸门（图表优化 P0-1 / P0-2）。

pydantic 实测：``json_schema`` 的模块图渲染成 1600×8622（约 5 屏高）。原因是图里
**没有规模闸门**——一个模块把自己的全部公开符号（≈150 个）画成一排叶子，外部依赖
（标准库 + 第三方）与仓内依赖等权逐条连线。

这里钉两件事：

1. **外部依赖分组**：标准库合成一个节点、第三方合成一个节点，仓内依赖照常逐个画
   （否则"我依赖谁"这个问题就被折叠没了）；
2. **符号叶子封顶**：默认 12 个，其余折成一个"另有 N 个符号"节点 —— 数字必须
   显式写出来，不能悄悄截断（读者要知道自己没看到什么）。
"""

from __future__ import annotations

from codewiki.graph import build_graph
from codewiki.graph.builder import module_id
from codewiki.models import Edge, EdgeType, FileModel, ImportFact, Node, NodeType, SymbolFact
from codewiki.packetio import packet_from_parts
from codewiki.render.mermaid_context import (
    DEFAULT_MAX_NEIGHBORS,
    DEFAULT_MAX_SYMBOLS,
    MIN_VISIBLE_SYMBOLS,
    render_module_context,
)


def _fm(path, *, imports=(), symbols=()):
    return FileModel(
        path=path,
        language="python",
        hash=path,
        imports=[
            ImportFact(module=m, name=n, lineno=ln, kind="import" if n is None else "from")
            for m, n, ln in imports
        ],
        symbols=[
            SymbolFact(name=n, qualname=q, kind="function", lineno=ln, endlineno=ln + 1)
            for n, q, ln in symbols
        ],
        calls=[],
    )


def _packet(files):
    result = build_graph(files)
    return packet_from_parts(title="t", layers=[], nodes=result.nodes, edges=result.edges)


def test_external_dependencies_are_grouped_by_stdlib_and_third_party():
    """标准库 / 第三方各合成一个节点；仓内依赖仍逐个画。"""
    files = [
        _fm(
            "a.py",
            imports=[
                ("os", None, 1),
                ("typing", None, 2),
                ("pydantic_core", "X", 3),
                (".b", "f", 4),
            ],
        ),
        _fm("b.py", symbols=[("f", "f", 1)]),
    ]
    text = render_module_context(_packet(files), module_id("a.py"))

    stdlib_line = next((ln for ln in text.splitlines() if "XD_STD" in ln), "")
    third_line = next((ln for ln in text.splitlines() if "XD_3P" in ln), "")
    assert "标准库" in stdlib_line and "2" in stdlib_line, f"两个标准库依赖应合并成一个节点:\n{text}"
    assert "第三方" in third_line and "1" in third_line, f"第三方依赖应单独成组:\n{text}"
    assert "#40;2#41;" in stdlib_line
    assert '"os"' not in text and '"typing"' not in text and '"pydantic_core"' not in text
    assert "b.py" in text, f"仓内依赖被误合并:\n{text}"


def test_symbol_leaves_are_capped_and_the_rest_is_folded():
    """符号叶子默认封顶 12 个，其余折成一个带计数的节点。"""
    symbols = [(f"s{i}", f"s{i}", i + 1) for i in range(30)]
    text = render_module_context(_packet([_fm("big.py", symbols=symbols)]), module_id("big.py"))

    contain_edges = [ln for ln in text.splitlines() if "M --- " in ln]
    assert len(contain_edges) == 13, (
        f"应为 12 个符号 + 1 个折叠节点，实际 {len(contain_edges)} 条包含边:\n{text}"
    )
    assert "另有 18 个符号" in text, f"折叠数量必须显式写出（30-12=18）:\n{text}"


def test_symbol_cap_is_configurable():
    """上限可配：小图想要全量时能关掉折叠。

    ``max_symbols=None`` 是"别折"的开关；传具体数字仍受右列行数预算约束
    （符号和下游挤在同一列，各自封顶管不住总高度）。
    """
    symbols = [(f"s{i}", f"s{i}", i + 1) for i in range(30)]
    packet = _packet([_fm("big.py", symbols=symbols)])

    text = render_module_context(packet, module_id("big.py"), max_symbols=None)
    assert text.count("M --- ") == 30
    assert "另有" not in text


def test_grouped_external_edges_keep_direction():
    """分组不改变方向语义：下游组画成 M → 组（外部依赖只会出现在下游侧，
    因为"以我为目标"的边不可能来自外部模块）。"""
    files = [
        _fm("up.py", imports=[(".mid", "f", 1)]),
        _fm("mid.py", imports=[("os", None, 1), (".down", "g", 2)]),
        _fm("down.py", symbols=[("g", "g", 1)]),
    ]
    text = render_module_context(_packet(files), module_id("mid.py"))

    assert any(ln.startswith("  M -->") and "XD_STD" in ln for ln in text.splitlines()), text
    assert any(ln.startswith("  U0[") for ln in text.splitlines()), text


def test_type_only_external_group_is_dashed():
    """整组都是 ``if TYPE_CHECKING`` 依赖时用虚线 —— 与运行时依赖区分开。"""
    files = [
        _fm("a.py", imports=[("typing", None, 1)]),
        _fm("b.py"),
    ]
    files[0].imports[0].type_only = True
    packet = _packet(files)

    text = render_module_context(packet, module_id("a.py"))
    assert "-.->" in text, f"仅类型的外部依赖组应为虚线:\n{text}"


def _fan(*, up: int = 0, down: int = 0, symbols: int = 0):
    """``hub.py`` 的上下文：``up`` 个模块依赖它、它依赖 ``down`` 个、提供 ``symbols`` 个符号。"""
    hub = module_id("hub.py")
    nodes = [Node(id=hub, type=NodeType.MODULE, path="hub.py")]
    edges = []
    for index in range(up):
        other = module_id(f"u{index}.py")
        nodes.append(Node(id=other, type=NodeType.MODULE, path=f"u{index}.py"))
        edges.append(Edge(source=other, target=hub, type=EdgeType.IMPORTS))
    for index in range(down):
        other = module_id(f"d{index}.py")
        nodes.append(Node(id=other, type=NodeType.MODULE, path=f"d{index}.py"))
        edges.append(Edge(source=hub, target=other, type=EdgeType.IMPORTS))
    for index in range(symbols):
        nodes.append(
            Node(
                id=f"sym::hub.py::s{index}",
                type=NodeType.SYMBOL,
                path="hub.py",
                name=f"s{index}",
                module_id=hub,
                line_start=index + 1,
            )
        )
    return packet_from_parts(title="t", layers=[], nodes=nodes, edges=edges)


def test_downstream_neighbours_are_capped_with_a_fold_note():
    """下游超过上限时折叠，并写明还有多少个 —— 折叠是必要的，藏起来就是误导。"""
    text = render_module_context(_fan(down=DEFAULT_MAX_NEIGHBORS + 20), module_id("hub.py"))

    shown = sum(1 for ln in text.splitlines() if ln.startswith("  M -->"))
    assert shown == DEFAULT_MAX_NEIGHBORS, f"下游应封顶在 {DEFAULT_MAX_NEIGHBORS}:\n{text[:500]}"
    assert "另有 20 个下游模块" in text, f"折叠数量必须显式写出:\n{text[-300:]}"


def test_upstream_neighbours_are_capped_with_a_fold_note():
    """上游同理：一侧 98 个模块就是几屏高。"""
    text = render_module_context(_fan(up=DEFAULT_MAX_NEIGHBORS + 5), module_id("hub.py"))

    shown = sum(
        1 for ln in text.splitlines() if ln.rstrip().endswith(" M") or "--> M" in ln or "-.-> M" in ln
    )
    assert shown == DEFAULT_MAX_NEIGHBORS, f"上游应封顶在 {DEFAULT_MAX_NEIGHBORS}:\n{text[:500]}"
    assert "另有 5 个上游模块" in text


def test_symbol_budget_shrinks_when_the_right_column_is_busy():
    """符号与下游挤在同一列：下游吃满预算后，符号让位但至少留 ``MIN_VISIBLE_SYMBOLS`` 个。

    pydantic `main.py` 实测：15 个下游 + 2 个外部组 + 12 个符号 + 2 条折叠说明 = 31 行、
    2688px。按行数打包后同一张图 1796px。
    """
    text = render_module_context(_fan(down=DEFAULT_MAX_NEIGHBORS + 20, symbols=30), module_id("hub.py"))

    symbol_nodes = [ln for ln in text.splitlines() if ln.endswith(")") and ln.strip().startswith("n")]
    assert MIN_VISIBLE_SYMBOLS <= len(symbol_nodes) < DEFAULT_MAX_SYMBOLS, (
        f"右列被下游占满后符号应让位到 [{MIN_VISIBLE_SYMBOLS}, {DEFAULT_MAX_SYMBOLS})，实际 {len(symbol_nodes)}:\n{text}"
    )
    assert "另有" in text and "个符号" in text, "让位掉的符号必须报数量"


def test_symbols_keep_a_floor_even_when_the_column_is_over_budget(monkeypatch):
    """预算被吃光时符号仍留 ``MIN_VISIBLE_SYMBOLS`` 个 —— "我能提供什么"是这张图的一半。

    当前常量下预算最小也有 5，所以这条护栏平时不触发；这里把列预算压到 10 逼它触发，
    否则它只是段没人走过的死代码。
    """
    from codewiki.render import mermaid_context

    monkeypatch.setattr(mermaid_context, "MAX_COLUMN_BOXES", 10)
    text = render_module_context(_fan(down=DEFAULT_MAX_NEIGHBORS + 5, symbols=30), module_id("hub.py"))

    symbol_nodes = [ln for ln in text.splitlines() if ln.endswith(")") and ln.strip().startswith("n")]
    assert len(symbol_nodes) == MIN_VISIBLE_SYMBOLS, f"预算超支时符号要保底:\n{text}"


def test_small_context_is_not_folded():
    """小图不折叠：3 个上游、2 个下游、5 个符号本来就是一屏。"""
    text = render_module_context(_fan(up=3, down=2, symbols=5), module_id("hub.py"))

    assert "另有" not in text, f"小图不该出现折叠说明:\n{text}"


__all__: list[str] = []
