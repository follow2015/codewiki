"""交互图离线资产的门禁（WP8）。

**为什么单开 `codewiki/assets/graph/` 子目录**：500 KB 预算必须能一句断言说完。
``assets/`` 下已经躺着 5.2 MB 的 mermaid，同目录混放的话，"目录总字节 < 500 KB"
会被 mermaid 当场撑爆，只能退化成"排除 mermaid"这种按文件名打补丁的规则 ——
换个资产名就失效，且没人记得为什么排除。分子目录后预算 = 该目录 .js 字节和，
口径自明，也不会因为将来再加一张图的运行时又被迫改断言。

**为什么哈希与版本各存一份**：两者证明的不是同一件事。``PINNED_VERSION`` 只说
"脚本会去拉哪个版本"，哈希只说"现在这份字节没被动过"。单有哈希时，上游把同名
文件替换掉（同版本号、不同内容）看不出来；单有版本时，仓里这份到底是不是那个
版本也证明不了。两个都比对，才谈得上供应链校验。
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.source_tree

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "codewiki" / "assets"
GRAPH = ASSETS / "graph"
ASSET = GRAPH / "cytoscape.min.js"
DIGEST = GRAPH / "cytoscape.min.js.sha256"
NOTICE = ROOT / "NOTICE"
FETCH = ROOT / "scripts" / "fetch-graph-asset.sh"
PYPROJECT = ROOT / "pyproject.toml"

BUDGET_BYTES = 500 * 1024

MIN_BYTES = 200 * 1024

_RECORD_RE = re.compile(r"([0-9a-f]{64})\s+\*?cytoscape\.min\.js")
_ALLOWED_URL_HOSTS = ("opensource.org", "en.wikipedia.org", "engelschall.com")
_URL_RE = re.compile(r"https?://[^\s'\"\)]+")


def _recorded_digest() -> str:
    text = DIGEST.read_text(encoding="utf-8")
    records = _RECORD_RE.findall(text)
    assert len(records) == 1, (
        f"{DIGEST.name} 应当只有一条记录，实际 {len(records)} 条 —— 多条会让'以哪一条为准'变成隐含约定"
    )
    return records[0]


def _pinned_version() -> str:
    """从拉取脚本里读 PINNED_VERSION —— 版本号的唯一真相源是那条赋值。"""
    text = FETCH.read_text(encoding="utf-8")
    match = re.search(r'^PINNED_VERSION="([^"]+)"', text, re.MULTILINE)
    assert match, f"{FETCH.name} 里找不到 PINNED_VERSION= 赋值，版本号无从核对"
    return match.group(1)


def test_asset_and_digest_are_both_committed():
    """资产与哈希记录必须成对存在。

    只留资产：无法验证来源；只留记录：无从校验。两者任一缺失都是发布事故
    —— wheel 里少了资产，交互页在别人机器上就是一张空白。
    """
    assert ASSET.is_file(), f"交互图资产缺失：{ASSET}"
    assert DIGEST.is_file(), f"资产哈希记录缺失：{DIGEST}"


def test_asset_matches_recorded_digest():
    """实际字节必须与记录的 SHA-256 一致 —— 供应链校验的本体。"""
    actual = hashlib.sha256(ASSET.read_bytes()).hexdigest()
    assert actual == _recorded_digest(), (
        "交互图资产与入库的 SHA-256 不一致。若确实要换版本，请重跑 "
        "`bash scripts/fetch-graph-asset.sh <版本>` 让脚本重算哈希；"
        "不要在没换版本的情况下手改这一行\n"
        f"  实际: {actual}\n  记录: {_recorded_digest()}"
    )


def test_asset_is_not_a_truncated_substitute():
    """体积下限：截断的下载也可能"哈希写对了"（只要先跑脚本改哈希）。"""
    size = ASSET.stat().st_size
    assert size > MIN_BYTES, f"交互图资产只有 {size} 字节，像是没下完"


def test_interactive_budget_stays_under_500kb():
    """交互页要加载的全部 JS 之和不超过 500 KB。

    这是计划里那条"压缩后 < 500 KB"的机器化形式。超了就得做取舍（换库、
    砍功能、只用子集），不能默默把预算改大 —— 真要改，改 BUDGET_BYTES
    并在这里写清为什么。

    先断言"至少有一个文件"：目录不存在时 ``glob`` 返回空、``sum`` 是 0，
    0 ≤ 预算恒真 —— 那这条用例会在资产整个丢失时欢呼通过，正好丢掉它要
    守的东西。零文件通过比超预算更糟。
    """
    paths = sorted(GRAPH.glob("*.js"))
    assert paths, f"{GRAPH.relative_to(ROOT)} 下没有任何 JS 资产，预算校验无从谈起"
    total = sum(path.stat().st_size for path in paths)
    files = ", ".join(f"{p.name}={p.stat().st_size}" for p in paths)
    assert total <= BUDGET_BYTES, f"交互图 JS 合计 {total} 字节，超出预算 {BUDGET_BYTES} 字节（{files}）"


def test_notice_lists_every_bundled_asset():
    """NOTICE 必须为每个随包分发的 .js 记明组件、版本、许可证。

    资产是压缩产物，文件里查不到版本号；许可证又要求分发时附带声明。
    漏一个就意味着那份资产的来源在交付物里彻底不可追溯 —— 而"随包分发"
    正是本项目的卖点之一。
    """
    assert NOTICE.is_file(), f"缺少 {NOTICE.name}：随包分发的第三方资产必须有许可证声明"
    text = NOTICE.read_text(encoding="utf-8")
    for path in sorted(ASSETS.rglob("*.js")):
        assert path.name in text, f"NOTICE 里没有 {path.name} 的条目（{path.relative_to(ROOT)}）"
        assert "MIT" in text, f"NOTICE 里没有 {path.name} 的许可证名"


def test_notice_version_matches_the_pinned_version():
    """NOTICE 里记的版本必须与拉取脚本 pin 的版本一致。

    两处各写一遍就会漂：有人升级资产、改了脚本的 PINNED_VERSION、忘了 NOTICE，
    于是交付物上写着"cytoscape 3.34.4"，实际装的是 3.35。这条用例把两处钉死。
    """
    version = _pinned_version()
    text = NOTICE.read_text(encoding="utf-8")
    assert version in text, (
        f"NOTICE 里找不到 {version}：脚本 pin 的是这个版本，声明里却不是它\n"
        f"  脚本: {FETCH.relative_to(ROOT)}\n  NOTICE: {NOTICE.relative_to(ROOT)}"
    )


def test_package_data_declares_the_graph_subdirectory():
    """pyproject 的 package-data 必须覆盖 assets/graph/*.js。

    setuptools 的 ``assets/*.js`` **不匹配子目录**。漏了这条声明，源码树跑得好
    好的、wheel 里却没有文件 —— editable 安装把这个坑盖得严严实实（本仓在
    mermaid 资产上已经踩过一次，pyproject 里那段注释就是那次留下的）。
    """
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    patterns = data["tool"]["setuptools"]["package-data"]["codewiki"]
    assert any(p.startswith("assets/graph/") and p.endswith(".js") for p in patterns), (
        f"package-data 没有覆盖 assets/graph/*.js，wheel 会缺交互图资产；当前：{patterns}"
    )


def test_asset_does_not_reference_the_network():
    """资产里出现的外部 URL 只能是许可证正文里的那几条。

    离线验收要求"DevTools 切 offline 刷新可用"。资产自己若引了 CDN 上的代码
    （或遥测端点），切 offline 就哑了，而这种引用在压缩后的单行文件里肉眼
    看不见。白名单之外出现任何 URL 都当作"有人动了手"来处理。
    """
    text = ASSET.read_text(encoding="utf-8", errors="replace")
    unexpected = [
        url for url in set(_URL_RE.findall(text)) if not any(host in url for host in _ALLOWED_URL_HOSTS)
    ]
    assert not unexpected, f"资产里出现了许可证之外的 URL：{unexpected}"


def test_asset_does_not_use_dynamic_code_loading():
    """不许有动态加载外部脚本/网络请求的原语。

    与上一条互补：URL 可能被拆字符串拼出来（``'ht'+'tp://…'``），只看字面量
    会漏。这里卡的是那几个原语本身 —— 一个纯前端图库不需要它们。
    """
    text = ASSET.read_text(encoding="utf-8", errors="replace")
    for primitive in ("importScripts(", "document.write", "XMLHttpRequest", "new WebSocket"):
        assert primitive not in text, f"资产里出现了 {primitive}，离线场景不该有动态加载"
