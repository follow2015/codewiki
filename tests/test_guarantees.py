"""README 承诺与测试的配对自检（设计文稿 §测试 · "承诺必须有测试"）。

CI 里会单独跑 ``scripts/check_guarantees.py``；这里再断言一次，是为了让"承诺
漂移"在本地 pytest 就红 —— 而不是等推送后才发现文档早就没跟上代码。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = ROOT / "scripts" / "check_guarantees.py"


def _checker():
    """按路径加载检查脚本（scripts/ 不是包，不能直接 import）。"""
    spec = importlib.util.spec_from_file_location("check_guarantees", _SCRIPT)
    assert spec is not None and spec.loader is not None, f"加载失败：{_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_readme_guarantee_has_a_test():
    """README 里标了 ``<!-- guarantee: id -->`` 的承诺，必须有测试认领。

    反向也查：测试标了 README 里不存在的 id ⇒ 拼错或承诺已被删而测试没跟上；
    同一 id 出现两次 ⇒ 改一处会漏另一处。
    """
    problems = _checker().check(ROOT)
    assert not problems, "README 承诺与测试不一致：\n" + "\n".join(problems)


def test_guarantee_marker_is_declared_in_pytest_config():
    """``guarantee`` marker 必须在 pyproject 注册。

    未注册时 pytest 只打一条 warning（不失败），于是 ``pytest -m guarantee``
    会静默选不到任何用例 —— 那正是"以为在守、其实没守"的形态。
    """
    ini = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "guarantee(id)" in ini, "pyproject 的 [tool.pytest.ini_options].markers 缺少 guarantee"
