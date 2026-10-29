"""从目录与依赖**起草**分层配置：静态骨架 + 可选 AI 归并（``codewiki init --src``）。

为什么要这个模块：手写一份 8 层配置实测要 30 分钟、3 轮试错（先把 ``config.py``
归错了层，108 条 ``from extensions import db`` 立刻伪造出"数据层→进程入口"）。而
完全不写交给兜底推断会碎成 120 层 —— 每层一个点分目录名，比不写还难读。

两者都没有踩到中间，因为 :func:`codewiki.layering.directory_groups` 是**自顶向下
切分**，而真实的架构层常要**跨目录归并**：``app/api`` + ``app/openapi`` +
``realtime_gateway`` 同属"接口层"。这种判断目录树推不出来，只能由人（或模型看着
依赖图）指定。

所以这里给两档，人工只做最后一步：

* :func:`directory_draft` —— 零凭据的骨架：粗粒度锚定 + 按依赖排层序 + 把
  "哪些目录互相依赖"点名出来（那正是人工花时间的地方）；
* :func:`ai_draft` —— 模型按职责给层，但输出**逐条校验**：path 必须命中真实模块
  （否则丢层并报）、层名必须唯一（否则整份拒绝）、覆盖率过低要显式告警。任一项
  不成立就退回静态骨架 —— 宁可给一份朴素的可用配置，也不写一份"看起来像配置"
  的坏输入。

两条路径共用同一份"算层序"实现，否则同一批层会因为入口不同而排出两种顺序。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import networkx as nx

from codewiki.config import LayerSpec
from codewiki.layering import (
    OTHER_GROUP_NAME,
    ROOT_GROUP_NAME,
    DirGroup,
    directory_groups,
    layer_of,
    rank_layers,
    unique_names,
)
from codewiki.schemas import Diagnostic

COARSE_MAX_GROUP = 120

HINT_MIN_EDGES = 2

AI_COVERAGE_MIN = 0.9

AI_MIN_LAYERS = 2

MERGE_ADVICE_MAX_LAYERS = 4


@dataclass(frozen=True)
class LayerDraft:
    """一层草稿。``paths`` 语义与 :class:`codewiki.config.LayerSpec` 完全一致。"""

    name: str
    paths: list[str]
    order: int | None = None
    modules: int = 0
    source: str = "directory"

    @property
    def id(self) -> str:
        return f"layer::{self.name}"


@dataclass(frozen=True)
class CrossDirHint:
    """两个目录互相依赖 —— 目录树说是两层，依赖图说它们可能是一层。"""

    a: str
    b: str
    ab: int
    ba: int


@dataclass
class DraftResult:
    """一次起草的全部结果（配置文本、诊断、待人工决策的提示）。"""

    layers: list[LayerDraft] = field(default_factory=list)
    hints: list[CrossDirHint] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    uncovered: list[str] = field(default_factory=list)
    cycles: list[list[str]] = field(default_factory=list)
    source: str = "directory"
    raw_reply: str = ""

    @property
    def coverage(self) -> float:
        total = len(self.uncovered) + sum(layer.modules for layer in self.layers)
        if total == 0:
            return 1.0
        return sum(layer.modules for layer in self.layers) / total

    def has_code(self, code: str) -> bool:
        return any(d.code == code for d in self.diagnostics)

    def render_notes(self) -> str:
        """给终端看的说明：归并提示优先，其次覆盖率与诊断。

        这些内容**不写进配置文件**（TOML 注释会被下一次渲染覆盖，且不该成为
        机器解析的前缀），只在命令输出里出现一次。
        """
        lines: list[str] = []
        if self.cycles:
            lines.append("层环提示（这些层互相依赖成环，analyze 会报 LAYER_CYCLE）：")
            for group in self.cycles:
                lines.append("  - " + "、".join(group))
                if len(group) <= MERGE_ADVICE_MAX_LAYERS:
                    lines.append("    ⇒ 职责可能被拆开了，考虑合并成一层。")
                else:
                    lines.append(
                        f"    ⇒ 环里 {len(group)} 层太多，通常说明当前粒度与真实依赖不符，"
                        "而不是「把这几层并成一层」；先按下面的目录级提示收拢强耦合的几对。"
                    )
        if self.hints:
            lines.append(f"跨目录归并提示（双向依赖 ≥ {HINT_MIN_EDGES} 条，可能属于同一职责）：")
            for hint in self.hints:
                lines.append(
                    f"  - {hint.a} ⇄ {hint.b} 互相依赖："
                    f"{hint.a} -> {hint.b} {hint.ab} 条、{hint.b} -> {hint.a} {hint.ba} 条"
                )
            lines.append("  ⇒ 若它们其实是同一层，把两者的 paths 合并到一层即可。")
        if self.uncovered:
            shown = ", ".join(self.uncovered[:6])
            more = f" 等 {len(self.uncovered)} 个" if len(self.uncovered) > 6 else ""
            lines.append(f"未归层模块（{self.coverage:.0%} 已覆盖）：{shown}{more}")
        for diagnostic in self.diagnostics:
            lines.append(f"[{diagnostic.level}] {diagnostic.code}: {diagnostic.message}")
        return "\n".join(lines)


def _coarse_max_group(total: int) -> int:
    """起草用的组大小上限：**超过全仓一半才继续下钻**。

    小仓库套 30 这种门槛会把 6 个模块的包拆成两半，得到"层"比模块还多的配置；
    大仓库又不能不拆（``app`` 695 个模块顶着"层"的名义没有信息量）。取一半的
    理由：一个占全仓一半以上的组必然盖住了多个职责（如 695/1320 的后端 vs 610 的
    前端），拆它有意义；不到一半的组通常是真的一层，拆了反而碎。

    实测 ipip arch-e0（约 1300 个模块）：``app``(695) 拆成子包共约 10 层、
    ``frontend-new``(610) 保持一层，总计 14 层 —— 与手写 8 层同一量级，人工再合并
    两三组就是终稿。
    """
    return max(COARSE_MAX_GROUP, total // 2)


def _paths_of(group) -> list[str]:  # noqa: ANN001 - DirGroup
    """组的匹配前缀；仓库根下的散文件没有前缀，只能逐个列出来。"""
    return list(group.prefixes) if group.prefixes else sorted(group.modules)


def count_modules(layer_paths: list[str], all_paths: list[str]) -> int:
    """单独看这一层时命中多少模块（**不看别的层**）。

    只用于"模型给的 path 是否指向真实代码"这类局部判断。最终归属数量必须用
    :func:`finalize_counts` 重算 —— 层之间会重叠（``app`` 与 ``app/api``），
    按本函数逐层相加会得到比模块总数还大的和。
    """
    pairs = [("layer::_probe", layer_paths)]
    return sum(1 for path in all_paths if layer_of(path, pairs) != "layer::_unlayered")


def finalize_counts(layers: list[LayerDraft], all_paths: list[str]) -> list[LayerDraft]:
    """按**最终归属**回填每层的模块数（最长前缀优先，一个模块只算一层）。

    之前每层各自数一遍：``app`` 命中全部 app 模块、``app/api`` 又命中其中 60 个，
    两层相加就超过了仓库总模块数（实测 2009 > 1322），人一看数字就知道这份配置
    不对劲 —— 但问题不在配置，在计数。
    """
    pairs = [(layer.id, layer.paths) for layer in layers]
    counts: Counter = Counter(layer_of(path, pairs) for path in all_paths)
    return [
        LayerDraft(
            name=layer.name,
            paths=layer.paths,
            order=layer.order,
            modules=counts.get(layer.id, 0),
            source=layer.source,
        )
        for layer in layers
    ]


def order_draft_layers(
    layers: list[LayerDraft], all_paths: list[str], deps: list[tuple[str, str]]
) -> tuple[list[LayerDraft], set[int]]:
    """按依赖方向给层编号（1 = 最上层），并归一化成连续的 ``1..N``。

    ``rank_layers`` 给的是拓扑深度，并列层（互不依赖、或同处一个环内）会拿到相同
    深度。直接照抄会出现 ``1,1,2,3`` 这种序列 —— 读起来像排错了，且下游按 order
    排序时并列层的相对位置变成偶然。这里按 ``(深度, 层名)`` 稳定排序后重编号，
    既保住了深度关系，又保证是严格递增的排列。
    """
    graph = nx.DiGraph()
    graph.add_nodes_from(all_paths)
    graph.add_edges_from((a, b) for a, b in deps if a in set(all_paths) and b in set(all_paths))

    pairs = [(layer.id, layer.paths) for layer in layers]
    index_of = {layer.id: position for position, layer in enumerate(layers)}
    assignment = {
        path: index_of[layer_of(path, pairs)] for path in all_paths if layer_of(path, pairs) in index_of
    }
    raw, cyclic = rank_layers(assignment, graph, len(layers)) if layers else ({}, set())

    ranked = sorted(range(len(layers)), key=lambda i: (raw.get(i, 0), layers[i].name))
    order_of = {index: position + 1 for position, index in enumerate(ranked)}
    return [
        LayerDraft(
            name=layer.name,
            paths=layer.paths,
            order=order_of[index],
            modules=layer.modules,
            source=layer.source,
        )
        for index, layer in enumerate(layers)
    ], cyclic


def _group_edges(all_paths: list[str], deps: list[tuple[str, str]]) -> tuple[dict[str, str], Counter]:
    """模块路径 → 组名，以及组间边计数。分组口径与骨架完全一致。"""
    groups = directory_groups(all_paths, max_group=_coarse_max_group(len(all_paths)))
    owner: dict[str, str] = {}
    for group in groups:
        pairs = [(group.name, _paths_of(group))]
        for path in all_paths:
            if layer_of(path, pairs) != "layer::_unlayered":
                owner.setdefault(path, group.name)
    edges: Counter = Counter()
    for source, target in deps:
        a, b = owner.get(source), owner.get(target)
        if a is not None and b is not None and a != b:
            edges[(a, b)] += 1
    return owner, edges


def _merge_hint_is_useless(a: str, b: str) -> bool:
    """这两组的"互相依赖"是不是必然现象、建议合并会不会是噪音。

    两类明确无意义：

    * **父子关系**（``app`` ⇄ ``app.utils``）：父组装的是直接躺在父目录下的散文件，
      子组是子包 —— 它们互相 import 是目录结构的必然，不是"同一职责被拆开"；
    * **``root`` 参与**（``root`` ⇄ ``app.utils``）：``root`` 是仓库根的散文件集合
      （``config.py``、``run.py``），它不是一个职责单元，"合并进某层"没有意义。

    实测 ipip arch-e0：不加这两条，5 条提示里 4 条是这两类，唯一有价值的那条
    （``app.persistence`` ⇄ ``app.services``，125 条服务层调持久层）会被埋掉。
    """
    if a == ROOT_GROUP_NAME or b == ROOT_GROUP_NAME:
        return True
    return a.startswith(f"{b}.") or b.startswith(f"{a}.")


def cross_dir_hints(all_paths: list[str], deps: list[tuple[str, str]] = ()) -> list[CrossDirHint]:
    """双向依赖都够重的组对 —— 目录树说两层，依赖图说可能是一层。

    对外只收"模块路径 + 依赖边"，不收中间的分组结果：调用方（``ai.draft``）改不了
    分组口径，就不会出现"提示按 A 口径分组、校验按 B 口径分组"的漂移。
    """
    _owner, edges = _group_edges(sorted(set(all_paths)), deps)
    seen: set[tuple[str, str]] = set()
    hints: list[CrossDirHint] = []
    for (a, b), ab in sorted(edges.items()):
        if (a, b) in seen or _merge_hint_is_useless(a, b):
            continue
        ba = edges.get((b, a), 0)
        seen.add((a, b))
        seen.add((b, a))
        if ab >= HINT_MIN_EDGES and ba >= HINT_MIN_EDGES:
            hints.append(CrossDirHint(a=a, b=b, ab=ab, ba=ba))
    hints.sort(key=lambda h: (-(h.ab + h.ba), h.a, h.b))
    return hints


def layer_cycles(
    layers: list[LayerDraft], all_paths: list[str], deps: list[tuple[str, str]]
) -> list[list[str]]:
    """把层间依赖压成层图，返回所有**成环**的层组（每项是一组层名）。

    为什么起草阶段就要算：``analyze`` 只会报一句"分层出现环"，人得自己顺着
    ``app.core → app.exceptions → …`` 一条条找该合并哪几层。实测 ipip arch-e0 的
    18 层草稿里有 4 个 ``LAYER_CYCLE``，而互相依赖的层名一列出来，判断就是一瞬间的事。

    只报 ``size > 1`` 的强连通分量：自环（层内模块互相 import）不是分层问题。
    """
    pairs = [(layer.id, layer.paths) for layer in layers]
    index_of = {layer.id: position for position, layer in enumerate(layers)}
    known = set(all_paths)
    layer_graph = nx.DiGraph()
    layer_graph.add_nodes_from(range(len(layers)))
    for source, target in deps:
        if source not in known or target not in known:
            continue
        a, b = index_of.get(layer_of(source, pairs)), index_of.get(layer_of(target, pairs))
        if a is not None and b is not None and a != b:
            layer_graph.add_edge(a, b)
    groups = [
        sorted(layers[index].name for index in component)
        for component in nx.strongly_connected_components(layer_graph)
        if len(component) > 1
    ]
    groups.sort(key=lambda group: (-len(group), group))
    return groups


def _merge_singletons(groups: list) -> list:  # noqa: ANN001 - list[DirGroup]
    """把只剩 1 个模块的组并进 ``other``。

    ``directory_groups`` 的 ``min_group`` 只作用于**顶层**分组；``_descend_oversized``
    下钻出来的小组不受它约束，于是一个只含单个文件的包会独占一层（实测
    ``app.infra`` 1 个模块）。一层一个模块不是分层，是目录清单。
    """
    if len(groups) < 2:
        return groups
    small = [group for group in groups if len(group.modules) < 2]
    if not small or len(small) == len(groups):
        return groups
    keep = [group for group in groups if len(group.modules) >= 2]
    prefixes: list[str] = []
    modules: list[str] = []
    for group in small:
        modules.extend(group.modules)
        for prefix in group.prefixes or group.modules:
            if prefix not in prefixes:
                prefixes.append(prefix)
    return [*keep, DirGroup(name=OTHER_GROUP_NAME, prefixes=prefixes, modules=modules)]


def directory_draft(
    all_paths: list[str], *, deps: list[tuple[str, str]] = (), max_group: int | None = None
) -> DraftResult:
    """纯静态骨架：按目录粗分、按依赖排序、点名互相依赖的目录对。

    粗分规则只有一条但很关键：**不让任何一层碎到"模块数比层数还少"**。做法是把
    ``max_group`` 抬到"总量的一半"（且不低于 :data:`COARSE_MAX_GROUP`），只在某组
    确实占到全仓一半以上时才继续下钻 —— 兜底分层用的 30 是"防止一层独大"，用在这里
    会直接碎成上百层。
    """
    paths = sorted(set(all_paths))
    if not paths:
        return DraftResult()

    groups = directory_groups(
        paths, max_depth=3, min_group=2, max_group=max_group or _coarse_max_group(len(paths))
    )
    groups = _merge_singletons(groups)
    names = unique_names(group.name for group in groups)
    layers = [
        LayerDraft(
            name=name,
            paths=_paths_of(group),
            modules=len(group.modules),
            source="directory",
        )
        for name, group in zip(names, groups, strict=True)
    ]

    layers, _cyclic = order_draft_layers(layers, paths, deps)
    layers = finalize_counts(layers, paths)
    pairs = [(layer.id, layer.paths) for layer in layers]
    uncovered = [path for path in paths if layer_of(path, pairs) == "layer::_unlayered"]

    return DraftResult(
        layers=layers,
        hints=cross_dir_hints(paths, deps),
        uncovered=uncovered,
        cycles=layer_cycles(layers, paths, deps),
        source="directory",
    )


def draft_census(all_paths: list[str], *, deps: list[tuple[str, str]] = ()) -> tuple[str, str, int]:
    """给模型的**目录普查 + 组间边**两段文本（外加模块总数）。

    实测仓库 63k 条边、1320 个模块 —— 全塞进提示既超预算也没信息量。聚合成
    "组 → 组"之后只剩几十行，而模型做归并判断需要的正是这个粒度：它要回答的是
    "哪些目录属于同一职责"，不是"谁 import 了谁"。

    返回文本而不是结构化数据：调用方（``ai.draft``）只负责套模板，不负责理解分组
    —— 否则"提示里写的组名"和"校验时用的组名"迟早会漂移。
    """
    paths = sorted(set(all_paths))
    groups = directory_groups(paths, max_group=_coarse_max_group(len(paths)))
    samples = {group.name: sorted(group.modules)[:3] for group in groups}
    sizes = {group.name: len(group.modules) for group in groups}
    prefixes = {group.name: "、".join(_paths_of(group)[:2]) or "（仓库根）" for group in groups}
    _owner, edges = _group_edges(paths, deps)

    census = "\n".join(
        f"- {name}（路径 {prefixes[name]}，{sizes[name]} 个模块）代表: {', '.join(samples[name])}"
        for name in sizes
    )
    edge_lines = (
        "\n".join(f"- {a} -> {b} : {count} 条" for (a, b), count in edges.most_common(60))
        or "（没有跨组依赖）"
    )
    return census, edge_lines, len(paths)


def _toml_string(value: str) -> str:
    """TOML 基本字符串转义（本仓唯一一份，另一处在 ``ai.annotate``）。"""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _toml_array(values: list[str]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def to_layer_specs(draft: DraftResult) -> list[LayerSpec]:
    return [LayerSpec(name=layer.name, paths=layer.paths, order=layer.order) for layer in draft.layers]


_ANALYZER_OF_LANGUAGE = {
    "python": "ast",
    "typescript": "treesitter",
    "javascript": "treesitter",
    "go": "treesitter",
}


def _language_block(languages: list[str]) -> list[str]:
    """渲染 ``[languages]`` 白名单。

    不写这一段的后果：``analyze`` 默认把**所有**文件都登记成模块，
    ``.DS_Store``/``README.md``/``*.db`` 全落进 ``_unlayered`` —— 实测 62 个，
    配置与刚才看到的层一一对不上。
    """
    from codewiki.repo import EXT_LANGUAGE

    exts: dict[str, list[str]] = {}
    for ext, language in EXT_LANGUAGE.items():
        exts.setdefault(language, []).append(ext)
    lines = ["[languages]"]
    for language in languages:
        analyzer = _ANALYZER_OF_LANGUAGE.get(language)
        if analyzer is None:
            continue
        lines.append(
            f'{language} = {{ ext = {_toml_array(sorted(exts.get(language, [])))}, analyzer = "{analyzer}" }}'
        )
    return lines


def render_config_toml(
    draft: DraftResult,
    *,
    project_name: str,
    excludes: list[str] | None = None,
    ts_aliases: dict[str, str] | None = None,
    languages: list[str] | None = None,
) -> str:
    """渲染成能直接喂给 ``analyze --config`` 的配置。

    只写"能用"的字段：``[project]`` + ``[analysis] exclude`` + ``[[architecture.layers]]``。
    归并提示、覆盖率、诊断都留在 :meth:`DraftResult.render_notes` 的终端输出里 —— 把注释写进
    配置文件看起来很贴心，但下一次重新起草会覆盖它们，而人会把注释的消失当信息丢失。
    """
    head = [
        "# codewiki 配置（由 `codewiki init --src` 起草）",
        f"# 层来源: {draft.source}"
        + ("（模型按职责归并，已逐条校验）" if draft.source == "ai" else "（纯目录骨架，未调用模型）"),
        "# 下一步：按需改层名 / 合并同职责层，然后",
        "#   codewiki analyze --src . --out wiki-output",
        "",
        "[project]",
        f"name = {_toml_string(project_name)}",
    ]
    if languages:
        head += ["", *_language_block(languages)]
    analysis: list[str] = []
    if excludes:
        analysis.append(f"exclude = {_toml_array(sorted(set(excludes)))}")
    if ts_aliases:
        pairs = ", ".join(
            f"{_toml_string(key)} = {_toml_string(value)}" for key, value in sorted(ts_aliases.items())
        )
        analysis.append(f"ts_aliases = {{ {pairs} }}")
    if analysis:
        head += ["", "[analysis]", *analysis]
    blocks = [
        "\n".join(
            [
                "[[architecture.layers]]",
                f"name = {_toml_string(layer.name)}",
                f"paths = {_toml_array(layer.paths)}",
                f"order = {layer.order}" if layer.order is not None else "order = 1",
            ]
        )
        for layer in draft.layers
    ]
    return "\n".join(head) + "\n\n" + "\n\n".join(blocks) + "\n"


__all__: list[Any] = [
    "AI_COVERAGE_MIN",
    "COARSE_MAX_GROUP",
    "CrossDirHint",
    "DraftResult",
    "LayerDraft",
    "directory_draft",
    "render_config_toml",
    "to_layer_specs",
]
