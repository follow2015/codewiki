"""analyze 的扫描口径判据（2026-10-23 评审 M1 修复回归）。

四个缺口都曾是"配置写了却不生效 / 失败静默消失"：

1. ``--exclude`` 必须与配置 ``[analysis] exclude`` **合并**（替换语义会把点目录
   扫进图谱 —— IPIP 的 .workbuddy 污染事故根源）；
2. ``[languages]`` 白名单必须传给扫描器（命中才解析，其余不进图谱）；
3. 无可用后端的语言必须显式诊断（注释声称"诊断里体现"但实际没有）；
4. ``--no-cache`` 下解析失败必须可见（stats 诊断此前只在有缓存时输出）。
"""
from __future__ import annotations

import json
from pathlib import Path

from codewiki.cli import main


def _codes(out: Path) -> list[dict]:
    packet = json.loads((out / "data" / "codewiki.json").read_text(encoding="utf-8"))
    return packet["diagnostics"]


def _module_paths(out: Path) -> set[str]:
    packet = json.loads((out / "data" / "codewiki.json").read_text(encoding="utf-8"))
    return {n["path"] for n in packet["nodes"] if n["type"] == "Module"}


def _mk_repo(root: Path) -> None:
    (root / "app").mkdir(parents=True)
    (root / "app" / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    for name in ("skipcfg", "skipcli"):
        noise = root / "app" / name
        noise.mkdir()
        (noise / "n.py").write_text("N = 1\n", encoding="utf-8")


def test_cli_exclude_merges_with_config_exclude(tmp_path):
    """CLI --exclude 与配置 exclude 是并集，不是替换（help 写的是"追加"）。"""
    root = tmp_path / "repo"
    _mk_repo(root)
    (root / "codewiki.toml").write_text(
        '[analysis]\nexclude = ["skipcfg"]\n', encoding="utf-8"
    )
    out = tmp_path / "out"

    assert main(["analyze", "--src", str(root), "--out", str(out)]) == 0
    paths = _module_paths(out)
    assert not any("skipcfg" in p for p in paths), "配置 exclude 未生效"
    assert any(p == "app/a.py" for p in paths)

    out2 = tmp_path / "out2"
    assert main(["analyze", "--src", str(root), "--out", str(out2), "--exclude", "skipcli"]) == 0
    paths2 = _module_paths(out2)
    assert not any("skipcfg" in p for p in paths2), "CLI --exclude 把配置 exclude 顶掉了"
    assert not any("skipcli" in p for p in paths2), "CLI --exclude 未生效"
    assert any(p == "app/a.py" for p in paths2)


def test_cli_exclude_still_works_without_config(tmp_path):
    """无配置文件时 --exclude 照常工作（并集语义不能破坏默认路径）。"""
    root = tmp_path / "repo"
    _mk_repo(root)
    out = tmp_path / "out"
    assert main(["analyze", "--src", str(root), "--out", str(out), "--exclude", "skipcfg"]) == 0
    assert not any("skipcfg" in p for p in _module_paths(out))
    assert any(p == "app/a.py" for p in _module_paths(out))


def test_languages_whitelist_filters_scan(tmp_path):
    """[languages] 只配 python ⇒ 非 python 文件不进图谱（白名单生效）。"""
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "app" / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "app" / "notes.md").write_text("# doc\n", encoding="utf-8")
    (root / "app" / "data.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    (root / "codewiki.toml").write_text(
        "[languages]\n[languages.python]\next = ['.py']\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    assert main(["analyze", "--src", str(root), "--out", str(out)]) == 0

    paths = _module_paths(out)
    assert any(p == "app/a.py" for p in paths), "白名单命中的语言必须照常解析"
    assert not any(p.endswith((".md", ".csv")) for p in paths), "白名单外的文件不应进图谱"


def test_backend_missing_language_is_reported(tmp_path, monkeypatch):
    """注册表里没有的后端语言 ⇒ 显式诊断，不静默消失。"""
    import codewiki.cli as cli_mod
    from codewiki.analyzers import REGISTRY, register_defaults

    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "m.ts").write_text("export const x = 1;\n", encoding="utf-8")
    out = tmp_path / "out"

    def _defaults_without_treesitter():
        register_defaults()
        for lang in ("typescript", "tsx", "javascript", "jsx"):
            REGISTRY._factories.pop((lang, "treesitter"), None)

    monkeypatch.setattr(cli_mod, "register_defaults", _defaults_without_treesitter)

    assert main(["analyze", "--src", str(root), "--out", str(out)]) == 0

    diags = _codes(out)
    missing = [d for d in diags if d["code"] == "BACKEND_MISSING"]
    assert missing, f"应有 BACKEND_MISSING 诊断: {[d['code'] for d in diags]}"
    assert "typescript" in missing[0]["message"]


def test_parse_failure_visible_without_cache(tmp_path, monkeypatch):
    """--no-cache 时解析失败也要可见（PARSE_FAILED 诊断 + 计数）。"""
    import codewiki.cli as cli_mod
    from codewiki.analyzers import REGISTRY, register_defaults

    register_defaults()
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "app" / "good.py").write_text("A = 1\n", encoding="utf-8")
    (root / "app" / "bad.py").write_text("B = 2\n", encoding="utf-8")
    out = tmp_path / "out"

    real = REGISTRY.get("python", "ast")

    class Wrapper:
        language = "python"

        def parse(self, path, content):
            if path.name == "bad.py":
                raise SyntaxError("simulated parse failure")
            return real.parse(path, content)

    def _defaults_with_wrapper():
        register_defaults()
        REGISTRY.register("python", "ast", Wrapper)

    monkeypatch.setattr(cli_mod, "register_defaults", _defaults_with_wrapper)

    assert main(["analyze", "--src", str(root), "--out", str(out), "--no-cache"]) == 0

    diags = _codes(out)
    assert any(d["code"] == "PARSE_FAILED" and "bad.py" in d.get("where", "") for d in diags), diags
    assert any("good.py" in str(p) for p in _module_paths(out)), "好文件不受影响"
