"""SCIP 高精度后端（可选，§4.6.2）。

分层：

* :mod:`pb` —— 极简 protobuf 编解码（零依赖，够读 SCIP 结构）；
* :mod:`mapper` —— ``.scip`` → ``FileModel`` 映射（**准入以样本测试全绿为准**）；
* :mod:`mock` —— 不依赖索引器的固定样本（阶段一要求：占位、不进主干关键路径）。

刻意不引入 PyPI 上的 ``scip`` 包：那是细胞图像处理库（GPL），与 SCIP 代码索引
格式无关（设计 v2.5 已订正）。
"""
from __future__ import annotations

__all__ = ["mapper", "mock", "pb"]
