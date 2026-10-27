"""HTML 转义的唯一出口。

为什么单独一个模块、还要分成两个函数：转义的正确性取决于**语境**，而语境在调用点
是隐式的 —— 一个 ``html.escape(x)`` 放在元素内容里还是属性值里，看代码看不出来。

- 文本语境（元素内容）：``&`` ``<`` ``>`` 必须转义；引号转义了也无害。
- 属性语境（``href="..."``）：引号**必须**转义，否则一个 ``"`` 就能逃出属性，
  后面接上 ``onerror=`` 之类就是注入。

两者当前都按 ``quote=True`` 实现（从严），但**契约不同**：将来若为了让产物更好读而
放宽文本语境的引号，属性语境不能跟着放宽。把两件事合在一个函数里，那次改动一定会
连属性侧一起放宽 —— 这就是把它拆成两个名字而不是一个 ``escape()`` 的理由。

一个实际约束：``md_to_html`` 采用"先整体转义、再套 markdown 规则"的顺序，所以
``_inline()`` 拿到的 ``[label](target)`` 里 ``target`` **已经是转义过的文本**。
属性侧此时不应再转一次，否则会双重转义（``&amp;`` → ``&amp;amp;``，链接直接坏掉）。
本模块只提供出口，不做"再转义一次"的补偿 —— 那是调用点顺序的问题，见
``site.md_to_html`` 的 docstring。
"""
from __future__ import annotations

import html


def escape_text(value: str) -> str:
    """元素内容里的转义。

    ``quote=True``：不只是"从严"，而是当前实现依赖它 —— ``_inline()`` 在页内链接
    的 href 里直接复用这段已转义文本，此时若引号没被转义，属性就被逃出了。
    要放宽得先改 ``_inline`` 的顺序（先切分结构、再分别转义），那是一处独立改动。
    """
    return html.escape(value, quote=True)


def escape_attr(value: str) -> str:
    """属性值里的转义（``href="..."``、``id="..."``、``style="..."``）。

    与 :func:`escape_text` 实现相同，但语义是**必须**转义引号 —— 写在这里是为了让
    调用点自证"我知道这是属性"，而不是靠读者去数引号。
    """
    return html.escape(value, quote=True)


__all__ = ["escape_attr", "escape_text"]
