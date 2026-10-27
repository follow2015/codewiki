"""L1 校验门判据（§4.3）—— 每条特征都必须有**能拦下的样本**与**不被误杀的样本**。

两组用例缺一不可：

* 只测坏图 ⇒ 把特征写严了会误杀合法图（合法 label 里的中文、括号实体）；
* 只测好图 ⇒ 特征退化成"永远通过"，坏图照样落盘。
"""
from __future__ import annotations

from codewiki.graph import build_graph
from codewiki.render.jsonio import packet_from_parts
from codewiki.render.mermaid_macro import escape_label, render_system_context
from codewiki.render.mermaid_validator import validate_l1
from tests.conftest import parse_all


def _packet(mini_py, layers=None):
    result = build_graph(parse_all(mini_py), layers=layers)
    return packet_from_parts(title="mini", layers=layers or [], nodes=result.nodes, edges=result.edges)


def test_missing_type_declaration_is_caught():
    issues = validate_l1('  A["x"] --> B["y"]\n')

    assert {i.code for i in issues} == {"TYPE_MISSING"}


def test_unsupported_type_is_caught():
    issues = validate_l1("pie title x\n  \"a\" : 1\n")

    assert any(i.code == "TYPE_UNSUPPORTED" for i in issues)


def test_unclosed_subgraph_is_caught():
    text = 'flowchart TD\n  subgraph S["层"]\n    A["a"]\n'

    assert any(i.code == "SUBGRAPH_UNCLOSED" for i in issues_of(text))


def test_unbalanced_brackets_and_quotes_are_caught():
    assert any(i.code == "BRACKET_UNBALANCED" for i in validate_l1('flowchart TD\n  A["a"\n'))
    assert any(i.code == "QUOTE_UNBALANCED" for i in validate_l1('flowchart TD\n  A["a --> B\n'))
    assert any(i.code == "PAREN_UNBALANCED" for i in validate_l1('flowchart TD\n  A("a"\n'))


def test_illegal_node_id_is_caught():
    text = 'flowchart TD\n  pkg.core["模块"]\n'

    assert any(i.code == "NODE_ID_ILLEGAL" for i in issues_of(text))


def test_unescaped_label_characters_are_caught():
    text = 'flowchart TD\n  A["my (label)"]\n'

    assert any(i.code == "LABEL_UNESCAPED" for i in issues_of(text))


def test_unclosed_code_fence_is_caught():
    text = '```mermaid\nflowchart TD\n  A["a"]\n'

    assert any(i.code == "FENCE_UNCLOSED" for i in issues_of(text))


def test_disallowed_rich_tag_is_caught():
    text = 'flowchart TD\n  A["<script>alert(1)</script>"]\n'

    assert any(i.code == "TAG_NOT_ALLOWED" for i in issues_of(text))


def test_empty_diagram_is_caught():
    assert any(i.code == "EMPTY_DIAGRAM" for i in validate_l1("   \n"))


def test_generated_system_context_passes_l1(mini_py):
    packet = _packet(mini_py)
    text = render_system_context(packet)

    assert validate_l1(text) == [], f"自家产物被自家校验门误杀: {validate_l1(text)}"


def test_legal_writing_with_chinese_and_entities_passes():
    text = (
        "flowchart TD\n"
        '  subgraph S["服务层"]\n'
        '    A["订单 #40;核心#41;"]\n'
        "  end\n"
        '  A -->|3| B["库存"]\n'
    )
    assert validate_l1(text) == []


def test_br_and_bold_tags_are_allowed():
    text = 'flowchart TD\n  A["<b>核心</b><br/>第二行"]\n'

    assert validate_l1(text) == []


def test_dynamic_edges_and_fenced_blocks_pass():
    text = "```mermaid\nflowchart TD\n  A -.->|0.40?| B\n```\n"

    assert validate_l1(text) == []


def test_escape_label_neutralises_special_characters():
    assert escape_label('a"b(c)[d]') == "a#quot;b#40;c#41;#91;d#93;"


def issues_of(text: str):
    return validate_l1(text)
