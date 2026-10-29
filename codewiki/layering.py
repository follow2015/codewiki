"""分层规划与匹配：唯一算法来源（§3.2 / §4.6.3）。

为什么单独抽一个模块：同样的"最长前缀匹配"此前在 ``config.layer_of`` 与
``graph.builder._layer_of`` 各写了一份 —— 两处只要有一处改了口径（比如未来改成
glob），就会出现"CLI 认为模块属于服务层、引擎认为是数据层"的幽灵不一致。
配置层负责提供 (层 id, paths)，引擎层负责在建图时用同一套算法，两者共用这里。

同样地，"目录怎么归层、层怎么排序"也被两条路径共用（无配置兜底分层与
``suggest-layers``）：这是同一种判断，不该因为入口不同而给出不同答案。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import networkx as nx

UNLAYERED_LAYER_ID = "layer::_unlayered"

DEFAULT_MAX_GROUP = 30


def longest_prefix_match(rel_path: str, layers: Iterable[tuple[str, list[str]]]) -> tuple[int, str] | None:
    """按 paths 前缀匹配，返回 ``(匹配长度, 层 id)``；无命中返回 ``None``。

    **最长前缀优先**：否则 ``app/`` 会抢在 ``app/services/`` 之前命中，细分层
    形同虚设。这是三条腥甜的分层 bug 里的第一条，别改成"首个命中即返回"。
    """
    best: tuple[int, str] | None = None
    for layer_id, paths in layers:
        for pattern in paths:
            prefix = _normalize(pattern)
            hit = prefix == "" or rel_path == prefix or rel_path.startswith(prefix + "/")
            if hit and (best is None or len(prefix) > best[0]):
                best = (len(prefix), layer_id)
    return best


def _normalize(pattern: str) -> str:
    """``"."`` / ``"./"`` / ``""`` → ``""``（仓库根）。

    `paths = ["."]` 是很常见的写法（"除细分层之外的都归我"）；不归一就会永远
    匹配不上 —— 症状是**这一层的 subgraph 是空的**，而且只在真正出图时才会
    被发现（单看数据包的分层计数也容易漏）。
    """
    stripped = pattern.strip().rstrip("/")
    return "" if stripped in ("", ".") else stripped


def layer_of(
    rel_path: str, layers: Iterable[tuple[str, list[str]]], *, default: str = UNLAYERED_LAYER_ID
) -> str:
    """匹配层 id；无命中返回 ``default``（默认虚拟层，由调用方决定是否出诊断）。"""
    matched = longest_prefix_match(rel_path, layers)
    return matched[1] if matched else default


OTHER_GROUP_NAME = "other"
ROOT_GROUP_NAME = "root"
RESERVED_GROUP_NAMES = frozenset({UNLAYERED_LAYER_ID.removeprefix("layer::")})


@dataclass
class DirGroup:
    """按目录归并出来的一组模块（建议分层与"无配置兜底分层"共用）。"""

    name: str
    prefixes: list[str]
    modules: list[str] = field(default_factory=list)


def unique_names(names: Iterable[str], *, reserved: Iterable[str] = RESERVED_GROUP_NAMES) -> list[str]:
    """层名去重（保留名不可占用）；冲突者依次追加 ``-2``、``-3``…

    **必须只在这里实现一次**：层 id 是 ``layer::<name>``，`derive_layer_specs`
    与 `suggest_layers` 都要保证名字唯一。任何一处漏掉的表现都一样 —— 两个不同的
    目录组被合并成同一层（模块归属错），并且渲染出同 id 的重复 subgraph。此前
    只有 `suggest_layers` 自己写了一份 `while name in used_names`，兜底分层那条路
    完全没有，于是"仓库里恰好有个 ``other/`` 目录"就会静默出错。

    分配顺序即输入顺序（调用方先排序），所以结果确定；``taken`` 预先收进全部
    原始名字，避免改名后又撞上后面某个目录的真名。

    **签名收 ``Iterable`` 就必须对迭代器安全**：``taken`` 先用 ``set(names)`` 把
    生成器耗尽，再由下面的循环遍历同一个对象，会拿到空结果 —— 一个"传生成器就
    静默返回 []"的坑。现有调用方恰好都传 list，所以一直没暴露。
    """
    names = list(names)
    reserved_set = set(reserved)
    taken = reserved_set | set(names)
    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        chosen = name
        if chosen in seen or chosen in reserved_set:
            suffix = 2
            while f"{name}-{suffix}" in taken:
                suffix += 1
            chosen = f"{name}-{suffix}"
            taken.add(chosen)
        seen.add(chosen)
        result.append(chosen)
    return result


def _split_by_next_segment(prefix: str, members: list[str]) -> dict[str, list[str]]:
    """按下一级目录拆组；直接躺在 ``prefix`` 下的文件留在 ``prefix`` 自己这组。"""
    split: dict[str, list[str]] = {}
    for path in members:
        rest = path[len(prefix) + 1 :] if prefix else path
        child = f"{prefix}/{rest.split('/', 1)[0]}" if "/" in rest else prefix
        split.setdefault(child, []).append(path)
    return split


def _group_name(prefix: str) -> str:
    return prefix.replace("/", ".") if prefix else ROOT_GROUP_NAME


def _descend_oversized(groups: dict[str, list[str]], max_group: int) -> None:
    """组超过 ``max_group`` 就按子目录继续下钻（原地改 ``groups``）。

    "下钻不动"要当成正常结局：平铺目录（``big/`` 下直接躺 90 个文件）本来就拆不出
    更细的目录信号 —— 那种情况留给社区发现（``suggest-layers``），或者老实承认
    它就是一层。这里只保证**不会无限循环**：每轮要么组数增加，要么出队。
    """
    queue = [prefix for prefix, members in groups.items() if prefix and len(members) > max_group]
    while queue:
        prefix = queue.pop()
        members = groups.get(prefix)
        if members is None or len(members) <= max_group:
            continue
        split = _split_by_next_segment(prefix, members)
        if set(split) == {prefix}:
            continue
        del groups[prefix]
        groups.update(split)
        queue.extend(child for child in split if child != prefix and len(split[child]) > max_group)


def directory_groups(
    module_paths: Iterable[str],
    *,
    dominant: float = 0.6,
    max_depth: int = 3,
    min_group: int = 2,
    max_group: int | None = None,
) -> list[DirGroup]:
    """把模块按目录归成组：**目录结构是最强的分层信号**，先用它，不够再下钻。

    1. 先按第一段目录分组，仓库根下的文件单独成 ``root`` 组；
    2. 若某组占全部模块的比例超过 ``dominant``（典型：整个项目都在 ``src/`` 下），
       就把它按下一级目录拆开 —— 否则"分层"只是一个目录名，没有信息量；
    3. 给了 ``max_group`` 时，任何超过该数的组继续按子目录下钻（可以连下几级），
       直到不超限或下钻不动 —— 这样"一个包 90 个模块"不会顶着"层"的名义留在图上；
    4. 模块数少于 ``min_group`` 的组合并成 ``other``（单模块的"层"只会让图碎掉）；
       只有一个这样的组时保持原样。

    结果确定（按组大小降序、同大小按名字排序），且**组名唯一**（见 :func:`unique_names`）。
    """
    paths = sorted(set(module_paths))
    total = len(paths)
    if total == 0:
        return []

    groups: dict[str, list[str]] = {}
    for path in paths:
        prefix = path.split("/", 1)[0] if "/" in path else ""
        groups.setdefault(prefix, []).append(path)

    for _depth in range(1, max_depth):
        candidates = [(prefix, members) for prefix, members in groups.items() if prefix]
        if not candidates:
            break
        prefix, members = max(candidates, key=lambda item: (len(item[1]), item[0]))
        if len(members) / total <= dominant:
            break
        split = _split_by_next_segment(prefix, members)
        if len(split) < 2:
            break
        del groups[prefix]
        groups.update(split)

    if max_group is not None:
        _descend_oversized(groups, max_group)

    big = {prefix: members for prefix, members in groups.items() if len(members) >= min_group or prefix == ""}
    small = {prefix: members for prefix, members in groups.items() if prefix not in big}
    result = [
        DirGroup(_group_name(prefix), [prefix] if prefix else [], members) for prefix, members in big.items()
    ]
    if len(small) >= 2:
        merged = sorted(m for members in small.values() for m in members)
        result.append(DirGroup(OTHER_GROUP_NAME, sorted(small), merged))
    else:
        result.extend(DirGroup(_group_name(prefix), [prefix], members) for prefix, members in small.items())
    result.sort(key=lambda g: (-len(g.modules), g.name))
    for group, name in zip(result, unique_names([g.name for g in result]), strict=True):
        group.name = name
    return result


def rank_layers(assignment: dict[str, int], graph: nx.DiGraph, count: int) -> tuple[dict[int, int], set[int]]:
    """层序：跨层依赖边 → 强连通分量压缩 → 拓扑深度（没人依赖的层在最上）。

    ``assignment`` 是"模块/节点 id → 层下标"。成环的层共享同一深度并被标出来 ——
    环本身就是"分层并不清晰"的证据，比任何单层结论都更该先看。
    """
    layer_graph = nx.DiGraph()
    layer_graph.add_nodes_from(range(count))
    for source, target in graph.edges():
        a, b = assignment.get(source), assignment.get(target)
        if a is not None and b is not None and a != b:
            layer_graph.add_edge(a, b)

    condensed = nx.condensation(layer_graph)
    component_of = condensed.graph["mapping"]
    depth: dict[int, int] = {}
    for node in nx.topological_sort(condensed):
        preds = list(condensed.predecessors(node))
        depth[node] = 1 + max(depth[p] for p in preds) if preds else 0

    cyclic = {layer for layer in range(count) if len(condensed.nodes[component_of[layer]]["members"]) > 1}
    return {layer: depth[component_of[layer]] + 1 for layer in range(count)}, cyclic


__all__: list[Any] = [
    "DEFAULT_MAX_GROUP",
    "DirGroup",
    "OTHER_GROUP_NAME",
    "RESERVED_GROUP_NAMES",
    "ROOT_GROUP_NAME",
    "UNLAYERED_LAYER_ID",
    "directory_groups",
    "layer_of",
    "longest_prefix_match",
    "rank_layers",
    "unique_names",
]
