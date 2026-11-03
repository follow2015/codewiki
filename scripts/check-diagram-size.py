#!/usr/bin/env python
"""渲染每张 .mmd 并报告高度超限的图（图表优化 P2-9）。

analyze 阶段没有浏览器，像素高度只有在真实渲染后才存在 —— 所以这件事
不在 render_all 里"估"，而是在这里**量**：用系统 Chrome + 包内离线的
``assets/mermaid.min.js`` 打开一个本地页面渲染每张图，读回 ``getBBox()``。
高度超过 ``--limit``（默认 4000px）的图会打印一条 ``DIAGRAM_TOO_LARGE``，
提示改看层图，进程以非零码退出（可当 CI 门禁用）。

用法::

    python scripts/check-diagram-size.py <目录或 .mmd 文件...> [--limit 4000]

Chrome 定位顺序：``--chrome`` 参数 > ``CW_CHROME`` 环境变量 > macOS 默认路径。
"""

from __future__ import annotations

import argparse
import html
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LIMIT = 4000

RESULT_RE = re.compile(r'<pre id="result">(.*?)</pre>', re.S)

_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>body{{margin:0;background:#fff}}</style>
<script src="{lib}"></script>
</head><body>
<pre id="result">pending</pre>
<div id="host"></div>
<script>
const src = {src};
mermaid.initialize({{ startOnLoad: false, theme: "default" }});
(async () => {{
  try {{
    const {{ svg }} = await mermaid.render("g1", src);
    document.getElementById("host").innerHTML = svg;
    const el = document.querySelector("#host svg");
    let w = 0, h = 0;
    try {{ const box = el.getBBox(); w = Math.ceil(box.width); h = Math.ceil(box.height); }} catch (e) {{}}
    if (!w || !h) {{
      const vb = (el.getAttribute("viewBox") || "").split(/\\s+/);
      if (vb.length === 4) {{ w = Math.ceil(+vb[2]); h = Math.ceil(+vb[3]); }}
    }}
    document.getElementById("result").textContent = "SIZE " + w + " " + h;
  }} catch (err) {{
    document.getElementById("result").textContent = "ERROR " + err;
  }}
}})();
</script></body></html>
"""


@dataclass(frozen=True)
class Measurement:
    """一张图的真实渲染尺寸。"""

    path: Path
    width: int
    height: int


def mermaid_asset() -> Path:
    """包内自带的离线 mermaid 运行时（与站点用同一份）。"""
    here = Path(__file__).resolve().parent.parent
    asset = here / "codewiki" / "assets" / "mermaid.min.js"
    if not asset.exists():
        raise SystemExit(f"找不到 {asset}；先运行 scripts/fetch-mermaid-asset.sh")
    return asset


def find_chrome(explicit: str | None = None) -> str:
    """定位 Chrome 本体。mmdc/puppeteer 起不来时这是唯一的渲染出口。"""
    candidates = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("CW_CHROME")
    if env:
        candidates.append(env)
    candidates.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise SystemExit("找不到 Chrome；用 --chrome 或 CW_CHROME 指定可执行文件路径")


def measure(source: Path, *, chrome: str, lib: Path) -> Measurement | str:
    """渲染一张图并返回宽高；渲染失败时返回错误文本。"""
    body = source.read_text(encoding="utf-8")
    page = _TEMPLATE.format(lib=lib.as_uri(), src=_js_string(body))
    tmp = Path(tempfile.gettempdir()) / "cw-diagram-size.html"
    tmp.write_text(page, encoding="utf-8")
    try:
        proc = subprocess.run(
            [
                chrome,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--virtual-time-budget=8000",
                "--dump-dom",
                tmp.as_uri(),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return "渲染超时（180s）"
    match = RESULT_RE.search(proc.stdout or "")
    if not match:
        return f"取不到渲染结果 rc={proc.returncode}"
    text = html.unescape(match.group(1)).strip()
    if not text.startswith("SIZE "):
        return text[:160]
    try:
        _, width, height = text.split()
        return Measurement(source, int(width), int(height))
    except ValueError:
        return f"结果不可解析: {text[:160]}"


def _js_string(text: str) -> str:
    return "`" + text.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${") + "`"


def collect_too_large(
    measurements: list[tuple[Path, int, int] | str],
    *,
    limit: int,
) -> list[tuple[Path, int, int]]:
    """从量测结果里挑出超限的图。独立成函数是为了可以在没有浏览器时单测。"""
    large: list[tuple[Path, int, int]] = []
    for item in measurements:
        if isinstance(item, str):
            continue
        path, width, height = item
        if height > limit:
            large.append((path, width, height))
    return large


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("paths", nargs="+", help=" diagrams 目录或 .mmd 文件")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="高度告警线（px）")
    parser.add_argument("--chrome", default=None, help="Chrome 可执行文件路径")
    args = parser.parse_args(argv)

    targets: list[Path] = []
    for raw in args.paths:
        path = Path(raw)
        targets.extend(sorted(path.rglob("*.mmd")) if path.is_dir() else [path])
    if not targets:
        print("没有找到 .mmd 文件")
        return 2

    chrome = find_chrome(args.chrome)
    lib = mermaid_asset()
    measurements: list[tuple[Path, int, int] | str] = []
    for path in targets:
        result = measure(path, chrome=chrome, lib=lib)
        if isinstance(result, Measurement):
            measurements.append((result.path, result.width, result.height))
            mark = " ⚠" if result.height > args.limit else ""
            print(f"{path.name:36s} {result.width}x{result.height}{mark}")
        else:
            measurements.append(result)
            print(f"{path.name:36s} 量测失败: {result}")

    failed = sum(1 for item in measurements if isinstance(item, str))
    large = collect_too_large(measurements, limit=args.limit)
    for path, width, height in large:
        print(
            f"DIAGRAM_TOO_LARGE {path}: {width}x{height}px 超过 {args.limit}px，"
            f"建议改看分层的层图（diagrams/layers/）或调大 --limit"
        )
    if failed:
        print(f"{failed} 张图量测失败")
        return 2
    return 1 if large else 0


if __name__ == "__main__":
    raise SystemExit(main())
