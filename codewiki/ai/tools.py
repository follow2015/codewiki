"""Agent 工具集（§5.1）：只读、可审计、不可越权。

五件套里 ``execute_shell`` 在 :mod:`codewiki.ai.sandbox`，``write_toml`` 在
:mod:`codewiki.ai.annotate`（它受 write_back 策略控制，不是自由写文件）。

三条贯穿始终的纪律：

1. **路径 jail**：``read_file`` 与 shell 共用同一把锁 —— 相对路径按 ``repo_root``
   解析，绝对路径与 ``..`` 都要归一化后判断是否仍在 root 内；
2. **代码即不可信数据**：读出来的内容一律用 :func:`wrap_untrusted` 包裹，并过
   :func:`scan_prompt_injection`（§5.4 第 6 条）。忽略扫描结果等于把仓库内容
   当成指令执行，这是本工具最主要的越权面；
3. **密钥不进模型**（外部评审 2026-10-23）：``read_file`` 能读到仓库里的
   ``.env`` —— 内容发给模型前必须脱敏（脱敏不能只做在审计日志上）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from codewiki.ai.audit import redact
from codewiki.ai.sandbox import ShellDenied, execute_shell

DEFAULT_MAX_CHARS = 24_000
DEFAULT_MAX_HITS = 50
MAX_PATTERN_CHARS = 500

DELIM_BEGIN = "<<<CODEWIKI_UNTRUSTED_DATA>>>"
DELIM_END = "<<<END_CODEWIKI_UNTRUSTED_DATA>>>"

_DELIM_NEUTERED = "[CODEWIKI 数据分隔符已移除]"
_DELIM_VARIANT = re.compile(r"(?i)<<<\s*(?:end_)?codewiki_untrusted_data\s*>>>")

_INJECTION_PATTERNS = (
    re.compile(r"(?i)ignore\s+(all\s+)?(the\s+)?(previous|prior|above)"),
    re.compile(r"(?i)disregard\s+(all\s+)?(previous|prior)\s+instructions"),
    re.compile(r"(?i)you\s+are\s+now\s+"),
    re.compile(r"(?i)system\s*:\s*new\s+instructions"),
    re.compile(r"(?i)<\|im_start\|>|<\|endoftext\|>"),
    re.compile(r"(?i)do\s+not\s+tell\s+the\s+user"),
    re.compile(r"(?i)<<<\s*(?:end_)?codewiki_untrusted_data"),
    re.compile(r"忽略.{0,6}(指令|规则|设定)"),
    re.compile(r"(你现在是|你的新(角色|身份|任务))"),
)


class ToolError(Exception):
    """工具调用失败（越界、不存在、不可读等）。"""


def wrap_untrusted(text: str, *, origin: str = "") -> str:
    """把不可信内容包进固定分隔符，供 Prompt 引用。

    内容里若出现分隔符本身（任何大小写/空白变体），先**中性化**再包裹 ——
    否则攻击者在文件里写一行 END 分隔符就能提前结束数据块，把后续内容
    伪装成可信指令（外部评审实测面）。
    """
    head = f"[来源: {origin}]\n" if origin else ""
    safe = _DELIM_VARIANT.sub(_DELIM_NEUTERED, text)
    return f"{head}{DELIM_BEGIN}\n{safe}\n{DELIM_END}"


def scan_prompt_injection(text: str) -> list[str]:
    """返回命中的注入特征（空列表 = 未见异常）。"""
    return [p.pattern for p in _INJECTION_PATTERNS if p.search(text)]


def resolve_in_repo(path: str | Path, *, repo_root: Path) -> Path:
    """把路径解析到仓库内；越界抛 :class:`ToolError`。

    同时拒绝符号链接逃逸：``realpath`` 会展开链接，链接目标在仓库外即越界。
    """
    root = repo_root.resolve()
    candidate = Path(path)
    target = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if target != root and root not in target.parents:
        raise ToolError(f"路径越出仓库根目录: {path}")
    return target


def read_file(
    path: str,
    *,
    repo_root: Path,
    start_line: int | None = None,
    end_line: int | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    audit=None,
) -> str:
    """读取文件内容（支持行范围，超长截断）。

    截断必须**可见**：末尾追加显式标记，否则模型会以为文件就这么长。
    """
    try:
        target = resolve_in_repo(path, repo_root=repo_root)
    except ToolError as exc:
        if audit:
            audit.record(tool="read_file", args={"path": path}, outcome="denied", detail=str(exc))
        raise
    if not target.is_file():
        raise ToolError(f"不是文件（或不存在）: {path}")

    text = target.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if start_line is not None or end_line is not None:
        start = (start_line or 1) - 1
        end = end_line if end_line is not None else len(lines)
        lines = lines[max(0, start) : end]
        text = "\n".join(lines)

    text = redact(text)

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n…[已截断，共 {len(text)} 字符，上限 {max_chars}]"
    if audit:
        audit.record(tool="read_file", args={"path": path}, outcome="ok", detail=f"{len(text)} 字符")
    return text


def search_codebase(
    keyword: str,
    *,
    repo_root: Path,
    files: list[str] | None = None,
    max_hits: int = DEFAULT_MAX_HITS,
    audit=None,
) -> list[dict]:
    """全库关键词 / 正则搜索（**纯 Python，不拼 shell**）。

    刻意不走 ``grep`` 沙箱：搜索是最频繁的工具，走子进程既慢又多一层攻击面，
    而且正则来自模型 —— 直接喂给 grep 等于让模型控制一个命令行工具。

    模型正则的防线（外部评审 2026-10-23）：
    - 模式长度上限 + **嵌套量词静态拒绝**（``(x+x+)+y`` 一类灾难性回溯模式
      直接拒 —— 注意不能用线程超时：``re`` 匹配在 C 层持有 GIL 不释放，
      主线程连 join 超时都醒不过来，实测整场测试挂死）；
    - 单行截断后再匹配（回溯代价随输入长度指数增长，长行先砍）；
    - ``files`` 参数逐个过路径 jail —— ``root / "/abs"`` 会整体替换 root，
      绝对路径与 ``..`` 一律跳过，不当报错也不泄漏。
    """
    if len(keyword) > MAX_PATTERN_CHARS:
        raise ToolError(f"正则过长（{len(keyword)} > {MAX_PATTERN_CHARS} 字符），拒绝执行")
    _reject_catastrophic_backtracking(keyword)
    try:
        pattern = re.compile(keyword)
    except re.error as exc:
        raise ToolError(f"非法正则: {exc}") from exc

    root = repo_root.resolve()
    if files is not None:
        candidates = []
        for rel in files:
            try:
                target = resolve_in_repo(str(rel), repo_root=root)
            except ToolError:
                continue
            candidates.append(target)
    else:
        candidates = [root / rel for rel in _iter_text_files(root)]

    hits: list[dict] = []
    for target in candidates:
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            if pattern.search(line[:MAX_LINE_CHARS]):
                hits.append({"path": str(target), "line": lineno, "text": line.strip()[:200]})
                if len(hits) >= max_hits:
                    if audit:
                        audit.record(
                            tool="search_codebase",
                            args={"keyword": keyword[:120]},
                            outcome="ok",
                            detail=f"{len(hits)} hits",
                        )
                    return hits
    if audit:
        audit.record(
            tool="search_codebase",
            args={"keyword": keyword[:120]},
            outcome="ok",
            detail=f"{len(hits)} hits",
        )
    return hits


MAX_LINE_CHARS = 1000

_NESTED_QUANTIFIER = re.compile(r"\([^()]*[*+{|][^()]*\)\s*[*+{?]")


def _reject_catastrophic_backtracking(keyword: str) -> None:
    """静态拒绝嵌套量词模式（``(x+)+`` / ``(.*)*`` / ``(a|aa)+`` 一族）。

    这是保守检查：漏网的 exotic 形状换不成"挂死进程"（行长已截断、模式长度
    有上限），误拦的模型可以换等价写法 —— fail-closed 的代价是可接受的。
    """
    compact = keyword.replace("?:", "")
    if _NESTED_QUANTIFIER.search(compact):
        raise ToolError(
            "正则含嵌套量词（如 (x+)+），属灾难性回溯高危形状，拒绝执行；"
            "请改写为无嵌套量词的等价模式"
        )


def _iter_text_files(root: Path) -> list[str]:
    """列出仓库内的文本文件相对路径（跳过常见噪声目录）。"""
    skip = {".git", "node_modules", ".venv", "__pycache__", "dist", "build", ".codewiki"}
    out: list[str] = []
    for item in root.rglob("*"):
        if not item.is_file():
            continue
        parts = set(item.relative_to(root).parts)
        if parts & skip:
            continue
        try:
            out.append(str(item.relative_to(root)))
        except ValueError:
            continue
    return out


@dataclass(frozen=True)
class GraphHit:
    """一次图谱查询的结果。"""

    node: dict
    related: list[dict]
    edges: list[dict]


def query_graph(
    packet,
    node_id: str,
    *,
    direction: str = "both",
    depth: int = 1,
) -> GraphHit:
    """查节点上下游（Agent 优先用它而不是瞎 grep，§5.1）。

    ``direction`` 取 ``upstream``（谁依赖我）/ ``downstream``（我依赖谁）/ ``both``。
    ``depth`` 来自模型 —— 非数字必须在这里拒绝，不许把 ``int()`` 的异常漏成崩溃。
    """
    try:
        depth = int(depth)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"depth 必须是整数: {depth!r}") from exc
    if depth < 1:
        depth = 1
    elif depth > 10:
        depth = 10

    nodes = {n.id: n.model_dump(mode="json") for n in packet.nodes}
    if node_id not in nodes:
        raise ToolError(f"节点不存在: {node_id}")

    frontier = {node_id}
    seen: set[str] = {node_id}
    collected_edges: list[dict] = []
    for _ in range(max(1, depth)):
        next_frontier: set[str] = set()
        for edge in packet.edges:
            e = edge.model_dump(mode="json")
            src, dst = e["source"], e["target"]
            forward = src in frontier and dst not in seen
            backward = dst in frontier and src not in seen
            if direction == "downstream" and not forward:
                continue
            if direction == "upstream" and not backward:
                continue
            if not (forward or backward):
                continue
            other = dst if forward else src
            collected_edges.append(e)
            if other not in seen:
                seen.add(other)
                next_frontier.add(other)
        frontier = next_frontier
        if not frontier:
            break

    return GraphHit(
        node=nodes[node_id],
        related=[nodes[i] for i in sorted(seen - {node_id}) if i in nodes],
        edges=collected_edges,
    )


def run_shell(command: str, *, repo_root: Path, audit=None) -> str:
    """执行白名单 shell；拒绝时抛 :class:`ToolError`（消息里带原因）。

    ``audit`` 非空时记一条审计 —— **被拒绝的调用也要记**（攻击尝试本身就是
    要留痕的信息，只记成功调用等于只留一半证据）。
    """
    try:
        result = execute_shell(command, repo_root=repo_root)
    except ShellDenied as exc:
        if audit:
            audit.record(tool="shell", args={"command": command}, outcome="denied", detail=str(exc))
        raise ToolError(str(exc)) from exc
    body = result.stdout or result.stderr
    if result.truncated:
        body += "\n…[输出已截断]"
    if audit:
        audit.record(
            tool="shell",
            args={"command": command},
            outcome="ok",
            detail=body,
            returncode=result.returncode,
        )
    return body


__all__ = [
    "DELIM_BEGIN",
    "DELIM_END",
    "GraphHit",
    "ToolError",
    "query_graph",
    "read_file",
    "resolve_in_repo",
    "run_shell",
    "scan_prompt_injection",
    "search_codebase",
    "wrap_untrusted",
]
