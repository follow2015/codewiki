"""目录归组、无配置兜底分层、带层序的分层建议。

动机（外部评审）：没配分层时，85 个模块全进 ``_unlayered`` 并产生 85 条逐模块警告；
``suggest-layers`` 对无向图跑社区发现，得到多个同名 ``root`` 层、一串单模块"层"
和一屏幕文件路径 —— 对"分层不清晰"的项目恰恰最没用。
"""

from __future__ import annotations

import json
import tomllib

from codewiki.config import derive_layer_specs
from codewiki.graph.merge_layers import render_layer_block
from codewiki.graph.suggest_layers import render_suggested_layers, suggest_layers
from codewiki.layering import directory_groups
from codewiki.models import CONF_STATIC, Edge, EdgeType, Node, NodeType
from codewiki.pipeline.analyze import AnalyzeRequest, run
from codewiki.render.jsonio import packet_from_parts


def _paths(prefix: str, n: int) -> list[str]:
    return [f"{prefix}/m{i}.py" for i in range(n)]


# ---------- directory_groups ----------


def test_groups_follow_top_level_directories():
    groups = {g.name: g for g in directory_groups(_paths("ai", 4) + _paths("cli", 3) + _paths("graph", 2))}
    assert set(groups) == {"ai", "cli", "graph"}
    assert groups["ai"].prefixes == ["ai"] and len(groups["ai"].modules) == 4


def test_dominant_directory_is_split_one_level_down():
    """整个项目都在 src/ 下时，"层"不能只是 src —— 要下钻一级。"""
    paths = _paths("src/pkg", 6) + _paths("src/core", 5) + ["tests/t1.py", "tests/t2.py"]
    names = {g.name for g in directory_groups(paths)}
    assert {"src.pkg", "src.core", "tests"} <= names
    assert "src" not in names


def test_tiny_groups_merge_into_other():
    groups = {g.name: g for g in directory_groups(_paths("ai", 4) + ["a/x.py", "b/y.py"])}
    assert set(groups) == {"ai", "other"}
    assert sorted(groups["other"].prefixes) == ["a", "b"]


def test_single_tiny_group_is_kept_not_renamed():
    names = {g.name for g in directory_groups(_paths("ai", 4) + ["lonely/x.py"])}
    assert names == {"ai", "lonely"}


def test_root_files_form_a_root_group():
    groups = {g.name: g for g in directory_groups(["a.py", "b.py", *_paths("ai", 3)])}
    assert groups["root"].prefixes == [] and len(groups["root"].modules) == 2


def test_groups_are_deterministic_and_empty_input_is_empty():
    paths = _paths("ai", 4) + _paths("cli", 4)
    assert [g.name for g in directory_groups(paths)] == [g.name for g in directory_groups(reversed(paths))]
    assert directory_groups([]) == []


def test_flat_repo_derives_no_layers():
    """全部平铺在一个目录：一个"层"包住所有模块没有信息量 —— 退回 _unlayered。"""
    assert derive_layer_specs(_paths("only", 6)) == []


def test_derived_specs_use_dot_for_root_and_unique_ids():
    specs = derive_layer_specs(["a.py", *_paths("ai", 3), *_paths("cli", 3)])
    by_name = {s.name: s for s in specs}
    assert by_name["root"].paths == ["."]
    assert len({s.id for s in specs}) == len(specs)


# ---------- 管线：无配置兜底 ----------


def _write_repo(root):
    files = {
        "app/api/h1.py": "from app.core import c1\n",
        "app/api/h2.py": "from app.core import c1\n",
        "app/core/c1.py": "def f():\n    return 1\n",
        "app/core/c2.py": "def g():\n    return 2\n",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _analyze(src, out):
    assert run(AnalyzeRequest(src=src, out=out, no_cache=True, timestamp="2026-01-01T00:00:00Z")) == 0
    return json.loads((out / "data" / "codewiki.json").read_text(encoding="utf-8"))


def test_without_config_layers_are_derived_from_directories(tmp_path):
    src = tmp_path / "repo"
    _write_repo(src)
    packet = _analyze(src, tmp_path / "out")

    names = {layer["name"] for layer in packet["layers"]}
    assert {"app.api", "app.core"} <= names
    codes = [d["code"] for d in packet["diagnostics"]]
    assert "LAYERS_DERIVED" in codes and "UNLAYERED_MODULE" not in codes
    modules = [n for n in packet["nodes"] if n["type"] == "Module"]
    assert all(n["layer_id"] != "layer::_unlayered" for n in modules)


def test_auto_layers_can_be_disabled(tmp_path):
    src = tmp_path / "repo"
    _write_repo(src)
    (src / "codewiki.toml").write_text("[architecture]\nauto_layers = false\n", encoding="utf-8")
    packet = _analyze(src, tmp_path / "out")

    assert packet["layers"] == []
    assert "LAYERS_DERIVED" not in [d["code"] for d in packet["diagnostics"]]


def test_declared_layers_are_never_overridden(tmp_path):
    src = tmp_path / "repo"
    _write_repo(src)
    (src / "codewiki.toml").write_text(
        '[[architecture.layers]]\nname = "手写"\npaths = ["app"]\n', encoding="utf-8"
    )
    packet = _analyze(src, tmp_path / "out")

    assert [layer["name"] for layer in packet["layers"]] == ["手写"]
    assert "LAYERS_DERIVED" not in [d["code"] for d in packet["diagnostics"]]


# ---------- suggest-layers：层序与环 ----------


def _packet(edges: list[tuple[str, str]], paths: list[str]):
    nodes = [Node(id=f"mod::{p}", type=NodeType.MODULE, path=p, layer_id="layer::x") for p in paths]
    return packet_from_parts(
        title="demo",
        layers=[],
        nodes=nodes,
        edges=[
            Edge(source=f"mod::{a}", target=f"mod::{b}", type=EdgeType.IMPORTS, confidence=CONF_STATIC)
            for a, b in edges
        ],
        annotations=[],
        diagnostics=[],
    )


CHAIN_PATHS = [*_paths("cli", 2), *_paths("svc", 2), *_paths("core", 2)]
CHAIN_EDGES = [
    ("cli/m0.py", "svc/m0.py"),
    ("cli/m1.py", "svc/m1.py"),
    ("svc/m0.py", "core/m0.py"),
    ("svc/m1.py", "core/m1.py"),
    ("cli/m0.py", "cli/m1.py"),
]


def test_layer_order_follows_dependency_direction():
    layers = {layer.name: layer for layer in suggest_layers(_packet(CHAIN_EDGES, CHAIN_PATHS))}
    assert layers["cli"].order == 1 < layers["svc"].order == 2 < layers["core"].order == 3
    assert not any(layer.cyclic for layer in layers.values())


def test_output_is_sorted_top_down():
    ordered = [layer.name for layer in suggest_layers(_packet(CHAIN_EDGES, CHAIN_PATHS))]
    assert ordered == ["cli", "svc", "core"]


def test_circular_layers_share_an_order_and_are_flagged():
    edges = [*CHAIN_EDGES, ("core/m0.py", "svc/m0.py")]  # core 反向依赖 svc → 环
    layers = {layer.name: layer for layer in suggest_layers(_packet(edges, CHAIN_PATHS))}
    assert layers["svc"].order == layers["core"].order
    assert layers["svc"].cyclic and layers["core"].cyclic and not layers["cli"].cyclic
    assert "循环依赖" in layers["svc"].reason()


def test_names_are_unique_and_tiny_dirs_are_merged():
    paths = [*CHAIN_PATHS, "a/x.py", "b/y.py"]
    layers = suggest_layers(_packet(CHAIN_EDGES, paths))
    names = [layer.name for layer in layers]
    assert len(names) == len(set(names))
    assert "other" in names and "a" not in names


def test_huge_directory_is_split_by_communities():
    """一个目录超过 max_group 时才用社区发现拆；两簇之间只有一条桥。"""
    paths = [f"big/a{i}.py" for i in range(5)] + [f"big/b{i}.py" for i in range(5)] + _paths("small", 3)
    edges = [(f"big/a{i}.py", f"big/a{j}.py") for i in range(5) for j in range(5) if i < j]
    edges += [(f"big/b{i}.py", f"big/b{j}.py") for i in range(5) for j in range(5) if i < j]
    edges += [("big/a0.py", "big/b0.py")]
    layers = suggest_layers(_packet(edges, paths), max_group=6)
    big_parts = [layer for layer in layers if layer.name.startswith("big.")]
    assert len(big_parts) == 2
    assert all(len(layer.modules) == 5 for layer in big_parts)
    assert all(p.endswith(".py") for layer in big_parts for p in layer.paths), "拆分出来的组只能列文件"


def test_suggestion_is_deterministic_with_order():
    first = [(layer.name, layer.order) for layer in suggest_layers(_packet(CHAIN_EDGES, CHAIN_PATHS))]
    second = [(layer.name, layer.order) for layer in suggest_layers(_packet(CHAIN_EDGES, CHAIN_PATHS))]
    assert first == second


def test_suggested_file_carries_order_and_roundtrips():
    text = render_suggested_layers(suggest_layers(_packet(CHAIN_EDGES, CHAIN_PATHS)), title="demo")
    layers = tomllib.loads(text)["architecture"]["layers"]
    assert [layer["order"] for layer in layers] == [1, 2, 3]


def test_merge_block_keeps_order():
    from codewiki.config import LayerSpec

    block = render_layer_block(LayerSpec(name="svc", paths=["svc"], order=2))
    assert "order = 2" in block


# ---------- 层名唯一（层 id 就是 layer::<name>）----------
#
# 目录名与合成组名撞车是**真实可复现**的：仓库里恰好有个 `other/` 或 `root/`
# 目录时，`directory_groups` 会给出两个同名组 ⇒ 两个 LayerSpec 拿到同一个
# `layer::other`。后果不是报错而是静默合并：两个组的模块归进同一层，渲染时同 id
# 的 subgraph 出现两次。`_unlayered` 更糟：那是虚拟层的保留 id。


def test_duplicate_group_names_are_made_unique():
    groups = directory_groups(["other/o1.py", "other/o2.py", "other/o3.py", "a/x.py", "b/y.py"])
    names = [group.name for group in groups]
    assert len(names) == len(set(names)), names
    # 大的那个（真实 other/ 目录）保住原名，合成组让位
    assert min(names) == "other" or "other" in names


def test_reserved_unlayered_name_is_avoided():
    """目录叫 `_unlayered` 时必须避让 —— 否则与虚拟层 id 撞车。"""
    names = {group.name for group in directory_groups(_paths("_unlayered", 3) + _paths("app", 3))}
    assert "_unlayered" not in names
    assert "_unlayered-2" in names


def test_renamed_group_does_not_collide_with_a_real_directory_name():
    """避让后的名字不能正好是另一个真实目录的名字。"""
    paths = ["other/o1.py", "other/o2.py", "a/x.py", "b/y.py", "other-2/z1.py", "other-2/z2.py"]
    names = [group.name for group in directory_groups(paths)]
    assert len(names) == len(set(names)), names
    assert "other" in names and "other-2" in names


def test_derived_layer_ids_are_unique_for_tricky_directories():
    for paths in (
        ["other/o1.py", "other/o2.py", "other/o3.py", "a/x.py", "b/y.py"],
        ["root/r1.py", "root/r2.py", "app/a1.py", "app/a2.py", "main.py"],
        ["_unlayered/u1.py", "_unlayered/u2.py", "app/a1.py", "app/a2.py"],
    ):
        specs = derive_layer_specs(paths)
        ids = [spec.id for spec in specs]
        assert len(ids) == len(set(ids)), (paths, ids)
        assert "layer::_unlayered" not in ids, (paths, ids)


def _write_repo_with_other_dir(root):
    files = {
        "other/o1.py": "from app.core import c1\n",
        "other/o2.py": "def h():\n    return 1\n",
        "other/o3.py": "def i():\n    return 2\n",
        "app/core/c1.py": "def f():\n    return 1\n",
        "app/core/c2.py": "def g():\n    return 2\n",
        "a/x.py": "def x():\n    return 0\n",
        "b/y.py": "def y():\n    return 0\n",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_analyze_keeps_real_other_dir_apart_from_the_merged_bucket(tmp_path):
    """端到端：`a/x.py` 不能被并进真实 `other/` 那一层（层 id 撞车时的静默合并）。"""
    src = tmp_path / "repo"
    _write_repo_with_other_dir(src)
    packet = _analyze(src, tmp_path / "out")

    ids = [layer["id"] for layer in packet["layers"]]
    assert len(ids) == len(set(ids)), ids

    by_path = {n["path"]: n["layer_id"] for n in packet["nodes"] if n["type"] == "Module"}
    assert by_path["a/x.py"] != by_path["other/o1.py"]
    assert by_path["b/y.py"] != by_path["other/o1.py"]


# ---------- 粒度一致 + 派生层带层序 ----------
#
# 同一种判断（"这个组还算一层吗""哪层在上"）不该因为入口不同给出不同答案：
# 兜底分层和 suggest-layers 共用 DEFAULT_MAX_GROUP 与同一份 rank_layers。


def test_oversized_group_descends_further():
    """超过 max_group 的组继续按子目录下钻，直到不超限或下钻不动。"""
    paths = _paths("svc/api/v1", 6) + _paths("svc/api/v2", 6) + _paths("cli", 4) + _paths("lib", 4)
    groups = {group.name: group for group in directory_groups(paths, max_group=5)}
    assert "svc" not in groups and "svc.api" not in groups, "大组必须继续下钻"
    assert {"svc.api.v1", "svc.api.v2"} <= set(groups)
    assert all(len(group.modules) <= 6 for group in groups.values())


def test_flat_oversized_directory_is_left_alone():
    """平铺目录下钻不动 —— 认了（社区发现那条路在 suggest-layers 里）。"""
    paths = _paths("big", 40) + _paths("cli", 4)
    groups = {group.name: group for group in directory_groups(paths, max_group=5)}
    assert "big" in groups and len(groups["big"].modules) == 40


def test_descend_terminates_on_deep_paths():
    """深层路径 + 小 max_group 也不能死循环。"""
    groups = directory_groups(_paths("a/b/c/d/e", 9), max_group=2)
    assert sum(len(group.modules) for group in groups) == 9


def test_derive_and_suggest_share_the_same_granularity_default():
    import inspect

    from codewiki.graph.suggest_layers import DEFAULT_MAX_GROUP as SUGGEST_DEFAULT

    default = inspect.signature(derive_layer_specs).parameters["max_group"].default
    assert default == SUGGEST_DEFAULT, "两条路径的拆分门槛必须一致"


def _write_repo_with_bigger_core(root):
    """`app/api`（2 个）依赖 `app/core`（4 个）—— 让"按大小排"与"按依赖排"分歧。

    故意让下游比上游多：不用依赖排序时，按模块数降序会把 core 排到前面，用例才
    真的判得出 layer order 有没有生效（同大小 + 字母序恰好一致就是假绿）。
    """
    files = {
        "app/api/h1.py": "from app.core import c1\n",
        "app/api/h2.py": "from app.core import c2\n",
        "app/core/c1.py": "def f():\n    return 1\n",
        "app/core/c2.py": "def g():\n    return 2\n",
        "app/core/c3.py": "def h():\n    return 3\n",
        "app/core/c4.py": "def i():\n    return 4\n",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_derived_layers_carry_dependency_order(tmp_path):
    """`app/api` 依赖 `app/core` ⇒ api 在上，哪怕 core 模块更多。"""
    src = tmp_path / "repo"
    _write_repo_with_bigger_core(src)
    packet = _analyze(src, tmp_path / "out")

    names = [layer["name"] for layer in packet["layers"]]
    assert names == ["app.api", "app.core"], names


def test_derived_order_survives_render(tmp_path):
    """层序要真的进图：system-context 里 api 的 subgraph 应排在 core 之前。"""
    src = tmp_path / "repo"
    _write_repo_with_bigger_core(src)
    _analyze(src, tmp_path / "out")
    mermaid = (tmp_path / "out" / "diagrams" / "system-context.mmd").read_text(encoding="utf-8")
    assert mermaid.index('"app.api"') < mermaid.index('"app.core"')


def test_direct_files_under_dominant_dir_keep_their_own_group():
    """`src/` 下直接躺的文件留在 `src` 组 —— 下钻不能把散文件弄丢。

    此前只有"src 下没有直接文件"的场景被测过；有 `src/glue.py` 时，按第二段
    目录拆分会给 `src` 自己留一组（`rest` 里没有 "/" ⇒ child == prefix）。
    这是有意的：散文件不属于任何子目录，硬塞进某个子包会把归属说错。
    """
    paths = _paths("src/pkg", 6) + _paths("src/core", 5) + ["src/glue.py", "tests/t1.py", "tests/t2.py"]
    groups = {group.name: group for group in directory_groups(paths)}
    assert groups["src"].modules == ["src/glue.py"]
    assert sum(len(group.modules) for group in groups.values()) == len(paths), "不能丢模块"


def test_cli_exposes_max_group():
    """门槛要能在命令行上调，否则它只是测试里的旋钮。"""
    from codewiki.cli.parser import build_parser

    args = build_parser().parse_args(["suggest-layers", "--max-group", "7"])
    assert args.max_group == 7
