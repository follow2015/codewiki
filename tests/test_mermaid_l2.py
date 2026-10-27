"""L2 真实渲染校验判据（§4.3）。

L2 是**可选**能力，所以判据的关键不是"能渲染"，而是三态语义：

* 环境不具备 → ``unavailable``，**绝不能冒充通过**（否则读者以为验过了）；
* 渲染成功 → ``ok``；
* 渲染失败 → ``invalid``，图不得落盘。

真跑 mermaid-cli 需要 Node 24 + puppeteer，CI 里不现实，所以这里用 monkeypatch
替换子进程，验证的是**判定逻辑**而不是 mermaid-cli 本身。
"""
from __future__ import annotations

import subprocess

from codewiki.render.mermaid_validator import L2Result, l2_available, validate, validate_l2

FLOW = "flowchart TD\n  A[\"a\"] --> B[\"b\"]\n"
BAD = "flowchart TD\n  A[\"a\" --> B\n"


class _Done:
    def __init__(self, returncode: int, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


def test_l1_failure_short_circuits_l2(monkeypatch):
    """L1 已判坏就不必再花钱调外部进程。"""
    called = []
    monkeypatch.setattr("codewiki.render.mermaid_validator.validate_l2", lambda *_a, **_k: called.append(1) or L2Result("ok", []))

    issues, l2 = validate(BAD, use_l2=True)

    assert issues, "L1 应判坏"
    assert not called and l2 is None


def test_unavailable_is_reported_not_passed(monkeypatch):
    monkeypatch.setattr("codewiki.render.mermaid_validator.l2_available", lambda: None)
    result = validate_l2(FLOW)

    assert result.status == "unavailable"
    assert "未做校验" in result.detail or "mermaid-cli" in result.detail
    assert result.issues == []


def test_render_failure_is_invalid(monkeypatch, tmp_path):
    monkeypatch.setattr("codewiki.render.mermaid_validator.l2_available", lambda: "mmdc")

    def fake_run(argv, **_kwargs):
        return _Done(1, "Parse error on line 2")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = validate_l2(FLOW)

    assert result.status == "invalid"
    assert result.issues[0].code == "L2_RENDER_FAILED"


def test_render_success_requires_svg(monkeypatch):
    """退出码 0 但没产出 svg 也不能算通过 —— 静默失败是最坏的一种失败。"""
    monkeypatch.setattr("codewiki.render.mermaid_validator.l2_available", lambda: "mmdc")
    monkeypatch.setattr(subprocess, "run", lambda argv, **_k: _Done(0))
    assert validate_l2(FLOW).status == "invalid"

    def writes_svg(argv, **_kwargs):
        from pathlib import Path

        Path(argv[-1]).write_text("<svg/>", encoding="utf-8")
        return _Done(0)

    monkeypatch.setattr(subprocess, "run", writes_svg)
    assert validate_l2(FLOW).status == "ok"


def test_timeout_counts_as_invalid(monkeypatch):
    monkeypatch.setattr("codewiki.render.mermaid_validator.l2_available", lambda: "mmdc")

    def boom(argv, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="mmdc", timeout=1)

    monkeypatch.setattr(subprocess, "run", boom)
    result = validate_l2(FLOW)
    assert result.status == "invalid" and result.issues[0].code == "L2_TIMEOUT"


def test_validate_passes_l1_and_l2(monkeypatch):
    monkeypatch.setattr("codewiki.render.mermaid_validator.validate_l2", lambda *_a, **_k: L2Result("ok", []))
    issues, l2 = validate(FLOW, use_l2=True)
    assert not issues and l2 is not None and l2.status == "ok"


def test_l2_available_uses_whitelist(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/mmdc" if name == "mmdc" else None)
    assert l2_available() == "mmdc"
