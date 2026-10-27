#!/usr/bin/env python3
"""verify-wheel.py — 校验构建产物（wheel）是否真的装下了源码树的全部模块。

用法：``python scripts/verify-wheel.py dist/*.whl``

为什么必须有这一步
------------------
``pip install -e .`` 直接用源码树，**打包配置写错它照样能跑**：

- ``package-data`` 漏声明 → 资产不在 wheel 里；
- ``[tool.setuptools.packages.find]`` 写窄 / ``namespaces`` 开关被改回默认
  → 整个子包静默漏掉，而 editable 安装仍然能 import（它根本没读 wheel）；
- 反过来 ``namespaces = true`` 会把没有 ``__init__.py`` 的目录也当包收进来。

这三种都很安静：冲烟绿、pytest 绿，只有用户 ``pip install`` 之后才崩。
唯一可靠的判据是**比对 wheel 的 namelist 与源码树清单**，逐文件对上。

退出码：0 通过 | 1 校验失败 | 2 用法/输入错误
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

REQUIRED_ASSETS = (
    "codewiki/assets/mermaid.min.js",
    "codewiki/assets/mermaid.min.js.sha256",
)

FORBIDDEN_FRAGMENTS = ("codewiki-deploy",)


def verify(wheel: Path, project_root: Path | str = ".", package: str = "codewiki") -> list[str]:
    """返回问题清单；空列表表示通过。

    ``project_root`` 是**仓库根**（含 ``codewiki/`` 的那一层），不是包目录本身 ——
    早先把包目录当根传进来，`rglob` 出来的绝对路径跟 wheel 条目永远对不上，
    校验"恒失败"，而失败信息看起来像是"漏了 N 个模块"，很容易误判成打包坏了。
    """
    problems: list[str] = []
    if not wheel.is_file():
        return [f"找不到 wheel: {wheel}"]

    try:
        with zipfile.ZipFile(wheel) as zf:
            names = zf.namelist()
    except zipfile.BadZipFile as exc:
        return [f"{wheel} 不是合法 zip: {exc}"]

    shipped = {n for n in names if n.endswith(".py")}
    root = Path(project_root)
    pkg = root / package
    expected = {p.relative_to(root).as_posix() for p in pkg.rglob("*.py")}
    if not expected:
        problems.append(f"源码树里一个 .py 都没有（project_root 写错了？）: {pkg}")

    missing = sorted(expected - shipped)
    extra = sorted(shipped - expected)
    if missing:
        problems.append(
            f"wheel 漏了 {len(missing)} 个模块（packages.find / package-data 配置坏了？）: {missing[:8]}"
        )
    if extra:
        problems.append(f"wheel 混入源码树没有的模块: {extra[:8]}")

    for asset in REQUIRED_ASSETS:
        if asset not in names:
            problems.append(f"wheel 缺交付资产: {asset}")

    for fragment in FORBIDDEN_FRAGMENTS:
        leaked = [n for n in names if fragment in n]
        if leaked:
            problems.append(f"wheel 混进了 {fragment}: {leaked[:3]}")

    return problems


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    failed = False
    for arg in argv:
        wheel = Path(arg)
        problems = verify(wheel)
        if problems:
            failed = True
            print(f"❌ {wheel.name}")
            for p in problems:
                print(f"   - {p}")
        else:
            with zipfile.ZipFile(wheel) as zf:
                count = sum(1 for n in zf.namelist() if n.endswith(".py"))
            print(f"✅ {wheel.name}：{count} 个模块与源码树逐一对应，交付资产齐全")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
