"""离线资源、本地预览与大图分包判据（§4.3 大图治理 / §六 serve）。

三件事各自的"不能假装"：

1. **离线资源**：有就复制进产物（产物要能单独拷走），没有就记诊断 ——
   不写一个指向包路径的引用，那样的产物换个机器就废了；
2. **预览服务**：`--port 0` 时真实端口由内核分配，必须回传**实际**端口，
   否则打印出来的地址根本连不上；
3. **大图分包**：超限时**不产出单张巨型图**（渲染必崩），改出分页 + 索引页，
   索引要给出下钻链接（否则读者只能靠猜翻文件）。
"""
from __future__ import annotations

import threading
import urllib.request

import pytest

from codewiki.models import CONF_STATIC, Edge, EdgeType, Node, NodeType
from codewiki.render.diagrams import (
    SYSTEM_DIAGRAM,
    SYSTEM_INDEX,
    SYSTEM_STEM,
    render_all,
    render_system_index,
)
from codewiki.render.jsonio import packet_from_parts
from codewiki.render.site import (
    MERMAID_ASSET,
    asset_diagnostics,
    copy_offline_asset,
    serve_preview,
)


@pytest.mark.guarantee("offline-diagram")
def test_asset_is_copied_into_the_product(tmp_path):
    source = tmp_path / "mermaid.min.js"
    source.write_text("/* mermaid */", encoding="utf-8")
    out = tmp_path / "wiki-output"

    copied = copy_offline_asset(out, source=source)

    assert copied == out / MERMAID_ASSET and copied.is_file()
    assert asset_diagnostics(out) == []


@pytest.mark.guarantee("no-fake-asset")
def test_missing_asset_is_reported_not_faked(tmp_path):
    assert copy_offline_asset(tmp_path, source=tmp_path / "nope.js") is None
    issues = asset_diagnostics(tmp_path)
    assert [i.code for i in issues] == ["ASSET_MISSING"]


def test_serve_returns_the_real_port(tmp_path):
    """port=0 ⇒ 内核分配，必须回传真实端口（打印假端口等于给个连不上的地址）。"""
    (tmp_path / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
    httpd, port = serve_preview(tmp_path, 0)

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/index.html", timeout=5) as response:  # noqa: S310
            body = response.read().decode("utf-8")
    finally:
        httpd.shutdown()
        httpd.server_close()

    assert port > 0 and "hi" in body


def _packet(module_count: int, *, edges: list[tuple[str, str]] | None = None):
    nodes = [
        Node(id=f"mod::m{i}.py", type=NodeType.MODULE, path=f"m{i}.py", layer_id="layer::a")
        for i in range(module_count)
    ]
    return packet_from_parts(
        title="big",
        layers=[],
        nodes=nodes,
        edges=[
            Edge(source=f"mod::{a}", target=f"mod::{b}", type=EdgeType.IMPORTS, confidence=CONF_STATIC)
            for a, b in (edges or [])
        ],
        annotations=[],
        diagnostics=[],
    )


def test_large_graph_is_paged_not_rendered_as_one(tmp_path):
    packet = _packet(5)
    result = render_all(packet, tmp_path, max_nodes=2)

    paged = sorted(p.name for p in (tmp_path / "diagrams").glob(f"{SYSTEM_STEM}-part-*.mmd"))
    assert paged, "超限时必须分页，而不是产出一张巨型图"
    assert not (tmp_path / "diagrams" / SYSTEM_DIAGRAM).exists(), "大图不该再写单张总览"
    assert [d.code for d in result.diagnostics] == ["DIAGRAM_PAGED"]
    assert result.diagnostics[0].level == "info"


def test_small_graph_stays_single(tmp_path):
    render_all(_packet(3), tmp_path, max_nodes=10)
    assert (tmp_path / "diagrams" / SYSTEM_DIAGRAM).exists()


def test_index_page_links_to_module_diagrams(tmp_path):
    packet = _packet(4)
    result = render_all(packet, tmp_path, max_nodes=2)

    index_text = (tmp_path / "diagrams" / SYSTEM_INDEX).read_text(encoding="utf-8")
    assert "下钻" in index_text and "modules/m0.mmd" in index_text
    assert any(SYSTEM_INDEX in str(p) for p in result.files)


def test_paging_covers_every_module(tmp_path):
    """分页不能丢模块：并集必须等于全集（否则诊断会静默少报）。"""
    packet = _packet(7)
    result = render_all(packet, tmp_path, max_nodes=3)

    assert len([p for p in result.files if "part-" in p.name]) == 3
    index_text = (tmp_path / "diagrams" / SYSTEM_INDEX).read_text(encoding="utf-8")
    for i in range(7):
        assert f"`m{i}.py`" in index_text


@pytest.mark.parametrize("max_nodes", [1, 2, 3])
def test_render_system_index_mentions_the_limit(max_nodes):
    text = render_system_index(_packet(4), [{"mod::m0.py"}], max_nodes=max_nodes)
    assert str(max_nodes) in text
