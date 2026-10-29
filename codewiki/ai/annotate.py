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

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from codewiki.ai.cache import PROMPT_VERSION, ResultCache, TokenBudget, cache_key, digest_of, estimate_tokens
from codewiki.ai.client import ChatMessage, LLMError, OpenAICompatClient
from codewiki.ai.deferred import PendingRequest
from codewiki.ai.jsonx import extract_json_object
from codewiki.ai.neighbors import module_neighbors, render_neighbors
from codewiki.ai.prompts import ANNOTATE_SYSTEM, render_annotate_repair, render_annotate_user
from codewiki.ai.registry import default_registry
from codewiki.ai.tools import resolve_in_repo, scan_prompt_injection, wrap_untrusted
from codewiki.schemas import Annotation, Diagnostic

SUGGEST_FILENAME = "annotations.suggest.toml"
SUGGEST_SUBDIR = "suggestions"

MAX_SOURCE_CHARS = 12_000

MAX_PARSE_ECHO_CHARS = 2_000

MAX_ANCHORS = 8


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
    pending: list[str] = field(default_factory=list)
    degraded: bool = False
    model_calls: int = 0


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


def number_lines(text: str, *, width: int = 5) -> str:
    """给源码加行号前缀（``   12| code``），供 Prompt 引用。

    §5.2 要求"结论必须引用 file:line"，而 :func:`codewiki.ai.tools.read_file`
    的原始输出**不带行号** —— 此前模型只能靠自己数行：evidence 校验只比对
    "行号是否落在文件范围内"，数错了照样通过，校验形同虚设。行号进了 Prompt，
    引用才有可核对的目标。
    """
    return "\n".join(f"{index:>{width}}| {line}" for index, line in enumerate(text.splitlines(), start=1))


def _read_for_prompt(path: str, *, src_root: Path | None) -> tuple[str, str]:
    """取证用的源码片段；取不到就返回 ``( "", "" )``（Prompt 里会显式标注"未取证"）。

    返回 ``(raw, wrapped)``：注入扫描必须做在 **raw** 上 —— wrapped 里带着我们
    自己的合法分隔符，拿去扫描会把每个模块都误报成注入。

    读文件仍走注册表（同一份路径 jail / 密钥过滤 / 审计），只有"加行号再包裹"
    这一层是本地做的：包裹用同一份 :func:`wrap_untrusted`（含分隔符中性化），
    不是另起一份拼接实现。
    """
    if src_root is None:
        return "", ""
    try:
        raw, _wrapped = default_registry(repo_root=src_root).call_raw(
            "read_file", {"path": path}, max_chars=MAX_SOURCE_CHARS
        )
    except Exception:  # noqa: BLE001 - 取证失败不得中断：退化为"无源码"路径
        return "", ""
    if not raw:
        return "", ""
    return raw, wrap_untrusted(number_lines(raw), origin=path)


@dataclass(frozen=True)
class ParseOutcome:
    """一次解析的结果：**ok 与空列表是两件事**。

    此前两者都返回 ``([], [])``，于是"模型答了但格式坏"与"模型说无法确定"
    在调用方看来一模一样 —— 前者该带着错误重试一次，后者不该重试（再问一遍
    只会得到同样的"无法确定"）。
    """

    suggestions: list[Suggestion] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    ok: bool = True
    error: str = ""


def _parse_suggestions(raw: str) -> ParseOutcome:
    """解析模型输出；解析不出来返回 ``ok=False`` + 可回喂的原因（调用方重试一次）。"""
    data, error = extract_json_object(raw or "")
    if data is None:
        return ParseOutcome(ok=False, error=error)

    modules = data.get("modules")
    unknown_raw = data.get("unknown")
    if modules is None and unknown_raw is None:
        return ParseOutcome(ok=False, error="JSON 里既没有 modules 也没有 unknown 字段")
    if modules is not None and not isinstance(modules, list):
        return ParseOutcome(ok=False, error="modules 必须是数组")
    if unknown_raw is not None and not isinstance(unknown_raw, list):
        return ParseOutcome(ok=False, error="unknown 必须是数组")

    items: list[Suggestion] = []
    dropped = 0
    for entry in modules or []:
        if not isinstance(entry, dict):
            dropped += 1
            continue
        target = entry.get("target")
        note = (entry.get("note") or "").strip() if isinstance(entry.get("note"), str) else ""
        if not isinstance(target, str) or not target or not note:
            dropped += 1
            continue
        evidence = entry.get("evidence")
        items.append(
            Suggestion(
                target=target,
                label=(entry.get("label") or None),
                note=note,
                evidence=[e for e in (evidence if isinstance(evidence, list) else []) if isinstance(e, dict)],
            )
        )
    unknown = [str(t) for t in (unknown_raw or [])]

    if dropped and not items and not unknown:
        return ParseOutcome(ok=False, error=f"modules 里 {dropped} 个条目缺少 target/note 或不是对象")
    return ParseOutcome(suggestions=items, unknown=unknown, ok=True)


_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_BACKTICK = re.compile(r"`([^`]{1,80})`")

_ANCHOR_STOPWORDS = frozenset(
    {
        "API",
        "URL",
        "HTTP",
        "JSON",
        "HTML",
        "SQL",
        "TODO",
        "NOTE",
        "README",
        "IMPORTS",
        "CALLS",
        "CONTAINS",
        "MODULE",
        "SYMBOL",
        "ROOT",
    }
)


def _is_code_identifier(token: str) -> bool:
    """结构判据：下划线命名、CamelCase 或全大写常量才算"代码名"。"""
    if len(token) < 3 or token in _ANCHOR_STOPWORDS:
        return False
    if "_" in token:
        return True
    return any(char.isupper() for char in token[1:])


def conclusion_anchors(note: str, *, limit: int = MAX_ANCHORS) -> list[str]:
    """从结论里抽**可核对**的代码标识符。

    只抽结构上像代码名的词（``mark_stale`` / ``AuthManager`` / ``MAX_RETRY``），
    不抽普通英文与中文 —— 后者在源码里"到处都是"，拿它判"支持"等于没判。
    反引号里的内容优先（那是模型自己声明的代码名）。
    """
    anchors: list[str] = []
    for chunk in _BACKTICK.findall(note or ""):
        for token in _TOKEN.findall(chunk):
            if len(token) >= 3 and token not in _ANCHOR_STOPWORDS and token not in anchors:
                anchors.append(token)
    for token in _TOKEN.findall(note or ""):
        if _is_code_identifier(token) and token not in anchors:
            anchors.append(token)
    return anchors[:limit]


@dataclass(frozen=True)
class EvidenceVerdict:
    """证据核对结论。``code`` 为空表示通过。"""

    ok: bool
    code: str = ""
    detail: str = ""


def _line_span(path: Path, lines: list[int], cache: dict[Path, list[str]]) -> str:
    if path not in cache:
        cache[path] = path.read_text(encoding="utf-8", errors="replace").splitlines()
    content = cache[path]
    picked: list[str] = []
    for number in lines:
        if isinstance(number, int) and 1 <= number <= len(content):
            picked.append(content[number - 1])
    return "\n".join(picked)


def check_evidence(
    evidence: Sequence[dict],
    *,
    note: str = "",
    src_root: Path | None,
) -> EvidenceVerdict:
    """核对证据：行号要真实存在，**并且**要能支持结论。

    前半段（存在性）挡编造与数错行；后半段才是这次升级的重点 —— 此前判据是
    "任一条证据的行号落在文件范围内就通过"，于是一条指向 ``README.md:1`` 的
    证据可以为任何结论背书。现在从结论里抽代码名锚点，要求**所引行区间内**
    至少出现一个：``note`` 说 ``mark_stale`` 而引的是第 4 行 ``class Base:``，
    行号虽然真实，却证明不了结论 —— 判 ``AI_EVIDENCE_UNSUPPORTED``。

    边界要说清：这仍只做**机械**核对（锚点是否出现在所引行里），不判断语义。
    结论里没有可抽取的代码名时（纯中文描述）只能退化为存在性核对 —— 那时如实
    说"只能核到存在"，而不是假装核过支持关系。
    """
    if src_root is None:
        if not evidence:
            return EvidenceVerdict(False, "AI_EVIDENCE_INVALID", "没有源码也没给证据，无法核对")
        return EvidenceVerdict(True)

    anchors = conclusion_anchors(note)
    exists = False
    supported = False
    spans: dict[Path, list[str]] = {}
    for item in evidence or []:
        rel = item.get("path")
        lines = item.get("lines") or []
        if not isinstance(rel, str) or not rel or not isinstance(lines, list) or not lines:
            continue
        try:
            target = resolve_in_repo(rel, repo_root=src_root)
        except Exception:  # noqa: BLE001 - 越界/不存在即视为证据无效
            continue
        if not target.is_file():
            continue
        try:
            total = len(target.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            continue
        if not all(isinstance(line, int) and 1 <= line <= total for line in lines):
            continue
        exists = True
        if not anchors:
            supported = True
            break
        span = _line_span(target, lines, spans)
        if any(anchor in span for anchor in anchors):
            supported = True
            break

    if not exists:
        return EvidenceVerdict(
            False, "AI_EVIDENCE_INVALID", "建议缺少可验证的代码行号证据，已丢弃（§十二：取证不足不写注解）"
        )
    if not supported:
        listed = "、".join(anchors) or "（无可抽取的代码名）"
        return EvidenceVerdict(
            False,
            "AI_EVIDENCE_UNSUPPORTED",
            f"证据行号真实存在，但所引行内没有结论提到的代码名 {listed} —— 引用不支持结论，已丢弃",
        )
    return EvidenceVerdict(True)


def _evidence_is_real(evidence: list[dict], *, src_root: Path | None, note: str = "") -> bool:
    """兼容层：只要结论（``check_evidence``）。

    保留旧名字与旧签名（``note`` 可省）是因为外部脚本按这个名字调用过；
    省略 ``note`` 时退化为纯存在性核对。
    """
    return check_evidence(evidence, note=note, src_root=src_root).ok


def _ask(
    client,
    history: list[ChatMessage],
    *,
    prompt_text: str,
    module_id: str,
    result: AnnotateResult,
    budget: TokenBudget,
    audit,
    json_kwargs: dict,
) -> tuple[str, str]:
    """发一次请求并记账；返回 ``(raw, status)``，status ∈ ``ok/pending/unavailable``。

    三种后果必须分开：``pending``（外置推理等作答）要继续下一个模块，``unavailable``
    （不可达/认证失败）要停下来，``ok`` 才继续解析 —— 合并成"失败"会让一次抖动
    把整轮 30 个模块全部作废。
    """
    try:
        reply = client.chat(history, **json_kwargs)
    except PendingRequest as exc:
        result.pending.append(module_id)
        if audit:
            audit.record(
                tool="llm.deferred", args={"target": module_id}, outcome="skipped", detail=f"待作答 {exc.key}"
            )
        return "", "pending"
    except LLMError as exc:
        result.degraded = True
        result.diagnostics.append(
            Diagnostic(
                level="warning",
                code="AI_UNAVAILABLE",
                message=f"AI 不可用，标注未完成（已生成的部分保留）: {exc}",
                where=module_id,
            )
        )
        if audit:
            audit.record(tool="llm.chat", args={"target": module_id}, outcome="error", detail=str(exc))
        return "", "unavailable"

    result.model_calls += 1
    budget.spend(reply.total_tokens or estimate_tokens(prompt_text + reply.content))
    if reply.degraded and not any(d.code == "AI_JSON_MODE_UNSUPPORTED" for d in result.diagnostics):
        result.diagnostics.append(
            Diagnostic(
                level="info",
                code="AI_JSON_MODE_UNSUPPORTED",
                message=f"网关不接受 {', '.join(reply.degraded)}，已回退为纯文本 JSON 输出",
            )
        )
    if audit:
        audit.record(
            tool="llm.chat",
            args={"target": module_id},
            outcome="ok",
            detail=reply.content,
            tokens=budget.used,
        )
    return reply.content, "ok"


def annotate_modules(
    packet,
    cfg,
    *,
    src_root: Path | None,
    client: OpenAICompatClient,
    cache_dir: Path | None = None,
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
    cache = ResultCache(cache_dir) if cache_dir else None
    budget = TokenBudget(budget_tokens)
    json_kwargs: dict = (
        {"response_format": {"type": "json_object"}} if getattr(client, "supports_json_mode", False) else {}
    )

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
        neighbors = module_neighbors(packet, module["id"])
        user_prompt = render_annotate_user(
            title=cfg.title,
            layers=layers,
            annotations=annotations,
            modules=[f"{m['id']} | {m['layer']} | 出度+入度={m['degree']}" for m in [module]],
            sources=snippet,
            neighbors=render_neighbors(neighbors),
        )
        messages = [
            ChatMessage(role="system", content=ANNOTATE_SYSTEM),
            ChatMessage(role="user", content=user_prompt),
        ]

        content_hash = digest_of(f"{module['path']}::{snippet}")
        key = cache_key(
            content_hash=content_hash,
            model=client.model,
            prompt_version=PROMPT_VERSION,
            neighbors_hash=neighbors.digest,
        )
        raw = ""
        from_cache = False
        if cache is not None:
            cached = cache.get(key)
            if cached:
                raw = cached.get("raw", "")
                from_cache = bool(raw)

        if raw and not _parse_suggestions(raw).ok:
            raw = ""
            from_cache = False

        if not raw:
            raw, status = _ask(
                client,
                messages,
                prompt_text=user_prompt,
                module_id=module["id"],
                result=result,
                budget=budget,
                audit=audit,
                json_kwargs=json_kwargs,
            )
            if status == "pending":
                continue
            if status == "unavailable":
                break

        outcome = _parse_suggestions(raw)
        if not outcome.ok:
            retry_prompt = render_annotate_repair(error=outcome.error, previous=raw[:MAX_PARSE_ECHO_CHARS])
            retry_messages = [
                *messages,
                ChatMessage(role="assistant", content=raw[:MAX_PARSE_ECHO_CHARS]),
                ChatMessage(role="user", content=retry_prompt),
            ]
            retry_raw, status = _ask(
                client,
                retry_messages,
                prompt_text=retry_prompt,
                module_id=module["id"],
                result=result,
                budget=budget,
                audit=audit,
                json_kwargs=json_kwargs,
            )
            if status == "pending":
                continue
            if status == "unavailable":
                break
            retried = _parse_suggestions(retry_raw)
            if retried.ok:
                result.diagnostics.append(
                    Diagnostic(
                        level="info",
                        code="AI_PARSE_RETRY",
                        message=f"首次输出无法解析（{outcome.error}），带错误重试后成功",
                        where=module["id"],
                    )
                )
                outcome = retried
                raw = retry_raw
                from_cache = False
            else:
                outcome = retried

        if not outcome.ok:
            result.diagnostics.append(
                Diagnostic(
                    level="warning",
                    code="AI_PARSE_FAILED",
                    message=f"模型输出无法解析（已带错误重试一次）：{outcome.error}",
                    where=module["id"],
                )
            )
            result.skipped.append(module["id"])
            if audit:
                audit.record(
                    tool="llm.parse", args={"target": module["id"]}, outcome="error", detail=outcome.error
                )
            continue

        if cache is not None and not from_cache:
            cache.set(key, {"raw": raw, "model": client.model, "prompt_version": PROMPT_VERSION})

        if hits:
            result.diagnostics.append(
                Diagnostic(
                    level="warning",
                    code="AI_PROMPT_INJECTION_SUSPECTED",
                    message=f"源码中疑似提示词注入（已按数据处理）: {len(hits)} 处",
                    where=module["path"],
                )
            )
        for target in outcome.unknown:
            result.skipped.append(f"{target}（模型答无法确定）")

        accepted = False
        for suggestion in outcome.suggestions:
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
            verdict = check_evidence(suggestion.evidence, note=suggestion.note, src_root=src_root)
            if not verdict.ok:
                result.diagnostics.append(
                    Diagnostic(
                        level="warning",
                        code=verdict.code or "AI_EVIDENCE_INVALID",
                        message=verdict.detail,
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
    "number_lines",
    "protected_targets",
    "render_suggestions_toml",
    "select_modules",
    "write_suggestions",
]
