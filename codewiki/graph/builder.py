"""图谱构建（引擎层核心，设计文稿 §2.4）。

职责：消费各 Analyzer 产出的 ``FileModel``，做**跨文件**的符号消歧、re-export
解析、调用解析、层级聚合与环检测，产出统一的 Node/Edge/Diagnostic。

关键算法（§2.4 逐条对应）：

1. **符号消歧**：先建全量符号表，再按 import 别名 → 当前作用域 → 同包 → 全局
   的顺序解析调用目标；解析失败落 ``External(unknown)`` 或记为动态边，
   **不做随机绑定**。
2. **re-export 解析**：建立"对外可见名 → 真实定义位置"的别名表，多跳链做
   不动点迭代直到收敛；成环则中止该链并记低置信动态边。
3. **环检测**：文件级用 SCC，层级级用 simple_cycles（分层出现环 = 严重诊断）。
"""

from __future__ import annotations

import itertools
import posixpath
import sys
from dataclasses import dataclass, field

import networkx as nx

from codewiki.layering import UNLAYERED_LAYER_ID, layer_of
from codewiki.models import (
    CONF_DYNAMIC,
    CONF_NAME_GUESS,
    CONF_NEAR,
    CONF_STATIC,
    Edge,
    EdgeType,
    ExternalKind,
    FileModel,
    Node,
    NodeType,
    Provenance,
    SymbolKind,
)
from codewiki.schemas import Diagnostic

#: 未命中任何分层的模块归入该虚拟层（§2.1）。

#: re-export 多跳迭代的上限（超过即视为异常，中止并记诊断）。
MAX_REEXPORT_HOPS = 10

#: 每个强连通分量最多列几条分层环（分层环过多时全量枚举会让分析挂死）。
MAX_CYCLES_PER_COMPONENT = 3

#: TS/JS 省略扩展名时的探测顺序（对齐 tsconfig 默认解析序：``.ts`` → ``.tsx`` →
#: ``.d.ts`` → ``.js``）。``.d.ts`` 必须排在 ``.ts`` 之后：两者并存时运行时依赖的是
#: 实现文件，连到声明文件等于把依赖指向一个没有代码的壳。
JS_SUFFIXES: tuple[str, ...] = (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs")

#: 目录导入的落点（``@/components`` → ``components/index.ts``）。
JS_INDEXES: tuple[str, ...] = (
    "/index.ts",
    "/index.tsx",
    "/index.d.ts",
    "/index.js",
    "/index.jsx",
)


def module_id(path: str) -> str:
    """Module 的稳定 ID（§2.3）。"""
    return f"mod::{path}"


def symbol_id(module_path: str, qualname: str) -> str:
    """Symbol 的稳定 ID（§2.3）。"""
    return f"sym::{module_path}::{qualname}"


def external_id(name: str) -> str:
    """External 的稳定 ID（第三方/标准库不展开）。"""
    return f"ext::{name}"


@dataclass
class BuildResult:
    """建图产物（供渲染层与 AI 层消费）。"""

    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)


class GraphBuilder:
    """由 FileModel 列表构建统一图谱。"""

    def __init__(
        self,
        *,
        min_confidence: float = 0.5,
        layers: list[dict] | None = None,
        root_package: str | None = None,
        ts_aliases: dict[str, str] | None = None,
    ) -> None:
        self.min_confidence = min_confidence
        self.layers_cfg = layers or []
        #: TS/JS 模块别名（``{pattern: 仓库内路径}``）。``pattern`` 以 ``*`` 结尾表示
        #: 前缀通配（``"@/*" → "frontend-new/src/*"``），否则按**精确匹配**。
        #: 别名来自 tsconfig ``paths``，但**只认配置**：读 tsconfig 需要 JSONC 解析，
        #: 且"猜"出来的别名一旦错了就会把第三方包错连成本地模块 —— 宁可不连。
        self.ts_aliases = ts_aliases or {}
        #: 分析根目录的包名（``codewiki analyze --src app`` 里的 ``app``）。
        #: 代码里的绝对导入常写成 ``from app.services.x import Y``，此时点分名
        #: **比解析根多一层**；剥离这一层才找得到文件（见 _resolve_module_path）。
        self.root_package = root_package or None
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        self.diagnostics: list[Diagnostic] = []

        #: 模块路径 → 该模块内名字 → symbol id
        self._module_symbols: dict[str, dict[str, str]] = {}
        #: 模块路径 → 对外可见名 → 目标 symbol id（re-export 别名表，迭代后收敛）
        self._exports: dict[str, dict[str, str]] = {}
        #: 模块路径 → FileModel
        self._files: dict[str, FileModel] = {}
        #: 全局名 → 命中过的 symbol id 列表（用于同名猜测与歧义判定）
        self._global_names: dict[str, list[str]] = {}
        #: 后端名 → Provenance（汇总进数据包 meta）
        self._provenance: dict[str, Provenance] = {}
        #: CALL_AMBIGUOUS 诊断已报条数（大仓库同名符号极多，设上限防诊断洪水）
        self._ambiguous_reported = 0
        #: (source, target, type, alias, static, confidence) → Edge（重复边合并索引）
        self._edge_index: dict[tuple, Edge] = {}
        #: 已报过 IMPORT_SHADOWED 的 (调用方模块, 绝对名) —— 同一处不重复刷诊断
        self._shadowed_reported: set[tuple[str, str]] = set()

    # ── 入口 ─────────────────────────────────────────────────────────────
    def build(self, files: list[FileModel]) -> BuildResult:
        self._files = {f.path: f for f in files}
        self._index_symbols(files)
        self._build_exports()
        self._add_modules_and_symbols(files)
        self._resolve_imports(files)
        self._resolve_calls(files)
        self._resolve_inheritance(files)
        self._resolve_dynamic_uses(files)
        self._drop_dangling_edges()
        self._aggregate_layer_dependencies()
        self._detect_cycles()
        self._warn_missing_layer_dependency()
        return BuildResult(nodes=list(self.nodes.values()), edges=self.edges, diagnostics=self.diagnostics)

    def _warn_missing_layer_dependency(self) -> None:
        """配了多层却一条层间依赖都没有 —— 几乎必然是 import 整体没解析出来。

        为什么不用"解析率过低"作判据：真第三方依赖（sqlalchemy / flask / typing）
        本来就解析不到仓库内文件，它们与"本地模块没解析出来"在数据上无法区分，
        按比例报会把正常仓库判成异常（IPIP 实测：修正后仍有 57% 的 import
        指向外部，但本地依赖其实已经解析出来了）。

        改用"分层图有没有边"作判据：真实项目里配出来的两层不可能零依赖；零依赖
        只有一种解释——跨文件 import 全部失效（``--src`` 选在包内部而代码用
        ``from <pkg>.x`` 绝对导入是典型成因）。此时图照出、层照分，读者却会以为
        "各层互不相关"，比报错更误导。
        """
        layered = {
            n.layer_id
            for n in self.nodes.values()
            if n.type == NodeType.MODULE and n.layer_id and n.layer_id != UNLAYERED_LAYER_ID
        }
        if len(layered) < 2:
            return
        if any(e.type == EdgeType.LAYER_DEPENDS for e in self.edges):
            return
        hint = (
            f"若代码写的是 from {self.root_package}.x import Y，请让 --src 指向该包根（而不是包内子目录）"
            if self.root_package
            else ""
        )
        self.diagnostics.append(
            Diagnostic(
                level="warning",
                code="NO_LAYER_DEPENDENCY",
                message=(
                    f"配置了 {len(layered)} 个层，但它们之间没有任何依赖边；"
                    f"跨文件 import 很可能整体未解析，架构图的层间关系不可信。{hint}"
                ),
            )
        )

    # ── 1. 索引 ──────────────────────────────────────────────────────────
    def _index_symbols(self, files: list[FileModel]) -> None:
        for file in files:
            table: dict[str, str] = {}
            for symbol in file.symbols:
                sid = symbol_id(file.path, symbol.qualname)
                table[symbol.qualname] = sid
                table.setdefault(symbol.name, sid)
            self._module_symbols[file.path] = table
            for symbol in file.symbols:
                self._global_names.setdefault(symbol.name, []).append(symbol_id(file.path, symbol.qualname))

    def _exported_names(self, file: FileModel) -> dict[str, str]:
        """模块的对外可见名 → symbol id（含 ``__all__`` 决定公开面）。"""
        table = self._module_symbols.get(file.path, {})
        names = {name: sid for name, sid in table.items() if "." not in name and not name.startswith("_")}
        if file.exports:
            names = {n: s for n, s in names.items() if n in file.exports}
        return names

    def _build_exports(self) -> None:
        """re-export 别名表（§2.4 第 2 条）：建立"对外可见名 → 真实定义"。

        两步：

        1. **登记**：本模块自己定义的符号直接指向其 symbol id；通过
           ``from X import Y [as Z]`` 绑定到本地的名字，先记为
           ``ref::<点分模块>::<名字>``（此刻还不知道 Y 的真身在哪）；
        2. **不动点迭代**：逐轮把 ``ref::`` 解析成真实 symbol id，直到收敛。
           多跳链（A re-export B，B 又 re-export C）因此在有限轮内穿透到 C。

        成环或超过 :data:`MAX_REEXPORT_HOPS` 轮的别名保留为 ``ref::`` 形态并记
        诊断 —— 下游会退化成"模块级 IMPORTS + 低置信"，**不猜测**（§2.4）。
        """
        for path, file in self._files.items():
            table = dict(self._exported_names(file))
            for imp in file.imports:
                if imp.kind != "from" or imp.is_star or not imp.name:
                    continue
                table.setdefault(imp.alias or imp.name, f"ref::{imp.module}::{imp.name}")
            self._exports[path] = table

        for _hop in range(MAX_REEXPORT_HOPS):
            changed = False
            for path, table in self._exports.items():
                for name, value in list(table.items()):
                    if not value.startswith("ref::"):
                        continue
                    _tag, dotted, target_name = value.split("::", 2)
                    module_path = self._resolve_module_path(dotted, from_module=path)
                    if module_path is None:
                        continue
                    resolved = self._exports.get(module_path, {}).get(target_name)
                    if resolved and resolved != value:
                        table[name] = resolved  # 可能仍是 ref:: —— 下一轮继续跟随
                        changed = True
            if not changed:
                return

        self.diagnostics.append(
            Diagnostic(
                level="warning",
                code="REEXPORT_HOP_LIMIT",
                message=f"re-export 链在 {MAX_REEXPORT_HOPS} 跳内未收敛（疑似成环），已保留当前别名并降低置信度",
            )
        )

    def _follow_alias(self, target: str, *, seen: set[str]) -> str | None:
        """跟随一个 symbol id 指向的别名链；成环返回 None。"""
        # 别名表的值是 symbol id（sym::<module>::<qualname>），若该 module 又把
        # 这个名字别名到别处，则继续跟。
        if not target.startswith("sym::"):
            return target
        body = target[len("sym::") :]
        module_path, _, qualname = body.rpartition("::")
        if not module_path or module_path in seen:
            return None
        nxt = self._exports.get(module_path, {}).get(qualname)
        if not nxt or nxt == target:
            return target
        return self._follow_alias(nxt, seen=seen | {module_path})

    # ── 2. 节点 ──────────────────────────────────────────────────────────
    def _add_modules_and_symbols(self, files: list[FileModel]) -> None:
        for file in files:
            mid = module_id(file.path)
            self._add(
                Node(
                    id=mid,
                    type=NodeType.MODULE,
                    name=file.path.rsplit("/", 1)[-1],
                    path=file.path,
                    language=file.language,
                    hash=file.hash,
                    layer_id=self._layer_of(file.path),
                )
            )
            if file.provenance:
                self._provenance[file.provenance.backend] = file.provenance

            for symbol in file.symbols:
                sid = symbol_id(file.path, symbol.qualname)
                self._add(
                    Node(
                        id=sid,
                        type=NodeType.SYMBOL,
                        name=symbol.name,
                        kind=symbol.kind,
                        module_id=mid,
                        line_start=symbol.lineno,
                        line_end=symbol.endlineno,
                        signature=symbol.signature,
                        docstring=symbol.docstring,
                        language=file.language,
                    )
                )
                parent = mid if symbol.parent is None else symbol_id(file.path, symbol.parent)
                self._edge(parent, sid, EdgeType.CONTAINS)

    # ── 3. 边 ────────────────────────────────────────────────────────────
    def _resolve_imports(self, files: list[FileModel]) -> None:
        for file in files:
            mid = module_id(file.path)
            for imp in file.imports:
                if imp.is_star:
                    # 星号导入：能确定模块就记 IMPORTS，名字层面记为动态（§0.3 非目标 3）
                    target_module = self._resolve_module_path(imp.module, from_module=file.path)
                    if target_module:
                        self._edge(mid, module_id(target_module), EdgeType.IMPORTS, weight=1)
                    continue

                if imp.module and imp.name is None:  # import pkg.sub [as x]
                    target_module = self._resolve_module_path(imp.module or "", from_module=file.path)
                    if target_module:
                        self._edge(
                            mid,
                            module_id(target_module),
                            EdgeType.IMPORTS,
                            alias=imp.alias,
                            weight=1,
                        )
                    else:
                        self._edge(mid, self._external(imp.module or ""), EdgeType.IMPORTS, alias=imp.alias)
                    continue

                # from pkg.sub import name [as alias]
                source = self._exports.get(file.path, {}).get(imp.alias or imp.name or "")
                if source and not source.startswith("ref::"):
                    # 名字已解析到具体符号：边指向其所属模块（模块级 IMPORTS）。
                    # 仍是 ref:: 说明 re-export 链没穿透（目标模块不在仓内），
                    # 不能拿 ``ref::…`` 当模块路径用 —— 那会造出指向
                    # ``mod::ref::*`` 的悬空边；往下走模块级解析落 External。
                    owner = self._owner_module(source)
                    if owner and owner != file.path:
                        self._edge(mid, module_id(owner), EdgeType.IMPORTS, alias=imp.alias)
                    continue

                # ``from pkg import sub``：``sub`` 若是 ``pkg/sub.py``，依赖的是子模块本身
                # （Python 的语义：先找属性，找不到再把它当子模块导入）。此前这条边
                # 落到 ``pkg/__init__.py``，包没有 ``__init__`` 时更是整条退化成
                # ``ext::pkg`` —— 图上少一条真实依赖，下游（度数、AI 语境）都跟着失真。
                submodule = self._resolve_submodule(imp.module, imp.name, from_module=file.path)
                if submodule:
                    self._edge(mid, module_id(submodule), EdgeType.IMPORTS, alias=imp.alias)
                    continue

                target_module = self._resolve_module_path(imp.module or "", from_module=file.path)
                if target_module:
                    self._edge(mid, module_id(target_module), EdgeType.IMPORTS, alias=imp.alias)
                    self.diagnostics.append(
                        Diagnostic(
                            level="info",
                            code="IMPORT_NAME_UNRESOLVED",
                            message=f"from {imp.module} import {imp.name} 未解析到具体符号",
                            where=f"{file.path}:{imp.lineno}",
                        )
                    )
                else:
                    self._edge(mid, self._external(imp.module or ""), EdgeType.IMPORTS, alias=imp.alias)

    def _resolve_calls(self, files: list[FileModel]) -> None:
        for file in files:
            for call in file.calls:
                if call.receiver:  # obj.method：无类型信息，按同名猜测（低置信）
                    guessed = self._guess_symbol(call.callee, file.path)
                    if guessed:
                        self._edge(
                            self._caller_id(file, call),
                            guessed,
                            EdgeType.CALLS,
                            static=False,
                            confidence=CONF_NAME_GUESS,
                            evidence=f"{file.path}:{call.lineno}",
                        )
                    continue

                target = self._lookup_name(call.callee, file.path)
                if target:
                    confidence = CONF_STATIC if self._is_local(call.callee, file.path) else CONF_NEAR
                    self._edge(
                        self._caller_id(file, call),
                        target,
                        EdgeType.CALLS,
                        confidence=confidence,
                        evidence=f"{file.path}:{call.lineno}",
                    )

    def _resolve_inheritance(self, files: list[FileModel]) -> None:
        for file in files:
            for item in file.inherits:
                base = self._lookup_name(item.base.split(".")[-1], file.path)
                if base is None:
                    base = self._external(item.base)
                self._edge(symbol_id(file.path, item.child), base, EdgeType.INHERITS)

    def _resolve_dynamic_uses(self, files: list[FileModel]) -> None:
        """动态引用 → ``USES_DYNAMIC`` 边（§0.3：低置信、虚线、不伪装成事实）。

        解析不到仓库内模块时**不猜**：落 External（unknown）而不是随便连一个
        同名的模块 —— 动态导入本身就是"运行时才知道"，把它画成实线依赖是误导。
        """
        for file in files:
            for item in file.dynamic:
                resolved = self._resolve_module_path(item.target, from_module=file.path)
                target = module_id(resolved) if resolved else self._external(item.target)
                self._edge(
                    module_id(file.path),
                    target,
                    EdgeType.USES_DYNAMIC,
                    static=False,
                    confidence=CONF_DYNAMIC,
                    evidence=f"{file.path}:{item.lineno}",
                )

    def _aggregate_layer_dependencies(self) -> None:
        """由 IMPORTS 聚合成 LAYER_DEPENDS（weight = 跨层边数，§2.2）。"""
        counts: dict[tuple[str, str], int] = {}
        for edge in self.edges:
            if edge.type != EdgeType.IMPORTS:
                continue
            src = self.nodes.get(edge.source)
            dst = self.nodes.get(edge.target)
            if not src or not dst or src.layer_id == dst.layer_id:
                continue
            if not src.layer_id or not dst.layer_id:
                continue
            key = (src.layer_id, dst.layer_id)
            counts[key] = counts.get(key, 0) + 1
        for (src_layer, dst_layer), weight in counts.items():
            self._edge(src_layer, dst_layer, EdgeType.LAYER_DEPENDS, weight=weight)

    def _detect_cycles(self) -> None:
        """层级环（error，分层失效）+ 模块环（warning）。

        虚拟层 ``_unlayered`` **不参与**层级环：它不是配置出来的层，只是"没匹配
        任何 paths"的兜底桶。它天然与每个真实层双向依赖（A 层里的模块 import
        了某个未分层模块，后者又 import 回 A），照算就必然报环 —— 一次部分分层
        的配置能刷出好几条 ``LAYER_CYCLE`` error，把真正的分层倒置淹掉。
        """
        layer_graph = nx.DiGraph()
        module_graph = nx.DiGraph()
        for edge in self.edges:
            if edge.type == EdgeType.LAYER_DEPENDS:
                if UNLAYERED_LAYER_ID in (edge.source, edge.target):
                    continue
                layer_graph.add_edge(edge.source, edge.target)
            elif edge.type == EdgeType.IMPORTS:
                module_graph.add_edge(edge.source, edge.target)

        # 分层环按**强连通分量**限量枚举：`simple_cycles` 在互相依赖的层图上是
        # 组合级的（实测 10 层互连 ⇒ 111 万条环，构造诊断就把整轮分析拖死）。
        # 诊断要的是"哪几层互相绕"，不是把所有绕法列全 —— 每分量取前 N 条，
        # 多出来的只报数量。
        for component in nx.strongly_connected_components(layer_graph):
            if len(component) < 2:
                continue
            sub = layer_graph.subgraph(component)
            # 多取一条用于判断"还有更多"；绝不数总数（数总数就是全量枚举，
            # 又回到组合爆炸）。
            probe = list(itertools.islice(nx.simple_cycles(sub), MAX_CYCLES_PER_COMPONENT + 1))
            for cycle in probe[:MAX_CYCLES_PER_COMPONENT]:
                self.diagnostics.append(
                    Diagnostic(
                        level="error",
                        code="LAYER_CYCLE",
                        message="架构分层出现环（分层失效）: " + " → ".join([*cycle, cycle[0]]),
                    )
                )
            if len(probe) > MAX_CYCLES_PER_COMPONENT:
                self.diagnostics.append(
                    Diagnostic(
                        level="error",
                        code="LAYER_CYCLE_TRUNCATED",
                        message=(
                            f"该分量内除以上 {MAX_CYCLES_PER_COMPONENT} 条环之外还有更多"
                            f"（这些层两两互相依赖）—— 需要重新划分层边界，而不是逐条修环"
                        ),
                    )
                )

        for component in nx.strongly_connected_components(module_graph):
            if len(component) > 1:
                self.diagnostics.append(
                    Diagnostic(
                        level="warning",
                        code="MODULE_CYCLE",
                        message="模块级依赖环: " + ", ".join(sorted(component)),
                    )
                )

    # ── 工具 ─────────────────────────────────────────────────────────────
    def _drop_dangling_edges(self) -> None:
        """引用完整性兜底（H4）：边端点必须存在于节点表。

        正常路径下各解析器已保证不产悬空边；这一道是防御层 —— 任何后端或解析
        分支的回归都在这里显式暴露（记诊断、丢边），而不是把悬空引用漏给下游
        渲染层静默画一张断链图。``LAYER_DEPENDS`` 的端点是层 ID、不在节点表，
        不在本检查范围。
        """
        if not self.edges:
            return
        kept, dropped = [], []
        for edge in self.edges:
            if edge.type == EdgeType.LAYER_DEPENDS:
                kept.append(edge)
                continue
            if edge.source in self.nodes and edge.target in self.nodes:
                kept.append(edge)
            else:
                dropped.append(edge)
        if dropped:
            self.diagnostics.append(
                Diagnostic(
                    level="warning",
                    code="DANGLING_EDGE_DROPPED",
                    message=f"丢弃 {len(dropped)} 条端点不存在的边（引用完整性兜底）: "
                    + "; ".join(f"{e.source} -{e.type}-> {e.target}" for e in dropped[:5]),
                )
            )
        self.edges = kept
        self._edge_index = {(e.source, e.target, e.type, e.alias, e.static, e.confidence): e for e in kept}

    def _add(self, node: Node) -> None:
        self.nodes.setdefault(node.id, node)

    def _edge(self, source: str, target: str, etype: EdgeType, **kwargs) -> None:
        if source == target:
            return
        # 同 (source, target, type, alias, static, confidence) 的重复边合并为
        # 一条、weight 累加 —— 重复 append 只会让边表膨胀（IPIP 实测 4 万条边
        # 里大量重复），下游 god-node / diff 又按条数统计，等于灌水。
        key = (
            source,
            target,
            etype,
            kwargs.get("alias"),
            kwargs.get("static", True),
            kwargs.get("confidence", CONF_STATIC),
        )
        existing = self._edge_index.get(key)
        if existing is not None:
            existing.weight += int(kwargs.get("weight", 1))
            return
        edge = Edge(source=source, target=target, type=etype, **kwargs)
        self.edges.append(edge)
        self._edge_index[key] = edge

    def _external(self, name: str) -> str:
        # TS/JS 的 specifier 是斜杠路径（``@tanstack/react-query`` / ``./a.js``），
        # 按 Python 点分切会截成包根甚至空串（``ext::`` 这种畸形节点会污染图）。
        root = name if ("/" in name or name.startswith(".")) else name.split(".")[0]
        kind = (
            ExternalKind.STDLIB
            if root in sys.stdlib_module_names
            else ExternalKind.THIRD_PARTY
            if root in self._known_roots()
            else ExternalKind.UNKNOWN
        )
        eid = external_id(root)
        self._add(Node(id=eid, type=NodeType.EXTERNAL, name=root, kind=str(kind)))
        return eid

    def _known_roots(self) -> set[str]:
        """本地已解析模块的顶层包名（用于区分第三方与 unknown）。"""
        return {path.split("/")[0] for path in self._files}

    def _resolve_module_path(self, dotted: str | None, *, from_module: str) -> str | None:
        """点分模块名 → 仓库内的文件路径。

        三种形态：

        1. **相对导入**（前导点，``.sib`` / ``..services.user``）：按调用方包路径
           折算 —— level=1 是当前包，level=2 往上一级；只有点（``from . import x``）
           解析到当前包的 ``__init__``。
        2. **绝对名**：仓库根优先（PEP 328），包内同名文件仅在根无匹配时兜底。
        3. **剥包根前缀**：``--src app`` + ``from app.services.x import Y`` 这类
           **包名比解析根多一层**的写法，前两种都必然落空，不剥离就退化成
           "整仓 import 全部解析失败"。只在首段**恰好等于分析根目录名**时剥离，
           避免 ``import os.path`` 被误解成本地 ``path.py``。
        """
        if not dotted:
            return None
        # TS/JS 的 specifier 是**斜杠路径**：相对（``./x`` ``../x``）或别名（``@/x``）。
        # 这两种形态进不了 Python 的点分逻辑 —— 它们的点只在文件名里（``./a.js``），
        # 按点切会截成空串，于是整片前端 import 都落 External（实测 3411 条边全丢）。
        if self._is_js_specifier(dotted):
            return self._resolve_js_specifier(dotted, from_module=from_module)
        base = from_module.rsplit("/", 1)[0] if "/" in from_module else ""

        if dotted.startswith("."):
            level = len(dotted) - len(dotted.lstrip("."))
            rest = dotted[level:].strip(".")
            for _ in range(level - 1):
                base = base.rsplit("/", 1)[0] if "/" in base else ""
            if not rest:
                # 只有点：当前（或上 N 级）包的 __init__
                return self._probe_candidate(f"{base}/__init__.py") if base else None
            return self._probe_candidate(
                "/".join([base, *rest.split(".")]) if base else rest.replace(".", "/")
            )

        parts = dotted.split(".")
        absolute = "/".join(parts)
        #: 包内同名候选：只在"仓库根没有这个绝对名"时兜底。反过来（包内优先）
        #: 会把 ``from utils import f`` 悄悄连到 ``pkg/utils.py``，与 Python 3
        #: 的绝对导入语义相反，且置信度仍是 1.0 —— 错误边无人察觉。
        sibling = "/".join([base, *parts]) if base else None

        root_hit = self._probe_candidate(absolute)
        # stdlib 名（``os`` / ``json`` …）即使与根目录同名也不剥离：
        # ``import os.path`` 剥成 ``path`` 会误连仓库里的 ``path.py``。
        # 取舍是"宁可不解析也不误连"——误连会把 stdlib 依赖画成内部依赖。
        if (
            root_hit is None
            and self.root_package
            and len(parts) > 1
            and parts[0] == self.root_package
            and parts[0] not in sys.stdlib_module_names
        ):
            root_hit = self._probe_candidate("/".join(parts[1:]))
        if root_hit:
            self._note_shadowed(sibling, root_hit, dotted, from_module)
            return root_hit
        return self._probe_candidate(sibling) if sibling else None

    def _note_shadowed(self, sibling: str | None, root_hit: str, dotted: str, from_module: str) -> None:
        """根候选与包内同名候选并存时报一条诊断 —— 挑了哪个要说出来。"""
        if not sibling:
            return
        sibling_hit = self._probe_candidate(sibling)
        if not sibling_hit or sibling_hit == root_hit:
            return
        marker = (from_module, dotted)
        if marker in self._shadowed_reported:
            return
        self._shadowed_reported.add(marker)
        self.diagnostics.append(
            Diagnostic(
                level="warning",
                code="IMPORT_SHADOWED",
                message=(
                    f"绝对导入 {dotted!r} 命中仓库根 {root_hit}，包内同名文件 "
                    f"{sibling_hit} 被忽略（Python 3 绝对导入语义）"
                ),
                where=from_module,
            )
        )

    def _resolve_submodule(self, module: str | None, name: str | None, *, from_module: str) -> str | None:
        """``from <module> import <name>`` 里的 ``name`` 若是子模块文件，返回其路径。

        TS/JS 例外：``import {Foo} from './x'`` 的 ``Foo`` 是 ``x`` 的**导出符号**，
        不是子模块 —— 按点拼出 ``./x.Foo`` 会误连到 ``x/Foo.ts``（若恰好存在）。
        """
        if not name:
            return None
        if module and "/" in module:
            return None
        dotted = f"{module or ''}{name}" if (module or "").endswith(".") or not module else f"{module}.{name}"
        return self._resolve_module_path(dotted, from_module=from_module)

    # ── TS/JS specifier 解析 ─────────────────────────────────────────────
    def _is_js_specifier(self, spec: str) -> bool:
        """是否是 TS/JS 风格：相对导入、含斜杠，或命中别名表（精确别名可以不带斜杠）。

        Python 点分名不含 ``/``，也不会有 ``@`` 前缀，所以这个判断不会误伤。
        """
        if spec.startswith(("./", "../")) or "/" in spec:
            return True
        return self._match_ts_alias(spec) is not None

    def _resolve_js_specifier(self, spec: str, *, from_module: str) -> str | None:
        """JS 风格 specifier → 仓库内文件；解析不到返回 None（由调用方落 External）。"""
        if spec.startswith(("./", "../")):
            base = posixpath.dirname(from_module)
            return self._probe_js(posixpath.normpath(posixpath.join(base, spec)))
        mapped = self._match_ts_alias(spec)
        return self._probe_js(mapped) if mapped else None

    def _match_ts_alias(self, spec: str) -> str | None:
        """按 ``ts_aliases`` 做**最长前缀**映射；未命中返回 None。

        带 ``*`` 的规则才做前缀匹配 —— 精确别名 ``@config`` 绝不能吞掉 ``@configX``
        （那会把用户写的另一个包错连进仓库）。
        """
        best: tuple[int, str] | None = None
        for pattern, target in self.ts_aliases.items():
            wildcard = pattern.endswith("*")
            prefix = pattern[:-1] if wildcard else pattern
            if wildcard:
                if not spec.startswith(prefix):
                    continue
                rest = spec[len(prefix) :]
                candidate = target.replace("*", rest)
            else:
                if spec != prefix:
                    continue
                candidate = target
            if best is None or len(prefix) > best[0]:
                best = (len(prefix), candidate)
        return best[1] if best else None

    def _probe_js(self, path: str) -> str | None:
        """路径 → 实际文件：原样 → 补扩展名 → 目录下的 ``index.*``。"""
        if not path:
            return None
        base = posixpath.normpath(path)
        if base in self._files:  # allowImportingTsExtensions：specifier 自带扩展名
            return base
        for suffix in JS_SUFFIXES:
            if base + suffix in self._files:
                return base + suffix
        for index in JS_INDEXES:
            if base + index in self._files:
                return base + index
        return None

    def _probe_candidate(self, candidate: str) -> str | None:
        """候选路径 → 实际文件（带语言后缀 / 包 __init__ / 裸路径）。"""
        for suffix in (".py", "/__init__.py", ".ts", ".js", ".go"):
            if candidate + suffix in self._files:
                return candidate + suffix
        if candidate in self._files:
            return candidate
        return None

    def _owner_module(self, sid: str) -> str | None:
        """symbol id → 所属模块路径；非 ``sym::`` 形态（如未解析的 ``ref::``）
        一律返回 None，绝不把别名标签误当模块路径。"""
        if not sid.startswith("sym::"):
            return None
        module_path, _, _q = sid[len("sym::") :].rpartition("::")
        return module_path or None

    def _caller_id(self, file: FileModel, call) -> str:
        """调用起点：能定位到所在符号就用符号，否则落到模块。"""
        if call.scope:
            sid = symbol_id(file.path, call.scope)
            if sid in self.nodes:
                return sid
        for symbol in file.symbols:
            if symbol.lineno <= call.lineno <= symbol.endlineno and symbol.kind != SymbolKind.CLASS:
                return symbol_id(file.path, symbol.qualname)
        return module_id(file.path)

    def _is_local(self, name: str, module_path: str) -> bool:
        return name in self._module_symbols.get(module_path, {})

    def _lookup_name(self, name: str, module_path: str) -> str | None:
        """按 import 别名 → 本模块 → 同包 → 全局（唯一）的顺序解析名字。

        别名表命中但值仍是 ``ref::``（re-export 未穿透）时返回 None：往下查
        本模块/同包可能绑到一个**不相干的同名符号**，比查不到更糟。
        """
        alias_hit = self._exports.get(module_path, {}).get(name)
        if alias_hit:
            return None if alias_hit.startswith("ref::") else alias_hit
        local = self._module_symbols.get(module_path, {}).get(name)
        if local:
            return local
        package = module_path.rsplit("/", 1)[0] if "/" in module_path else ""
        for path in self._files:
            if not package or path.rsplit("/", 1)[0] != package:
                continue
            hit = self._module_symbols.get(path, {}).get(name)
            if hit:
                return hit
        candidates = self._global_names.get(name, [])
        return candidates[0] if len(candidates) == 1 else None

    def _guess_symbol(self, name: str, module_path: str) -> str | None:
        """同名猜测（无接收者类型信息时）：**唯一**候选才绑（CONF_NAME_GUESS）。

        多个同名候选时返回 None 并记 ``CALL_AMBIGUOUS`` 诊断 —— 挑第一个就是
        随机绑定，README 红线"绝不随机绑定"在这里同样适用（§0.3 非目标 3）。
        """
        candidates = self._global_names.get(name, [])
        if not candidates:
            return None
        if len(candidates) > 1:
            if self._ambiguous_reported < 100:
                self._ambiguous_reported += 1
                self.diagnostics.append(
                    Diagnostic(
                        level="info",
                        code="CALL_AMBIGUOUS",
                        message=f"同名符号 {name!r} 有 {len(candidates)} 个候选，无接收者类型信息，不绑定: "
                        + ", ".join(sorted(candidates)),
                        where=module_path,
                    )
                )
            return None
        return candidates[0]

    def _layer_of(self, path: str) -> str:
        """按配置匹配分层（§3.2 的 paths 匹配）；未命中给虚拟层 + 诊断。

        算法与 :meth:`CodewikiConfig.layer_of` **同一份**（codewiki.layering），
        否则 CLI 与引擎会对同一个模块给出不同层。
        """
        candidates = [
            (layer.get("id") or f"layer::{layer.get('name', '?')}", layer.get("paths", []))
            for layer in self.layers_cfg
        ]
        return layer_of(path, candidates)

    def diagnostics_for_unlayered(self) -> list[Diagnostic]:
        """未分层模块清单（由调用方决定是否合并进诊断列表）。"""
        return [
            Diagnostic(level="warning", code="UNLAYERED_MODULE", message="模块未匹配任何分层", where=n.path)
            for n in self.nodes.values()
            if n.type == NodeType.MODULE and n.layer_id == UNLAYERED_LAYER_ID and n.path
        ]


def build_graph(
    files: list[FileModel],
    *,
    min_confidence: float = 0.5,
    layers: list[dict] | None = None,
    root_package: str | None = None,
    ts_aliases: dict[str, str] | None = None,
) -> BuildResult:
    """便捷入口：由 FileModel 列表构建图谱。"""
    builder = GraphBuilder(
        min_confidence=min_confidence,
        layers=layers,
        root_package=root_package,
        ts_aliases=ts_aliases,
    )
    result = builder.build(files)
    result.diagnostics.extend(builder.diagnostics_for_unlayered())
    return result


__all__ = ["BuildResult", "GraphBuilder", "build_graph", "module_id", "symbol_id", "external_id"]
