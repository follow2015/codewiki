"""``init --src``：从目录与依赖**起草**分层配置（静态骨架 + AI 归并）。

动机（用户实测）：手写一份 8 层配置要 30 分钟、3 轮试错才消掉假环。而"完全不写"
交给兜底推断会碎成 120 层（每层一个点分目录名）。中间没有过渡带 —— 因为
``directory_groups`` 是**自顶向下切分**，而架构层常要**跨目录归并**
（``app/api`` + ``app/openapi`` + ``realtime_gateway`` 同属"接口层"）。

所以本模块给两个起点，人工只做最后一步（改层名、勾归并）：

* :func:`directory_draft` —— 纯静态，零凭据；
* :func:`ai_draft` —— 模型按职责归并，但输出要过**逐条校验**（path 必须命中真实
  模块、层名必须唯一、覆盖率过低要报），校验不过就退回静态骨架，不写坏配置。

核心断言是性质而不是实现：不碎、覆盖完整、能回读、模型乱写不会污染配置。
"""

from __future__ import annotations

import json

import pytest

from codewiki.ai.client import ChatResult, LLMError
from codewiki.ai.draft import ai_draft, build_draft_context
from codewiki.config import load_config
from codewiki.layering import UNLAYERED_LAYER_ID, layer_of
from codewiki.layering_draft import directory_draft, render_config_toml


@pytest.fixture(autouse=True)
def _no_tmp_leak():
    """``pytest`` 的 ``tmp_path`` 在部分宿主机上会 EEXIST；本文件不依赖它。"""
    yield


def _app_paths() -> list[str]:
    """一个"典型"仓库：三层后端 + 一个前端 + 两个根脚本。"""
    return [
        *(f"app/api/m{i}.py" for i in range(6)),
        *(f"app/services/m{i}.py" for i in range(12)),
        *(f"app/models/m{i}.py" for i in range(5)),
        *(f"frontend/src/c{i}.tsx" for i in range(10)),
        "run.py",
        "wsgi.py",
    ]


def _deps() -> list[tuple[str, str]]:
    """``api -> services -> models`` 的单向依赖（层序唯一解）。"""
    out: list[tuple[str, str]] = []
    out += [(f"app/api/m{i}.py", f"app/services/m{i % 12}.py") for i in range(6)]
    out += [(f"app/services/m{i}.py", f"app/models/m{i % 5}.py") for i in range(12)]
    out += [(f"frontend/src/c{i}.tsx", f"app/api/m{i % 6}.py") for i in range(10)]
    return out


class _StubClient:
    """最小 client 桩：只实现 ``chat`` 与两个能力开关（与 DeferredClient 同形状）。"""

    supports_json_mode = False
    supports_native_tools = False
    model = "stub"

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[list] = []

    def chat(self, messages, **kwargs):  # noqa: ANN001 - 跟真客户端同签名
        self.calls.append(messages)
        return ChatResult(content=self._reply, model=self.model)


class _FailingClient:
    model = "stub"
    supports_json_mode = False
    supports_native_tools = False

    def chat(self, messages, **kwargs):  # noqa: ANN001
        raise LLMError("connection refused")


def test_directory_draft_covers_every_directory_module():
    """起草配置的第一职责：**有职责的模块都有层**。

    组合根（仓库根的装配/入口）是唯一例外 —— 见下一条：给它们一层必然产出假环。
    """
    paths = _app_paths()
    draft = directory_draft(paths)
    pairs = [(layer.id, layer.paths) for layer in draft.layers]

    uncovered = [p for p in paths if layer_of(p, pairs) == UNLAYERED_LAYER_ID]
    assert uncovered == ["run.py", "wsgi.py"]
    assert sorted(draft.entry_modules) == ["run.py", "wsgi.py"]


def test_entry_modules_do_not_become_a_layer():
    """仓库根的装配/入口**不成层**：它跟每一层都互相依赖，必然报假 LAYER_CYCLE。

    实测 ipip（670 模块）：``root``(6 个根文件) / ``app``(2) / ``other``(2) 三个碎层
    凑出 3 条 ``app → root → app`` 形态的 error；手写配置把这 10 个归未分层就没有。
    这些模块没有共同的职责，只是"恰好放在根目录"，一层包住它们是目录清单不是分层。
    """
    draft = directory_draft(_app_paths(), deps=_deps())
    assert "root" not in {layer.name for layer in draft.layers}


def test_merged_singleton_bucket_is_not_written_as_a_layer():
    """``other`` 合并桶没有职责含义 —— 写进配置等于把"杂项"当成一层。

    它同样会跟所有层互依（桶里的模块散布在各个目录），是假环的另一个来源。
    """
    paths = [*_app_paths(), "lonely/x.py", "odd/y.py"]
    draft = directory_draft(paths)
    names = {layer.name for layer in draft.layers}
    assert not [n for n in names if n.startswith("other")], names
    assert {"lonely/x.py", "odd/y.py"} <= set(draft.entry_modules)


def test_real_other_directory_still_gets_a_layer():
    """**不能按名字过滤**：仓库里真有个 ``other/`` 目录时，它和合成桶同名但性质不同。

    合成桶由多个不相干前缀拼成；真实目录的模块全在同一个前缀下。
    """
    paths = [*(f"app/api/m{i}.py" for i in range(6)), *(f"other/m{i}.py" for i in range(3)), "lonely/x.py"]
    draft = directory_draft(paths)
    by_name = {layer.name: layer for layer in draft.layers}

    assert by_name["other"].paths == ["other"]
    assert "other-2" not in by_name
    assert "lonely/x.py" in draft.entry_modules


def test_module_caught_by_a_prefix_layer_is_not_reported_as_assembly():
    """``app`` 层（paths=["app"]）会把 ``app/config`` 一并兜住 —— 不能同时说它未归层。

    这条在 ipip 上真实出现：合成桶收了 ``app/config/__init__.py`` 与
    ``app/infra/__init__.py``，而 ``app`` 层的前缀匹配把两者都覆盖了。文案里多报
    2 个不存在的遗漏，人只能去追一个查不出来的问题。
    """
    paths = [
        "app/__init__.py",
        "app/celery_app.py",
        *(f"app/api/m{i}.py" for i in range(6)),
        "app/config/__init__.py",
        "run.py",
    ]
    draft = directory_draft(paths)

    assert "app" in {layer.name for layer in draft.layers}
    assert "app/config/__init__.py" not in draft.entry_modules
    assert draft.entry_modules == ["run.py"]


def test_tiny_repo_falls_back_to_the_bucket_instead_of_no_layers():
    """3 个模块分居 3 个目录：过滤掉零散桶就一层不剩，那时退回它。

    一层都没有的配置对 analyze 毫无价值（全归未分层，连分层视图都没有），而
    "组合根 vs 杂项"的区分在 3 个模块的仓库里本来就没有意义。平铺仓库另有出路
    （见下一条）：一个层包住全部不提供任何信息。
    """
    paths = ["app/api/h.py", "app/svc/l.py", "app/m/r.py"]
    draft = directory_draft(paths)

    assert [layer.name for layer in draft.layers] == ["other"]
    assert draft.entry_modules == []


def test_repo_of_only_entry_files_yields_no_layers():
    """全是根文件（平铺仓库）时起草不出层 —— 交给调用方退回模板。"""
    assert directory_draft(["run.py", "wsgi.py", "config.py"]).layers == []


def test_coverage_ignores_modules_that_are_intentionally_unlayered():
    """组合根不算"漏了"：一份正确的配置不该显示成 97% 覆盖。

    分母含 ``entry_modules`` 时，``_app_paths()`` 的覆盖率会变成 30/32 —— 人对着
    这个数字只会去追两个不存在的遗漏。
    """
    draft = directory_draft(_app_paths(), deps=_deps())
    assert draft.coverage == 1.0


def test_notes_explain_why_entry_modules_are_left_unlayered():
    """不归层要说明白：否则人只看到"未归层 2 个"，会以为是工具漏了。"""
    notes = directory_draft(_app_paths()).render_notes()
    assert "组合根" in notes
    assert "run.py" in notes


def test_directory_draft_does_not_fragment_into_single_module_layers():
    """碎片化是用户最直接的抱怨（满屏单文件"层"），起草阶段就不能有。"""
    draft = directory_draft(_app_paths())
    names = [layer.name for layer in draft.layers]

    assert not [n for n in names if ".part" in n or n.endswith(".misc")]
    assert all(layer.modules >= 2 for layer in draft.layers), [
        (layer.name, layer.modules) for layer in draft.layers
    ]
    assert len(names) <= 6, names


def test_directory_draft_orders_layers_by_dependency_direction():
    """``api -> services -> models``：上层是"没人依赖它"的那一端，order 最小。"""
    draft = directory_draft(_app_paths(), deps=_deps())
    order = {layer.name: layer.order for layer in draft.layers}

    assert order["app.api"] < order["app.services"] < order["app.models"]
    assert sorted(order.values()) == list(range(1, len(order) + 1))


def test_directory_draft_anchors_top_level_when_project_is_flat():
    """整个项目都在 ``src/`` 下时不能只给一层 ``src`` —— 要下钻到有信息量的一级。"""
    paths = [*(f"src/pkg/m{i}.py" for i in range(8)), *(f"src/core/m{i}.py" for i in range(6))]
    draft = directory_draft(paths)

    assert "src" not in {layer.name for layer in draft.layers}
    assert len(draft.layers) >= 2


def test_directory_draft_is_deterministic():
    first = directory_draft(_app_paths(), deps=_deps())
    second = directory_draft(_app_paths(), deps=_deps())
    assert [(layer.name, layer.paths, layer.order) for layer in first.layers] == [
        (layer.name, layer.paths, layer.order) for layer in second.layers
    ]


def test_duplicate_directory_names_are_made_unique():
    """层 id 是 ``layer::<name>``：重名即静默合并（曾把真目录和 other 混成一层）。"""
    paths = [*(f"a/other/m{i}.py" for i in range(3)), *(f"b/other/m{i}.py" for i in range(3))]
    draft = directory_draft(paths)
    names = [layer.name for layer in draft.layers]
    assert len(names) == len(set(names))


def test_rendered_config_roundtrips_through_load_config():
    """生成的 TOML 必须能被自己的加载器读回同样的层 —— 否则产物只是"看起来像配置"。"""
    draft = directory_draft(_app_paths(), deps=_deps())
    text = render_config_toml(draft, project_name="demo")

    assert "[[architecture.layers]]" in text
    assert "order = " in text

    cfg = load_config_text(text)
    assert [(layer.name, layer.paths, layer.order) for layer in cfg.architecture.layers] == [
        (layer.name, layer.paths, layer.order) for layer in draft.layers
    ]


def test_rendered_config_survives_analyze_roundtrip(tmp_path):
    """真正接上 analyze：起草的层要能建图和算层序，不报 error 级诊断。"""
    from codewiki.pipeline.analyze import AnalyzeRequest, run

    root = tmp_path / "repo"
    for rel, text in [
        ("app/api/handler.py", "from app.services import logic\n"),
        ("app/services/logic.py", "from app.models import row\n"),
        ("app/models/row.py", "X = 1\n"),
        ("app/api/health.py", "from app.services import logic\n"),
        ("app/services/other.py", "from app.models import row\n"),
        ("app/models/other.py", "Y = 2\n"),
    ]:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (root / "codewiki.toml").write_text(
        render_config_toml(directory_draft(_rel_paths(root)), project_name="demo"),
        encoding="utf-8",
    )

    out = tmp_path / "out"
    assert run(AnalyzeRequest(src=root, out=out, config=root / "codewiki.toml", no_cache=True)) == 0
    packet = json.loads((out / "data" / "codewiki.json").read_text(encoding="utf-8"))
    errors = [d for d in packet["diagnostics"] if d["level"] == "error"]
    assert errors == []
    layer_names = {layer["name"] for layer in packet["layers"]}
    assert "app.api" in layer_names and "app.models" in layer_names


def _rel_paths(root) -> list[str]:  # noqa: ANN001
    return sorted(str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*.py"))


def load_config_text(text: str):  # noqa: ANN201
    """把文本写进临时文件再交给 :func:`load_config`（复用真加载路径）。"""
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8") as handle:
        handle.write(text)
        name = handle.name
    return load_config(Path(name))


def test_mutual_dependency_between_directories_is_surfaced_as_hint():
    """``app/api`` 与 ``gateway`` 互相依赖 ⇒ 提示"可能该并成一层"。

    这正是人工那 30 分钟里真正花时间的判断（哪个目录跨目录同属一个职责），
    工具应该说出口，而不是等人自己盯着 63k 条边看。
    """
    paths = [
        *(f"app/api/m{i}.py" for i in range(5)),
        *(f"gateway/m{i}.py" for i in range(5)),
        *(f"app/models/m{i}.py" for i in range(5)),
    ]
    deps = [
        *[(f"app/api/m{i}.py", f"gateway/m{i}.py") for i in range(5)],
        *[(f"gateway/m{i}.py", f"app/api/m{i}.py") for i in range(5)],
        *[(f"app/api/m{i}.py", f"app/models/m{i}.py") for i in range(5)],
    ]
    draft = directory_draft(paths, deps=deps)

    joined = {(hint.a, hint.b) for hint in draft.hints}
    assert ("app.api", "gateway") in joined
    assert draft.render_notes()
    assert "互相" in draft.render_notes()


def test_one_way_dependency_is_not_reported_as_merge_hint():
    """单向依赖是正常分层，不该被建议归并 —— 否则提示退化成噪音。"""
    paths = [*(f"app/api/m{i}.py" for i in range(5)), *(f"app/models/m{i}.py" for i in range(5))]
    deps = [(f"app/api/m{i}.py", f"app/models/m{i}.py") for i in range(5)]
    draft = directory_draft(paths, deps=deps)
    assert draft.hints == []


def _ai_reply(layers: list[dict]) -> str:
    return json.dumps({"layers": layers}, ensure_ascii=False)


def test_ai_draft_accepts_model_layers_and_recomputes_order():
    paths = _app_paths()
    reply = _ai_reply(
        [
            {"name": "接口层", "paths": ["app/api"], "order": 9},
            {"name": "服务层", "paths": ["app/services"], "order": 9},
            {"name": "数据层", "paths": ["app/models"], "order": 9},
            {"name": "前端", "paths": ["frontend"]},
            {"name": "装配", "paths": ["run.py", "wsgi.py"]},
        ]
    )
    draft = ai_draft(paths, deps=_deps(), client=_StubClient(reply))

    assert draft.source == "ai"
    assert {layer.name for layer in draft.layers} >= {"接口层", "服务层", "数据层"}
    assert sorted(layer.order for layer in draft.layers) == list(range(1, len(draft.layers) + 1))
    order = {layer.name: layer.order for layer in draft.layers}
    assert order["接口层"] < order["服务层"] < order["数据层"]


def test_ai_layer_pointing_at_nothing_is_dropped_with_diagnostic():
    """模型编一个不存在的目录 —— 丢这一层并报，不能写进配置让 analyze 空转。"""
    reply = _ai_reply(
        [
            {"name": "接口层", "paths": ["app/api"]},
            {"name": "服务层", "paths": ["app/services"]},
            {"name": "数据层", "paths": ["app/models"]},
            {"name": "幻觉层", "paths": ["totally/made/up"]},
            {"name": "前端", "paths": ["frontend"]},
            {"name": "装配", "paths": ["run.py", "wsgi.py"]},
        ]
    )
    draft = ai_draft(_app_paths(), deps=_deps(), client=_StubClient(reply))

    assert "幻觉层" not in {layer.name for layer in draft.layers}
    assert any(d.code == "AI_LAYER_PATH_UNMATCHED" for d in draft.diagnostics)


def test_ai_duplicate_layer_names_reject_the_whole_reply_and_fall_back():
    """重名会让 ``layer::<name>`` 静默合并 —— 必须整份拒绝，退回静态骨架。"""
    reply = _ai_reply(
        [
            {"name": "服务层", "paths": ["app/api"]},
            {"name": "服务层", "paths": ["app/services"]},
        ]
    )
    draft = ai_draft(_app_paths(), deps=_deps(), client=_StubClient(reply))

    assert draft.source == "directory"
    assert any(d.code == "AI_LAYER_DUPLICATE_NAME" for d in draft.diagnostics)
    assert draft.layers, "回退必须给出可用的静态骨架，而不是空配置"


def test_ai_low_coverage_is_flagged_but_not_discarded():
    """模型只分了两层（漏了一大片）—— 保留结果但明确报覆盖率，让人知道要补。"""
    reply = _ai_reply(
        [{"name": "接口层", "paths": ["app/api"]}, {"name": "服务层", "paths": ["app/services"]}]
    )
    draft = ai_draft(_app_paths(), deps=_deps(), client=_StubClient(reply))

    assert draft.source == "ai"
    warning = [d for d in draft.diagnostics if d.code == "AI_LAYER_COVERAGE_LOW"]
    assert warning
    assert "%" in warning[0].message
    assert draft.uncovered


def test_ai_bad_json_falls_back_to_directory_draft():
    draft = ai_draft(_app_paths(), deps=_deps(), client=_StubClient("这不是 JSON"))
    assert draft.source == "directory"
    assert any(d.code == "AI_LAYER_PARSE_FAILED" for d in draft.diagnostics)


def test_ai_unavailable_falls_back_to_directory_draft():
    draft = ai_draft(_app_paths(), deps=_deps(), client=_FailingClient())
    assert draft.source == "directory"
    assert any(d.code == "AI_LAYER_UNAVAILABLE" for d in draft.diagnostics)


def test_ai_prompt_contains_directory_census_and_cross_directory_edges():
    """喂给模型的是"目录普查 + 目录间边"（可压缩），不是 63k 条原始边。"""
    system, user = build_draft_context(_app_paths(), deps=_deps())

    assert "layer" in system.lower() or "层" in system
    assert "app/services" in user
    assert "app/api" in user
    assert "app.api -> app.services" in user
    assert user.count("app/services/m") <= 3


def test_ai_reply_with_no_layers_falls_back():
    draft = ai_draft(_app_paths(), deps=_deps(), client=_StubClient(_ai_reply([])))
    assert draft.source == "directory"
    assert draft.layers


def test_parent_child_mutual_dependency_is_not_a_merge_hint():
    """``app`` 与 ``app.utils`` 互相依赖是目录结构的必然，不是"同一职责被拆开"。

    实测 ipip arch-e0：不加这条过滤，5 条提示里 4 条都是父子关系或 ``root`` 参与，
    唯一有价值的那条（服务层 ⇄ 持久层 125 条）会被埋掉。
    """
    paths = [
        *(f"app/m{i}.py" for i in range(4)),
        *(f"app/utils/m{i}.py" for i in range(4)),
        "run.py",
    ]
    deps = [
        *[(f"app/m{i}.py", f"app/utils/m{i}.py") for i in range(4)],
        *[(f"app/utils/m{i}.py", f"app/m{i}.py") for i in range(4)],
        *[(f"app/utils/m{i}.py", "run.py") for i in range(4)],
        *[("run.py", f"app/utils/m{i}.py") for i in range(4)],
    ]
    draft = directory_draft(paths, deps=deps)
    assert draft.hints == []


def test_hint_survives_between_unrelated_siblings():
    """对照组：真正的"两个独立目录互相依赖"仍要被提示出来（否则上面的过滤是过度屏蔽）。"""
    paths = [*(f"svc/m{i}.py" for i in range(4)), *(f"repo/m{i}.py" for i in range(4))]
    deps = [
        *[(f"svc/m{i}.py", f"repo/m{i}.py") for i in range(4)],
        *[(f"repo/m{i}.py", f"svc/m{i}.py") for i in range(3)],
    ]
    draft = directory_draft(paths, deps=deps)
    assert [(hint.a, hint.b) for hint in draft.hints] == [("repo", "svc")]


def test_single_module_groups_are_not_given_their_own_layer():
    """下钻出来的单模块组既不单独成层，也不该跟别的零散目录凑成一个 ``other`` 层。

    实测 ipip arch-e0：``app/infra`` 只有 1 个模块，却独占一层 —— 一层一个模块不是
    分层，是目录清单。而把它们打包成 ``other`` 是另一个极端：那个桶没有职责含义，
    跟每层互依，只会换来一条假 LAYER_CYCLE。
    """
    paths = [
        *(f"app/api/m{i}.py" for i in range(6)),
        *(f"app/svc/m{i}.py" for i in range(4)),
        "app/infra/only.py",
        *(f"web/m{i}.py" for i in range(2)),
    ]
    draft = directory_draft(paths)
    names = {layer.name for layer in draft.layers}

    assert "app.infra" not in names
    assert not [name for name in names if name.startswith("other")], names
    assert "app/infra/only.py" in draft.entry_modules
    assert all(layer.modules >= 2 for layer in draft.layers), [
        (layer.name, layer.modules) for layer in draft.layers
    ]


def test_oversized_group_is_split_only_past_half_the_repo():
    """拆分阈值是"占全仓一半以上"，不是"某个固定数"。

    这条锁的是**阈值口径**：用固定 30 会把 40 个模块的仓库拆成一堆碎片（层比模块还
    多），完全不拆又会让 695 个模块的 ``app`` 顶着"层"的名义留在图上。
    """
    big = [*(f"mono/a/m{i}.py" for i in range(6)), *(f"mono/b/m{i}.py" for i in range(6))]
    draft = directory_draft([*big, "web/x.py", "web/y.py"])
    assert {layer.name for layer in draft.layers} >= {"mono.a", "mono.b"}

    balanced = [*(f"alpha/m{i}.py" for i in range(20)), *(f"beta/m{i}.py" for i in range(20))]
    names = [layer.name for layer in directory_draft(balanced).layers]
    assert names == ["alpha", "beta"], names


def test_layer_cycles_names_the_groups_to_merge():
    """多跳成环（A→B→C→A）抓不到"两两互相依赖"，但正好是 analyze 报 LAYER_CYCLE 的形态。

    起草阶段就把该合并的层名说出来，人不必顺着 error 一条条追。
    """
    paths = [
        *(f"core/m{i}.py" for i in range(3)),
        *(f"exc/m{i}.py" for i in range(3)),
        *(f"util/m{i}.py" for i in range(3)),
    ]
    deps = [
        ("core/m0.py", "exc/m0.py"),
        ("exc/m0.py", "util/m0.py"),
        ("util/m0.py", "core/m0.py"),
    ]
    draft = directory_draft(paths, deps=deps)

    assert draft.cycles == [["core", "exc", "util"]]
    assert "考虑合并成一层" in draft.render_notes()


def test_acyclic_layers_report_no_cycle():
    draft = directory_draft(_app_paths(), deps=_deps())
    assert draft.cycles == []


def test_big_cycle_does_not_advise_merging_everything_into_one_layer():
    """大环不能建议"合并成一层"——把 11 层并成 1 层等于没有分层，比现状更糟。

    实测 ipip arch-e0 的 18 层草稿里，SCC 吞掉 11 层（`root` 的 config/extensions
    被所有层 import，又反向 import 它们）。那种环说明"粒度与真实依赖不符"，
    不是"该合并"。
    """
    paths = [*(f"m{i}/x{j}.py" for i in range(6) for j in range(2))]
    deps = [(f"m{i}/x0.py", f"m{(i + 1) % 6}/x0.py") for i in range(6)]
    draft = directory_draft(paths, deps=deps)

    assert len(draft.cycles[0]) == 6
    notes = draft.render_notes()
    assert "考虑合并成一层" not in notes
    assert "粒度与真实依赖不符" in notes


def test_overlapping_layers_report_final_counts_not_double_counting():
    """``app`` 与 ``app/api`` 重叠时，模块数必须按**最终归属**算。

    每层各自数一遍的话，两层相加会超过仓库总模块数（实测 AI 版：2009 > 1322），
    人一眼看到数字就以为配置坏了 —— 而错的是计数。
    """
    from codewiki.layering_draft import DraftResult, LayerDraft, finalize_counts

    paths = [*(f"app/api/m{i}.py" for i in range(4)), *(f"app/svc/m{i}.py" for i in range(3))]
    draft = DraftResult(
        layers=[
            LayerDraft(name="接口", paths=["app/api"], order=1),
            LayerDraft(name="装配", paths=["app"], order=2),
        ],
        source="ai",
    )
    fixed = finalize_counts(draft.layers, paths)

    assert [layer.modules for layer in fixed] == [4, 3]
    assert sum(layer.modules for layer in fixed) == len(paths)
