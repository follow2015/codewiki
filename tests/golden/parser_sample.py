"""模块文档（黄金样本：改解析行为会让 digest 变化）。"""
import os
from typing import Any

MAX = 3


class Outer:
    class Inner:
        def deep(self) -> None:
            return None

    def run(self, x: int) -> int:
        return _helper(x)


def _helper(v: int) -> int:
    return v + MAX
