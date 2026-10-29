"""AI 起草分层：让模型按职责归并，再**逐条校验**（``codewiki init --ai``）。

放在 ``ai/`` 而不是 ``layering_draft``，是被依赖方向约束逼出来的正确位置：起草的
静态部分属于 core，而 core 不允许 import ``ai``（``tests/test_dependency_direction``
守着这条线，理由是渲染层一旦能反向 import AI 层，安全边界就只剩文档上的一句话）。
反过来 ``ai`` → ``core`` 是允许的，所以归并逻辑住在这里，复用 core 的分组与层序。

校验为什么必须存在：模型编一个不存在的目录是常见幻觉，而**编出来的层在配置里
看不出任何异常** —— 要等 analyze 跑完看到一层空模块才发现。所以宁可丢层并报，
也不写一份"看起来像配置"的坏输入。
"""

from __future__ import annotations

from typing import Any

from codewiki.ai.client import ChatMessage
from codewiki.ai.jsonx import extract_json_object
from codewiki.ai.prompts import draft_system_prompt, render_draft_user
from codewiki.layering import layer_of, unique_names
from codewiki.layering_draft import (
    AI_COVERAGE_MIN,
    AI_MIN_LAYERS,
    DraftResult,
    LayerDraft,
    count_modules,
    cross_dir_hints,
    directory_draft,
    draft_census,
    finalize_counts,
    layer_cycles,
    order_draft_layers,
)
from codewiki.schemas import Diagnostic


def build_draft_context(all_paths: list[str], *, deps: list[tuple[str, str]] = ()) -> tuple[str, str]:
    """渲染 ``(system, user)`` 提示。"""
    census, edges, total = draft_census(all_paths, deps=deps)
    return draft_system_prompt(), render_draft_user(census=census, edges=edges, total=total)


def _parse_reply(raw: str) -> tuple[list[dict], str]:
    """取 ``{"layers": [...]}``；返回 ``(层列表, 错误原因)``。"""
    value, error = extract_json_object(raw)
    if value is None:
        return [], error
    layers = value.get("layers")
    if not isinstance(layers, list):
        return [], "缺少 layers 数组"
    return [item for item in layers if isinstance(item, dict)], ""


def _to_layers(items: list[dict], all_paths: list[str], result: DraftResult) -> list[LayerDraft]:
    """把模型答复转成草稿，逐条校验；不合规的层丢掉并记账。"""
    drafts: list[LayerDraft] = []
    for item in items:
        name = item.get("name")
        raw_paths = item.get("paths")
        if not isinstance(name, str) or not name.strip():
            result.diagnostics.append(
                Diagnostic(
                    level="warning",
                    code="AI_LAYER_PATH_UNMATCHED",
                    message=f"层缺少有效名字，已忽略: {item}",
                )
            )
            continue
        if not isinstance(raw_paths, list) or not raw_paths:
            result.diagnostics.append(
                Diagnostic(
                    level="warning",
                    code="AI_LAYER_PATH_UNMATCHED",
                    message=f"层 {name!r} 没有给出 paths，已忽略",
                    where=name,
                )
            )
            continue
        paths = [str(p).strip().rstrip("/") for p in raw_paths if str(p).strip()]
        modules = count_modules(paths, all_paths)
        if modules == 0:
            result.diagnostics.append(
                Diagnostic(
                    level="warning",
                    code="AI_LAYER_PATH_UNMATCHED",
                    message=f"层 {name!r} 的 paths {paths} 没有命中任何模块，已丢弃",
                    where=name,
                )
            )
            continue
        drafts.append(LayerDraft(name=name.strip(), paths=paths, modules=modules, source="ai"))
    return drafts


def ai_draft(
    all_paths: list[str],
    *,
    deps: list[tuple[str, str]] = (),
    client,  # noqa: ANN001 - OpenAICompatClient / DeferredClient
) -> DraftResult:
    """让模型按职责归并，再逐条校验；任一项不成立就退回静态骨架。

    校验是**分级**的，因为不同错误的严重程度不同：

    * 层名重复 ⇒ 整份拒绝。层 id 是 ``layer::<name>``，重名会静默合并两个不同的组
      （而且渲染出同 id 的重复 subgraph），这是"看着成功、结果错得离谱"的典型；
    * path 不命中 ⇒ 只丢这一层。模型编一个目录是常见幻觉，其余层通常仍然可用；
    * 覆盖率不足 ⇒ 采用但告警。少分层是"没分够"，不是"分错了"，人补几行就行。
    """
    paths = sorted(set(all_paths))
    fallback = directory_draft(paths, deps=deps)
    if not paths:
        return fallback

    system, user = build_draft_context(paths, deps=deps)
    try:
        reply = client.chat(
            [ChatMessage(role="system", content=system), ChatMessage(role="user", content=user)]
        )
    except Exception as exc:  # noqa: BLE001 - LLMError 与网关的各种抖动都在这里归一
        fallback.diagnostics.append(
            Diagnostic(
                level="warning", code="AI_LAYER_UNAVAILABLE", message=f"AI 不可用，已退回目录骨架: {exc}"
            )
        )
        return fallback

    raw = getattr(reply, "content", "") or ""
    items, error = _parse_reply(raw)
    if error:
        fallback.diagnostics.append(
            Diagnostic(
                level="warning",
                code="AI_LAYER_PARSE_FAILED",
                message=f"模型答复无法解析（{error}），已退回目录骨架",
            )
        )
        fallback.raw_reply = raw
        return fallback

    result = DraftResult(source="ai", raw_reply=raw)
    drafts = _to_layers(items, paths, result)

    names = [draft.name for draft in drafts]
    duplicated = {name for name in names if names.count(name) > 1}
    if duplicated:
        fallback.diagnostics.append(
            Diagnostic(
                level="warning",
                code="AI_LAYER_DUPLICATE_NAME",
                message=(
                    f"模型给出的层名重复（{', '.join(sorted(duplicated))}）；"
                    "层 id 是 layer::<name>，重名会静默合并，已退回目录骨架"
                ),
            )
        )
        return fallback
    if len(drafts) < AI_MIN_LAYERS:
        fallback.diagnostics.append(
            Diagnostic(
                level="warning",
                code="AI_LAYER_COVERAGE_LOW",
                message=f"模型只给出 {len(drafts)} 层，不构成分层，已退回目录骨架",
            )
        )
        return fallback

    unique = unique_names(draft.name for draft in drafts)
    drafts = [
        LayerDraft(name=name, paths=draft.paths, modules=draft.modules, source="ai")
        for name, draft in zip(unique, drafts, strict=True)
    ]
    drafts, _cyclic = order_draft_layers(drafts, paths, deps)
    drafts = finalize_counts(drafts, paths)
    pairs = [(draft.id, draft.paths) for draft in drafts]
    uncovered = [path for path in paths if layer_of(path, pairs) == "layer::_unlayered"]

    result.layers = drafts
    result.uncovered = uncovered
    if uncovered:
        covered = len(paths) - len(uncovered)
        result.diagnostics.append(
            Diagnostic(
                level="warning",
                code="AI_LAYER_COVERAGE_LOW",
                message=(
                    f"模型分层只覆盖 {covered}/{len(paths)} 个模块（{covered / len(paths):.0%}，"
                    f"建议 ≥ {AI_COVERAGE_MIN:.0%}）；未覆盖的模块会落进 _unlayered"
                ),
            )
        )
    result.hints = cross_dir_hints(paths, deps)
    result.cycles = layer_cycles(drafts, paths, deps)
    return result


__all__: list[Any] = ["ai_draft", "build_draft_context"]
