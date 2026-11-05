"""WP10：``ARCHITECTURE.md`` 一页架构报告。

计划给的验收只有一条，但它是硬约束：**与站点首页的六类数字逐项相等**。这条逼出的
不是"两边都调同一个函数"（那样相等是构造出来的、测不到东西），而是两个渲染器各自
把数字摆进自己的载体之后，从**生成出来的文本**里读回来还能对上。

所以这里的做法是：断言从 index.html 的 chips 解析出的数字、从 ARCHITECTURE.md 的
概览表解析出的数字、以及 ``architecture_stats`` 的返回值三方一致。任一边摆错位置、
写错键、漏一项，都会有一方对不上。

报告只写产物里已有的事实，**不调模型、不重算图**（环/核心节点/违例全部从
``packet.diagnostics`` 取）—— 与站点同源才不会出现"报告说 3 个环、站点说 2 个"。
"""

from __future__ import annotations

import re

from codewiki.models import CONF_STATIC, Edge, EdgeType, Node, NodeType
from codewiki.render.architecture_md import render_architecture_md
from codewiki.render.i18n import t
from codewiki.render.jsonio import packet_from_parts
from codewiki.render.site import render_index_html
from codewiki.render.stats import architecture_stats
from codewiki.schemas import Diagnostic

_CHIP = re.compile(r'<span class="chip[^"]*"><b>(\d+)</b>\s*([^<]+?)</span>')
_MD_ROW = re.compile(r"^\|\s*(.+?)\s*\|\s*(\d+)\s*\|")


def _packet():
    layers = [
        {"id": "layer::api", "name": "api", "order": 1},
        {"id": "layer::svc", "name": "svc", "order": 2},
        {"id": "layer::models", "name": "models", "order": 3},
    ]
    nodes = [
        Node(id="mod::api/a.py", type=NodeType.MODULE, path="api/a.py", layer_id="layer::api"),
        Node(id="mod::api/b.py", type=NodeType.MODULE, path="api/b.py", layer_id="layer::api"),
        Node(id="mod::svc/s.py", type=NodeType.MODULE, path="svc/s.py", layer_id="layer::svc"),
        Node(id="mod::models/m.py", type=NodeType.MODULE, path="models/m.py", layer_id="layer::models"),
    ]
    edges = [
        Edge(
            source="mod::api/a.py",
            target="mod::svc/s.py",
            type=EdgeType.IMPORTS,
            evidence="api/a.py:1",
            confidence=CONF_STATIC,
        ),
        Edge(
            source="mod::api/b.py",
            target="mod::models/m.py",
            type=EdgeType.IMPORTS,
            evidence="api/b.py:1",
            confidence=CONF_STATIC,
        ),
        Edge(
            source="layer::api",
            target="layer::svc",
            type=EdgeType.LAYER_DEPENDS,
            evidence="1 条跨层 import",
            confidence=CONF_STATIC,
            weight=1,
        ),
    ]
    diagnostics = [
        Diagnostic(level="info", code="RULES_EVALUATED", message="已按 1 条规则检查"),
        Diagnostic(
            level="error",
            code="RULE_VIOLATION",
            message="api 不许直接依赖 models",
            where="api/b.py:1",
            rule_id="no-api-to-models",
        ),
        Diagnostic(level="warning", code="MODULE_CYCLE", message="发现 1 个模块环", where="mod::svc/s.py"),
        Diagnostic(level="info", code="GOD_NODE", message="mod::svc/s.py 被 4 个模块依赖（跨层 2）"),
    ]
    return packet_from_parts(
        title="demo",
        layers=layers,
        nodes=nodes,
        edges=edges,
        annotations=[],
        diagnostics=diagnostics,
        generated_at="2026-01-01T00:00:00Z",
        repo_commit="deadbeef",
    )


def _chips(html: str) -> dict[str, int]:
    return {label.strip(): int(count) for count, label in _CHIP.findall(html)}


def _md_rows(md: str) -> dict[str, int]:
    rows: dict[str, int] = {}
    for line in md.splitlines():
        match = _MD_ROW.match(line.strip())
        if match:
            rows.setdefault(match.group(1).strip(), int(match.group(2)))
    return rows


SIX_KEYS = (
    "site.chip_modules",
    "site.chip_nodes",
    "site.chip_edges",
    "site.chip_layers",
    "site.chip_errors",
    "site.chip_violations",
)


def test_six_numbers_match_between_index_and_report():
    packet = _packet()
    stats = architecture_stats(packet)
    chips = _chips(render_index_html(packet))
    rows = _md_rows(render_architecture_md(packet))

    expected = [
        stats.modules,
        stats.nodes,
        stats.edges,
        stats.layers,
        stats.diagnostics_error,
        stats.violations_error,
    ]
    for key, value in zip(SIX_KEYS, expected, strict=True):
        label = t(key)
        assert label in chips, f"首页缺 chip：{label}（现有 {sorted(chips)}）"
        assert label in rows, f"报告缺概览行：{label}（现有 {sorted(rows)}）"
        assert chips[label] == value, f"首页 {label}={chips[label]}，真值 {value}"
        assert rows[label] == value, f"报告 {label}={rows[label]}，真值 {value}"


def test_violation_count_is_not_the_diagnostic_count():
    """两个数必须分得开：诊断 error 会把规则违例算进去，反之不成立。

    合成一个数的最惨后果是——规则违例涨了、解析诊断降了，总数不变，于是"没变化"。
    """
    packet = _packet()
    stats = architecture_stats(packet)

    assert stats.diagnostics_error == 1
    assert stats.violations_error == 1

    only_diagnostic = packet.model_copy(
        update={
            "diagnostics": [d for d in packet.diagnostics if d.code != "RULE_VIOLATION"]
            + [Diagnostic(level="error", code="PARSE_FAILED", message="x", where="a.py")]
        }
    )
    shifted = architecture_stats(only_diagnostic)
    assert shifted.diagnostics_error == 1
    assert shifted.violations_error == 0


def test_report_covers_layers_cycles_violations_and_god_nodes():
    md = render_architecture_md(_packet())

    assert t("arch.section_layers") in md
    assert t("arch.section_cycles") in md
    assert t("arch.section_violations") in md
    assert t("arch.section_nodes") in md

    assert "no-api-to-models" in md, "违例要带规则 id（分组依据）"
    assert "api/b.py:1" in md, "违例要带 where，否则贴进 PR 也没人知道改哪"
    assert "svc/s.py" in md
    assert "mod::" not in md, "内部节点 id 前缀不该出现在给人读的报告里"


def test_report_lists_every_layer_with_its_module_count():
    md = render_architecture_md(_packet())
    rows = _md_rows(md)

    assert rows.get("api") == 2
    assert rows.get("svc") == 1
    assert rows.get("models") == 1


def test_report_has_no_empty_sections():
    """没有环、没有违例时整段消失 —— 空标题会被读成"检查了但没有"，或者"漏了"。

    断言用 ``## <标题>`` 而不是裸文案：标题与 chip 标签可能互为子串（"规则违例" 是
    "规则违例 error" 的前缀），裸文本断言会红在一个完全不相干的地方。
    """
    packet = packet_from_parts(
        title="clean",
        layers=[{"id": "layer::only", "name": "only", "order": 1}],
        nodes=[Node(id="mod::only/a.py", type=NodeType.MODULE, path="only/a.py", layer_id="layer::only")],
        edges=[],
        annotations=[],
        diagnostics=[],
        generated_at="2026-01-01T00:00:00Z",
    )
    md = render_architecture_md(packet)

    assert f"## {t('arch.section_cycles')}" not in md
    assert f"## {t('arch.section_violations')}" not in md
    assert f"## {t('arch.section_nodes')}" not in md
    assert f"## {t('arch.section_diagnostics')}" in md


def test_trend_section_needs_two_rows():
    """与首页同一条判据：一个点不成趋势。"""
    from codewiki.graph.history import build_entry

    packet = _packet()
    assert t("arch.section_trend") not in render_architecture_md(packet)
    assert t("arch.section_trend") not in render_architecture_md(
        packet, history=[build_entry(packet, files=4)]
    )

    two = [build_entry(packet, files=4), build_entry(packet, files=5)]
    assert t("arch.section_trend") in render_architecture_md(packet, history=two)


def test_report_is_deterministic():
    """同一份数据包两次生成逐字节相同（报告是归档物，不是运行日志）。"""
    packet = _packet()

    assert render_architecture_md(packet) == render_architecture_md(packet)


def test_report_points_at_the_site_and_the_packet():
    """报告是给人贴 PR 的，要给出"想看细节去哪"。"""
    md = render_architecture_md(_packet())

    assert "index.html" in md
    assert "data/codewiki.json" in md


def test_report_records_provenance():
    md = render_architecture_md(_packet())

    assert "deadbeef" in md
    assert "2026-01-01T00:00:00Z" in md


def _build_repo(root) -> None:
    (root / "app" / "api").mkdir(parents=True, exist_ok=True)
    (root / "app" / "svc").mkdir(parents=True, exist_ok=True)
    (root / "app" / "api" / "h.py").write_text(
        "from app.svc.s import run\n\n\ndef handle():\n    return run()\n", encoding="utf-8"
    )
    (root / "app" / "svc" / "s.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (root / "codewiki.toml").write_text(
        """
[architecture]
[[architecture.layers]]
name = "api"
paths = ["app/api"]

[[architecture.layers]]
name = "svc"
paths = ["app/svc"]

[[rules]]
id = "no-api-svc"
kind = "forbid-dependency"
from = "layer:api"
to = "layer:svc"
severity = "error"
""",
        encoding="utf-8",
    )


def test_analyze_writes_the_report_next_to_the_packet(tmp_path):
    """端到端：报告随产物落盘，且六类数字与同一轮的 index.html 对得上。"""
    from codewiki.cli import main

    repo = tmp_path / "repo"
    _build_repo(repo)
    out = tmp_path / "out"

    assert main(["analyze", "--src", str(repo), "--out", str(out)]) == 0

    md = (out / "ARCHITECTURE.md").read_text(encoding="utf-8")
    chips = _chips((out / "index.html").read_text(encoding="utf-8"))
    rows = _md_rows(md)
    for key in SIX_KEYS:
        label = t(key)
        assert label in chips and label in rows, f"{label} 只出现在一边"
        assert chips[label] == rows[label], f"{label}：首页 {chips[label]}，报告 {rows[label]}"

    assert "index.html" in md


def _noisy_packet():
    """真实规模的噪声包：ipip 那种（几百条 info + 几十个核心节点 + 一堆违例）。

    小夹具测不出尺寸问题 —— 它们永远"一页"，于是限流代码可以整段删掉而不红。
    """
    layers = [{"id": f"layer::l{i}", "name": f"l{i}", "order": i + 1} for i in range(12)]
    nodes = [
        Node(
            id=f"mod::p{i}/m{i}.py", type=NodeType.MODULE, path=f"p{i}/m{i}.py", layer_id=f"layer::l{i % 12}"
        )
        for i in range(120)
    ]
    edges = [
        Edge(
            source=f"mod::p{i}/m{i}.py",
            target=f"mod::p{(i + 1) % 120}/m{(i + 1) % 120}.py",
            type=EdgeType.IMPORTS,
            evidence=f"p{i}/m{i}.py:1",
            confidence=CONF_STATIC,
        )
        for i in range(120)
    ]
    diagnostics: list[Diagnostic] = []
    for i in range(30):
        diagnostics.append(
            Diagnostic(
                level="warning",
                code="GOD_NODE",
                message=f"枢纽模块 m{i}：入度 {200 - i} / 出度 3",
                where=f"p{i}/m{i}.py",
            )
        )
    for i in range(40):
        diagnostics.append(
            Diagnostic(
                level="error",
                code="RULE_VIOLATION",
                message="层间直连不被允许",
                where=f"p{i}/m{i}.py:1",
                rule_id=f"rule-{i % 4}",
            )
        )
    for i in range(600):
        diagnostics.append(
            Diagnostic(
                level="info",
                code="CALL_AMBIGUOUS",
                message=f"同名符号 'f{i}' 有 6 个候选，无接收者类型信息，不绑定: "
                + ", ".join(f"sym::p{j}/m{j}.py::f{i}" for j in range(6)),
                where=f"p{i % 120}/m{i % 120}.py",
            )
        )
    for _ in range(25):
        diagnostics.append(
            Diagnostic(
                level="warning",
                code="MODULE_CYCLE",
                message="模块级依赖环: " + ", ".join(f"mod::p{j}/m{j}.py" for j in range(60)),
            )
        )
    return packet_from_parts(
        title="noisy",
        layers=layers,
        nodes=nodes,
        edges=edges,
        annotations=[],
        diagnostics=diagnostics,
        generated_at="2026-01-01T00:00:00Z",
    )


def test_report_stays_one_page():
    """一页 = 贴进 PR 不用折叠。

    ipip 真实数据（670 模块 / 717 条诊断）不加闸时是 **817 行 / 174 KB，最长一行
    1164 字符** —— 那不叫"一页报告"，没人会读。所以这里用同样规模的噪声包钉住：
    限流一旦被删掉，行数立刻翻十倍。
    """
    md = render_architecture_md(_noisy_packet())
    lines = md.splitlines()

    assert len(lines) <= 100, f"报告 {len(lines)} 行，超过一页了"
    assert len(md.encode()) <= 20_000, f"报告 {len(md.encode())} 字节"
    assert max(len(line) for line in lines) <= 400, "有超长行（会撑出一屏横滚）"

    assert "另有" in md or "more" in md
    assert "CALL_AMBIGUOUS" not in md, "info 级诊断挤掉了真正该看的 error/warning"


def test_violation_row_disappears_when_no_rules_ran():
    """没配规则 / 没跑过规则时，"规则违例 0" 必须整项消失。

    0 与"没检查"在数字上完全一样，含义却相反；``RULES_EVALUATED`` 就是二者的分界
    （WP5 引入它正是为此）。首页与报告必须同时遵守，否则一边说"0 条"、另一边不
    提，读者会以为两边口径不一致。
    """
    packet = _packet().model_copy(
        update={"diagnostics": [d for d in _packet().diagnostics if d.code != "RULES_EVALUATED"]}
    )

    assert t("site.chip_violations") not in _chips(render_index_html(packet))
    assert t("site.chip_violations") not in _md_rows(render_architecture_md(packet))
    assert t("site.chip_modules") in _chips(render_index_html(packet))


def test_report_has_no_trend_unless_trend_is_on(tmp_path):
    """趋势段与首页同一个开关（``--trend``）。

    这条是守卫：报告里放趋势等于把跨运行状态写进归档物，
    ``test_incremental_equals_full`` 的逐字节比对当场就红。所以默认必须没有 ——
    而且"没有"不能靠"历史只有 1 行"这种巧合，要有用例钉住。
    """
    from codewiki.cli import main

    repo = tmp_path / "repo"
    _build_repo(repo)
    out = tmp_path / "out"
    for _ in range(2):
        assert main(["analyze", "--src", str(repo), "--out", str(out)]) == 0

    md = (out / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert f"## {t('arch.section_trend')}" not in md

    assert main(["analyze", "--src", str(repo), "--out", str(out), "--trend"]) == 0
    md_with_trend = (out / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert f"## {t('arch.section_trend')}" in md_with_trend
