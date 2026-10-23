"""分层匹配：唯一算法来源（§3.2）。

为什么单独抽一个模块：同样的"最长前缀匹配"此前在 ``config.layer_of`` 与
``graph.builder._layer_of`` 各写了一份 —— 两处只要有一处改了口径（比如未来改成
glob），就会出现"CLI 认为模块属于服务层、引擎认为是数据层"的幽灵不一致。
配置层负责提供 (层 id, paths)，引擎层负责在建图时用同一套算法，两者共用这里。
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

UNLAYERED_LAYER_ID = "layer::_unlayered"


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


def layer_of(rel_path: str, layers: Iterable[tuple[str, list[str]]], *, default: str = UNLAYERED_LAYER_ID) -> str:
    """匹配层 id；无命中返回 ``default``（默认虚拟层，由调用方决定是否出诊断）。"""
    matched = longest_prefix_match(rel_path, layers)
    return matched[1] if matched else default


__all__: list[Any] = ["UNLAYERED_LAYER_ID", "layer_of", "longest_prefix_match"]
