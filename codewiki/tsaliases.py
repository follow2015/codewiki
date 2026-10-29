"""从 ``tsconfig.json`` 推断 TS 导入别名，供起草配置时写进 ``[analysis] ts_aliases``。

为什么需要这一步：前端代码几乎不用相对路径，一律写 ``@/theme/dark``。没有别名表，
``builder._resolve_js_specifier`` 一条都解析不出来 —— 实测 ipip arch-e0 的 610 个
前端模块，模块间 IMPORTS 边是 **0**，``suggest-layers`` 里前端各组内聚度全 0%。

为什么 builder 自己不读 tsconfig：``analyze`` 分析的是**可能不可信**的仓库，读它的
配置等于让被分析对象决定解析口径 —— 猜错会把 ``@tanstack/react-query`` 这类 scoped
包吞成本地模块，比"没连"更难发现。所以那条路只认显式配置。

而 ``init`` 是本地起草命令：**读出来给人看**，写进配置后人可以删改。立场不同，
处理方式也不同。为了让"读"不出错，这里只保留目标路径**真实存在**的别名 ——
``@tanstack/*`` 的 targets 在仓库里找不到，自然被丢掉。

tsconfig 是 JSONC（带注释、可能带尾逗号），标准库 ``json`` 直接解析会失败，所以
先剥注释。剥的时候必须跳过字符串字面量：``"https://x"`` 里的 ``//`` 不是注释。
"""

from __future__ import annotations

import json
import posixpath
from pathlib import Path

MAX_DEPTH = 3

_SKIP_DIRS = frozenset({"node_modules", "dist", "build", "__pycache__"})


def strip_jsonc(text: str) -> str:
    """剥掉 ``//`` 与 ``/* */`` 注释，保留字符串字面量里的内容。

    逐字符扫，维护"是否在字符串内"与"是否刚读到转义符"两个状态。用正则一次性
    替换会误伤 ``"https://example.com"`` 这类值 —— 而 tsconfig 里恰好常有 URL。
    """
    out: list[str] = []
    index, length = 0, len(text)
    in_string = False
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(text[index + 1])
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if text.startswith("//", index):
            newline = text.find("\n", index)
            index = length if newline == -1 else newline
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = length if end == -1 else end + 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def load_jsonc(path: Path) -> dict | None:
    """读一个 JSONC 文件；解析不了返回 ``None``（起草不该因为一个坏 tsconfig 中断）。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        value = json.loads(strip_jsonc(text))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _tsconfig_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("tsconfig*.json")):
        rel = path.relative_to(root)
        if len(rel.parts) > MAX_DEPTH:
            continue
        if any(part in _SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
            continue
        files.append(path)
    return files


def detect_aliases(root: Path) -> dict[str, str]:
    """``{"@/*": "frontend/src/*"}`` —— 只保留目标真实存在的别名。

    ``baseUrl`` 相对 tsconfig 所在目录，最终统一折算成**仓库根相对路径**（本仓配置
    的 ``ts_aliases`` 就是这个口径）。
    """
    aliases: dict[str, str] = {}
    for path in _tsconfig_files(root):
        data = load_jsonc(path)
        if not data:
            continue
        options = data.get("compilerOptions")
        if not isinstance(options, dict):
            continue
        paths = options.get("paths")
        if not isinstance(paths, dict):
            continue
        base_url = options.get("baseUrl")
        base = base_url if isinstance(base_url, str) else "."
        config_dir = path.parent.relative_to(root).as_posix()

        for pattern, targets in paths.items():
            if not isinstance(pattern, str) or not isinstance(targets, list) or not targets:
                continue
            target = targets[0]
            if not isinstance(target, str):
                continue
            joined = posixpath.normpath(posixpath.join(config_dir, base, target))
            if joined.startswith(".."):
                continue
            probe = joined.split("*", 1)[0].rstrip("/")
            if probe and not (root / probe).exists():
                continue
            aliases.setdefault(pattern, joined)
    return aliases


__all__ = ["MAX_DEPTH", "detect_aliases", "load_jsonc", "strip_jsonc"]
