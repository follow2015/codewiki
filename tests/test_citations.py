"""答案里的 ``file:line`` 引用核对（存在性，不是语义）。"""

from __future__ import annotations

import json

from codewiki.ai.agent import AgentLoop
from codewiki.ai.citations import (
    MAX_CITATIONS_CHECKED,
    extract_citations,
    looks_like_path,
    scan_citations,
    verify_citations,
)
from codewiki.graph import build_graph
from codewiki.render.jsonio import packet_from_parts
from tests.conftest import FakeClient, parse_all


def _repo(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "core.py").write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    (tmp_path / ".env").write_text("K=v\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text("all:\n\techo hi\n", encoding="utf-8")
    return tmp_path


def test_extract_handles_ranges_dedup_and_punctuation():
    text = "见 pkg/core.py:2，以及 (pkg/core.py:1-3)；重复 pkg/core.py:2。版本 v1.2:3 不是文件"
    cits = [str(c) for c in extract_citations(text)]
    assert cits.count("pkg/core.py:2") == 1
    assert "pkg/core.py:1-3" in cits
    assert "v1.2:3" not in cits, "版本号不该被当成文件引用（会白烧一次纠错轮）"


def test_valid_citations_pass(tmp_path):
    repo = _repo(tmp_path)
    assert verify_citations("Service 在 pkg/core.py:2，范围 pkg/core.py:1-3", repo_root=repo) == []


def test_answer_without_citations_is_not_a_problem(tmp_path):
    assert verify_citations("无法确定", repo_root=_repo(tmp_path)) == []


def test_out_of_range_missing_file_and_escape_are_flagged(tmp_path):
    repo = _repo(tmp_path)
    problems = verify_citations(
        "pkg/core.py:99 与 pkg/ghost.py:1 与 ../etc/passwd.txt:1 与 pkg/core.py:0", repo_root=repo
    )
    reasons = " ".join(p.reason for p in problems)
    assert "越界" in reasons and "不存在" in reasons
    assert len(problems) >= 3


def test_dotfile_citation_is_flagged_as_secret(tmp_path):
    """``.env:1`` 必须被核对（并判为密钥）—— 以前它压根不被当作引用，防线是漏的。"""
    problems = verify_citations("配置在 .env:1", repo_root=_repo(tmp_path))
    assert problems and "密钥" in problems[0].reason


def test_extensionless_file_citation_is_checked(tmp_path):
    """``Makefile:2`` 没有后缀，但它是真文件，行号越界要能被抓到。"""
    repo = _repo(tmp_path)
    assert verify_citations("目标见 Makefile:2", repo_root=repo) == []
    problems = verify_citations("目标见 Makefile:99", repo_root=repo)
    assert problems and "越界" in problems[0].reason


def test_suffixed_secret_file_is_still_flagged(tmp_path):
    repo = _repo(tmp_path)
    (tmp_path / "prod.pem").write_text("x\n", encoding="utf-8")
    problems = verify_citations("密钥在 prod.pem:1", repo_root=repo)
    assert problems and "密钥" in problems[0].reason


def test_looks_like_path_separates_versions_modules_and_files():
    assert looks_like_path("pkg/core.py")
    assert looks_like_path(".env")
    assert looks_like_path("Makefile")
    assert looks_like_path("docker/Dockerfile.prod")
    assert not looks_like_path("v1.2")
    assert not looks_like_path("3.10")
    assert not looks_like_path("app.services")
    assert not looks_like_path("foo.xyz")


def test_truncation_is_reported_not_hidden(tmp_path):
    """超过上限的引用必须能被上层看见 —— 静默截断等于把"没查"当成"查过了"。"""
    repo = _repo(tmp_path)
    (tmp_path / "many").mkdir()
    for i in range(MAX_CITATIONS_CHECKED + 5):
        (tmp_path / "many" / f"f{i}.py").write_text("x = 1\n", encoding="utf-8")
    text = " ".join(f"many/f{i}.py:1" for i in range(MAX_CITATIONS_CHECKED + 5))

    report = scan_citations(text, repo_root=repo)
    assert report.checked == MAX_CITATIONS_CHECKED
    assert report.skipped == 5
    assert report.problems == []


def test_agent_reports_citation_truncation(mini_py, tmp_path):
    """交付的答案超出核对上限时，必须留下 AI_CITATION_TRUNCATED 告警。"""
    (tmp_path / "many").mkdir()
    total = MAX_CITATIONS_CHECKED + 3
    for i in range(total):
        (tmp_path / "many" / f"f{i}.py").write_text("x = 1\n", encoding="utf-8")
    answer = " ".join(f"many/f{i}.py:1" for i in range(total))

    result = build_graph(parse_all(mini_py))
    packet = packet_from_parts(
        title="mini", layers=[], nodes=result.nodes, edges=result.edges, annotations=[], diagnostics=[]
    )
    client = FakeClient([json.dumps({"thought": "够了", "final": answer})])
    out = AgentLoop(client, packet, repo_root=tmp_path).run("q")
    assert out.answer == answer and out.finished
    assert any("AI_CITATION_TRUNCATED" in d for d in out.diagnostics)
    assert not any("AI_CITATION_INVALID" in d for d in out.diagnostics)
