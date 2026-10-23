"""图谱与解析层的冻结数据模型（设计文稿 §2.1 / §2.2 / §2.3 / §4.1）。

本文件是**接口先行**的第一半：节点/边/事实的字段集在此定稿，渲染层、AI 层、
环检测、diff 全部消费它们（文稿 §十三 决策 1：图谱是唯一事实源）。

改动纪律：
- 新增字段只能**加可选**（有默认值），不得改必填字段语义；
- 修改字段类型/枚举值必须同步 `schemas.py` 的 `DataPacket` 与
  `tests/golden/packet_schema.json` 快照（那条用例会当场红）。
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1

CONF_STATIC = 1.0
CONF_NEAR = 0.8
CONF_NAME_GUESS = 0.5
CONF_DYNAMIC = 0.4

DEFAULT_MIN_CONFIDENCE = 0.5


class NodeType(StrEnum):
    """节点类型（§2.1）。"""

    LAYER = "Layer"
    PACKAGE = "Package"
    MODULE = "Module"
    SYMBOL = "Symbol"
    EXTERNAL = "External"


class EdgeType(StrEnum):
    """边类型（§2.2）。"""

    CONTAINS = "CONTAINS"
    IMPORTS = "IMPORTS"
    CALLS = "CALLS"
    INHERITS = "INHERITS"
    LAYER_DEPENDS = "LAYER_DEPENDS"
    USES_DYNAMIC = "USES_DYNAMIC"


class ExternalKind(StrEnum):
    """外部依赖的细分（不展开，避免污染图谱）。"""

    STDLIB = "stdlib"
    THIRD_PARTY = "third_party"
    UNKNOWN = "unknown"


class SymbolKind(StrEnum):
    """符号种类（Symbol 节点的 kind）。"""

    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    CONSTANT = "constant"


_EXTRA_FORBID = ConfigDict(extra="forbid")


class Provenance(BaseModel):
    model_config = _EXTRA_FORBID
    """事实来源（§4.6.2）：哪个后端、什么版本产出的这条事实。

    一期即预留（阶段三引入 SCIP 时不必改 schema）。
    """

    backend: str
    version: str | None = None


class Node(BaseModel):
    """图谱节点（§2.1 必备属性的并集；各类型只用其中一部分）。"""

    model_config = _EXTRA_FORBID

    id: str
    type: NodeType
    name: str | None = None
    qualname: str | None = None
    path: str | None = None
    language: str | None = None
    layer_id: str | None = None
    hash: str | None = None
    kind: str | None = None
    module_id: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    signature: str | None = None
    docstring: str | None = None
    order: int | None = None
    color: str | None = None


class Edge(BaseModel):
    """图谱边（§2.2 关键属性的并集）。"""
    model_config = _EXTRA_FORBID


    source: str
    target: str
    type: EdgeType
    alias: str | None = None
    static: bool = True
    confidence: float = Field(default=CONF_STATIC, ge=0.0, le=1.0)
    weight: int = 1
    evidence: str | None = None




class ImportFact(BaseModel):
    """一条 import 事实。

    ``module`` 用于 ``import pkg.sub`` / ``from pkg import name`` 的模块部分；
    ``name`` 用于 ``from`` 导入的具体名字；``is_star`` 标记 ``from x import *``。
    TS/JS 的 re-export 形态（``export {x} from``）由语言后端映射到本模型。
    """
    model_config = _EXTRA_FORBID


    module: str | None = None
    name: str | None = None
    alias: str | None = None
    is_star: bool = False
    lineno: int
    kind: str = "import"


class SymbolFact(BaseModel):
    """一个符号的定义事实（类/函数/方法/常量）。"""
    model_config = _EXTRA_FORBID


    name: str
    qualname: str
    kind: str
    lineno: int
    endlineno: int
    signature: str | None = None
    docstring: str | None = None
    parent: str | None = None
    decorators: list[str] = Field(default_factory=list)


class CallFact(BaseModel):
    """一处调用（语法上的名字，不在此处做跨文件消歧）。"""
    model_config = _EXTRA_FORBID


    callee: str
    receiver: str | None = None
    lineno: int
    scope: str | None = None


class InheritanceFact(BaseModel):
    """一条继承关系（基类名字同样留到引擎层消歧）。"""
    model_config = _EXTRA_FORBID


    child: str
    base: str


class DynamicFact(BaseModel):
    """一处**动态**引用（§0.3：宁缺毋滥，低置信、虚线呈现）。

    只收"能读出字面目标"的形态（``importlib.import_module("pkg.mod")``、
    ``__import__("pkg.mod")``）。``getattr(obj, name)`` 这类目标不可知的一律
    不收 —— 收了就是编造依赖。
    """
    model_config = _EXTRA_FORBID


    target: str
    lineno: int
    reason: str


class FileModel(BaseModel):
    """单个文件的**原始事实**（RawFacts）。

    约束（§4.1）：Analyzer **不做跨文件解析**，只产出本文件能看到的事实；
    符号消歧与建图统一由引擎层完成。新增语言只需实现 `Analyzer.parse` 返回本模型。
    """
    model_config = _EXTRA_FORBID


    path: str
    language: str
    hash: str
    imports: list[ImportFact] = Field(default_factory=list)
    symbols: list[SymbolFact] = Field(default_factory=list)
    calls: list[CallFact] = Field(default_factory=list)
    inherits: list[InheritanceFact] = Field(default_factory=list)
    dynamic: list[DynamicFact] = Field(default_factory=list)
    exports: list[str] = Field(default_factory=list)
    provenance: Provenance | None = None
