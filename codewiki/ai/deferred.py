"""外置推理（deferred reasoning）：让**上层 agent 自己充当模型后端**。

``ai-annotate`` 默认走 OpenAI 兼容 HTTP，需要 ``OPENAI_API_KEY``。但在
WorkBuddy / CodeBuddy 这类**自带模型**的 agent 里跑时，凭据与出网都未必可得 ——
而 agent 本身就是一个能读源码、能写 JSON 的推理方。于是把"调模型"拆成三步：

1. **导出**：缺答案的对话落成 ``<root>/requests/<purpose>-<key>.json``（内含完整
   messages、作答协议与输出形状），命令以 ``EXIT_PENDING``(3) 结束；
2. **作答**：agent 读请求、按协议写 ``<root>/answers/<同名文件>``；
3. **回灌**：重跑同一条命令，答案按 key 命中，流程跑完并照常写入 ai-cache。

四条设计约束：

* **key 由内容派生**：``sha256(purpose + model + messages)``。改了提示词、换了
  模型或源码变了，旧答案自动失效 —— 不会出现"拿昨天的结论回答今天的问题"。
  这也是不按模块名做 key 的原因（模块名稳定，内容不稳定）。
* **答案是不可信输入**：只当"模型输出的文本"来解析，仍要过 §5.2 的三道闸
  （target 必须是本模块、evidence 行号必须真实存在、解析不出来就丢弃）。
  能写文件的一方绕不过这些校验。
* **本模块不做网络**：不 import urllib，不读任何密钥环境变量。``--deferred``
  与 ``OPENAI_API_KEY`` 完全无关 —— 这也让它天然是"零凭据、可审计"的路径。
* **请求文件自描述**：``instructions`` 字段写清了"答案叫什么、长什么样"，
  接手作答的 agent 不需要先读这份文档。

协议版本化：``protocol`` 字段变化时答案目录整体视为无效（宁可重导，不做格式
猜测 —— 猜错的后果是把旧结构的答案喂进新解析器，产出看起来正常的垃圾）。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from codewiki.ai.client import ChatMessage, ChatResult, LLMError

PROTOCOL = "codewiki-deferred/1"

DEFAULT_MODEL = "agent"

REQUESTS_DIRNAME = "requests"
ANSWERS_DIRNAME = "answers"

MAX_ANSWER_CHARS = 256_000

_NAME_RE = re.compile(r"^(?P<purpose>[a-z][a-z0-9_]{0,31})-(?P<key>[0-9a-f]{16,64})\.json$")

_INSTRUCTIONS = (
    "你是本次请求的作答方（agent 自身即可，无需外部模型服务）。",
    "读 messages：system 是规则，user 是本轮材料；按 system 要求的输出形状生成**模型原文**。",
    '把原文写进 answers 目录下的同名文件：{"key": "<本文件的 key>", "content": "<模型原文>"}。',
    "content 必须是字符串，不要加解释、不要改需求、不要用 Markdown 代码块围栏包裹 JSON。",
    "答案会被当作模型输出解析，仍受两道校验：target 必须是本模块、evidence 行号必须真实存在。",
    "文件名必须与请求文件完全一致（含 purpose 前缀与 16 位 key），否则不会被命中。",
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def request_key(*, purpose: str, model: str, messages: list[ChatMessage]) -> str:
    """请求指纹：purpose + model + messages 的 sha256。

    ``sort_keys`` 只排序**字典键**，messages 列表顺序照旧保留 —— 顺序是语义的
    一部分（system 在前还是在后，对模型是两件事），不能被"规范化"掉。
    """
    payload = {
        "protocol": PROTOCOL,
        "purpose": purpose,
        "model": model,
        "messages": [{"role": m.role, "content": m.content} for m in messages],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def file_name(*, purpose: str, key: str) -> str:
    """请求与答案共用的文件名（``<purpose>-<key 前 16 位>.json``）。"""
    return f"{purpose}-{key[:16]}.json"


class PendingRequest(LLMError):
    """该对话还没有答案 —— **不是失败**，是"等待作答"。

    继承 :class:`LLMError` 但语义相反：调用方看到它应当**继续**处理别的模块
    （`AI_UNAVAILABLE` 才是要停下来的那种）。分开的理由很实际 —— 一次导出
    30 个模块的请求，不能因为第 1 个缺答案就只导出 1 个。
    """

    def __init__(self, key: str, *, path: Path | None = None) -> None:
        self.key = key
        self.path = path
        where = f"（请求已写入 {path}）" if path else ""
        super().__init__(f"待外部作答{where}")


@dataclass(frozen=True)
class PendingItem:
    """一条待作答请求（供 CLI 汇总打印）。"""

    name: str
    key: str
    path: Path


class DeferredClient:
    """与 :class:`~codewiki.ai.client.OpenAICompatClient` 同接口的文件型后端。

    只实现 annotate 用到的部分（``model`` 属性 + ``chat()``）：假装接口更宽会
    诱使别的调用方接进来，而那些路径（例如 agent 的多轮工具循环）需要的是
    "每一轮都由人/agent 现场决定"，用文件回放只会得到一个假的确定性。

    ``tools`` / ``response_format`` 收到即忽略，且**不参与**请求 key：作答方是
    按 messages 里写明的形状作答的，原生参数对它没有语义；把它们塞进 key 只会
    让同一份提示词的旧答案全部失效（实测会凭空重导出 30 个请求）。同理这里把
    两个能力开关置 False —— 上层据此就知道"别指望原生结构化输出"。
    """

    supports_native_tools = False
    supports_json_mode = False

    def __init__(
        self,
        root: Path,
        *,
        model: str = DEFAULT_MODEL,
        purpose: str = "annotate",
        max_answer_chars: int = MAX_ANSWER_CHARS,
    ) -> None:
        if not _NAME_RE.match(file_name(purpose=purpose, key="0" * 16)):
            raise ValueError(f"非法的 purpose: {purpose!r}（只允许小写字母/数字/下划线）")
        self.root = Path(root)
        self.model = model
        self.purpose = purpose
        self.max_answer_chars = max_answer_chars
        self.hits = 0
        self.misses: list[PendingItem] = []

    @property
    def requests_dir(self) -> Path:
        return self.root / REQUESTS_DIRNAME

    @property
    def answers_dir(self) -> Path:
        return self.root / ANSWERS_DIRNAME

    def pending(self) -> list[PendingItem]:
        """扫描请求目录，返回**还没有答案**的条目（按文件名排序，稳定）。

        key 必须从**请求文件内容**里读，不能从文件名截取 —— 文件名只有前 16 位，
        拿它去和答案里的完整 64 位 key 比对，结果永远是"不匹配"，于是"全部答完了"
        也会被报成一堆待作答（实测踩过：退出码一直卡在 3）。
        """
        if not self.requests_dir.is_dir():
            return []
        items: list[PendingItem] = []
        for path in sorted(self.requests_dir.glob("*.json")):
            if not path.is_file() or not _NAME_RE.match(path.name):
                continue
            try:
                key = json.loads(path.read_text(encoding="utf-8")).get("key")
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(key, str) or not key:
                continue
            if self._read_answer(path.name, key) is None:
                items.append(PendingItem(name=path.name, key=key, path=path))
        return items

    def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.2,
        tools=None,
        response_format=None,
        tool_choice=None,
    ) -> ChatResult:
        """命中答案就返回；没有就把请求写盘并抛 :class:`PendingRequest`。

        ``tools`` / ``response_format`` / ``tool_choice`` 接受但忽略（见类
        docstring）：外置作答方按 messages 作答，写进 key 会让旧答案整体失效。
        """
        key = request_key(purpose=self.purpose, model=self.model, messages=messages)
        name = file_name(purpose=self.purpose, key=key)

        content = self._read_answer(name, key)
        if content is None:
            path = self._write_request(name, key, messages, temperature=temperature)
            item = PendingItem(name=name, key=key, path=path)
            self.misses.append(item)
            raise PendingRequest(key, path=path)

        self.hits += 1
        return ChatResult(content=content, model=self.model)

    def _write_request(self, name: str, key: str, messages: list[ChatMessage], *, temperature: float) -> Path:
        """原子写请求文件（同 key 重跑覆盖，内容相同，天然幂等）。"""
        path = self.requests_dir / name
        payload = {
            "protocol": PROTOCOL,
            "purpose": self.purpose,
            "key": key,
            "model": self.model,
            "created_at": _utc_now(),
            "temperature": temperature,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "answer_to": str(self.answers_dir / name),
            "instructions": list(_INSTRUCTIONS),
        }
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return path

    def _read_answer(self, name: str, key: str) -> str | None:
        """读答案；任何不合规都当"未作答"（**绝不**退化成部分内容）。

        为什么不把"坏了"和"没有"分开处理：两者对调用方的正确动作相同（导出请求
        等作答），而分开处理会诱使实现"跳过坏答案继续用别的答案"—— 那正是串台。
        """
        if not _NAME_RE.match(name):
            return None
        path = self.answers_dir / name
        try:
            resolved = path.resolve()
            if not resolved.is_relative_to(self.answers_dir.resolve()):
                return None
            if not resolved.is_file():
                return None
            if resolved.stat().st_size > self.max_answer_chars * 4:
                return None
            raw = resolved.read_text(encoding="utf-8", errors="replace")
        except (OSError, RuntimeError):
            return None
        if len(raw) > self.max_answer_chars:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        stated = data.get("key")
        if stated is not None and str(stated) != key:
            return None
        content = data.get("content")
        if not isinstance(content, str) or not content.strip():
            return None
        return content


def write_answer(root: Path, *, purpose: str, key: str, content: str) -> Path:
    """写一条答案（供脚本 / 测试 / agent 的工具化调用；协议与 :meth:`_read_answer` 对齐）。"""
    name = file_name(purpose=purpose, key=key)
    answers = Path(root) / ANSWERS_DIRNAME
    answers.mkdir(parents=True, exist_ok=True)
    path = answers / name
    payload = {"protocol": PROTOCOL, "key": key, "content": content, "answered_at": _utc_now()}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def read_request(path: Path) -> dict:
    """读请求文件（供作答方使用）；结构不符抛 ``ValueError``。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("请求文件不是 JSON 对象")
    if data.get("protocol") != PROTOCOL:
        raise ValueError(f"协议不匹配：{data.get('protocol')!r} != {PROTOCOL!r}")
    for field in ("key", "messages"):
        if field not in data:
            raise ValueError(f"请求文件缺少字段: {field}")
    return data


def render_pending_report(items: list[PendingItem], *, root: Path) -> str:
    """待作答清单（终端用；包含"下一条命令"而不是让人去翻文档）。"""
    if not items:
        return ""
    lines = [
        f"待外部作答 {len(items)} 个请求 —— 已写入 {Path(root) / REQUESTS_DIRNAME}",
        "作答方式：读请求文件里的 messages，按 instructions 写 answers/<同名文件>；",
        "          完成后重跑同一条命令即命中（无需 OPENAI_API_KEY，也不发网络请求）。",
    ]
    for item in items[:5]:
        lines.append(f"  - {item.name}")
    if len(items) > 5:
        lines.append(f"  … 其余 {len(items) - 5} 个")
    return "\n".join(lines)


__all__ = [
    "ANSWERS_DIRNAME",
    "DEFAULT_MODEL",
    "DeferredClient",
    "MAX_ANSWER_CHARS",
    "PROTOCOL",
    "PendingItem",
    "PendingRequest",
    "REQUESTS_DIRNAME",
    "file_name",
    "read_request",
    "render_pending_report",
    "request_key",
    "write_answer",
]
