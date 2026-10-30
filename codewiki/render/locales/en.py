"""English catalog.

Keys must match ``zh.CATALOG`` exactly (enforced by ``tests/test_i18n_catalog.py``);
placeholders too, or switching language blows up only in English and only at runtime.

译文口径：
* 表头、标题用句首大写（Sentence case），与本项目 README 的英文风格一致；
* 不动 ``codewiki`` / ``.mmd`` / 代码标识符；
* 中文的破折号 ``——`` 在英文里退成全角破折号的两倍宽不合适，用 em dash ``—``。
"""

from __future__ import annotations

CATALOG: dict[str, str] = {
    "common.unlayered": "Unlayered",
    "site.overview": "Architecture overview",
    "site.chip_modules": "modules",
    "site.chip_nodes": "nodes",
    "site.chip_edges": "edges",
    "site.data_packet": "data packet",
    "site.diagnostics": "diagnostics",
    "site.search_placeholder": "Filter modules (path fragments such as api / service)…",
    "wiki.commit_line": "> commit: {commit} · generated at {at}",
    "wiki.layers": "## Architecture layers",
    "wiki.layer_line": "- **{name}** ({count} modules)",
    "wiki.unlayered_modules": "## Unlayered modules",
    "wiki.modules_no_layers": "## Modules (no layers configured)",
    "wiki.diagnostics": "## Diagnostics",
    "wiki.diag_counts": "- error {errors} / warning {warnings} / total {total}",
    "wiki.diag_full_list": "See [diagnostics.md](diagnostics.md) for the full list.",
    "wiki.language_bit": "language `{lang}`",
    "wiki.layer_bit": "layer {name}",
    "wiki.annotations": "## Annotations",
    "wiki.tag_human": "human",
    "wiki.tag_ai": "AI (reviewed={flag})",
    "wiki.annotation_line": "- {label}{note} — *{tag}*",
    "wiki.context_diagram": "## Context diagram",
    "wiki.symbols": "## Symbols",
    "wiki.symbol_header": "| Name | Kind | Line | Signature | Docs |",
    "wiki.neighbors": "## Neighbors",
    "wiki.upstream": "**Upstream** (who uses me)",
    "wiki.downstream": "**Downstream** (what I depend on)",
    "wiki.neighbor_line": "- {link} ({relation})",
    "wiki.external": "(external)",
    "wiki.back_to_index": "[← Back to index](../{index})",
    "layer_map.root": "(root)",
    "layer_map.module_stats": "{modules} modules · {symbols} symbols",
    "layer_map.cross_layer_top": "Top cross-layer refs {count}",
    "layer_map.no_cross_layer": "no cross-layer refs",
    "layer_map.more_flows": "… {count} more pairs",
    "diagrams.paged_title": "# Architecture overview (paged)",
    "diagrams.paged_note": "Node count exceeds the per-page limit ({max}); split into {pages} pages by layer.",
    "diagrams.page_heading": "## Page {index} — `{file}`",
    "diagrams.drill_down": "- `{path}` (drill down: [{slug}.mmd](modules/{slug}.mmd))",
    "diagrams.cycle_hint": "%% cycle hint: sink `{name}` first (most depended-on inside the cycle), so the rest flows upper → lower",
    "diagnostics.title": "# codewiki diagnostics",
    "diagnostics.empty": "No diagnostics.",
}
