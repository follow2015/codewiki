"""WP9：交互图页面（``graph.html``）。

计划给的验收是"pydantic 与 ipip 首屏可交互 < 1.5 s；点节点看到的出入边与
``LAYER_DEPENDS`` 一致"。前者没法在 pytest 里量浏览器，只能在真实仓库上人工
验（提交说明里记数字）；后者可以，而且它比字面意思重要得多：

**"一致"不是指"两边碰巧相等"，而是指"JS 不许自己再算一遍"。** 交互页里的层间
条数、层环全部由 Python 侧算好塞进 JSON，JS 只做渲染和过滤。否则第一个 bug 就是
"首页写 82、交互图写 52"，而这个仓在 :func:`codewiki.render.layer_map._layer_pairs`
的注释里已经为同一类问题踩过一次 —— 那时是系统图与首页各算一遍。

所以这里的核心用例是 ``test_layer_edges_match_layer_depends``：从**渲染出的 HTML
文本**里把内联 JSON 抠出来，与 ``LAYER_DEPENDS`` 的聚合结果逐项比对。抠文本而不是
调函数，是为了测到"数字真的进了产物"这一步 —— 只调函数的话，渲染器写错键名也绿。
"""

from __future__ import annotations

import json
import re

from codewiki.models import CONF_STATIC, Edge, EdgeType, Node, NodeType
from codewiki.packetio import packet_from_parts
from codewiki.render.graph_page import (
    GRAPH_HTML,
    render_graph_html,
    write_graph_html,
)

_DATA = re.compile(r'<script id="graph-data" type="application/json">(.*?)</script>', re.DOTALL)


def _payload(html: str) -> dict:
    match = _DATA.search(html)
    assert match, "页面里没有找到内联数据块（script#graph-data）"
    return json.loads(match.group(1))


def _packet(*, evil: bool = False):
    """三层小仓：api → svc → models，外加一条 api → models 的越层直连。"""
    api_name = "</script><img src=x onerror=alert(1)>" if evil else "api"
    layers = [
        {"id": "layer::api", "name": api_name, "order": 1},
        {"id": "layer::svc", "name": "svc", "order": 2},
        {"id": "layer::models", "name": "models", "order": 3},
    ]
    nodes = [
        Node(id="mod::api/a.py", type=NodeType.MODULE, path="api/a.py", layer_id="layer::api"),
        Node(id="mod::api/b.py", type=NodeType.MODULE, path="api/b.py", layer_id="layer::api"),
        Node(id="mod::svc/s.py", type=NodeType.MODULE, path="svc/s.py", layer_id="layer::svc"),
        Node(
            id="mod::models/m.py",
            type=NodeType.MODULE,
            path="models/m.py",
            layer_id="layer::models",
        ),
        Node(id="sym::api/a.py::f", type=NodeType.SYMBOL, path="api/a.py::f", layer_id="layer::api"),
    ]
    edges = [
        Edge(
            source="mod::api/a.py",
            target="mod::svc/s.py",
            type=EdgeType.IMPORTS,
            evidence="api/a.py:1",
            confidence=CONF_STATIC,
        ),
        Edge(
            source="mod::api/a.py",
            target="mod::svc/s.py",
            type=EdgeType.IMPORTS,
            evidence="api/a.py:2",
            confidence=CONF_STATIC,
        ),
        Edge(
            source="mod::api/b.py",
            target="mod::models/m.py",
            type=EdgeType.IMPORTS,
            evidence="api/b.py:1",
            confidence=CONF_STATIC,
        ),
        Edge(
            source="mod::svc/s.py",
            target="mod::models/m.py",
            type=EdgeType.CALLS,
            evidence="svc/s.py:9",
            confidence=CONF_STATIC,
        ),
        Edge(
            source="layer::api",
            target="layer::svc",
            type=EdgeType.LAYER_DEPENDS,
            evidence="2 条跨层 import",
            confidence=CONF_STATIC,
            weight=2,
        ),
        Edge(
            source="layer::api",
            target="layer::models",
            type=EdgeType.LAYER_DEPENDS,
            evidence="1 条跨层 import",
            confidence=CONF_STATIC,
            weight=1,
        ),
        Edge(
            source="layer::svc",
            target="layer::api",
            type=EdgeType.LAYER_DEPENDS,
            evidence="1 条跨层 import",
            confidence=CONF_STATIC,
            weight=1,
        ),
    ]
    return packet_from_parts(title="demo", layers=layers, nodes=nodes, edges=edges)


def test_graph_page_is_written(tmp_path):
    """产物里必须有 graph.html —— 否则其余用例断言的是"一个不存在的页面"。"""
    packet = _packet()
    count = write_graph_html(tmp_path, packet)
    assert count == 1
    page = tmp_path / GRAPH_HTML
    assert page.is_file(), f"{GRAPH_HTML} 没有落盘"
    assert "<html" in page.read_text(encoding="utf-8")


def test_layer_edges_match_layer_depends(tmp_path):
    """交互图的层间边必须等于 ``LAYER_DEPENDS`` 的聚合 —— WP9 的验收本体。

    从产物文本里抠 JSON，与 packet 里的 ``LAYER_DEPENDS`` 逐项对拍。JS 若自己
    按 IMPORTS 重算，这里立刻就对不上（夹具里 api→svc 的 IMPORTS 是 2 条、
    api→svc 的层边 weight 也是 2，但 svc→api 的层边**没有**对应 IMPORTS，
    JS 重算会漏掉它）。
    """
    packet = _packet()
    payload = _payload(render_graph_html(packet))

    expected: dict[tuple[str, str], int] = {}
    for edge in packet.edges:
        if edge.type == EdgeType.LAYER_DEPENDS and edge.source != edge.target:
            key = (edge.source, edge.target)
            expected[key] = expected.get(key, 0) + edge.weight

    layer_ids = [item["id"] for item in payload["layers"]]
    actual = {(layer_ids[src], layer_ids[dst]): weight for src, dst, weight in payload["layerEdges"]}
    assert actual == expected


def test_module_edges_are_imports_only_and_deduplicated(tmp_path):
    """模块级边只收 ``IMPORTS``，重复对合并成一条并累加 weight。

    两条 ``api/a.py → svc/s.py`` 的 IMPORTS 必须合成 ``weight=2`` 的一条：
    不合并的话重复边在图上完全重叠、点选时互相遮挡，而且边数会虚高。
    ``CALLS`` 不收 —— 收了数据量翻倍（ipip 实测 3905 vs 13028），而"看架构"
    看的是 import 关系。
    """
    payload = _payload(render_graph_html(_packet()))
    paths = [m["p"] for m in payload["modules"]]
    index = {path: i for i, path in enumerate(paths)}

    expected = {
        (index["api/a.py"], index["svc/s.py"]): 2,
        (index["api/b.py"], index["models/m.py"]): 1,
    }
    actual = {(src, dst): weight for src, dst, weight in payload["moduleEdges"]}
    assert actual == expected
    assert all(isinstance(weight, int) and weight >= 1 for weight in actual.values())


def test_symbol_nodes_are_not_drawn(tmp_path):
    """符号不画。ipip 8325 个符号进图 ⇒ 9000 节点，首屏必卡且读不出结构。"""
    payload = _payload(render_graph_html(_packet()))
    assert payload["modules"], "模块列表不该为空"
    assert all(not m["p"].endswith("::f") for m in payload["modules"])


def test_every_module_appears_once(tmp_path):
    """每个模块恰好一条记录 —— 漏了模块，交互图就是个残缺的索引。"""
    packet = _packet()
    payload = _payload(render_graph_html(packet))
    expected = sorted(n.path for n in packet.nodes if n.type == NodeType.MODULE and n.path)
    assert sorted(m["p"] for m in payload["modules"]) == expected


def test_page_references_only_local_assets(tmp_path):
    """页面只许引本地相对路径的资产 —— 离线双击可用的硬条件。

    绝对 URL（CDN）在断网机器上就是白屏，而这类引用在生成的 HTML 里很容易
    混进去（顺手加个字体、加个图标库）。这里扫所有 ``src=`` / ``href=``。
    """
    html = render_graph_html(_packet())
    for match in re.finditer(r'(?:src|href)="([^"]+)"', html):
        target = match.group(1)
        assert not target.startswith(("http://", "https://", "//")), (
            f"页面引用了远程资源：{target}（离线场景会白屏）"
        )


def test_cycle_highlight_data_comes_from_python(tmp_path):
    """层环由 Python 算好传下去，JS 不自己找环。

    api ⇄ svc 是夹具里故意造的两点环。JS 侧做环检测意味着"两边各有一套环算法"，
    而站点首页与 ARCHITECTURE.md 的环数字来自 networkx —— 迟早对不上。
    """
    payload = _payload(render_graph_html(_packet()))
    assert payload["layerCycles"], "层环没传下去，环高亮将无从谈起"
    cycles = [set(cycle) for cycle in payload["layerCycles"]]
    layer_ids = [item["id"] for item in payload["layers"]]
    api, svc = layer_ids.index("layer::api"), layer_ids.index("layer::svc")
    assert any({api, svc} <= cycle for cycle in cycles), f"没找到 api⇄svc 环：{cycles}"


def test_inline_json_cannot_break_out_of_the_script_tag(tmp_path):
    """内联 JSON 里的 ``</script>`` 必须转义，否则页面从这里截断、后面全成文本。

    层名来自被分析仓库的配置（不可信输入）。这是内联数据最经典的一个坑：
    ``json.dumps`` 不会转义 ``<``，直接拼进去就是一个静默的破页 —— 而且只在
    特定仓库上才复现。
    """
    html = render_graph_html(_packet(evil=True))
    assert "</script><img" not in html, "内联 JSON 没有转义 </script>，页面会被截断"
    payload = _payload(html)
    assert any("onerror" in item["n"] for item in payload["layers"])


def test_rendering_is_deterministic(tmp_path):
    """同一 packet 渲染两次逐字节相同 —— 增量产物比对的前提。"""
    packet = _packet()
    assert render_graph_html(packet) == render_graph_html(packet)


def test_page_stays_within_a_reasonable_size():
    """页面体积上限：内联数据 + 胶水脚本要在 1.5 s 首屏内吃得下。

    ipip（729 模块 / 3905 条 IMPORTS）实测约 120 KB 的页面。上限按 400 KB 卡，
    留出三倍余量 —— 它拦的不是"稍微大一点"，而是"把整个 14 MB 数据包塞进来"
    或者"每条边写成完整对象"这类结构性失控。
    """
    scroll = "x" * 200
    layers = [{"id": f"layer::{i}", "name": f"L{i}", "order": i} for i in range(6)]
    nodes = [
        Node(
            id=f"mod::m{i}/{scroll}.py",
            type=NodeType.MODULE,
            path=f"m{i}/{scroll}.py",
            layer_id=f"layer::{i % 6}",
        )
        for i in range(600)
    ]
    edges = [
        Edge(
            source=f"mod::m{i}/{scroll}.py",
            target=f"mod::m{(i + 1) % 600}/{scroll}.py",
            type=EdgeType.IMPORTS,
            evidence="e",
            confidence=CONF_STATIC,
        )
        for i in range(3000)
    ]
    packet = packet_from_parts(title="big", layers=layers, nodes=nodes, edges=edges)
    html = render_graph_html(packet)
    assert len(html.encode("utf-8")) < 400 * 1024, f"页面 {len(html)} 字符，超上限"
