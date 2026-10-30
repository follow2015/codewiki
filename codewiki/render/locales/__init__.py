"""产物文案语言包（``[project] lang``）。

## 为什么是键表而不是 ``t(中文, English)``

上一版是**就地双语**：``t("架构总览", "Architecture overview")``。它有一个当时
没被重视的缺陷 —— **加不了第三种语言**。要加日语，必须把每一处调用从两个参数
改成三个参数，而第三个参数在每个调用点都是空的。那不是多语言，是双方言。

键表把「有哪些文案」与「文案是什么语言」拆开：

* 调用点只说键：``t("site.overview")``；
* 每门语言一个 ``CATALOG``（``zh.py`` / ``en.py``）；
* 加语言 = 新建一个表 + 在 ``CATALOGS`` 登记，**一行调用点都不用动**
  （``tests/test_i18n_catalog.py::test_adding_a_language_needs_no_call_site_change``
  把这条钉住）。

代价是多了一层间接，以及「漏翻」的形态变了：不再是英文产物里露出中文，而是
新写的文案压根没进表。所以门禁也换了一套（见 ``tests/test_i18n_catalog.py``）：
键集对齐、占位符对齐、调用点核对、**漏抽扫描**（含中文的字符串字面量必须已进表，
确属配置键的用行内 ``# i18n-ignore`` 显式豁免）。

## 什么不进语言包

* **诊断消息**（``Diagnostic.message``）—— 给维护者看的结构化诊断，``code`` 才
  是稳定接口。双语化只会让 grep 失效，所以门禁对 ``Diagnostic(message=...)``
  整体豁免，而不是逐个打标记；
* **配置键**（``layer_map`` 的六个语义层名）—— 它们是按层名查配色表的键，翻译
  它等于让配色失效；
* 代码注释与 docstring —— 不进产物。

## 缺键时的行为

先回退默认语言，再回退键本身（并打 warning）。回退而不是抛异常：一次漏译不该
让整条渲染流水线挂掉，但产物里出现 ``site.overview`` 这种字面量必须刺眼可见 ——
所以最后那道回退会 warning。理论上到不了那一步，键集对齐测试会先红。
"""

from __future__ import annotations

from .en import CATALOG as _EN
from .zh import CATALOG as _ZH

CATALOGS: dict[str, dict[str, str]] = {
    "zh": _ZH,
    "en": _EN,
}

DEFAULT_LANG = "zh"

HTML_LANGS: dict[str, str] = {
    "zh": "zh-CN",
    "en": "en",
}


def catalog(lang: str) -> dict[str, str]:
    """取某语言的表；没有这门语言就给默认语言（调用方一般不必自己判断）。"""
    return CATALOGS.get(lang) or CATALOGS[DEFAULT_LANG]


def languages() -> tuple[str, ...]:
    return tuple(CATALOGS)


def keys() -> frozenset[str]:
    return frozenset(CATALOGS[DEFAULT_LANG])


__all__ = [
    "CATALOGS",
    "DEFAULT_LANG",
    "HTML_LANGS",
    "catalog",
    "keys",
    "languages",
]
