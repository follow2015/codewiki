"""``codewiki init``：生成配置模板。

模板内容就是这个命令的全部价值，所以它跟命令放在一起 —— 改字段时只有一个地方
要动，不会出现"模板在 A、提示在 B"的漂移。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from codewiki.config import CONFIG_FILENAME

_INIT_TEMPLATE = """# codewiki 配置（由 `codewiki init` 生成）
# 完整字段见 codewiki.toml.example；编辑器补全用 wiki-output/data/codewiki.schema.json

[project]
name = "my-project"            # 产物标题

[analysis]
min_confidence = 0.5           # 低于该置信度的边不进结果
max_nodes = 300                # 宏观图单张节点上限（超出自动按层分包）

# 架构分层（按需增删；paths 是前缀匹配）
[[architecture.layers]]
name = "接口层"
paths = ["api/", "routes/"]

[[architecture.layers]]
name = "服务层"
paths = ["services/", "domain/"]

[[architecture.layers]]
name = "数据层"
paths = ["models/", "db/"]
"""


def init(args: argparse.Namespace) -> int:
    """生成 ``codewiki.toml`` 模板（配置错误提示里引用的就是本命令）。"""
    target = Path(args.path) / CONFIG_FILENAME
    if target.exists():
        print(f"error: {target} 已存在，不覆盖（要重生成请先删除）", file=sys.stderr)
        return 2
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_INIT_TEMPLATE, encoding="utf-8")
    print(f"已生成 {target}")
    print("下一步: codewiki analyze --src <仓库根> --out wiki-output")
    return 0
