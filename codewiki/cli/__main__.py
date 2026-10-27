"""``python -m codewiki.cli`` 的入口。

``bench.py`` 与 CI 都是这个姿势，包化之后必须有 ``__main__.py``，否则 ``-m``
只会报"No module named codewiki.cli.__main__"。
"""

from __future__ import annotations

from . import main

if __name__ == "__main__":
    raise SystemExit(main())
