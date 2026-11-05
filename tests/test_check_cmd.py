"""``codewiki check``：把规则裁决成退出码（阶段一 1.1 的出口）。

这条命令的三条设计约束，每条都有一个用例：

1. **退出码 0/1/2 三态**：0 = 无 error 级违例；1 = 有；2 = 配置或输入错误。
   ``3`` 已被"外置推理待作答"占用，语义完全不同，不许挪用。
2. **必须说清"你在裁决哪份快照"**：输出头固定打印 ``generated_at`` /
   ``repo_commit`` / ``tool_version``。拿旧产物跑出绿灯是这条链路上最容易发生的误判。
3. **"没有规则" ≠ "检查通过"**：没声明规则时退出 0，但措辞必须让人看出
   "什么都没检查" —— 这两件事在 CI 日志里长得一模一样。
4. **存量与新增分账**：基线内的违例不报明细、但条数要进摘要；基线里已消失的条目
   提示收缩（``RULE_BASELINE_STALE``，warning，不进退出码）。核心用例是"被基线记录的
   代码整体上移 10 行 ⇒ 仍退出 0"，变异点是"把行号塞进身份"。
"""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from codewiki.cli.check_cmd import check
from codewiki.models import CONF_STATIC, Edge, EdgeType, Node, NodeType
from codewiki.render.jsonio import packet_from_parts

GOLDEN = Path(__file__).parent / "golden" / "check_report.json"

CONFIG = """
[architecture]
[[architecture.layers]]
name = "ui"
paths = ["ui"]

[[architecture.layers]]
name = "infra"
paths = ["infra"]
"""

RULE_ERROR = """
[[rules]]
id = "no-ui-infra"
kind = "forbid-dependency"
from = "layer:ui"
to = "layer:infra"
severity = "error"
reason = "改走服务层"
"""

RULE_WARNING = """
[[rules]]
id = "no-ui-infra"
kind = "forbid-dependency"
from = "layer:ui"
to = "layer:infra"
severity = "warning"
"""

RULE_MISSING_LAYER = """
[[architecture.layers]]
name = "web"
paths = ["web"]

[[rules]]
id = "no-web-infra"
kind = "forbid-dependency"
from = "layer:web"
to = "layer:infra"
"""


def _packet(*, sources=("ui/a.py",), lines=None):
    """最小可裁决的数据包：每个 ``sources`` 里的一条都从 ui 模块 import 一个 infra 模块。

    ``lines`` 用来单独改某条的 evidence 行号 —— WP4 的核心用例靠它。
    """
    lines = lines or {}
    nodes = [Node(id="mod::infra/db.py", type=NodeType.MODULE, path="infra/db.py", layer_id="layer::infra")]
    edges = []
    for src in sources:
        nodes.append(Node(id=f"mod::{src}", type=NodeType.MODULE, path=src, layer_id="layer::ui"))
        edges.append(
            Edge(
                source=f"mod::{src}",
                target="mod::infra/db.py",
                type=EdgeType.IMPORTS,
                evidence=f"{src}:{lines.get(src, 3)}",
                confidence=CONF_STATIC,
            )
        )
    return packet_from_parts(title="t", layers=[], nodes=nodes, edges=edges, annotations=[], diagnostics=[])


def _args(
    tmp_path,
    *,
    cfg: Path,
    fmt: str = "text",
    max_per_rule: int = 20,
    baseline=None,
    write_baseline: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        input=str(tmp_path / "codewiki.json"),
        config=str(cfg),
        format=fmt,
        max_per_rule=max_per_rule,
        baseline=None if baseline is None else str(baseline),
        write_baseline=write_baseline,
    )


def _run(
    tmp_path,
    *,
    config: str,
    fmt: str = "text",
    max_per_rule: int = 20,
    baseline=None,
    write_baseline: bool = False,
    packet=None,
) -> tuple[int, str]:
    (tmp_path / "codewiki.json").write_text((packet or _packet()).model_dump_json(), encoding="utf-8")
    cfg = tmp_path / "codewiki.toml"
    cfg.write_text(CONFIG + config, encoding="utf-8")
    args = _args(
        tmp_path,
        cfg=cfg,
        fmt=fmt,
        max_per_rule=max_per_rule,
        baseline=baseline,
        write_baseline=write_baseline,
    )
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = check(args)
    return code, out.getvalue() + err.getvalue()


def test_error_violation_exits_1(tmp_path):
    code, out = _run(tmp_path, config=RULE_ERROR)
    assert code == 1
    assert "no-ui-infra" in out
    assert "ui/a.py:3" in out


def test_warning_only_exits_0(tmp_path):
    """warning 不进退出码 —— 否则"提醒"和"拦截"没区别，用户只能把规则全删掉。"""
    code, out = _run(tmp_path, config=RULE_WARNING)
    assert code == 0
    assert "no-ui-infra" in out


def test_clean_run_exits_0(tmp_path):
    code, _ = _run(
        tmp_path,
        config=RULE_ERROR.replace('"layer:ui"\nto = "layer:infra"', '"layer:infra"\nto = "layer:ui"'),
    )
    assert code == 0


def test_missing_packet_exits_2(tmp_path):
    args = _args(tmp_path, cfg=tmp_path / "codewiki.toml")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = check(args)
    assert code == 2


def test_invalid_config_exits_2(tmp_path):
    code, out = _run(
        tmp_path,
        config="""
[[rules]]
id = "broken"
kind = "forbid-dependency"
from = "layer:ui"
""",
    )
    assert code == 2
    assert "broken" in out


def test_no_rules_exits_0_but_says_nothing_was_checked(tmp_path):
    """退出 0，但措辞必须与"检查通过"区分开 —— 这是最容易被当绿灯的一种状态。"""
    code, out = _run(tmp_path, config="")
    assert code == 0
    assert "没有任何东西被检查" in out or "未声明" in out


def test_output_header_pins_the_snapshot(tmp_path):
    """裁决哪份快照必须能看见：generated_at / repo_commit / tool_version 一个都不能少。"""
    code, out = _run(tmp_path, config=RULE_ERROR)
    assert code == 1
    for field in ("generated_at", "repo_commit", "tool_version"):
        assert field in out, out


def test_json_format_has_stable_fields(tmp_path):
    code, out = _run(tmp_path, config=RULE_ERROR, fmt="json")
    assert code == 1
    payload = json.loads(out)

    assert set(payload) == {
        "packet",
        "rules",
        "summary",
        "violations",
        "truncated",
        "ignored_type_only",
        "baseline",
        "problems",
    }
    assert payload["rules"]["declared"] == 1
    assert payload["summary"]["error"] == 1
    assert payload["baseline"] == {"enabled": False, "path": None, "skipped": 0, "stale": []}
    assert payload["violations"][0]["where"] == "ui/a.py:3"
    assert payload["violations"][0]["rule_id"] == "no-ui-infra"


def test_max_per_rule_truncates_but_reports_the_rest(tmp_path):
    """截断是给终端看的，但**丢掉多少条**必须写出来，否则等于谎报"就这么多"。"""
    (tmp_path / "codewiki.json").write_text(
        _packet(sources=("ui/a.py", "ui/b.py")).model_dump_json(), encoding="utf-8"
    )
    cfg = tmp_path / "codewiki.toml"
    cfg.write_text(CONFIG + RULE_ERROR, encoding="utf-8")

    args = _args(tmp_path, cfg=cfg, fmt="json", max_per_rule=1)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = check(args)

    payload = json.loads(buffer.getvalue())
    assert code == 1
    assert len(payload["violations"]) == 1
    assert payload["truncated"] == {"no-ui-infra": 1}
    assert payload["summary"]["error"] == 2


def test_rule_target_missing_from_packet_exits_2(tmp_path):
    """配置里声明了 web 层、规则引用它，但产物里一个 web 模块都没有。

    典型成因：产物由旧配置生成（改了分层没重跑 analyze）。此时规则恒不命中，
    "零违例"会被当成"架构干净" —— 所以这一轮裁决无效，给 2 而不是 0。
    """
    code, out = _run(tmp_path, config=RULE_MISSING_LAYER)
    assert code == 2
    assert "web" in out


def test_json_report_matches_golden(tmp_path):
    """字段名与文案一旦发布就是契约（CI 会解析它）：逐字段比对，不靠"看起来对"。"""
    nodes = [
        Node(id="mod::ui/a.py", type=NodeType.MODULE, path="ui/a.py", layer_id="layer::ui"),
        Node(id="mod::infra/db.py", type=NodeType.MODULE, path="infra/db.py", layer_id="layer::infra"),
    ]
    edges = [
        Edge(
            source="mod::ui/a.py",
            target="mod::infra/db.py",
            type=EdgeType.IMPORTS,
            evidence="ui/a.py:3",
            confidence=CONF_STATIC,
        ),
        Edge(
            source="mod::ui/a.py",
            target="mod::infra/db.py",
            type=EdgeType.IMPORTS,
            evidence="ui/a.py:9",
            confidence=CONF_STATIC,
            type_only=True,
        ),
    ]
    packet = packet_from_parts(
        title="t",
        layers=[],
        nodes=nodes,
        edges=edges,
        annotations=[],
        diagnostics=[],
        generated_at="2026-01-01T00:00:00Z",
        repo_commit="deadbeef",
    )
    packet_path = tmp_path / "codewiki.json"
    packet_path.write_text(packet.model_dump_json(), encoding="utf-8")
    cfg = tmp_path / "codewiki.toml"
    cfg.write_text(CONFIG + RULE_ERROR, encoding="utf-8")

    args = _args(tmp_path, cfg=cfg, fmt="json")
    out = io.StringIO()
    with redirect_stdout(out):
        code = check(args)

    payload = json.loads(out.getvalue())
    payload["packet"]["path"] = "<packet>"
    payload["packet"]["tool_version"] = "<tool_version>"
    assert payload == json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert code == 1


def _baseline_for(tmp_path, *, packet=None) -> Path:
    """先写一本基线（"我现在接受这些"的显式动作），返回它的路径。"""
    baseline = tmp_path / "baseline.json"
    code, out = _run(tmp_path, config=RULE_ERROR, baseline=baseline, write_baseline=True, packet=packet)
    assert code == 0, out
    assert baseline.is_file(), out
    return baseline


def test_write_baseline_prints_counts_then_clean_run_exits_0(tmp_path):
    """写基线和读基线是两个开关：写是"我现在接受这些"，读是 CI 每跑一次的日常。

    写之前必须把"记了什么"打出来 —— 静默记存量等于让新违例永远进不了账。
    """
    baseline = tmp_path / "baseline.json"
    code, out = _run(tmp_path, config=RULE_ERROR, baseline=baseline, write_baseline=True)
    assert code == 0, out
    assert "记录 1 条" in out
    assert "新增 1" in out
    assert "已消失 0" in out

    code, out = _run(tmp_path, config=RULE_ERROR, baseline=baseline)
    assert code == 0, out
    assert "另有 1 条存量" in out
    assert "ui/a.py:3" not in out


def test_only_the_new_violation_is_reported(tmp_path):
    """基线之后手工加一处违规：退出 1，且**只**报这一条。"""
    baseline = _baseline_for(tmp_path)

    code, out = _run(
        tmp_path,
        config=RULE_ERROR,
        baseline=baseline,
        packet=_packet(sources=("ui/a.py", "ui/b.py")),
    )
    assert code == 1, out
    assert "ui/b.py" in out
    assert "ui/a.py" not in out


def test_shifting_the_code_down_keeps_the_baseline_valid(tmp_path):
    """WP4 的核心用例：被基线记录的代码整体上移 10 行 ⇒ **仍然退出 0**。

    身份是 ``(rule_id, src, dst, edge_type)``，不含行号。行号一进身份，改一行注释
    就能让整本基线失效 —— 那和大家干脆不用基线是同一种结局。
    """
    baseline = _baseline_for(tmp_path, packet=_packet(lines={"ui/a.py": 3}))

    code, out = _run(
        tmp_path,
        config=RULE_ERROR,
        baseline=baseline,
        packet=_packet(lines={"ui/a.py": 13}),
    )
    assert code == 0, out
    assert "另有 1 条存量" in out
    assert "RULE_BASELINE_STALE" not in out


def test_stale_baseline_entry_is_a_warning_not_a_failure(tmp_path):
    """基线里有、当前已经没有的条目：提示收缩，但**不拦 CI**。

    这不是"架构出了问题"，而是"账该更新了"。让它拦 CI，团队唯一的出路是把基线
    删掉，正好毁掉基线存在的意义。
    """
    baseline = _baseline_for(tmp_path, packet=_packet(sources=("ui/a.py", "ui/b.py")))

    code, out = _run(tmp_path, config=RULE_ERROR, baseline=baseline, packet=_packet(sources=("ui/a.py",)))
    assert code == 0, out
    assert "RULE_BASELINE_STALE" in out
    assert "ui/b.py" in out
    assert "已不存在" in out


def test_stale_entries_are_capped_like_violations(tmp_path):
    """失效条目也按 --max-per-rule 截断，但截掉的条数要说出来。"""
    baseline = _baseline_for(tmp_path, packet=_packet(sources=("ui/a.py", "ui/b.py", "ui/c.py")))

    code, out = _run(
        tmp_path,
        config=RULE_ERROR,
        baseline=baseline,
        max_per_rule=1,
        packet=_packet(sources=("ui/a.py",)),
    )
    assert code == 0, out
    assert "另有 1 条未列出" in out


def test_broken_baseline_exits_2(tmp_path):
    """坏基线**不能**当空基线：那会把全部存量当成新增，一次刷出几百条。"""
    baseline = tmp_path / "baseline.json"
    baseline.write_text("{not json", encoding="utf-8")

    code, out = _run(tmp_path, config=RULE_ERROR, baseline=baseline)
    assert code == 2
    assert "基线" in out


def test_write_baseline_refuses_when_rules_did_not_take_effect(tmp_path):
    """规则指向的层在产物里不存在时，写下的是一本**假账**。

    违例因"规则没生效"而缺失，基线却把它记成"这些已经清干净了" —— 以后真出现
    违例也会被这本账静默吞掉。所以这里拒绝写，并给 2（裁决本身无效）。
    """
    baseline = tmp_path / "baseline.json"
    code, out = _run(tmp_path, config=RULE_MISSING_LAYER, baseline=baseline, write_baseline=True)
    assert code == 2, out
    assert not baseline.exists()
    assert "拒绝写入基线" in out
