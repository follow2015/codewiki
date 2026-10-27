"""离线资产的供应链校验（设计文稿 §CI · mermaid 离线资产）。

问题：资产是压缩后的产物，文件里没有 banner、grep 不到版本号。``PINNED_VERSION``
只说明"脚本会去拉哪个版本"，**不能证明仓里这份就是那个版本** —— 有人手动换掉、
上游把同名文件替换掉、下载被截断，三种情况都看不出来。

所以把 SHA-256 一并入库，测试每次比对实际值。改资产就必须改哈希，改不动说明
中间有人动过手 —— 这正是"供应链换掉上游内容时能被发现"要的效果。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "codewiki" / "assets"
ASSET = ASSETS / "mermaid.min.js"
DIGEST = ASSETS / "mermaid.min.js.sha256"

_RECORD_RE = re.compile(r"([0-9a-f]{64})\s+\*?mermaid\.min\.js")


def _recorded_digest() -> str:
    text = DIGEST.read_text(encoding="utf-8")
    records = _RECORD_RE.findall(text)
    assert len(records) == 1, (
        f"{DIGEST.name} 应当只有一条记录，实际 {len(records)} 条 —— 多条会让"
        "'以哪一条为准'变成隐含约定"
    )
    return records[0]


def test_offline_asset_and_its_digest_are_both_committed():
    """资产与哈希记录必须成对存在。

    只留资产：无法验证来源；只留记录：无从校验。两者任一缺失都是发布事故
    （wheel 里少了资产 → 双击看不了图，而 CLI 只记一条 ASSET_MISSING）。
    """
    assert ASSET.is_file(), f"离线资产缺失：{ASSET}"
    assert DIGEST.is_file(), f"资产哈希记录缺失：{DIGEST}"


def test_offline_asset_matches_recorded_digest():
    """实际字节必须与记录的 SHA-256 一致 —— 这是供应链校验的本体。"""
    actual = hashlib.sha256(ASSET.read_bytes()).hexdigest()
    assert actual == _recorded_digest(), (
        "离线资产与入库的 SHA-256 不一致。若确实要换版本，请重跑 "
        "`bash scripts/fetch-mermaid-asset.sh <版本>` 让脚本重算哈希；"
        "不要在没换版本的情况下手改这一行\n"
        f"  实际: {actual}\n  记录: {_recorded_digest()}"
    )


def test_offline_asset_is_not_a_truncated_substitute():
    """体积下限：截断的下载也可能"哈希写对了"（只要先跑脚本改哈希）。

    mermaid 12 的单文件 IIFE 产物约 5.2MB，1MB 是宽松下限 —— 它拦不住刻意
    构造的替换，只拦"下了一半就提交"这种实际操作失误。
    """
    size = ASSET.stat().st_size
    assert size > 1_000_000, f"离线资产只有 {size} 字节，像是没下完"
