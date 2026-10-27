#!/usr/bin/env python3
"""check_release_tag.py — 发布前校验 git tag 与 pyproject 版本一致。

用法：``python scripts/check_release_tag.py --tag v1.0.0``
      ``python scripts/check_release_tag.py``（从 ``GITHUB_REF_NAME`` 取）

为什么必须是脚本，而不是 workflow 里的 ``python -c``
----------------------------------------------------
那段内联代码**只有打 tag 才会执行**，也就是说它写错了要到发版当天才发现 —— 而
发版是最不该现场调试的环节。抽成脚本之后有两件事同时成立：

- 本地随手可验：``python scripts/check_release_tag.py --tag v1.0.0``；
- 平时就在验：``tests/test_check_release_tag.py`` 在每次 CI 跑它。

判据（宁严勿宽）
----------------
去掉**一个**前导 ``v`` 之后逐字相等。刻意不做 PEP 440 正规化：

- 那需要自造一个版本解析器，而它出错的方式恰好是"把不该相等的判成相等" ——
  等于把这道闸拆掉；
- 真要发 ``v1.1.0-rc1``，就让 pyproject 里也写 ``1.1.0-rc1``，两边同一个串。

不一致时产出的是"名叫 v1.2.0、内容是 1.1.0"的包：装上去很难察觉，却让所有
issue 与回归都失去可复现性。

退出码：0 一致 | 1 不一致 | 2 输入不可用（缺 tag / 文件 / version）
"""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
from pathlib import Path

EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_USAGE = 2

TAG_ENV = "GITHUB_REF_NAME"


def read_version(pyproject: Path) -> str:
    """读 ``[project].version``；缺失或非字符串都算输入错误，不返回空串糊过去。"""
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"{pyproject} 不是合法 TOML：{exc}") from exc
    version = (data.get("project") or {}).get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"{pyproject} 里没有可用的 project.version（收到 {version!r}）")
    return version.strip()


def normalize_tag(tag: str) -> str:
    """去掉一个前导 ``v``。只认小写：``V1.0.0`` 不是本项目约定的 tag 形态。"""
    return tag[1:] if tag.startswith("v") else tag


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="校验 tag 与 pyproject 版本一致")
    parser.add_argument("--tag", default=None, help=f"默认取环境变量 {TAG_ENV}")
    parser.add_argument("--pyproject", default="pyproject.toml")
    args = parser.parse_args(argv)

    raw_tag = args.tag if args.tag is not None else os.environ.get(TAG_ENV, "")
    tag = raw_tag.strip()
    if not tag:
        print(
            f"错误：没有 tag —— --tag 未给出，环境变量 {TAG_ENV} 也是空的。",
            file=sys.stderr,
        )
        return EXIT_USAGE

    path = Path(args.pyproject)
    if not path.is_file():
        print(f"错误：找不到 {path}", file=sys.stderr)
        return EXIT_USAGE

    try:
        version = read_version(path)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_USAGE

    if normalize_tag(tag) != version:
        print(
            f"❌ tag 「{tag}」与 {path} 的 version 「{version}」不一致。\n"
            f"   这样产出的是「名叫 {tag}、内容是 {version}」的包：装上去很难察觉，\n"
            f"   却让所有 issue 与回归都无法复现。\n"
            f"   修法：把 pyproject 的 version 改成 {normalize_tag(tag)}，或重新打 tag。",
            file=sys.stderr,
        )
        return EXIT_MISMATCH

    print(f"✅ 版本一致：{version}（tag {tag}）")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
