"""缓存与预算判据（§5.5）。

缓存键的三个成分各自对应一类"改了却没生效"的事故，逐条钉死：

* 换文件 ⇒ 键变（否则改了代码还拿旧结论）；
* 换模型 ⇒ 键变（否则两个模型的答案混着用，成本与质量都算不清）；
* 换提示词版本 ⇒ 键变（否则改了 Prompt 却命中旧缓存，表现为"改了没效果"）。
"""

from __future__ import annotations

from codewiki.ai.cache import (
    PROMPT_VERSION,
    ResultCache,
    TokenBudget,
    cache_key,
    digest_of,
    estimate_tokens,
)


def test_key_changes_with_content():
    base = dict(model="m", prompt_version=PROMPT_VERSION)
    assert cache_key(content_hash=digest_of("a"), **base) != cache_key(content_hash=digest_of("b"), **base)


def test_key_changes_with_model():
    assert cache_key(content_hash="h", model="m1") != cache_key(content_hash="h", model="m2")


def test_key_changes_with_prompt_version():
    assert cache_key(content_hash="h", model="m", prompt_version="v1") != cache_key(
        content_hash="h", model="m", prompt_version="v2"
    )


def test_topology_hash_is_opt_in():
    plain = cache_key(content_hash="h", model="m")
    with_topo = cache_key(content_hash="h", model="m", topology_hash="t")
    assert plain != with_topo, "开关注入后键必须变，否则开关形同虚设"
    assert "topo" not in plain


def test_cache_roundtrip(tmp_path):
    cache = ResultCache(tmp_path)
    assert cache.get("k") is None
    cache.set("k", {"raw": "x"})
    assert cache.get("k") == {"raw": "x"}


def test_corrupt_cache_is_treated_as_miss(tmp_path):
    cache = ResultCache(tmp_path)
    cache.dir.mkdir(parents=True, exist_ok=True)
    (cache.dir / "k.json").write_text("{broken", encoding="utf-8")
    assert cache.get("k") is None


def test_budget_exhaustion():
    budget = TokenBudget(100)
    assert not budget.exceeded and budget.remaining() == 100
    budget.spend(60)
    assert budget.remaining() == 40 and not budget.exceeded
    budget.spend(40)
    assert budget.exceeded and budget.remaining() == 0


def test_unlimited_budget_never_exceeds():
    budget = TokenBudget(None)
    budget.spend(10**9)
    assert not budget.exceeded and budget.remaining() is None


def test_token_estimate_overestimates_cjk():
    """宁可高估：低估会让预算形同虚设。"""
    assert estimate_tokens("中文" * 10) >= 20
    assert estimate_tokens("a" * 400) >= 100


def test_default_cache_dir_is_user_level(monkeypatch, tmp_path):
    """默认缓存不得落在被分析仓库里。

    落在 ``--src`` 下时存在投毒通道：缓存键 = 内容 hash + 提示词版本 + 模型名，
    同版本工具可自行复算；被审仓库预置 ``ai-cache/<key>.json`` 后，命中时
    ``raw`` 只校验"是 JSON"就被当作模型答案采信。缓存只是加速，没有理由写进
    别人的仓库。
    """
    from codewiki.ai.cache import default_cache_dir

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert default_cache_dir() == tmp_path / "xdg" / "codewiki" / "ai-cache"

    monkeypatch.delenv("XDG_CACHE_HOME")
    fallback = default_cache_dir()
    assert fallback.is_absolute()
    assert fallback.name == "ai-cache" and fallback.parent.name == "codewiki"


def test_cli_default_cache_dir_ignores_src(monkeypatch, tmp_path):
    """CLI 未给 --cache-dir 时走用户级目录，而不是 --src。"""
    from codewiki.cli.annotate_cmd import resolve_cache_dir

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    resolved = resolve_cache_dir(None)
    assert str(tmp_path / "xdg") in str(resolved), resolved
    assert resolved.name == "ai-cache"


def test_cli_explicit_cache_dir_keeps_root_semantics(tmp_path):
    """--cache-dir 是"根"：缓存落在其下的 .codewiki/ai-cache（保持旧语义）。"""
    from codewiki.ai.cache import CACHE_DIRNAME
    from codewiki.cli.annotate_cmd import resolve_cache_dir

    assert resolve_cache_dir(str(tmp_path)) == tmp_path.resolve() / CACHE_DIRNAME
