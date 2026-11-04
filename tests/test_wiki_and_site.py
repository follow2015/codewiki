"""Wiki 与站点判据（§4.5）。

钉三件事：

1. 模块页三要素齐全：**符号 / 上下游 / 注解**，并内嵌上下文图；
2. 索引页给出分层 → 模块的导航 + 诊断摘要；
3. ``export --zip`` 原子化打包，且**不把临时文件打进去**。
"""

from __future__ import annotations

import zipfile

from codewiki.graph import build_graph
from codewiki.render.diagrams import render_all
from codewiki.render.jsonio import packet_from_parts
from codewiki.render.site import asset_diagnostics, render_index_html, render_module_html, write_zip
from codewiki.render.wiki import render_index, render_module_page
from codewiki.schemas import Annotation
from tests.conftest import parse_all


def _packet(mini_py, *, annotations=None):
    result = build_graph(parse_all(mini_py))
    return packet_from_parts(
        title="mini",
        layers=[],
        nodes=result.nodes,
        edges=result.edges,
        annotations=annotations or [],
        diagnostics=result.diagnostics,
    )


def test_module_page_covers_symbols_neighbors_and_annotations(mini_py):
    packet = _packet(
        mini_py,
        annotations=[
            Annotation(
                target="mod::pkg/core.py", label="核心", note="下单主链路。", source="human", reviewed=True
            )
        ],
    )
    page = render_module_page(packet, "mod::pkg/core.py", diagram_text=None)

    assert "# pkg/core.py" in page
    assert "## 符号" in page and "`Service`" in page and "`helper`" in page
    assert "## 上下游" in page and "app.py" in page
    assert "## 注解" in page and "下单主链路。" in page and "人工" in page
    assert "返回索引" in page


def test_ai_annotation_tag_uses_lowercase_bool(mini_py):
    """AI 注解的审阅标记要与配置里的字面量一致（``reviewed = false``）。

    直接把 bool 插进 f-string 会把 Python 的 ``False`` 印到页面上（实测 30 个
    模块页全是 ``AI（reviewed=False）``）：读的人会当成另一个字段名，而配置里
    写的是小写 false —— 页面与配置对不上，是最容易被忽略的那种不一致。
    """
    packet = _packet(
        mini_py,
        annotations=[Annotation(target="mod::pkg/core.py", note="AI 建议。", source="ai", reviewed=False)],
    )
    page = render_module_page(packet, "mod::pkg/core.py", diagram_text=None)

    assert "AI（reviewed=false）" in page
    assert "False" not in page


def test_module_page_embeds_the_context_diagram(mini_py):
    packet = _packet(mini_py)
    page = render_module_page(packet, "mod::pkg/core.py", diagram_text='flowchart LR\n  M["x"]\n')

    assert "```mermaid" in page and 'M["x"]' in page


def test_index_lists_layers_modules_and_diagnostics(mini_py):
    packet = _packet(mini_py)
    text = render_index(packet)

    assert text.startswith("# mini")
    assert "## 诊断摘要" in text
    assert "modules/pkg_core.md" in text


def test_site_html_is_self_contained_and_references_the_local_asset(mini_py):
    packet = _packet(mini_py)
    html_text = render_index_html(packet)

    assert html_text.startswith("<!doctype html>")
    assert 'src="assets/mermaid.min.js"' not in html_text, "首页没有 mermaid 块就别加载运行时"
    module_html = render_module_html("# t\n", title="t")
    assert 'src="assets/mermaid.min.js"' in module_html, "模块页的图仍由随包 mermaid 运行时渲染"


def test_missing_asset_is_reported_not_silently_ignored(tmp_path):
    issues = asset_diagnostics(tmp_path)

    assert [i.code for i in issues] == ["ASSET_MISSING"]
    assert issues[0].level == "info"


def test_export_zip_is_atomic_and_skips_temp_files(mini_py, tmp_path):
    out_dir = tmp_path / "wiki-output"
    render_all(_packet(mini_py), out_dir)
    (out_dir / "stray.tmp").write_text("junk", encoding="utf-8")

    target = tmp_path / "bundle.zip"
    produced = write_zip(out_dir, target)

    assert produced == target and target.exists()
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
    assert "system-context.mmd" in "".join(names) or any(n.endswith("system-context.mmd") for n in names)
    assert not any(n.endswith(".tmp") for n in names), "临时文件不能进交付物"
    assert not target.with_suffix(".zip.tmp").exists()
    assert not (tmp_path / "bundle.zip.tmp").exists()


def _packet_with_paths(paths, *, layers=None):
    from codewiki.models import FileModel

    files = [FileModel(path=p, language="python", hash=p, symbols=[], imports=[], calls=[]) for p in paths]
    result = build_graph(files)
    nodes = [
        n.model_copy(update={"layer_id": layers[n.path]}) if layers and n.path in layers else n
        for n in result.nodes
    ]
    layer_nodes = (
        [
            {"id": lid, "name": lid.removeprefix("layer::"), "order": i}
            for i, lid in enumerate(dict.fromkeys(layers.values()))
        ]
        if layers
        else []
    )
    return packet_from_parts(
        title="t",
        layers=layer_nodes,
        nodes=nodes,
        edges=result.edges,
        annotations=[],
        diagnostics=result.diagnostics,
    )


def test_site_index_links_point_to_rendered_html_not_raw_md():
    """index.html 的模块链接必须指向渲染后的 .html 页，浏览器点开可读。"""
    packet = _packet_with_paths(["m/a.py"])
    html_text = render_index_html(packet)

    assert 'href="modules/m_a.html"' in html_text, html_text
    assert '.md"' not in html_text.replace("diagnostics.html", ""), "不得再链接 .md 源码"


def test_site_index_links_are_collision_safe():
    """a-b.py 与 a/b.py 同 slug：链接必须用消歧后的文件名（与落盘页面一致）。"""
    packet = _packet_with_paths(["m/a-b.py", "m/a/b.py"])
    html_text = render_index_html(packet)

    hrefs = [
        line.split('href="')[1].split('"')[0] for line in html_text.splitlines() if 'href="modules/' in line
    ]
    assert len(hrefs) == 2 and len(set(hrefs)) == 2, f"两条链接撞车: {hrefs}"
    for href in hrefs:
        assert href.endswith(".html")


def test_md_to_html_renders_known_subset():
    """自研零依赖渲染器：覆盖自家模块页用到的 markdown 子集。"""
    from codewiki.render.site import md_to_html

    md = (
        "# pkg/core.py\n\n"
        "语言 `python` · 层 服务层\n\n"
        "## 符号\n\n"
        "| 名称 | 类型 |\n| --- | --- |\n| `run` | function |\n\n"
        "## 上下游\n\n"
        "- [pkg/util.py](pkg_util.md) （import）\n"
        "- **上游**（谁用到我）\n\n"
        '```mermaid\nflowchart LR\n  M["x"]\n```\n\n'
        "```python\nA = 1\n```\n\n"
        "[← 返回索引](../index.md)\n"
    )
    out = md_to_html(md)

    assert "<h1>" in out and "<h2>" in out
    assert "<code>python</code>" in out
    assert "<table>" in out and "<td><code>run</code></td>" in out
    assert '<a href="pkg_util.html">' in out, "页内互链要改写成 .html"
    assert "<strong>上游</strong>" in out
    assert '<pre class="mermaid">' in out, "mermaid 块交给 mermaid.js 渲染"
    assert "<pre><code>A = 1" in out, "普通代码块照常转义呈现"
    assert '<a href="../index.html">' in out
    assert 'src="assets/mermaid.min.js"' in render_module_html(md, title="pkg/core.py")


def test_analyze_writes_module_html_pages(tmp_path):
    """端到端：analyze 产物里每个模块页都有配对的 .html，且与 index 链接一一对应。"""
    import json as jsonlib

    from codewiki.cli import main

    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("#\n", encoding="utf-8")
    (root / "pkg" / "a.py").write_text(
        "from pkg.b import B\n\nclass A:\n    def run(self):\n        return B()\n", encoding="utf-8"
    )
    (root / "pkg" / "b.py").write_text("class B:\n    pass\n", encoding="utf-8")
    out = tmp_path / "out"

    assert main(["analyze", "--src", str(root), "--out", str(out)]) == 0

    pages = sorted(p.name for p in (out / "modules").glob("*.html"))
    assert pages, "模块页必须有 HTML 版"
    index_html = (out / "index.html").read_text(encoding="utf-8")
    for name in pages:
        assert f"modules/{name}" in index_html, f"index 链接缺 {name}"
        page = (out / "modules" / name).read_text(encoding="utf-8")
        assert page.startswith("<!doctype html>")
    packet = jsonlib.loads((out / "data" / "codewiki.json").read_text(encoding="utf-8"))
    n_mods = sum(1 for n in packet["nodes"] if n["type"] == "Module")
    assert len(pages) == n_mods, f"{len(pages)} 个 html vs {n_mods} 个模块"


def test_analyze_prunes_stale_pages(tmp_path):
    """模块删除/改名后，上一轮的过期页面必须被清掉——否则 wiki 越积越陈旧
    （IPIP 实测：口径收紧后 modules/ 里躺着 111 个旧口径页面）。"""
    from codewiki.cli import main

    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("#\n", encoding="utf-8")
    (root / "pkg" / "a.py").write_text("A = 1\n", encoding="utf-8")
    out = tmp_path / "out"
    stale_md = out / "modules" / "removed_old.md"
    stale_html = out / "modules" / "removed_old.html"
    stale_mmd = out / "diagrams" / "modules" / "removed_old.mmd"
    for p in (stale_md, stale_html, stale_mmd):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("stale\n", encoding="utf-8")

    assert main(["analyze", "--src", str(root), "--out", str(out)]) == 0

    assert not stale_md.exists() and not stale_html.exists() and not stale_mmd.exists()


def test_module_html_has_themed_mermaid_init():
    page = render_module_html('<pre class="mermaid">flowchart LR</pre>\n', title="t")
    assert "mermaid.initialize" in page, "按系统主题初始化 mermaid（暗色图不至于白底刺眼）"


def test_module_pages_reference_asset_from_parent_dir(tmp_path):
    """模块页落在 ``modules/`` 子目录：资源引用必须带 ``../`` 前缀。

    否则脚本按 ``modules/assets/…`` 解析 → 404 → mermaid 不加载，
    上下文图只能以源码示人（IPIP 实测翻车）。根下的 diagnostics.html
    仍用 ``assets/…``。
    """
    from codewiki.render.site import write_module_html

    modules = tmp_path / "modules"
    modules.mkdir()
    (modules / "a.md").write_text("# A\n", encoding="utf-8")
    (tmp_path / "diagnostics.md").write_text("# D\n", encoding="utf-8")

    write_module_html(tmp_path)

    module_html = (modules / "a.html").read_text(encoding="utf-8")
    diag_html = (tmp_path / "diagnostics.html").read_text(encoding="utf-8")
    assert 'src="../assets/mermaid.min.js"' in module_html
    assert 'src="assets/mermaid.min.js"' in diag_html


def test_mermaid_init_raises_max_text_size():
    """mermaid 默认 maxTextSize=50000（字符），大仓库的分包图逼近该上限
    会整图报错；初始化必须显式放宽（IPIP 实测 107KB 主图当场翻车）。"""
    page = render_module_html("# t\n", title="t")
    assert "maxTextSize" in page


def _packet_with_layers_and_edges(pairs, *, layers=None):
    """pairs: [(src_path, dst_path), …] → IMPORTS 边；layers: path → layer_id。"""
    from codewiki.models import Edge, EdgeType, FileModel

    paths = list(dict.fromkeys([p for pair in pairs for p in pair] + list((layers or {}).keys())))
    files = [FileModel(path=p, language="python", hash=p, symbols=[], imports=[], calls=[]) for p in paths]
    result = build_graph(files)
    nodes = [
        n.model_copy(update={"layer_id": layers[n.path]}) if layers and n.path in layers else n
        for n in result.nodes
    ]
    layer_nodes = (
        [
            {"id": lid, "name": lid.removeprefix("layer::"), "order": i}
            for i, lid in enumerate(dict.fromkeys(layers.values()))
        ]
        if layers
        else []
    )
    edges = [Edge(source=f"mod::{s}", target=f"mod::{d}", type=EdgeType.IMPORTS) for s, d in pairs]
    if layers:
        from codewiki.layering import UNLAYERED_LAYER_ID

        counts: dict[tuple[str, str], int] = {}
        for s, d in pairs:
            src = layers.get(s, UNLAYERED_LAYER_ID)
            dst = layers.get(d, UNLAYERED_LAYER_ID)
            if src != dst:
                counts[(src, dst)] = counts.get((src, dst), 0) + 1
        edges += [
            Edge(source=src, target=dst, type=EdgeType.LAYER_DEPENDS, weight=weight)
            for (src, dst), weight in counts.items()
        ]
    return packet_from_parts(
        title="t",
        layers=layer_nodes,
        nodes=nodes,
        edges=edges,
        annotations=[],
        diagnostics=result.diagnostics,
    )


def test_index_renders_layer_map_not_mermaid_macro():
    """首页宏观图 = 分层架构地图（内联 SVG），不再嵌 mermaid 巨图。"""
    packet = _packet_with_layers_and_edges(
        [("web/a.py", "svc/b.py"), ("web/a.py", "svc/c.py"), ("svc/b.py", "svc/c.py")],
        layers={
            "web/a.py": "layer::接口层",
            "svc/b.py": "layer::服务层",
            "svc/c.py": "layer::服务层",
        },
    )
    html_text = render_index_html(packet)

    assert "<svg" in html_text and "layer-map" in html_text, "首页要有分层架构地图（内联 SVG）"
    assert "接口层" in html_text and "服务层" in html_text, "层带要显示层名"
    assert 'class="mermaid"' not in html_text, "首页不再嵌 mermaid 宏观图"


def test_layer_map_shows_counts_flows_and_anchors():
    """层带带模块/符号计数；跨层流要显示方向与条数；层卡可点击跳到分组。"""
    packet = _packet_with_layers_and_edges(
        [("web/a.py", "svc/b.py"), ("web/a.py", "svc/c.py"), ("svc/b.py", "svc/c.py")],
        layers={
            "web/a.py": "layer::接口层",
            "svc/b.py": "layer::服务层",
            "svc/c.py": "layer::服务层",
        },
    )
    html_text = render_index_html(packet)

    assert "接口层 → 服务层" in html_text, "跨层流要写明方向"
    assert 'href="#layer-layer::服务层"' in html_text, "层卡点击跳到模块分组"
    assert 'id="layer-layer::服务层"' in html_text, "模块分组要有锚点 id"


def test_layer_map_excludes_within_layer_edges_from_flows():
    """同层引用不算跨层流 —— 流列表只收层与层之间的边。"""
    packet = _packet_with_layers_and_edges(
        [("svc/b.py", "svc/c.py")],
        layers={"svc/b.py": "layer::服务层", "svc/c.py": "layer::服务层"},
    )
    html_text = render_index_html(packet)

    assert "服务层 → 服务层" not in html_text


def test_layer_map_names_unlayered_bucket_in_flows():
    """未分层模块参与的跨层流要显示「未分层」，不许泄漏原始桶 id。"""
    packet = _packet_with_layers_and_edges(
        [("web/a.py", "misc/z.py")],
        layers={"web/a.py": "layer::接口层"},
    )
    html_text = render_index_html(packet)

    assert "接口层 → 未分层" in html_text
    assert "layer::_unlayered" not in html_text


def test_layer_map_svg_void_elements_are_self_closed():
    """SVG 的 rect/circle/line 必须自闭合（``/>``）。

    内联在 HTML 里时，未闭合的 ``<rect>`` 会把后续 ``<text>`` 吞成自己的
    子元素 —— SVG 不渲染 shape 的子节点，整条层带渲染成空白卡片
    （IPIP 实测翻车：层名/计数全消失，只有 a 外面的 ⇄ 标签幸存）。
    """
    import re

    from codewiki.render.layer_map import render_layer_map_svg

    packet = _packet_with_layers_and_edges(
        [("web/a.py", "svc/b.py")],
        layers={"web/a.py": "layer::接口层", "svc/b.py": "layer::服务层"},
    )
    svg = render_layer_map_svg(packet)

    leaks = re.findall(r"<(?:rect|circle|line)(?![^>]*?/>)", svg)
    assert not leaks, f"存在未自闭合的 SVG 元素: {leaks[:3]}"


def test_layer_map_respects_layer_order_and_unlayered_last():
    """层带按配置 order 排；未分层兜底在最后。"""
    packet = _packet_with_layers_and_edges(
        [],
        layers={
            "web/a.py": "layer::接口层",
            "svc/b.py": "layer::服务层",
            "misc/z.py": "layer::基础设施",
        },
    )
    html_text = render_index_html(packet)
    i_interface = html_text.index("接口层")
    i_service = html_text.index("服务层")
    i_infra = html_text.index("基础设施")
    assert i_interface < i_service < i_infra


def test_layer_map_omits_zero_symbol_count():
    """符号数为 0 时只写模块数 —— 七层全写「· 0 符号」是纯噪声。"""
    from codewiki.render.layer_map import render_layer_map_svg

    packet = _packet_with_layers_and_edges(
        [("web/a.py", "svc/b.py")],
        layers={"web/a.py": "layer::接口层", "svc/b.py": "layer::服务层"},
    )
    svg = render_layer_map_svg(packet)

    assert "0 符号" not in svg, f"零符号不该出现在层带上:\n{svg[:400]}"
    assert "1 模块" in svg


def test_layer_map_band_shows_outbound_and_inbound():
    """层带右侧标出该层的出/入跨层引用条数，作为耦合度的第二维。"""
    from codewiki.render.layer_map import render_layer_map_svg

    packet = _packet_with_layers_and_edges(
        [("web/a.py", "svc/b.py"), ("web/a.py", "svc/c.py"), ("svc/b.py", "svc/c.py")],
        layers={
            "web/a.py": "layer::接口层",
            "svc/b.py": "layer::服务层",
            "svc/c.py": "layer::服务层",
        },
    )
    svg = render_layer_map_svg(packet)

    assert "出 2 · 入 0" in svg, "接口层只出不入"
    assert "出 0 · 入 2" in svg, "服务层只入不出（同层边不计）"


def test_layer_map_flow_bars_are_proportional_to_counts():
    """右栏是条形图：最重的一对占满轨道，其余按比例缩短。"""
    import re

    from codewiki.render.layer_map import render_layer_map_svg

    packet = _packet_with_layers_and_edges(
        [
            ("web/a.py", "svc/b.py"),
            ("web/a.py", "infra/c.py"),
            ("web/a.py", "infra/d.py"),
        ],
        layers={
            "web/a.py": "layer::接口层",
            "svc/b.py": "layer::服务层",
            "infra/c.py": "layer::基础设施",
            "infra/d.py": "layer::基础设施",
        },
    )
    svg = render_layer_map_svg(packet)

    tracks = [
        float(w) for w in re.findall(r'width="([\d.]+)" height="4" rx="2" style="fill:var\(--border\)"', svg)
    ]
    fills = [float(w) for w in re.findall(r'width="([\d.]+)" height="4" rx="2" fill="#', svg)]
    assert len(tracks) == len(fills) == 2, f"两对跨层引用各要一条轨道 + 一条填充:\n{svg[-800:]}"
    assert max(fills) == max(tracks), "最重的一对要占满轨道"
    assert min(fills) < max(fills), f"较轻的一对要按比例缩短，实际: {fills}"


def test_layer_map_columns_share_the_same_height():
    """左右两列等高：右栏高度必须顶到最后一个层带的下沿。"""
    import re

    from codewiki.render.layer_map import BAND_GAP, BAND_H, render_layer_map_svg

    packet = _packet_with_layers_and_edges([], layers={"web/a.py": "layer::A", "svc/b.py": "layer::B"})
    svg = render_layer_map_svg(packet)

    bands = 2
    stack_h = bands * BAND_H + (bands - 1) * BAND_GAP
    panel_heights = [float(h) for h in re.findall(r'width="300(?:\.0)?" height="([\d.]+)"', svg)]
    assert panel_heights == [stack_h], f"右栏应顶到左列下沿 {stack_h}，实际 {panel_heights}"


def test_index_layer_chips_carry_a_color_dot():
    """header 的层 chips 带层色圆点 —— 只设 `--c` 却没有元素消费它等于没上色。"""
    import re

    packet = _packet_with_layers_and_edges(
        [("web/a.py", "svc/b.py")],
        layers={"web/a.py": "layer::接口层", "svc/b.py": "layer::服务层"},
    )
    html_text = render_index_html(packet)

    assert 'class="chip layer"' in html_text
    assert re.search(r'class="chip layer" style="--c:#[0-9a-f]{6}"><i></i>', html_text)
    assert ".chip.layer i {" in html_text, "CSS 要真的消费 --c，否则圆点不显示"


def test_index_without_layer_config_has_no_empty_overview_block():
    """没有分层配置时不留「架构总览」空标题 —— 没有图就该整段消失。"""
    from codewiki.render.i18n import t

    packet = _packet_with_layers_and_edges([("pkg/a.py", "pkg/b.py")])
    html_text = render_index_html(packet)

    assert 'class="layer-map"' not in html_text
    assert t("site.overview") not in html_text, "无图时标题要一起省掉"


def test_module_cards_dim_the_directory_part():
    """卡片里目录压暗、文件名保持正文色，680 张长路径才有落点。"""
    packet = _packet_with_layers_and_edges([("pkg/sub/a.py", "pkg/sub/b.py")])
    html_text = render_index_html(packet)

    assert '<span class="dir">pkg/sub/</span>a.py' in html_text


def test_hostile_module_name_cannot_break_out_of_the_href():
    """文件名里的引号不能闭合 href 属性（仓库文件名是**不可信输入**）。

    Linux 允许 `"` / `<` 出现在文件名里，而 slug 同时用作页面名与 index.html
    的 href 片段；不设防时产物里会出现
    `<a href="modules/x"><img src=x onerror=...>.html">`。
    """
    hostile = 'x"><img src=x onerror=alert(1)>.py'
    packet = _packet_with_paths([hostile, "ok.py"])
    html_text = render_index_html(packet)

    assert "onerror=alert(1)" not in html_text.split("<code>")[0], "属性被提前闭合了"
    for chunk in html_text.split('href="')[1:]:
        href = chunk.split('"')[0]
        assert '"' not in href and "<" not in href, f"href 里出现危险字符: {href!r}"


def test_hostile_layer_name_cannot_break_out_of_the_id():
    """层名来自被分析仓库的配置，同样不可信。"""
    hostile_name = 'x" onmouseover="alert(1)'
    packet = _packet_with_paths(["m/a.py"], layers={"m/a.py": f"layer::{hostile_name}"})
    html_text = render_index_html(packet)

    assert 'onmouseover="alert(1)"' not in html_text, "层 id 闭合了属性"
    assert 'id="layer-layer::x__onmouseover=_alert(1)"' in html_text


def test_index_relative_links_stay_inside_the_output_dir():
    """index.md 里的相对链接不能越出产物目录（`../diagnostics.md` 曾是死链）。

    index.md 就在产物根，任何 `../` 都指向目录外。逐条检查比只盯着一处更耐用。
    """
    import re

    text = render_index(_packet_with_paths(["m/a.py", "n/b.py"]))

    links = re.findall(r"\]\(([^)]+)\)", text)
    assert links, "索引页应当有链接（否则本用例没测到东西）"
    for link in links:
        assert not link.startswith("../"), f"链接越出产物目录: {link!r}"
        assert "://" not in link, f"索引页不该出现外链: {link!r}"


def test_zip_does_not_follow_symlinks(tmp_path):
    """打包不能跟着符号链接走 —— 否则会把仓库外的文件内容带进产物。

    `zipfile.write` 默认写链接指向的内容（实测 `leak.txt -> /etc/passwd` 被
    原样打包进 zip）。产物目录里出现软链本身就是异常，跳过即可。
    """
    out = tmp_path / "out"
    out.mkdir()
    (out / "real.txt").write_text("ok", encoding="utf-8")
    outside_target = tmp_path / "outside.txt"
    outside_target.write_text("TOP-SECRET", encoding="utf-8")
    (out / "leak.txt").symlink_to(outside_target)

    target = tmp_path / "bundle.zip"
    write_zip(out, target)

    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        assert "real.txt" in names and "leak.txt" not in names
        assert "TOP-SECRET" not in archive.read("real.txt").decode()


def test_mermaid_initializes_with_explicit_security_level():
    """``securityLevel`` 必须显式写死，不能靠 mermaid 的默认值。

    图里的标签文本来自被分析仓库（层名、模块路径）。当前 mermaid 的默认就是
    ``strict``，所以这条断言现在**测不出安全问题**——它测的是"默认值变化时我们
    会不会发现"：升级后若默认放宽成 ``loose``，标签里的 HTML 会被当标记渲染，
    而代码一行没改、别的用例全绿。默认值不是契约，写下来才是。
    """
    html = render_module_html("# 标题\n\n正文\n", title="页面")

    assert 'securityLevel:"strict"' in html, (
        "mermaid.initialize 里没有显式的 securityLevel:'strict' —— "
        "升级 mermaid 时默认值一变，标签里的 HTML 就会被当标记渲染"
    )


def test_module_pages_carry_diagram_interactions(mini_py):
    """图表交互（P2-8）要随壳注入：悬停高亮上下游 + 点层框聚焦。

    交互脚本只依赖渲染后的 DOM id（``flowchart-<id>-<n>`` 与
    ``-L_<起>_<终>_<n>``），不碰 mermaid 内部 API —— 升级 mermaid 时
    只要这两类 id 还在，交互就还在。这里盯的是"壳里真的带了脚本与样式"。
    """
    html = render_module_html('# t\n\n```mermaid\nflowchart TD\n  a["A"]\n```\n', title="t")

    assert "cw-dim" in html, "交互样式类没进壳"
    assert "flowchart-link" in html, "没按 link id 反解端点"
    assert "g.cluster" in html, "没有点层框聚焦"
    assert 'document.addEventListener("mouseover"' in html, "必须用 document 级事件委托"
    index = render_index_html(_packet(mini_py))
    assert "pre.mermaid" not in index or "diagramOf" not in index, "index 没有 mermaid 图，别注入交互脚本"


def _packet_with_symbols(symbols):
    from codewiki.models import FileModel

    files = [FileModel(path="pkg/a.py", language="python", hash="h", symbols=symbols, imports=[], calls=[])]
    result = build_graph(files)
    return packet_from_parts(
        title="t",
        layers=[],
        nodes=result.nodes,
        edges=result.edges,
        annotations=[],
        diagnostics=result.diagnostics,
    )


def _symbol(name, kind="function", signature=None, doc=None, line=1):
    from codewiki.models import SymbolFact

    return SymbolFact(
        name=name,
        qualname=name,
        kind=kind,
        lineno=line,
        endlineno=line,
        signature=signature,
        docstring=doc,
    )


def test_symbol_table_keeps_union_types_in_their_own_cell():
    """``str | None`` 必须留在签名格里，不能把一行切成两行两列。"""
    import re

    from codewiki.render.site import md_to_html

    packet = _packet_with_symbols(
        [_symbol("find", signature="(self, ip: str) -> SwitchNode | None", doc="给定 IP 返回交换机。")]
    )
    md = render_module_page(packet, "mod::pkg/a.py", diagram_text=None)
    html = md_to_html(md)

    row = re.search(r"<tr>(?:(?!</tr>).)*find(?:(?!</tr>).)*</tr>", html, re.S).group(0)
    assert row.count("<td>") == 5, f"整行应当是 5 格，实际 {row.count('<td>')}: {row}"
    assert "<code>(self, ip: str) -&gt; SwitchNode | None</code>" in row
    assert "\\" not in row, f"转义符不该漏到页面上: {row}"


def test_symbol_table_row_overflow_is_merged_not_spilled():
    """列数异常时并进末列 —— 从前多出来的两格会撑出第三列、把表格撑破。"""
    import re

    from codewiki.render.site import md_to_html

    md = "| 名称 | 类型 | 行 | 签名 | 说明 |\n| --- | --- | --- | --- | --- |\n| `a` | function | 1 | `() -> int` | 说明甲 | 说明乙 |\n"
    html = md_to_html(md)

    row = re.search(r"<tr>(?:(?!</tr>).)*<td>(?:(?!</tr>).)*</tr>", html, re.S).group(0)
    assert row.count("<td>") == 5, row
    assert "说明乙" in row, "溢出内容不能丢"


def test_empty_signature_renders_no_bare_backticks():
    """常量没有签名：留空单元格，不写 ```` `` ````（页面上是两个裸露的反引号）。

    IPIP 实测 9025 个符号行里有 2337 行是这么显示签名的。
    """
    from codewiki.render.site import md_to_html

    packet = _packet_with_symbols([_symbol("LIMIT", kind="constant", line=3)])
    md = render_module_page(packet, "mod::pkg/a.py", diagram_text=None)
    html = md_to_html(md)

    assert "``" not in html, f"空签名还在写双双反引号:\n{md}"
    assert "<td></td>" in html


def test_symbol_table_gets_fixed_column_widths():
    """符号表按固定列宽排版：长签名只在自己那列换行，说明列不被挤成竖缝。"""
    from codewiki.render.site import md_to_html

    md = "| 名称 | 类型 | 行 | 签名 | 说明 |\n| --- | --- | --- | --- | --- |\n| `a` | function | 1 | `()` | x |\n"
    html = md_to_html(md)

    assert '<table class="sym">' in html and "table.sym { table-layout: fixed" in render_module_html(
        md, title="t"
    )


def test_docstring_double_backtick_code_spans_render_as_code():
    """reST 风格的 ``` ``literal`` ``` 要渲染成 ``<code>``，不能留两条裸反引号。

    说明栏直接取 docstring 首行，而本仓 docstring 满篇 ``` ``...`` ```
    （``_strip_fence`` 的那条就是）。只认单反引号的旧规则会把中间那一对
    当成围栏，页面上剩下两条裸露的反引号。
    """
    from codewiki.render.site import md_to_html

    md = (
        "| 名称 | 类型 | 行 | 签名 | 说明 |\n| --- | --- | --- | --- | --- |\n"
        "| `a` | function | 1 | `()` | 剥离 ``json`` 围栏。 |\n"
    )
    html = md_to_html(md)

    assert "``" not in html, html
    assert "<code>json</code>" in html


def test_code_span_content_is_literal():
    """反引号里的字符是字面量：``**kw`` 不能被粗体规则吃掉。"""
    from codewiki.render.site import md_to_html

    md = (
        "| 名称 | 类型 | 行 | 签名 | 说明 |\n| --- | --- | --- | --- | --- |\n"
        "| `a` | function | 1 | `(self, **kw)` | 透传。 |\n"
    )
    html = md_to_html(md)

    assert "<code>(self, **kw)</code>" in html, html
    assert "<strong>" not in html


def test_doc_summary_drops_section_headers_and_rest_roles():
    """说明栏取的是 docstring 摘要，不是「Args:」也不是 ``:meth:`` 前缀。

    实测 8 个模块页的说明栏只有一句 ``Args:``（docstring 首行就是字段列表的
    小标题），另有 12 处 ``:meth:`notify``` 把 sphinx 角色名原样印了出来。
    """
    from codewiki.render.wiki import _doc_summary

    assert _doc_summary("Args:\n    user_id (int): 用户 id") == ""
    assert _doc_summary(":param x: 参数") == ""
    assert _doc_summary("\n\n真正的摘要。\n\nArgs:") == "真正的摘要。"
    assert _doc_summary("与 :meth:`notify` 完全相同。") == "与 `notify` 完全相同。"
    assert _doc_summary(None) == "" and _doc_summary("   ") == ""


def test_symbol_table_uses_docstring_summary_not_field_list():
    from codewiki.render.site import md_to_html

    packet = _packet_with_symbols([_symbol("run", signature="() -> None", doc="Args:\n    x (int): 数量。")])
    md = render_module_page(packet, "mod::pkg/a.py", diagram_text=None)
    html = md_to_html(md)

    assert "Args:" not in html, md


def test_long_signature_is_clamped_and_expands_on_hover():
    """超长签名默认折行到 6 行就截断，悬停展开全文。

    服务端**不截断**：截断是 CSS 的 line-clamp，所以悬停/复制都能拿到完整签名。
    用 line-clamp 而不是 max-height：前者收在行边界并自带省略号，后者会把一行
    劈成两半，看着像渲染坏了。
    """
    from codewiki.render.site import md_to_html

    sig = "(self, a: str | None = None, b: dict[str, int] | None = None, c: list[str] | None = None) -> dict[str, int] | None"
    md = (
        "| 名称 | 类型 | 行 | 签名 | 说明 |\n| --- | --- | --- | --- | --- |\n"
        f"| `f` | method | 1 | `{sig.replace('|', chr(92) + '|')}` | 说明。 |\n"
    )
    html = md_to_html(md)
    page = render_module_html(md, title="t")

    assert "-webkit-line-clamp: 6" in page, "默认要截断到 6 行"
    assert "-webkit-line-clamp: unset" in page
    assert "c: list[str] | None = None) -&gt; dict[str, int] | None</code>" in html


def test_hover_expand_is_gated_behind_a_real_pointer():
    """悬停展开必须关在 ``@media (hover: hover)`` 里，否则触屏上点不动。

    iOS 的 tap 会顺带把 ``:hover`` 粘住：无条件 hover 规则下，第一次 tap「展开」
    其实是 hover 生效，第二次 tap 切掉 ``data-expanded`` 后 hover 仍然撑着，
    看起来就是点了没反应。
    """
    import re

    from codewiki.render.site import render_module_html

    page = render_module_html("| a |\n| --- |\n| b |\n", title="t")

    assert re.search(r"@media \(hover: hover\) \{\s*table\.sym td:nth-child\(4\) > code:hover", page), (
        "hover 规则要落在 (hover: hover) 媒体查询内"
    )
    assert "table.sym td:nth-child(4) > code[data-expanded]" in page


def test_touch_users_can_tap_a_clipped_signature_open_and_shut():
    """触屏靠点击/回车切换：只有**度量出真被截断**的签名才可点、可聚焦。"""
    from codewiki.render.site import render_module_html

    page = render_module_html("| a |\n| --- |\n| b |\n", title="t")

    assert 'document.querySelectorAll("table.sym td:nth-child(4) > code")' in page
    assert "el.scrollHeight - el.clientHeight" in page
    assert page.index("data-clipped") < page.index('el.setAttribute("tabindex", "0")')
    for hook in ('addEventListener("click"', 'addEventListener("keydown"', "aria-expanded"):
        assert hook in page, hook
    assert '"Enter"' in page and '" "' in page, "键盘要能用回车/空格切换"


def test_signature_hint_is_escaped_into_the_script_as_a_literal():
    """提示文案走 ``json.dumps`` 填进 JS：里面带引号也不会把脚本拼坏。"""
    import json
    import re

    from codewiki.render.i18n import set_lang, t
    from codewiki.render.site import render_module_html

    try:
        set_lang("en")
        page = render_module_html("| a |\n| --- |\n| b |\n", title="t")
        literal = re.search(r"var hint = (.*?);", page).group(1)
        assert json.loads(literal) == t("wiki.signature_hint")
        assert "Click to expand" in json.loads(literal)
    finally:
        set_lang("zh")

    page = render_module_html("| a |\n| --- |\n| b |\n", title="t")
    assert "点击展开完整签名" in page


def test_signature_hint_with_quotes_cannot_break_the_script(monkeypatch):
    """文案里带双引号也不能把脚本拼坏 —— 这是 ``json.dumps`` 而非手拼的意义。"""
    import json
    import re

    from codewiki.render.site import render_module_html

    monkeypatch.setattr("codewiki.render.site.t", lambda key, **fmt: 'open the "full" signature')
    page = render_module_html("| a |\n| --- |\n| b |\n", title="t")

    literal = re.search(r"var hint = (.*?);", page).group(1)
    assert json.loads(literal) == 'open the "full" signature'


def _packet_with_raw_edges(layer_specs, nodes, edges):
    """直接按给定 nodes/edges 组装 packet（不走 builder 的聚合）。"""
    return packet_from_parts(
        title="t",
        layers=[{"id": lid, "name": name, "order": i} for i, (lid, name) in enumerate(layer_specs)],
        nodes=nodes,
        edges=edges,
        annotations=[],
        diagnostics=[],
    )


def _two_layer_packet(*, layer_weight_a=52, layer_weight_b=66, calls_a=82, calls_b=76):
    from codewiki.models import Edge, EdgeType, Node, NodeType

    a, b = "layer::_internal", "layer::root"
    nodes = [
        Node(id="mod::a.py", type=NodeType.MODULE, path="a.py", layer_id=a),
        Node(id="mod::b.py", type=NodeType.MODULE, path="b.py", layer_id=b),
    ]
    edges = [
        Edge(source=a, target=b, type=EdgeType.LAYER_DEPENDS, weight=layer_weight_a),
        Edge(source=b, target=a, type=EdgeType.LAYER_DEPENDS, weight=layer_weight_b),
        *[Edge(source="mod::a.py", target="mod::b.py", type=EdgeType.CALLS) for _ in range(calls_a)],
        *[Edge(source="mod::b.py", target="mod::a.py", type=EdgeType.CALLS) for _ in range(calls_b)],
    ]
    return _packet_with_raw_edges([(a, "_internal"), (b, "root")], nodes, edges), a, b


def test_cross_layer_flows_uses_layer_depends_weights_only():
    """TOP10 只数 LAYER_DEPENDS 的 weight —— 不把 CALLS 等模块级边再累加一遍。"""
    from codewiki.render.layer_map import cross_layer_flows

    packet, a, b = _two_layer_packet()

    ranked, overflow = cross_layer_flows(packet)
    assert dict(ranked) == {(a, b): 52, (b, a): 66}, f"应与系统图一致，实际: {ranked}"
    assert overflow == 0


def test_layer_map_shows_system_diagram_numbers_not_inflated_totals():
    """首页 SVG 里出现的必须是 52/66，不能是 82/76。"""
    from codewiki.render.layer_map import render_layer_map_svg

    packet, _, _ = _two_layer_packet()
    svg = render_layer_map_svg(packet)

    assert ">52<" in svg and ">66<" in svg, svg[-1200:]
    assert ">82<" not in svg and ">76<" not in svg, "CALLS 累加后的虚高数字不该出现"


def test_band_io_counts_use_the_same_layer_depends_basis():
    """层带的「出 x · 入 y」与连线徽标也得同口径，否则同页仍有两组数字。"""
    from codewiki.render.layer_map import _layer_flow_counts

    packet, a, b = _two_layer_packet()
    io, pairs = _layer_flow_counts(packet)

    assert io[a] == (52, 66) and io[b] == (66, 52), io
    assert pairs[(a, b)] == 52 and pairs[(b, a)] == 66, pairs


def test_unlayered_band_is_hidden_when_it_contains_no_modules():
    """符号本来就没有层：「0 模块 · 2454 符号」这行是噪声，整行不画。"""
    from codewiki.models import Node, NodeType
    from codewiki.render.layer_map import render_layer_map_svg

    lid = "layer::服务层"
    nodes = [Node(id="mod::a.py", type=NodeType.MODULE, path="a.py", layer_id=lid)]
    nodes += [
        Node(id=f"sym::{i}", type=NodeType.SYMBOL, name=f"s{i}", module_id="mod::a.py") for i in range(5)
    ]
    packet = _packet_with_raw_edges([(lid, "服务层")], nodes, [])

    svg = render_layer_map_svg(packet)

    assert "0 模块 · 5 符号" not in svg, f"零模块的未分层带还在:\n{svg[:600]}"
    assert "未分层" not in svg, "不该凭空多出一条未分层层带"
