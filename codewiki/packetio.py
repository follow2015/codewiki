"""数据包的唯一门面：构造、读取、迁移（设计文稿 §数据包与增量分析）。

此前 ``codewiki.json`` 在至少三处各自 ``model_validate_json`` —— 于是"版本"这件事
只在文件里躺着一个数字，没人读它：

- 用新版工具写的包拿来给旧版读，旧版会**静默丢掉它不认识的字段**再照常渲染，
  产物看起来正常、信息已经缺了；
- 未来真的升到 schema 2 时，没有任何地方会走迁移；
- 每处读包的报错文案与"坏包算不算错误"的判断也各不相同。

这里把三件事一次定死：**版本更旧就迁移、版本更新就明确报错、坏包给可读原因**。

构造也放在这里，而不是留在 ``render/jsonio.py``：导入器（``graph/importers``）
要产出数据包，但它不该依赖渲染层 —— 那会形成 ``graph → render`` 的环，让"渲染
层随便改"和"导入层随便改"互相牵连。构造只需要 ``schemas`` 与版本号。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from codewiki import __version__
from codewiki.models import SCHEMA_VERSION
from codewiki.schemas import DataPacket


class PacketError(Exception):
    """数据包不可用（坏 JSON / 结构不符 / 版本问题）。消息面向使用者。"""


class PacketVersionError(PacketError):
    """版本问题（比当前工具新，或缺少可用的迁移路径）。"""


MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {}


def _declared_version(raw: Any) -> int:
    if not isinstance(raw, dict):
        raise PacketError("数据包顶层必须是 JSON 对象")
    meta = raw.get("meta")
    if not isinstance(meta, dict):
        raise PacketError("数据包缺少 meta 段")
    version = meta.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise PacketError("数据包缺少整数 meta.schema_version（无法判断格式版本）")
    return version


def migrate_packet(raw: dict[str, Any], from_version: int) -> dict[str, Any]:
    """逐级迁移到当前版本；缺少任何一级迁移函数就明确报错，不猜。"""
    version = from_version
    while version < SCHEMA_VERSION:
        migrate = MIGRATIONS.get(version)
        if migrate is None:
            raise PacketVersionError(
                f"数据包 schema 版本 {from_version} 过旧，且缺少 {version} → {version + 1} "
                f"的迁移路径（当前支持 {SCHEMA_VERSION}）"
            )
        raw = migrate(raw)
        version += 1
        meta = raw.setdefault("meta", {})
        meta["schema_version"] = version
    return raw


def load_packet(path: Path) -> DataPacket:
    """读取并校验数据包。失败一律抛 :class:`PacketError`（调用方转成退出码 2）。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise PacketError(f"数据包读不出来: {exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PacketError(f"数据包不是合法 JSON: {exc}") from exc

    version = _declared_version(raw)
    if version > SCHEMA_VERSION:
        raise PacketVersionError(
            f"数据包 schema 版本 {version} 比当前工具支持的 {SCHEMA_VERSION} 新，"
            "请升级 codewiki 后再读（不降级解析：会静默丢字段）"
        )
    if version < SCHEMA_VERSION:
        raw = migrate_packet(raw, version)

    try:
        return DataPacket.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - pydantic 的具体异常类型不外露
        raise PacketError(f"数据包结构不符 schema {SCHEMA_VERSION}: {exc}") from exc


def load_packet_or_none(path: Path) -> DataPacket | None:
    """读取失败返回 ``None``（给"上一轮快照坏了就全量"这类调用方用）。"""
    try:
        return load_packet(path)
    except PacketError:
        return None


def packet_from_parts(
    *,
    title: str,
    layers,
    nodes,
    edges,
    annotations=None,
    diagnostics=None,
    provenance=None,
    repo_root: str | None = None,
    repo_commit: str | None = None,
    generated_at: str | None = None,
) -> DataPacket:
    """组装数据包（CLI、导入器与测试共用，避免各写各的 meta）。

    ``generated_at`` 由调用方注入（``--timestamp`` / ``SOURCE_DATE_EPOCH``）；
    不给则取当前时间。可注入是逐字节比对验收的前提，见 ``codewiki.clock``。
    """
    meta = DataPacket.new_meta(title, repo_root=repo_root, repo_commit=repo_commit, generated_at=generated_at)
    meta.tool_version = __version__
    if provenance:
        meta.provenance = provenance
    return DataPacket(
        meta=meta,
        layers=list(layers),
        nodes=list(nodes),
        edges=[e for e in edges if e.confidence >= 0.0],
        annotations=list(annotations or []),
        diagnostics=list(diagnostics or []),
    )


__all__ = [
    "MIGRATIONS",
    "PacketError",
    "PacketVersionError",
    "load_packet",
    "load_packet_or_none",
    "migrate_packet",
    "packet_from_parts",
]
