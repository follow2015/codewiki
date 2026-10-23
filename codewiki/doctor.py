"""环境自检（``codewiki doctor``，§9.1 / §六）。

阶段一最小版：检查"能不能跑起来 + 哪些可选能力不可用"，**不阻断**（退出码恒 0），
把结论写清楚即可 —— 用户据此知道缺什么、怎么补。

检查项（与 9.1 的探测表逐条对应）：

- Python 版本：低于 3.14.7 → 提示升级（能跑到这里说明已安装，故为 warning）；
- 分析后端：python(ast) 必备；regex 必备；tree-sitter 可选（缺失不影响默认流程）；
- Node：缺失或 < 22.13 → L2 不可用（info）；主版本 ≠ 24 → warning（基线 24 LTS）；
- 离线看图资源：assets/mermaid.min.js 是否随包（阶段三落地前恒缺，info）；
- AI：未配置 → info（纯静态是正常形态）；配了模型没凭据 → warning；``--probe``
  才真连一次。

原则上 doctor **不发可能花钱/耗时的请求**，AI 连通性一律由 ``--probe`` 显式开启。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass

MIN_PYTHON = (3, 14, 7)
NODE_BASELINE_MAJOR = 24
NODE_MINIMUM = (22, 13)


@dataclass
class Check:
    """一条自检结论。"""

    level: str
    name: str
    message: str


def run_checks(*, out_dir: str | None = None, probe_ai: bool = False) -> list[Check]:
    """跑全部检查。

    默认**纯本地探测不联网**；``probe_ai=True`` 才真的发一次极短对话（要花钱、
    可能慢），所以由 ``doctor --probe`` 显式开启。
    """
    checks: list[Check] = [_check_python(), *_check_backends(), _check_node(), _check_asset_source()]
    checks.append(_check_ai(probe=probe_ai))
    if out_dir:
        checks.append(_check_offline_assets(out_dir))
    return checks


def render_report(checks: list[Check]) -> str:
    """把检查结论渲染成人类可读文本。"""
    icon = {"ok": "✅", "info": "⏭️", "warning": "⚠️", "error": "❌"}
    lines = ["codewiki doctor", ""]
    for check in checks:
        lines.append(f"{icon.get(check.level, '·')} {check.name}: {check.message}")
    lines.append("")
    level = "error" if any(c.level == "error" for c in checks) else (
        "warning" if any(c.level == "warning" for c in checks) else "ok"
    )
    lines.append(f"结论：{level}（doctor 不阻断，退出码恒 0）")
    return "\n".join(lines) + "\n"




def _check_python() -> Check:
    version = sys.version_info[:3]
    text = ".".join(str(v) for v in version)
    if version >= MIN_PYTHON:
        return Check("ok", "Python", f"{text}（≥ 3.14.7）")
    return Check(
        "warning",
        "Python",
        f"{text} 低于基线 3.14.7：发布与 CI 以 3.14.7+ 为准，本机可用但不在支持范围内",
    )


def _check_backends() -> list[Check]:
    checks = [Check("ok", "后端 python/ast", "标准库 ast 可用")]

    try:
        from codewiki.analyzers.regex_backend import RegexAnalyzer  # noqa: F401

        checks.append(Check("ok", "后端 regex", "零依赖降级后端可用"))
    except Exception as exc:
        checks.append(Check("error", "后端 regex", f"导入失败: {exc}"))

    try:
        import tree_sitter  # noqa: F401

        checks.append(Check("ok", "后端 tree-sitter", "可选依赖已安装（TS/Go 可用）"))
    except Exception:
        checks.append(
            Check("info", "后端 tree-sitter", "未安装（可选）：TS/Go 将降级为正则；见 [treesitter] extra")
        )
    return checks


def _check_node() -> Check:
    node = shutil.which("node")
    if not node:
        return Check("info", "Node", "未安装（可选）：L2 真实渲染校验不可用，仅用 L1 特征门")

    raw = ""
    try:
        raw = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        raw = ""

    parsed = _parse_version(raw)
    if parsed is None:
        return Check("warning", "Node", f"无法解析版本号（{raw!r}）：L2 视为不可用")

    major, minor = parsed[0], parsed[1] if len(parsed) > 1 else 0
    if (major, minor) < NODE_MINIMUM:
        return Check("info", "Node", f"{raw} 低于 22.13：L2 不可用，回退 L1")
    if major != NODE_BASELINE_MAJOR:
        return Check("warning", "Node", f"{raw}：基线为 24 LTS，其他版本未验证")
    return Check("ok", "Node", f"{raw}（基线 24 LTS）")


def _check_offline_assets(out_dir: str) -> Check:
    from pathlib import Path

    asset = Path(out_dir) / "assets" / "mermaid.min.js"
    if asset.exists():
        return Check("ok", "离线看图资源", "assets/mermaid.min.js 已随包")
    return Check(
        "info",
        "离线看图资源",
        f"{asset} 缺失：Zip 内图以 .mmd 原文呈现（随包资源属阶段三）",
    )


def _check_ai(*, probe: bool) -> Check:
    """AI 可用性（阶段二新增）。

    三级结论必须分清：

    * 未配置 → info（纯静态模式是**正常形态**，不是缺陷）；
    * 配了模型但拿不到凭据 → warning（下一步就用不了）；
    * 配齐且 ``probe`` → 真连一次，连通与否给 ok / warning。
    """
    import os
    from pathlib import Path

    api_key = os.environ.get("OPENAI_API_KEY", "")
    env_base = os.environ.get("OPENAI_BASE_URL", "")
    model = None
    base_url = env_base or None
    try:
        from codewiki.config import CONFIG_FILENAME, load_config

        cfg = load_config(Path.cwd() / CONFIG_FILENAME)
        model = cfg.ai.model
        base_url = base_url or cfg.ai.base_url
    except Exception:  # noqa: BLE001 - doctor 不该因配置坏而崩（那是 load_config 的职责）
        pass

    if not model and not env_base:
        return Check("info", "AI", "未配置：纯静态模式（analyze 全功能可用）")

    display = model or "(由 base_url 决定)"
    if not api_key and not (base_url and base_url.startswith(("http://localhost", "http://127.0.0.1"))):
        return Check("warning", "AI", f"模型 {display} 已配置，但未设置 OPENAI_API_KEY")

    if not probe:
        return Check("ok", "AI", f"已配置 {display}（未探测连通性；加 --probe 才真连一次）")

    from codewiki.ai.client import OpenAICompatClient, probe_connectivity

    ok, detail = probe_connectivity(OpenAICompatClient(model=model or "codewiki-probe", base_url=base_url))
    return Check("ok" if ok else "warning", "AI", detail)


def _check_asset_source() -> Check:
    """离线看图资源的**来源**（包内是否已取得 mermaid.min.js）。

    与"产物里有没有"是两回事：产物缺可以重跑 analyze，包内缺则要先跑取资源的
    脚本 —— 提示要给出**可执行**的下一步。
    """
    from codewiki.render.site import PACKAGE_ASSET

    if PACKAGE_ASSET.exists():
        return Check("ok", "离线看图资源(包内)", f"{PACKAGE_ASSET.name} 已就位，analyze 会复制进产物")
    return Check(
        "info",
        "离线看图资源(包内)",
        "未取得：跑 `bash scripts/fetch-mermaid-asset.sh` 后，analyze 会随包携带",
    )


def _parse_version(raw: str) -> tuple[int, ...] | None:
    """``v24.1.0`` → ``(24, 1, 0)``。"""
    digits = raw.lstrip("v").split(".")[:3]
    try:
        return tuple(int(part) for part in digits)
    except ValueError:
        return None


__all__ = ["Check", "MIN_PYTHON", "NODE_BASELINE_MAJOR", "NODE_MINIMUM", "render_report", "run_checks"]
