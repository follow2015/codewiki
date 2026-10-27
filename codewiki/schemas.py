"""标准 JSON 数据包 schema（**冻结契约**，设计文稿 §4.4 / §十一 第一阶段第 1 项）。

这是渲染层与 AI 层唯一消费的中间产物（§十三 决策 1）。字段定死之后：

- 新增字段只能加可选（有默认值）；
- 改类型/枚举必须同步 ``tests/golden/packet_schema.json``（该用例逐字段比对，
  改坏了当场红，不需要评审时靠人眼发现）。

``provenance`` 一期即预留（阶段三引入 SCIP 时不必动 schema）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from codewiki import __version__
from codewiki.clock import now_iso
from codewiki.models import (
    SCHEMA_VERSION,
    Edge,
    Node,
    Provenance,
)


class Diagnostic(BaseModel):
    """一条诊断（解析告警 / 环 / 未分层 / God Node 都走这里）。"""

    level: str = Field(description="error | warning | info")
    code: str = Field(description="稳定的诊断码，如 LAYER_CYCLE")
    message: str
    where: str | None = Field(default=None, description="文件:行 或 节点 id")


class Annotation(BaseModel):
    """人工或 AI 的富文本注解（§3.1 的 annotations 段）。"""

    target: str
    label: str | None = None
    note: str
    source: str = Field(default="human", description="human | ai")
    reviewed: bool = False


class LayerNode(BaseModel):
    """架构层（来自 ``codewiki.toml`` 的 architecture.layers）。"""

    id: str
    name: str
    color: str | None = None
    order: int | None = None


class Meta(BaseModel):
    """数据包元信息。"""

    title: str
    schema_version: int = SCHEMA_VERSION
    generated_at: str
    repo_root: str | None = None
    repo_commit: str | None = None
    tool_version: str = __version__
    render_inputs: dict[str, str] = Field(default_factory=dict)
    provenance: dict[str, Provenance] = Field(default_factory=dict)


class DataPacket(BaseModel):
    """标准数据包：六段（§4.4）。"""

    meta: Meta
    layers: list[LayerNode] = Field(default_factory=list)
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    annotations: list[Annotation] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)

    @staticmethod
    def new_meta(
        title: str,
        *,
        repo_root: str | None = None,
        repo_commit: str | None = None,
        generated_at: str | None = None,
    ) -> Meta:
        """构造 meta（UTC ISO 带 Z，与 IPIP 的时间口径一致）。

        ``generated_at`` 可由调用方给定（``--timestamp`` / ``SOURCE_DATE_EPOCH``），
        否则取 :func:`codewiki.clock.now_iso`。可注入是"增量产物 == 全量产物逐字节
        一致"这条验收的前提 —— 时间戳每次不同，字节比对就永远只能在"排除某字段"
        的前提下做。
        """
        return Meta(
            title=title,
            generated_at=generated_at or now_iso(),
            repo_root=repo_root,
            repo_commit=repo_commit,
        )

    def json_schema(self) -> dict:
        """导出 JSON Schema（供编辑器补全与冻结留档）。"""
        return self.__class__.model_json_schema()
