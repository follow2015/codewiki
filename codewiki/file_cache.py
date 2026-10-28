"""FileModel 增量缓存（§7 性能、缓存与增量分析）。

文档定的键：``路径 + mtime + 内容 hash``。三者缺一不可：

* 少了路径 ⇒ 不同文件同内容会互撞；
* 少了 mtime ⇒ 内容相同但路径变了（改名）时无法区分；
* 少了内容 hash ⇒ mtime 被 touch 过（内容没变）会白白重解析，而反过来
  mtime 相同但内容变了（快速回滚/重写）就会读到旧事实 —— 两个都要。

三条纪律：

1. **缓存坏了当未命中**：缓存是加速手段，不是真源。解析失败/JSON 损坏/字段不符
   一律回退到重新解析，绝不因为缓存问题让整轮失败；
2. **不可用时静默降级**：缓存目录建不出来（只读仓库 / 权限）就用无缓存模式，
   不阻断分析；
3. **命中率要可见**：调用方会把 hit/miss 写进诊断，否则"缓存在不在工作"没人知道。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from codewiki.models import FileModel

CACHE_DIRNAME = ".codewiki/cache"


@dataclass
class CacheStats:
    """一轮的缓存命中情况（用于诊断，让"缓存在不在工作"可观测）。"""

    hit: int = 0
    miss: int = 0
    failed: int = 0

    @property
    def total(self) -> int:
        return self.hit + self.miss + self.failed


class FileModelCache:
    """文件系统缓存（一个 FileModel 一个 JSON，按 key 前两位分目录）。"""

    def __init__(self, root: Path) -> None:
        self.dir = root / CACHE_DIRNAME
        self.enabled = True

    @staticmethod
    def key(path: str, mtime_ns: int, content_hash: str, backend: str, *, parser_version: int) -> str:
        """缓存键：**必须含后端与解析结果格式版本**。

        同一份文件用不同后端解析出的 FileModel 不是一回事（ast 能拿到调用与
        继承，regex 只有 import 骨架）。键里没有 backend 时，一轮 `--backend ast`
        之后再来一轮 `--backend regex`，会全部命中 ast 的缓存 —— 用户点名要 regex，
        拿到的却是 ast 的事实，且 provenance 也跟着说谎。

        ``parser_version`` 解决的是另一半：**后端没换、解析逻辑变了**。修一个
        嵌套类限定名、改一处 import 归属，输出就变了，而路径/mtime/内容 hash 一模
        一样 —— 缓存照样命中，拿到旧格式的 FileModel，症状是"改了代码但产物没变"。
        后端改了解析行为就要 +1（见 ``analyzers.base.FORMAT_VERSIONS``）。

        两个参数都做成必需（``parser_version`` 还是 keyword-only）：新增调用点
        漏传会当场 TypeError，而不是悄悄退化成"没有版本维度的键"。
        """
        payload = f"{backend}|{parser_version}|{path}|{mtime_ns}|{content_hash}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _path_of(self, key: str) -> Path:
        return self.dir / key[:2] / f"{key}.json"

    def _ensure_dir(self) -> bool:
        """建好缓存根目录并写一份 ``.gitignore``（内容 ``*``）；建不出来返回 ``False``。

        为什么缓存目录必须自带忽略规则：FileModel 缓存是**运行时产物**，但它落在
        被分析仓库里（``<repo>/.codewiki/cache/``）。缺这份忽略文件时，一次
        ``git add -A`` 就会把"上一轮的解析结果"连同缓存一起提交；之后任何人 clone
        下来都带着旧 FileModel，源码改了照样命中 —— 症状与 ``key()`` 里
        ``parser_version`` 要防的"改了代码产物没变"完全一样，但这次是缓存文件
        本身进了库，键里再多维度也救不了。``ai/cache.py`` 的 ai-cache 一直这么做，
        这里补齐，两边同构。
        """
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.enabled = False
            return False
        ignore = self.dir / ".gitignore"
        if not ignore.exists():
            with contextlib.suppress(OSError):
                ignore.write_text("*\n", encoding="utf-8")
        return True

    def get(self, key: str) -> FileModel | None:
        if not self.enabled:
            return None
        path = self._path_of(key)
        if not path.exists():
            return None
        try:
            return FileModel.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def set(self, key: str, model: FileModel) -> None:
        if not self.enabled:
            return
        if not self._ensure_dir():
            return
        path = self._path_of(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.enabled = False
            return
        try:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(model.model_dump_json(), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            self.enabled = False


def mtime_ns_of(path: Path) -> int:
    """取 mtime（纳秒）；取不到返回 0（不因 stat 失败而阻断）。"""
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return 0


def content_hash_of(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]


__all__ = ["CACHE_DIRNAME", "CacheStats", "FileModelCache", "content_hash_of", "mtime_ns_of"]
