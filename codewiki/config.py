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
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from codewiki.layering import layer_of

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


class AiSection(BaseModel):
    """AI 增强（整段缺失等同关闭，§3.1；阶段二新增字段一律有默认值）。"""
    model_config = _EXTRA_FORBID


    enabled: bool = False
    base_url: str | None = None
    model: str | None = None
    budget_tokens: int | None = None
    max_retries: int = Field(default=2, ge=0)
    timeout_s: float = Field(default=60.0, gt=0)
    top_n: int = Field(default=30, gt=0)
    write_back: str = "review-file"
    cache_include_topology: bool = False
    l2_validate: bool = False


class CodewikiConfig(BaseModel):
    """``codewiki.toml`` 的完整模型。"""
    model_config = _EXTRA_FORBID


    project: ProjectSection = Field(default_factory=ProjectSection)
    languages: dict[str, LanguageSpec] = Field(default_factory=dict)
    analysis: AnalysisSection = Field(default_factory=AnalysisSection)
    architecture: ArchitectureSection = Field(default_factory=lambda: ArchitectureSection())
    annotations: list[AnnotationSpec] = Field(default_factory=list)
    ai: AiSection = Field(default_factory=AiSection)

    @property
    def title(self) -> str:
        return self.project.name or "codewiki"

    def layer_of(self, rel_path: str) -> str:
        """按 paths 前缀匹配分层（算法来自 :mod:`codewiki.layering`，与引擎共用）。"""
        return layer_of(rel_path, [(layer.id, layer.paths) for layer in self.architecture.layers])

    def layers_as_nodes(self) -> list[dict]:
        """层 → 数据包里的 layers 段（按 order 排序，无 order 时按声明顺序）。"""
        ordered = sorted(
            self.architecture.layers, key=lambda layer: (layer.order if layer.order is not None else 10_000)
        )
        return [
            {"id": layer.id, "name": layer.name, "color": layer.color, "order": index}
            for index, layer in enumerate(ordered)
        ]


class ArchitectureSection(BaseModel):
    """架构分层定义（§3.1 的 architecture）。"""
    model_config = _EXTRA_FORBID


    layers: list[LayerSpec] = Field(default_factory=list)


CodewikiConfig.model_rebuild()


class ConfigError(Exception):
    """配置加载失败（带文件位置与修复建议）。"""

    def __init__(self, message: str, *, path: str | None = None, line: int | None = None, hint: str | None = None) -> None:
        self.path = path
        self.line = line
        self.hint = hint
        location = f" ({path}:{line})" if path and line else (f" ({path})" if path else "")
        super().__init__(f"{message}{location}{chr(10)}  {hint}" if hint else f"{message}{location}")


def load_config(path: Path) -> CodewikiConfig:
    """读取并校验配置；失败抛 :class:`ConfigError`（含行列号与修复建议）。"""
    if not path.exists():
        return CodewikiConfig()

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            "TOML 语法错误",
            path=str(path),
            line=getattr(exc, "lineno", None),
            hint="用支持 TOML 的编辑器检查缩进与引号；或 `codewiki init` 重新生成模板",
        ) from exc

    try:
        return CodewikiConfig.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(p) for p in first.get("loc", ())) or "<root>"
        raise ConfigError(
            f"配置项 `{location}` 无效：{first.get('msg')}",
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
    return "对照导出的 JSON Schema（wiki-output/data/codewiki.schema.json）检查字段名与类型"


def config_schema_json() -> str:
    """导出配置 JSON Schema（供编辑器补全）。"""
    return json.dumps(CodewikiConfig.model_json_schema(), ensure_ascii=False, indent=2) + "\n"


__all__ = [
    "CONFIG_FILENAME",
    "AiSection",
    "AnalysisSection",
    "AnnotationSpec",
    "CodewikiConfig",
    "ConfigError",
    "LanguageSpec",
    "LayerSpec",
    "ProjectSection",
    "config_schema_json",
    "load_config",
]
