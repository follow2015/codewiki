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

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from codewiki.ai.guard import (
    PathDenied,
    is_secret_file,
    redact_model,
    relative_to_root,
    resolve_in_root,
)
from codewiki.ai.sandbox import ShellDenied, execute_shell
from codewiki.subproc import SubprocessFailed, SubprocessTimeout, run, run_isolated_python

DEFAULT_MAX_CHARS = 24_000
DEFAULT_MAX_HITS = 50
MAX_PATTERN_CHARS = 500
REGEX_TIMEOUT_S = 10.0

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

    实现已收敛到 :func:`codewiki.ai.guard.paths.resolve_in_root`（"路径是否在
    仓库内"只允许有一处定义）。这里保留原名字与异常类型，是对既有调用面的
    兼容层 —— 新增代码请直接用 guard。
    """
    try:
        return resolve_in_root(path, root=repo_root)
    except PathDenied as exc:
        raise ToolError(str(exc)) from exc


def _is_secret_file(path: Path) -> bool:
    """兼容层：密钥文件判定已收敛到 :func:`codewiki.ai.guard.secrets.is_secret_file`。"""
    return is_secret_file(path)


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
    if _is_secret_file(target):
        detail = f"拒绝读取疑似密钥文件: {path}（.env / 私钥 / 凭据文件不入模型上下文）"
        if audit:
            audit.record(tool="read_file", args={"path": path}, outcome="denied", detail=detail)
        raise ToolError(detail)
    if not target.is_file():
        raise ToolError(f"不是文件（或不存在）: {path}")

    text = target.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if start_line is not None or end_line is not None:
        start = (start_line or 1) - 1
        end = end_line if end_line is not None else len(lines)
        lines = lines[max(0, start) : end]
        text = "\n".join(lines)

    text = redact_model(text)

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
    - 匹配整体放进**独立子进程并带硬超时**：静态检查只能认单层括号
      （``((a+))+$`` 之类能绕过），进程级超时才能兜住漏网形状；
    - 单行截断后再匹配（回溯代价随输入长度指数增长，长行先砍）；
    - 候选文件**逐个 resolve** —— 仓库里指向外部的符号链接（``evil -> /etc/x``）
      曾经被 ``root / rel`` 原样读走，等于绕过 read_file 的 jail；
    - 返回给模型的 ``path`` 一律是**相对仓库根**的形式（绝对路径会把本机目录
      结构带进上下文，也和提示词里 ``file:line`` 的引用约定不一致）。
    """
    if len(keyword) > MAX_PATTERN_CHARS:
        raise ToolError(f"正则过长（{len(keyword)} > {MAX_PATTERN_CHARS} 字符），拒绝执行")
    _reject_catastrophic_backtracking(keyword)
    try:
        re.compile(keyword)
    except re.error as exc:
        raise ToolError(f"非法正则: {exc}") from exc

    root = repo_root.resolve()
    requested = [str(rel) for rel in files] if files is not None else _iter_text_files(root)
    candidates: list[str] = []
    for rel in requested:
        try:
            target = resolve_in_root(rel, root=root)
        except PathDenied:
            continue
        if not target.is_file():
            continue
        if is_secret_file(target):
            continue
        try:
            candidates.append(relative_to_root(target, root=root))
        except PathDenied:
            continue

    hits = _scan_in_subprocess(keyword, root=root, candidates=candidates, max_hits=max_hits)
    if audit:
        audit.record(
            tool="search_codebase",
            args={"keyword": keyword[:120]},
            outcome="ok",
            detail=f"{len(hits)} hits",
        )
    return hits


def _scan_in_subprocess(keyword: str, *, root: Path, candidates: list[str], max_hits: int) -> list[dict]:
    """在子进程里跑正则；超时即中止（灾难性回溯到此为止）。

    ``re`` 匹配持 GIL，进程内没有可行的超时手段 —— 这是**唯一**能兜住静态检查
    漏网形状的做法（外部评审：``((a+))+$`` 26 字符就要 5.75 秒，每加一字符翻倍）。
    启动方式是安全边界的一部分：走
    :func:`codewiki.subproc.run_isolated_python` —— ``python -I`` + 空临时
    目录作 cwd。若写成 ``python -m codewiki.ai._regex_worker`` 并继承 cwd，``-m``
    会把 cwd 顶到 ``sys.path`` 首位，被分析仓库根目录里的一个 ``json.py`` 就会
    先于标准库被导入并执行（外部评审实测，现由 guard 用例锁死）。
    环境只留 PATH + PYTHONIOENCODING：密钥类环境变量不往下传。
    """
    if not candidates:
        return []

    payload = json.dumps(
        {
            "pattern": keyword,
            "root": str(root),
            "files": candidates,
            "max_line_chars": MAX_LINE_CHARS,
            "max_hits": max_hits,
        }
    )
    worker = Path(__file__).resolve().parent / "_regex_worker.py"
    try:
        proc = run_isolated_python(worker, payload=payload, timeout=REGEX_TIMEOUT_S)
    except SubprocessTimeout as exc:
        raise ToolError(f"正则匹配超过 {REGEX_TIMEOUT_S:g} 秒已中止（疑似灾难性回溯），请改写模式") from exc
    except SubprocessFailed as exc:
        raise ToolError(f"正则扫描子进程无法启动: {exc}") from exc

    if proc.returncode != 0:
        raise ToolError(f"正则扫描失败（退出码 {proc.returncode}）: {proc.stdout[:200]}")
    try:
        raw = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ToolError("正则扫描返回不是合法 JSON") from exc
    if isinstance(raw, dict) and "error" in raw:
        raise ToolError(f"正则扫描失败: {raw['error']}")

    return [{"path": hit["path"], "line": hit["line"], "text": redact_model(hit["text"])} for hit in raw]


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
            "正则含嵌套量词（如 (x+)+），属灾难性回溯高危形状，拒绝执行；请改写为无嵌套量词的等价模式"
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


_DIRECTION_ALIASES: dict[str, str] = {
    "both": "both",
    "up": "upstream",
    "upstream": "upstream",
    "down": "downstream",
    "downstream": "downstream",
}


def query_graph(
    packet,
    node_id: str,
    *,
    direction: str = "both",
    depth: int = 1,
) -> GraphHit:
    """查节点上下游（Agent 优先用它而不是瞎 grep，§5.1）。

    ``direction`` 取 ``upstream``（谁依赖我）/ ``downstream``（我依赖谁）/ ``both``，
    也接受 ``up`` / ``down`` 简写。``depth`` 来自模型 —— 非数字必须在这里拒绝，
    不许把 ``int()`` 的异常漏成崩溃。
    """
    try:
        depth = int(depth)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"depth 必须是整数: {depth!r}") from exc
    if depth < 1:
        depth = 1
    elif depth > 10:
        depth = 10

    key = str(direction or "both").strip().lower()
    if key not in _DIRECTION_ALIASES:
        raise ToolError(f"direction 取值非法：{direction!r}（可选 both / upstream / downstream）")
    direction = _DIRECTION_ALIASES[key]

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


_GIT_NO_FILENAME_FLAG = re.compile(r"-[A-Za-z]*h[A-Za-z]*")
_GIT_DIFF_HEADER = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")
_SECRET_OMITTED = "[已省略密钥文件内容]"


def _deny_secret_args(command: str) -> None:
    """命令参数点名了密钥文件（含 ``HEAD:.env`` 这类 rev:path 形式）即拒绝。

    ``cat .env`` 曾经能从 shell 读到明文，而 ``read_file`` 早已拒读 —— 同一个
    规则两处实现，就必然有一处漏。判定复用 guard 的
    :func:`~codewiki.ai.guard.secrets.is_secret_file`，不再各写一份。
    """
    try:
        argv = shlex.split(command)
    except ValueError:
        return
    for arg in argv[1:]:
        if arg.startswith("-"):
            continue
        for part in arg.split(":"):
            if part and is_secret_file(Path(part.strip())):
                raise ToolError(f"拒绝读取疑似密钥文件: {part}（.env / 私钥 / 凭据文件不入模型上下文）")
    if argv[:2] == ["git", "grep"] and (
        "--no-filename" in argv[2:] or any(_GIT_NO_FILENAME_FLAG.fullmatch(a) for a in argv[2:])
    ):
        raise ToolError("git grep 不允许 -h / --no-filename（去掉文件名后无法剔除密钥文件的命中行）")


def _strip_secret_output(body: str) -> str:
    """剔除输出里来自密钥文件的内容（参数拒读漏网时的兜底）。

    - ``path:line:text`` / ``path:text``（grep / git grep）：前两段任一是密钥
      文件名则丢行；
    - ``diff --git a/x b/x``（git log -p / show / diff）：x 是密钥文件则整块丢。

    ``grep`` 另有 ``--exclude`` 在源头就跳过（见 sandbox），这里是第二道。
    """
    out: list[str] = []
    skipping = False
    dropped = False
    for line in body.splitlines(keepends=True):
        header = _GIT_DIFF_HEADER.match(line.rstrip("\r\n"))
        if header:
            skipping = is_secret_file(Path(header["b"])) or is_secret_file(Path(header["a"]))
        if skipping:
            dropped = True
            continue
        head = line.split(":", 2)[:2]
        if any(part.strip() and is_secret_file(Path(part.strip())) for part in head):
            dropped = True
            continue
        out.append(line)
    if dropped:
        out.append(f"\n{_SECRET_OMITTED}\n")
    return "".join(out)


def run_shell(command: str, *, repo_root: Path, audit=None) -> str:
    """执行白名单 shell；拒绝时抛 :class:`ToolError`（消息里带原因）。

    ``audit`` 非空时记一条审计 —— **被拒绝的调用也要记**（攻击尝试本身就是
    要留痕的信息，只记成功调用等于只留一半证据）。
    """
    try:
        _deny_secret_args(command)
    except ToolError as exc:
        if audit:
            audit.record(tool="shell", args={"command": command}, outcome="denied", detail=str(exc))
        raise
    try:
        result = execute_shell(command, repo_root=repo_root)
    except ShellDenied as exc:
        if audit:
            audit.record(tool="shell", args={"command": command}, outcome="denied", detail=str(exc))
        raise ToolError(str(exc)) from exc
    body = redact_model(_strip_secret_output(result.stdout or result.stderr))
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


_REV_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/~^@{}-]{0,200}")

MAX_GIT_LOG_LIMIT = 100


def _check_rev(rev: str) -> str:
    """校验版本引用；不合形状就拒（不猜、不补全）。"""
    if not _REV_RE.fullmatch(rev):
        raise ToolError(f"非法的版本引用: {rev!r}（只接受分支/tag/HEAD~n/提交号；不接受以 - 开头的字符串）")
    return rev


def _repo_relative(path: str, *, repo_root: Path) -> str:
    """把模型给的路径归一化成**仓库相对**的 POSIX 路径。

    绝对路径、``..``、指向仓库外的符号链接都在 :func:`resolve_in_root` 里被拒；
    归一化则是为了让 git 收到它能理解的路径 —— 传绝对路径 git 会当成 pathspec
    逃逸出仓库（``git log -- /etc`` 在仓库内是可以跑的）。
    """
    try:
        target = resolve_in_root(path, root=repo_root)
        return relative_to_root(target, root=repo_root)
    except PathDenied as exc:
        raise ToolError(str(exc)) from exc


def git_log(*, repo_root: Path, path: str = "", limit: int = 20) -> str:
    """某条路径的提交历史（**固定 argv 模板**，不接受任何选项）。

    这是取代通用 shell 的两个工具之一：真实需求（"这行是谁改的"）用它能覆盖，
    而它没有"选项白名单"这个攻击面 —— 模板里没有任何位置能塞进 ``--output=``。
    """
    if not 1 <= limit <= MAX_GIT_LOG_LIMIT:
        raise ToolError(f"limit 需在 1..{MAX_GIT_LOG_LIMIT}（收到 {limit}）")
    argv = [
        "git",
        "--no-pager",
        "log",
        "--no-color",
        "--date=short",
        "--format=%h %ad %s",
        f"-n{limit}",
    ]
    if path:
        argv += ["--", _repo_relative(path, repo_root=repo_root)]
    done = run(argv, cwd=repo_root.resolve())
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()
        raise ToolError(f"git log 失败（退出码 {done.returncode}）: {detail[0] if detail else '无输出'}")
    return done.stdout.strip() or "（无提交记录）"


def git_show(*, repo_root: Path, rev: str, path: str) -> str:
    """某个版本里某个文件的内容（``git show <rev>:<path>``）。

    ``path`` 先过路径 jail，再按**文件名**判密钥 —— 后者必须在读之前：
    ``read_file`` 早就拒读 ``.env``，而 ``git show HEAD:.env`` 曾经照样把明文
    交出去，同一个规则两处实现就必然有一处漏。
    """
    rev = _check_rev(rev)
    rel = _repo_relative(path, repo_root=repo_root)
    if is_secret_file(Path(rel)):
        raise ToolError(f"拒绝读取疑似密钥文件: {rel}（.env / 私钥 / 凭据文件不入模型上下文）")
    done = run(["git", "--no-pager", "show", f"{rev}:{rel}"], cwd=repo_root.resolve())
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()
        raise ToolError(f"git show 失败（退出码 {done.returncode}）: {detail[0] if detail else '无输出'}")
    body = redact_model(_strip_secret_output(done.stdout))
    if done.truncated:
        body += "\n…[输出已截断]"
    return body


__all__ = [
    "DELIM_BEGIN",
    "DELIM_END",
    "GraphHit",
    "MAX_GIT_LOG_LIMIT",
    "ToolError",
    "git_log",
    "git_show",
    "query_graph",
    "read_file",
    "resolve_in_repo",
    "run_shell",
    "scan_prompt_injection",
    "search_codebase",
    "wrap_untrusted",
]
