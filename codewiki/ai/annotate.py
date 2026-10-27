"""``ai-annotate`` —— 自动架构标注（§5.2 工作流 A）。

与 ``analyze`` **完全解耦**：输入是数据包 + 源码 + ``codewiki.toml``，输出是
独立的建议文件，**不动主配置**（合并由用户显式发起 ``merge-annotations``）。

三条硬规则（对应 §十二 风险表）：

1. **必带语境**：层名与人工注解进系统提示（v2.4 强制），否则 AI 会产出与项目
   术语脱节的文案；
2. **注解可追溯**：每条建议必须带 ``evidence``（文件 + 行号），且行号必须真的
   存在于源码里 —— 校验不过的建议**丢弃并记诊断**，不写进建议文件；
3. **AI 失败不阻断静态产物**：模型不可达 / 认证失败时，已生成的部分照写，
   诊断里明确标 ``AI_UNAVAILABLE``，调用方（CLI）自行决定退出码。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from codewiki.ai.cache import PROMPT_VERSION, ResultCache, TokenBudget, cache_key, digest_of, estimate_tokens
from codewiki.ai.client import ChatMessage, LLMError, OpenAICompatClient
from codewiki.ai.prompts import ANNOTATE_SYSTEM, render_annotate_user
from codewiki.ai.registry import default_registry
from codewiki.ai.tools import resolve_in_repo, scan_prompt_injection
from codewiki.schemas import Annotation, Diagnostic

SUGGEST_FILENAME = "annotations.suggest.toml"
SUGGEST_SUBDIR = "suggestions"

MAX_SOURCE_CHARS = 12_000

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class Suggestion:
    """一条 AI 建议（未审阅）。"""

    target: str
    label: str | None
    note: str
    evidence: list[dict] = field(default_factory=list)

    def to_annotation(self) -> Annotation:
        return Annotation(
            target=self.target,
            label=self.label,
            note=self.note,
            source="ai",
            reviewed=False,
        )


@dataclass
class AnnotateResult:
    suggestions: list[Suggestion] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    degraded: bool = False


def module_degrees(packet) -> dict[str, int]:
    """模块级度数（只数 ``mod::`` 之间的边，符号级边不计入模块热度）。"""
    degrees: dict[str, int] = {}
    for edge in packet.edges:
        if edge.source.startswith("mod::") and edge.target.startswith("mod::"):
            degrees[edge.source] = degrees.get(edge.source, 0) + 1
            degrees[edge.target] = degrees.get(edge.target, 0) + 1
    return degrees


def select_modules(packet, *, top_n: int = 30, protected: set[str] | None = None) -> list[dict]:
    """挑待标注模块：按度数降序取 TopN，跳过受保护（已有人工/已审阅）的模块。

    度数相同时按 path 排序 —— 保证**确定性**，否则同一份数据包两次跑出不同
    结果，golden 与缓存都会失灵。
    """
    protected = protected or set()
    degrees = module_degrees(packet)
    modules = [n for n in packet.nodes if n.type.value == "Module" and n.path]
    ranked = sorted(
        modules,
        key=lambda n: (-degrees.get(n.id, 0), n.path or ""),
    )
    picked = []
    for node in ranked:
        if node.id in protected:
            continue
        picked.append(
            {"id": node.id, "path": node.path, "layer": node.layer_id, "degree": degrees.get(node.id, 0)}
        )
        if len(picked) >= top_n:
            break
    return picked


def protected_targets(cfg_annotations, packet) -> set[str]:
    """受保护注解的 target：人工写的，或已审阅的（§5.2 第 2 步：跳过它们）。"""
    targets = {a.target for a in cfg_annotations}
    targets |= {a.target for a in packet.annotations if a.reviewed or a.source == "human"}
    return targets


def build_context(packet, cfg, *, src_root: Path | None) -> tuple[list[str], list[str]]:
    """拼系统提示要用的语境（层名列表 + 已有人工注解列表）。"""
    layers = [layer.name for layer in cfg.architecture.layers]
    if not layers:
        layers = [layer.name for layer in packet.layers]
    annotations = [f"{a.target}｜{a.label or ''}｜{a.note}".strip("｜") for a in cfg.annotations] or [
        f"{a.target}｜{a.label or ''}｜{a.note}".strip("｜")
        for a in packet.annotations
        if a.source == "human"
    ]
    return layers, annotations


def _read_for_prompt(path: str, *, src_root: Path | None) -> tuple[str, str]:
    """取证用的源码片段；取不到就返回 ``( "", "" )``（Prompt 里会显式标注"未取证"）。

    返回 ``(raw, wrapped)``：注入扫描必须做在 **raw** 上 —— wrapped 里带着我们
    自己的合法分隔符，拿去扫描会把每个模块都误报成注入。两档都由注册表产出，
    annotate 不自己调 read_file、也不自己包裹（同一份过滤/审计实现，设计文稿
    §AI 层安全模型第 2 条"没有第二条读源码的路径"）。
    """
    if src_root is None:
        return "", ""
    try:
        return default_registry(repo_root=src_root).call_raw(
            "read_file", {"path": path}, max_chars=MAX_SOURCE_CHARS
        )
    except Exception:  # noqa: BLE001 - 取证失败不得中断：退化为"无源码"路径
        return "", ""


def _parse_suggestions(raw: str) -> tuple[list[Suggestion], list[str]]:
    """解析模型输出；解析不出来返回空列表（调用方记诊断，不猜）。"""
    match = _JSON_OBJECT.search(raw or "")
    if not match:
        return [], []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return [], []
    items: list[Suggestion] = []
    unknown: list[str] = []
    for entry in data.get("modules") or []:
        target = entry.get("target")
        note = (entry.get("note") or "").strip()
        if not target or not note:
            continue
        items.append(
            Suggestion(
                target=str(target),
                label=(entry.get("label") or None),
                note=note,
                evidence=list(entry.get("evidence") or []),
            )
        )
    for target in data.get("unknown") or []:
        unknown.append(str(target))
    return items, unknown


def _evidence_is_real(evidence: list[dict], *, src_root: Path | None) -> bool:
    """行号必须真的存在（§5.2：结论必须引用代码行号）。

    没有源码（``src_root`` 为空）时**不做**校验，但调用方会在 note 里标
    "未取证" —— 不校验不等于放行编造，而是把不确定性显式暴露给审阅者。
    """
    if src_root is None:
        return bool(evidence)
    for item in evidence:
        rel = item.get("path")
        lines = item.get("lines") or []
        if not rel or not lines:
            continue
        try:
            target = resolve_in_repo(str(rel), repo_root=src_root)
        except Exception:  # noqa: BLE001 - 越界/不存在即视为证据无效
            continue
        if not target.is_file():
            continue
        try:
            total = len(target.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            continue
        if all(isinstance(line, int) and 1 <= line <= total for line in lines):
            return True
    return False


def annotate_modules(
    packet,
    cfg,
    *,
    src_root: Path | None,
    client: OpenAICompatClient,
    cache_root: Path | None = None,
    budget_tokens: int | None = None,
    top_n: int = 30,
    audit=None,
) -> AnnotateResult:
    """跑一轮标注（默认每个模块一次调用，命中缓存则不花钱）。"""
    result = AnnotateResult()
    protected = protected_targets(cfg.annotations, packet)
    modules = select_modules(packet, top_n=top_n, protected=protected)
    if not modules:
        result.diagnostics.append(
            Diagnostic(level="info", code="AI_NO_TARGET", message="没有待标注模块（全部已有人工/已审阅注解）")
        )
        return result

    layers, annotations = build_context(packet, cfg, src_root=src_root)
    cache = ResultCache(cache_root) if cache_root else None
    budget = TokenBudget(budget_tokens)

    for module in modules:
        if budget.exceeded:
            result.diagnostics.append(
                Diagnostic(
                    level="warning",
                    code="AI_BUDGET_EXHAUSTED",
                    message=f"token 预算耗尽（上限 {budget_tokens}），剩余 {len(modules) - len(result.suggestions)} 个模块未标注",
                    where=module["id"],
                )
            )
            break

        raw_source, snippet = _read_for_prompt(module["path"], src_root=src_root)
        hits = scan_prompt_injection(raw_source)
        user_prompt = render_annotate_user(
            title=cfg.title,
            layers=layers,
            annotations=annotations,
            modules=[f"{m['id']} | {m['layer']} | 出度+入度={m['degree']}" for m in [module]],
            sources=snippet,
        )

        content_hash = digest_of(f"{module['path']}::{snippet}")
        key = cache_key(content_hash=content_hash, model=client.model, prompt_version=PROMPT_VERSION)
        raw = ""
        if cache is not None:
            cached = cache.get(key)
            if cached:
                raw = cached.get("raw", "")

        if not raw:
            messages = [
                ChatMessage(role="system", content=ANNOTATE_SYSTEM),
                ChatMessage(role="user", content=user_prompt),
            ]
            try:
                reply = client.chat(messages)
            except LLMError as exc:
                result.degraded = True
                result.diagnostics.append(
                    Diagnostic(
                        level="warning",
                        code="AI_UNAVAILABLE",
                        message=f"AI 不可用，标注未完成（已生成的部分保留）: {exc}",
                        where=module["id"],
                    )
                )
                if audit:
                    audit.record(
                        tool="llm.chat", args={"target": module["id"]}, outcome="error", detail=str(exc)
                    )
                break
            raw = reply.content
            budget.spend(reply.total_tokens or estimate_tokens(user_prompt + raw))
            if cache is not None:
                cache.set(key, {"raw": raw, "model": client.model, "prompt_version": PROMPT_VERSION})
            if audit:
                audit.record(
                    tool="llm.chat",
                    args={"target": module["id"]},
                    outcome="ok",
                    detail=raw,
                    tokens=budget.used,
                )

        suggestions, unknown = _parse_suggestions(raw)
        if hits:
            result.diagnostics.append(
                Diagnostic(
                    level="warning",
                    code="AI_PROMPT_INJECTION_SUSPECTED",
                    message=f"源码中疑似提示词注入（已按数据处理）: {len(hits)} 处",
                    where=module["path"],
                )
            )
        for target in unknown:
            result.skipped.append(f"{target}（模型答无法确定）")

        accepted = False
        for suggestion in suggestions:
            if suggestion.target != module["id"]:
                result.diagnostics.append(
                    Diagnostic(
                        level="warning",
                        code="AI_TARGET_MISMATCH",
                        message=f"模型返回了非本模块的 target（{suggestion.target}），已丢弃",
                        where=module["id"],
                    )
                )
                continue
            if not _evidence_is_real(suggestion.evidence, src_root=src_root):
                result.diagnostics.append(
                    Diagnostic(
                        level="warning",
                        code="AI_EVIDENCE_INVALID",
                        message="建议缺少可验证的代码行号证据，已丢弃（§十二：取证不足不写注解）",
                        where=suggestion.target,
                    )
                )
                continue
            result.suggestions.append(suggestion)
            accepted = True
        if not accepted:
            result.skipped.append(module["id"])
    return result


def _toml_string(value: str) -> str:
    """TOML 基本字符串转义（不引第三方写库，只写这一个形状）。"""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def render_suggestions_toml(suggestions: list[Suggestion]) -> str:
    """渲染建议文件（全部 ``source="ai"``、``reviewed=false``）。"""
    lines = [
        "# codewiki AI 注解建议 —— **未经审阅**（reviewed=false）",
        "# 请用 diff 工具逐条确认后执行：codewiki merge-annotations <本文件>",
        "",
    ]
    if not suggestions:
        lines.append("# （本轮没有可采纳的建议）")
        lines.append("")
    for item in suggestions:
        lines.append("[[annotations]]")
        lines.append(f"target = {_toml_string(item.target)}")
        if item.label:
            lines.append(f"label = {_toml_string(item.label)}")
        lines.append(f"note = {_toml_string(item.note)}")
        lines.append('source = "ai"')
        lines.append("reviewed = false")
        if item.evidence:
            lines.append("# 证据（生成时已校验行号存在）")
            for ev in item.evidence:
                path = ev.get("path", "")
                nums = ", ".join(str(n) for n in ev.get("lines") or [] if isinstance(n, int))
                lines.append(f"#   - {path}:{nums}")
        lines.append("")
    return "\n".join(lines)


def write_suggestions(suggestions: list[Suggestion], out_dir: Path) -> Path:
    """写 ``suggestions/annotations.suggest.toml``（原子化）。"""
    target_dir = out_dir / SUGGEST_SUBDIR
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / SUGGEST_FILENAME
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(render_suggestions_toml(suggestions), encoding="utf-8")
    tmp.replace(path)
    return path


__all__ = [
    "AnnotateResult",
    "SUGGEST_FILENAME",
    "SUGGEST_SUBDIR",
    "Suggestion",
    "annotate_modules",
    "build_context",
    "module_degrees",
    "protected_targets",
    "render_suggestions_toml",
    "select_modules",
    "write_suggestions",
]
