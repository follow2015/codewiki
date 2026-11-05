"""产物语言（``[project] lang``）—— 运行时门面。

语言表本身在 :mod:`codewiki.render.locales`，这里只负责「当前是哪门语言」和
「按键取一条文案」。两件事分开的理由：表是数据（加语言只动数据），上下文是状态
（一次运行只设一次）。

为什么用 ``ContextVar`` 而不是给每个渲染函数加 ``lang`` 参数：渲染入口有七八个
（``render_index`` / ``render_module_page`` / ``render_all`` / ``render_layer_map``
…），逐个透传会让签名翻倍，而它们在一次运行里是单线程一次性的 —— 上下文变量在这里
既安全又不污染 API。写入只发生在两个地方：``pipeline.analyze`` 的入口与测试夹具。

**不翻译的东西**，以及理由：

* ``Diagnostic.message`` —— 结构化诊断是给维护者看的，``code`` 才是稳定接口，
  双语化只会让 grep 失效（门禁对这类整体豁免，见 ``locales/__init__`` 的说明）；
* ``layer_map._LAYER_SEMANTIC`` 的六个层名 —— 那是**配色表的键**（按层名查语义色），
  翻译它等于让配色失效；
* 代码里的中文注释与 docstring —— 不进产物。
"""

from __future__ import annotations

import warnings
from contextvars import ContextVar

from codewiki.render.locales import CATALOGS, DEFAULT_LANG, HTML_LANGS

_current: ContextVar[str] = ContextVar("codewiki_lang", default=DEFAULT_LANG)


def normalize(lang: str | None) -> str:
    """把配置里的写法归一到语言表里的键。

    接受 ``en`` / ``en-US`` / ``EN`` / ``en_US`` 这类写法：写 ``en_US`` 的人不该
    得到一份中文产物。认不出的值退回默认语言而不是报错 —— 这个字段不影响分析结果
    的正确性，没必要为它中断整条流水线。
    """
    value = (lang or "").strip().lower().replace("_", "-")
    if not value:
        return DEFAULT_LANG
    if value in CATALOGS:
        return value
    base = value.split("-", 1)[0]
    if base in CATALOGS:
        return base
    return DEFAULT_LANG


def set_lang(lang: str | None) -> None:
    """设定本次运行的产物语言。"""
    _current.set(normalize(lang))


def current_lang() -> str:
    return _current.get()


def t(key: str, **fmt: object) -> str:
    """按键取一条文案，并按需填充 ``{name}`` 占位符。

    缺键时回退默认语言，再缺就返回键本身并告警 —— 一次漏译不该让整条渲染流水线
    挂掉，但产物里冒出 ``site.overview`` 必须刺眼。参数名对不上（文案改了占位符
    名而调用点没跟上）同样告警并返回未填充的原文，而不是抛 ``KeyError``。
    """
    lang = _current.get()
    text = CATALOGS[lang].get(key)
    if text is None:
        text = CATALOGS[DEFAULT_LANG].get(key)
        if text is None:
            warnings.warn(f"未知的文案键 {key!r}（语言 {lang}）", RuntimeWarning, stacklevel=2)
            return key
    try:
        return text.format(**fmt)
    except (KeyError, IndexError) as exc:
        warnings.warn(
            f"文案 {key!r} 的占位符与调用点不符：{exc}（{text!r}）",
            RuntimeWarning,
            stacklevel=2,
        )
        return text


def t_raw(key: str) -> str:
    """取**未填充**的文案模板，留给前端按自己的数据替换 ``{name}``。

    为什么不让 ``t(key)`` 在缺参数时静默返回原文就完事：那条告警是为了抓"调用点
    漏传参数"这种真 bug（文案改了占位符名、调用点没跟上）。同一个入口既当"取
    模板"又当"报 bug"用，后者会被淹没在正常用法里。

    当前唯一的使用者是交互图：文案要在浏览器里按每个节点各自的数字渲染，服务端
    只能给出模板。替换在前端用 ``String.replace`` 做，键名与占位符仍由语言表定义，
    换第三种语言时不必改 JS（语序不同也能表达）。
    """
    lang = _current.get()
    text = CATALOGS[lang].get(key)
    if text is None:
        text = CATALOGS[DEFAULT_LANG].get(key)
        if text is None:
            warnings.warn(f"未知的文案键 {key!r}（语言 {lang}）", RuntimeWarning, stacklevel=2)
            return key
    return text


def html_lang() -> str:
    """``<html lang="...">`` 的值。"""
    lang = _current.get()
    return HTML_LANGS.get(lang, lang)


__all__ = [
    "DEFAULT_LANG",
    "current_lang",
    "html_lang",
    "normalize",
    "set_lang",
    "t",
]
