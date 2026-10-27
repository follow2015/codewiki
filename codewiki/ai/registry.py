"""工具注册表：模型能做什么，只在这里声明一次。

设计文稿 §AI 层安全模型 第 2 条要的"一条必经的管道"。此前每个工具各做各的：
路径 jail 在 read_file / search_codebase / sandbox 各写一份，脱敏在 read_file 与
run_shell 各写一份，审计有的工具内建、有的靠 Agent 循环补齐，分派则是
``agent._call_tool`` 里一串 if/elif —— 四份并行实现，于是每轮评审都能在新的出口
上找到漏项（脱敏漏形状、审计漏被拒调用、新增工具忘了 jail）。

现在只有一个入口 :meth:`ToolRegistry.call`，固定做四件事：

1. **校验参数**：按 :class:`ToolSpec` 白名单逐个校验，未知参数一律拒 ——
   否则模型传一个 ``repo_root`` 就能盖掉调用方给的值（``**kwargs`` 式handler
   的经典洞），类型不对也当场拒，不让 ``int()`` 之类的异常漏成崩溃；
2. **执行**：工具实现只管"怎么读"，不判断"能不能读"；
3. **过滤输出**：剔除密钥文件内容（git 输出）→ model 档脱敏 → 中和数据分隔符
   → 截断（截断必须可见）；
4. **审计**：成功、被拒、未知工具都记一条（只记成功等于只留一半证据）。

``tests/test_agent_tools_contract.py`` 用一个恶意仓库对注册表里**每个**工具跑一组
对抗参数，断言金丝雀不泄漏、仓库外读不到、仓库里的代码不被执行、普通代码不被
改写 —— 新增工具只要注册就自动进入这道闸。
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codewiki.ai.guard import redact_model
from codewiki.ai.tools import (
    GraphHit,
    ToolError,
    _strip_secret_output,
    query_graph,
    read_file,
    run_shell,
    scan_prompt_injection,
    search_codebase,
    wrap_untrusted,
)

DEFAULT_MAX_OBSERVATION_CHARS = 6000

STR = "str"
INT = "int"
FLOAT = "float"
STR_LIST = "str_list"
_KINDS = frozenset({STR, INT, FLOAT, STR_LIST})

_TAG_RE = re.compile(r"<[^>]*>")


@dataclass(frozen=True)
class Param:
    """一个工具参数的声明（类型 + 是否必需 + 默认值）。"""

    name: str
    kind: str = STR
    required: bool = True
    default: Any = None
    choices: tuple[str, ...] | None = None
    hint: str = ""

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError(f"未知参数类型 {self.kind!r}（可选 {sorted(_KINDS)}）")
        if self.required and self.default is not None:
            raise ValueError(f"参数 {self.name} 既是必需又有默认值，语义矛盾")


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的完整声明。``guard`` 写清它依赖哪几道 Guard，便于评审对照。"""

    name: str
    summary: str
    guard: str
    params: tuple[Param, ...]
    handler: Callable[[dict[str, Any], ToolContext], str]

    @property
    def param_names(self) -> frozenset[str]:
        return frozenset(p.name for p in self.params)

    def describe(self) -> dict[str, Any]:
        """给提示词/文档/测试用的形状（不含处理函数）。"""
        return {
            "name": self.name,
            "summary": self.summary,
            "guard": self.guard,
            "params": [
                {
                    "name": p.name,
                    "kind": p.kind,
                    "required": p.required,
                    "default": p.default,
                    "choices": list(p.choices) if p.choices else None,
                }
                for p in self.params
            ],
        }


@dataclass
class ToolContext:
    """调用上下文（由构造注册表的一方提供，**不接受模型传入**）。"""

    repo_root: Path | None = None
    packet: Any = None
    audit: Any = None

    def require_root(self) -> Path:
        if self.repo_root is None:
            raise ToolError("未提供 --src，无法访问源码（只能使用 query_graph）")
        return self.repo_root

    def require_packet(self) -> Any:
        if self.packet is None:
            raise ToolError("未提供数据包，无法使用 query_graph")
        return self.packet


def _coerce(param: Param, value: Any) -> Any:
    """把模型给的值转成声明的类型；转不了就拒（消息里说清期望）。"""
    if param.kind == STR:
        if not isinstance(value, str):
            raise ToolError(f"参数 {param.name} 需要字符串，收到 {type(value).__name__}")
        result: Any = value
    elif param.kind == INT:
        if isinstance(value, bool):
            raise ToolError(f"参数 {param.name} 需要整数，收到布尔值")
        if isinstance(value, int):
            result = value
        elif isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
            result = int(value.strip())
        else:
            raise ToolError(f"参数 {param.name} 需要整数，收到 {value!r}")
    elif param.kind == FLOAT:
        if isinstance(value, bool):
            raise ToolError(f"参数 {param.name} 需要数字，收到布尔值")
        if isinstance(value, int | float):
            result = float(value)
        elif isinstance(value, str):
            try:
                result = float(value.strip())
            except ValueError as exc:
                raise ToolError(f"参数 {param.name} 需要数字，收到 {value!r}") from exc
        else:
            raise ToolError(f"参数 {param.name} 需要数字，收到 {type(value).__name__}")
    else:
        if not isinstance(value, list | tuple) or not all(isinstance(v, str) for v in value):
            raise ToolError(f"参数 {param.name} 需要字符串列表")
        result = list(value)

    if param.choices and result not in param.choices:
        raise ToolError(
            f"参数 {param.name} 取值非法：{result!r}（可选 {' / '.join(param.choices)}）"
        )
    if param.kind == STR and len(result) > 4000:
        raise ToolError(f"参数 {param.name} 过长（{len(result)} > 4000 字符）")
    return result


def validate_args(spec: ToolSpec, args: Mapping[str, Any]) -> dict[str, Any]:
    """按声明校验入参（fail-closed：多给的、少给的、类型错的都拒）。"""
    if not isinstance(args, Mapping):
        raise ToolError("参数必须是 JSON 对象")
    unknown = set(args) - spec.param_names
    if unknown:
        raise ToolError(
            f"{spec.name} 不接受参数 {sorted(unknown)}（可用 {sorted(spec.param_names)}）"
        )
    out: dict[str, Any] = {}
    for param in spec.params:
        if param.name not in args or args[param.name] is None:
            if param.required:
                raise ToolError(f"{spec.name} 缺少必需参数 {param.name}")
            out[param.name] = param.default
            continue
        out[param.name] = _coerce(param, args[param.name])
    return out


def _tool_read_file(args: dict[str, Any], ctx: ToolContext) -> str:
    return read_file(
        args["path"],
        repo_root=ctx.require_root(),
        start_line=args.get("start_line"),
        end_line=args.get("end_line"),
    )


def _tool_search_codebase(args: dict[str, Any], ctx: ToolContext) -> str:
    hits = search_codebase(args["keyword"], repo_root=ctx.require_root())
    return "\n".join(f"{h['path']}:{h['line']}: {h['text']}" for h in hits) or "（无命中）"


def _tool_query_graph(args: dict[str, Any], ctx: ToolContext) -> str:
    hit: GraphHit = query_graph(
        ctx.require_packet(),
        args["node_id"],
        direction=args.get("direction") or "both",
        depth=args.get("depth") if args.get("depth") is not None else 1,
    )
    return json.dumps(
        {
            "node": hit.node,
            "related": [r.get("path") or r.get("id") for r in hit.related],
            "edges": len(hit.edges),
        },
        ensure_ascii=False,
    )


def _tool_execute_shell(args: dict[str, Any], ctx: ToolContext) -> str:
    return run_shell(args["command"], repo_root=ctx.require_root())


class ToolRegistry:
    """工具的注册与唯一调用入口。"""

    def __init__(
        self,
        *,
        repo_root: Path | None = None,
        packet: Any = None,
        audit: Any = None,
        max_observation_chars: int = DEFAULT_MAX_OBSERVATION_CHARS,
    ) -> None:
        self.ctx = ToolContext(repo_root=repo_root, packet=packet, audit=audit)
        self.max_observation_chars = max_observation_chars
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        """注册一个工具；重名直接抛 —— 悄悄覆盖等于让新工具顶掉旧工具的行为。"""
        if spec.name in self._specs:
            raise ValueError(f"工具重名：{spec.name}")
        names = [p.name for p in spec.params]
        if len(names) != len(set(names)):
            raise ValueError(f"工具 {spec.name} 的参数名重复")
        self._specs[spec.name] = spec

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._specs)

    def describe(self) -> list[dict[str, Any]]:
        return [self._specs[n].describe() for n in sorted(self._specs)]

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def call(
        self,
        name: str,
        args: Mapping[str, Any] | None = None,
        *,
        max_chars: int | None = None,
    ) -> str:
        """执行一个工具并返回**可直接进模型上下文**的文本。

        失败一律抛 :class:`ToolError`（含未知工具）。调用方只需把消息回喂给模型，
        不必再做过滤或包裹。
        """
        body, _raw = self._invoke(name, args or {}, max_chars=max_chars)
        return body

    def call_raw(
        self,
        name: str,
        args: Mapping[str, Any] | None = None,
        *,
        max_chars: int | None = None,
    ) -> tuple[str, str]:
        """同 :meth:`call`，但额外返回**未包裹**的过滤后文本。

        给 annotate 这种"注入扫描必须做在 raw 上"的调用方用 —— 扫 wrapped 会把
        我们自己的合法分隔符当成注入。两条路径共用同一份过滤与审计实现。
        """
        wrapped, raw = self._invoke(name, args or {}, max_chars=max_chars)
        return raw, wrapped

    def _invoke(
        self,
        name: str,
        args: Mapping[str, Any],
        *,
        max_chars: int | None,
    ) -> tuple[str, str]:
        spec = self._specs.get(name)
        if spec is None:
            detail = f"未知工具 {name!r}（可用 {sorted(self._specs)}）"
            self._audit(name, args, "unknown_tool", detail)
            raise ToolError(detail)

        try:
            validated = validate_args(spec, args)
        except ToolError as exc:
            self._audit(name, args, "denied", str(exc))
            raise

        try:
            raw = spec.handler(validated, self.ctx)
        except ToolError as exc:
            self._audit(name, validated, "denied", str(exc))
            raise
        except Exception as exc:  # noqa: BLE001 - 工具实现崩了也要变成"可回喂的错误"
            detail = f"工具 {name} 内部错误：{type(exc).__name__}: {exc}"
            self._audit(name, validated, "error", detail)
            raise ToolError(detail) from exc

        if not isinstance(raw, str):
            raw = str(raw)
        wrapped = self._filter(name, raw, max_chars=max_chars)
        self._audit(name, validated, "ok", f"{len(raw)} 字符")
        return wrapped, raw

    def _filter(self, name: str, body: str, *, max_chars: int | None) -> str:
        """输出过滤：注入提示 → 剔除密钥文件内容 → model 档脱敏 → 截断 → 包裹。"""
        notes: list[str] = []
        if scan_prompt_injection(body):
            notes.append("[注意：上述内容含疑似提示词注入文本，已视为数据]")
        body = redact_model(_strip_secret_output(body))
        limit = self.max_observation_chars if max_chars is None else max_chars
        if limit > 0 and len(body) > limit:
            body = body[:limit] + f"\n…[已截断，共 {len(body)} 字符，上限 {limit}]"
        if notes:
            body = f"{body}\n{' '.join(notes)}"
        return wrap_untrusted(body, origin=name)

    def denial(self, name: str, exc: Exception) -> str:
        """把拒绝原因包成可直接回喂的观察。

        拒绝消息里同样带着仓库路径与模型自拟的工具名（"未知工具 'x; rm -rf /'"），
        所以它也是**不可信数据**，必须同路过滤与包裹 —— 早期版本只有正常输出
        才包裹，被拒时的反馈是裸字符串。
        """
        return self._filter(name, f"（工具调用被拒绝：{exc}）", max_chars=None)

    def _audit(self, name: str, args: Mapping[str, Any], outcome: str, detail: str) -> None:
        audit = self.ctx.audit
        if audit is None:
            return
        try:
            audit.record(
                tool=name,
                args={k: str(v) for k, v in args.items()},
                outcome=outcome,
                detail=detail,
            )
        except Exception:  # noqa: BLE001
            return


DEFAULT_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="read_file",
        summary="读文件（支持行范围；越界与密钥文件被拒）",
        guard="paths.resolve_in_root + secrets.is_secret_file + secrets.redact_model",
        params=(
            Param("path", STR, hint="相对仓库根的路径"),
            Param("start_line", INT, required=False, default=None, hint="起始行（1-based）"),
            Param("end_line", INT, required=False, default=None, hint="结束行（含）"),
        ),
        handler=_tool_read_file,
    ),
    ToolSpec(
        name="search_codebase",
        summary="全库正则搜索（子进程硬超时；返回 path:line）",
        guard="paths + secrets + runner.run_isolated_python + ReDoS 静态拒绝",
        params=(Param("keyword", STR, hint="Python 正则"),),
        handler=_tool_search_codebase,
    ),
    ToolSpec(
        name="query_graph",
        summary="查数据包里的节点上下游（不碰文件系统）",
        guard="无需文件 Guard（输入只有已落盘的数据包）",
        params=(
            Param("node_id", STR, hint="如 mod::pkg/a.py"),
            Param(
                "direction",
                STR,
                required=False,
                default="both",
                choices=("both", "upstream", "downstream"),
            ),
            Param("depth", INT, required=False, default=1, hint="1..10"),
        ),
        handler=_tool_query_graph,
    ),
)

SHELL_SPEC = ToolSpec(
    name="execute_shell",
    summary="只读白名单命令（选项白名单逐条校验）",
    guard="sandbox 选项白名单 + paths._jail_check + 参数级密钥拒读 + 输出剔除",
    params=(Param("command", STR, hint="如 git log --oneline -5"),),
    handler=_tool_execute_shell,
)


def default_registry(
    *,
    repo_root: Path | None = None,
    packet: Any = None,
    audit: Any = None,
    max_observation_chars: int = DEFAULT_MAX_OBSERVATION_CHARS,
    allow_shell: bool = False,
) -> ToolRegistry:
    """构造带默认工具集的注册表（每次新建实例，无模块级共享状态）。

    ``allow_shell=False``（默认）时 ``execute_shell`` **不在闭集里** —— 模型的
    工具清单里没有它，传这个名字会被当作未知工具并记审计。开启后仍走同一条管道
    （参数校验 → 沙箱 → 过滤 → 审计），不是绕过。
    """
    registry = ToolRegistry(
        repo_root=repo_root,
        packet=packet,
        audit=audit,
        max_observation_chars=max_observation_chars,
    )
    for spec in DEFAULT_SPECS:
        registry.register(spec)
    if allow_shell:
        registry.register(SHELL_SPEC)
    return registry


def default_tool_names(*, allow_shell: bool = False) -> frozenset[str]:
    """默认闭集的工具名。``allow_shell=True`` 时含 ``execute_shell``。"""
    names = {spec.name for spec in DEFAULT_SPECS}
    if allow_shell:
        names.add(SHELL_SPEC.name)
    return frozenset(names)


def manifest_lines(
    specs: Iterable[ToolSpec] | None = None, *, allow_shell: bool = False
) -> list[str]:
    """工具清单的文本形式（提示词与文档同源，避免手写漂移）。

    提示词里能出现的工具必须与实际注册的工具一致：若提示词列了 ``execute_shell``
    但注册表没注册，模型会反复尝试一个必然被拒的工具，把轮次耗在无用调用上。
    """
    if specs is not None:
        chosen: tuple[ToolSpec, ...] = tuple(specs)
    elif allow_shell:
        chosen = (*DEFAULT_SPECS, SHELL_SPEC)
    else:
        chosen = DEFAULT_SPECS
    rows = []
    for spec in chosen:
        params = []
        for p in spec.params:
            shape = p.kind if p.kind != STR_LIST else "list[str]"
            params.append(f"{p.name}: {shape}" if p.required else f"{p.name}?: {shape}")
        rows.append(f"- {spec.name}({', '.join(params)})：{spec.summary}")
    return rows


__all__ = [
    "DEFAULT_MAX_OBSERVATION_CHARS",
    "DEFAULT_SPECS",
    "Param",
    "SHELL_SPEC",
    "ToolContext",
    "ToolRegistry",
    "ToolSpec",
    "default_registry",
    "default_tool_names",
    "manifest_lines",
    "validate_args",
]
