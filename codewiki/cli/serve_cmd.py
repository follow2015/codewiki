"""``codewiki serve``：本地静态预览（§六 serve）。

只做**静态**服务：不解析模板、不执行脚本，因为产物本来就是纯静态目录。这条
不做的事比做的事重要 —— 一个"顺手支持模板"的预览服务器会把产物目录变成执行面。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from codewiki.render.site import serve_preview


def serve(args: argparse.Namespace) -> int:
    directory = Path(args.dir).resolve()
    if not directory.is_dir():
        print(f"error: 产物目录不存在: {directory}", file=sys.stderr)
        return 2
    httpd, port = serve_preview(directory, args.port)
    print(f"预览：http://127.0.0.1:{port}/ （{directory}）—— Ctrl-C 结束")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0
