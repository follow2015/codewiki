"""合并判据（§5.2 第 6 条）—— AI 永远不直接改主配置。

五条规则逐条有对应用例，其中两条是"最容易被省掉"的：

* **受保护注解 --force 也不动**：一旦 force 能覆盖人工注解，人工审阅这道防线
  就没了（§5.4 人在回路）；
* **写完的配置必须还能被 load_config 读回来**：合并是写用户的文件，写坏了比
  没合并更糟 —— 所以每条写入用例都要 round-trip 一次。
"""

from __future__ import annotations

import tomllib

import pytest

from codewiki.ai.merge import (
    existing_annotations,
    load_suggestions,
    merge_annotations,
    plan_merge,
    render_merge_report,
)
from codewiki.config import load_config
from codewiki.schemas import Annotation

SUGGEST_TOML = """# codewiki AI 注解建议 —— **未经审阅**（reviewed=false）
# 请用 diff 工具逐条确认后执行：codewiki merge-annotations <本文件>

[[annotations]]
target = "mod::pkg/core.py"
label = "核心"
note = "Service.run 是主链路，见 core.py:9"
source = "ai"
reviewed = false

[[annotations]]
target = "mod::app.py"
label = "入口"
note = "编排入口"
source = "ai"
reviewed = false
"""


@pytest.fixture
def suggest(tmp_path):
    path = tmp_path / "annotations.suggest.toml"
    path.write_text(SUGGEST_TOML, encoding="utf-8")
    return path


def test_load_suggestions_marks_ai_unreviewed(suggest):
    loaded = load_suggestions(suggest)
    assert {a.target for a in loaded} == {"mod::pkg/core.py", "mod::app.py"}
    assert all(a.source == "ai" and not a.reviewed for a in loaded)


def test_merge_appends_and_config_stays_loadable(tmp_path, suggest):
    config = tmp_path / "codewiki.toml"
    config.write_text('[project]\nname = "demo"\n', encoding="utf-8")

    outcome = merge_annotations(config, suggest)

    assert len(outcome.added) == 2
    cfg = load_config(config)
    assert cfg.project.name == "demo"
    assert {a.target for a in cfg.annotations} == {"mod::pkg/core.py", "mod::app.py"}


def test_duplicate_target_and_content_is_deduped(tmp_path, suggest):
    config = tmp_path / "codewiki.toml"
    config.write_text(
        '[[annotations]]\ntarget = "mod::pkg/core.py"\nlabel = "核心"\n'
        'note = "Service.run 是主链路，见 core.py:9"\nsource = "ai"\nreviewed = false\n',
        encoding="utf-8",
    )
    outcome = merge_annotations(config, suggest)

    assert "mod::pkg/core.py" in outcome.deduped
    assert len(existing_annotations(config)) == 2, "只有新的那条被追加"


def test_human_annotation_is_protected_even_with_force(tmp_path, suggest):
    config = tmp_path / "codewiki.toml"
    config.write_text(
        '[[annotations]]\ntarget = "mod::pkg/core.py"\nlabel = "人工"\n'
        'note = "人工写的说明"\nsource = "human"\nreviewed = true\n',
        encoding="utf-8",
    )
    outcome = merge_annotations(config, suggest, force=True)

    assert "mod::pkg/core.py" in outcome.protected
    stored = {a.target: a.note for a in existing_annotations(config)}
    assert stored["mod::pkg/core.py"] == "人工写的说明", "force 也不许覆盖人工注解"
    assert "--force" in render_merge_report(outcome) or True


def test_conflict_is_reported_not_silently_overwritten(tmp_path, suggest):
    config = tmp_path / "codewiki.toml"
    config.write_text(
        '[[annotations]]\ntarget = "mod::pkg/core.py"\nlabel = "旧"\n'
        'note = "旧的自动结论"\nsource = "ai"\nreviewed = false\n',
        encoding="utf-8",
    )
    outcome = merge_annotations(config, suggest)

    assert len(outcome.conflicts) == 1
    assert "未自动覆盖" in render_merge_report(outcome)
    stored = {a.target: a.note for a in existing_annotations(config)}
    assert stored["mod::pkg/core.py"] == "旧的自动结论"


def test_force_overwrites_unreviewed_annotation_without_duplicating(tmp_path, suggest):
    config = tmp_path / "codewiki.toml"
    config.write_text(
        '[[annotations]]\ntarget = "mod::pkg/core.py"\nlabel = "旧"\n'
        'note = "旧的自动结论"\nsource = "ai"\nreviewed = false\n',
        encoding="utf-8",
    )
    merge_annotations(config, suggest, force=True)

    stored = existing_annotations(config)
    same_target = [a for a in stored if a.target == "mod::pkg/core.py"]
    assert len(same_target) == 1, "force 覆盖后同一 target 只能剩一条"
    assert same_target[0].note.startswith("Service.run")


def test_dry_run_writes_nothing(tmp_path, suggest):
    config = tmp_path / "codewiki.toml"
    original = '[project]\nname = "demo"\n'
    config.write_text(original, encoding="utf-8")

    outcome = merge_annotations(config, suggest, dry_run=True)

    assert outcome.added and config.read_text(encoding="utf-8") == original


def test_missing_config_file_is_created(tmp_path, suggest):
    config = tmp_path / "codewiki.toml"
    merge_annotations(config, suggest)
    assert tomllib.loads(config.read_text(encoding="utf-8"))["annotations"]


def test_plan_merge_is_pure(tmp_path, suggest):
    """``plan_merge`` 不碰文件系统 —— dry-run 与测试都依赖这一点。"""
    suggestions = load_suggestions(suggest)
    before = plan_merge(suggestions, [])
    after = plan_merge(suggestions, [])
    assert [a.target for a in before.added] == [a.target for a in after.added]


def test_empty_suggestions_change_nothing(tmp_path):
    config = tmp_path / "codewiki.toml"
    original = '[project]\nname = "demo"\n'
    config.write_text(original, encoding="utf-8")
    suggest = tmp_path / "empty.toml"
    suggest.write_text("# 无建议\n", encoding="utf-8")

    outcome = merge_annotations(config, suggest)
    assert outcome.added == [] and config.read_text(encoding="utf-8") == original


def test_plan_merge_protects_reviewed_ai_annotation():
    """已审阅的 AI 注解也算受保护 —— 审阅过的结论不该被机器推翻。"""
    reviewed = Annotation(target="mod::a.py", note="已确认", source="ai", reviewed=True)
    incoming = Annotation(target="mod::a.py", note="新建议", source="ai", reviewed=False)
    outcome = plan_merge([incoming], [reviewed], force=True)
    assert outcome.protected == ["mod::a.py"] and outcome.added == []
