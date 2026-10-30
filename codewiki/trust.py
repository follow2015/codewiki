"""TrustedSettings —— ``codewiki.toml`` 是不可信输入（设计文稿 §AI 层安全模型）。

``codewiki.toml`` 通常就躺在**被分析仓库**里，作者可控。它决定"怎么扫这个仓库"
的字段（分层、语言、阈值）本该如此 —— 那是项目事实。但有一类字段决定的是
**操作者的钱、密钥与写入权限**，让仓库作者说了算就不对：

* ``base_url``   —— 改了它，请求连同 ``OPENAI_API_KEY`` 会以 Bearer 头发往任意地址；
* ``model``      —— 换了它，代码被送去操作者没选过的模型；
* ``write_back`` —— 改成 ``auto``，就绕过"AI 只出建议文件、人工合并"这一环。

这三项因此标为 **仅操作者可设**：只认 CLI 与环境变量。文件里写了不算数，给一条
warning 说明"已忽略、正确的写法是什么"。

要为自己拥有的仓库恢复文件配置，用 ``--trust-config`` 或
``CODEWIKI_TRUST_CONFIG=1`` —— 那是操作者亲手按下的开关，仓库作者按不到
（这也是设计文稿 §风险与待决问题 第 3 条选定的"保留脚本化用法、去掉被分析仓库
自己开启的可能"）。

放在 ``trust.py`` 而不是散在 ``cli.py`` 里，是因为"哪些字段可信"必须只有一份：
上一轮 ``resolve_base_url`` 只覆盖了 ``base_url`` 一个字段，``write_back`` 就这样
漏了两轮评审。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from codewiki.config import CodewikiConfig, load_config

ENV_TRUST_CONFIG = "CODEWIKI_TRUST_CONFIG"

ENV_FORCE_AI = "CODEWIKI_FORCE_AI"

OPERATOR_ONLY: dict[str, frozenset[str]] = {
    "ai": frozenset({"base_url", "model", "write_back"}),
}

_HOWTO: dict[str, str] = {
    "base_url": "--base-url 或环境变量 OPENAI_BASE_URL",
    "model": "--model 或环境变量 OPENAI_MODEL",
    "write_back": "--write-back",
}


@dataclass(frozen=True)
class TrustReport:
    """脱敏（脱去不可信字段）后的配置 + 说明。"""

    config: CodewikiConfig
    warnings: tuple[str, ...] = ()
    ignored: frozenset[str] = field(default_factory=frozenset)


def is_trusted(env: dict[str, str] | None = None) -> bool:
    """``CODEWIKI_TRUST_CONFIG`` 是否显式授权信任仓库里的配置。"""
    raw = (env if env is not None else os.environ).get(ENV_TRUST_CONFIG, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def is_force_ai(env: dict[str, str] | None = None) -> bool:
    """``CODEWIKI_FORCE_AI`` 是否显式覆盖"项目方不要 AI"的声明。"""
    raw = (env if env is not None else os.environ).get(ENV_FORCE_AI, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def ai_denied(cfg: CodewikiConfig, *, force: bool = False) -> str | None:
    """配置显式声明 ``[ai] enabled = false`` 时返回**拒绝理由**（含如何覆盖）。

    只认显式的 ``false``：缺省是 ``None``，老配置（及其作者）只是没写这一行，
    不该被当成"拒绝"。

    返回字符串而不是抛异常，是因为三个 AI 命令的既有风格是"打 stderr + return 2"，
    抛异常会把"用法拒绝"和"运行失败"混成一个 traceback。
    """
    if force or cfg.ai.enabled is not False:
        return None
    return (
        "error: 该仓库的 codewiki.toml 声明 [ai] enabled = false"
        "（项目方不要 AI 分析）；确实要跑请加 --force-ai 或置 "
        f"{ENV_FORCE_AI}=1"
    )


def apply_trust_policy(cfg: CodewikiConfig, *, trusted: bool = False) -> TrustReport:
    """把"仅操作者可设"的字段打回**默认值**（不是置空 —— 默认值语义更稳）。

    只处理 ``model_fields_set`` 里真出现过的字段：值与默认值恰好相同（用户写了
    ``write_back = "review-file"``）也算"写了"，因为意图已经表达，静默放过会让
    下一轮改成 ``auto`` 时无人察觉。
    """
    if trusted:
        return TrustReport(config=cfg)

    updates: dict[str, object] = {}
    ignored: list[str] = []
    warnings: list[str] = []

    for section_name, guarded in OPERATOR_ONLY.items():
        section = getattr(cfg, section_name)
        explicit = section.model_fields_set & guarded
        if not explicit:
            continue
        patched = section.model_copy(
            update={
                name: type(section).model_fields[name].get_default(call_default_factory=True)
                for name in explicit
            }
        )
        updates[section_name] = patched
        for name in sorted(explicit):
            qualified = f"{section_name}.{name}"
            ignored.append(qualified)
            warnings.append(
                f"[{section_name}].{name} 已忽略：codewiki.toml 位于被分析仓库、作者可控，"
                f"该项只认操作者（{_HOWTO.get(name, 'CLI 或环境变量')}；"
                f"确实要信任该配置就加 --trust-config 或置 {ENV_TRUST_CONFIG}=1）"
            )

    if not updates:
        return TrustReport(config=cfg)
    return TrustReport(
        config=cfg.model_copy(update=updates),
        warnings=tuple(warnings),
        ignored=frozenset(ignored),
    )


__all__ = [
    "ENV_FORCE_AI",
    "ENV_TRUST_CONFIG",
    "OPERATOR_ONLY",
    "TrustReport",
    "ai_denied",
    "apply_trust_policy",
    "is_force_ai",
    "is_trusted",
    "load_trusted_config",
]


def load_trusted_config(path: Path, *, trusted: bool = False) -> CodewikiConfig:
    """读配置并施加信任策略（``analyze`` / 三个 AI 命令共用的唯一入口）。

    放在这里而不是命令层，是因为流水线（``pipeline.analyze``）也要读配置，而
    依赖方向禁止 pipeline 反向 import cli。两份实现迟早会漂：一份按信任策略
    打回默认值、另一份忘了，于是同一个仓库在 ``analyze`` 与 ``ai-annotate``
    下读到两套 ``[ai]`` 配置。

    warning 直接打到 stderr：这条提示是给**操作者**看的（"你写的这行不算数，
    要改就加 --base-url"），沉默忽略等于让用户以为配置生效了。
    """
    report = apply_trust_policy(load_config(path), trusted=trusted)
    for warning in report.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return report.config
