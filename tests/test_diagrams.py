"""出图判据（§4.2 / §4.3 / §4.5）。

钉四件事：

1. **宏观图 golden 逐字节**：产物形状变了（节点顺序、边标签、ID 规则）当场红 ——
   图纸是给人看的，形状漂移比"图能渲染"更要紧；
2. **上下文图方向**：上游在左、自身居中、下游在右；
3. **校验门中断**：静态图坏 ⇒ 一张都不落盘（§4.3：坏图绝不产出 .mmd）；
4. **置信度过滤**：低于阈值的边不进图。
"""

from __future__ import annotations

from codewiki.graph import build_graph
from codewiki.models import Edge, EdgeType
from codewiki.render.diagrams import render_all
from codewiki.render.jsonio import packet_from_parts
from codewiki.render.mermaid_context import render_module_context
from codewiki.render.mermaid_macro import render_system_context
from tests.conftest import GOLDEN, parse_all

GOLDEN_MACRO = GOLDEN / "system_context.mmd"


def _packet(mini_py, *, layers=None, edges=None):
    result = build_graph(parse_all(mini_py), layers=layers)
    return packet_from_parts(
        title="mini",
        layers=layers or [],
        nodes=result.nodes,
        edges=result.edges if edges is None else edges,
    )


def test_system_context_matches_golden_byte_for_byte(mini_py):
    text = render_system_context(_packet(mini_py))

    if not GOLDEN_MACRO.exists():
        GOLDEN_MACRO.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_MACRO.write_text(text, encoding="utf-8")

    assert text == GOLDEN_MACRO.read_text(encoding="utf-8"), (
        "宏观图形状漂移：golden 是冻结版本，形状变更必须同批改 golden 并说明原因"
    )


def test_pagination_removes_stale_single_macro_diagram(mini_py, tmp_path):
    """宏图从「单张」转「分包」后，旧主图必须清掉。

    否则首页取图优先读到陈旧巨图（IPIP 实测：107KB 旧主图 shadow 了新
    分包，mermaid 超 maxTextSize 整图报错）。
    """
    from codewiki.render.diagrams import SYSTEM_DIAGRAM

    packet = _packet(mini_py)
    render_all(packet, tmp_path, max_nodes=10_000)
    assert (tmp_path / "diagrams" / SYSTEM_DIAGRAM).exists()

    result = render_all(packet, tmp_path, max_nodes=0)

    diagrams = tmp_path / "diagrams"
    assert not (diagrams / SYSTEM_DIAGRAM).exists(), "分包后旧主图必须删除，不许 shadow 新分包"
    assert list(diagrams.glob("system-context-part-*.mmd"))
    assert not any(d.level == "error" for d in result.diagnostics)


def test_single_macro_diagram_removes_stale_paged_files(mini_py, tmp_path):
    """反向同理：口径放宽回到单张后，旧分包与索引页不许残留。"""
    from codewiki.render.diagrams import SYSTEM_DIAGRAM, SYSTEM_INDEX

    packet = _packet(mini_py)
    render_all(packet, tmp_path, max_nodes=0)
    assert list((tmp_path / "diagrams").glob("system-context-part-*.mmd"))

    render_all(packet, tmp_path, max_nodes=10_000)

    diagrams = tmp_path / "diagrams"
    assert (diagrams / SYSTEM_DIAGRAM).exists()
    assert not list(diagrams.glob("system-context-part-*.mmd")), "回到单张后旧分包必须删除"
    assert not (diagrams / SYSTEM_INDEX).exists(), "回到单张后旧分包索引必须删除"


def test_module_context_puts_upstream_left_and_downstream_right(mini_py):
    packet = _packet(mini_py)
    text = render_module_context(packet, "mod::pkg/core.py")

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    assert lines[0] == "flowchart LR"
    upstream_declared = [i for i, ln in enumerate(lines) if ln.startswith("U0[")]
    center_declared = [i for i, ln in enumerate(lines) if ln.startswith("M[")]
    assert upstream_declared and center_declared
    assert upstream_declared[0] < center_declared[0]


def test_low_confidence_edges_are_not_rendered(mini_py):
    packet = _packet(mini_py)
    weak = Edge(source="mod::app.py", target="mod::pkg/util.py", type=EdgeType.IMPORTS, confidence=0.4)
    packet.edges.append(weak)

    text = render_system_context(packet, min_confidence=0.5)

    assert "mod::pkg/util.py" not in text
    assert render_system_context(packet, min_confidence=0.3).count("-->") >= text.count("-->")


def test_invalid_static_diagram_aborts_without_writing_files(mini_py, tmp_path, monkeypatch):
    """静态图坏 = 工具缺陷 ⇒ 中断整轮，一张 .mmd 都不落盘（§4.3）。"""
    import codewiki.render.diagrams as diagrams

    monkeypatch.setattr(diagrams, "render_system_context", lambda *a, **k: 'flowchart TD\n  A["未闭合\n')
    result = render_all(_packet(mini_py), tmp_path)

    assert any(d.level == "error" and d.code == "DIAGRAM_INVALID" for d in result.diagnostics)
    assert result.files == [], "坏图绝不产出文件"
    assert not list(tmp_path.rglob("*.mmd"))


def test_valid_run_writes_system_and_module_diagrams(mini_py, tmp_path):
    result = render_all(_packet(mini_py), tmp_path)

    names = sorted(p.name for p in result.files)
    assert "system-context.mmd" in names
    assert any(n.startswith("pkg_core") for n in names)
    assert not [d for d in result.diagnostics if d.level == "error"]


def test_cross_layer_module_imports_are_not_drawn_in_system_context():
    """跨层模块边不画，只留 LAYER_DEPENDS 聚合边。

    照画会让两个 subgraph 之间多出几十条直连，Mermaid 的 dagre 失去"层间上下
    分层"的依据，整张图塌成一条横向长带（IPIP 实测 5588×133 px）：层是分对了，
    但图上完全看不出来。
    """
    from codewiki.models import Node, NodeType
    from codewiki.render.mermaid_macro import node_id
    from codewiki.schemas import LayerNode

    a1, a2, s1 = "mod::adapters/a.py", "mod::adapters/b.py", "mod::svc/s.py"
    nodes = [
        Node(id=a1, type=NodeType.MODULE, path="adapters/a.py", layer_id="layer::A"),
        Node(id=a2, type=NodeType.MODULE, path="adapters/b.py", layer_id="layer::A"),
        Node(id=s1, type=NodeType.MODULE, path="svc/s.py", layer_id="layer::B"),
    ]
    edges = [
        Edge(source=a1, target=a2, type=EdgeType.IMPORTS),
        Edge(source=a1, target=s1, type=EdgeType.IMPORTS),
        Edge(source="layer::A", target="layer::B", type=EdgeType.LAYER_DEPENDS, weight=3),
    ]
    packet = packet_from_parts(
        title="t",
        layers=[LayerNode(id="layer::A", name="适配层"), LayerNode(id="layer::B", name="服务层")],
        nodes=nodes,
        edges=edges,
    )

    text = render_system_context(packet)

    assert f"{node_id(a1)} --> {node_id(a2)}" in text, "同层边必须保留"
    assert f"{node_id(a1)} --> {node_id(s1)}" not in text, "跨层模块边重复表达，必须由聚合边代替"
    assert f"{node_id('layer::A')} -->|3| {node_id('layer::B')}" in text, "层间聚合边（带次数）必须在"


def test_unlayered_modules_appear_in_system_context():
    """没归类的模块必须在图上出现 —— 不能"层没配到就当它不存在"。

    此前只遍历 packet.layers 生成 subgraph，`_unlayered` 不在其中 ⇒ 这些模块
    连节点都不定义，而索引页照样列出全部模块（实测索引 3 个 / 主图 1 个）。
    """
    from codewiki.layering import UNLAYERED_LAYER_ID
    from codewiki.models import Node, NodeType
    from codewiki.render.i18n import t
    from codewiki.render.mermaid_macro import UNLAYERED_LABEL, node_id
    from codewiki.schemas import LayerNode

    a1 = "mod::adapters/a.py"
    loose = "mod::loose.py"
    nodes = [
        Node(id=a1, type=NodeType.MODULE, path="adapters/a.py", layer_id="layer::A"),
        Node(id=loose, type=NodeType.MODULE, path="loose.py", layer_id=UNLAYERED_LAYER_ID),
    ]
    packet = packet_from_parts(
        title="t",
        layers=[LayerNode(id="layer::A", name="适配层")],
        nodes=nodes,
        edges=[],
    )

    text = render_system_context(packet)

    assert t(UNLAYERED_LABEL) in text, "要有未分层兜底组"
    assert f'{node_id(loose)}["loose.py"]' in text, "未分层模块必须出现在图上"


def test_slug_strips_attribute_breaking_characters():
    """slug 同时是页面名与 href 片段，不能带引号/尖括号（第一道防线）。

    `site.py` 渲染时还会再 `html.escape`（纵深防御），但页面名本身也得干净 ——
    文件名里出现 `"` 在 Linux 上是合法的，而被分析的仓库不是我们自己的。
    """
    from codewiki.render.diagrams import _slug

    slug = _slug('x"><img src=x onerror=alert(1)>.py')
    assert '"' not in slug and "<" not in slug and ">" not in slug, slug
    assert _slug("服务/log.py") == "服务_log"
