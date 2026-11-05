"""交互式架构图页面（``graph.html``，WP9）。

**为什么要有这一页**：首页的分层图是内联 SVG，静态、读得出结构但下不去。真实
排查是"接口层 120 个模块，哪几个在偷偷直连数据层" —— 那要在 729 个点上搜索、
点开、看邻居。静态 SVG 做不到，SVG 里塞 729 个可点击节点会把首页撑到几 MB。

**核心设计：JS 不算图。** 层间条数、层环、模块索引全部在 Python 侧算好，塞成
一段 JSON；页面脚本只做四件事 —— 建图、下钻、搜索、高亮。理由不是洁癖：本仓在
:func:`codewiki.render.layer_map._layer_pairs` 那儿已经为"两边各算一遍"踩过一次
（系统图写 52、首页写 82），代价是一张同页两张图互相矛盾。图算法留在 Python 还
有个好处：环检测用的是 ``graph/analysis.py`` 里那份 networkx 实现，与诊断、与
``ARCHITECTURE.md`` 的环数天然同源。

**为什么坐标也由 Python 给**：cytoscape 自带 ``dagre``/``cose`` 要额外装扩展
（+100 KB），而层的顺序在我们这儿是配置里就有的事实（``layers[].order``）——
让图库从边关系反推一个"它认为"的层次，反而会把人工声明的分层排错。层视图用
``preset`` 直接吃 ``order`` 换算出的坐标；模块视图用内置 ``grid``（纯排布，
不涉及图关系）。

**数据为什么要瘦身**：ipip 完整数据包 14 MB（9155 节点 / 22401 边），内联进
HTML 会让首屏直接超时。这里只取模块级（729 个）、只取 ``IMPORTS``（3905 条，
``CALLS`` 会让边数翻到 13028）、同对模块的重复 import 合并成带 ``weight`` 的
一条，最终 JSON 约 90 KB。符号节点一个都不进 —— 8325 个符号铺开就是毛线团。
"""

from __future__ import annotations

import json
from pathlib import Path

from codewiki.graph.analysis import find_layer_cycles
from codewiki.models import EdgeType, NodeType
from codewiki.render.i18n import html_lang, t, t_raw
from codewiki.render.layer_map import layer_color
from codewiki.schemas import DataPacket, Diagnostic

GRAPH_HTML = "graph.html"
GRAPH_ASSET = "assets/graph/cytoscape.min.js"
PACKAGE_GRAPH_ASSET = Path(__file__).resolve().parent.parent / "assets" / "graph" / "cytoscape.min.js"

UNLAYERED = ""

PER_COLUMN = 12
LAYER_GAP = 118.0
LAYER_X = 320.0
LAYER_COL_GAP = 360.0


def _ordered_layers(packet: DataPacket):
    """按 ``order`` 排层（``None`` 排最后），与首页/SVG 分层图同一顺序。"""
    return sorted(packet.layers, key=lambda item: (item.order if item.order is not None else 10_000, item.id))


def _module_rows(packet: DataPacket) -> list[tuple[str, str, str]]:
    """``(模块 id, 路径, 层 id)``，按层序再按路径排 —— 索引稳定，产物才可复现。

    只收 ``MODULE`` 且有 ``path`` 的节点：符号进图既没法看（ipip 8325 个），
    也没必要（结构问题在模块粒度就已经显形）。
    """
    order = {layer.id: i for i, layer in enumerate(_ordered_layers(packet))}
    modules = [
        (node.id, node.path, node.layer_id or UNLAYERED)
        for node in packet.nodes
        if node.type == NodeType.MODULE and node.path
    ]
    return sorted(modules, key=lambda row: (order.get(row[2], 10_000), row[2], row[1]))


def build_graph_payload(packet: DataPacket) -> dict:
    """页面要用的全部数据。**不含任何图算法**：环从 ``graph/analysis.py`` 取。

    返回的每个"边"都是 ``[源索引, 宿索引, 权重]`` 三元组而不是对象 —— 3905 条边
    写成 ``{"source": …, "target": …, "weight": …}`` 会让 JSON 从 90 KB 涨到
    300 KB 以上，键名在小数据量下无所谓，在 729×3905 的规模上就是首屏时间。
    """
    layers = _ordered_layers(packet)
    layer_index = {layer.id: i for i, layer in enumerate(layers)}
    modules = _module_rows(packet)
    module_index = {mid: i for i, (mid, _, _) in enumerate(modules)}

    per_layer: dict[str, int] = {}
    for _, _, layer_id in modules:
        per_layer[layer_id] = per_layer.get(layer_id, 0) + 1

    def slot(i: int) -> tuple[float, float]:
        """第 i 个层节点的坐标：顺着 order 往下填，满 ``PER_COLUMN`` 换下一列。

        顺序仍然是 order，只是折行 —— 相邻层在多数情况下还是上下相邻，
        而 46 层的仓库不至于变成一条读不完的竖线。
        """
        return LAYER_X + LAYER_COL_GAP * (i // PER_COLUMN), LAYER_GAP * (i % PER_COLUMN)

    rows = []
    for i, layer in enumerate(layers):
        x, y = slot(i)
        rows.append(
            {
                "id": layer.id,
                "n": layer.name,
                "c": layer_color(layer.name),
                "x": x,
                "y": y,
                "m": per_layer.get(layer.id, 0),
            }
        )
    if per_layer.get(UNLAYERED):
        x, y = slot(len(rows))
        rows.append(
            {
                "id": UNLAYERED,
                "n": t("common.unlayered"),
                "c": "#94a3b8",
                "x": x,
                "y": y,
                "m": per_layer[UNLAYERED],
            }
        )

    layer_edges: dict[tuple[int, int], int] = {}
    for edge in packet.edges:
        if edge.type != EdgeType.LAYER_DEPENDS or edge.source == edge.target:
            continue
        src, dst = layer_index.get(edge.source), layer_index.get(edge.target)
        if src is None or dst is None:
            continue
        layer_edges[(src, dst)] = layer_edges.get((src, dst), 0) + edge.weight

    module_edges: dict[tuple[int, int], int] = {}
    for edge in packet.edges:
        if edge.type != EdgeType.IMPORTS:
            continue
        src, dst = module_index.get(edge.source), module_index.get(edge.target)
        if src is None or dst is None or src == dst:
            continue
        module_edges[(src, dst)] = module_edges.get((src, dst), 0) + edge.weight

    cycles = [
        [layer_index[member] for member in cycle.members if member in layer_index]
        for cycle in find_layer_cycles(packet)
    ]

    return {
        "title": packet.meta.title,
        "layers": rows,
        "layerEdges": [[s, d, w] for (s, d), w in sorted(layer_edges.items())],
        "modules": [
            {"p": path, "l": layer_index.get(layer_id, len(rows) - 1 if rows else 0)}
            for _, path, layer_id in modules
        ],
        "moduleEdges": [[s, d, w] for (s, d), w in sorted(module_edges.items())],
        "layerCycles": [cycle for cycle in cycles if len(cycle) >= 2],
    }


def _inline_json(payload: dict) -> str:
    """把数据塞进 ``<script type="application/json">``，转义成页面安全的形态。

    ``json.dumps`` **不转义 ``<``**，于是层名里出现 ``</script>`` 就会把页面从
    那里截断 —— 后面全部变成正文，图不出现、控制台无报错，而层名来自被分析仓库
    的配置（不可信输入）。转义 ``<`` / ``>`` / ``&`` 三个字符即可关上这条路，
    ``\\u003c`` 在 JSON 里与 ``<`` 等价，读回来一字不差。
    """
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


_GRAPH_STYLES = """
:root{
  --bg:#ffffff; --fg:#0f172a; --muted:#64748b; --line:#e2e8f0;
  --chip:#f1f5f9; --accent:#4f46e5;
}
@media (prefers-color-scheme: dark){
  :root{ --bg:#0b1120; --fg:#e2e8f0; --muted:#94a3b8; --line:#1e293b;
         --chip:#111c33; --accent:#818cf8; }
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--fg);
  font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
header{display:flex;gap:10px;align-items:center;padding:10px 16px;
  border-bottom:1px solid var(--line);flex-wrap:wrap}
h1{font-size:15px;font-weight:600;margin:0 8px 0 0}
button,input,select{font:inherit;color:inherit;background:var(--chip);
  border:1px solid var(--line);border-radius:7px;padding:5px 10px}
button{cursor:pointer}
button:hover{border-color:var(--accent)}
input{min-width:200px}
#hint{color:var(--muted);font-size:12.5px;margin-left:auto}
#cy{position:fixed;top:0;left:0;right:0;bottom:0;margin-top:56px}
#empty{color:var(--muted);padding:24px;font-size:13px}
@media print{header{display:none}#cy{margin-top:0}}
"""

_GRAPH_SCRIPT = """
(function () {
  "use strict";
  var node = document.getElementById("graph-data");
  if (!node) return;
  var D = JSON.parse(node.textContent);
  var T = window.__GRAPH_I18N;

  var cy = cytoscape({
    container: document.getElementById("cy"),
    wheelSensitivity: 0.2,
    style: [
      {selector: "node", style: {
        "label": "data(label)", "font-size": 11, "color": "#fff",
        "text-valign": "center", "text-halign": "center",
        "background-color": "data(color)", "width": "data(w)", "height": "data(w)",
        "text-wrap": "wrap", "text-max-width": "data(w)", "padding": 6
      }},
      {selector: "edge", style: {
        "width": "data(w)", "line-color": "#94a3b8", "target-arrow-color": "#94a3b8",
        "target-arrow-shape": "triangle", "curve-style": "bezier",
        "opacity": 0.72, "arrow-scale": 0.8
      }},
      {selector: "edge[weight]", style: {
        "label": "data(weight)", "font-size": 9, "color": "#64748b",
        "text-background-color": "#fff", "text-background-opacity": 0.72,
        "text-background-padding": 1
      }},
      {selector: ".dim", style: {"opacity": 0.12}},
      {selector: ".hit", style: {"border-width": 3, "border-color": "#f59e0b"}},
      {selector: "node.cyc", style: {"border-width": 3.5, "border-color": "#ef4444"}},
      {selector: "edge.cyc", style: {
        "line-color": "#ef4444", "target-arrow-color": "#ef4444",
        "width": 2.6, "opacity": 1
      }}
    ]
  });

  var view = {kind: "layers"};
  var cycleOn = false;

  function layerElements() {
    var els = D.layers.map(function (l, i) {
      return {data: {id: "L" + i, label: l.n + "\\n" + T.modules.replace("{n}", l.m),
                     color: l.c, w: 54 + Math.min(46, l.m)}, position: {x: l.x, y: l.y}};
    });
    D.layerEdges.forEach(function (e, i) {
      els.push({data: {id: "LE" + i, source: "L" + e[0], target: "L" + e[1], weight: e[2]}});
    });
    return els;
  }

  function moduleElements(layerIdx) {
    var idx = [], els = [];
    D.modules.forEach(function (m, i) {
      if (m.l !== layerIdx) return;
      idx.push(i);
      var base = m.p.split("/").pop();
      els.push({data: {id: "M" + i, label: base, color: D.layers[layerIdx].c, w: 22}});
    });
    var keep = new Set(idx);
    D.moduleEdges.forEach(function (e, i) {
      if (!keep.has(e[0]) || !keep.has(e[1])) return;
      els.push({data: {id: "ME" + i, source: "M" + e[0], target: "M" + e[1], weight: e[2]}});
    });
    return els;
  }

  function layerOf(kind) { return kind === "layers" ? -1 : view.layer; }

  function render() {
    var els = view.kind === "layers" ? layerElements() : moduleElements(view.layer);
    cy.elements().remove();
    cy.add(els);
    if (view.kind === "layers") {
      cy.layout({name: "preset", fit: true, padding: 60}).run();
    } else {
      cy.layout({name: "grid", fit: true, padding: 40, avoidOverlap: true,
                 condense: false}).run();
    }
    document.getElementById("empty").hidden = els.length > 0;
    if (cycleOn) markCycles();
  }

  function markCycles() {
    cy.elements().removeClass("cyc");
    if (view.kind !== "layers") return;
    var on = new Set();
    D.layerCycles.forEach(function (ring) { ring.forEach(function (i) { on.add(i); }); });
    if (!on.size) return;
    cy.nodes().forEach(function (n) {
      var i = Number(n.id().slice(1));
      if (on.has(i)) n.addClass("cyc");
    });
    cy.edges().forEach(function (e) {
      var a = Number(e.source().id().slice(1)), b = Number(e.target().id().slice(1));
      D.layerCycles.forEach(function (ring) {
        if (ring.indexOf(a) >= 0 && ring.indexOf(b) >= 0) e.addClass("cyc");
      });
    });
  }

  function search(q) {
    cy.elements().removeClass("dim hit");
    if (!q) return;
    var needle = q.toLowerCase();
    cy.nodes().forEach(function (n) {
      var hit = String(n.data("label")).toLowerCase().indexOf(needle) >= 0;
      n.addClass(hit ? "hit" : "dim");
      if (hit) n.connectedEdges().removeClass("dim");
      else n.connectedEdges().addClass("dim");
    });
  }

  function drill(idx) {
    view = {kind: "modules", layer: idx};
    document.getElementById("back").hidden = false;
    document.getElementById("hint").textContent = T.detail;
    cycleOn = false;
    render();
  }

  function back() {
    view = {kind: "layers"};
    document.getElementById("back").hidden = true;
    document.getElementById("hint").textContent = T.layers;
    render();
  }

  cy.on("tap", "node", function (ev) {
    if (view.kind === "layers") drill(Number(ev.target.id().slice(1)));
  });

  document.getElementById("back").addEventListener("click", back);
  document.getElementById("search").addEventListener("input", function (ev) {
    search(ev.target.value.trim());
  });
  document.getElementById("cyc").addEventListener("click", function () {
    if (view.kind !== "layers") return;
    cycleOn = !cycleOn;
    if (cycleOn) markCycles();
    else cy.elements().removeClass("cyc");
  });

  render();
})();
"""


def render_graph_html(packet: DataPacket) -> str:
    """交互图页面。数据与文案都从参数/语言表来，函数本身无副作用。"""
    payload = build_graph_payload(packet)
    labels = {
        "modules": t_raw("graph.modules_in_layer"),
        "layers": t("graph.hint_layers"),
        "detail": t("graph.hint_detail"),
    }
    return (
        "<!doctype html>\n"
        f'<html lang="{html_lang()}">\n<head>\n<meta charset="utf-8" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        f"<title>{t('graph.page_title')} · codewiki</title>\n"
        f"<style>{_GRAPH_STYLES}</style>\n"
        "</head>\n<body>\n"
        "<header>\n"
        f"  <h1>{t('graph.heading')}</h1>\n"
        f'  <button id="back" hidden>{t("graph.back")}</button>\n'
        f'  <input id="search" type="search" placeholder="{t("graph.search_placeholder")}" />\n'
        f'  <button id="cyc">{t("graph.highlight_cycles")}</button>\n'
        f'  <span id="hint">{labels["layers"]}</span>\n'
        "</header>\n"
        f'<div id="cy"></div>\n'
        f'<div id="empty" hidden>{t("graph.empty")}</div>\n'
        f'<script id="graph-data" type="application/json">{_inline_json(payload)}</script>\n'
        f"<script>window.__GRAPH_I18N={json.dumps(labels, ensure_ascii=False)};</script>\n"
        f'<script src="{GRAPH_ASSET}"></script>\n'
        f"<script>{_GRAPH_SCRIPT}</script>\n"
        "</body>\n</html>\n"
    )


def graph_asset_diagnostics(out_dir: Path) -> list[Diagnostic]:
    """交互图运行时是否就位（缺失只提示，不阻断）。

    单独一个函数而不是并进 ``site.asset_diagnostics``：那个函数的语义是"mermaid
    是否就位"，两条诊断混在一个返回值里，调用方想分别处理就得分字符串。缺
    cytoscape 时 ``graph.html`` 仍然生成（数据都在），只是打开来是一张白板 ——
    这正是要让人看见的事，所以记一条 info。
    """
    if (out_dir / GRAPH_ASSET).exists():
        return []
    return [
        Diagnostic(
            level="info",
            code="ASSET_MISSING",
            message=f"缺少 {GRAPH_ASSET}：交互架构图（graph.html）无法渲染",  # i18n-ignore
        )
    ]


def write_graph_html(out_dir: Path, packet: DataPacket) -> int:
    """渲染并落盘；返回写的页数（恒为 1，与 ``write_module_html`` 同形）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / GRAPH_HTML).write_text(render_graph_html(packet), encoding="utf-8")
    return 1


def copy_graph_asset(out_dir: Path, *, source: Path | None = None) -> Path | None:
    """把 cytoscape 复制进产物；包内没有时返回 ``None``（不伪造）。

    与 ``site.copy_offline_asset`` 同一姿势：**复制而不是引用**，产物要能整包
    拷到另一台机器上打开。
    """
    import shutil

    src = Path(source) if source else PACKAGE_GRAPH_ASSET
    if not src.is_file():
        return None
    dst = out_dir / GRAPH_ASSET
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return dst


__all__ = [
    "GRAPH_ASSET",
    "GRAPH_HTML",
    "PACKAGE_GRAPH_ASSET",
    "build_graph_payload",
    "copy_graph_asset",
    "graph_asset_diagnostics",
    "render_graph_html",
    "write_graph_html",
]
