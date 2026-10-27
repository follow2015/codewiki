"""重度重导出口：一跳（A）、别名（AliasB）、二跳（C）。"""

from pkg.a import A
from pkg.b import B as AliasB
from pkg.c import C

__all__ = ["A", "AliasB", "C"]
