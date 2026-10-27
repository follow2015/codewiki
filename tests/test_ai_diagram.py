"""AI 时序图判据（§4.2 / §4.3 失败处理）。

最要紧的一条：**坏图必须零落盘**。本模块不写文件，所以判据落在"返回 None"上 ——
调用方拿到 None 才不会去写 .mmd；返回了坏文本就等于把责任推给下游。

另两条：

* 自修**只有一次**（无限自修会把 token 烧光，而且越修越歪）；
* AI 不可用时是 info/warning 降级，不是错误 —— 静态产物照常产出。
"""

from __future__ import annotations

from codewiki.ai.client import LLMUnavailable
from codewiki.ai.diagram import generate_sequence_diagram, strip_fences

GOOD = "sequenceDiagram\n  Alice->>Bob: hi\n"
BAD = "sequenceDiagram\n  Alice-!!->Bob: broken [\n"


def test_valid_diagram_is_returned(fake_client_factory):
    client = fake_client_factory([GOOD])
    result = generate_sequence_diagram(["A -> B"], client=client)
    assert result.text == GOOD.strip() and not result.diagnostics and not result.repaired


def test_fences_are_stripped():
    assert strip_fences("```mermaid\nflowchart TD\n  A[B]\n```") == "flowchart TD\n  A[B]"


def test_bad_diagram_is_repaired_once(fake_client_factory):
    client = fake_client_factory([BAD, GOOD])
    result = generate_sequence_diagram(["A -> B"], client=client)
    assert result.text == GOOD.strip() and result.repaired
    assert len(client.calls) == 2, "自修只有一次"


def test_still_bad_after_repair_yields_no_diagram(fake_client_factory):
    """两次都坏 ⇒ 返回 None（**不交付坏图**）+ 记 AI_DIAGRAM_INVALID。"""
    client = fake_client_factory([BAD, BAD])
    result = generate_sequence_diagram(["A -> B"], client=client, where="mod::a.py")

    assert result.text is None, "坏图必须不返回任何文本"
    assert [d.code for d in result.diagnostics] == ["AI_DIAGRAM_INVALID"]
    assert result.diagnostics[0].where == "mod::a.py"
    assert len(client.calls) == 2


def test_ai_unavailable_degrades_to_no_diagram(fake_client_factory):
    client = fake_client_factory([LLMUnavailable("down")])
    result = generate_sequence_diagram(["A -> B"], client=client)

    assert result.text is None
    assert [d.code for d in result.diagnostics] == ["AI_UNAVAILABLE"]


def test_no_client_is_skipped_not_failed():
    result = generate_sequence_diagram(["A -> B"], client=None)
    assert result.text is None and result.skipped
    assert result.diagnostics[0].level == "info"


def test_empty_chain_is_skipped(fake_client_factory):
    result = generate_sequence_diagram([], client=fake_client_factory([GOOD]))
    assert result.text is None and result.skipped and not result.diagnostics[0].code.endswith("INVALID")
