"""可注入时钟：让产物里的时间戳**可复现**。

动机不是洁癖，而是验收：设计文稿要求"同一份输入的 ``--incremental`` 产物与全量
产物**逐字节**一致"。``generated_at`` 每轮都不同，这条验收就永远做不到 —— 只能
比较"排除 generated_at 之后的部分"，而"排除某个字段"的比对迟早会漏掉真正该比
的东西。

约定：

- 环境变量 ``SOURCE_DATE_EPOCH``（[可复现构建](https://reproducible-builds.org/specs/source-date-epoch/)
  的通行做法）优先，值必须是**整数秒**；
- CLI 的 ``--timestamp`` 优先于环境变量（显式参数 > 环境）；
- 两者都没有才用真实时间。

非法取值**报错而不是静默退回真实时间**：设了 ``SOURCE_DATE_EPOCH`` 却悄悄忽略，
等于让人以为产物可复现、实际不可复现 —— 这种"看起来成功了"的失败最难查。
"""
from __future__ import annotations

import os
from datetime import UTC, datetime

ENV_SOURCE_DATE_EPOCH = "SOURCE_DATE_EPOCH"


class ClockError(ValueError):
    """时间取值不可用（消息面向使用者）。"""


def format_iso(moment: datetime) -> str:
    """统一的落盘格式：UTC + ``Z`` 后缀（与 IPIP 的时间口径一致）。"""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def from_epoch(value: str | int) -> datetime:
    """把 ``SOURCE_DATE_EPOCH`` 的秒数转成 UTC 时间；非法值明确报错。"""
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ClockError(f"{ENV_SOURCE_DATE_EPOCH} 必须是整数秒，收到 {value!r}") from exc
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise ClockError(f"{ENV_SOURCE_DATE_EPOCH}={seconds} 超出可表示范围") from exc


def parse_timestamp(value: str) -> str:
    """解析 ``--timestamp``：接受 ISO-8601（``2026-01-01T00:00:00Z``）或整数秒。

    返回统一格式的字符串 —— 调用方拿到即可直接写进产物，不必再关心格式。
    """
    text = (value or "").strip()
    if not text:
        raise ClockError("--timestamp 不能为空")
    if text.lstrip("-").isdigit():
        return format_iso(from_epoch(text))
    candidate = text.replace("Z", "+00:00")
    try:
        return format_iso(datetime.fromisoformat(candidate))
    except ValueError as exc:
        raise ClockError(
            f"--timestamp 无法解析：{value!r}（用 ISO-8601 如 2026-01-01T00:00:00Z，或整数秒）"
        ) from exc


def now_utc() -> datetime:
    """当前时间；``SOURCE_DATE_EPOCH`` 存在时用它（且校验它合法）。"""
    raw = os.environ.get(ENV_SOURCE_DATE_EPOCH)
    if raw is not None and raw.strip() != "":
        return from_epoch(raw)
    return datetime.now(UTC)


def now_iso() -> str:
    return format_iso(now_utc())


def resolve(cli_value: str | None) -> str:
    """``--timestamp`` > ``SOURCE_DATE_EPOCH`` > 真实时间，统一成落盘字符串。"""
    if cli_value:
        return parse_timestamp(cli_value)
    return now_iso()


__all__ = [
    "ENV_SOURCE_DATE_EPOCH",
    "ClockError",
    "format_iso",
    "from_epoch",
    "now_iso",
    "now_utc",
    "parse_timestamp",
    "resolve",
]
