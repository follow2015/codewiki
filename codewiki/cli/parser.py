"""命令行定义：只有参数、帮助文本与分发，没有任何业务逻辑。

每个子命令的 ``func=`` 都指向自己模块里的入口（``analyze_cmd.analyze`` 之类），
不是指向这里的转发壳。这一点很实际：测试要劫持某条路径时会 ``monkeypatch``
实现模块里的对象，多一层壳就意味着补丁打在壳上、断言永远不生效。

新增子命令的姿势：先写 ``xxx_cmd.py``（入口吃 ``argparse.Namespace``、返回退出
码），再来这里加一个 ``add_parser``。
"""

from __future__ import annotations

import argparse

from codewiki import __version__
from codewiki.config import CONFIG_FILENAME
from codewiki.layering import DEFAULT_MAX_GROUP

from .agent_cmd import agent
from .analyze_cmd import analyze
from .annotate_cmd import WRITE_BACK_POLICIES, annotate
from .cycles_cmd import cycles
from .diff_cmd import diff
from .doctor_cmd import doctor
from .export_cmd import export
from .import_graph_cmd import import_graph
from .init_cmd import init
from .merge_annotations_cmd import merge_annotations
from .merge_layers_cmd import merge_layers
from .serve_cmd import serve
from .suggest_layers_cmd import suggest_layers


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codewiki", description="代码架构知识图谱与文档生成器")
    parser.add_argument("--version", action="version", version=f"codewiki {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    init_cmd = sub.add_parser("init", help="生成 codewiki.toml（带 --src 时按真实目录与依赖起草分层）")
    init_cmd.add_argument("--path", default=".", help="生成位置（默认当前目录）")
    init_cmd.add_argument("--src", help="仓库根目录：给了就扫描并起草分层（不给则只吐模板）")
    init_cmd.add_argument("--force", action="store_true", help="允许覆盖已存在的配置")
    ai_group = init_cmd.add_mutually_exclusive_group()
    ai_group.add_argument(
        "--ai",
        dest="ai",
        action="store_true",
        default=None,
        help="强制让模型按职责归并（结果会逐条校验；缺模型配置时报错）",
    )
    ai_group.add_argument(
        "--no-ai",
        dest="ai",
        action="store_false",
        help="强制只用目录骨架（即使环境里配了 AI）",
    )
    init_cmd.add_argument("--model", help="模型名（也可用环境变量 OPENAI_MODEL）")
    init_cmd.add_argument("--base-url", help="OpenAI 兼容网关地址（也可用 OPENAI_BASE_URL）")
    init_cmd.add_argument("--deferred", help="外置推理目录：导出请求由 agent 作答，零凭据零网络")
    init_cmd.add_argument(
        "--max-group", type=int, default=None, help="单层模块数上限（默认按总量自动取，约总量的 1/3）"
    )
    init_cmd.add_argument("--exclude", action="append", help="追加跳过的目录名（可多次）")
    init_cmd.add_argument("--backend", default="auto", help="auto | ast | treesitter | regex")
    init_cmd.set_defaults(func=init)

    analyze_cmd = sub.add_parser("analyze", help="纯静态分析，产出标准数据包")
    analyze_cmd.add_argument("--src", default=".", help="待分析仓库根目录")
    analyze_cmd.add_argument("--out", default="wiki-output", help="产物输出目录")
    analyze_cmd.add_argument("--backend", default="auto", help="auto | ast | treesitter | regex")
    analyze_cmd.add_argument("--exclude", action="append", help="追加跳过的目录名（可多次）")
    analyze_cmd.add_argument("--config", help=f"配置路径（默认 <src>/{CONFIG_FILENAME}）")
    analyze_cmd.add_argument(
        "--trust-config",
        action="store_true",
        help="信任配置里的 [ai] base_url/model/write_back（默认忽略：该文件来自被分析仓库）",
    )
    analyze_cmd.add_argument("--offline-asset", help="离线看图用的 mermaid.min.js（默认取包内自带的那份）")
    analyze_cmd.add_argument(
        "--no-cache", action="store_true", help="跳过 FileModel 增量缓存（强制全量解析）"
    )
    analyze_cmd.add_argument(
        "--incremental",
        action="store_true",
        help="增量渲染：只重渲受影响模块（复用上一轮产物；无快照或配置变更时自动全量）",
    )
    analyze_cmd.add_argument(
        "--min-confidence",
        type=float,
        default=None,
        help="覆盖配置里的 analysis.min_confidence",
    )
    analyze_cmd.add_argument("--strict", action="store_true", help="存在 error 级诊断时以非 0 退出")
    analyze_cmd.add_argument(
        "--timestamp",
        help="固定 generated_at（ISO-8601 或整数秒）；也可用环境变量 SOURCE_DATE_EPOCH"
        " —— 两者都是为了让产物可复现、可逐字节比对",
    )
    analyze_cmd.set_defaults(func=analyze)

    export_cmd = sub.add_parser("export", help="把产物目录打成单个 Zip（原子化）")
    export_cmd.add_argument("--dir", default="wiki-output", help="产物目录")
    export_cmd.add_argument("--zip", dest="zip_path", default=None, help="输出 Zip 路径（默认 <目录>.zip）")
    export_cmd.set_defaults(func=export)

    doctor_cmd = sub.add_parser("doctor", help="环境自检（Python / 后端 / Node / 离线资源）")
    doctor_cmd.add_argument("--dir", default=None, help="顺带检查产物目录里的离线资源")
    doctor_cmd.add_argument(
        "--probe", action="store_true", help="连一次模型服务验证（会真实调用，可能产生费用）"
    )
    doctor_cmd.set_defaults(func=doctor)

    annotate_cmd = sub.add_parser("ai-annotate", help="AI 架构标注（独立命令，需模型环境）")
    annotate_cmd.add_argument(
        "--input", default="wiki-output/data/codewiki.json", help="analyze 产出的数据包"
    )
    annotate_cmd.add_argument(
        "--config", help=f"配置路径（默认 ./{CONFIG_FILENAME}）：注入分层与人工注解语境"
    )
    annotate_cmd.add_argument("--src", help="源码根目录（只读取证；不给则仅依据数据包事实）")
    annotate_cmd.add_argument("--out", help="建议输出目录（默认数据包所在产物根目录）")
    annotate_cmd.add_argument(
        "--cache-dir",
        help="AI 缓存根目录（默认 ~/.cache/codewiki/ai-cache；刻意不落在 --src 里，避免被审仓库预置缓存）",
    )
    annotate_cmd.add_argument("--model", help="模型名（优先于 OPENAI_MODEL 与配置）")
    annotate_cmd.add_argument("--base-url", help="覆盖 [ai] base_url（本地 Ollama / vLLM 内网网关）")
    annotate_cmd.add_argument(
        "--deferred",
        metavar="DIR",
        help="外置推理：不发网络请求、不需要 OPENAI_API_KEY。把待问内容写成 DIR/requests/*.json，"
        "由 agent 作答到 DIR/answers/ 后重跑本命令（还有待作答时退出码 3）",
    )
    annotate_cmd.add_argument(
        "--write-back", choices=sorted(WRITE_BACK_POLICIES), help="产出策略（默认 review-file）"
    )
    annotate_cmd.add_argument(
        "--trust-config",
        action="store_true",
        help="信任配置里的 [ai] base_url/model/write_back（默认忽略：该文件来自被分析仓库）",
    )
    annotate_cmd.add_argument("--top-n", type=int, help="标注前 N 个热点模块")
    annotate_cmd.add_argument("--budget", type=int, help="单轮 token 硬上限")
    annotate_cmd.set_defaults(func=annotate)

    merge_cmd = sub.add_parser("merge-annotations", help="把 AI 建议合并进 codewiki.toml")
    merge_cmd.add_argument("file", help="建议文件路径（suggestions/annotations.suggest.toml）")
    merge_cmd.add_argument("--config", help=f"主配置路径（默认 ./{CONFIG_FILENAME}）")
    merge_cmd.add_argument("--force", action="store_true", help="冲突时以建议为准（人工/已审阅注解仍受保护）")
    merge_cmd.add_argument("--dry-run", action="store_true", help="只打印合并计划，不写文件")
    merge_cmd.set_defaults(func=merge_annotations)

    cycles_cmd = sub.add_parser("cycles", help="依赖环检测（模块环 + 层级环）")
    cycles_cmd.add_argument("--input", default="wiki-output/data/codewiki.json")
    cycles_cmd.add_argument("--out", help="顺带把每个环写成 .mmd")
    cycles_cmd.set_defaults(func=cycles)

    diff_cmd = sub.add_parser("diff", help="变更影响分析（两个快照或 git 范围）")
    diff_cmd.add_argument("--base", help="基线数据包")
    diff_cmd.add_argument("--head", help="目标数据包")
    diff_cmd.add_argument("--input", default="wiki-output/data/codewiki.json", help="git 模式用的当前数据包")
    diff_cmd.add_argument("--src", default=".", help="git 模式用的仓库根")
    diff_cmd.add_argument("range", nargs="?", help="git 范围（如 v1.2.0..HEAD），不传则用 --base/--head")
    diff_cmd.set_defaults(func=diff)

    suggest = sub.add_parser("suggest-layers", help="目录优先 → 建议分层（纯静态）")
    suggest.add_argument("--input", default="wiki-output/data/codewiki.json")
    suggest.add_argument("--out", help="建议输出目录")
    suggest.add_argument(
        "--max-group",
        type=int,
        default=DEFAULT_MAX_GROUP,
        help=f"单层模块数上限，超过就继续按子目录下钻（默认 {DEFAULT_MAX_GROUP}）",
    )
    suggest.set_defaults(func=suggest_layers)

    merge_layers_cmd_parser = sub.add_parser("merge-layers", help="把建议分层合并进 codewiki.toml")
    merge_layers_cmd_parser.add_argument("file", help="建议文件路径（suggestions/suggested-layers.toml）")
    merge_layers_cmd_parser.add_argument("--config", help=f"主配置路径（默认 ./{CONFIG_FILENAME}）")
    merge_layers_cmd_parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写文件")
    merge_layers_cmd_parser.set_defaults(func=merge_layers)

    serve_cmd = sub.add_parser("serve", help="本地静态预览（纯静态目录，不执行脚本）")
    serve_cmd.add_argument("--dir", default="wiki-output", help="产物目录")
    serve_cmd.add_argument("--port", type=int, default=8000, help="端口（0 = 由系统分配）")
    serve_cmd.set_defaults(func=serve)

    import_graph_cmd = sub.add_parser("import-graph", help="导入外部图谱（目前支持 graphify）")
    import_graph_cmd.add_argument("--from", dest="source", default="graphify", help="来源格式")
    import_graph_cmd.add_argument(
        "--input", required=True, help="图谱文件路径（如 graphify-out/<日期>/graph.json）"
    )
    import_graph_cmd.add_argument("--out", default="wiki-output", help="产物输出目录")
    import_graph_cmd.set_defaults(func=import_graph)

    agent_cmd = sub.add_parser("agent", help="交互问答：模型调用只读工具取证后作答")
    agent_cmd.add_argument("question", nargs="?", help="问题；不给则从 stdin 读")
    agent_cmd.add_argument("--input", default="wiki-output/data/codewiki.json")
    agent_cmd.add_argument("--src", help="源码根目录（用于 read_file / search / shell）")
    agent_cmd.add_argument("--config", help=f"配置路径（默认 ./{CONFIG_FILENAME}）")
    agent_cmd.add_argument("--model", help="模型名（优先于 OPENAI_MODEL 与配置）")
    agent_cmd.add_argument("--base-url", help="覆盖 [ai] base_url")
    agent_cmd.add_argument(
        "--trust-config",
        action="store_true",
        help="信任配置里的 [ai] base_url/model/write_back（默认忽略：该文件来自被分析仓库）",
    )
    agent_cmd.add_argument("--max-rounds", type=int, default=10, help="最大取证轮次（防无限循环）")
    agent_cmd.add_argument(
        "--allow-shell",
        action="store_true",
        help="允许模型调用 execute_shell（只读白名单命令）。默认关闭",
    )
    agent_cmd.add_argument("--out", help="审计日志目录（默认与数据包同级的产物根目录）")
    agent_cmd.set_defaults(func=agent)

    return parser
