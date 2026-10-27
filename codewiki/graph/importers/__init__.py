"""外部图谱导入适配器（§4.6.4）。

只放进得来、走得掉的格式：读外部产物 → 统一数据包；不 import 对方的代码，
也不把我们的产物写回去。
"""

from __future__ import annotations

__all__ = ["graphify"]
