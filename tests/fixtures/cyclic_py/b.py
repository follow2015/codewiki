"""与 a 互相 import（成环夹具）。"""

import a  # noqa: F401 - 刻意成环


def b_fn() -> int:
    return 1
