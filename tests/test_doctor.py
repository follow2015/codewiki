"""环境自检判据（§9.1）。

doctor **只报告不阻断**（退出码恒 0），所以判据要钉的是"结论是否正确"：

- Python 低于 3.14.7 必须被点出来（本机就是 3.14.3，这条最容易假装没看见）；
- Node 主版本 ≠ 24 → warning（基线钉死 24 LTS 的决策要落到代码里）；
- 缺失的可选依赖只能是 info，不能是 error（否则等于把可选变必选）。
"""

from __future__ import annotations

import sys

import codewiki.cli.doctor as doctor


def test_python_below_baseline_is_flagged(monkeypatch):
    monkeypatch.setattr(sys, "version_info", (3, 14, 3, "final", 0))
    check = doctor._check_python()

    assert check.level == "warning" and "3.14.7" in check.message


def test_python_at_baseline_is_ok(monkeypatch):
    monkeypatch.setattr(sys, "version_info", (3, 14, 8, "final", 0))
    check = doctor._check_python()

    assert check.level == "ok"


def test_version_parsing():
    assert doctor._parse_version("v24.1.0") == (24, 1, 0)
    assert doctor._parse_version("22.13.1") == (22, 13, 1)
    assert doctor._parse_version("") is None


def test_node_missing_is_info_not_error(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _n: None)

    check = doctor._check_node()

    assert check.level == "info"


def test_node_off_baseline_major_is_warning(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _n: "/usr/bin/node")
    monkeypatch.setattr(
        doctor,
        "run",
        lambda *_a, **_k: type("R", (), {"stdout": "v26.10.0\n"})(),
    )

    check = doctor._check_node()

    assert check.level == "warning" and "24" in check.message


def test_node_on_baseline_is_ok(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _n: "/usr/bin/node")
    monkeypatch.setattr(doctor, "run", lambda *_a, **_k: type("R", (), {"stdout": "v24.9.0\n"})())

    assert doctor._check_node().level == "ok"


def test_required_backends_are_present():
    checks = doctor.run_checks()

    names = {c.name for c in checks}
    assert "后端 python/ast" in names and "后端 regex" in names
    assert all(c.level != "error" for c in checks if "python/ast" in c.name or "regex" in c.name)


def test_report_never_hides_a_warning():
    report = doctor.render_report([doctor.Check("warning", "X", "有问题"), doctor.Check("ok", "Y", "正常")])

    assert "结论：warning" in report and "X" in report
