"""规则求值：数据包 → 违例（阶段一 1.1 的核心）。

一组必须钉住的语义（写错了规则会"看着在跑、其实恒不命中"）：

1. **层归属只认 ``Node.layer_id``**：builder 已经算过一遍，这里再用 ``paths``
   前缀匹配一遍就是两套口径 —— 两套口径必然漂移，漂移后没人知道该信哪个。
2. **IMPORTS 与 CALLS 的端点不是一类东西**：前者连模块节点，后者连**符号**节点。
   不先经 ``Node.module_id`` 升到模块，CALLS 会全部落进"未分层"，于是规则
   安静地永不命中 —— 这种"恒假"比报错更难发现。
3. **默认只看 IMPORTS**：Python 里调用必然先 import，两条都算 = 一处违规报两次。
4. **``type_only`` 计入但降级**：它是真实耦合（规则算耦合度），但不成运行时环
   （环检测算可达性）—— 两处口径故意不同。
5. **不许编行号**：``evidence`` 为空就降级成模块路径并在文案里说明"仅模块级"。
"""

from __future__ import annotations

from codewiki.config import RuleSpec
from codewiki.graph.rules import evaluate_rules
from codewiki.models import CONF_STATIC, Edge, EdgeType, Node, NodeType
from codewiki.render.jsonio import packet_from_parts


def _mod(path: str, layer: str | None) -> Node:
    return Node(id=f"mod::{path}", type=NodeType.MODULE, path=path, layer_id=layer)


def _sym(path: str, qualname: str) -> Node:
    return Node(
        id=f"sym::{path}::{qualname}",
        type=NodeType.SYMBOL,
        name=qualname,
        module_id=f"mod::{path}",
    )


def _imp(src: str, dst: str, *, evidence: str | None = None, type_only: bool = False) -> Edge:
    return Edge(
        source=f"mod::{src}",
        target=f"mod::{dst}",
        type=EdgeType.IMPORTS,
        evidence=evidence,
        type_only=type_only,
        confidence=CONF_STATIC,
    )


def _calls(src: tuple[str, str], dst: tuple[str, str], *, evidence: str | None = None) -> Edge:
    return Edge(
        source=f"sym::{src[0]}::{src[1]}",
        target=f"sym::{dst[0]}::{dst[1]}",
        type=EdgeType.CALLS,
        evidence=evidence,
        confidence=CONF_STATIC,
    )


def _packet(nodes: list[Node], edges: list[Edge]):
    return packet_from_parts(title="t", layers=[], nodes=nodes, edges=edges, annotations=[], diagnostics=[])


def _rule(**kwargs) -> RuleSpec:
    kwargs.setdefault("kind", "forbid-dependency")
    return RuleSpec(**kwargs)


NODES = [_mod("ui/a.py", "layer::ui"), _mod("infra/db.py", "layer::infra")]


def test_dependency_violation_carries_file_and_line():
    packet = _packet(NODES, [_imp("ui/a.py", "infra/db.py", evidence="ui/a.py:3")])
    rule = _rule(id="no-ui-infra", from_="layer:ui", to="layer:infra", reason="改走服务层")

    report = evaluate_rules(packet, [rule])
    (violation,) = report.violations

    assert (violation.src, violation.dst) == ("ui/a.py", "infra/db.py")
    assert violation.where == "ui/a.py:3"
    assert violation.edge_type == "IMPORTS"
    assert violation.severity == "error"
    assert "改走服务层" in violation.message


def test_reverse_direction_is_not_a_violation():
    packet = _packet(NODES, [_imp("infra/db.py", "ui/a.py", evidence="infra/db.py:1")])
    rule = _rule(id="no-ui-infra", from_="layer:ui", to="layer:infra")
    assert evaluate_rules(packet, [rule]).violations == []


def test_layer_attribution_comes_from_layer_id_not_path_prefix():
    """层的归属只认 ``Node.layer_id``，**不按层名去猜路径前缀**。

    真实仓库里层名与目录名经常对不上（层叫"界面层"，路径是 ``frontend-new/src``；
    或者一个层横跨多个目录）。按层名反推前缀，会在这种仓库上安静地恒不命中。
    """
    nodes = [
        _mod("frontend/web/x.py", "layer::ui"),
        _mod("backend/db/y.py", "layer::infra"),
    ]
    packet = _packet(nodes, [_imp("frontend/web/x.py", "backend/db/y.py", evidence="frontend/web/x.py:4")])
    rule = _rule(id="by-layer", from_="layer:ui", to="layer:infra")

    (violation,) = evaluate_rules(packet, [rule]).violations
    assert violation.src == "frontend/web/x.py"


def test_type_only_is_reported_but_downgraded_to_warning():
    packet = _packet(NODES, [_imp("ui/a.py", "infra/db.py", evidence="ui/a.py:9", type_only=True)])
    rule = _rule(id="no-ui-infra", from_="layer:ui", to="layer:infra")

    (violation,) = evaluate_rules(packet, [rule]).violations
    assert violation.severity == "warning"
    assert violation.type_only is True
    assert "仅类型" in violation.message


def test_type_only_error_mode_keeps_rule_severity():
    packet = _packet(NODES, [_imp("ui/a.py", "infra/db.py", evidence="ui/a.py:9", type_only=True)])
    rule = _rule(id="strict", from_="layer:ui", to="layer:infra", type_only="error")
    (violation,) = evaluate_rules(packet, [rule]).violations
    assert violation.severity == "error"


def test_type_only_ignore_hides_detail_but_still_counts():
    """``ignore`` 不列明细，但**摘要计数不许为 0** —— 否则等于"配了就无声消失"。"""
    packet = _packet(NODES, [_imp("ui/a.py", "infra/db.py", evidence="ui/a.py:9", type_only=True)])
    rule = _rule(id="quiet", from_="layer:ui", to="layer:infra", type_only="ignore")

    report = evaluate_rules(packet, [rule])
    assert report.violations == []
    assert report.ignored_type_only == {"quiet": 1}


def test_imports_and_calls_are_not_double_counted_by_default():
    """同一对模块同时有 IMPORTS 与 CALLS：默认只报 1 条；显式要 CALLS 才报 2 条。"""
    packet = _packet(
        NODES + [_sym("ui/a.py", "render"), _sym("infra/db.py", "query")],
        [
            _imp("ui/a.py", "infra/db.py", evidence="ui/a.py:3"),
            _calls(("ui/a.py", "render"), ("infra/db.py", "query"), evidence="ui/a.py:20"),
        ],
    )
    only_imports = _rule(id="r", from_="layer:ui", to="layer:infra")
    both = _rule(id="r", from_="layer:ui", to="layer:infra", edges=["IMPORTS", "CALLS"])

    assert len(evaluate_rules(packet, [only_imports]).violations) == 1
    violations = evaluate_rules(packet, [both]).violations
    assert sorted(v.edge_type for v in violations) == ["CALLS", "IMPORTS"]


def test_calls_endpoints_are_lifted_to_their_modules():
    """CALLS 连的是符号节点。不先升到模块，跨层调用永远命不中（恒假规则）。"""
    packet = _packet(
        NODES + [_sym("ui/a.py", "render"), _sym("infra/db.py", "query")],
        [_calls(("ui/a.py", "render"), ("infra/db.py", "query"), evidence="ui/a.py:20")],
    )
    rule = _rule(id="call", from_="layer:ui", to="layer:infra", edges=["CALLS"])

    (violation,) = evaluate_rules(packet, [rule]).violations
    assert (violation.src, violation.dst) == ("ui/a.py", "infra/db.py")
    assert violation.where == "ui/a.py:20"


def test_dir_target_matches_prefix_only():
    packet = _packet(
        NODES + [_mod("ui/sub/deep.py", "layer::ui")],
        [
            _imp("ui/a.py", "infra/db.py", evidence="ui/a.py:3"),
            _imp("ui/sub/deep.py", "infra/db.py", evidence="ui/sub/deep.py:7"),
        ],
    )
    rule = _rule(id="dir", from_="dir:ui", to="dir:infra")
    assert {v.src for v in evaluate_rules(packet, [rule]).violations} == {
        "ui/a.py",
        "ui/sub/deep.py",
    }

    narrow = _rule(id="narrow", from_="dir:ui/sub", to="dir:infra")
    assert [v.src for v in evaluate_rules(packet, [narrow]).violations] == ["ui/sub/deep.py"]


def test_module_target_matches_exact_path():
    packet = _packet(
        NODES + [_mod("ui/b.py", "layer::ui")],
        [
            _imp("ui/a.py", "infra/db.py", evidence="ui/a.py:3"),
            _imp("ui/b.py", "infra/db.py", evidence="ui/b.py:4"),
        ],
    )
    rule = _rule(id="exact", from_="module:ui/a.py", to="module:infra/db.py")
    assert [v.src for v in evaluate_rules(packet, [rule]).violations] == ["ui/a.py"]


def test_missing_evidence_degrades_to_module_path_not_a_fake_line():
    packet = _packet(NODES, [_imp("ui/a.py", "infra/db.py")])
    rule = _rule(id="no-ev", from_="layer:ui", to="layer:infra")

    (violation,) = evaluate_rules(packet, [rule]).violations
    assert violation.where == "ui/a.py"
    assert "仅模块级" in violation.message


def test_cycle_rule_only_reports_inside_scope():
    """scope 之外成环不算这条规则的账。"""
    nodes = [
        _mod("ui/a.py", "layer::ui"),
        _mod("ui/b.py", "layer::ui"),
        _mod("infra/x.py", "layer::infra"),
        _mod("infra/y.py", "layer::infra"),
    ]
    edges = [
        _imp("ui/a.py", "ui/b.py", evidence="ui/a.py:1"),
        _imp("ui/b.py", "ui/a.py", evidence="ui/b.py:2"),
        _imp("infra/x.py", "infra/y.py", evidence="infra/x.py:1"),
        _imp("infra/y.py", "infra/x.py", evidence="infra/y.py:2"),
    ]
    packet = _packet(nodes, edges)
    rule = _rule(id="no-ui-cycles", kind="forbid-cycle", scope="layer:ui", severity="warning")

    violations = evaluate_rules(packet, [rule]).violations
    assert len(violations) == 1
    assert {violations[0].src, violations[0].dst} == {"ui/a.py", "ui/b.py"}
    assert violations[0].severity == "warning"
    assert "环" in violations[0].message


def test_acyclic_scope_reports_nothing():
    nodes = [_mod("ui/a.py", "layer::ui"), _mod("ui/b.py", "layer::ui")]
    packet = _packet(nodes, [_imp("ui/a.py", "ui/b.py", evidence="ui/a.py:1")])
    rule = _rule(id="cyc", kind="forbid-cycle", scope="layer:ui")
    assert evaluate_rules(packet, [rule]).violations == []


def test_layer_reference_missing_from_packet_is_reported_as_problem():
    """配置里的层名在数据包里不存在 ⇒ 必须报出来。

    典型成因：产物是旧配置生成的（改了层名没重跑 analyze）。此时规则会恒不命中，
    而"零违例"看起来像"架构很干净" —— 这是最危险的一种绿灯。
    """
    packet = _packet(NODES, [_imp("ui/a.py", "infra/db.py", evidence="ui/a.py:3")])
    rule = _rule(id="stale", from_="layer:web", to="layer:infra")

    report = evaluate_rules(packet, [rule])
    assert report.violations == []
    assert report.problems and "web" in report.problems[0]


def test_unmatched_dir_target_is_reported_as_problem():
    packet = _packet(NODES, [])
    rule = _rule(id="typo", from_="dir:nope", to="layer:infra")
    report = evaluate_rules(packet, [rule])
    assert report.problems and "nope" in report.problems[0]


def test_clean_packet_reports_nothing():
    packet = _packet(NODES, [_imp("infra/db.py", "ui/a.py", evidence="infra/db.py:1")])
    rule = _rule(id="r", from_="layer:ui", to="layer:infra")
    report = evaluate_rules(packet, [rule])
    assert report.violations == [] and report.problems == []
