#!/usr/bin/env python3
"""检查 README 里每条承诺都有对应测试（设计文稿 §测试 · "承诺必须有测试"的落地）。

问题：README 写"离线可看图""增量等于全量""AI 工具闭集"，但没有任何机制保证
这些话仍然成立。文档与实现漂移是这类项目的典型失效方式 —— 一次重构顺手改了行为，
文字还留在那儿，读者信了、坏了没人知道。

做法：README 的承诺行尾挂 ``<!-- guarantee: <id> -->``，对应测试加
``@pytest.mark.guarantee("<id>")``。本脚本双向比对：

- README 有、测试没有 → 这条承诺没人验证（**失败**）
- 测试有、README 没有 → id 拼错了，或承诺已被删而测试没跟着改（**失败**）
- 同一个 id 在 README 出现两次 → 复制粘贴串了，改一处会漏另一处（**失败**）

发布仓不含 ``tests/``，此时脚本跳过检查并返回 0（公开仓只发布，不验证）。

用法：
    python scripts/check_guarantees.py [仓库根]
退出码：0 通过 / 1 不通过
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

README = "README.md"
TESTS_DIR = "tests"

_README_RE = re.compile(r"<!--\s*guarantee:\s*([a-z0-9][a-z0-9-]*)\s*-->")
_TEST_RE = re.compile(r"""guarantee\(\s*(?:id\s*=\s*)?["']([a-z0-9][a-z0-9-]*)["']""")


def _duplicates(ids: list[str]) -> set[str]:
    seen: set[str] = set()
    dupes: set[str] = set()
    for i in ids:
        if i in seen:
            dupes.add(i)
        seen.add(i)
    return dupes


def collect(root: Path) -> tuple[list[str], list[str]]:
    """返回 ``(README 里的 id 列表, 测试里的 id 列表)``（保留顺序与重复项）。"""
    readme = (root / README).read_text(encoding="utf-8")
    declared = _README_RE.findall(readme)

    used: list[str] = []
    for path in sorted((root / TESTS_DIR).rglob("*.py")):
        used.extend(_TEST_RE.findall(path.read_text(encoding="utf-8")))
    return declared, used


def check(root: Path) -> list[str]:
    """返回问题列表；空列表表示通过。"""
    if not (root / TESTS_DIR).is_dir():
        return []

    declared, used = collect(root)
    declared_set, used_set = set(declared), set(used)
    problems: list[str] = []

    for missing in sorted(declared_set - used_set):
        problems.append(f'README 承诺 `{missing}` 没有任何测试标注 guarantee("{missing}")')
    for extra in sorted(used_set - declared_set):
        problems.append(f"测试标注了 `{extra}`，但 README 里没有这条承诺（拼错或承诺已被删）")
    for dupe in sorted(_duplicates(declared)):
        problems.append(f"README 里 `{dupe}` 出现了不止一次（改一处会漏另一处）")
    return problems


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path(__file__).resolve().parent.parent
    if not (root / TESTS_DIR).is_dir():
        print(f"跳过：{root} 不含 {TESTS_DIR}/（发布仓不做承诺校验）")
        return 0

    problems = check(root)
    if problems:
        print("承诺与测试不一致：")
        for p in problems:
            print(f"  - {p}")
        return 1

    declared, _ = collect(root)
    print(f"通过：{len(set(declared))} 条承诺都有对应测试")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
