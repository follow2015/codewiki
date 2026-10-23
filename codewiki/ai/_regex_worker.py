"""正则扫描子进程（ReDoS 防线的最后一道闸，外部评审 2026-10-23）。

**为什么必须是子进程**：``re`` 的匹配在 C 层执行期间**不释放 GIL**，主线程连
``join(timeout)`` 都醒不过来 —— 线程超时方案实测会把整场 pytest 挂死。进程级
超时是唯一能真正抢回控制权的做法（``subprocess.run(timeout=…)`` → SIGKILL）。

协议：stdin 收一个 JSON 作业，stdout 吐一个 JSON 命中数组。

- 作业：``{"pattern": str, "root": str, "files": [相对路径…],
  "max_line_chars": int, "max_hits": int}``
- 命中：``[{"path": 相对路径, "line": int, "text": str}, …]``

路径由父进程过完 jail 再传进来；本模块只做"按行匹配"这一件事，不解析路径、
不碰网络、不写文件 —— 子进程里跑的代码越少，能被利用的面越小。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def main() -> int:
    try:
        job = json.load(sys.stdin)
        pattern = re.compile(job["pattern"])
        root = Path(job["root"])
        files = job["files"]
        max_line_chars = int(job["max_line_chars"])
        max_hits = int(job["max_hits"])
    except Exception as exc:  # noqa: BLE001 - 协议错误一律回结构化失败，不抛栈
        json.dump({"error": f"作业解析失败: {exc}"}, sys.stdout)
        return 2

    hits: list[dict] = []
    for rel in files:
        try:
            content = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            if pattern.search(line[:max_line_chars]):
                hits.append({"path": rel, "line": lineno, "text": line.strip()[:200]})
                if len(hits) >= max_hits:
                    json.dump(hits, sys.stdout)
                    return 0
    json.dump(hits, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
