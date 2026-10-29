"""从 ``tsconfig.json`` 推断前端别名（``codewiki init --src`` 用它填 ``ts_aliases``）。

不写进配置的后果很具体：前端几乎不用相对路径，一律 ``@/theme/dark``；没有别名表，
``builder`` 一条都解析不出来 —— 实测 ipip arch-e0 的 610 个前端模块，模块间
IMPORTS 边是 **0**，分层建议里前端各组内聚度全 0%。

而 builder 自己**不能**读 tsconfig（分析的是可能不可信的仓库），所以这件事只能在
本地起草命令里做，且必须过滤掉"目标路径不存在"的别名。
"""

from __future__ import annotations

import json

from codewiki.tsaliases import detect_aliases, load_jsonc, strip_jsonc


def _write(path, text: str) -> None:  # noqa: ANN001
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_strip_jsonc_keeps_urls_inside_strings():
    """``"https://x"`` 里的 ``//`` 不是注释 —— 用正则一刀切会把它砍掉。"""
    text = '{"url": "https://example.com/a", "x": 1} // 行尾注释\n/* 块注释 */'
    assert json.loads(strip_jsonc(text)) == {"url": "https://example.com/a", "x": 1}


def test_strip_jsonc_handles_block_comment_with_star_inside_string():
    text = '{"a": "/* not a comment */", "b": 2}'
    assert json.loads(strip_jsonc(text)) == {"a": "/* not a comment */", "b": 2}


def test_detect_aliases_joins_base_url_and_tsconfig_dir(tmp_path):
    """``baseUrl: "."`` 相对 tsconfig 所在目录，最终要折算成**仓库根**相对路径。"""
    _write(
        tmp_path / "frontend-new" / "tsconfig.json",
        '{\n  // 路径映射\n  "compilerOptions": {\n    "baseUrl": ".",\n'
        '    "paths": {"@/*": ["src/*"]}\n  }\n}',
    )
    (tmp_path / "frontend-new" / "src").mkdir(parents=True)

    assert detect_aliases(tmp_path) == {"@/*": "frontend-new/src/*"}


def test_detect_aliases_respects_non_dot_base_url(tmp_path):
    _write(
        tmp_path / "web" / "tsconfig.json",
        '{"compilerOptions": {"baseUrl": "src", "paths": {"~/*": ["*"]}}}',
    )
    (tmp_path / "web" / "src").mkdir(parents=True)

    assert detect_aliases(tmp_path) == {"~/*": "web/src/*"}


def test_detect_aliases_drops_targets_that_do_not_exist(tmp_path):
    """``@tanstack/*`` 这类 scoped 包的目标在仓库里不存在 —— 必须丢掉。

    猜错会把第三方包吞成本地模块，比"没连"更难发现。
    """
    _write(
        tmp_path / "tsconfig.json",
        '{"compilerOptions": {"baseUrl": ".", "paths": {'
        '"@/*": ["src/*"], "@tanstack/*": ["node_modules/@tanstack/*"]}}}',
    )
    (tmp_path / "src").mkdir()

    assert detect_aliases(tmp_path) == {"@/*": "src/*"}


def test_detect_aliases_ignores_broken_tsconfig(tmp_path):
    """一个坏 tsconfig 不该让起草中断（起草的价值是"先有一份能看的配置"）。"""
    _write(tmp_path / "tsconfig.json", "{ 这不是 JSON")
    assert load_jsonc(tmp_path / "tsconfig.json") is None
    assert detect_aliases(tmp_path) == {}


def test_detect_aliases_skips_node_modules(tmp_path):
    _write(
        tmp_path / "node_modules" / "pkg" / "tsconfig.json",
        '{"compilerOptions": {"baseUrl": ".", "paths": {"x/*": ["*"]}}}',
    )
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True, exist_ok=True)
    assert detect_aliases(tmp_path) == {}


def test_init_writes_detected_aliases_into_the_config(tmp_path):
    """端到端：tsconfig 里的 ``@/*`` 要出现在生成的配置里，并且能被加载器读回。"""
    from codewiki.cli import main
    from codewiki.config import load_config

    repo = tmp_path / "repo"
    _write(repo / "app" / "api.py", "X = 1\n")
    _write(repo / "app" / "svc.py", "Y = 2\n")
    _write(repo / "web" / "src" / "App.tsx", "export const A = 1\n")
    _write(repo / "web" / "src" / "z.ts", "export const Z = 1\n")
    _write(
        repo / "web" / "tsconfig.json",
        '{"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"]}}}',
    )
    out = tmp_path / "cfg"

    assert main(["init", "--src", str(repo), "--path", str(out)]) == 0
    assert load_config(out / "codewiki.toml").analysis.ts_aliases == {"@/*": "web/src/*"}
