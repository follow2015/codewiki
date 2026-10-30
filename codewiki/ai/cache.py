"""AI 结果缓存与 token 预算（§5.5）。

缓存键 = ``文件内容 hash + 提示词版本 + 模型名``（+ 可选邻居上下文 hash / 拓扑 hash）。
这些成分缺一不可：

- 少了文件 hash ⇒ 改了代码还拿旧结论；
- 少了提示词版本 ⇒ 改了 Prompt 却命中旧缓存，表现为"改了没效果"；
- 少了模型名 ⇒ 换模型后拿另一个模型的答案，成本与质量都对不上账；
- 少了**邻居 hash** ⇒ 本模块一字未改、但它的上下游变了，注解依据已经不同，
  旧答案却被复用 —— 这是"注解与图谱对不上"的最隐蔽来源（本模块文件内容是
  缓存键里唯一会变的东西时尤其如此）。

拓扑 hash 默认**关**（§5.5 明确为可选优化项，不作为阶段二验收门槛）。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

CACHE_DIRNAME = ".codewiki/ai-cache"

PROMPT_VERSION = "annotate-v4"


def digest_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cache_key(
    *,
    content_hash: str,
    model: str,
    prompt_version: str = PROMPT_VERSION,
    topology_hash: str | None = None,
    neighbors_hash: str | None = None,
) -> str:
    """按各成分算缓存键。"""
    parts = [prompt_version, model, content_hash]
    if neighbors_hash:
        parts.append(f"nbr:{neighbors_hash}")
    if topology_hash:
        parts.append(f"topo:{topology_hash}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def default_cache_dir() -> Path:
    """用户级默认缓存目录 —— **不落在被分析仓库里**。

    此前默认取 ``--src``（被分析仓库）：键 = 内容 hash + 提示词版本 + 模型名，
    同版本工具可自行复算，于是被审仓库只要预置 ``ai-cache/<key>.json``，
    命中时 ``raw`` 只校验"是 JSON"就被当作模型答案采信 —— 审计第三方代码时
    这是一条可投毒的通道。缓存只是加速，没有理由写进别人的仓库。
    """
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "codewiki" / "ai-cache"


class ResultCache:
    """文件系统缓存（JSON 文件，一个键一个文件）。

    ``directory`` 就是缓存目录本身。目录创建时自带 ``.gitignore``（内容 ``*``），
    万一路径落在工作树里也不脏人家的 ``git status``。
    """

    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)

    def get(self, key: str) -> dict | None:
        path = self.dir / f"{key}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def set(self, key: str, value: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        gitignore = self.dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("*\n", encoding="utf-8")
        path = self.dir / f"{key}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)


class TokenBudget:
    """硬上限预算：超限前优雅停止，并告知还剩多少（§5.5）。"""

    def __init__(self, max_tokens: int | None) -> None:
        self.max_tokens = max_tokens
        self.used = 0

    @property
    def exceeded(self) -> bool:
        return self.max_tokens is not None and self.used >= self.max_tokens

    def spend(self, tokens: int) -> None:
        self.used += tokens

    def remaining(self) -> int | None:
        if self.max_tokens is None:
            return None
        return max(0, self.max_tokens - self.used)


def estimate_tokens(text: str) -> int:
    """粗估 token 数（无 tokenizer 依赖）。

    中文按 ~1.5 token/字、其余按 ~4 字符/token 估，**宁可高估**：低估会让预算
    形同虚设（超限了还以为没超）。
    """
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    others = max(0, len(text) - cjk)
    return int(cjk * 1.5 + others / 4) + 1


__all__ = [
    "CACHE_DIRNAME",
    "PROMPT_VERSION",
    "ResultCache",
    "TokenBudget",
    "cache_key",
    "digest_of",
    "estimate_tokens",
]
