"""外置推理（agent 充当模型后端）的判据。

背景：``ai-annotate`` 原本只能走 HTTP，必须有 ``OPENAI_API_KEY``。在自带模型的
agent 环境里（WorkBuddy / CodeBuddy）既没有凭据也不该出网 —— 于是把"调模型"拆成
"导出请求 → agent 作答 → 回灌"。这个文件锁的就是拆分后的三条底线：

1. **缺答案不等于失败**：一次导出要覆盖全部待答模块，不能碰到第一个就 break；
2. **答案必须对得上请求**：key 由内容派生，写错文件 / 改错 key / 坏 JSON 都按
   "未作答"处理，绝不退化成"用一份错答案"；
3. **零网络、零凭据**：不发 HTTP、不读密钥环境变量。
"""

from __future__ import annotations

import json
import os
import re

import pytest

from codewiki.ai.annotate import annotate_modules, protected_targets, select_modules
from codewiki.ai.client import ChatMessage
from codewiki.ai.deferred import (
    PROTOCOL,
    DeferredClient,
    file_name,
    request_key,
    write_answer,
)
from codewiki.cli import main
from codewiki.config import AnnotationSpec, ArchitectureSection, CodewikiConfig, LayerSpec
from codewiki.graph import build_graph
from codewiki.render.jsonio import packet_from_parts
from tests.conftest import parse_all

PROTECTED_NOTE = "人工写的说明，AI 不能覆盖"

MINI_CFG = """[project]
name = "mini"

[[architecture.layers]]
name = "应用层"
paths = ["app.py"]
color = "#B3E5FC"

[[annotations]]
target = "mod::pkg/util.py"
label = "工具"
note = "人工注解：AI 不许覆盖我"
"""

_TARGET_LINE = re.compile(r"^(mod::[^\s|]+)\s*\|", re.M)
_NUMBERED_LINE = re.compile(r"^\s*(\d+)\| ", re.M)
_RAW_LINE = re.compile(r"^\s*[a-zA-Z#@\"]", re.M)


def _packet(src):
    result = build_graph(parse_all(src))
    return packet_from_parts(
        title="mini", layers=[], nodes=result.nodes, edges=result.edges, annotations=[], diagnostics=[]
    )


def _cfg():
    return CodewikiConfig(
        project=CodewikiConfig().project,
        architecture=ArchitectureSection(layers=[LayerSpec(name="应用层", paths=["app.py"])]),
        annotations=[AnnotationSpec(target="mod::pkg/util.py", label="工具", note=PROTECTED_NOTE)],
    )


def _targets(packet, top_n: int) -> list[str]:
    return [
        m["id"]
        for m in select_modules(packet, top_n=top_n, protected=protected_targets(_cfg().annotations, packet))
    ]


def _answer_for(request: dict) -> str:
    """按请求的 user 消息作答：target 取自消息本身，行号取自**消息里带行号的源码**。"""
    user = next(m["content"] for m in request["messages"] if m["role"] == "user")
    target = _TARGET_LINE.search(user).group(1)
    rel = target.removeprefix("mod::")
    lines = [int(n) for n in _NUMBERED_LINE.findall(user)]
    assert lines, "取证片段必须带行号（否则 evidence 只能靠模型数行，校验形同虚设）"
    return json.dumps(
        {
            "modules": [
                {
                    "target": target,
                    "label": "入口",
                    "note": f"负责入口编排，见 {rel}:{lines[0]}",
                    "evidence": [{"path": rel, "lines": [lines[0]]}],
                }
            ],
            "unknown": [],
        }
    )


def _answer_all(root, *, purpose: str = "annotate") -> int:
    """对目录里所有请求作答，返回作答条数。"""
    count = 0
    for path in sorted((root / "requests").glob("*.json")):
        request = json.loads(path.read_text(encoding="utf-8"))
        write_answer(root, purpose=purpose, key=request["key"], content=_answer_for(request))
        count += 1
    return count


def test_missing_answer_exports_every_module_and_does_not_degrade(mini_py, tmp_path):
    """缺答案不是失败：3 个模块要导出 3 个请求。

    此前 annotate 遇到 ``LLMError`` 一律 break —— 直接复用它做导出，一轮只会
    写出第一个请求，30 个模块得来回跑 30 遍才能凑齐清单。
    """
    packet = _packet(mini_py)
    targets = _targets(packet, 3)
    client = DeferredClient(tmp_path, model="agent")

    result = annotate_modules(packet, _cfg(), src_root=mini_py, client=client, top_n=3)

    assert result.degraded is False, "缺答案不该被当成 AI 不可用"
    assert sorted(result.pending) == sorted(targets)
    assert len(list((tmp_path / "requests").glob("*.json"))) == len(targets)


def test_request_is_self_describing(mini_py, tmp_path):
    """请求文件要能让**没读过文档**的 agent 直接作答。"""
    packet = _packet(mini_py)
    annotate_modules(packet, _cfg(), src_root=mini_py, client=DeferredClient(tmp_path), top_n=1)
    request = json.loads(next((tmp_path / "requests").glob("*.json")).read_text(encoding="utf-8"))

    assert request["protocol"] == PROTOCOL
    assert len(request["key"]) == 64
    assert [m["role"] for m in request["messages"]] == ["system", "user"]
    assert request["instructions"] and any("answers" in line for line in request["instructions"])
    assert request["answer_to"].endswith(".json")
    user = next(m["content"] for m in request["messages"] if m["role"] == "user")
    assert "应用层" in user and PROTECTED_NOTE in user


def test_source_snippet_carries_line_numbers(mini_py, tmp_path):
    """取证片段必须带行号 —— 否则"evidence 行号存在"校验只是碰运气。"""
    packet = _packet(mini_py)
    annotate_modules(packet, _cfg(), src_root=mini_py, client=DeferredClient(tmp_path), top_n=1)
    request = json.loads(next((tmp_path / "requests").glob("*.json")).read_text(encoding="utf-8"))
    user = next(m["content"] for m in request["messages"] if m["role"] == "user")

    assert _NUMBERED_LINE.search(user), "源码没带行号前缀"
    assert "<<<CODEWIKI_UNTRUSTED_DATA>>>" in user, "仍要用固定分隔符包裹不可信数据"


def test_answered_requests_become_suggestions(mini_py, tmp_path):
    """作答后重跑：建议数与请求数一致，且不再有待作答。"""
    packet = _packet(mini_py)
    targets = _targets(packet, 2)
    client = DeferredClient(tmp_path)
    annotate_modules(packet, _cfg(), src_root=mini_py, client=client, top_n=2)
    assert _answer_all(tmp_path) == 2

    second = DeferredClient(tmp_path)
    result = annotate_modules(packet, _cfg(), src_root=mini_py, client=second, top_n=2)

    assert result.pending == []
    assert second.hits == 2
    assert [s.target for s in result.suggestions] == targets
    assert not second.pending(), "全部作答后不该再有待作答条目"


def test_answer_key_must_match_the_request(mini_py, tmp_path):
    """答案文件里自带 key 却对不上 ⇒ 视为未作答（防止把旧答案拷错位置）。"""
    packet = _packet(mini_py)
    client = DeferredClient(tmp_path)
    annotate_modules(packet, _cfg(), src_root=mini_py, client=client, top_n=1)
    request = json.loads(next((tmp_path / "requests").glob("*.json")).read_text(encoding="utf-8"))

    write_answer(tmp_path, purpose="annotate", key=request["key"], content=_answer_for(request))
    path = tmp_path / "answers" / file_name(purpose="annotate", key=request["key"])
    data = json.loads(path.read_text(encoding="utf-8"))
    data["key"] = "0" * 64
    path.write_text(json.dumps(data), encoding="utf-8")

    again = DeferredClient(tmp_path)
    result = annotate_modules(packet, _cfg(), src_root=mini_py, client=again, top_n=1)
    assert again.hits == 0 and result.suggestions == []
    assert result.pending, "key 不符应回到待作答"


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda d: "{ 坏 JSON", id="corrupt-json"),
        pytest.param(lambda d: json.dumps({"key": d["key"], "content": "   "}), id="blank-content"),
        pytest.param(lambda d: json.dumps({"key": d["key"], "content": 42}), id="content-not-str"),
        pytest.param(lambda d: json.dumps([d]), id="not-an-object"),
    ],
)
def test_bad_answer_is_treated_as_missing(mini_py, tmp_path, mutate):
    """坏答案只能是"没答"，不能变成"用一半"—— 那正是串台的来源。"""
    packet = _packet(mini_py)
    annotate_modules(packet, _cfg(), src_root=mini_py, client=DeferredClient(tmp_path), top_n=1)
    request = json.loads(next((tmp_path / "requests").glob("*.json")).read_text(encoding="utf-8"))

    write_answer(tmp_path, purpose="annotate", key=request["key"], content=_answer_for(request))
    path = tmp_path / "answers" / file_name(purpose="annotate", key=request["key"])
    path.write_text(mutate(request), encoding="utf-8")

    client = DeferredClient(tmp_path)
    result = annotate_modules(packet, _cfg(), src_root=mini_py, client=client, top_n=1)
    assert client.hits == 0 and result.pending


def test_oversized_answer_is_ignored(mini_py, tmp_path):
    """超限要按"未作答"处理 —— 两道闸（字节粗筛 / 字符精筛）都得真的拦住。

    2000 字符配 1000 上限：JSON 包装后约 2.1KB，低于 ``max*4`` 的字节粗筛门槛，
    因此只有**字符数**那道闸能拦。用 5000 字符会让粗筛先命中，精筛形同虚设
    （实测：删掉字符检查后用例照样通过）。
    """
    packet = _packet(mini_py)
    annotate_modules(packet, _cfg(), src_root=mini_py, client=DeferredClient(tmp_path), top_n=1)
    request = json.loads(next((tmp_path / "requests").glob("*.json")).read_text(encoding="utf-8"))
    write_answer(tmp_path, purpose="annotate", key=request["key"], content="x" * 2000)

    client = DeferredClient(tmp_path, max_answer_chars=1000)
    result = annotate_modules(packet, _cfg(), src_root=mini_py, client=client, top_n=1)
    assert client.hits == 0 and result.pending


def test_symlinked_answer_outside_root_is_rejected(mini_py, tmp_path):
    """答案目录里的符号链接可以指到任意位置 —— 归一化后必须仍在 answers 内。"""
    packet = _packet(mini_py)
    annotate_modules(packet, _cfg(), src_root=mini_py, client=DeferredClient(tmp_path), top_n=1)
    request = json.loads(next((tmp_path / "requests").glob("*.json")).read_text(encoding="utf-8"))

    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"key": request["key"], "content": _answer_for(request)}), encoding="utf-8")
    answers = tmp_path / "answers"
    answers.mkdir(exist_ok=True)
    os.symlink(outside, answers / file_name(purpose="annotate", key=request["key"]))

    client = DeferredClient(tmp_path)
    result = annotate_modules(packet, _cfg(), src_root=mini_py, client=client, top_n=1)
    assert client.hits == 0, "符号链接逃出 answers 目录仍被读了"
    assert result.pending


def test_fake_answer_still_faces_evidence_check(mini_py, tmp_path):
    """答案文件不等同于可信结论：证据行号仍要真实存在。"""
    packet = _packet(mini_py)
    annotate_modules(packet, _cfg(), src_root=mini_py, client=DeferredClient(tmp_path), top_n=1)
    request = json.loads(next((tmp_path / "requests").glob("*.json")).read_text(encoding="utf-8"))
    user = next(m["content"] for m in request["messages"] if m["role"] == "user")
    target = _TARGET_LINE.search(user).group(1)

    write_answer(
        tmp_path,
        purpose="annotate",
        key=request["key"],
        content=json.dumps(
            {
                "modules": [
                    {
                        "target": target,
                        "label": "编的",
                        "note": "行号不存在",
                        "evidence": [{"path": target.removeprefix("mod::"), "lines": [99999]}],
                    }
                ]
            }
        ),
    )
    result = annotate_modules(packet, _cfg(), src_root=mini_py, client=DeferredClient(tmp_path), top_n=1)

    assert result.suggestions == []
    assert [d.code for d in result.diagnostics] == ["AI_EVIDENCE_INVALID"]


def test_key_is_derived_from_content_not_module_name():
    msgs = [ChatMessage(role="system", content="s"), ChatMessage(role="user", content="u")]
    key = request_key(purpose="annotate", model="agent", messages=msgs)

    assert key == request_key(purpose="annotate", model="agent", messages=list(msgs))
    assert key != request_key(purpose="annotate", model="other", messages=msgs)
    assert key != request_key(purpose="other", model="agent", messages=msgs)
    assert key != request_key(
        purpose="annotate",
        model="agent",
        messages=[ChatMessage(role="system", content="s"), ChatMessage(role="user", content="u2")],
    )


def test_message_order_changes_the_key():
    """system 在前还是在后是两件事，不能被"规范化"掉。"""
    a = [ChatMessage(role="system", content="s"), ChatMessage(role="user", content="u")]
    b = [ChatMessage(role="user", content="u"), ChatMessage(role="system", content="s")]
    assert request_key(purpose="annotate", model="agent", messages=a) != request_key(
        purpose="annotate", model="agent", messages=b
    )


@pytest.mark.guarantee("ai-deferred")
def test_no_network_and_no_api_key(mini_py, tmp_path, monkeypatch):
    """把 urlopen 换成一碰就炸：能跑完就证明这条路径真的不出网。"""
    import urllib.request

    def _boom(*_args, **_kwargs):
        raise AssertionError("deferred 模式不应发任何 HTTP 请求")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    result = annotate_modules(
        _packet(mini_py), _cfg(), src_root=mini_py, client=DeferredClient(tmp_path), top_n=2
    )
    assert result.pending and not result.degraded


@pytest.fixture
def prepared(tmp_path, mini_py):
    cfg = tmp_path / "codewiki.toml"
    cfg.write_text(MINI_CFG, encoding="utf-8")
    out = tmp_path / "wiki-output"
    assert (
        main(["analyze", "--src", str(mini_py), "--out", str(out), "--config", str(cfg), "--no-cache"]) == 0
    )
    return {"cfg": cfg, "out": out, "packet": out / "data" / "codewiki.json", "src": mini_py}


def _annotate_args(prepared, queue, *extra):
    return [
        "ai-annotate",
        "--input",
        str(prepared["packet"]),
        "--config",
        str(prepared["cfg"]),
        "--src",
        str(prepared["src"]),
        "--out",
        str(prepared["out"]),
        "--cache-dir",
        str(prepared["out"] / "cache"),
        "--deferred",
        str(queue),
        *extra,
    ]


def test_cli_pending_exit_code_then_zero_after_answers(prepared, tmp_path, monkeypatch, capsys):
    """无 API Key、无网络：导出 → 作答 → 重跑，退出码从 3 变 0 并产出建议文件。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    queue = tmp_path / "ai-queue"

    code = main(_annotate_args(prepared, queue, "--top-n", "2"))
    assert code == 3, "有待作答必须以 3 退出（1 留给真正失败）"
    assert "待外部作答" in capsys.readouterr().err
    assert len(list((queue / "requests").glob("*.json"))) == 2

    assert _answer_all(queue) == 2
    code = main(_annotate_args(prepared, queue, "--top-n", "2"))
    assert code == 0, capsys.readouterr().err

    text = (prepared["out"] / "suggestions" / "annotations.suggest.toml").read_text(encoding="utf-8")
    assert "reviewed = false" in text and 'source = "ai"' in text
    assert 'target = "mod::pkg/util.py"' not in text, "人工注解不得被 AI 触碰"


def test_cli_pending_blocks_auto_write_back(prepared, tmp_path, capsys):
    """半截结果不许自动合并：事后没人分得清哪些结论是完整的。"""
    cfg_before = prepared["cfg"].read_text(encoding="utf-8")
    queue = tmp_path / "ai-queue"

    code = main(_annotate_args(prepared, queue, "--top-n", "1", "--write-back", "auto"))
    assert code == 3
    err = capsys.readouterr().err
    assert "降级为 review-file" in err
    assert prepared["cfg"].read_text(encoding="utf-8") == cfg_before, "待作答时不得改写主配置"


def test_cli_deferred_ignores_base_url(prepared, tmp_path, capsys):
    queue = tmp_path / "ai-queue"
    code = main(_annotate_args(prepared, queue, "--top-n", "1", "--base-url", "http://127.0.0.1:1/v1"))
    assert code == 3
    assert "--base-url 已忽略" in capsys.readouterr().err
