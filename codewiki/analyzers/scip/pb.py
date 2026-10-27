"""极简 protobuf 编解码（只解 SCIP 需要的那点东西）。

为什么自己写而不用 `protobuf` / 现成的 `scip` 包：

1. PyPI 上名为 `scip` 的包是**细胞图像处理流水线**（GPL、依赖 dask/cellpose），
   与 Sourcegraph SCIP 毫无关系 —— 照着名字写依赖会把一个无关且带 GPL 的包拖进
   依赖树（这条在设计 v2.5 合并时已订正）；
2. 我们需要的只是"读一个扁平的 Index → Document → Occurrence/SymbolInformation"，
   完整 protobuf 运行时（含代码生成、protoc 工具链）为这点需求引入的重量不划算；
3. 官方 `.scip` 是 protobuf **二进制**，但我们的映射层只需要字段号与两种线型。

支持范围刻意做窄：varint / 64bit-fixed / length-delimited / 32bit-fixed，
**不支持** group（已废弃）与 zigzag 之外的特殊编码（SCIP 用不到）。
"""

from __future__ import annotations

from dataclasses import dataclass

WIRE_VARINT = 0
WIRE_64 = 1
WIRE_LEN = 2
WIRE_32 = 5


@dataclass(frozen=True)
class Field:
    """一个字段（原始值；嵌套消息保留字节以便按需再解）。"""

    number: int
    wire: int
    value: int | bytes


def decode_message(data: bytes) -> dict[int, list[Field]]:
    """解一层消息：``{字段号: [Field, ...]}``（重复字段保留顺序）。"""
    out: dict[int, list[Field]] = {}
    pos = 0
    end = len(data)
    while pos < end:
        key, pos = _read_varint(data, pos)
        number, wire = key >> 3, key & 0x07
        if wire == WIRE_VARINT:
            value, pos = _read_varint(data, pos)
        elif wire == WIRE_LEN:
            length, pos = _read_varint(data, pos)
            value = data[pos : pos + length]
            pos += length
        elif wire == WIRE_64:
            value = int.from_bytes(data[pos : pos + 8], "little")
            pos += 8
        elif wire == WIRE_32:
            value = int.from_bytes(data[pos : pos + 4], "little")
            pos += 4
        else:
            raise ValueError(f"不支持的 wire type: {wire}（字段 {number}）")
        out.setdefault(number, []).append(Field(number, wire, value))
    return out


def encode_message(fields: list[tuple[int, int, int | bytes]]) -> bytes:
    """编码一层消息（`fields` = ``[(字段号, wire, 值)]``）。

    主要用于**生成测试样本**：真实 `.scip` 由索引器产出，我们只需要能造出
    结构等价的字节来验证映射逻辑。
    """
    out = bytearray()
    for number, wire, value in fields:
        out += _write_varint((number << 3) | wire)
        if wire == WIRE_VARINT:
            out += _write_varint(int(value))  # type: ignore[arg-type]
        elif wire == WIRE_LEN:
            raw = value if isinstance(value, bytes) else str(value).encode("utf-8")
            out += _write_varint(len(raw))
            out += raw
        elif wire == WIRE_64:
            out += int(value).to_bytes(8, "little")  # type: ignore[arg-type]
        elif wire == WIRE_32:
            out += int(value).to_bytes(4, "little")  # type: ignore[arg-type]
        else:
            raise ValueError(f"不支持的 wire type: {wire}")
    return bytes(out)


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("varint 越界（样本可能不完整）")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint 过长")


def _write_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def encode_packed_ints(values: list[int]) -> bytes:
    """编码 packed repeated int32（SCIP 的 ``range`` 就是这种：一串 varint，
    **没有**字段键）。

    踩过的坑：把它当成嵌套消息编码（先写 0x08 键）会让解码出的第一个值变成 8，
    行号因此串行 —— 样本里所有 occurrence 都变成第 9 行。
    """
    out = bytearray()
    for value in values:
        out += _write_varint(value & 0xFFFFFFFF if value >= 0 else value + (1 << 32))
    return bytes(out)


def packed_ints(field: Field) -> list[int]:
    """解 packed repeated int32（SCIP 的 ``range`` 就是这种）。"""
    if not isinstance(field.value, bytes):
        return [int(field.value)]
    values: list[int] = []
    pos = 0
    data = field.value
    while pos < len(data):
        value, pos = _read_varint(data, pos)
        values.append(_zigzag_or_plain(value))
    return values


def _zigzag_or_plain(value: int) -> int:
    """SCIP 的 range 用 int32（负数表示"相对上一行"），按 protobuf int32 语义还原。"""
    return value - (1 << 32) if value >= (1 << 31) else value


def first(fields: dict[int, list[Field]], number: int) -> Field | None:
    values = fields.get(number)
    return values[0] if values else None


def text(field: Field | None) -> str | None:
    if field is None:
        return None
    raw = field.value
    return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)


__all__ = [
    "Field",
    "WIRE_32",
    "WIRE_64",
    "WIRE_LEN",
    "WIRE_VARINT",
    "decode_message",
    "encode_message",
    "encode_packed_ints",
    "first",
    "packed_ints",
    "text",
]
