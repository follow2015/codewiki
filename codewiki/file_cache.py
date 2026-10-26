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
    def key(path: str, mtime_ns: int, content_hash: str, backend: str) -> str:
        """缓存键：**必须含后端**。

        同一份文件用不同后端解析出的 FileModel 不是一回事（ast 能拿到调用与
        继承，regex 只有 import 骨架）。键里没有 backend 时，一轮 `--backend ast`
        之后再来一轮 `--backend regex`，会全部命中 ast 的缓存 —— 用户点名要 regex，
        拿到的却是 ast 的事实，且 provenance 也跟着说谎。做成必需参数，避免
        以后新增调用点悄悄漏掉。
        """
        payload = f"{backend}|{path}|{mtime_ns}|{content_hash}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _path_of(self, key: str) -> Path:
        return self.dir / key[:2] / f"{key}.json"

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
