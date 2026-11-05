"""``codewiki.toml`` 配置加载与校验（设计文稿 §3）。

独立性核心：**彻底废弃硬编码** —— 任意项目只放一个 TOML 即可完成扫描、建模、
出图、出文档（§0.2 目标 1）。

三条纪律（§3.3）：

1. 用 ``tomllib`` 读取（3.11+ 标准库），Pydantic v2 做 schema 校验；
2. 校验错误必须带**文件行列号 + 修复建议**（否则用户面对一个"字段无效"无从下手）；
3. 导出 JSON Schema 供编辑器补全。
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from codewiki.layering import DEFAULT_MAX_GROUP, directory_groups, layer_of

_EXTRA_FORBID = ConfigDict(extra="forbid")

CONFIG_FILENAME = "codewiki.toml"
SCHEMA_EXPORT_NAME = "codewiki.schema.json"


class ProjectSection(BaseModel):
    """产物语言与展示文案相关（不影响解析逻辑）。"""

    model_config = _EXTRA_FORBID

    name: str | None = None
    lang: str = "zh"


class LanguageSpec(BaseModel):
    """一个语言的解析配置。"""

    model_config = _EXTRA_FORBID

    ext: list[str] = Field(default_factory=list)
    analyzer: str = "auto"


class AnalysisSection(BaseModel):
    """分析行为（§3.1：置信度下限、单图节点上限）。"""

    model_config = _EXTRA_FORBID

    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    max_nodes: int = Field(default=300, gt=0)
    exclude: list[str] = Field(default_factory=list)
    god_node_top_n: int = Field(default=10, gt=0)
    god_node_cross_layer_ratio: float = Field(default=0.5, ge=0.0, le=1.0)
    ts_aliases: dict[str, str] = Field(default_factory=dict)
    root_package: str | None = None
    """Python 包名与分析根目录名不一致时手工指定（如 ``--src src`` + ``from app.x import Y`` 填 ``app``）。
    不填则用目录名，并从 import 统计里自动推断（见 ``ROOT_PACKAGE_INFERRED``）。"""


class LayerSpec(BaseModel):
    """一个架构层（§3.1 的 architecture.layers 元素）。"""

    model_config = _EXTRA_FORBID

    name: str
    paths: list[str] = Field(default_factory=list)
    color: str | None = None
    order: int | None = None

    @property
    def id(self) -> str:
        """层 ID（与路径、顺序无关，保证跨版本稳定）。"""
        return f"layer::{self.name}"


class AnnotationSpec(BaseModel):
    """注解（§3.1 的 annotations 元素；AI 建议合入后也落这里）。

    ``source`` / ``reviewed`` 是**必带**字段（不是装饰）：合并进来的 AI 注解若不
    记这两个字段，下次读配置时就会被当成"人工写且已审阅"，等于把 §十二 的防线
    （AI 注解默认未审阅）悄悄拆掉。
    """

    model_config = _EXTRA_FORBID

    target: str
    label: str | None = None
    note: str = ""
    source: str = "human"
    reviewed: bool = False


RULE_TARGET_PREFIXES = ("layer:", "dir:", "module:")


class RuleSpec(BaseModel):
    """一条架构规则（``[[rules]]``，阶段一 1.1）。

    ``from`` 是 Python 关键字：必须 ``alias="from"`` 才能让 TOML 里的
    ``from = "layer:ui"`` 被接受（否则报"未知字段"，而这是本段最主要的写法）。
    同时开 ``populate_by_name``，Python 侧构造时 ``from_=`` 也认。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    """稳定身份：基线按它记账，**改 id = 换一条新规则**（存量会被当成"全量新增"）。"""

    kind: Literal["forbid-dependency", "forbid-cycle"]

    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    scope: str | None = None
    """``forbid-cycle`` 的环搜索范围（形如 ``layer:ui``）。"""

    edges: list[Literal["IMPORTS", "CALLS"]] = Field(default_factory=lambda: ["IMPORTS"])
    """``forbid-dependency`` 看哪些边。默认**只看 IMPORTS**：Python 里调用必然先 import，
    两条都算等于一处违规报两次；CALLS 回答的是另一个问题（绕经 re-export 的调用），
    要它请显式写出来。``forbid-cycle`` 上写这个字段是无效配置（环看全类型边）。"""

    type_only: Literal["warn", "ignore", "error"] = "warn"
    """仅在 ``TYPE_CHECKING`` 下的依赖如何处置：默认计入但降级为 warning。

    与环检测的口径**故意不同**（那里直接跳过）：环算的是运行时可达性，类型依赖
    确实不成环；规则算的是耦合度，类型依赖也是真实耦合。"""

    severity: Literal["error", "warning"] = "error"
    """``error`` 才影响退出码；``warning`` 只出现在报告与 JSON 里。"""

    reason: str = ""
    """写进报告与 PR 注释的"为什么" —— 没有它，下一个人只会把违例塞进基线。"""

    @property
    def edges_explicit(self) -> bool:
        """是否**显式**写了 ``edges``（默认值不算）。用于拒绝 ``forbid-cycle`` 上的无效配置。"""
        return "edges" in self.model_fields_set


class AiSection(BaseModel):
    """AI 增强（整段缺失等同关闭，§3.1；阶段二新增字段一律有默认值）。"""

    model_config = _EXTRA_FORBID

    enabled: bool | None = None
    base_url: str | None = None
    model: str | None = None
    budget_tokens: int | None = None
    max_retries: int = Field(default=2, ge=0)
    timeout_s: float = Field(default=60.0, gt=0)
    top_n: int = Field(default=30, gt=0)
    write_back: str = "review-file"
    cache_include_topology: bool = True
    l2_validate: bool = False


def _check_rule_target(value: str, field: str, where: str, layer_names: list[str]) -> None:
    """校验规则的 ``from`` / ``to`` / ``scope``：前缀合法、层名存在。"""
    prefix = next((p for p in RULE_TARGET_PREFIXES if value.startswith(p)), None)
    if prefix is None:
        raise ValueError(
            f"{where} 的 {field} = {value!r} 缺少前缀：只认 layer: / dir: / module:"
            "（layer 指向分层名，dir / module 指向仓库内路径）"
        )
    body = value[len(prefix) :].strip()
    if not body:
        raise ValueError(f"{where} 的 {field} = {value!r} 前缀后面是空的")
    if prefix != "layer:":
        return
    if body not in layer_names:
        available = "、".join(sorted(layer_names)) or "（当前没有声明任何层）"
        raise ValueError(f"{where} 的 {field} = {value!r} 引用了不存在的层；当前可用的层：{available}")


def _validate_rules(rules: list[RuleSpec], layer_names: list[str]) -> None:
    """规则之间的校验：重名、字段与 ``kind`` 不匹配、空条件。

    抛出 ``ValueError`` → pydantic 包成 ``ValidationError``（``loc=("rules",)``）→
    ``load_config`` 转成带修复建议的 :class:`ConfigError`。**消息必须带规则 id**：
    规则一多，"第 3 条错了"等于让用户自己数。
    """
    seen: dict[str, int] = {}
    for index, rule in enumerate(rules, start=1):
        where = f"规则 {rule.id!r}（第 {index} 条）"
        if not rule.id.strip():
            raise ValueError(f"第 {index} 条规则的 id 不能为空")
        if rule.id in seen:
            raise ValueError(
                f"规则 id 重复：{rule.id!r}（第 {seen[rule.id]} 条与第 {index} 条）"
                " —— 基线按 id 记账，重名等于两条规则共用一本账"
            )
        seen[rule.id] = index

        if not rule.edges:
            raise ValueError(f"{where} 的 edges 是空列表：一条什么都不看的规则，跟没写一样却占着一个 id")

        if rule.kind == "forbid-cycle":
            if rule.edges_explicit:
                raise ValueError(
                    f"{where} 是 forbid-cycle，不该写 edges —— "
                    "环看的是全类型边，这里的 edges 不会被使用（写上去等于让人以为限定生效了）"
                )
            if not rule.scope:
                raise ValueError(f"{where} 是 forbid-cycle，必须给 scope（环的搜索范围，如 layer:ui）")
            _check_rule_target(rule.scope, "scope", where, layer_names)
            continue

        if not rule.from_ or not rule.to:
            missing = "、".join(name for name, value in (("from", rule.from_), ("to", rule.to)) if not value)
            raise ValueError(f"{where} 是 forbid-dependency，必须同时给 from 与 to（缺 {missing}）")
        _check_rule_target(rule.from_, "from", where, layer_names)
        _check_rule_target(rule.to, "to", where, layer_names)


class CodewikiConfig(BaseModel):
    """``codewiki.toml`` 的完整模型。"""

    model_config = _EXTRA_FORBID

    project: ProjectSection = Field(default_factory=ProjectSection)
    languages: dict[str, LanguageSpec] = Field(default_factory=dict)
    analysis: AnalysisSection = Field(default_factory=AnalysisSection)
    architecture: ArchitectureSection = Field(default_factory=lambda: ArchitectureSection())
    annotations: list[AnnotationSpec] = Field(default_factory=list)
    ai: AiSection = Field(default_factory=AiSection)
    rules: list[RuleSpec] = Field(default_factory=list)
    """架构规则（``[[rules]]``）。空表示"没有声明"，与"声明了但没违例"是两回事 ——
    报告里必须能区分，否则读者无法判断这套闸门到底立没立。"""

    @model_validator(mode="after")
    def _check_rules_against_layers(self) -> CodewikiConfig:
        """规则与分层的交叉校验（pydantic 单字段校验表达不了）。

        ``dir:`` / ``module:`` 是否命中模块**不在这里判**：配置加载时还没有模块表。
        那条留到 ``check`` 里用真实数据包判（见 ``RULE_TARGET_UNKNOWN``）。
        """
        _validate_rules(self.rules, [layer.name for layer in self.architecture.layers])
        return self

    @property
    def title(self) -> str:
        return self.project.name or "codewiki"

    def layer_of(self, rel_path: str) -> str:
        """按 paths 前缀匹配分层（算法来自 :mod:`codewiki.layering`，与引擎共用）。"""
        return layer_of(rel_path, [(layer.id, layer.paths) for layer in self.architecture.layers])

    def layers_as_nodes(self) -> list[dict]:
        """层 → 数据包里的 layers 段（按 order 排序，无 order 时按声明顺序）。"""
        ordered = sorted(
            self.architecture.layers, key=lambda layer: layer.order if layer.order is not None else 10_000
        )
        return [
            {"id": layer.id, "name": layer.name, "color": layer.color, "order": index}
            for index, layer in enumerate(ordered)
        ]


class ArchitectureSection(BaseModel):
    """架构分层定义（§3.1 的 architecture）。"""

    model_config = _EXTRA_FORBID

    layers: list[LayerSpec] = Field(default_factory=list)
    auto_layers: bool = Field(
        default=True,
        description="未声明任何层时，按目录结构推断分层（false = 全部归入虚拟层 _unlayered）",
    )


CodewikiConfig.model_rebuild()


def derive_layer_specs(
    module_paths: list[str], *, max_group: int | None = DEFAULT_MAX_GROUP
) -> list[LayerSpec]:
    """未声明分层时的兜底：按目录结构推断层（算法见 :func:`codewiki.layering.directory_groups`）。

    只有一个组（整个仓库平铺在一个目录里）时返回空 —— 一个"层"包住全部模块没有
    信息量，退回虚拟层 ``_unlayered`` 更诚实。

    ``max_group`` 与 ``suggest-layers`` 用同一个默认值：超过就继续按子目录下钻，
    免得一个 90 个模块的包顶着一层的名义留在图上。下钻不动（平铺目录）时它就是一
    层 —— 兜底分层没有图，做不了社区发现，这一点由 ``LAYERS_DERIVED`` 的文案说明。
    """
    groups = directory_groups(module_paths, max_group=max_group)
    if len(groups) < 2:
        return []
    return [LayerSpec(name=group.name, paths=group.prefixes or ["."]) for group in groups]


class ConfigError(Exception):
    """配置加载失败（带文件位置与修复建议）。"""

    def __init__(
        self, message: str, *, path: str | None = None, line: int | None = None, hint: str | None = None
    ) -> None:
        self.path = path
        self.line = line
        self.hint = hint
        location = f" ({path}:{line})" if path and line else (f" ({path})" if path else "")
        super().__init__(f"{message}{location}{chr(10)}  {hint}" if hint else f"{message}{location}")


_TOML_POS = re.compile(r"(?:at\s+line|line)\s*(\d+)", re.I)

_TOML_AT_END = re.compile(r"at end of document", re.I)


def _toml_error_line(exc: tomllib.TOMLDecodeError, text: str | None = None) -> int | None:
    """取 TOML 报错行号；确实取不到才返回 ``None``，不编一个出来。

    三条腿，按可靠性排序，缺一条就会在某个版本上掉行号：

    1. ``lineno`` 属性（3.14+）；
    2. 消息里的 ``(at line N, column M)``（3.11–3.13 唯一的来源，CI 上
       ``test (3.11)`` 正是因缺这条而红）；
    3. ``at end of document`` ⇒ 文件末尾，也就是最后一行。3.14 对同一份文本
       报的就是最后一行，这里补上才能让跨版本口径一致。
    """
    lineno = getattr(exc, "lineno", None)
    if isinstance(lineno, int) and lineno >= 1:
        return lineno

    message = str(exc)
    match = _TOML_POS.search(message)
    if match:
        return int(match.group(1))
    if text is not None and _TOML_AT_END.search(message):
        return text.count(chr(10)) + 1
    return None


def load_config(path: Path) -> CodewikiConfig:
    """读取并校验配置；失败抛 :class:`ConfigError`（含行列号与修复建议）。"""
    if not path.exists():
        return CodewikiConfig()

    text = path.read_text(encoding="utf-8")
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            "TOML 语法错误",
            path=str(path),
            line=_toml_error_line(exc, text),
            hint="用支持 TOML 的编辑器检查缩进与引号；或 `codewiki init` 重新生成模板",
        ) from exc

    try:
        return CodewikiConfig.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(p) for p in first.get("loc", ())) or "<root>"
        message = str(first.get("msg", "")).removeprefix("Value error, ")
        prefix = "" if location == "<root>" else f"配置项 `{location}` 无效："
        raise ConfigError(
            f"{prefix}{message}",
            path=str(path),
            hint=_hint_for(first),
        ) from exc


def _hint_for(error: dict) -> str:
    """把 Pydantic 错误翻成人能执行的修复建议。"""
    kind = error.get("type")
    location = ".".join(str(p) for p in error.get("loc", ())) or "<root>"
    if kind == "missing":
        return f"补上 `{location}`；不确定取值可参考 codewiki.toml.example"
    if kind in ("float_parsing", "int_parsing"):
        return f"`{location}` 需要数字（不要加引号）"
    if kind == "greater_than_equal":
        return f"`{location}` 低于允许下限（见 error 中的 limit）"
    if kind == "extra_forbidden":
        return f"删掉未识别的 `{location}`（可能是改名后的旧字段）"
    if kind == "value_error":
        return (
            "按上面的提示修正该条规则；`[[rules]]` 的写法与可用层名见 codewiki.toml.example，"
            "层名必须与 `[[architecture.layers]]` 的 name 一致"
        )
    return "对照导出的 JSON Schema（wiki-output/data/codewiki.schema.json）检查字段名与类型"


def config_schema_json() -> str:
    """导出配置 JSON Schema（供编辑器补全）。"""
    return json.dumps(CodewikiConfig.model_json_schema(), ensure_ascii=False, indent=2) + "\n"


__all__ = [
    "CONFIG_FILENAME",
    "RULE_TARGET_PREFIXES",
    "AiSection",
    "AnalysisSection",
    "AnnotationSpec",
    "CodewikiConfig",
    "ConfigError",
    "LanguageSpec",
    "LayerSpec",
    "ProjectSection",
    "RuleSpec",
    "config_schema_json",
    "derive_layer_specs",
    "load_config",
]
