"""Gitea workflow 手写依赖清单的漂移守卫。

`.gitea/workflows/ci.yml` 刻意**不** `pip install -e .`（runner 的 Python 可能低于
requires-python 下限，装包会被直接拒；本仓源码直跑，见 workflow 内注释），所以它的
依赖是**手写清单**。手写清单与 ``pyproject.toml`` 是两份事实，必然漂移：pyyaml 进了
``[dev]``、没进清单，``import yaml`` 会让整个测试**收集**崩掉（不是个别用例红），
而 Gitea 侧没有第二道防线能兜住它。

本守卫的判据：**[project] dependencies 与 [dev] 的每个包名，都必须出现在 workflow
的安装命令里**。方向是"声明 ⊆ 清单"而不是反过来 —— workflow 多装一个工具（uv）
不算错，少装一个声明了的依赖才是事故。

部署仓没有 ``.gitea/``（该 workflow 只在主仓 Gitea 上跑），此处诚实 skip。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".gitea" / "workflows" / "ci.yml"
PYPROJECT = ROOT / "pyproject.toml"


def _canonical(name: str) -> str:
    """PEP 503 规范名：pyYAML / pyyaml / PyYAML 必须判成同一个包。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared_names() -> set[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    specs = [*data["project"]["dependencies"], *data["project"]["optional-dependencies"]["dev"]]
    return {_canonical(re.split(r"[<>=!~;\[ ]", spec, maxsplit=1)[0]) for spec in specs}


def _workflow_install_names() -> set[str]:
    """从安装命令里取包名。

    用带引号的 token 而不是裸词：注释里也写着包名（"pyyaml 必须在"那几行），
    裸词正则会被**注释满足** —— 假绿。引号是 shell 语法，注释里不会有。
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    install_scripts = [
        step["run"]
        for step in workflow["jobs"]["lint-and-test"]["steps"]
        if isinstance(step.get("run"), str) and "uv pip install" in step["run"]
    ]
    assert install_scripts, "ci.yml 里找不到 uv pip install 步骤 —— 安装方式变了，本守卫要跟着改"

    names: set[str] = set()
    for script in install_scripts:
        for spec in re.findall(r'"([^"]+)"', script):
            name = re.split(r"[<>=!~;\[ ]", spec, maxsplit=1)[0]
            if name and not name.startswith("-"):
                names.add(_canonical(name))
    return names


def test_gitea_install_list_covers_every_declared_dependency():
    if not WORKFLOW.exists():
        pytest.skip(".gitea/workflows/ci.yml 只存在于主仓（部署仓不在 Gitea 上跑）")

    declared = _declared_names()
    installed = _workflow_install_names()

    missing = sorted(declared - installed)
    assert not missing, (
        f"pyproject 声明了这些依赖，但 .gitea/workflows/ci.yml 的安装清单里没有: {missing}。"
        "该 workflow 刻意不装包，依赖靠手写清单 —— 漏掉的会在测试收集阶段 ImportError，"
        "把整个 Gitea CI 打崩而不是让个别用例红。补进清单，或真的装包后删掉手写清单。"
    )


def test_the_install_step_actually_installs_something():
    """参数化退化守卫的姊妹条：安装清单本身被清空时，上一条会恒真。"""
    if not WORKFLOW.exists():
        pytest.skip(".gitea/workflows/ci.yml 只存在于主仓")
    assert _workflow_install_names(), "安装清单是空的 —— 守卫已经退化为摆设"
