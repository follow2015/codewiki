"""CLI 输出编码兜底（``cli.make_stdio_forgiving``）。

测的是**降级行为本身**，不是"某行提示长什么样"：本仓每条提示都是中文，
而 Windows 控制台默认 cp1252 一个汉字都编不出来 —— 这类缺陷只在异平台上炸，
本地 macOS/UTF-8 跑一万遍也看不见。所以用 ascii 编码的假流把条件造出来。
"""

from __future__ import annotations

import io

from codewiki.cli import make_stdio_forgiving

CHINESE = "缓存：命中 3 / 重新解析 2"


class _Stream:
    """记录 ``reconfigure`` 收到什么参数的假流。"""

    def __init__(self, *, tty: bool) -> None:
        self._tty = tty
        self.calls: list[dict[str, object]] = []

    def isatty(self) -> bool:
        return self._tty

    def reconfigure(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


class _NoReconfigure:
    """像 ``io.StringIO`` / pytest 的捕获对象：没有编码可改。"""

    def isatty(self) -> bool:
        return False


def test_non_tty_stream_is_switched_to_utf8():
    """管道 / 文件 / CI 日志：没有"控制台编码"这回事，统一 UTF-8。"""
    stream = _Stream(tty=False)
    make_stdio_forgiving(stream)
    assert stream.calls == [{"encoding": "utf-8", "errors": "replace"}]


def test_tty_keeps_its_encoding_and_only_relaxes_errors():
    """真终端上不改编码。

    老 cmd 用 ``chcp 936`` 时把流改成 UTF-8 只会得到乱码，比 ``?`` 更糟。
    这条断言就是那个取舍的锚点：谁把 ``encoding`` 加进 TTY 分支，它就红。
    """
    stream = _Stream(tty=True)
    make_stdio_forgiving(stream)
    assert stream.calls == [{"errors": "replace"}]


def test_stream_without_reconfigure_is_ignored():
    """StringIO、pytest 捕获对象没有 ``reconfigure``，不许因此崩。"""
    make_stdio_forgiving(_NoReconfigure())


def test_ascii_stream_does_not_raise_on_chinese_output():
    """端到端：ascii 编码的流在兜底之后，写中文不再抛 ``UnicodeEncodeError``。

    没有这一条，前三条只证明"参数传对了"，没证明"传对了就不炸"。
    """
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="ascii")
    assert not stream.isatty()

    make_stdio_forgiving(stream)
    stream.write(CHINESE + "\n")
    stream.flush()

    assert CHINESE in buffer.getvalue().decode("utf-8")


def test_closed_stream_does_not_raise():
    """流已关闭时 ``reconfigure`` 抛 ``ValueError`` —— 吞掉，继续跑。"""
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="ascii")
    stream.close()

    make_stdio_forgiving(stream)
