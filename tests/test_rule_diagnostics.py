"""WP5：违例进产物（``packet.diagnostics``）+ 首页「规则违例」段。

三件事各有各的失败方式，所以分开钉：

1. **映射**：``Violation`` → ``Diagnostic``。``rule_id`` 不是装饰 —— 站点按它分组，
   丢了就只剩一堆互不相识的 message。
2. **analyze 落盘**：违例必须真的进 ``data/codewiki.json``（在 ``write_packet``
   之前追加）。只打印到终端、不进产物的实现，跑一遍 CLI 也看不出差别。
3. **站点**：按规则分组、命中数、前若干条、``文件:行`` 链接。两句"看起来都对"的
   话必须分开：**"检查过、全过"** 与 **"根本没检查"**，以及 **"规则没生效"** ——
   后者写"全部通过"就是在替一个恒不命中的规则背书。
"""

from __future__ import annotations

import json

from codewiki.cli import main
from codewiki.config import RuleSpec
from codewiki.graph.rules import evaluate_rules, to_diagnostics
from codewiki.models import CONF_STATIC, Edge, EdgeType, Node, NodeType
from codewiki.render.i18n import t
from codewiki.render.jsonio import packet_from_parts
from codewiki.render.site import RULES_PREVIEW_LIMIT, render_index_html

def _packet(*, violations: int = 1):
    """api 层里有 N 个模块各 import 一次 models/user.py（每处行号不同）。

    ``violations=0`` 时仍留一对 **api 内部** 的 import：规则的两端都得至少命中
    一个模块才会被求值（两端各挂一条 `problems` 的规则等于没跑），所以"零违例"
    必须靠一条合规的边，而不是靠"什么都没有"。
    """
    nodes = [
        Node(id="mod::models/user.py", type=NodeType.MODULE, path="models/user.py", layer_id="layer::models"),
        Node(id="mod::api/base.py", type=NodeType.MODULE, path="api/base.py", layer_id="layer::api"),
        Node(id="mod::api/util.py", type=NodeType.MODULE, path="api/util.py", layer_id="layer::api"),
    ]
    edges = [
        Edge(
            source="mod::api/base.py",
            target="mod::api/util.py",
            type=EdgeType.IMPORTS,
            evidence="api/base.py:1",
            confidence=CONF_STATIC,
        )
    ]
    for i in range(violations):
        path = f"api/h{i}.py"
        nodes.append(Node(id=f"mod::{path}", type=NodeType.MODULE, path=path, layer_id="layer::api"))
        edges.append(
            Edge(
                source=f"mod::{path}",
                target="mod::models/user.py",
                type=EdgeType.IMPORTS,
                evidence=f"{path}:{i + 3}",
                confidence=CONF_STATIC,
            )
        )
    return packet_from_parts(
        title="t",
        layers=[
            {"id": "layer::api", "name": "api", "order": 1},
            {"id": "layer::models", "name": "models", "order": 2},
        ],
        nodes=nodes,
        edges=edges,
        annotations=[],
        diagnostics=[],
    )


def _rule(**overrides):
    spec = {
        "id": "no-api-models",
        "kind": "forbid-dependency",
        "from_": "layer:api",
        "to": "layer:models",
        "severity": "error",
    }
    spec.update(overrides)
    return RuleSpec(**spec)


def _with_rules(*, violations: int = 1, rules=None):
    """按 analyze 的真实姿势把违例挂进 packet.diagnostics。"""
    packet = _packet(violations=violations)
    rules = rules if rules is not None else [_rule()]
    report = evaluate_rules(packet, rules)
    packet.diagnostics.extend(to_diagnostics(report, declared=len(rules)))
    return packet


def test_violation_becomes_a_diagnostic_carrying_rule_id():
    report = evaluate_rules(_packet(), [_rule()])
    found = [d for d in to_diagnostics(report, declared=1) if d.code == "RULE_VIOLATION"]

    assert len(found) == 1
    item = found[0]
    assert (item.level, item.rule_id) == ("error", "no-api-models")
    assert item.where == "api/h0.py:3"
    assert "违反规则 no-api-models" in item.message


def test_evaluated_marker_records_how_many_rules_ran():
    """零违例时站点要能说"检查过、全过" —— 判据是这条 marker，不是"有没有违例"。"""
    report = evaluate_rules(_packet(violations=0), [_rule()])
    markers = [d for d in to_diagnostics(report, declared=1) if d.code == "RULES_EVALUATED"]

    assert len(markers) == 1
    assert markers[0].level == "info"
    assert "按 1 条规则" in markers[0].message
    assert "error 0" in markers[0].message


def test_rule_that_matched_nothing_becomes_a_warning_not_a_silent_zero():
    """规则指向不存在的层 ⇒ 求值结束、零违例，但那是"没跑"不是"通过"。"""
    report = evaluate_rules(_packet(violations=0), [_rule(from_="layer:nope")])
    diags = to_diagnostics(report, declared=1)

    assert [d.code for d in diags] == ["RULES_EVALUATED", "RULE_NOT_EFFECTIVE"]
    assert diags[1].level == "warning"
    assert "nope" in diags[1].message


CONFIG = """
[architecture]
[[architecture.layers]]
name = "api"
paths = ["app/api"]

[[architecture.layers]]
name = "models"
paths = ["app/models"]

[[rules]]
id = "no-api-models"
kind = "forbid-dependency"
from = "layer:api"
to = "layer:models"
severity = "error"
reason = "改走 service 层"
"""


def _repo(tmp_path, *, config: str | None = CONFIG):
    root = tmp_path / "repo"
    (root / "app" / "api").mkdir(parents=True)
    (root / "app" / "models").mkdir(parents=True)
    (root / "app" / "api" / "user.py").write_text("from app.models.user import User\n", encoding="utf-8")
    (root / "app" / "models" / "user.py").write_text("class User:\n    pass\n", encoding="utf-8")
    if config is not None:
        (root / "codewiki.toml").write_text(config, encoding="utf-8")
    return root


def _analyze(tmp_path, *extra) -> tuple[int, dict, str]:
    root = _repo(tmp_path)
    out = tmp_path / "out"
    code = main(["analyze", "--src", str(root), "--out", str(out), "--no-cache", *extra])
    packet = json.loads((out / "data" / "codewiki.json").read_text(encoding="utf-8"))
    return code, packet, (out / "index.html").read_text(encoding="utf-8")


def test_analyze_writes_rule_violations_into_the_packet(tmp_path):
    code, packet, html = _analyze(tmp_path)

    assert code == 0
    found = [d for d in packet["diagnostics"] if d["code"] == "RULE_VIOLATION"]
    assert len(found) == 1, [d["code"] for d in packet["diagnostics"]]
    item = found[0]
    assert item["rule_id"] == "no-api-models"
    assert item["level"] == "error"
    assert item["where"] == "app/api/user.py:1"
    assert item["message"].endswith("改走 service 层"), "reason 要跟着 message 一起进产物"
    assert any(d["code"] == "RULES_EVALUATED" for d in packet["diagnostics"])
    assert 'data-rule="no-api-models"' in html


def test_strict_fails_on_an_error_level_rule_violation(tmp_path):
    """``--strict`` 的既有语义是"有 error 级诊断就退出 1"，规则违例走同一条路。"""
    root = _repo(tmp_path)
    out = tmp_path / "out"

    assert main(["analyze", "--src", str(root), "--out", str(out), "--no-cache", "--strict"]) == 1


def test_analyze_without_rules_emits_no_rule_diagnostics(tmp_path):
    """没声明规则 ⇒ 一条规则诊断都不该有；站点也不该凭空多出这一段。"""
    root = _repo(tmp_path, config=None)
    out = tmp_path / "out"

    assert main(["analyze", "--src", str(root), "--out", str(out), "--no-cache"]) == 0
    packet = json.loads((out / "data" / "codewiki.json").read_text(encoding="utf-8"))
    codes = {d["code"] for d in packet["diagnostics"]}
    assert not codes & {"RULE_VIOLATION", "RULES_EVALUATED", "RULE_NOT_EFFECTIVE"}, codes
    assert t("site.rules") not in (out / "index.html").read_text(encoding="utf-8")


def _rule_block(html: str, rule_id: str) -> str:
    """切出某条规则的块（到该块结束的 ``</div>`` 为止）。"""
    tail = html.split(f'data-rule="{rule_id}"', 1)[1]
    return tail.split("</div>", 1)[0]


def test_site_groups_violations_under_their_rule_with_hit_counts():
    html = render_index_html(_with_rules(violations=2))

    assert t("site.rules") in html
    head = _rule_block(html, "no-api-models")
    assert 'data-level="error"' in html
    assert 'data-hits="2"' in html
    assert "<code>no-api-models</code>" in head
    assert 'href="modules/api_h0.html"' in head
    assert "<code>api/h0.py:3</code>" in head


def test_site_lists_only_the_head_of_a_long_rule_but_says_how_many_remain():
    """截断不能变成谎报 —— 少列几条就必须写清还有几条。"""
    html = render_index_html(_with_rules(violations=RULES_PREVIEW_LIMIT + 3))
    block = _rule_block(html, "no-api-models")

    assert block.count("<li>") == RULES_PREVIEW_LIMIT
    assert t("site.rule_more", count=3) in block


def test_site_says_rules_ran_and_passed_instead_of_staying_silent():
    """「没有这一节」会被读成「没检查过」—— 检查过且干净要明说。"""
    html = render_index_html(_with_rules(violations=0))

    assert t("site.rules") in html
    assert t("site.rules_ok") in html
    assert "data-rule=" not in html


def test_site_does_not_claim_success_when_a_rule_did_not_take_effect():
    """规则恒不命中时"全部通过"是假的 —— 那句话必须换成"结论不完整"。"""
    packet = _packet(violations=0)
    report = evaluate_rules(packet, [_rule(from_="layer:nope")])
    packet.diagnostics.extend(to_diagnostics(report, declared=1))

    html = render_index_html(packet)

    assert t("site.rules_ok") not in html
    assert t("site.rules_incomplete") in html
    assert "&#x27;nope&#x27;" in html


def test_site_omits_the_linked_location_when_the_module_page_cannot_exist():
    """``where`` 指向数据包里没有的模块（旧产物 / 手工改过的包）时只显示文本。

    链接拼出来必然是死链，而"点开 404"比"没有链接"更让人怀疑产物坏了。
    """
    packet = _with_rules(violations=0)
    from codewiki.schemas import Diagnostic

    packet.diagnostics.append(
        Diagnostic(
            level="error",
            code="RULE_VIOLATION",
            message="x 依赖了 y（IMPORTS 边），违反规则 no-api-models",
            where="gone/away.py:7",
            rule_id="no-api-models",
        )
    )
    block = _rule_block(render_index_html(packet), "no-api-models")

    assert "<code>gone/away.py:7</code>" in block
    assert 'href="modules/' not in block
