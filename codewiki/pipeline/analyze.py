"""``analyze`` 流水线：从仓库到产物（设计文稿 §目标架构 · pipeline/ 负责编排）。

这里原本是 ``cli.py`` 里那个 230 行的 ``_analyze``。搬出来不是为了好看：它当时
混了三件事 —— 解析命令行给定值、编排分析与渲染、以及把结果打印成人看的摘要。
混在一起的直接后果是**规则只能靠端到端验证**（构造一个仓库、跑完整条流水线、
再看产物像不像对的），而增量判据那种"给定上一轮和本轮输入，返回重渲范围"的
纯逻辑根本没法单独喂输入。

现在入口是 :func:`run` + :class:`AnalyzeRequest`：请求对象里没有 ``argparse``
的任何痕迹，测试可以只构造关心的那几项。命令层（``cli/analyze_cmd.py``）只做
"Namespace → 请求对象"的映射。

行为与拆分前逐字节一致（``print`` 的文案、退出码的取值都没动）：这是渐进迁移
的前提，否则"产物相同"这条验收会被无关的输出变化淹没。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from codewiki import clock
from codewiki.analyzers import AnalyzerRegistry, build_registry, format_version_of
from codewiki.clock import ClockError
from codewiki.config import CONFIG_FILENAME, ConfigError, LayerSpec, derive_layer_specs
from codewiki.file_cache import CacheStats, FileModelCache, content_hash_of, mtime_ns_of
from codewiki.graph import build_graph
from codewiki.graph.analysis import diff_packets, god_node_diagnostics, module_graph_from_parts
from codewiki.graph.index import build_index
from codewiki.layering import rank_layers
from codewiki.models import FileModel, NodeType
from codewiki.packetio import load_packet_or_none, packet_from_parts
from codewiki.pipeline.incremental import (
    INCOMPLETE_MARKER,
    changed_components,
    clear_incomplete_marker,
    has_incomplete_marker,
    render_inputs,
    write_incomplete_marker,
)
from codewiki.render.diagnostics_md import write_diagnostics_md
from codewiki.render.diagrams import render_all
from codewiki.render.jsonio import write_packet
from codewiki.render.site import (
    INDEX_HTML,
    asset_diagnostics,
    copy_offline_asset,
    prune_stale_pages,
    render_index_html,
    write_module_html,
)
from codewiki.render.wiki import INDEX_FILENAME, MODULES_SUBDIR, render_index, render_module_page
from codewiki.repo import iter_source_files, repo_commit
from codewiki.schemas import DataPacket, Diagnostic
from codewiki.trust import load_trusted_config


@dataclass(frozen=True)
class AnalyzeRequest:
    """一次分析的全部输入（无 argparse 依赖，可直接构造）。

    字段名刻意贴近命令行参数：迁移期的 review 要能一眼对上 ``--min-confidence``
    这类开关，少一层心智翻译。
    """

    src: str | Path
    out: str | Path
    backend: str = "auto"
    exclude: tuple[str, ...] = ()
    config: str | Path | None = None
    trust_config: bool = False
    offline_asset: str | Path | None = None
    no_cache: bool = False
    incremental: bool = False
    min_confidence: float | None = None
    strict: bool = False
    timestamp: str | None = None
    registry: AnalyzerRegistry | None = None


def _backend_of(analyzer) -> str:  # noqa: ANN001
    """实际生效的后端名（``auto`` 要解析到真身），用于缓存键与诊断。

    各后端模块都声明了模块级 ``BACKEND``；取不到就退回类名（宁可缓存永不命中，
    也不要让两个后端共用一份缓存）。
    """
    module = sys.modules.get(type(analyzer).__module__)
    return str(getattr(module, "BACKEND", type(analyzer).__name__))


def _order_by_dependencies(layers: list[LayerSpec], result) -> list[LayerSpec]:
    """给"按目录推断出来"的层补 order：按跨层依赖排（1 = 最上层）。

    目录只回答"谁和谁是一组"，不回答"谁在上"——后者得看依赖方向。不补这一步，
    层图只能按模块数从多到少排，读起来像随手排的，也就丢了分层图最重要的信息。
    排序算法与 ``suggest-layers`` 是同一份（:func:`codewiki.layering.rank_layers`）。
    """
    graph = module_graph_from_parts(result.nodes, result.edges)
    index_of = {layer.id: position for position, layer in enumerate(layers)}
    assignment = {
        node.id: index_of[node.layer_id]
        for node in result.nodes
        if node.type == NodeType.MODULE and node.layer_id in index_of
    }
    order_of, _cyclic = rank_layers(assignment, graph, len(layers))
    return [layer.model_copy(update={"order": order_of[position]}) for position, layer in enumerate(layers)]


def run(request: AnalyzeRequest) -> int:
    """执行一次分析，返回进程退出码（0 成功 / 1 运行失败 / 2 用法错误）。"""
    root = Path(request.src).resolve()
    out_dir = Path(request.out).resolve()
    if not root.is_dir():
        print(f"error: --src 不是目录: {root}", file=sys.stderr)
        return 2

    try:
        generated_at = clock.resolve(request.timestamp)
    except ClockError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    config_path = Path(request.config) if request.config else root / CONFIG_FILENAME
    try:
        cfg = load_trusted_config(config_path, trusted=request.trust_config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    registry = request.registry if request.registry is not None else build_registry()
    excludes = tuple([*request.exclude, *cfg.analysis.exclude])
    languages = tuple(cfg.languages) if cfg.languages else None
    sources = iter_source_files(root, extra_excludes=excludes, languages=languages)

    cache = None if request.no_cache else FileModelCache(root)
    stats = CacheStats()
    models: list[FileModel] = []
    backend_missing: list[tuple[str, str]] = []
    parse_failed: list[tuple[str, str]] = []
    for source in sources:
        try:
            analyzer = registry.get(source.language, request.backend)
        except KeyError:
            backend_missing.append((source.path, source.language))
            continue
        key = (
            FileModelCache.key(
                source.path,
                mtime_ns_of(root / source.path),
                content_hash_of(source.content),
                _backend_of(analyzer),
                parser_version=format_version_of(_backend_of(analyzer)),
            )
            if cache
            else None
        )
        cached = cache.get(key) if cache and key else None
        if cached is not None:
            stats.hit += 1
            models.append(cached)
            continue
        stats.miss += 1
        try:
            model = analyzer.parse(Path(source.path), source.content)
        except Exception as exc:  # noqa: BLE001 - 单文件解析失败不该拖垮整轮
            stats.failed += 1
            if len(parse_failed) < 20:
                parse_failed.append((source.path, f"{type(exc).__name__}: {exc}"))
            continue
        if cache and key:
            cache.set(key, model)
        models.append(model)

    derived_layers: list[LayerSpec] = []
    if not cfg.architecture.layers and cfg.architecture.auto_layers:
        derived_layers = derive_layer_specs([model.path for model in models])
        if derived_layers:
            cfg = cfg.model_copy(
                update={"architecture": cfg.architecture.model_copy(update={"layers": derived_layers})}
            )
    layers_cfg = [
        {"id": layer.id, "name": layer.name, "paths": layer.paths} for layer in cfg.architecture.layers
    ]
    min_confidence = (
        request.min_confidence if request.min_confidence is not None else cfg.analysis.min_confidence
    )
    result = build_graph(
        models,
        min_confidence=min_confidence,
        layers=layers_cfg,
        root_package=root.name,
        ts_aliases=cfg.analysis.ts_aliases,
    )
    if derived_layers:
        cfg = cfg.model_copy(
            update={
                "architecture": cfg.architecture.model_copy(
                    update={"layers": _order_by_dependencies(cfg.architecture.layers, result)}
                )
            }
        )
    diagnostics = list(result.diagnostics)
    if backend_missing:
        langs = sorted({lang for _, lang in backend_missing})
        sample = ", ".join(p for p, _ in backend_missing[:5])
        diagnostics.append(
            Diagnostic(
                level="warning",
                code="BACKEND_MISSING",
                message=(
                    f"{len(backend_missing)} 个文件的语言没有可用分析后端（{', '.join(langs)}），"
                    f"未进图谱: {sample}{'…' if len(backend_missing) > 5 else ''}"
                ),
            )
        )
    for path, err in parse_failed:
        diagnostics.append(
            Diagnostic(level="warning", code="PARSE_FAILED", message=f"解析失败: {err}", where=path)
        )
    if len(parse_failed) == 20:
        diagnostics.append(
            Diagnostic(level="info", code="PARSE_FAILED", message="解析失败文件超过 20 个，其余仅计入统计")
        )

    if not config_path.exists():
        fallback = (
            "分层已按目录结构推断（见 LAYERS_DERIVED）"
            if derived_layers
            else "全部模块归入虚拟层 _unlayered，分层图退化为单图"
        )
        diagnostics.append(
            Diagnostic(level="info", code="CONFIG_MISSING", message=f"未提供 {CONFIG_FILENAME}：{fallback}")
        )
    if derived_layers:
        ordered = sorted(cfg.architecture.layers, key=lambda layer: layer.order or 0)
        names = ", ".join(f"{layer.name}({layer.order})" for layer in ordered[:8])
        more = "…" if len(ordered) > 8 else ""
        diagnostics.append(
            Diagnostic(
                level="info",
                code="LAYERS_DERIVED",
                message=(
                    f"未声明分层，已按目录结构推断 {len(ordered)} 层，并按跨层依赖排出层序"
                    f"（{names}{more}；order 1 = 最上层）。目录归类不等于架构意图："
                    "可运行 suggest-layers 拿到带内聚度与环标注的建议，"
                    "或在 codewiki.toml 里手写 [[architecture.layers]]（architecture.auto_layers = false 可关闭推断）"
                ),
            )
        )

    if stats.total:
        if cache is not None:
            print(
                f"FileModel 缓存：命中 {stats.hit} / 重新解析 {stats.miss} / 解析失败 {stats.failed}"
                + ("" if cache.enabled else "（缓存目录不可写，已降级为无缓存）")
            )
        else:
            print(f"解析统计（--no-cache）：解析 {stats.miss} / 失败 {stats.failed}")

    packet = packet_from_parts(
        title=cfg.title if cfg.project.name else root.name,
        layers=cfg.layers_as_nodes(),
        nodes=result.nodes,
        edges=result.edges,
        annotations=_annotations_from_config(cfg),
        diagnostics=diagnostics,
        provenance=_collect_provenance(models),
        repo_root=str(root),
        repo_commit=repo_commit(root),
        generated_at=generated_at,
    )
    god = god_node_diagnostics(
        packet,
        top_n=cfg.analysis.god_node_top_n,
        cross_layer_ratio=cfg.analysis.god_node_cross_layer_ratio,
    )
    packet.diagnostics.extend(god)
    diagnostics.extend(god)

    previous_snapshot = _read_previous_snapshot(out_dir) if request.incremental else None

    inputs = render_inputs(
        cfg=cfg,
        packet=packet,
        min_confidence=min_confidence,
        backends=_collect_provenance(models),
    )
    packet.meta.render_inputs = inputs

    only_modules, incremental_note = _incremental_scope(
        packet,
        enabled=request.incremental,
        previous=previous_snapshot,
        inputs=inputs,
        interrupted=has_incomplete_marker(out_dir),
    )
    if incremental_note:
        print(incremental_note)

    write_incomplete_marker(out_dir)
    packet_path = write_packet(packet, out_dir)

    diagrams = render_all(
        packet,
        out_dir,
        min_confidence=min_confidence,
        max_nodes=cfg.analysis.max_nodes,
        only_modules=only_modules,
    )
    diagnostics.extend(diagrams.diagnostics)
    if any(d.level == "error" for d in diagrams.diagnostics):
        packet_path.write_text(
            json.dumps(_packet_with(packet, diagnostics), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        write_diagnostics_md(out_dir, diagnostics)
        print("出图失败（图未通过 L1 校验），已放弃出图", file=sys.stderr)
        return 1

    _write_wiki(packet, out_dir, diagrams_dir=out_dir / "diagrams", only_modules=only_modules)

    copy_offline_asset(out_dir, source=Path(request.offline_asset) if request.offline_asset else None)
    diagnostics.extend(asset_diagnostics(out_dir))
    write_diagnostics_md(out_dir, diagnostics)
    (out_dir / INDEX_HTML).write_text(render_index_html(packet), encoding="utf-8")
    write_module_html(out_dir)

    clear_incomplete_marker(out_dir)

    print(
        f"分析完成：{len(models)} 个文件 / {len(result.nodes)} 节点 / {len(result.edges)} 边 / "
        f"{len(diagrams.files)} 张图"
    )
    print(f"数据包：{packet_path}")
    errors = [d for d in diagnostics if d.level == "error"]
    if errors and request.strict:
        print(f"--strict：存在 {len(errors)} 条 error 级诊断", file=sys.stderr)
        return 1
    return 0


def _incremental_scope(
    packet,
    *,
    enabled: bool,
    previous: DataPacket | None,
    inputs: dict[str, str],
    interrupted: bool,
):
    """算出本轮需要重渲的模块集合；返回 ``(集合|None, 说明|None)``。

    ``None`` 表示"不做增量"（全量渲染）。每个"不做"的理由都要说出来 —— 否则
    "开了增量却没生效"会变成又一个静默问题；理由还必须**点名是哪一项变了**，
    只说"输入变了"等于让用户去猜。

    说明**进 stdout，不进数据包**：数据包是归档物，描述的是"仓库长什么样"，
    不描述"这一轮是怎么跑的"。把它写进 packet.diagnostics 会让"同一份输入的
    增量产物"永远比全量产物多一条诊断 —— 逐字节比对这条验收就废了（`CACHE_STATS`
    踩过同一个坑）。

    判据本身（分量表 / 比较 / 标记）在 :mod:`codewiki.pipeline.incremental`，
    这里只做编排：读上一轮、给出结论。
    """
    if not enabled:
        return None, None

    if interrupted:
        return None, (
            f"上一轮渲染未完成（{INCOMPLETE_MARKER} 仍在产物目录）⇒ 本轮全量重渲，不复用可能残缺的页面"
        )

    if previous is None:
        return None, "增量已开启但无上一轮快照（首次运行）⇒ 本轮全量渲染"

    if not previous.meta.render_inputs:
        return None, "上一轮快照没有渲染指纹（旧版本产物）⇒ 本轮全量渲染，本轮起可增量"

    changed = changed_components(previous.meta.render_inputs, inputs)
    if changed:
        return None, f"渲染输入相对上一轮有变化（{'、'.join(changed)}）⇒ 本轮全量渲染"

    impact = diff_packets(previous, packet)
    scope = set(impact.changed) | set(impact.added) | set(impact.direct) | set(impact.indirect)
    reused = sum(1 for n in packet.nodes if n.type.value == "Module" and n.id not in scope)
    return scope, f"增量渲染：重渲 {len(scope)} 个模块 / 复用 {reused} 个（改动 {len(impact.changed)}）"


def _read_previous_snapshot(out_dir: Path):
    """读上一轮产物（供增量比较）；不存在 / 损坏 / 版本不兼容都返回 ``None``。

    "读不了就当没有" 在这里是**正确**选择：增量复用旧页面要求确实理解上一轮，
    读不懂就全量重渲（宁可多渲，不可拿旧结论当新结论）。
    """
    return load_packet_or_none(out_dir / "data" / "codewiki.json")


def _write_wiki(packet, out_dir: Path, *, diagrams_dir: Path, only_modules: set[str] | None = None) -> None:
    """写 index.md 与 modules/*.md（图来自已落盘的 .mmd，点与图同源）。"""
    modules_dir = out_dir / MODULES_SUBDIR
    modules_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / INDEX_FILENAME).write_text(render_index(packet), encoding="utf-8")

    from codewiki.models import NodeType
    from codewiki.render.diagrams import build_slug_map

    slug_map = build_slug_map([n.path for n in packet.nodes if n.type == NodeType.MODULE and n.path])

    index = build_index(packet)
    for node in packet.nodes:
        if node.type.value != "Module" or not node.path:
            continue
        page_path = modules_dir / f"{slug_map[node.path]}.md"
        if only_modules is not None and node.id not in only_modules and page_path.exists():
            continue
        diagram_path = diagrams_dir / "modules" / f"{slug_map[node.path]}.mmd"
        diagram_text = diagram_path.read_text(encoding="utf-8") if diagram_path.exists() else None
        page = render_module_page(packet, node.id, diagram_text=diagram_text, index=index)
        (modules_dir / f"{slug_map[node.path]}.md").write_text(page, encoding="utf-8")

    prune_stale_pages(out_dir, set(slug_map.values()))


def _packet_with(packet, diagnostics):
    """替换诊断后返回可序列化的字典（中断路径下也要留下完整诊断）。"""
    data = packet.model_dump(mode="json")
    data["diagnostics"] = [d.model_dump(mode="json") for d in diagnostics]
    return data


def _annotations_from_config(cfg):
    """配置注解进数据包（人工与已合并的 AI 建议都走这里，§3.1）。

    ``source`` / ``reviewed`` **照抄配置**，不自作主张改写：人工写的如果没标
    reviewed=true，产物里就该是未审阅 —— 替用户点头等于绕过人工过滤。
    """
    from codewiki.schemas import Annotation

    return [
        Annotation(
            target=a.target,
            label=a.label,
            note=a.note,
            source=a.source,
            reviewed=a.reviewed,
        )
        for a in cfg.annotations
    ]


def _collect_provenance(models: list[FileModel]) -> dict:
    out = {}
    for model in models:
        if model.provenance:
            out[model.provenance.backend] = model.provenance
    return out


__all__ = ["AnalyzeRequest", "run"]
