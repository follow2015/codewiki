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
