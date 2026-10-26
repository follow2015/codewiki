"""Mermaid 校验门 L1（§4.3）—— **防御性特征匹配**，不是完整语法解析。

定位（§4.3 原文）："拦截绝大多数已知坏图"，不保证零漏判；L1 通过但用户侧渲染
失败属于降级容错范围（L2 若启用会兜底，问题样本回灌进 L1 特征库）。

检查的特征（每条都必须有对应用例，见 ``tests/test_mermaid_validator.py``）：

``flowchart``
* 节点括号 / 引号不配对；
* ``subgraph`` 缺少 ``end``；
* 节点 ID 含非法字符（非字母数字下划线）；
* Label 里出现未转义的 ``()[]"`` 等特殊字符。

公共项
* 图类型声明缺失或不支持；
* 代码围栏未闭合；
* 富文本出现 ``<br/>`` / ``<b>`` / ``<i>`` 之外的标签。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_TYPES: tuple[str, ...] = ("flowchart", "graph", "sequenceDiagram", "classDiagram")

ALLOWED_TAGS: tuple[str, ...] = ("<br/>", "<br>", "<b>", "</b>", "<i>", "</i>")

_RE_DECL = re.compile(r"^\s*([A-Za-z][\w-]*)")
_RE_SUBGRAPH = re.compile(r"^\s*subgraph\b", re.M)
_RE_END = re.compile(r"^\s*end\s*$", re.M)
_RE_NODE_DEF = re.compile(r"([A-Za-z_][\w.]*)\s*(\[|\(|\{|\")")
_RE_ID_TOKEN = re.compile(r"\b([^\s\-\[\]{}()\"|]+)\b")
_RE_ANY_TAG = re.compile(r"</?\s*([A-Za-z][A-Za-z0-9]*)\s*/?>")


@dataclass
class Issue:
    """一条校验问题。"""

    code: str
    message: str


def validate_l1(text: str) -> list[Issue]:
    """对一段 Mermaid 文本做 L1 特征检查（返回问题列表，空 = 通过）。"""
    issues: list[Issue] = []
    body = text.strip()
    if not body:
        return [Issue("EMPTY_DIAGRAM", "图内容为空")]

    fences = body.count("```")
    if fences:
        if fences % 2 != 0:
            issues.append(Issue("FENCE_UNCLOSED", "代码围栏未闭合"))
        body = re.sub(r"^```[a-zA-Z]*\n?", "", body)
        body = re.sub(r"\n?```\s*$", "", body)

    first_line = next((ln for ln in body.splitlines() if ln.strip()), "")
    has_graph_tokens = any(tok in first_line for tok in ("[", "(", "-->", "-.->", "|"))
    declaration = None if has_graph_tokens else _RE_DECL.match(body)
    if declaration is None:
        issues.append(Issue("TYPE_MISSING", "缺少图类型声明（如 flowchart TD）"))
    elif declaration.group(1) not in SUPPORTED_TYPES:
        issues.append(Issue("TYPE_UNSUPPORTED", f"不支持的图类型: {declaration.group(1)}"))

    if len(_RE_SUBGRAPH.findall(body)) != len(_RE_END.findall(body)):
        issues.append(Issue("SUBGRAPH_UNCLOSED", "subgraph 与 end 数量不配对"))

    for lineno, line in enumerate(body.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("%%"):
            continue
        if stripped.count("[") != stripped.count("]"):
            issues.append(Issue("BRACKET_UNBALANCED", f"第 {lineno} 行方括号不配对"))
        if stripped.count("(") != stripped.count(")"):
            issues.append(Issue("PAREN_UNBALANCED", f"第 {lineno} 行圆括号不配对"))
        if stripped.count('"') % 2 != 0:
            issues.append(Issue("QUOTE_UNBALANCED", f"第 {lineno} 行引号不配对"))

        structural = re.sub(r'"[^"]*"', '""', stripped)
        for match in _RE_NODE_DEF.finditer(structural):
            token = match.group(1)
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token):
                issues.append(Issue("NODE_ID_ILLEGAL", f"第 {lineno} 行节点 ID 非法: {token!r}"))

        for quoted in re.findall(r'"([^"]*)"', stripped):
            for ch in "()[]{}":
                if ch in quoted and f"#{ord(ch)};" not in quoted:
                    issues.append(
                        Issue("LABEL_UNESCAPED", f"第 {lineno} 行 Label 含未转义字符 {ch!r}")
                    )
                    break

        for tag in _RE_ANY_TAG.finditer(stripped):
            raw = tag.group(0)
            if raw not in ALLOWED_TAGS:
                issues.append(Issue("TAG_NOT_ALLOWED", f"第 {lineno} 行出现不允许的标签 {raw}"))

    return _dedupe(issues)


def is_valid(text: str) -> bool:
    """L1 是否通过（便捷入口）。"""
    return not validate_l1(text)


MMDC_CANDIDATES: tuple[str, ...] = ("mmdc", "npx")


@dataclass
class L2Result:
    """L2 校验结果。

    ``status`` 三态：``ok``（真实渲染通过）/ ``invalid``（渲染失败）/ ``unavailable``
    （环境不具备，**未做校验**）。后两者都不能当作"图没问题"。
    """

    status: str
    issues: list[Issue]
    detail: str = ""


def l2_available() -> str | None:
    """返回可用的 mermaid-cli 可执行名；不具备返回 ``None``。"""
    import shutil

    for name in MMDC_CANDIDATES:
        if shutil.which(name):
            return name
    return None


def validate_l2(text: str, *, timeout_s: float = 60.0) -> L2Result:
    """用 mermaid-cli 真实解析一段 Mermaid（临时文件进/出，不落产物目录）。"""
    import tempfile

    from codewiki.subproc import SubprocessFailed, SubprocessTimeout, run

    program = l2_available()
    if program is None:
        return L2Result("unavailable", [], "未检测到 mermaid-cli（mmdc/npx），仅执行了 L1")

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "diagram.mmd"
        src.write_text(text, encoding="utf-8")
        out = Path(tmp) / "diagram.svg"
        if program == "npx":
            argv = ["npx", "--yes", "@mermaid-js/mermaid-cli", "-i", str(src), "-o", str(out)]
        else:
            argv = [program, "-i", str(src), "-o", str(out)]
        try:
            completed = run(argv, timeout=timeout_s)
        except (SubprocessTimeout, SubprocessFailed) as exc:
            return L2Result("invalid", [Issue("L2_TIMEOUT", f"L2 渲染超时/不可执行: {exc}")])
        if completed.returncode == 0 and out.exists():
            return L2Result("ok", [], "mermaid-cli 渲染通过")
        message = (completed.stderr or completed.stdout or "").strip()[:400]
        return L2Result("invalid", [Issue("L2_RENDER_FAILED", f"mermaid-cli 渲染失败: {message}")])


def validate(text: str, *, use_l2: bool = False) -> tuple[list[Issue], L2Result | None]:
    """组合校验：L1 必跑；``use_l2`` 为真且 L2 可用时再跑一次真实渲染。"""
    issues = validate_l1(text)
    if not use_l2:
        return issues, None
    if issues:
        return issues, None
    return issues, validate_l2(text)


def _dedupe(issues: list[Issue]) -> list[Issue]:
    """同码同消息去重（同一行可能命中多次同一特征）。"""
    seen: set[tuple[str, str]] = set()
    out: list[Issue] = []
    for issue in issues:
        key = (issue.code, issue.message)
        if key in seen:
            continue
        seen.add(key)
        out.append(issue)
    return out


__all__ = [
    "ALLOWED_TAGS",
    "L2Result",
    "MMDC_CANDIDATES",
    "SUPPORTED_TYPES",
    "Issue",
    "is_valid",
    "l2_available",
    "validate",
    "validate_l1",
    "validate_l2",
]
