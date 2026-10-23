"""``.scip`` → ``FileModel`` 映射（§4.6.2）。

准入口径（设计第十一节）：**以固定 `.scip` 样本映射全绿为准入**，真实索引器
联调属于发布前手工检查。所以这里的实现只做"读结构 → 产事实"，不探测、不下载
索引器、不猜测字段。

映射规则（SCIP → 我们的事实模型）：

* ``Document`` → 一个 :class:`FileModel`（path / language）；
* ``SymbolInformation`` → ``SymbolFact``（按 symbol 后缀判 kind：``#``=方法、
  ``.``=字段、``:``=类型/类，其余为函数）；
* ``Occurrence`` 按 ``symbol_roles`` 分派：
  - ``Definition(0x1)`` → 符号定义（行号取 range[0]）；
  - ``Import(0x2)`` → ``ImportFact``（module 取 symbol 的 descriptor 部分）；
  - 其余读/写访问 → ``CallFact``（callee 取 symbol 名字，receiver 用 enclosing
    符号，**跨文件消歧仍交给引擎层**）。

``symbol`` 字符串形如 ``scip-python python pkg/mod 1.0 Service#run()``，
拆分按 SCIP 规范：``<scheme> <manager> <package> <version> <descriptor>``。
"""
from __future__ import annotations

import hashlib

from codewiki.analyzers.scip import pb
from codewiki.models import CallFact, FileModel, ImportFact, Provenance, SymbolFact

BACKEND = "scip"

ROLE_DEFINITION = 0x1
ROLE_IMPORT = 0x2

INDEX_FIELD_DOCUMENTS = 4
DOC_FIELD_LANGUAGE = 1
DOC_FIELD_PATH = 2
DOC_FIELD_OCCURRENCES = 3
DOC_FIELD_SYMBOLS = 4
OCC_FIELD_RANGE = 1
OCC_FIELD_SYMBOL = 2
OCC_FIELD_ROLES = 4
OCC_FIELD_ENCLOSING_RANGE = 6
SYMINFO_FIELD_SYMBOL = 1
SYMINFO_FIELD_DOCUMENTATION = 2
SYMINFO_FIELD_DISPLAY_NAME = 5


def map_index(data: bytes) -> list[FileModel]:
    """把一份 SCIP 索引映射成 FileModel 列表（一个 Document 一个）。"""
    index = pb.decode_message(data)
    models: list[FileModel] = []
    for doc_field in index.get(INDEX_FIELD_DOCUMENTS, []):
        if not isinstance(doc_field.value, bytes):
            continue
        models.append(_map_document(pb.decode_message(doc_field.value)))
    return models


def _map_document(doc: dict[int, list[pb.Field]]) -> FileModel:
    path = pb.text(pb.first(doc, DOC_FIELD_PATH)) or ""
    language = pb.text(pb.first(doc, DOC_FIELD_LANGUAGE)) or ""

    symbols: list[SymbolFact] = []
    imports: list[ImportFact] = []
    calls: list[CallFact] = []

    kind_by_symbol: dict[str, str] = {}
    for field in doc.get(DOC_FIELD_SYMBOLS, []):
        if not isinstance(field.value, bytes):
            continue
        info = pb.decode_message(field.value)
        symbol = pb.text(pb.first(info, SYMINFO_FIELD_SYMBOL)) or ""
        display = pb.text(pb.first(info, SYMINFO_FIELD_DISPLAY_NAME)) or symbol_name(symbol)
        documentation = pb.text(pb.first(info, SYMINFO_FIELD_DOCUMENTATION))
        kind = kind_of(symbol)
        kind_by_symbol[symbol] = kind
        symbols.append(
            SymbolFact(
                name=display or symbol,
                qualname=display or symbol,
                kind=kind,
                lineno=0,
                endlineno=0,
                docstring=_first_line(documentation),
            )
        )

    for field in doc.get(DOC_FIELD_OCCURRENCES, []):
        if not isinstance(field.value, bytes):
            continue
        occ = pb.decode_message(field.value)
        symbol = pb.text(pb.first(occ, OCC_FIELD_SYMBOL)) or ""
        roles_field = pb.first(occ, OCC_FIELD_ROLES)
        roles = int(roles_field.value) if roles_field and isinstance(roles_field.value, int) else 0
        range_field = pb.first(occ, OCC_FIELD_RANGE)
        line = pb.packed_ints(range_field)[0] + 1 if range_field else 0

        if roles & ROLE_IMPORT:
            imports.append(ImportFact(module=module_like(symbol), lineno=line, kind="import"))
            continue
        if roles & ROLE_DEFINITION:
            _apply_definition(symbols, symbol, line)
            continue
        enclosing = pb.first(occ, OCC_FIELD_ENCLOSING_RANGE)
        caller = _enclosing_label(enclosing) if enclosing else None
        calls.append(CallFact(callee=symbol_name(symbol), scope=caller, lineno=line))

    return FileModel(
        path=path,
        language=language,
        hash=hashlib.sha256(path.encode("utf-8")).hexdigest()[:16],
        imports=imports,
        symbols=symbols,
        calls=calls,
        provenance=Provenance(backend=BACKEND, version="scip-index"),
    )


def _apply_definition(symbols: list[SymbolFact], symbol: str, line: int) -> None:
    """把定义位置补到已有符号上（符号信息表里没有行号）。"""
    name = symbol_name(symbol)
    for fact in symbols:
        if fact.qualname == name or fact.name == name:
            if not fact.lineno:
                fact.lineno = line
                fact.endlineno = line
            return
    symbols.append(
        SymbolFact(name=name, qualname=name, kind=kind_of(symbol), lineno=line, endlineno=line)
    )


def _enclosing_label(field: pb.Field) -> str | None:
    """enclosing_range 是 packed int32（行/列），只取行作为作用域标签。"""
    try:
        values = pb.packed_ints(field)
    except ValueError:
        return None
    return f"line{values[0] + 1}" if values else None


def _first_line(text_value: str | None) -> str | None:
    if not text_value:
        return None
    return text_value.strip().splitlines()[0]


def symbol_name(symbol: str) -> str:
    """``<scheme> <manager> <package> <version> <descriptor>`` → 可读名字。

    SCIP 的 descriptor 用标点编码层级（``:``=命名空间/类、``#``=方法、
    ``.``=字段、``()``=签名）。转成"点分"才能与我们其它后端的 qualname 对齐，
    否则符号表与定义位置对不上（会产出两条同名符号）。
    """
    descriptor = descriptor_of(symbol).rstrip(":")
    body = descriptor.split(":")[-1] if ":" in descriptor else descriptor
    body = body.replace("#", ".")
    body = body.rstrip("().").strip(".")
    return body or descriptor


def descriptor_of(symbol: str) -> str:
    """取 descriptor（第 5 段起；含空格的 descriptor 用反引号包裹）。"""
    parts = symbol.split(" ")
    if len(parts) >= 5:
        return " ".join(parts[4:])
    return symbol


def module_like(symbol: str) -> str:
    """import 的模块名：``pkg/util.py:`` → ``pkg.util``。

    转成点分是因为引擎层的模块解析按点分匹配（与其它后端一致）；保留路径形式
    会让 SCIP 来的 import 全部解析不到。
    """
    descriptor = descriptor_of(symbol).rstrip(":")
    for suffix in (".py", ".ts", ".go", ".tsx"):
        if descriptor.endswith(suffix):
            descriptor = descriptor[: -len(suffix)]
            break
    return descriptor.replace("/", ".")


def kind_of(symbol: str) -> str:
    """按 SCIP descriptor 标点判符号类型。

    判定顺序有讲究：``Service#run().`` 同时以 ``()`` 与 ``.`` 结尾，若先判
    ``.`` 会把它当成字段 —— 先判"含 # 的方法"才对。
    """
    descriptor = descriptor_of(symbol)
    body = descriptor.rstrip(".")
    if body.endswith("()"):
        return "method" if "#" in descriptor else "function"
    if "#" in descriptor:
        return "field"
    if descriptor.endswith(":"):
        return "class"
    return "function"


__all__ = ["BACKEND", "descriptor_of", "kind_of", "map_index", "module_like", "symbol_name"]
