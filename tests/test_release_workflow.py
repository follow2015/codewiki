"""``.github/workflows/release.yml`` 的结构契约。

这是仓库里唯一"只有打 tag 才执行"的代码：平时 CI 不碰、本地不碰。于是它坏掉的
方式是**静默**的 —— 引号写歪、把 ``--tag`` 写成 ``--tags``、引用的脚本没同步过去，
全都要等到发版当天才发现，而发版恰恰是最不该现场调试的时刻。

更具体的两个历史缺口（本文件就是为它们写的）：

1. 它里面的版本校验与 wheel 校验原本是**内联** ``python -c``：本地无法预先跑，
   而且它与 ``scripts/verify-wheel.py`` 是两份实现 —— 副本迟早漂移，而漂移之后
   "CI 过了、发布没过"（或反之）就没有任何单一可信来源可查。
2. 它本身**不在同步白名单里**：主仓写了 111 行，部署仓 ``.github/workflows/`` 里
   只有 ci.yml，打 tag 什么也不会发生。那条守卫在 ``test_sync_to_deploy.py``。

这里钉四件事：
- 触发条件还在（``push.tags: v*``）；
- 每段 run 脚本都过 ``bash -n``；
- 校验逻辑来自 ``scripts/``，且**引用的脚本真实存在**；
- 跑测试的那份 python 是装好 wheel 的 venv，不是源码树。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "release.yml"

_GH_EXPR = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "release.yml 没解析成映射"
    return data


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    """取 ``on:`` 段。

    ⚠️ YAML 1.1（pyyaml 就是 1.1）把裸 ``on`` 解析成布尔 ``True`` —— 于是
    ``workflow["on"]`` 会 KeyError，而 ``workflow[True]`` 才是真触发段。这不是
    写法问题，是规范问题；不处理就会得到"看起来没有触发条件"的假红。
    """
    triggers = workflow.get("on", workflow.get(True))
    assert triggers is not None, "release.yml 没有 on: 触发段"
    assert isinstance(triggers, dict), f"触发段不是映射：{triggers!r}"
    return triggers


def _steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict) and jobs, "release.yml 没有 jobs"
    steps: list[dict[str, Any]] = []
    for job in jobs.values():
        steps.extend(job.get("steps") or [])
    assert steps, "release.yml 里一步都没有"
    return steps


def _run_scripts(workflow: dict[str, Any]) -> list[tuple[str, str]]:
    """``[(步骤名, 脚本)]``；只收带 ``run`` 的步骤。"""
    collected: list[tuple[str, str]] = []
    for index, step in enumerate(_steps(workflow)):
        script = step.get("run")
        if script is None:
            continue
        name = str(step.get("name") or f"step#{index}")
        collected.append((name, str(script)))
    assert collected, "release.yml 没有任何 run 步骤"
    return collected


def test_release_runs_on_version_tags(workflow: dict[str, Any]):
    """触发条件没了 = 打 tag 什么也不发生，而 GitHub 上不会因此报错。"""
    push = _triggers(workflow).get("push")
    assert isinstance(push, dict), f"没有 push 触发：{_triggers(workflow)!r}"
    assert push.get("tags") == ["v*"], f"tag 模式变了：{push.get('tags')!r}"


@pytest.mark.skipif(shutil.which("bash") is None, reason="本机没有 bash")
def test_every_run_script_is_valid_bash(workflow: dict[str, Any]):
    """每段 run 脚本都要过 ``bash -n``。

    这是 release.yml 最现实的失效方式：多行块里的引号/续行/缩进写错，YAML 仍然
    合法（它只是字符串），只有 runner 真正执行时才炸。``bash -n`` 只做语法检查、
    不执行任何命令，所以可以在本地安全地跑。
    """
    for name, script in _run_scripts(workflow):
        probe = _GH_EXPR.sub("GITHUB_EXPRESSION", script)
        result = subprocess.run(
            ["bash", "-n"],
            input=probe,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"步骤「{name}」的 run 脚本语法错误：\n{result.stderr}"


def test_version_check_delegates_to_the_shared_script(workflow: dict[str, Any]):
    """版本校验必须调用 ``scripts/check_release_tag.py``，不许内联重写。

    内联副本只有打 tag 才执行，本地无法预先验；而且它和脚本会各自演化。判据用
    ``tomllib`` 这个**实现细节**：只要有人把解析内联回来，这个词就会出现。
    """
    scripts = _run_scripts(workflow)
    joined = "\n".join(script for _, script in scripts)
    assert "scripts/check_release_tag.py" in joined, "版本校验没有调用共享脚本"
    assert "tomllib" not in joined, (
        "workflow 里又出现了内联的 TOML 解析 —— 版本校验必须只走 "
        "scripts/check_release_tag.py，否则本地无法预先验"
    )


def test_wheel_check_delegates_to_verify_wheel(workflow: dict[str, Any]):
    """wheel 校验必须调用 ``scripts/verify-wheel.py``（与 CI 同一份）。

    原先是内联的 3 条断言（资产/哈希/deploy 泄漏），比脚本弱得多：脚本还比对
    "wheel 的模块清单 == 源码树清单"，那才是漏打包的主判据。注释里写着"同一份
    校验也放在 CI 里"，实际不是同一份 —— 这条就是防止它再退回去。
    """
    scripts = _run_scripts(workflow)
    joined = "\n".join(script for _, script in scripts)
    assert "scripts/verify-wheel.py" in joined, "wheel 校验没有调用共享脚本"
    assert "zipfile" not in joined, (
        "workflow 里又出现了内联的 zip 检查 —— wheel 校验必须只走 scripts/verify-wheel.py，否则两处判据会漂移"
    )


def test_referenced_scripts_actually_exist(workflow: dict[str, Any]):
    """workflow 引用的每个 ``scripts/*`` 都必须真实存在。

    这是"接线"判据：抽脚本时最容易漏的是"主仓有、部署仓没有"或"路径写错"。脚本
    不存在时 runner 报的是 ``can't open file``，而那已经是在发版途中了。
    """
    joined = "\n".join(script for _, script in _run_scripts(workflow))
    referenced = sorted(set(re.findall(r"scripts/[A-Za-z0-9_.-]+", joined)))
    assert referenced, "workflow 没有引用任何 scripts/ 下的脚本？"
    missing = [rel for rel in referenced if not (REPO / rel).is_file()]
    assert not missing, f"workflow 引用了不存在的脚本：{missing}"


def test_tests_run_against_the_installed_wheel_not_the_source_tree(workflow: dict[str, Any]):
    """跑测试的解释器必须是装了 wheel 的那个 venv。

    退回源码树的后果不是"报错"，而是**全绿**：``pip install -e .`` 让打包配置写错
    也照样能 import。判据是三步同时成立 —— 用 venv 的 python、cwd 在临时目录、
    ``-o pythonpath=`` 清掉源码树路径。

    ⚠️ 必须**按行**判断 ``-m pytest`` 那条命令，不能只看"这段脚本里出现过 venv 路径"：
    同一步骤的上一行是 ``"$RUNNER_TEMP/release/bin/python" -m pip install pytest``，
    它足以让"出现了就算过"的宽松判据恒真 —— 于是把 pytest 改成源码树的 ``python``
    也能存活（实测变异 M5 就是这么活下来的）。
    """
    pytest_steps = [(name, script) for name, script in _run_scripts(workflow) if "-m pytest" in script]
    assert pytest_steps, "release.yml 不再对构建产物跑测试"

    for name, script in pytest_steps:
        commands = [line for line in script.splitlines() if "-m pytest" in line]
        assert commands, f"步骤「{name}」里找不到 pytest 命令"
        for command in commands:
            assert "$RUNNER_TEMP/release/bin/python" in command, (
                f"步骤「{name}」跑的这条 pytest 没用装好 wheel 的 venv：{command.strip()}"
            )
        assert "-o pythonpath=" in script, f"步骤「{name}」没清 pythonpath —— 仓库根的源码树会顶掉已安装的包"


def test_installed_package_is_proven_to_come_from_site_packages(workflow: dict[str, Any]):
    """装完之后必须硬断言 import 自 site-packages。

    没有这条，"跑测试全绿"可能测的是源码树 —— 那等于没验打包。
    """
    joined = "\n".join(script for _, script in _run_scripts(workflow))
    assert "site-packages" in joined, "缺少「import 自 site-packages」的自证"


def test_site_packages_assertion_runs_away_from_the_repo_root(workflow: dict[str, Any]):
    """自证断言所在步骤必须先 cd 出仓库根。

    `python -c` 会把 cwd 塞进 sys.path —— v1.0.1 首跑（run 40621075897）就是在
    仓库根跑这条断言，import 到源码树当场 AssertionError。wheel 装没装对根本
    不是问题，问题出在步骤自己的工作目录。
    """
    steps = [(name, script) for name, script in _run_scripts(workflow) if "site-packages" in script]
    assert steps, "缺少「import 自 site-packages」的自证"

    for name, script in steps:
        lines = script.splitlines()
        assert_idx = next(i for i, ln in enumerate(lines) if "site-packages" in ln)
        cd_idx = next(
            (i for i, ln in enumerate(lines) if ln.strip().startswith('cd "$RUNNER_TEMP"')),
            -1,
        )
        assert cd_idx != -1 and cd_idx < assert_idx, (
            f"步骤「{name}」在 cd 出仓库根之前就 import 了 —— python -c 会把 cwd 塞进 "
            "sys.path，源码树会顶掉已安装的包（v1.0.1 首跑实测翻车点）"
        )


def test_publish_and_artifact_steps_are_present(workflow: dict[str, Any]):
    """发布动作与产物上传都在：少了任何一步，tag 打完也只是"构建了一次"。"""
    joined = "\n".join(script for _, script in _run_scripts(workflow))
    assert "gh release create" in joined, "没有创建 GitHub Release"
    uses = [str(step.get("uses")) for step in _steps(workflow) if step.get("uses")]
    assert any(u.startswith("actions/upload-artifact@") for u in uses), "没有上传构建产物"


def test_every_action_is_pinned_to_a_version(workflow: dict[str, Any]):
    """``uses:`` 必须带版本：``@main`` 这类写法等于把发布流程交给上游的任意提交。"""
    unpinned = [
        str(step.get("uses"))
        for step in _steps(workflow)
        if step.get("uses") and "@" not in str(step.get("uses"))
    ]
    assert not unpinned, f"这些 action 没有固定版本：{unpinned}"
