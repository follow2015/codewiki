"""CLI 入口（设计文稿 §六）。

已落：``analyze``（纯静态、零网络）、``doctor``、``export``、``ai-annotate``、
``merge-annotations``。其余命令随阶段推进补齐，命令表与参数口径见设计文稿。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from codewiki import __version__
from codewiki.ai.agent import AgentLoop
from codewiki.ai.annotate import annotate_modules, write_suggestions
from codewiki.ai.audit import AUDIT_FILENAME, AuditLog
from codewiki.ai.client import OpenAICompatClient
from codewiki.ai.merge import merge_annotations, render_merge_report
from codewiki.analyzers import REGISTRY, register_defaults
from codewiki.config import CONFIG_FILENAME, ConfigError, load_config
from codewiki.doctor import render_report, run_checks
from codewiki.file_cache import CacheStats, FileModelCache, content_hash_of, mtime_ns_of
from codewiki.graph import build_graph
from codewiki.graph.analysis import (
    cycles_to_diagnostics,
    diff_packets,
    find_layer_cycles,
    find_module_cycles,
    god_node_diagnostics,
    impact_of,
)
from codewiki.graph.index import build_index
from codewiki.graph.merge_layers import merge_layers, render_layer_merge_report
from codewiki.graph.suggest_layers import (
    SUGGESTED_LAYERS_FILENAME,
    render_suggested_layers,
    suggest_layers,
)
from codewiki.models import FileModel
from codewiki.render.diagrams import render_all
from codewiki.render.jsonio import packet_from_parts, write_packet
from codewiki.render.mermaid_cycle import render_all_cycles
from codewiki.render.site import (
    INDEX_HTML,
    asset_diagnostics,
    copy_offline_asset,
    prune_stale_pages,
    render_index_html,
    serve_preview,
    write_module_html,
    write_zip,
)
from codewiki.render.wiki import INDEX_FILENAME, MODULES_SUBDIR, render_index, render_module_page
from codewiki.repo import iter_source_files, repo_commit
from codewiki.schemas import DataPacket, Diagnostic


def _analyze(args: argparse.Namespace) -> int:
    root = Path(args.src).resolve()
    out_dir = Path(args.out).resolve()
    if not root.is_dir():
        print(f"error: --src 不是目录: {root}", file=sys.stderr)
        return 2

    config_path = Path(args.config) if args.config else root / CONFIG_FILENAME
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    register_defaults()
    excludes = tuple([*(args.exclude or []), *cfg.analysis.exclude])
    languages = tuple(cfg.languages) if cfg.languages else None
    sources = iter_source_files(root, extra_excludes=excludes, languages=languages)

    cache = None if args.no_cache else FileModelCache(root)
    stats = CacheStats()
    models: list[FileModel] = []
    backend_missing: list[tuple[str, str]] = []
    parse_failed: list[tuple[str, str]] = []
    for source in sources:
        try:
            analyzer = REGISTRY.get(source.language, args.backend)
        except KeyError:
            backend_missing.append((source.path, source.language))
            continue
        key = (
            FileModelCache.key(
                source.path, mtime_ns_of(root / source.path), content_hash_of(source.content)
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

    layers_cfg = [
        {"id": layer.id, "name": layer.name, "paths": layer.paths}
        for layer in cfg.architecture.layers
    ]
    result = build_graph(
        models,
        min_confidence=args.min_confidence
        if args.min_confidence is not None
        else cfg.analysis.min_confidence,
        layers=layers_cfg,
        root_package=root.name,
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
        diagnostics.append(
            Diagnostic(
                level="info",
                code="CONFIG_MISSING",
                message=f"未提供 {CONFIG_FILENAME}：全部模块归入虚拟层 _unlayered，分层图退化为单图",
            )
        )

    if stats.total:
        if cache is not None:
            diagnostics.append(
                Diagnostic(
                    level="info",
                    code="CACHE_STATS",
                    message=f"FileModel 缓存：命中 {stats.hit} / 重新解析 {stats.miss} / 解析失败 {stats.failed}"
                    + ("" if cache.enabled else "（缓存目录不可写，已降级为无缓存）"),
                )
            )
        else:
            diagnostics.append(
                Diagnostic(
                    level="info",
                    code="CACHE_STATS",
                    message=f"解析统计（--no-cache）：解析 {stats.miss} / 失败 {stats.failed}",
                )
            )

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
    )
    god = god_node_diagnostics(
        packet,
        top_n=cfg.analysis.god_node_top_n,
        cross_layer_ratio=cfg.analysis.god_node_cross_layer_ratio,
    )
    packet.diagnostics.extend(god)
    diagnostics.extend(god)

    previous_snapshot = _read_previous_snapshot(out_dir) if args.incremental else None

    only_modules, incremental_note = _incremental_scope(
        packet, cfg, enabled=args.incremental, previous=previous_snapshot
    )
    if incremental_note:
        packet.diagnostics.append(incremental_note)
        diagnostics.append(incremental_note)

    packet_path = write_packet(packet, out_dir)

    diagrams = render_all(
        packet,
        out_dir,
        min_confidence=cfg.analysis.min_confidence,
        max_nodes=cfg.analysis.max_nodes,
        only_modules=only_modules,
    )
    diagnostics.extend(diagrams.diagnostics)
    if any(d.level == "error" for d in diagrams.diagnostics):
        packet_path.write_text(
            json.dumps(_packet_with(packet, diagnostics), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _write_diagnostics_md(out_dir, diagnostics)
        print("出图失败（图未通过 L1 校验），已放弃出图", file=sys.stderr)
        return 1

    _write_wiki(packet, out_dir, diagrams_dir=out_dir / "diagrams", only_modules=only_modules)

    copy_offline_asset(out_dir, source=Path(args.offline_asset) if args.offline_asset else None)
    diagnostics.extend(asset_diagnostics(out_dir))
    _write_diagnostics_md(out_dir, diagnostics)
    (out_dir / INDEX_HTML).write_text(render_index_html(packet), encoding="utf-8")
    write_module_html(out_dir)

    print(
        f"分析完成：{len(models)} 个文件 / {len(result.nodes)} 节点 / {len(result.edges)} 边 / "
        f"{len(diagrams.files)} 张图"
    )
    print(f"数据包：{packet_path}")
    errors = [d for d in diagnostics if d.level == "error"]
    if errors and args.strict:
        print(f"--strict：存在 {len(errors)} 条 error 级诊断", file=sys.stderr)
        return 1
    return 0


def _incremental_scope(packet, cfg, *, enabled: bool, previous: DataPacket | None):
    """算出本轮需要重渲的模块集合；返回 ``(集合|None, 诊断|None)``。

    ``None`` 表示"不做增量"（全量渲染）。判据写清为什么不做 —— 否则"开了增量
    却没生效"会变成又一个静默问题。
    """
    if not enabled:
        return None, None

    if previous is None:
        return None, Diagnostic(
            level="info",
            code="INCREMENTAL_SKIPPED",
            message="增量已开启但无上一轮快照（首次运行）⇒ 本轮全量渲染",
        )

    if _config_fingerprint(previous) != _config_fingerprint(packet, cfg=cfg):
        return None, Diagnostic(
            level="info",
            code="INCREMENTAL_SKIPPED",
            message="分层或注解相对上一轮有变化（每个模块页都带层名与注解）⇒ 本轮全量渲染",
        )

    impact = diff_packets(previous, packet)
    scope = set(impact.changed) | set(impact.added) | set(impact.direct) | set(impact.indirect)
    reused = sum(1 for n in packet.nodes if n.type.value == "Module" and n.id not in scope)
    return scope, Diagnostic(
        level="info",
        code="INCREMENTAL_SCOPE",
        message=f"增量渲染：重渲 {len(scope)} 个模块 / 复用 {reused} 个（改动 {len(impact.changed)}）",
    )


def _read_previous_snapshot(out_dir: Path):
    """读上一轮产物（供增量比较）；不存在或损坏返回 ``None``（⇒ 本轮全量）。"""
    snapshot = out_dir / "data" / "codewiki.json"
    if not snapshot.is_file():
        return None
    try:
        return DataPacket.model_validate_json(snapshot.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 上一轮产物坏了就全量，不猜
        return None


def _config_fingerprint(packet, *, cfg=None) -> str:
    """分层与注解的指纹（用于判断"配置变了就该全量重渲"）。"""
    import hashlib

    layers = [(layer.id, layer.name) for layer in packet.layers]
    notes = [(a.target, a.label, a.note, a.source, a.reviewed) for a in packet.annotations]
    if cfg is not None:
        layers = [(layer["id"], layer["name"]) for layer in cfg.layers_as_nodes()]
        notes = [(a.target, a.label, a.note, a.source, a.reviewed) for a in cfg.annotations]
    payload = repr(sorted(layers)) + "|" + repr(sorted(notes))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _write_wiki(packet, out_dir: Path, *, diagrams_dir: Path, only_modules: set[str] | None = None) -> None:
    """写 index.md 与 modules/*.md（图来自已落盘的 .mmd，点与图同源）。"""
    modules_dir = out_dir / MODULES_SUBDIR
    modules_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / INDEX_FILENAME).write_text(render_index(packet), encoding="utf-8")

    from codewiki.models import NodeType
    from codewiki.render.diagrams import build_slug_map

    slug_map = build_slug_map(
        [n.path for n in packet.nodes if n.type == NodeType.MODULE and n.path]
    )

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


def _write_diagnostics_md(out_dir: Path, diagnostics: list[Diagnostic]) -> None:
    """诊断清单（§4.5 的 diagnostics.md），人类可读的最小形态。"""
    lines = ["# codewiki 诊断", ""]
    if not diagnostics:
        lines.append("无诊断输出。")
    for item in diagnostics:
        where = f"（{item.where}）" if item.where else ""
        lines.append(f"- **{item.level}** `{item.code}` {where}：{item.message}")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "diagnostics.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _export(args: argparse.Namespace) -> int:
    out_dir = Path(args.dir).resolve()
    target = Path(args.zip_path).resolve() if args.zip_path else out_dir.with_suffix(".zip")
    if not out_dir.is_dir():
        print(f"error: 产物目录不存在: {out_dir}", file=sys.stderr)
        return 2
    path = write_zip(out_dir, target)
    print(f"已打包：{path}")
    return 0


def _doctor_cmd(args: argparse.Namespace) -> int:
    print(render_report(run_checks(out_dir=args.dir, probe_ai=args.probe)), end="")
    return 0


def _ai_annotate_cmd(args: argparse.Namespace) -> int:
    """独立 AI 标注命令（§5.2 工作流 A）。

    刻意与 ``analyze`` 分离：无网环境出静态产物，另一台带模型的机器读数据包 +
    源码出标注，再拷回合并。因此本命令**从不改写已产出的静态产物**。
    """
    input_path = Path(args.input).resolve()
    if not input_path.is_file():
        print(f"error: 数据包不存在: {input_path}（先跑 analyze）", file=sys.stderr)
        return 2

    config_path = Path(args.config).resolve() if args.config else Path.cwd() / CONFIG_FILENAME
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    model = args.model or cfg.ai.model
    if not model:
        print(
            "error: 未指定模型 —— 在 codewiki.toml 里配 [ai] model，或用 --model 指定",
            file=sys.stderr,
        )
        return 2

    src_root = Path(args.src).resolve() if args.src else None
    if src_root is not None and not src_root.is_dir():
        print(f"error: --src 不是目录: {src_root}", file=sys.stderr)
        return 2

    try:
        packet = DataPacket.model_validate_json(input_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 数据包损坏属用户输入错误，给 2 不是 500
        print(f"error: 数据包无法解析: {exc}", file=sys.stderr)
        return 2

    out_dir = Path(args.out).resolve() if args.out else input_path.parent.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_root = Path(args.cache_dir).resolve() if args.cache_dir else (src_root or Path.cwd())

    client = OpenAICompatClient(
        model=model,
        base_url=args.base_url or cfg.ai.base_url,
        timeout_s=cfg.ai.timeout_s,
        max_retries=cfg.ai.max_retries,
    )
    write_back = args.write_back or cfg.ai.write_back
    if write_back not in _WRITE_BACK_POLICIES:
        print(f"error: 未知 write-back 策略 {write_back!r}（可选 {sorted(_WRITE_BACK_POLICIES)}）", file=sys.stderr)
        return 2

    diagnostics: list[Diagnostic] = []
    with AuditLog(out_dir / AUDIT_FILENAME) as audit:
        result = annotate_modules(
            packet,
            cfg,
            src_root=src_root,
            client=client,
            cache_root=cache_root,
            budget_tokens=args.budget if args.budget is not None else cfg.ai.budget_tokens,
            top_n=args.top_n or cfg.ai.top_n,
            audit=audit,
        )
    diagnostics.extend(result.diagnostics)

    suggest_path = write_suggestions(result.suggestions, out_dir)
    _write_diagnostics_md(out_dir / "suggestions", diagnostics)

    print(f"AI 建议 {len(result.suggestions)} 条（跳过 {len(result.skipped)} 个模块）")
    if write_back == "dry-run":
        print("（dry-run：未写文件）")
        print(suggest_path.read_text(encoding="utf-8"))
    elif write_back == "confirm":
        kept = [s for s in result.suggestions if _confirm(s)]
        suggest_path = write_suggestions(kept, out_dir)
        print(f"审阅后保留 {len(kept)} 条")
    elif write_back == "auto":
        outcome = merge_annotations(config_path, suggest_path, dry_run=False)
        print(render_merge_report(outcome))
    else:
        print(f"建议文件：{suggest_path}")
        print(f"审阅后合并：codewiki merge-annotations {suggest_path}")

    for diag in diagnostics:
        print(f"  [{diag.level}] {diag.code}: {diag.message}")

    if result.degraded:
        print("AI 未完成（部分结果已保留，静态产物不受影响）", file=sys.stderr)
        return 1
    return 0


_WRITE_BACK_POLICIES = frozenset({"review-file", "dry-run", "confirm", "auto"})


def _confirm(suggestion) -> bool:
    """逐条确认（少量模块时的交互策略）。"""
    label = suggestion.label or suggestion.target
    print(f"\n[{suggestion.target}] {label}")
    print(suggestion.note)
    try:
        return input("采纳？(y/N) ").strip().lower() == "y"
    except EOFError:
        return False


def _merge_annotations_cmd(args: argparse.Namespace) -> int:
    suggest_path = Path(args.file).resolve()
    if not suggest_path.is_file():
        print(f"error: 建议文件不存在: {suggest_path}", file=sys.stderr)
        return 2
    config_path = Path(args.config).resolve() if args.config else Path.cwd() / CONFIG_FILENAME

    outcome = merge_annotations(config_path, suggest_path, force=args.force, dry_run=args.dry_run)
    print(render_merge_report(outcome))
    return 1 if outcome.conflicts else 0


def _load_packet(path: Path) -> DataPacket | None:
    """读数据包（统一入口）；不存在或损坏返回 ``None`` 并由调用方给退出码 2。

    刻意不抛 :class:`SystemExit`：带字符串的 SystemExit 退出码是 1，与"运行失败"
    混在一起，而"输入文件不存在"是**用法错误**（应给 2）。
    """
    if not path.is_file():
        print(f"error: 数据包不存在: {path}（先跑 analyze）", file=sys.stderr)
        return None
    try:
        return DataPacket.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 坏数据包属用户输入错误
        print(f"error: 数据包无法解析: {exc}", file=sys.stderr)
        return None


def _cycles_cmd(args: argparse.Namespace) -> int:
    """环检测（§2.5）：模块环 warning、层级环 error。"""
    packet = _load_packet(Path(args.input))
    if packet is None:
        return 2
    cycles = find_module_cycles(packet) + find_layer_cycles(packet)
    diagnostics = cycles_to_diagnostics(cycles)

    print(f"环 {len(cycles)} 个（模块 {sum(1 for c in cycles if c.kind == 'module')} / "
          f"层级 {sum(1 for c in cycles if c.kind == 'layer')}）")
    for diag in diagnostics:
        print(f"  [{diag.level}] {diag.code}: {diag.message}")

    if args.out:
        out_dir = Path(args.out)
        written = render_all_cycles(cycles, out_dir / "diagrams" / "cycles")
        for name in written:
            print(f"环图：{out_dir / 'diagrams' / 'cycles' / name}")

    return 1 if any(d.level == "error" for d in diagnostics) else 0


def _diff_cmd(args: argparse.Namespace) -> int:
    """变更影响分析（§2.5）：支持两个快照，或 git 范围。"""
    if args.base and args.head:
        base = _load_packet(Path(args.base))
        head = _load_packet(Path(args.head))
        if base is None or head is None:
            return 2
        impact = diff_packets(base, head)
    else:
        packet = _load_packet(Path(args.input))
        if packet is None:
            return 2
        if not args.range:
            print("error: 需要 git 范围（如 v1.2.0..HEAD）或 --base/--head", file=sys.stderr)
            return 2
        changed = _changed_modules_from_git(args.range, repo_root=Path(args.src or "."))
        impact = impact_of(packet, changed)

    print(f"改动 {len(impact.changed)} 个模块 / 新增 {len(impact.added)} / 删除 {len(impact.removed)}")
    for node_id in impact.changed[:20]:
        print(f"  改动：{_short(node_id)}")
    print(f"直接影响 {len(impact.direct)} 个、间接影响 {len(impact.indirect)} 个（影响半径 {impact.radius}）")
    for node_id in impact.direct[:20]:
        print(f"  直接：{_short(node_id)}")
    for node_id in impact.indirect[:20]:
        print(f"  间接：{_short(node_id)}")
    return 0


def _changed_modules_from_git(rev_range: str, *, repo_root: Path):
    """用**沙箱**跑 git 拿改动文件（不自己拼命令，也不给 AI 开口子）。"""
    from codewiki.ai.tools import run_shell

    output = run_shell(f"git diff --name-only {rev_range}", repo_root=repo_root.resolve())
    return [f"mod::{line.strip()}" for line in output.splitlines() if line.strip()]


def _short(node_id: str) -> str:
    return node_id.removeprefix("mod::").removeprefix("layer::")


def _suggest_layers_cmd(args: argparse.Namespace) -> int:
    packet = _load_packet(Path(args.input))
    if packet is None:
        return 2
    layers = suggest_layers(packet)
    out_dir = Path(args.out) if args.out else Path(args.input).parent.parent
    target_dir = out_dir / "suggestions"
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / SUGGESTED_LAYERS_FILENAME
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(render_suggested_layers(layers, title=packet.meta.title), encoding="utf-8")
    tmp.replace(path)

    print(f"建议分层 {len(layers)} 层：{path}")
    for layer in layers:
        print(f"  - {layer.name}: {layer.reason()}")
    print(f"审阅后合并：codewiki merge-layers {path}")
    return 0


def _merge_layers_cmd(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve() if args.config else Path.cwd() / CONFIG_FILENAME
    outcome = merge_layers(config_path, Path(args.file).resolve(), dry_run=args.dry_run)
    print(render_layer_merge_report(outcome))
    return 1 if outcome.overlaps else 0


def _serve_cmd(args: argparse.Namespace) -> int:
    """本地静态预览（§六 serve）。

    只做**静态**服务：不解析模板、不执行脚本，因为产物本来就是纯静态目录。
    """
    directory = Path(args.dir).resolve()
    if not directory.is_dir():
        print(f"error: 产物目录不存在: {directory}", file=sys.stderr)
        return 2
    httpd, port = serve_preview(directory, args.port)
    print(f"预览：http://127.0.0.1:{port}/ （{directory}）—— Ctrl-C 结束")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0


def _import_graph_cmd(args: argparse.Namespace) -> int:
    """导入外部图谱（§4.6.4）：graphify → 统一数据包。"""
    from codewiki.graph.importers.graphify import GraphifyFormatError, load_graphify

    source = Path(args.input).resolve()
    out_dir = Path(args.out).resolve()
    try:
        packet, diagnostics = load_graphify(source)
    except GraphifyFormatError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    diagnostics.append(
        Diagnostic(level="info", code="GRAPHIFY_IMPORTED", message=f"已从 {source} 导入", where=str(source))
    )
    from codewiki.render.jsonio import write_packet

    path = write_packet(packet, out_dir)
    print(f"导入完成：{len(packet.nodes)} 节点 / {len(packet.edges)} 边")
    for diag in diagnostics:
        print(f"  [{diag.level}] {diag.code}: {diag.message}")
    print(f"数据包：{path}")
    return 0


def _agent_cmd(args: argparse.Namespace) -> int:
    """交互问答（§5.2 工作流 B）：模型决定调什么工具，我们负责执行与安全。"""
    packet = _load_packet(Path(args.input))
    if packet is None:
        return 2
    try:
        model = args.model or load_config(
            Path(args.config).resolve() if args.config else Path.cwd() / CONFIG_FILENAME
        ).ai.model
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not model:
        print("error: 未指定模型（[ai] model 或 --model）", file=sys.stderr)
        return 2

    question = args.question or _read_question_from_stdin()
    if not question:
        print("error: 需要提供问题（作为参数，或从 stdin 读入）", file=sys.stderr)
        return 2

    out_dir = Path(args.out).resolve() if args.out else Path(args.input).parent.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    audit = AuditLog(out_dir / AUDIT_FILENAME)

    with audit:
        loop = AgentLoop(
            OpenAICompatClient(model=model, base_url=args.base_url, timeout_s=60.0),
            packet,
            repo_root=Path(args.src).resolve() if args.src else None,
            audit=audit,
            max_rounds=args.max_rounds,
        )
        result = loop.run(question)

    for step in result.rounds:
        tool = step.get("tool")
        suffix = f" → {tool}({step.get('args')})" if tool else " → 结论"
        print(f"[{step['round']}] {step['thought'][:80]}{suffix}")

    if result.answer:
        print("\n答案：")
        print(result.answer)
    else:
        print("\n未得出结论。", file=sys.stderr)
    for note in result.diagnostics:
        print(f"  {note}", file=sys.stderr)
    return 0 if result.answer else 1


def _read_question_from_stdin() -> str:
    """非交互管道场景：从 stdin 读问题（没有输入就返回空串，不阻塞）。"""
    if sys.stdin.isatty():
        return ""
    return sys.stdin.read().strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codewiki", description="代码架构知识图谱与文档生成器")
    parser.add_argument("--version", action="version", version=f"codewiki {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    analyze = sub.add_parser("analyze", help="纯静态分析，产出标准数据包")
    analyze.add_argument("--src", default=".", help="待分析仓库根目录")
    analyze.add_argument("--out", default="wiki-output", help="产物输出目录")
    analyze.add_argument("--backend", default="auto", help="auto | ast | treesitter | regex")
    analyze.add_argument("--exclude", action="append", help="追加跳过的目录名（可多次）")
    analyze.add_argument(
        "--config", help=f"配置路径（默认 <src>/{CONFIG_FILENAME}）"
    )
    analyze.add_argument(
        "--offline-asset", help="离线看图用的 mermaid.min.js（默认取包内自带的那份）"
    )
    analyze.add_argument("--no-cache", action="store_true", help="跳过 FileModel 增量缓存（强制全量解析）")
    analyze.add_argument(
        "--incremental",
        action="store_true",
        help="增量渲染：只重渲受影响模块（复用上一轮产物；无快照或配置变更时自动全量）",
    )
    analyze.add_argument(
        "--min-confidence",
        type=float,
        default=None,
        help="覆盖配置里的 analysis.min_confidence",
    )
    analyze.add_argument("--strict", action="store_true", help="存在 error 级诊断时以非 0 退出")
    analyze.set_defaults(func=_analyze)

    export = sub.add_parser("export", help="把产物目录打成单个 Zip（原子化）")
    export.add_argument("--dir", default="wiki-output", help="产物目录")
    export.add_argument("--zip", dest="zip_path", default=None, help="输出 Zip 路径（默认 <目录>.zip）")
    export.set_defaults(func=_export)

    doctor = sub.add_parser("doctor", help="环境自检（Python / 后端 / Node / 离线资源）")
    doctor.add_argument("--dir", default=None, help="顺带检查产物目录里的离线资源")
    doctor.add_argument("--probe", action="store_true", help="连一次模型服务验证（会真实调用，可能产生费用）")
    doctor.set_defaults(func=_doctor_cmd)

    annotate = sub.add_parser("ai-annotate", help="AI 架构标注（独立命令，需模型环境）")
    annotate.add_argument("--input", default="wiki-output/data/codewiki.json", help="analyze 产出的数据包")
    annotate.add_argument("--config", help=f"配置路径（默认 ./{CONFIG_FILENAME}）：注入分层与人工注解语境")
    annotate.add_argument("--src", help="源码根目录（只读取证；不给则仅依据数据包事实）")
    annotate.add_argument("--out", help="建议输出目录（默认数据包所在产物根目录）")
    annotate.add_argument("--cache-dir", help="AI 缓存根目录（默认 --src 或当前目录）")
    annotate.add_argument("--model", help="覆盖配置里的 [ai] model")
    annotate.add_argument("--base-url", help="覆盖 [ai] base_url（本地 Ollama / vLLM 内网网关）")
    annotate.add_argument("--write-back", choices=sorted(_WRITE_BACK_POLICIES), help="产出策略（默认 review-file）")
    annotate.add_argument("--top-n", type=int, help="标注前 N 个热点模块")
    annotate.add_argument("--budget", type=int, help="单轮 token 硬上限")
    annotate.set_defaults(func=_ai_annotate_cmd)

    merge_cmd = sub.add_parser("merge-annotations", help="把 AI 建议合并进 codewiki.toml")
    merge_cmd.add_argument("file", help="建议文件路径（suggestions/annotations.suggest.toml）")
    merge_cmd.add_argument("--config", help=f"主配置路径（默认 ./{CONFIG_FILENAME}）")
    merge_cmd.add_argument("--force", action="store_true", help="冲突时以建议为准（人工/已审阅注解仍受保护）")
    merge_cmd.add_argument("--dry-run", action="store_true", help="只打印合并计划，不写文件")
    merge_cmd.set_defaults(func=_merge_annotations_cmd)

    cycles_cmd = sub.add_parser("cycles", help="依赖环检测（模块环 + 层级环）")
    cycles_cmd.add_argument("--input", default="wiki-output/data/codewiki.json")
    cycles_cmd.add_argument("--out", help="顺带把每个环写成 .mmd")
    cycles_cmd.set_defaults(func=_cycles_cmd)

    diff_cmd = sub.add_parser("diff", help="变更影响分析（两个快照或 git 范围）")
    diff_cmd.add_argument("--base", help="基线数据包")
    diff_cmd.add_argument("--head", help="目标数据包")
    diff_cmd.add_argument("--input", default="wiki-output/data/codewiki.json", help="git 模式用的当前数据包")
    diff_cmd.add_argument("--src", default=".", help="git 模式用的仓库根")
    diff_cmd.add_argument("range", nargs="?", help="git 范围（如 v1.2.0..HEAD），不传则用 --base/--head")
    diff_cmd.set_defaults(func=_diff_cmd)

    suggest = sub.add_parser("suggest-layers", help="社区发现 → 建议分层（纯静态）")
    suggest.add_argument("--input", default="wiki-output/data/codewiki.json")
    suggest.add_argument("--out", help="建议输出目录")
    suggest.set_defaults(func=_suggest_layers_cmd)

    merge_layers_cmd = sub.add_parser("merge-layers", help="把建议分层合并进 codewiki.toml")
    merge_layers_cmd.add_argument("file", help="建议文件路径（suggestions/suggested-layers.toml）")
    merge_layers_cmd.add_argument("--config", help=f"主配置路径（默认 ./{CONFIG_FILENAME}）")
    merge_layers_cmd.add_argument("--dry-run", action="store_true", help="只打印计划，不写文件")
    merge_layers_cmd.set_defaults(func=_merge_layers_cmd)

    serve_cmd = sub.add_parser("serve", help="本地静态预览（纯静态目录，不执行脚本）")
    serve_cmd.add_argument("--dir", default="wiki-output", help="产物目录")
    serve_cmd.add_argument("--port", type=int, default=8000, help="端口（0 = 由系统分配）")
    serve_cmd.set_defaults(func=_serve_cmd)

    import_graph = sub.add_parser("import-graph", help="导入外部图谱（目前支持 graphify）")
    import_graph.add_argument("--from", dest="source", default="graphify", help="来源格式")
    import_graph.add_argument("--input", required=True, help="图谱文件路径（如 graphify-out/<日期>/graph.json）")
    import_graph.add_argument("--out", default="wiki-output", help="产物输出目录")
    import_graph.set_defaults(func=_import_graph_cmd)

    agent_cmd = sub.add_parser("agent", help="交互问答：模型调用只读工具取证后作答")
    agent_cmd.add_argument("question", nargs="?", help="问题；不给则从 stdin 读")
    agent_cmd.add_argument("--input", default="wiki-output/data/codewiki.json")
    agent_cmd.add_argument("--src", help="源码根目录（用于 read_file / search / shell）")
    agent_cmd.add_argument("--config", help=f"配置路径（默认 ./{CONFIG_FILENAME}）")
    agent_cmd.add_argument("--model", help="覆盖 [ai] model")
    agent_cmd.add_argument("--base-url", help="覆盖 [ai] base_url")
    agent_cmd.add_argument("--max-rounds", type=int, default=10, help="最大取证轮次（防无限循环）")
    agent_cmd.add_argument("--out", help="审计日志目录（默认与数据包同级的产物根目录）")
    agent_cmd.set_defaults(func=_agent_cmd)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
