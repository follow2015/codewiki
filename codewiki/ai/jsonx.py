"""从模型输出里取出**一个** JSON 对象 —— 按括号配平，不按正则。

此前用的是 ``re.compile(r"\\{.*\\}", re.DOTALL)``。它的失效方式很具体：

* 模型先写解释再写 JSON、或在 JSON 后又补一段话时，贪婪匹配会把"第一个 ``{``
  到最后一个 ``}``"整段吞下 —— 中间夹着散文就 ``JSONDecodeError``；
* 一轮输出里出现**两个**对象（例如先给结论、再给附录）时，吞下来的那段不是
  合法 JSON，于是"模型明明答对了"却被判成解析失败；
* 字符串字面量里的 ``}``（``{"note": "见 foo()} "}``）与转义引号同样会把
  正则骗过去。

配平扫描按真实嵌套层级收尾，并且**跳过字符串字面量**（含 ``\\"`` 转义）。
解析不出来时返回**原因**而不是空字符串：上层要把原因回喂给模型重试一次，
没有原因的"你错了"只会让它再猜一遍。
"""

from __future__ import annotations

import json

MAX_SCAN_CHARS = 1_000_000


def _match_object(text: str, start: int) -> tuple[int, bool]:
    """从 ``start``（必须是 ``{``）起找到配平的右括号位置。

    返回 ``(end, balanced)``：``end`` 是右括号的下一个下标（可直接切片）；
    ``balanced=False`` 表示扫到结尾都没闭合 —— 调用方据此停止向后搜索，
    因为不闭合的对象会把后续内容全吞掉，后面不可能再有一个完整对象。
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1, True
    return len(text), False


def extract_json_object(text: str) -> tuple[dict | None, str]:
    """取输出里第一个**能解析成对象**的配平片段。

    返回 ``(value, error)``：成功时 ``error == ""``；失败时 ``value is None``
    且 ``error`` 是可回喂的中文原因。
    """
    source = text or ""
    if len(source) > MAX_SCAN_CHARS:
        source = source[:MAX_SCAN_CHARS]
    if not source.strip():
        return None, "输出为空，没有可解析的内容"

    start = 0
    last_error = ""
    while True:
        begin = source.find("{", start)
        if begin < 0:
            break
        end, balanced = _match_object(source, begin)
        if not balanced:
            last_error = last_error or "JSON 不完整（花括号未配平），输出可能被截断"
            break
        chunk = source[begin:end]
        try:
            value = json.loads(chunk)
        except json.JSONDecodeError as exc:
            last_error = f"JSON 解析失败：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）"
            start = begin + 1
            continue
        if not isinstance(value, dict):
            last_error = "JSON 顶层不是对象（协议要求 {...}）"
            start = end
            continue
        return value, ""

    if last_error:
        return None, last_error
    if source.strip().startswith("["):
        return None, "JSON 顶层不是对象（协议要求 {...}）"
    return None, "输出里没有找到 JSON 对象（模型可能没有按约定形状作答）"


__all__ = ["MAX_SCAN_CHARS", "extract_json_object"]
