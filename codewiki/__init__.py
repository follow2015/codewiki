"""codewiki —— 配置驱动、可离线交付的代码架构知识图谱与文档生成器。

分层（设计文稿 §一）：Infrastructure → Core Engine → Render & Export → AI（可选）。
一期先落前三层（纯静态、零网络依赖），AI 层在阶段二以独立命令接入。
"""
from __future__ import annotations

__version__ = "0.1.0"
