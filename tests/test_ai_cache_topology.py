"""拓扑感知缓存键：分层 / 注解 / 项目名变了，旧答案就不能再用。

``[ai] cache_include_topology`` 此前**没有任何读取点**，而标注用的 prompt 里
确实带着项目名、层名列表与人工注解（``render_annotate_user`` 的 title / layers /
annotations）。这三样都来自 ``codewiki.toml``：改了分层或补了注解，同一个模块
该得到的答案就变了 —— 缓存键里不含它们，命中就等于**拿旧语境作答**，表现为
"我明明改了分层，AI 的注解还是按老层说的"。

这不是"可选优化"（原注释的说法），而是正确性要求：v4 提示词把层名与注解净化后
放进了用户提示，键却还是"内容 hash + 模型 + 提示词版本 + 邻居 hash"。

默认值因此从 ``False`` 改成 ``True``；保留开关是因为注解频繁增长的仓库会因此
反复失效（每条新注解都会改变所有模块的键），那种场景可以显式关掉，代价自负。
"""

from __future__ import annotations

import json

from codewiki.ai.annotate import annotate_modules, protected_targets, select_modules
from codewiki.config import AnnotationSpec, ArchitectureSection, CodewikiConfig, LayerSpec
from codewiki.graph import build_graph
from codewiki.render.jsonio import packet_from_parts
from tests.conftest import FakeClient, parse_all


def _packet(root):
    result = build_graph(parse_all(root))
    return packet_from_parts(
        title="mini", layers=[], nodes=result.nodes, edges=result.edges, annotations=[], diagnostics=[]
    )


def _cfg(*, layer: str = "应用层", extra_note: str = "别的模块的说明", topology: bool | None = None):
    ai = CodewikiConfig().ai
    if topology is not None:
        ai = ai.model_copy(update={"cache_include_topology": topology})
    return CodewikiConfig(
        architecture=ArchitectureSection(layers=[LayerSpec(name=layer, paths=["app.py"])]),
        annotations=[AnnotationSpec(target="mod::pkg/util.py", label="工具", note=extra_note)],
        ai=ai,
    )


def _reply(target: str) -> str:
    return json.dumps(
        {
            "modules": [
                {
                    "target": target,
                    "label": "入口",
                    "note": "负责入口编排，见 line 1",
                    "evidence": [{"path": "app.py", "lines": [1]}],
                }
            ],
            "unknown": [],
        }
    )


def _target(root) -> str:
    packet = _packet(root)
    return select_modules(packet, top_n=1, protected=protected_targets(_cfg().annotations, packet))[0]["id"]


def _run(root, cfg, cache_dir, replies):
    client = FakeClient(list(replies))
    annotate_modules(_packet(root), cfg, src_root=root, client=client, top_n=1, cache_dir=cache_dir)
    return client


def test_default_is_enabled():
    """默认必须开：prompt 里已经含拓扑，不把它打进键就是拿旧语境作答。"""
    assert CodewikiConfig().ai.cache_include_topology is True


def test_same_topology_hits_cache(mini_py, tmp_path):
    """先证明缓存本来就会命中，否则下面的"未命中"断言毫无意义。"""
    target = _target(mini_py)
    _run(mini_py, _cfg(), tmp_path, [_reply(target)])

    second = _run(mini_py, _cfg(), tmp_path, [])

    assert second.calls == []


def test_layer_change_invalidates_cache(mini_py, tmp_path):
    """改层名 ⇒ prompt 里的层名列表变了 ⇒ 判断依据变了，不能复用。"""
    target = _target(mini_py)
    _run(mini_py, _cfg(layer="应用层"), tmp_path, [_reply(target)])

    second = _run(mini_py, _cfg(layer="接口层"), tmp_path, [_reply(target)])

    assert second.calls, "分层变了仍命中缓存 ⇒ 用旧语境作答"


def test_annotation_change_invalidates_cache(mini_py, tmp_path):
    """补一条别的模块的人工注解 ⇒ prompt 里的注解段变了，同样不能复用。"""
    target = _target(mini_py)
    _run(mini_py, _cfg(extra_note="原来的说明"), tmp_path, [_reply(target)])

    second = _run(mini_py, _cfg(extra_note="补充了一条口径说明"), tmp_path, [_reply(target)])

    assert second.calls, "人工注解变了仍命中缓存 ⇒ 用旧语境作答"


def test_switched_off_keeps_hitting_across_topology_change(mini_py, tmp_path):
    """显式关掉时按老行为：拓扑不进键。这条锁的是"开关真的能关"。"""
    target = _target(mini_py)
    _run(mini_py, _cfg(layer="应用层", topology=False), tmp_path, [_reply(target)])

    second = _run(mini_py, _cfg(layer="接口层", topology=False), tmp_path, [])

    assert second.calls == []
