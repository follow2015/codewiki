"""审计留痕的深度：拒绝要落盘，长内容不能只留开头。

背景（2026-11-04 复核 ipip 产物时发现）：``ai-audit.jsonl`` 里看起来"全程可溯"，
实际上两处断链——

1. **证据门禁的拒绝不落盘**。``AI_EVIDENCE_UNSUPPORTED`` 只进 ``result.diagnostics``，
   而 diagnostics 随产物发布、按需过滤；被丢弃的作答在审计里没有独立记录。复核时
   只能靠比对两轮 ``llm.chat`` 的 detail 差异反推"哪些被拒了"——换个场景（两轮都
   写同样的错答案）就完全看不出来。
2. **detail 一律截断到 240 字符**（``summarize``）。模型输出里 file:line 证据通常在
   末尾，正好被切掉；于是"可溯"实际只能溯到"谁、何时、成败"，溯不到答了什么。

判据：拒绝必须有一条独立审计行（带 code），长内容必须能完整取回。
"""

from __future__ import annotations

import json

from codewiki.ai.annotate import annotate_modules
from codewiki.ai.audit import AuditLog, summarize
from tests.conftest import FakeClient
from tests.test_ai_annotate import (
    _cfg,
    _evidence_payload,
    _first_target,
    _packet,
)


def _rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_summarize_keeps_its_short_form():
    """``summary`` 的短摘要语义不变（既有读取方按它显示）。"""
    text = "x" * 500
    out = summarize(text)
    assert len(out) == 241
    assert out.endswith("…")


def test_long_detail_keeps_full_copy(tmp_path):
    """超限时 ``detail`` 仍是短摘要，但完整版必须能取回。"""
    path = tmp_path / "a.jsonl"
    body = "开头" + "中段" * 200 + "app/api/device.py:1105"
    with AuditLog(path) as audit:
        audit.record(
            tool="llm.chat", args={"target": "mod::a.py"}, outcome="ok", detail=body, full_detail=True
        )

    row = _rows(path)[0]
    assert row["detail"].endswith("…") and len(row["detail"]) == 241
    assert row["detail_full"] == body, "完整内容必须无截断地落盘"
    assert row["detail_chars"] == len(body)
    assert "device.py:1105" not in row["detail"], "短摘要里本来就该看不到结尾（这正是要修的地方）"


def test_short_detail_does_not_pay_for_a_second_field(tmp_path):
    """没超限就不写 ``detail_full``——审计文件不该为短内容翻倍。"""
    path = tmp_path / "a.jsonl"
    with AuditLog(path) as audit:
        audit.record(tool="llm.chat", args={"target": "m"}, outcome="ok", detail="短", full_detail=True)

    row = _rows(path)[0]
    assert "detail_full" not in row and "detail_chars" not in row


def test_full_detail_is_still_redacted(tmp_path):
    """完整版走同一档脱敏：留痕不能成为泄密钥的旁路。

    假密钥**运行时拼接**：写成字面量会被 sync-to-deploy 的机密扫描拦下
    （PASSWORD_ASSIGN 命中），而这里要验的是脱敏行为，不是扫描器的豁免。
    """
    path = tmp_path / "a.jsonl"
    fake = "sk-" + "live-" + "abcdef0123456789" + "abcd"
    body = f"api_key = '{fake}' " + "x" * 300
    with AuditLog(path) as audit:
        audit.record(tool="llm.chat", args={"target": "m"}, outcome="ok", detail=body, full_detail=True)

    row = _rows(path)[0]
    assert fake not in row["detail_full"], "完整版必须同样脱敏"


def test_rejected_evidence_is_recorded_in_audit(mini_py, tmp_path):
    """行号真实但证据不支持结论 ⇒ 审计里要有独立的 rejected 行。"""
    target = _first_target(mini_py)
    payload = _evidence_payload(target, note="`MAX_RETRY` 控制重试次数上限", path="pkg/core.py", lines=[4])
    path = tmp_path / "ai-audit.jsonl"
    with AuditLog(path) as audit:
        result = annotate_modules(
            _packet(mini_py), _cfg(), src_root=mini_py, client=FakeClient([payload]), top_n=1, audit=audit
        )

    assert result.suggestions == []
    rows = [r for r in _rows(path) if r["tool"] == "llm.evidence"]
    assert len(rows) == 1, "被丢弃的作答必须留下一条独立审计行"
    assert rows[0]["outcome"] == "rejected"
    assert rows[0]["code"] == "AI_EVIDENCE_UNSUPPORTED"
    assert rows[0]["args"]["target"] == target
    detail = rows[0].get("detail_full") or rows[0]["detail"]
    assert "MAX_RETRY" in detail
    assert "pkg/core.py:4" in detail


def test_accepted_evidence_leaves_no_rejection_row(mini_py, tmp_path):
    """通过的作答不该造出 rejected 行——否则拒绝记录会失去信号。"""
    target = _first_target(mini_py)
    payload = _evidence_payload(target, note="`MAX_RETRY` 控制重试次数上限", path="pkg/core.py", lines=[23])
    path = tmp_path / "ai-audit.jsonl"
    with AuditLog(path) as audit:
        result = annotate_modules(
            _packet(mini_py), _cfg(), src_root=mini_py, client=FakeClient([payload]), top_n=1, audit=audit
        )

    assert result.suggestions, "这条证据应当通过"
    assert [r for r in _rows(path) if r["tool"] == "llm.evidence"] == []


def test_chat_record_keeps_full_model_output(mini_py, tmp_path):
    """模型原文必须完整落盘——证据门禁的判定依据就是这段原文。"""
    target = _first_target(mini_py)
    long_note = "`MAX_RETRY` 控制重试次数上限，" + "这段填充是为了把长度顶过摘要阈值。" * 20 + "zzendmark"
    payload = _evidence_payload(target, note=long_note, path="pkg/core.py", lines=[23])
    path = tmp_path / "ai-audit.jsonl"
    with AuditLog(path) as audit:
        annotate_modules(
            _packet(mini_py), _cfg(), src_root=mini_py, client=FakeClient([payload]), top_n=1, audit=audit
        )

    chat = [r for r in _rows(path) if r["tool"] == "llm.chat"][0]
    assert "zzendmark" not in chat["detail"], "短摘要本来就该看不到结尾（这正是修之前的状态）"
    assert "zzendmark" in chat["detail_full"], "完整原文必须可读回"
    assert chat["detail_chars"] > 240


def test_cache_hit_is_recorded(mini_py, tmp_path):
    """命中缓存要留痕——否则审计里分不清"真调了"和"复用旧答案"。"""
    target = _first_target(mini_py)
    payload = _evidence_payload(target, note="`MAX_RETRY` 控制重试次数上限", path="pkg/core.py", lines=[23])
    path = tmp_path / "ai-audit.jsonl"
    cache = tmp_path / "cache"
    with AuditLog(path) as audit:
        for _ in range(2):
            annotate_modules(
                _packet(mini_py),
                _cfg(),
                src_root=mini_py,
                client=FakeClient([payload]),
                top_n=1,
                audit=audit,
                cache_dir=cache,
            )

    rows = _rows(path)
    hits = [r for r in rows if r["tool"] == "llm.cache" and r["outcome"] == "hit"]
    assert len(hits) == 1, "第二轮应当命中缓存并单独记一行"
    assert hits[0]["args"]["target"] == target
    assert len([r for r in rows if r["tool"] == "llm.chat"]) == 1, "命中缓存不得再调模型"


def test_target_mismatch_is_recorded(mini_py, tmp_path):
    """模型串台（返回别的模块）同样是丢弃，也要留痕。"""
    target = _first_target(mini_py)
    payload = _evidence_payload("mod::somewhere/else.py", note="`MAX_RETRY`", path="pkg/core.py", lines=[23])
    path = tmp_path / "ai-audit.jsonl"
    with AuditLog(path) as audit:
        result = annotate_modules(
            _packet(mini_py), _cfg(), src_root=mini_py, client=FakeClient([payload]), top_n=1, audit=audit
        )

    assert result.suggestions == []
    rows = [r for r in _rows(path) if r["tool"] == "llm.evidence"]
    assert len(rows) == 1
    assert rows[0]["code"] == "AI_TARGET_MISMATCH"
    assert rows[0]["outcome"] == "rejected", "串台是丢弃，不能记成正常通过"
    assert rows[0]["args"]["target"] == target, "留痕要记被问的模块，不是模型瞎报的那个"
