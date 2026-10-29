"""``codewiki init``：生成配置。

两种口径，按有没有 ``--src`` 分开：

* 不带 ``--src`` —— 老行为，吐一份带示例层的模板。适合"先看看长什么样"；
* 带 ``--src`` —— **扫描真实仓库起草**：目录骨架 + 按依赖排层序 + 点名互相依赖的
  目录对（这些提示才是人工真正花时间的部分）。加 ``--ai`` 让模型按职责归并。

为什么值得做成命令：实测手写一份 8 层配置要 30 分钟、3 轮试错（第一版把
``config.py`` 归进"进程入口"，108 条 ``from extensions import db`` 立刻伪造出
"数据层→进程入口"，再经 DI 装配反连服务层，3 个环全是假的）。而完全不写交给兜底
推断会碎成 120 层。起草把这段压缩成"看一眼、改几个层名"。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from codewiki.ai.client import OpenAICompatClient, resolve_base_url, resolve_model
from codewiki.ai.deferred import DEFAULT_MODEL, DeferredClient, render_pending_report
from codewiki.ai.draft import ai_draft
from codewiki.config import CONFIG_FILENAME
from codewiki.graph import build_graph
from codewiki.layering_draft import directory_draft, render_config_toml
from codewiki.models import EdgeType
from codewiki.repo import iter_source_files
from codewiki.tsaliases import detect_aliases

EXIT_PENDING = 3

_MODULE_EDGE_TYPES = frozenset({EdgeType.IMPORTS, EdgeType.CALLS, EdgeType.USES_DYNAMIC})

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


def _write_template(target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_INIT_TEMPLATE, encoding="utf-8")
    print(f"已生成 {target}（模板）")
    print("下一步: codewiki analyze --src <仓库根> --out wiki-output")
    print("提示: `codewiki init --src <仓库根>` 会按真实目录与依赖起草分层，不用手写")
    return 0


def _scan(
    root: Path,
    *,
    backend: str,
    excludes: tuple[str, ...],
    registry=None,  # noqa: ANN001 - 测试可注入只支持部分语言的注册表
) -> tuple[list[str], list[tuple[str, str]], list[str], dict[str, int]]:
    """扫描仓库，返回 ``(模块路径, 模块间依赖边, 实际用到的语言, 缺后端的语言计数)``。

    最后一个返回值不能省：没装 ``codewiki[treesitter]`` 时 TS/JS 会**整片被跳过**，
    起草结果里就没有前端层 —— 而这看起来跟"仓库本来没有前端"一模一样。实测 CI
    （刻意不装 treesitter）里 1322 个模块只剩 700 个。

    只取**有语言的源文件**：``iter_source_files`` 会把 ``.DS_Store``、``README.md``、
    ``Makefile`` 这类也收进来（``language_of`` 给它们 ``other``），对 analyze 无害 ——
    它们只是资源节点；但对起草是灾难：实测 ``root`` 层里 27 个"模块"有 21 个是
    这类文件，人第一眼就不信这份配置。

    刻意不接增量缓存：起草是**一次性**动作，为它维护缓存只会给"改了配置重跑却没
    生效"这类问题多一个来源。
    """
    from codewiki.analyzers.base import build_registry
    from codewiki.repo import EXT_LANGUAGE

    registry = registry if registry is not None else build_registry()
    models = []
    languages: set[str] = set()
    missing: dict[str, int] = {}
    for source in iter_source_files(
        root, extra_excludes=excludes, languages=tuple(sorted(set(EXT_LANGUAGE.values())))
    ):
        try:
            analyzer = registry.get(source.language, backend)
        except KeyError:
            missing[source.language] = missing.get(source.language, 0) + 1
            continue
        try:
            models.append(analyzer.parse(Path(source.path), source.content))
        except Exception:  # noqa: BLE001 - 单文件解析失败不该拦住起草
            continue
        languages.add(source.language)
    if not models:
        return [], [], [], missing

    result = build_graph(models, layers=[], root_package=root.name)
    deps = [
        (edge.source.removeprefix("mod::"), edge.target.removeprefix("mod::"))
        for edge in result.edges
        if edge.type in _MODULE_EDGE_TYPES
        and edge.source.startswith("mod::")
        and edge.target.startswith("mod::")
    ]
    return [model.path for model in models], deps, sorted(languages), missing


def _client_for(args: argparse.Namespace):  # noqa: ANN202 - OpenAICompatClient | DeferredClient
    """按 ``--deferred`` / ``--model`` 装配 client；缺参数时返回 ``(None, 错误文案)``。"""
    deferred_root = Path(args.deferred).resolve() if args.deferred else None
    model = resolve_model(cli_value=args.model, config_value=None)
    if not model and deferred_root is not None:
        model = DEFAULT_MODEL
    if not model:
        return None, (
            "起草需要模型：用 --model 或环境变量 OPENAI_MODEL 指定；"
            "没有凭据时用 --deferred <目录>（外置推理，零网络）"
        )
    if deferred_root is not None:
        return DeferredClient(deferred_root, model=model, purpose="draft"), ""
    base_url, warning = resolve_base_url(cli_value=args.base_url, config_value=None)
    if warning:
        print(f"warning: {warning}", file=sys.stderr)
    return OpenAICompatClient(model=model, base_url=base_url), ""


def init(args: argparse.Namespace) -> int:
    """生成 ``codewiki.toml``（不带 ``--src`` 时吐模板，带则按真实仓库起草）。"""
    target = Path(args.path) / CONFIG_FILENAME
    if target.exists() and not args.force:
        print(f"error: {target} 已存在，不覆盖（要重生成请加 --force 或先删除）", file=sys.stderr)
        return 2

    if not args.src:
        return _write_template(target)

    root = Path(args.src).resolve()
    if not root.is_dir():
        print(f"error: --src 不是目录: {root}", file=sys.stderr)
        return 2

    paths, deps, languages, missing = _scan(root, backend=args.backend, excludes=tuple(args.exclude or ()))
    if missing:
        detail = "、".join(f"{lang} {count} 个" for lang, count in sorted(missing.items()))
        print(
            f"warning: 缺少解析后端，已跳过 {detail} 文件 —— 起草结果会缺这些语言的层。"
            "装依赖后重跑：pip install 'codewiki[treesitter]'",
            file=sys.stderr,
        )
    if not paths:
        print(f"error: {root} 里没有可分析的源文件（检查 --exclude 或语言支持）", file=sys.stderr)
        return 2

    use_ai = args.ai is not False
    client = None
    if use_ai:
        client, error = _client_for(args)
        if client is None:
            if args.ai:
                print(f"error: {error}", file=sys.stderr)
                return 2
            use_ai = False
    if use_ai:
        draft = ai_draft(paths, deps=deps, client=client)
        if isinstance(client, DeferredClient) and client.misses:
            print(render_pending_report(client.misses, root=client.root), file=sys.stderr)
            return EXIT_PENDING
    else:
        draft = directory_draft(paths, deps=deps, max_group=args.max_group)

    if not draft.layers:
        print(
            f"error: 没能起草出分层（{len(paths)} 个模块全在一个目录里？）—— "
            "退回模板：codewiki init（不带 --src）",
            file=sys.stderr,
        )
        return 2

    aliases = detect_aliases(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        render_config_toml(
            draft,
            project_name=root.name,
            excludes=list(args.exclude or ()),
            ts_aliases=aliases,
            languages=languages,
        ),
        encoding="utf-8",
    )

    print(f"已起草 {target}：{len(draft.layers)} 层 / {len(paths)} 个模块（来源: {draft.source}）")
    for layer in sorted(draft.layers, key=lambda item: (item.order or 0, item.name)):
        print(f"  order={layer.order}  {layer.name:24s} {layer.modules:4d} 模块  {layer.paths}")
    if aliases:
        pairs = "、".join(f"{key} → {value}" for key, value in sorted(aliases.items()))
        print(f"从 tsconfig 推断别名: {pairs}")
    notes = draft.render_notes()
    if notes:
        print()
        print(notes)
    print()
    print("下一步: codewiki analyze --src . --out wiki-output")
    print("改完层名/合并同职责层后，重跑 analyze 即可；不需要重跑本命令。")
    return 0
