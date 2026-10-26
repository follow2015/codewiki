"""AI 层安全边界（设计文档《codewiki 重构与优化设计文档》§AI 层安全模型）。

这个包存在的理由，是把"模型能读到什么、能执行什么"从**分散在调用点的判断**
收敛成**一个包里的一条规则**：

======  ==========================================  ==========================
组件    职责                                          取代
======  ==========================================  ==========================
paths   ``resolve()`` 后必须在仓库内                     tools.resolve_in_repo /
                                                        sandbox._jail_check / 搜索逐文件 resolve
secrets 密钥文件判定 + 两个脱敏档位                      tools._is_secret_file /
                                                        audit.redact（一个函数兼两职）
runner  项目里唯一启动子进程的入口                       tools / sandbox / doctor /
        （实现落在 ``codewiki/subproc.py``，见其 docstring）  repo / mermaid_validator 各自 run()
======  ==========================================  ==========================

设计约束（与文档一致）：``guard`` 只依赖标准库。它是最底层，导入它不会引入
任何上层包，也就不可能出现 ``guard → tools → guard`` 的环。
"""

from __future__ import annotations

from codewiki.ai.guard.paths import PathDenied, relative_to_root, resolve_in_root
from codewiki.ai.guard.secrets import (
    EXCLUDE_GLOBS,
    is_secret_file,
    redact_audit,
    redact_model,
)

from codewiki.subproc import Completed, minimal_env, run, run_isolated_python

__all__ = [
    "EXCLUDE_GLOBS",
    "Completed",
    "PathDenied",
    "is_secret_file",
    "minimal_env",
    "redact_audit",
    "redact_model",
    "relative_to_root",
    "resolve_in_root",
    "run",
    "run_isolated_python",
]
