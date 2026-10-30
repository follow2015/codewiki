"""产物语言（``[project] lang``）。

此前这个字段**只进缓存指纹**：渲染层的界面文案全部硬编码中文，``<html lang="zh-CN">``
也是写死的。于是 ``lang = "en"`` 的效果是"什么都没有" —— 而文档说它决定产物语言。
一个字段承诺了一种能力却完全不生效，比不支持更费解：人会怀疑是自己拼错了。

为什么用 ``ContextVar`` 而不是给每个渲染函数加 ``lang`` 参数：渲染入口有七八个
（``render_index`` / ``render_module_page`` / ``render_all`` / ``render_layer_map``
…），逐个透传会让签名翻倍，而它们在一次运行里是单线程一次性的 —— 上下文变量在这里
既安全又不污染 API。写入只发生在两个地方：``pipeline.analyze`` 的入口与测试夹具。

为什么 ``t(zh, en)`` 用"就地双语"而不是键表：键表要额外维护一套 key，漏一个就在
产物里露出 ``{section_title}`` 这种东西；就地写两句话，漏翻当场看得见（中文串留在
英文产物里）。

**不翻译的东西**，以及理由：

* 诊断码与开发期错误消息（``AI_DIAGRAM_INVALID`` 之类）—— 它们是给维护者看的，
  双语化只会让 grep 失效；
* ``layer_map._LAYER_SEMANTIC`` 的六个层名 —— 那是**配色表的键**（按层名查语义色），
  翻译它等于让配色失效；
* 代码里的中文注释与 docstring —— 不进产物。
"""

from __future__ import annotations

from contextvars import ContextVar

DEFAULT_LANG = "zh"

_current: ContextVar[str] = ContextVar("codewiki_lang", default=DEFAULT_LANG)


def normalize(lang: str | None) -> str:
    """把配置里的写法归一到 ``zh`` / ``en``。

    接受 ``en`` / ``en-US`` / ``EN`` 这类写法：写 ``en_US`` 的人不该得到的是一份
    中文产物。认不出的值退回 ``zh``（默认语言），而不是报错 —— 这个字段不影响
    分析结果的正确性，没必要为它中断整条流水线。
    """
    value = (lang or "").strip().lower().replace("_", "-")
    if value == "en" or value.startswith("en-"):
        return "en"
    return "zh"


def set_lang(lang: str | None) -> None:
    """设定本次运行的产物语言。"""
    _current.set(normalize(lang))


def current_lang() -> str:
    return _current.get()


def t(zh: str, en: str) -> str:
    """按当前语言取一条文案（就地双语，不维护键表）。"""
    return en if _current.get() == "en" else zh


def html_lang() -> str:
    """``<html lang="...">`` 的值。"""
    return "en" if _current.get() == "en" else "zh-CN"


__all__ = [
    "DEFAULT_LANG",
    "current_lang",
    "html_lang",
    "normalize",
    "set_lang",
    "t",
]
