"""WP6：`rules --suggest`（静态）+ `merge-rules`。

三档策略出自路线图 1.2：

| 该方向现存依赖 | 生成什么 | 理由 |
|---|---|---|
| 0 条 | `forbid-dependency` + `error` | 现在没人违反，立成硬约束等于白赚一道闸 |
| 1–3 条 | `forbid-dependency` + `warning` | 可见、可讨论，但不阻断别人 |
| 更多 | 不出规则，只列为「待重构目标」 | 几十条依赖不是配置能解决的 |

**关键约束：不许用基线兜底。** 草案里每条 `error` 规则的现存违例数必须是 0 ——
否则 `check` 得先 `--write-baseline` 才过，等于草案自己给自己发了赦免状。用例直接
拿同一份求值器复核，并跑一遍真 CLI（不加 `--baseline`）。

**层对筛选（本实现对路线图的一处收紧）**：只为"至少一个方向有依赖"的层对出规则。
两个从不相往来的层之间写规则，描述的是目录树不是架构；按字面把 0 条依赖的层对
全部出成 error，15 层就是 210 条规则 —— 那正是"规则过严导致被整体无视"。
"""

from __future__ import annotations

import argparse
import io
import json
import tomllib
from contextlib import redirect_stderr, redirect_stdout

import pytest

from codewiki.config import RuleSpec, load_config
from codewiki.graph.merge_rules import merge_rules, render_rule_merge_report
from codewiki.graph.rules import evaluate_rules
from codewiki.graph.suggest_rules import render_suggested_rules, suggest_rules
from codewiki.models import CONF_STATIC, Edge, EdgeType, Node, NodeType
from codewiki.render.jsonio import packet_from_parts

def _packet(modules: dict[str, list[str]], edges: list[tuple[str, str]]):
    """``{层名: [模块路径…]}`` + ``[(src_path, dst_path)…]`` → 数据包。"""
    nodes = []
    layers = []
    for order, (layer, paths) in enumerate(modules.items(), start=1):
        layers.append({"id": f"layer::{layer}", "name": layer, "order": order})
        nodes += [
            Node(id=f"mod::{path}", type=NodeType.MODULE, path=path, layer_id=f"layer::{layer}")
            for path in paths
        ]
    edge_objs = [
        Edge(
            source=f"mod::{src}",
            target=f"mod::{dst}",
            type=EdgeType.IMPORTS,
            evidence=f"{src}:1",
            confidence=CONF_STATIC,
        )
        for src, dst in edges
    ]
    return packet_from_parts(
        title="t", layers=layers, nodes=nodes, edges=edge_objs, annotations=[], diagnostics=[]
    )


def _two_layer_packet(*, forward: int = 0, backward: int = 0):
    """接口层 ⇄ 服务层；``forward`` 条 接口→服务，``backward`` 条 服务→接口。"""
    api = [f"api/a{i}.py" for i in range(max(forward, 1))]
    svc = [f"svc/b{i}.py" for i in range(max(backward, 1))]
    edges = [(f"api/a{i}.py", "svc/b0.py") for i in range(forward)]
    edges += [("svc/b0.py", f"api/a{i}.py") for i in range(backward)]
    return _packet({"接口层": api, "服务层": svc}, edges)


def _rules_of(draft, *, severity: str):
    return [item.rule for item in draft.suggestions if item.rule.severity == severity]


def test_a_clean_direction_becomes_an_error_rule():
    """现存 0 条的那个方向 = 免费收益：立成 error，防的是将来的一步回边。"""
    draft = suggest_rules(_two_layer_packet(forward=1))

    errors = _rules_of(draft, severity="error")
    assert len(errors) == 1, [r.id for r in draft.suggestions]
    guard = errors[0]
    assert (guard.from_, guard.to) == ("layer:服务层", "layer:接口层"), "error 要出在干净的那个方向上"
    assert guard.kind == "forbid-dependency"
    assert draft.suggestions[0].violations >= 0


def test_one_to_three_dependencies_become_warnings():
    draft = suggest_rules(_two_layer_packet(forward=2))

    forward = [item for item in draft.suggestions if item.rule.from_ == "layer:接口层"]
    assert len(forward) == 1
    assert forward[0].rule.severity == "warning"
    assert forward[0].violations == 2


def test_four_or_more_dependencies_become_targets_not_rules():
    """几十条依赖不是配置能解决的 —— 出一条必红规则只会被塞进基线永久豁免。"""
    draft = suggest_rules(_two_layer_packet(forward=5))

    assert not [r for r in _rules_of(draft, severity="warning") if r.from_ == "layer:接口层"]
    targets = {(t.from_layer, t.to_layer): t.violations for t in draft.targets}
    assert targets == {("接口层", "服务层"): 5}


def test_unrelated_layer_pairs_produce_no_rules():
    """两个从不相往来的层之间不出规则：那是目录树，不是架构。"""
    packet = _packet(
        {"甲": ["jia/a.py", "jia/b.py"], "乙": ["yi/c.py", "yi/d.py"]},
        [("jia/a.py", "jia/b.py"), ("yi/c.py", "yi/d.py")],
    )

    draft = suggest_rules(packet)

    assert draft.suggestions == [], [r.id for r in draft.suggestions]
    assert draft.unrelated_pairs == 2


def test_rule_ids_are_unique_even_when_slugs_collide():
    """``a.b`` 与 ``a-b`` 归一到同一个 slug —— 规则 id 重复会让配置加载直接报错。"""
    packet = _packet(
        {"a.b": ["x/one.py"], "a-b": ["y/two.py"], "目标": ["z/three.py"]},
        [("x/one.py", "z/three.py"), ("y/two.py", "z/three.py")],
    )

    ids = [item.rule.id for item in suggest_rules(packet).suggestions]

    assert len(ids) == len(set(ids)), ids
    assert all(rid.startswith("no-") for rid in ids)


def test_layer_internal_cycles_become_warning_rules():
    """层内模块环能表达成 `forbid-cycle`（单层作用域），给 warning。"""
    packet = _packet(
        {"服务层": ["svc/a.py", "svc/b.py"], "上游": ["up/u.py"]},
        [("svc/a.py", "svc/b.py"), ("svc/b.py", "svc/a.py"), ("up/u.py", "svc/a.py")],
    )

    cycles = [item for item in suggest_rules(packet).suggestions if item.rule.kind == "forbid-cycle"]

    assert len(cycles) == 1
    assert cycles[0].rule.scope == "layer:服务层"
    assert cycles[0].rule.severity == "warning"
    assert cycles[0].violations == 1


def test_a_layer_inside_a_transitive_cycle_never_gets_an_error_rule():
    """甲→乙→丙→甲 三层层环：没有直接回边的那一对也算"成环的层对"。

    只盯"直接双向"的判定会在这里漏掉 —— 甲→丙 的直接依赖是 0，按"0 条 ⇒ error"
    就会给它一条 error 规则，而这两层明明已经绕了一圈互相依赖。环的拆解是设计
    工作，这里最多只该给 warning。
    """
    packet = _packet(
        {"甲": ["j/a.py"], "乙": ["y/b.py"], "丙": ["b/c.py"]},
        [("j/a.py", "y/b.py"), ("y/b.py", "b/c.py"), ("b/c.py", "j/a.py")],
    )

    draft = suggest_rules(packet)

    assert draft.cycles, "三层层环应当被识别出来"
    assert not _rules_of(draft, severity="error"), [r.id for r in _rules_of(draft, severity="error")]
    assert len(_rules_of(draft, severity="warning")) == 6


def test_a_layer_cycle_never_becomes_an_error_rule():
    """pydantic 的 ``root ⇄ _internal`` 这对已知层环：双向都有依赖，不能出 error。

    前提（这一对确实双向成环）在用例里**显式断言**：不成立就 skip 并说清原因，
    不许静默通过 —— 否则前提一漂移，这条用例就退化成永远绿的装饰品。
    """
    packet = _packet(
        {"root": ["pydantic/main.py", "pydantic/fields.py"], "_internal": ["pydantic/_internal/x.py"]},
        [
            ("pydantic/main.py", "pydantic/_internal/x.py"),
            ("pydantic/fields.py", "pydantic/_internal/x.py"),
            ("pydantic/_internal/x.py", "pydantic/main.py"),
        ],
    )

    forward = evaluate_rules(
        packet,
        [RuleSpec(id="probe-a", kind="forbid-dependency", from_="layer:root", to="layer:_internal")],
    )
    backward = evaluate_rules(
        packet,
        [RuleSpec(id="probe-b", kind="forbid-dependency", from_="layer:_internal", to="layer:root")],
    )
    if not (forward.violations and backward.violations):
        pytest.skip("前提不成立：root 与 _internal 在这份数据包里没有双向依赖，本用例失去意义")

    draft = suggest_rules(packet)
    errors = _rules_of(draft, severity="error")

    def layers_of(rule: RuleSpec) -> set[str]:
        return {spec.split(":", 1)[1] for spec in (rule.from_ or "", rule.to or "") if spec}

    assert not [r for r in errors if {"root", "_internal"} <= layers_of(r)], (
        f"成环的层对不该出 error 规则：{[r.id for r in errors]}"
    )
    assert draft.cycles, "这对层成环，草案里应当把它列为层间环"


def _run_check(packet_path, config_path) -> tuple[int, str]:
    from codewiki.cli.check_cmd import check

    args = argparse.Namespace(
        input=str(packet_path),
        config=str(config_path),
        format="json",
        max_per_rule=20,
        baseline=None,
        write_baseline=False,
    )
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        code = check(args)
    return code, out.getvalue()


CONFIG_HEAD = """
[architecture]
[[architecture.layers]]
name = "接口层"
paths = ["api"]

[[architecture.layers]]
name = "服务层"
paths = ["svc"]
"""

CONFIG_HEAD_THREE = (
    CONFIG_HEAD
    + """
[[architecture.layers]]
name = "数据层"
paths = ["db"]
"""
)


def _layered_packet_1_2_3():
    return _packet(
        {
            "接口层": ["api/a1.py"],
            "服务层": ["svc/b1.py", "svc/b2.py"],
            "数据层": ["db/c1.py", "db/c2.py", "db/c3.py"],
        },
        [
            ("api/a1.py", "svc/b1.py"),
            ("svc/b1.py", "db/c1.py"),
            ("svc/b2.py", "db/c2.py"),
            ("api/a1.py", "db/c1.py"),
            ("api/a1.py", "db/c2.py"),
            ("api/a1.py", "db/c3.py"),
        ],
    )


def test_every_error_rule_has_zero_existing_violations(tmp_path):
    """草案自洽的判据：error 规则的现存违例数为 0 ⇒ check 不加 --baseline 也退出 0。"""
    packet = _layered_packet_1_2_3()
    draft = suggest_rules(packet)
    errors = _rules_of(draft, severity="error")
    assert len(errors) == 3, [r.id for r in errors]
    assert {item.violations for item in draft.suggestions if item.rule.severity == "warning"} == {1, 2, 3}

    report = evaluate_rules(packet, errors)
    assert report.violations == [], [v.message for v in report.violations]

    packet_path = tmp_path / "codewiki.json"
    packet_path.write_text(packet.model_dump_json(), encoding="utf-8")
    config = tmp_path / "codewiki.toml"
    config.write_text(CONFIG_HEAD_THREE + _rules_toml(draft), encoding="utf-8")

    code, payload = _run_check(packet_path, config)

    assert code == 0, payload
    assert json.loads(payload)["summary"]["error"] == 0


def test_a_hand_added_back_edge_is_caught_by_the_draft(tmp_path):
    """草案真约束：手工加一处被 error 规则禁止的 import ⇒ 退出 1 且只报这一条。"""
    draft = suggest_rules(_two_layer_packet(forward=2))
    broken = _two_layer_packet(forward=2, backward=1)

    packet_path = tmp_path / "codewiki.json"
    packet_path.write_text(broken.model_dump_json(), encoding="utf-8")
    config = tmp_path / "codewiki.toml"
    config.write_text(CONFIG_HEAD + _rules_toml(draft), encoding="utf-8")

    code, payload = _run_check(packet_path, config)
    report = json.loads(payload)

    assert code == 1
    assert report["summary"]["error"] == 1
    errors = [item for item in report["violations"] if item["severity"] == "error"]
    assert len(errors) == 1, report["violations"]
    assert errors[0]["where"] == "svc/b0.py:1", "报出来的必须是那条新加的回边"


def _rules_toml(draft) -> str:
    """只取 `[[rules]]` 段（配置里已经手写了 layers）。"""
    text = render_suggested_rules(draft)
    index = text.find("[[rules]]")
    return "\n" + text[index:] if index >= 0 else ""


def test_draft_file_is_valid_toml_and_validates_as_config(tmp_path):
    """草案必须能被 tomllib 读回、被 `load_config` 校验（层名要真的存在）。"""
    draft = suggest_rules(_two_layer_packet(forward=2, backward=1))
    config = tmp_path / "codewiki.toml"
    config.write_text(CONFIG_HEAD + _rules_toml(draft), encoding="utf-8")

    parsed = tomllib.loads(render_suggested_rules(draft))
    assert len(parsed["rules"]) == len(draft.suggestions)

    cfg = load_config(config)
    assert len(cfg.rules) == len(draft.suggestions)


def test_merge_rules_appends_only_the_new_rules(tmp_path):
    draft = suggest_rules(_two_layer_packet(forward=2))
    suggestion = tmp_path / "rules.suggest.toml"
    suggestion.write_text(render_suggested_rules(draft), encoding="utf-8")
    config = tmp_path / "codewiki.toml"
    config.write_text(CONFIG_HEAD, encoding="utf-8")

    outcome = merge_rules(config, suggestion)

    assert len(outcome.added) == len(draft.suggestions)
    assert outcome.conflicts == []
    cfg = load_config(config)
    assert {r.id for r in cfg.rules} == {item.rule.id for item in draft.suggestions}

    again = merge_rules(config, suggestion)
    assert again.added == []
    assert len(again.skipped) == len(draft.suggestions)
    assert load_config(config).rules == cfg.rules


def test_merge_rules_reports_a_conflict_and_writes_nothing(tmp_path):
    """同名不同内容 ⇒ 退出 1（需要人看一眼），且**一个字节都不写**。"""
    draft = suggest_rules(_two_layer_packet(forward=2))
    suggestion = tmp_path / "rules.suggest.toml"
    suggestion.write_text(render_suggested_rules(draft), encoding="utf-8")

    first = draft.suggestions[0].rule
    hand_written = (
        CONFIG_HEAD
        + f"""
[[rules]]
id = "{first.id}"
kind = "forbid-dependency"
from = "{first.from_}"
to = "{first.to}"
reason = "手写的理由"
"""
    )
    config = tmp_path / "codewiki.toml"
    config.write_text(hand_written, encoding="utf-8")
    before = config.read_text(encoding="utf-8")

    outcome = merge_rules(config, suggestion)

    assert config.read_text(encoding="utf-8") == before
    assert outcome.changed is False
    assert [item["id"] for item in outcome.conflicts] == [first.id]
    assert "需要人工裁决" in render_rule_merge_report(outcome)


def test_merge_rules_dry_run_never_touches_the_file(tmp_path):
    draft = suggest_rules(_two_layer_packet(forward=2))
    suggestion = tmp_path / "rules.suggest.toml"
    suggestion.write_text(render_suggested_rules(draft), encoding="utf-8")
    config = tmp_path / "codewiki.toml"
    config.write_text(CONFIG_HEAD, encoding="utf-8")
    before = config.read_text(encoding="utf-8")

    outcome = merge_rules(config, suggestion, dry_run=True)

    assert outcome.added
    assert config.read_text(encoding="utf-8") == before


def test_cli_rules_suggest_prints_a_draft_to_stdout(tmp_path):
    from codewiki.cli import main

    packet = _two_layer_packet(forward=2)
    packet_path = tmp_path / "codewiki.json"
    packet_path.write_text(packet.model_dump_json(), encoding="utf-8")

    out = io.StringIO()
    with redirect_stdout(out):
        code = main(["rules", "--suggest", "--input", str(packet_path)])

    assert code == 0
    text = out.getvalue()
    assert "[[rules]]" in text
    assert 'severity = "error"' in text
    assert not list(tmp_path.glob("**/*.toml")), "不传 --out 时不该写文件"


def test_cli_rules_suggest_writes_the_file_when_asked(tmp_path):
    from codewiki.cli import main

    packet = _two_layer_packet(forward=2)
    packet_path = tmp_path / "codewiki.json"
    packet_path.write_text(packet.model_dump_json(), encoding="utf-8")
    target = tmp_path / "suggestions" / "rules.suggest.toml"

    out = io.StringIO()
    with redirect_stdout(out):
        code = main(["rules", "--suggest", "--input", str(packet_path), "--out", str(target)])

    assert code == 0
    assert target.exists()
    assert "merge-rules" in out.getvalue()
    assert len(tomllib.loads(target.read_text(encoding="utf-8"))["rules"]) == 2


def test_cli_rules_without_suggest_is_a_usage_error(tmp_path):
    from codewiki.cli import main

    err = io.StringIO()
    with redirect_stderr(err):
        code = main(["rules"])

    assert code == 2
    assert "--suggest" in err.getvalue()
