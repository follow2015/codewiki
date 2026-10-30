"""``codewiki init`` 的 CLI 契约：模板 / 起草 / 不覆盖 / 外置推理闭环。

这些用例锁的是**命令行为**，分层算法本身在 ``test_layering_draft.py`` 里测。分开
是因为两者的失败含义不同：算法错了是"配置质量差"，命令错了是"文件被覆盖 / 退出码
骗人"，后者破坏性更大。
"""

from __future__ import annotations

import json

from codewiki.ai.deferred import write_answer
from codewiki.cli import main
from codewiki.config import load_config


def _write_repo(root) -> None:  # noqa: ANN001
    files = {
        "app/api/handler.py": "from app.services import logic\n",
        "app/api/health.py": "from app.services import logic\n",
        "app/services/logic.py": "from app.models import row\n",
        "app/services/audit.py": "from app.models import row\n",
        "app/models/row.py": "X = 1\n",
        "app/models/user.py": "Y = 2\n",
        "web/src/App.tsx": "import {x} from '../api/x'\n",
        "web/src/api/x.ts": "export const x = 1\n",
        "run.py": "import app.api.handler\n",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_init_without_src_still_writes_the_template(tmp_path):
    """老用法必须继续可用 —— 不带 --src 时行为不变，否则脚本会静默变脸。"""
    assert main(["init", "--path", str(tmp_path)]) == 0
    text = (tmp_path / "codewiki.toml").read_text(encoding="utf-8")
    assert "接口层" in text and "[[architecture.layers]]" in text


def test_init_with_src_drafts_layers_from_real_directories(tmp_path):
    repo = tmp_path / "repo"
    _write_repo(repo)
    out = tmp_path / "cfg"

    assert main(["init", "--src", str(repo), "--path", str(out)]) == 0

    cfg = load_config(out / "codewiki.toml")
    names = [layer.name for layer in cfg.architecture.layers]
    assert names, "起草结果不能是空配置"
    assert any(name.startswith("app.") for name in names), names
    assert "接口层" not in names
    assert all(layer.order is not None for layer in cfg.architecture.layers), "层序必须自动算好"


def test_init_refuses_to_overwrite_unless_forced(tmp_path):
    repo = tmp_path / "repo"
    _write_repo(repo)
    target = tmp_path / "codewiki.toml"
    target.write_text("# 人工维护的配置\n", encoding="utf-8")

    assert main(["init", "--src", str(repo), "--path", str(tmp_path)]) == 2
    assert target.read_text(encoding="utf-8") == "# 人工维护的配置\n"

    assert main(["init", "--src", str(repo), "--path", str(tmp_path), "--force"]) == 0
    assert "# 人工维护的配置" not in target.read_text(encoding="utf-8")


def test_init_ai_without_model_or_deferred_fails_fast(tmp_path, monkeypatch):
    """起草要花钱的动作，缺模型时必须当场报错 —— 而不是悄悄退回静态骨架。"""
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    repo = tmp_path / "repo"
    _write_repo(repo)

    assert main(["init", "--src", str(repo), "--path", str(tmp_path), "--ai"]) == 2
    assert not (tmp_path / "codewiki.toml").exists()


def _only_request_key(root) -> str:  # noqa: ANN001
    requests = list((root / "requests").glob("*.json"))
    assert len(requests) == 1, [p.name for p in requests]
    return json.loads(requests[0].read_text(encoding="utf-8"))["key"]


def test_init_ai_deferred_closes_the_loop_without_credentials(tmp_path):
    """零凭据全自动：第一次导出请求（rc=3，**不写半成品配置**），作答后重跑即落配置。

    这条链路是"开启 AI 就能全自动"的实际兑现方式：没有 API key 时由宿主 agent 读
    请求文件作答，命令本身不联网。
    """
    repo = tmp_path / "repo"
    _write_repo(repo)
    deferred = tmp_path / "deferred"
    out = tmp_path / "cfg"

    first = main(["init", "--src", str(repo), "--path", str(out), "--ai", "--deferred", str(deferred)])
    assert first == 3, "请求已导出，应返回 3（待作答）而不是 0"
    assert not (out / "codewiki.toml").exists(), "半成品层边界比没有更难判断，不能写"

    key = _only_request_key(deferred)
    write_answer(
        deferred,
        purpose="draft",
        key=key,
        content=json.dumps(
            {
                "layers": [
                    {"name": "接口层", "paths": ["app/api", "web"]},
                    {"name": "服务层", "paths": ["app/services"]},
                    {"name": "数据层", "paths": ["app/models"]},
                    {"name": "入口", "paths": ["run.py"]},
                ]
            },
            ensure_ascii=False,
        ),
    )

    assert main(["init", "--src", str(repo), "--path", str(out), "--ai", "--deferred", str(deferred)]) == 0
    cfg = load_config(out / "codewiki.toml")
    assert [layer.name for layer in cfg.architecture.layers] == ["接口层", "服务层", "数据层", "入口"]
    order = {layer.name: layer.order for layer in cfg.architecture.layers}
    assert order["接口层"] < order["服务层"] < order["数据层"]


def test_init_ai_deferred_rejects_invented_paths(tmp_path):
    """模型编目录时，那一层要被丢掉而不是写进配置（否则 analyze 里是空层）。"""
    repo = tmp_path / "repo"
    _write_repo(repo)
    deferred = tmp_path / "deferred"
    out = tmp_path / "cfg"

    assert main(["init", "--src", str(repo), "--path", str(out), "--ai", "--deferred", str(deferred)]) == 3
    key = _only_request_key(deferred)
    write_answer(
        deferred,
        purpose="draft",
        key=key,
        content=json.dumps(
            {
                "layers": [
                    {"name": "接口层", "paths": ["app/api"]},
                    {"name": "服务层", "paths": ["app/services"]},
                    {"name": "数据层", "paths": ["app/models"]},
                    {"name": "不存在的层", "paths": ["nope/nothing"]},
                ]
            },
            ensure_ascii=False,
        ),
    )

    assert main(["init", "--src", str(repo), "--path", str(out), "--ai", "--deferred", str(deferred)]) == 0
    names = [layer.name for layer in load_config(out / "codewiki.toml").architecture.layers]
    assert "不存在的层" not in names


def test_init_writes_the_excludes_it_used(tmp_path):
    """起草时排掉的目录必须落进配置 —— 否则下一次 analyze 用默认口径扫回来，
    这些模块整片落进 _unlayered，配置与刚才看到的层一一对不上。"""
    repo = tmp_path / "repo"
    _write_repo(repo)
    (repo / "vendor").mkdir()
    (repo / "vendor" / "third.py").write_text("Z = 3\n", encoding="utf-8")

    assert main(["init", "--src", str(repo), "--path", str(tmp_path), "--exclude", "vendor"]) == 0
    cfg = load_config(tmp_path / "codewiki.toml")
    assert cfg.analysis.exclude == ["vendor"]


def test_init_writes_a_language_whitelist(tmp_path):
    """起草必须写 ``[languages]``：否则 analyze 把每个文件都登记成模块，
    ``.DS_Store``/``README.md``/``*.db`` 全落进 ``_unlayered``（实测 62 个），
    配置与刚才看到的层一一对不上。"""
    repo = tmp_path / "repo"
    _write_repo(repo)
    (repo / "README.md").write_text("# hi\n", encoding="utf-8")

    assert main(["init", "--src", str(repo), "--path", str(tmp_path)]) == 0
    cfg = load_config(tmp_path / "codewiki.toml")
    assert "python" in cfg.languages
    assert "other" not in cfg.languages
    assert set(cfg.languages) <= {"python", "typescript", "javascript", "go"}


def test_configured_ai_is_used_automatically(tmp_path, monkeypatch):
    """配好了 AI（``OPENAI_MODEL``）就不该再要求人多敲 ``--ai`` —— 这是"开了 AI 就全自动"。

    但只在**确实配了**时才用：没配时自动模式老实走静态骨架（见下一条），
    隐式花钱和隐式不花钱一样不可接受。
    """
    monkeypatch.setenv("OPENAI_MODEL", "stub-model")
    repo = tmp_path / "repo"
    _write_repo(repo)
    deferred = tmp_path / "deferred"

    rc = main(["init", "--src", str(repo), "--path", str(tmp_path / "cfg"), "--deferred", str(deferred)])
    assert rc == 3, "自动模式应已调用模型并导出请求"
    assert list((deferred / "requests").glob("*.json")), "没有请求文件说明根本没走 AI"


def test_no_ai_forces_the_static_skeleton(tmp_path, monkeypatch):
    """``--no-ai`` 必须压过环境里的 AI 配置：有的仓库你就是不想让它花钱。"""
    monkeypatch.setenv("OPENAI_MODEL", "stub-model")
    repo = tmp_path / "repo"
    _write_repo(repo)
    out = tmp_path / "cfg"

    assert main(["init", "--src", str(repo), "--path", str(out), "--no-ai"]) == 0
    assert load_config(out / "codewiki.toml").architecture.layers
    assert "未调用模型" in (out / "codewiki.toml").read_text(encoding="utf-8")


def _write_structure_noise(root) -> None:  # noqa: ANN001
    """给仓库加上四类"不是产品代码"的目录，各自代表一条自动排除判据。"""
    (root / "tests").mkdir()
    (root / "tests" / "test_api.py").write_text("from app.api import handler\n", encoding="utf-8")
    (root / "archive").mkdir()
    (root / "archive" / "old.py").write_text("Z = 1\n", encoding="utf-8")
    (root / "deploy-copy").mkdir()
    (root / "deploy-copy" / ".git").mkdir()
    (root / "deploy-copy" / "copy.py").write_text("Y = 2\n", encoding="utf-8")
    (root / "lib-9.3.0").mkdir()
    (root / "lib-9.3.0" / "third.py").write_text("W = 3\n", encoding="utf-8")


def test_init_auto_excludes_structure_noise(tmp_path):
    """结构性的"不是产品代码"必须自动排除 —— 手写 31 条 exclude 的工具没人会用。

    实测 `configs/ipip.toml` 的 31 条 exclude 里，除了两三个项目特例，其余全是：独立
    仓库克隆、测试、迁移、文档、部署、存档、带版本号的第三方包。这些都是**结构事实**，
    不需要领域知识就能判。

    断言穿透到 paths：配置里写了 exclude 但扫描没生效，是最容易发生也最难发现的假成功。
    """
    from codewiki.cli.init_cmd import _scan

    repo = tmp_path / "repo"
    _write_repo(repo)
    _write_structure_noise(repo)

    out = tmp_path / "cfg" / "repo.toml"
    assert main(["init", "--src", str(repo), "--out", str(out)]) == 0

    cfg = load_config(out)
    assert set(cfg.analysis.exclude) >= {"tests", "archive", "deploy-copy", "lib-9.3.0"}

    paths, *_ = _scan(repo, backend="auto", excludes=tuple(cfg.analysis.exclude))
    leaked = [p for p in paths if p.split("/")[0] in {"tests", "archive", "deploy-copy", "lib-9.3.0"}]
    assert not leaked, f"自动排除没穿透到扫描：{leaked}"

    assert any(p.startswith("app/") for p in paths)
    assert not (out.parent / "codewiki.toml").exists()


def test_out_writes_an_external_config_path(tmp_path):
    """外挂式配置（`configs/ipip.toml`）必须能由 init 直接产出。

    以前只有 ``--path``，文件名被钉死成 ``codewiki.toml`` —— 想放 `configs/<项目>.toml`
    就只能手写或改完名再搬，而"手写"正是这套工具想消灭的动作。
    """
    repo = tmp_path / "repo"
    _write_repo(repo)

    target = tmp_path / "configs" / "ipip.toml"
    assert main(["init", "--src", str(repo), "--out", str(target)]) == 0
    assert target.exists()
    assert load_config(target).architecture.layers


def test_no_auto_exclude_keeps_every_directory(tmp_path):
    """自动排除是**默认**不是**强制**：判断错了要能一键关掉。"""
    repo = tmp_path / "repo"
    _write_repo(repo)
    _write_structure_noise(repo)

    assert main(["init", "--src", str(repo), "--path", str(tmp_path), "--no-auto-exclude"]) == 0
    cfg = load_config(tmp_path / "codewiki.toml")
    assert "tests" not in (cfg.analysis.exclude or [])
    counted = [path for layer in cfg.architecture.layers for path in layer.paths]
    assert any("tests" in path for path in counted), counted


def test_languages_whitelist_skips_other_languages(tmp_path):
    """不分析前端时用 ``--languages python`` 表达，而不是让人数一遍前端目录名。"""
    from codewiki.cli.init_cmd import _scan

    repo = tmp_path / "repo"
    _write_repo(repo)

    out = tmp_path / "cfg"
    assert main(["init", "--src", str(repo), "--path", str(out), "--languages", "python"]) == 0
    cfg = load_config(out / "codewiki.toml")
    assert set(cfg.languages) == {"python"}

    paths, *_ = _scan(repo, backend="auto", excludes=(), only_languages=("python",))
    assert paths and all(p.endswith(".py") for p in paths)


def test_aliases_into_excluded_dirs_are_dropped(tmp_path):
    """排除了前端目录还留着指向它的别名，是一条永远用不到的死配置。

    裸跑 ipip 时真实发生过：`ts_aliases = { "@/*" = "frontend-new/src/*" }`，而
    frontend-new 已经在 exclude 里 —— 读的人还得花时间想它为什么在。
    """
    repo = tmp_path / "repo"
    _write_repo(repo)
    (repo / "web" / "tsconfig.json").write_text(
        json.dumps({"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["./src/*"]}}}),
        encoding="utf-8",
    )

    without = tmp_path / "keep"
    assert main(["init", "--src", str(repo), "--path", str(without)]) == 0
    text = (without / "codewiki.toml").read_text(encoding="utf-8")
    assert "ts_aliases" in text, "没排除前端时别名必须保留（否则前端模块间零依赖）"

    dropped = tmp_path / "drop"
    assert main(["init", "--src", str(repo), "--path", str(dropped), "--exclude", "web"]) == 0
    assert "ts_aliases" not in (dropped / "codewiki.toml").read_text(encoding="utf-8")


def test_missing_backend_is_reported_not_silently_dropped(tmp_path, capsys):
    """没装 tree-sitter 时 TS 会被整片跳过 —— 必须明说，否则看起来像"仓库没有前端"。
    CI 刻意不装 tree-sitter（`.gitea/workflows/ci.yml`），实测 1322 个模块里
    700 个被静默丢掉，起草结果直接少一个"前端"层。
    """
    from codewiki.cli.init_cmd import _scan

    repo = tmp_path / "repo"
    _write_repo(repo)

    class _PyOnly:
        def get(self, language, backend):  # noqa: ANN001
            if language != "python":
                raise KeyError(language)
            from codewiki.analyzers.base import build_registry

            return build_registry().get("python", backend)

    paths, _deps, languages, missing = _scan(repo, backend="auto", excludes=(), registry=_PyOnly())
    assert languages == ["python"]
    assert missing.get("typescript", 0) >= 1
    assert all(not path.endswith((".ts", ".tsx")) for path in paths)
