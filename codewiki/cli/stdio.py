"""输出流的编码容错（Windows 控制台下中文会直接崩）。

从 ``cli.py`` 原样搬来，一行逻辑没改 —— 它是"CLI 层"的事：只有在真正往终端
打印时才需要，流水线不该关心控制台用什么编码。
"""

from __future__ import annotations

import sys
from typing import TextIO


def make_stdio_forgiving(*streams: TextIO) -> None:
    """让输出流遇到"本编码表示不了"的字符时降级，而不是抛 ``UnicodeEncodeError``。

    背景：本 CLI 几乎每条提示都是中文，而 **Windows 控制台的默认编码是 cp1252**
    —— 一个汉字都编不出来，于是 ``analyze`` 在打"缓存：命中 N"时就整个崩掉
    （CI 的 windows 跑腿实测）。中文提示不是可选装饰，输出侧必须容得下它。

    策略分两档，按"有没有真人在看"决定：

    - **不是终端**（管道 / 重定向到文件 / CI 日志）：没有"控制台编码"这回事，
      统一改 UTF-8。下游拿到的字节流本来就该是 UTF-8。
    - **是终端**：**不改编码**。老 cmd 用 ``chcp 936`` 时把它改成 UTF-8 只会
      得到乱码，比 ``?`` 更糟；这里只把 ``errors`` 从 strict 放松到 replace，
      汉字退化成 ``?``，但命令能跑完。

    用 ``reconfigure`` 而不是重新包一层 ``TextIOWrapper``：包一层会让已经缓存了
    旧 ``sys.stdout`` 引用的库（日志、``print`` 包装器）继续往旧流写，输出顺序
    会乱。

    Args:
        *streams: 要处理的流，默认 ``sys.stdout`` 与 ``sys.stderr``。显式传入是
            为了可测 —— 测试拿 ascii 编码的假流进来，断言不再抛异常。
    """
    for stream in streams or (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if stream.isatty():
                reconfigure(errors="replace")
            else:
                reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            continue
