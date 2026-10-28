"""括号配平 JSON 抽取（替代贪婪 ``\\{.*\\}``）。

贪婪正则的失效方式很具体：模型先写一段解释、再写 JSON，或者输出**两个** JSON
对象时，``\\{.*\\}`` 会把"第一个 ``{``"到"最后一个 ``}``"整段吞下去 —— 中间夹着
散文就解析失败，夹着第二个对象就解析成垃圾。配平扫描按真实的嵌套层级收尾，
字符串字面量里的花括号（``{"note": "}"}``）也不该被当成结构。

这里同时锁住"解析不出来要给出**原因**"：重试需要把错误回喂给模型，空字符串
让模型只能瞎猜。
"""

from __future__ import annotations

import json

import pytest

from codewiki.ai.jsonx import extract_json_object


def test_plain_object():
    value, error = extract_json_object('{"a": 1}')
    assert value == {"a": 1} and error == ""


def test_surrounded_by_prose_and_fence():
    raw = '思考一下……\n```json\n{"modules": [{"target": "mod::a.py"}]}\n```\n以上。'
    value, error = extract_json_object(raw)
    assert value == {"modules": [{"target": "mod::a.py"}]} and error == ""


def test_braces_inside_strings_are_not_balanced():
    value, error = extract_json_object('{"note": "用 { 表示集合，} 表示结束", "n": 2}')
    assert value == {"note": "用 { 表示集合，} 表示结束", "n": 2}
    assert error == ""


def test_escaped_quote_inside_string():
    raw = r'{"note": "他说 \"}\" 就结束了", "ok": true}'
    value, error = extract_json_object(raw)
    assert value == {"note": '他说 "}" 就结束了', "ok": True}
    assert error == ""


def test_two_objects_take_the_first_balanced_one():
    """贪婪正则在这里会把两个对象连同中间的散文一起吞掉。"""
    raw = '{"a": 1}\n\n补充说明：还有一个\n{"b": 2}'
    value, error = extract_json_object(raw)
    assert value == {"a": 1} and error == ""


def test_nested_objects_are_kept_whole():
    raw = 'prefix {"modules":[{"evidence":[{"path":"a.py","lines":[1,2]}]}]} suffix'
    value, error = extract_json_object(raw)
    assert value["modules"][0]["evidence"][0]["lines"] == [1, 2]


def test_no_object_reports_reason():
    value, error = extract_json_object("完全不是 JSON")
    assert value is None and "没有找到" in error


def test_unbalanced_reports_reason():
    value, error = extract_json_object('{"a": 1, "b": ')
    assert value is None and "不完整" in error


def test_bad_syntax_reports_json_error():
    value, error = extract_json_object('{"a": 1,,}')
    assert value is None and "解析失败" in error


def test_non_object_json_is_rejected():
    """``[1,2]`` 是合法 JSON 但不是"对象"—— 调用方要拿到 None 而不是 list。"""
    value, error = extract_json_object("[1, 2, 3]")
    assert value is None and "对象" in error


def test_empty_input():
    value, error = extract_json_object("")
    assert value is None and error


@pytest.mark.parametrize(
    "raw",
    [
        '{"text": "a}b{c"}',
        '{"text": "\\\\"}',
        '{"deep": {"deeper": {"x": [1, {"y": 2}]}}}',
    ],
)
def test_roundtrip_matches_stdlib(raw):
    """配平结果必须与 ``json.loads`` 对同一段的解析一致（自己不能有偏差）。"""
    value, error = extract_json_object(raw)
    assert error == ""
    assert value == json.loads(raw)
