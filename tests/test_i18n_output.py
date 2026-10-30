"""``[project] lang`` 决定产物语言 —— 此前它只进缓存指纹，界面文案全硬编码中文。

``<html lang="zh-CN">`` 与"架构总览""诊断摘要"这些固定文案都是写死的，于是
``lang = "en"`` 的实际效果是"什么都没有"。一个字段承诺了一种能力却完全不生效，
比不支持更费解：人会先怀疑自己拼错了。

这一组用例锁的是**出口**而不是某几行代码：英文配置下产出物里不该再出现固定
中文文案，中文配置下的产物一个字都不变。
"""

from __future__ import annotations

import pytest

from codewiki.cli import main
from codewiki.render.i18n import html_lang, normalize, set_lang, t


def _analyze(tmp_path, mini_py, *, lang: str | None):
    out = tmp_path / "wiki-output"
    cfg_lines = ["[project]", 'name = "mini"']
    if lang is not None:
        cfg_lines.append(f'lang = "{lang}"')
    cfg_lines += ["", "[[architecture.layers]]", 'name = "应用层"', 'paths = ["app.py"]']
    cfg = tmp_path / "codewiki.toml"
    cfg.write_text("\n".join(cfg_lines) + "\n", encoding="utf-8")
    code = main(["analyze", "--src", str(mini_py), "--out", str(out), "--config", str(cfg), "--no-cache"])
    assert code == 0
    return out


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("en", "en"),
        ("EN", "en"),
        ("en-US", "en"),
        ("en_US", "en"),
        ("zh", "zh"),
        ("zh-CN", "zh"),
        (None, "zh"),
        ("fr", "zh"),
    ],
)
def test_normalize(raw, expected):
    assert normalize(raw) == expected


def test_t_is_in_place_bilingual():
    set_lang("zh")
    assert t("中文", "English") == "中文"
    set_lang("en")
    assert t("中文", "English") == "English"


def test_html_lang_follows_setting():
    set_lang("en")
    assert html_lang() == "en"
    set_lang("zh")
    assert html_lang() == "zh-CN"


def test_index_md_is_english(tmp_path, mini_py):
    out = _analyze(tmp_path, mini_py, lang="en")
    index = (out / "index.md").read_text(encoding="utf-8")

    assert "## Architecture layers" in index
    assert "## 架构分层" not in index
    assert "## 诊断摘要" not in index


def test_index_html_declares_english(tmp_path, mini_py):
    out = _analyze(tmp_path, mini_py, lang="en")
    html = (out / "index.html").read_text(encoding="utf-8")

    assert '<html lang="en">' in html
    assert "Architecture overview" in html
    assert "架构总览" not in html


def test_module_page_is_english(tmp_path, mini_py):
    out = _analyze(tmp_path, mini_py, lang="en")
    pages = list((out / "modules").glob("*.md"))
    assert pages, "没有模块页，用例没测到东西"
    text = "".join(page.read_text(encoding="utf-8") for page in pages)

    assert "## Symbols" in text or "## Neighbors" in text
    assert "## 符号" not in text
    assert "## 上下游" not in text


def test_diagnostics_md_is_english(tmp_path, mini_py):
    out = _analyze(tmp_path, mini_py, lang="en")
    text = (out / "diagnostics.md").read_text(encoding="utf-8")

    assert text.startswith("# codewiki diagnostics") or "--no-cache" in text


def test_chinese_stays_default(tmp_path, mini_py):
    """不写 lang 时行为必须一个字都不变 —— 绝大多数仓库是这个形态。"""
    out = _analyze(tmp_path, mini_py, lang=None)
    index = (out / "index.md").read_text(encoding="utf-8")
    html = (out / "index.html").read_text(encoding="utf-8")

    assert "## 架构分层" in index
    assert '<html lang="zh-CN">' in html
    assert "架构总览" in html


def test_language_does_not_leak_to_next_run(tmp_path, mini_py):
    """跑完英文再跑中文必须回到中文：上下文变量漏了 reset，产物会静默变英文。"""
    _analyze(tmp_path, mini_py, lang="en")

    out2 = tmp_path / "second"
    cfg = tmp_path / "codewiki.toml"
    cfg.write_text('[project]\nname = "mini"\n', encoding="utf-8")
    assert (
        main(["analyze", "--src", str(mini_py), "--out", str(out2), "--config", str(cfg), "--no-cache"]) == 0
    )

    assert "架构总览" in (out2 / "index.html").read_text(encoding="utf-8")
