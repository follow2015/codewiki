"""与 b 互相 import（成环夹具）。"""

import b  # noqa: F401 - 刻意成环


def a_fn() -> int:
    return b.b_fn()
