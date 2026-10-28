"""AI 增强层（§五）—— **可插拔**：整段缺失等同关闭，纯静态链路不依赖它。

模块地图（与 §九 目录结构一致）：

* :mod:`codewiki.ai.client` —— OpenAI 兼容客户端（零依赖 urllib）；
* :mod:`codewiki.ai.deferred` —— 外置推理：由 agent 作答，零凭据、零网络；
* :mod:`codewiki.ai.agent` —— Thought-Action-Observation 问答循环；
* :mod:`codewiki.ai.overview` —— 开局项目概览（分层 / 热点 / 已审阅注解）；
* :mod:`codewiki.ai.graph_tools` —— 图谱发现工具：list_modules / find_symbol；
* :mod:`codewiki.ai.citations` —— 答案里 ``file:line`` 引用的存在性核对；
* :mod:`codewiki.ai.sandbox` —— 只读 shell 白名单沙箱（六层防御）；
* :mod:`codewiki.ai.tools` —— read_file / search_codebase / query_graph；
* :mod:`codewiki.ai.cache` —— 结果缓存与 token 预算；
* :mod:`codewiki.ai.annotate` —— ``ai-annotate``；
* :mod:`codewiki.ai.merge` —— ``merge-annotations``；
* :mod:`codewiki.ai.diagram` —— AI 时序图 + 坏图零落盘回退；
* :mod:`codewiki.ai.audit` —— JSONL 审计（脱敏）。
"""

from __future__ import annotations

__all__ = [
    "agent",
    "annotate",
    "audit",
    "cache",
    "citations",
    "client",
    "deferred",
    "diagram",
    "graph_tools",
    "merge",
    "overview",
    "prompts",
    "registry",
    "sandbox",
    "tools",
]
