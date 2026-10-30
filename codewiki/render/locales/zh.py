"""中文文案（默认语言，也是其他语言的键集基准）。

这里的字符串就是产物里逐字出现的内容 —— 改动会被端到端用例抓到，不要为了
"更通顺"随手改：``index.md`` / ``diagnostics.md`` 的现有断言盯着它们。
"""

from __future__ import annotations

CATALOG: dict[str, str] = {
    "common.unlayered": "未分层",
    "site.overview": "架构总览",
    "site.chip_modules": "模块",
    "site.chip_nodes": "节点",
    "site.chip_edges": "边",
    "site.data_packet": "标准数据包",
    "site.diagnostics": "诊断清单",
    "site.search_placeholder": "筛选模块（支持路径片段，如 api / service）…",
    "wiki.commit_line": "> 提交：{commit} · 生成于 {at}",
    "wiki.layers": "## 架构分层",
    "wiki.layer_line": "- **{name}**（{count} 个模块）",
    "wiki.unlayered_modules": "## 未分层模块",
    "wiki.modules_no_layers": "## 模块清单（未配置分层）",
    "wiki.diagnostics": "## 诊断摘要",
    "wiki.diag_counts": "- error {errors} / warning {warnings} / 共 {total}",
    "wiki.diag_full_list": "完整清单见 [diagnostics.md](diagnostics.md)。",
    "wiki.language_bit": "语言 `{lang}`",
    "wiki.layer_bit": "层 {name}",
    "wiki.annotations": "## 注解",
    "wiki.tag_human": "人工",
    "wiki.tag_ai": "AI（reviewed={flag}）",
    "wiki.annotation_line": "- {label}{note} —— *{tag}*",
    "wiki.context_diagram": "## 上下文图",
    "wiki.symbols": "## 符号",
    "wiki.symbol_header": "| 名称 | 类型 | 行 | 签名 | 说明 |",
    "wiki.neighbors": "## 上下游",
    "wiki.upstream": "**上游**（谁用到我）",
    "wiki.downstream": "**下游**（我依赖谁）",
    "wiki.neighbor_line": "- {link} （{relation}）",
    "wiki.external": "（外部）",
    "wiki.back_to_index": "[← 返回索引](../{index})",
    "layer_map.root": "(根)",
    "layer_map.module_stats": "{modules} 模块 · {symbols} 符号",
    "layer_map.cross_layer_top": "跨层引用 TOP {count}",
    "layer_map.no_cross_layer": "无跨层引用",
    "layer_map.more_flows": "… 还有 {count} 对",
    "diagrams.paged_title": "# 宏观架构图（分页）",
    "diagrams.paged_note": "节点数超过单图上限 {max}，已按层拆成 {pages} 页。",
    "diagrams.page_heading": "## 第 {index} 页 — `{file}`",
    "diagrams.drill_down": "- `{path}`（下钻：[{slug}.mmd](modules/{slug}.mmd)）",
    "diagrams.cycle_hint": "%% 断环建议：优先下沉 `{name}`（环内被依赖最多），使其余依赖变为上层→下层",
    "diagnostics.title": "# codewiki 诊断",
    "diagnostics.empty": "无诊断输出。",
}
