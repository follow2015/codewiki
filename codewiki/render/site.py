"""静态站点与离线打包（§4.5）。

- ``index.html``：免服务端的入口页（内嵌宏观图 + 模块清单）。
- ``modules/*.html``：模块页的渲染版（``md_to_html`` 自研零依赖渲染器，
  只覆盖自家模块页用到的 markdown 子集）。点击链接直接可读——
  此前链接指向 ``.md`` 原文，浏览器只能显示源码。
- 离线看图依赖 ``assets/mermaid.min.js``（**随包携带**，阶段三落地）。
  当前该文件不存在时：HTML 照常生成，但记 ``ASSET_MISSING``（info）——
  图以 ``.mmd`` 原文呈现，不假装能离线渲染。
- ``export --zip``：把整个产物目录原子化打成单个 Zip（标准库 zipfile）。
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from codewiki.layering import UNLAYERED_LAYER_ID
from codewiki.models import NodeType
from codewiki.render.diagrams import build_slug_map
from codewiki.render.escape import escape_attr, escape_text
from codewiki.render.i18n import html_lang, t
from codewiki.render.layer_map import layer_anchor, layer_color, render_layer_map_svg
from codewiki.render.wiki import MODULES_SUBDIR
from codewiki.schemas import DataPacket, Diagnostic

INDEX_HTML = "index.html"
MERMAID_ASSET = "assets/mermaid.min.js"

PACKAGE_ASSET = Path(__file__).resolve().parent.parent / "assets" / "mermaid.min.js"

_FENCE_RE = re.compile(r"^```(\w*)\s*$")
_TABLE_SEP_RE = re.compile(r"^\|(\s*:?-{3,}:?\s*\|)+$")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")
_LIST_RE = re.compile(r"^(\s*)-\s+(.*)$")


def _href(target: str) -> str:
    """页内链接统一改写为 .html（模块互链与返回索引）。"""
    if target.endswith(".md"):
        return target[:-3] + ".html"
    return target


def _inline(text: str) -> str:
    text = _LINK_RE.sub(lambda m: f'<a href="{_href(m.group(2))}">{m.group(1)}</a>', text)
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _CODE_RE.sub(r"<code>\1</code>", text)
    return text


def md_to_html(md: str) -> str:
    """把自家渲染器产出的 markdown 转成 HTML（子集见模块 docstring）。

    输入先整体转义再逐块处理，代码块内容不经过行内规则——杜绝把代码
    当标签渲染。mermaid 围栏输出 ``<pre class="mermaid">``，交给
    mermaid.js 现场成图。
    """
    out: list[str] = []
    lines = md.splitlines()
    i = 0
    para: list[str] = []

    def flush_para() -> None:
        if para:
            out.append(f"<p>{_inline(' '.join(para))}</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        fence = _FENCE_RE.match(line)
        if fence:
            flush_para()
            lang = fence.group(1)
            body: list[str] = []
            i += 1
            while i < len(lines) and not _FENCE_RE.match(lines[i]):
                body.append(lines[i])
                i += 1
            i += 1
            text = escape_text("\n".join(body))
            if lang == "mermaid":
                out.append(f'<pre class="mermaid">{text}\n</pre>')
            else:
                out.append(f"<pre><code>{text}\n</code></pre>")
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            flush_para()
            level = min(len(heading.group(1)), 6)
            out.append(f"<h{level}>{_inline(escape_text(heading.group(2)))}</h{level}>")
            i += 1
            continue

        if line.strip() == "---":
            flush_para()
            out.append("<hr>")
            i += 1
            continue

        if line.startswith("|"):
            flush_para()
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].startswith("|"):
                if not _TABLE_SEP_RE.match(lines[i]):
                    cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                    rows.append([_inline(escape_text(c)) for c in cells])
                i += 1
            if rows:
                head, *body_rows = rows
                parts = ["<table>", "<thead>", "<tr>"]
                parts += [f"<th>{c}</th>" for c in head]
                parts += ["</tr></thead>"]
                if body_rows:
                    parts.append("<tbody>")
                    for r in body_rows:
                        parts.append("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>")
                    parts.append("</tbody>")
                parts.append("</table>")
                out.append("".join(parts))
            continue

        listed = _LIST_RE.match(line)
        if listed:
            flush_para()
            while i < len(lines) and (m := _LIST_RE.match(lines[i])):
                out.append(f"<li>{_inline(escape_text(m.group(2)))}</li>")
                i += 1
            continue

        if not line.strip():
            flush_para()
            i += 1
            continue

        para.append(escape_text(line))
        i += 1

    flush_para()
    return "\n".join(out)


_PAGE_STYLES = """  :root {
    --bg: #f7f8fa; --panel: #ffffff; --panel-2: #f1f3f7;
    --text: #17202b; --muted: #5d6b7c; --faint: #8b97a6;
    --border: #e3e7ee; --border-strong: #cfd6e0;
    --accent: #4f46e5; --accent-soft: #eef2ff; --accent-text: #3730a3;
    --code-bg: #0f1420; --code-text: #dbe2f0;
    --mono: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace;
    --sans: -apple-system, "SF Pro Text", "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    --radius: 10px; --radius-sm: 7px;
    --shadow: 0 1px 2px rgba(16,24,40,.05), 0 1px 3px rgba(16,24,40,.06);
    --maxw: 1080px;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0c1017; --panel: #131926; --panel-2: #1a2233;
      --text: #e4eaf3; --muted: #97a3b5; --faint: #6b7787;
      --border: #232d3f; --border-strong: #34405a;
      --accent: #818cf8; --accent-soft: #1c2340; --accent-text: #b3bcf7;
      --code-bg: #0a0e18; --code-text: #cdd6e6;
      --shadow: 0 1px 2px rgba(0,0,0,.4);
    }
  }
  * { box-sizing: border-box; }
  body {
    font-family: var(--sans); background: var(--bg); color: var(--text);
    margin: 0; line-height: 1.65; font-size: 15px;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: var(--maxw); margin: 0 auto; padding: 2.5rem 1.5rem 4rem; }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; text-underline-offset: 3px; }
  code { font-family: var(--mono); font-size: .875em; }
  p > code, li > code, td > code, h1, h2 code, th > code {
    background: var(--panel-2); border: 1px solid var(--border);
    border-radius: 5px; padding: .1em .35em;
  }
  h1 { font-family: var(--mono); font-size: 1.3rem; letter-spacing: -.01em; }
  h2 {
    font-size: 1.02rem; margin: 2.2rem 0 .8rem; padding-bottom: .45rem;
    border-bottom: 1px solid var(--border); letter-spacing: .01em;
  }
  h3 { font-size: .95rem; margin: 1.6rem 0 .6rem; }

  /* ── hero ── */
  .hero { padding: 1.2rem 0 1.6rem; border-bottom: 1px solid var(--border); }
  .hero h1 { font-family: var(--sans); font-size: 1.65rem; font-weight: 700; margin: 0 0 .7rem; }
  .chips { display: flex; flex-wrap: wrap; gap: .45rem; margin: 0 0 .7rem; }
  .chip {
    font-size: .78rem; color: var(--muted); background: var(--panel);
    border: 1px solid var(--border); border-radius: 999px; padding: .18rem .7rem;
  }
  .chip b { color: var(--text); font-weight: 600; }
  .chip.err { color: #dc2626; border-color: #fecaca; background: #fef2f2; }
  .hero .links { font-size: .85rem; color: var(--muted); }
  @media (prefers-color-scheme: dark) {
    .chip.err { color: #f87171; border-color: #7f1d1d; background: #2a1215; }
  }

  /* ── toolbar (search) ── */
  .layer-map {
    margin: 1rem 0 .4rem; overflow-x: auto;
  }
  .layer-group { scroll-margin-top: 1rem; }
  .toolbar { margin: 1.6rem 0 1.2rem; }
  .toolbar input {
    width: 100%; font: inherit; font-size: .9rem; color: var(--text);
    background: var(--panel); border: 1px solid var(--border-strong);
    border-radius: var(--radius-sm); padding: .55rem .9rem; outline: none;
  }
  .toolbar input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }

  /* ── layer groups + module cards ── */
  .layer-group { margin: 1.5rem 0 2rem; }
  .layer-head {
    display: flex; align-items: center; gap: .55rem;
    font-size: .95rem; font-weight: 650; margin: 0 0 .7rem;
  }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--c, var(--faint)); flex: none; }
  .count {
    font-size: .74rem; font-weight: 500; color: var(--muted);
    background: var(--panel-2); border-radius: 999px; padding: .05rem .55rem;
  }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: .55rem; }
  .mod-card {
    display: block; font-family: var(--mono); font-size: .8rem; color: var(--text);
    background: var(--panel); border: 1px solid var(--border);
    border-radius: var(--radius-sm); padding: .55rem .8rem;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    box-shadow: var(--shadow); transition: border-color .12s, transform .12s;
  }
  .mod-card:hover {
    text-decoration: none; border-color: var(--accent);
    transform: translateY(-1px); color: var(--accent-text);
  }
  .mod-card code { background: none; border: none; padding: 0; }

  /* ── body (module / diagnostics pages) ── */
  .md-body {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: var(--radius); box-shadow: var(--shadow);
    padding: 1.8rem 2rem; margin-top: 1.5rem;
  }
  .md-body > :first-child { margin-top: 0; }
  .md-body > p:first-of-type { color: var(--muted); font-size: .85rem; margin-top: -.4rem; }
  .md-body > p:last-of-type > a { font-size: .85rem; }
  .md-body > h2:first-of-type { margin-top: 1.2rem; }

  pre {
    background: var(--code-bg); color: var(--code-text);
    border-radius: var(--radius-sm); padding: .9rem 1.1rem;
    overflow: auto; font-size: .8rem; line-height: 1.55;
  }
  pre code { background: none; border: none; padding: 0; color: inherit; }
  pre.mermaid {
    background: var(--panel); color: var(--text);
    border: 1px solid var(--border); display: flex; justify-content: center;
  }

  table { border-collapse: collapse; width: 100%; font-size: .85rem; margin: .6rem 0 1rem; }
  th, td { border: 1px solid var(--border); padding: .42rem .7rem; text-align: left; vertical-align: top; }
  thead th { background: var(--panel-2); font-weight: 600; font-size: .78rem; }
  tbody tr:nth-child(even) { background: color-mix(in srgb, var(--panel-2) 45%, transparent); }

  li { margin: .25rem 0; }
  ul, ol { padding-left: 1.3rem; }
  hr { border: none; border-top: 1px solid var(--border); margin: 1.6rem 0; }
  @media (max-width: 640px) { .wrap { padding: 1.4rem 1rem 3rem; } .md-body { padding: 1.2rem 1.1rem; } }"""

_MERMAID_INIT = (
    "<script>mermaid.initialize({startOnLoad:true,"
    "maxTextSize:2000000,maxEdges:20000,"
    'layout:"dagre",look:"classic",'
    'securityLevel:"strict",'
    'theme:matchMedia("(prefers-color-scheme: dark)").matches?"dark":"neutral"});</script>\n'
)


def render_module_html(md: str, *, title: str, asset_prefix: str = "") -> str:
    """模块页 HTML：md_to_html 渲染 + 站点壳（设计系统样式与 mermaid 资产）。

    ``asset_prefix``：页面相对产物根的深度修正 —— ``modules/`` 下的页面
    必须用 ``../``（否则脚本按 ``modules/assets/…`` 解析 404，mermaid 不
    加载，图只能以源码示人）；根下的页面（diagnostics.html）用默认 ``""``。
    """
    body = md_to_html(md)
    safe_title = escape_text(title)
    return (
        "<!doctype html>\n"
        f'<html lang="{html_lang()}">\n<head>\n<meta charset="utf-8" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        f"<title>{safe_title} · codewiki</title>\n<style>\n{_PAGE_STYLES}</style>\n"
        "</head>\n<body>\n"
        '  <div class="wrap">\n'
        f'  <main class="md-body">\n{body}\n  </main>\n'
        f'  </div>\n  <script src="{asset_prefix}{MERMAID_ASSET}"></script>\n'
        f"  {_MERMAID_INIT}"
        "</body>\n</html>\n"
    )


def write_module_html(out_dir: Path) -> int:
    """把 modules/*.md 与 diagnostics.md 逐个渲染成 .html（幂等、增量安全）。

    直接以**已落盘**的 .md 为源（而不是渲染时顺带写），增量复用旧页时
    HTML 也能补齐；返回写的页数。资源前缀按页面深度给：modules/ 下的
    页面 ``../``，根下的页面 ``""``。
    """
    written = 0
    modules_dir = out_dir / MODULES_SUBDIR
    targets = [(p, "..") for p in sorted(modules_dir.glob("*.md"))]
    diag = out_dir / "diagnostics.md"
    if diag.exists():
        targets.append((diag, "."))
    for md_path, depth in targets:
        title = md_path.stem
        prefix = "" if depth == "." else "../"
        html_path = md_path.with_suffix(".html")
        page = render_module_html(md_path.read_text(encoding="utf-8"), title=title, asset_prefix=prefix)
        html_path.write_text(page, encoding="utf-8")
        written += 1
    return written


def prune_stale_pages(out_dir: Path, valid_stems: set[str], *, with_diagrams: bool = True) -> int:
    """清掉上一轮残留的过期页面（模块删除/改名/口径变化后必然出现）。

    ``valid_stems`` 是**当前**数据包的模块 slug 全集；modules/ 里凡不在
    集合内的 .md/.html 都是陈旧产物（IPIP 实测口径收紧后残留 111 页），
    一并清掉对应 .mmd 图，避免 wiki 越积越陈旧、搜索命中死页。
    返回删除的文件数。
    """
    removed = 0
    modules_dir = out_dir / MODULES_SUBDIR
    if modules_dir.is_dir():
        for p in sorted(modules_dir.iterdir()):
            if p.suffix in (".md", ".html") and p.stem not in valid_stems:
                p.unlink()
                removed += 1
    if with_diagrams:
        diagrams_dir = out_dir / "diagrams" / MODULES_SUBDIR
        if diagrams_dir.is_dir():
            for p in sorted(diagrams_dir.iterdir()):
                if p.suffix == ".mmd" and p.stem not in valid_stems:
                    p.unlink()
                    removed += 1
    return removed


def render_index_html(packet: DataPacket) -> str:
    """生成 index.html（自包含、无需服务端）。

    架构总览 = 分层架构地图（内联 SVG，见 ``render/layer_map.py``）——
    参考手工结构图的表达方式，不再嵌自动布局的 mermaid 巨图。模块按层
    分组为卡片网格（层色点 + 计数），配一个零依赖的实时筛选框；链接指向
    渲染后的 ``modules/<slug>.html``，文件名与落盘页面同源（同一张
    build_slug_map）——点开即读，碰撞路径不会互相覆盖。
    """
    safe_title = escape_text(packet.meta.title)
    modules = sorted(
        (n for n in packet.nodes if n.type == NodeType.MODULE and n.path),
        key=lambda n: n.path or "",
    )

    layer_map_svg = render_layer_map_svg(packet)

    slug_map = build_slug_map([n.path for n in modules if n.path])

    layer_names = {layer.id: layer.name for layer in packet.layers}
    layer_orders = {layer.id: (layer.order if layer.order is not None else 10_000) for layer in packet.layers}
    groups: dict[str, list] = {}
    for n in modules:
        lid = n.layer_id or UNLAYERED_LAYER_ID
        groups.setdefault(lid, []).append(n)

    def _group_html(lid: str, members: list) -> str:
        name = layer_names.get(
            lid,
            t("common.unlayered") if lid == UNLAYERED_LAYER_ID else lid.rsplit("::", 1)[-1],
        )
        color = layer_color(name)
        cards = "\n".join(
            f'    <a class="mod-card" href="{MODULES_SUBDIR}/{escape_attr(slug_map[m.path])}.html">'
            f"<code>{escape_text(m.path or '')}</code></a>"
            for m in members
        )
        return (
            f'  <section class="layer-group" id="{escape_attr(layer_anchor(lid))}">\n'
            f'    <h2 class="layer-head"><span class="dot" style="--c:{color}"></span>'
            f'{escape_text(name)} <span class="count" data-count="{len(members)}">{len(members)}</span></h2>\n'
            f'    <div class="grid">\n{cards}\n    </div>\n  </section>'
        )

    ordered_ids = sorted(groups, key=lambda g: (layer_orders.get(g, 10_000), g == UNLAYERED_LAYER_ID, g))
    sections = "\n".join(_group_html(lid, groups[lid]) for lid in ordered_ids)

    errors = [d for d in packet.diagnostics if d.level == "error"]
    err_chip = f'<span class="chip err"><b>{len(errors)}</b> error</span>' if errors else ""
    chips = (
        f'<span class="chip"><b>{len(modules)}</b> {t("site.chip_modules")}</span>'
        f'<span class="chip"><b>{len(packet.nodes)}</b> {t("site.chip_nodes")}</span>'
        f'<span class="chip"><b>{len(packet.edges)}</b> {t("site.chip_edges")}</span>{err_chip}'
    )
    layers_meta = "".join(
        f'<span class="chip" style="--c:{layer_color(layer_names[lid])}">{len(groups[lid])} {escape_text(layer_names.get(lid, lid))}</span>'
        for lid in ordered_ids
        if lid in layer_names
    )

    return f"""<!doctype html>
<html lang="{html_lang()}">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{safe_title} · codewiki</title>
<style>
{_PAGE_STYLES}</style>
</head>
<body>
  <div class="wrap">
  <header class="hero">
    <h1>{safe_title}</h1>
    <div class="chips">{chips}</div>
    <div class="chips">{layers_meta}</div>
    <p class="links"><a href="data/codewiki.json">{t("site.data_packet")}</a> · <a href="diagnostics.html">{t("site.diagnostics")}</a></p>
  </header>
  <h2>{t("site.overview")}</h2>
  <div class="layer-map">
{layer_map_svg}
  </div>
  <div class="toolbar">
    <input id="module-search" type="search" placeholder="{t("site.search_placeholder")}" />
  </div>
{sections}
  </div>
  <script>
    const q = document.getElementById('module-search');
    q.addEventListener('input', () => {{
      const s = q.value.toLowerCase();
      document.querySelectorAll('.mod-card').forEach(a => {{
        a.style.display = a.textContent.toLowerCase().includes(s) ? '' : 'none';
      }});
      document.querySelectorAll('.layer-group').forEach(g => {{
        const visible = [...g.querySelectorAll('.mod-card')].some(a => a.style.display !== 'none');
        g.style.display = visible ? '' : 'none';
      }});
    }});
  </script>
</body>
</html>
"""


def asset_diagnostics(out_dir: Path) -> list[Diagnostic]:
    """离线看图资源是否就位（缺失只提示，不阻断）。"""
    if (out_dir / MERMAID_ASSET).exists():
        return []
    return [
        Diagnostic(
            level="info",
            code="ASSET_MISSING",
            message=f"缺少 {MERMAID_ASSET}：离线看图不可用（阶段三随包携带；当前图以 .mmd 原文呈现）",  # i18n-ignore
        )
    ]


def write_zip(out_dir: Path, target: Path) -> Path:
    """把产物目录打成单个 Zip（原子化：先写临时文件再替换）。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(out_dir.rglob("*")):
            if path.is_dir() or path == tmp or path.name.endswith(".tmp"):
                continue
            if path.is_symlink():
                continue
            archive.write(path, path.relative_to(out_dir).as_posix())
    tmp.replace(target)
    return target


__all__ = [
    "INDEX_HTML",
    "MERMAID_ASSET",
    "PACKAGE_ASSET",
    "asset_diagnostics",
    "copy_offline_asset",
    "default_asset_source",
    "render_index_html",
    "serve_preview",
    "write_zip",
]


def default_asset_source() -> Path:
    """包内自带的 mermaid 运行时路径（可能不存在）。"""
    return PACKAGE_ASSET


def copy_offline_asset(out_dir: Path, *, source: Path | None = None) -> Path | None:
    """把 mermaid 运行时复制进产物；没有就返回 ``None``（不伪造）。

    **复制而不是引用**：产物要能单独拷走（Zip / 内网共享），引用包路径会在
    另一台机器上失效。
    """
    import shutil

    src = Path(source) if source else PACKAGE_ASSET
    if not src.is_file():
        return None
    dst = out_dir / MERMAID_ASSET
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return dst


def serve_preview(directory: Path, port: int = 8000) -> tuple[object, int]:
    """起一个本地静态预览服务（``codewiki serve``）。

    返回 ``(服务器对象, 实际端口)`` —— port=0 时由内核分配，调用方要用返回值
    里的端口而不是入参。
    """
    import functools
    import http.server

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    return httpd, int(httpd.server_address[1])
