"""诊断清单的 Markdown 形态（``diagnostics.md``，设计文稿 §4.5）。

放在 render 层而不是命令层：``analyze`` 与 ``ai-annotate`` 都要写它（后者写进
``suggestions/``），而这两条路径分属 pipeline 与 cli —— 谁都不拥有对方的输出
细节。写一次、两边调用，格式才不会一边加了 ``where`` 一边没加。

内容刻意只由诊断本身决定：不写时间戳、不写"本轮怎么跑的"。归档物里混入运行
历史会让"同一份输入两次运行产物相同"这条验收作废。
"""

from __future__ import annotations

from pathlib import Path

from codewiki.render.i18n import t
from codewiki.schemas import Diagnostic


def write_diagnostics_md(out_dir: Path, diagnostics: list[Diagnostic]) -> None:
    """把诊断写成人类可读的最小清单（无诊断时也落一行说明，不写空文件）。"""
    lines = [t("# codewiki 诊断", "# codewiki diagnostics"), ""]
    if not diagnostics:
        lines.append(t("无诊断输出。", "No diagnostics."))
    for item in diagnostics:
        where = f"（{item.where}）" if item.where else ""
        lines.append(f"- **{item.level}** `{item.code}` {where}：{item.message}")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "diagnostics.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
