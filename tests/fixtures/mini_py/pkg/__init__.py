"""包的重导出口：对外只暴露 Svc（真实定义在 pkg.core）。"""

from pkg.core import Service as Svc

__all__ = ["Svc"]
