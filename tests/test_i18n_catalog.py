"""语言包质量门禁 —— 锁「加一门语言」这件事真的只是加一个表。

此前 ``t(中文, English)`` 是**就地双语**：调用点只认两种语言，加第三种要把每处
调用从两个参数改成三个参数，而第三个参数在哪儿都是空的。那不是多语言，是双方言。

改成键表之后，「漏翻」这类问题的形态变了 —— 不再是「英文产物里露出中文」，而是
「新增文案直接写中文字面量，压根没进表」。所以门禁也换了一套，照 ipip 的做法：

1. **键集对齐**：每个语言的键必须与默认语言完全相同（缺 and 多都报）。
2. **占位符对齐**：同一键在各语言的 ``{name}`` 集合必须相同，否则切语言才炸。
3. **调用点核对**：``t("x.y")`` 用到的键必须真的在表里。
4. **漏抽扫描**：``codewiki/render/`` 里含中文的字符串字面量必须已进表，
   确属配置键/注释的用行内 ``# i18n-ignore`` 显式豁免。

第 4 条是 ipip ``i18n-check.mjs scan`` 的等价物：伪语言巡检要人眼在浏览器里
看，这个能在 CI 里跑，把「英文界面露中文」在合码前拦下来。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from codewiki.render.i18n import current_lang, html_lang, normalize, set_lang, t
from codewiki.render.locales import CATALOGS, DEFAULT_LANG

REPO = Path(__file__).resolve().parent.parent
RENDER_DIR = REPO / "codewiki" / "render"

HAN = "\u4e00-\u9fff"


def _han(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def test_every_language_has_exactly_the_default_keys():
    expected = set(CATALOGS[DEFAULT_LANG])
    for lang, catalog in CATALOGS.items():
        missing = expected - set(catalog)
        extra = set(catalog) - expected
        assert not missing, f"{lang} 缺少键：{sorted(missing)[:8]}"
        assert not extra, f"{lang} 多出键（默认语言没有）：{sorted(extra)[:8]}"


def _fields(text: str) -> set[str]:
    import string

    try:
        return {name for _, name, _, _ in string.Formatter().parse(text) if name}
    except ValueError as exc:
        raise AssertionError(f"文案里的花括号不成对：{text!r}（{exc}）") from exc


def test_placeholders_match_across_languages():
    import string

    for key, zh in CATALOGS[DEFAULT_LANG].items():
        ref = {n for _, n, _, _ in string.Formatter().parse(zh) if n}
        for lang, catalog in CATALOGS.items():
            if lang == DEFAULT_LANG:
                continue
            got = _fields(catalog[key])
            assert got == ref, f"{lang} 的 {key} 占位符不一致：期望 {sorted(ref)}，实际 {sorted(got)}"


def test_no_empty_or_padded_entries():
    for lang, catalog in CATALOGS.items():
        for key, value in catalog.items():
            assert value.strip(), f"{lang} 的 {key} 是空文案"
            assert value == value.strip(), f"{lang} 的 {key} 首尾有多余空白"


def _t_call_keys() -> list[tuple[Path, int, str]]:
    """``render/`` 下所有 ``t("字面量", ...)`` 的键与位置。"""
    out: list[tuple[Path, int, str]] = []
    for path in sorted(RENDER_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Name) and func.id == "t") or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                out.append((path, node.lineno, first.value))
    return out


def test_every_t_call_uses_a_known_key():
    known = set(CATALOGS[DEFAULT_LANG])
    bad = [(p.name, line, key) for p, line, key in _t_call_keys() if key not in known]
    assert not bad, f"t() 用了表里没有的键：{bad[:6]}"


def test_render_layer_actually_calls_t():
    """防止有人把 t() 全删掉后门禁空转通过。"""
    assert len(_t_call_keys()) >= 30


def _docstring_lines(tree: ast.AST) -> set[int]:
    """模块/类/函数的第一条语句若是裸字符串，那不是文案。"""
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", [])
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            lines.add(first.lineno)
    return lines


def _ignored_lines(source: str) -> set[int]:
    return {i for i, line in enumerate(source.splitlines(), start=1) if "# i18n-ignore" in line}


def _catalog_texts() -> list[str]:
    return [value for catalog in CATALOGS.values() for value in catalog.values()]


def _non_product_lines(tree: ast.AST) -> set[int]:
    """「不面向产物读者」的消息所在行：诊断消息、告警、异常。

    ``Diagnostic(message=...)`` 进产物（diagnostics.md），但它是**结构化诊断**：
    ``code`` 才是稳定接口，消息是给维护者的细节。三十来条消息双语化只会让
    ``rg AI_DIAGRAM_INVALID`` 之类失效，所以按设计不翻。

    规则化而不是逐个 ``# i18n-ignore``：二十多个标记会把「哪些是设计决定、哪些是
    疏漏」淹没掉，而这两件事的处理方式完全不同。
    """
    lines: set[int] = set()

    def mark(node: ast.AST) -> None:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                for ln in range(sub.lineno, (sub.end_lineno or sub.lineno) + 1):
                    lines.add(ln)

    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and node.exc is not None:
            mark(node.exc)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in {"Diagnostic", "Issue", "L2Result"}:
                for arg in node.args:
                    mark(arg)
                for kw in node.keywords:
                    if kw.arg == "message" and kw.value is not None:
                        mark(kw.value)
            elif (
                isinstance(func, ast.Attribute)
                and func.attr == "warn"
                and isinstance(func.value, ast.Name)
                and func.value.id == "warnings"
            ):
                for arg in node.args:
                    mark(arg)
    return lines


def test_no_unextracted_chinese_in_render_layer():
    """含中文的字符串字面量必须已进语言包 —— 否则英文产物里就露出中文了。

    判定方式是「是某条文案的子串」：``f"层 {name}"`` 的常量片段是 ``"层 "``，
    它确实是 ``"层 {name}"`` 的一部分。这比逐字相等更贴合 f-string 的实际形态。
    """
    texts = _catalog_texts()
    offenders: list[str] = []

    for path in sorted(RENDER_DIR.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        exempt = _docstring_lines(tree) | _ignored_lines(source) | _non_product_lines(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if node.lineno in exempt or not _han(node.value):
                continue
            if any(node.value in text for text in texts):
                continue
            offenders.append(f"{path.relative_to(REPO)}:{node.lineno} {node.value[:50]!r}")

    assert not offenders, "这些中文字面量没进语言包（确属配置键/注释请加 `# i18n-ignore`）：\n" + "\n".join(
        offenders[:15]
    )


def test_diagnostic_exemption_is_not_vacuous():
    """豁免规则必须真的命中东西，否则它只是把门禁悄悄关掉了。"""
    validator = RENDER_DIR / "mermaid_validator.py"
    tree = ast.parse(validator.read_text(encoding="utf-8"), filename=str(validator))
    exempt = _non_product_lines(tree)
    han_exempt = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _han(node.value)
        and node.lineno in exempt
    ]
    assert len(han_exempt) >= 5, "诊断消息豁免没命中任何中文，规则可能写坏了"


def test_i18n_ignore_marks_are_still_needed():
    """豁免标记不能变成永久绕过路径：标了的中文必须还在。"""
    marked = 0
    for path in sorted(RENDER_DIR.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for line_no in _ignored_lines(source):
            line = source.splitlines()[line_no - 1]
            if _han(line):
                marked += 1
    assert marked >= 2, "豁免标记全部失效了，该删的标记要删掉"


def test_t_formats_named_placeholders():
    set_lang("zh")
    assert t("wiki.layer_line", name="核心", count=3) == "- **核心**（3 个模块）"
    set_lang("en")
    assert t("wiki.layer_line", name="core", count=3) == "- **core** (3 modules)"


def test_t_falls_back_to_default_language_on_missing_key(monkeypatch):
    """某语言漏了一条不该让产物出现 ``site.overview`` 这种字面量。"""
    broken = dict(CATALOGS["en"])
    broken.pop("site.overview")
    monkeypatch.setitem(CATALOGS, "en", broken)
    set_lang("en")
    assert t("site.overview") == CATALOGS[DEFAULT_LANG]["site.overview"]


def test_normalize_accepts_region_variants_and_unknown_falls_back():
    assert normalize("en-US") == "en"
    assert normalize("EN_us") == "en"
    assert normalize("zh-CN") == "zh"
    assert normalize(None) == DEFAULT_LANG
    assert normalize("tlh") == DEFAULT_LANG


def test_adding_a_language_needs_no_call_site_change(monkeypatch):
    """这条是多语言的**定义**：注册第 N 门语言，一行调用点都不用动。"""
    monkeypatch.setitem(CATALOGS, "xx", {key: f"xx:{key}" for key in CATALOGS[DEFAULT_LANG]})
    set_lang("xx")

    assert current_lang() == "xx"
    assert t("site.overview") == "xx:site.overview"
    assert html_lang() == "xx"


def test_html_lang_maps_default_language_to_its_locale():
    set_lang("zh")
    assert html_lang() == "zh-CN"
    set_lang("en")
    assert html_lang() == "en"


@pytest.mark.parametrize("lang", sorted(CATALOGS))
def test_no_catalog_entry_is_a_key_echo(lang):
    """文案值不可能是键本身 —— 那说明有人把 t() 的旧写法照搬进表里了。"""
    for key, value in CATALOGS[lang].items():
        assert value != key, f"{lang} 的 {key} 值是键本身"


def test_non_default_catalogs_contain_no_chinese():
    """非默认语言里残留中文 = 漏翻，而产物不会给任何提示（只是一段中文夹在英文里）。

    这条能抓到「新加语言时把中文表整份复制过去，只翻了一半」。
    """
    for lang, catalog in CATALOGS.items():
        if lang == DEFAULT_LANG:
            continue
        leftovers = sorted(key for key, value in catalog.items() if _han(value))
        assert not leftovers, f"{lang} 里还有中文（漏翻）：{leftovers[:8]}"
