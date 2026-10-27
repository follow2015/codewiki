"""端到端：无网 analyze → 内网 ai-annotate → 拷回 merge → 重新 analyze（§5.2）。

这条链路是阶段二**头号验收项**，所以这里用真实的 HTTP stub（不起假 client），
把 client → 沙箱 → 标注 → 建议文件 → 合并 → 再渲染整条串起来跑。

stub 故意**按请求的模块动态作答**（而不是返回固定文案）：这样才能顺带验证
"AI 注解的 target 与证据行号真的能对上"，固定文案会把串台问题藏起来。
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from codewiki.cli import main
from codewiki.config import load_config

_TARGET_LINE = re.compile(r"^(mod::[^\s|]+)\s*\|", re.M)

MINI_CFG = """[project]
name = "mini"

[analysis]
min_confidence = 0.5

[[architecture.layers]]
name = "应用层"
paths = ["app.py"]
color = "#B3E5FC"

[[annotations]]
target = "mod::pkg/util.py"
label = "工具"
note = "人工注解：AI 不许覆盖我"

[ai]
enabled = true
# 两个陷阱：被分析仓库配置里的 model / base_url 都必须被忽略（外部评审 2026-10-23
# —— hostile 仓库可借此把 env 里的 API Key 送走，或把代码送去它选的模型）。
# 操作者侧的取值走 OPENAI_MODEL / OPENAI_BASE_URL（见 prepared fixture）。
model = "repo-chosen-model"
base_url = "http://127.0.0.1:1/v1"
"""


def _reply_for(user_content: str) -> str:
    targets = _TARGET_LINE.findall(user_content)
    target = targets[0] if targets else ""
    rel = target.removeprefix("mod::")
    return json.dumps(
        {
            "modules": [
                {
                    "target": target,
                    "label": "Stub 结论",
                    "note": f"这是 stub 针对 {rel} 写的说明，见 {rel}:1",
                    "evidence": [{"path": rel, "lines": [1]}],
                }
            ],
            "unknown": [],
        }
    )


class _StubHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - http.server 的固定命名
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        user_content = next(
            (m["content"] for m in reversed(payload.get("messages", [])) if m.get("role") == "user"),
            "",
        )
        body = json.dumps(
            {
                "choices": [{"message": {"content": _reply_for(user_content)}}],
                "model": "stub-model",
                "usage": {"prompt_tokens": 8, "completion_tokens": 16},
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return None


@pytest.fixture
def stub_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()
    server.server_close()


@pytest.fixture
def prepared(tmp_path, mini_py, stub_url, monkeypatch):
    """跑通 analyze + 准备好配置与目录。配置里 base_url / model 是陷阱值（必须被忽略）。

    模型名由**操作者**给（环境变量），与配置里的 ``repo-chosen-model`` 无关 ——
    断言这一点的是 test_trust.py，这里只要流程能跑。
    """
    monkeypatch.setenv("OPENAI_MODEL", "stub-model")
    cfg = tmp_path / "codewiki.toml"
    cfg.write_text(MINI_CFG, encoding="utf-8")
    out = tmp_path / "wiki-output"
    assert main(["analyze", "--src", str(mini_py), "--out", str(out), "--config", str(cfg)]) == 0
    return {
        "cfg": cfg,
        "out": out,
        "packet": out / "data" / "codewiki.json",
        "src": mini_py,
        "cache": tmp_path / "ai-cache-root",
        "stub_url": stub_url,
    }


def test_full_loop_analyze_annotate_merge_rerender(prepared, monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub-not-a-real-key")
    monkeypatch.setenv("OPENAI_BASE_URL", prepared["stub_url"])
    assert main(
        [
            "ai-annotate",
            "--input", str(prepared["packet"]),
            "--config", str(prepared["cfg"]),
            "--src", str(prepared["src"]),
            "--out", str(prepared["out"]),
            "--cache-dir", str(prepared["cache"]),
            "--top-n", "2",
        ]
    ) == 0

    suggest = prepared["out"] / "suggestions" / "annotations.suggest.toml"
    text = suggest.read_text(encoding="utf-8")
    assert suggest.exists() and 'reviewed = false' in text and 'source = "ai"' in text
    assert (prepared["out"] / "ai-audit.jsonl").exists()

    assert 'target = "mod::pkg/util.py"' not in text

    assert main(["merge-annotations", str(suggest), "--config", str(prepared["cfg"])]) == 0

    cfg_after = load_config(prepared["cfg"])
    ai_targets = [a.target for a in cfg_after.annotations if a.source == "ai"]
    human = [a for a in cfg_after.annotations if a.target == "mod::pkg/util.py"]
    assert ai_targets, "建议没被合进主配置"
    assert human and human[0].note.startswith("人工注解"), "人工注解被改写"

    out2 = prepared["out"].parent / "wiki-output2"
    assert main(["analyze", "--src", str(prepared["src"]), "--out", str(out2), "--config", str(prepared["cfg"])]) == 0
    packet = json.loads((out2 / "data" / "codewiki.json").read_text(encoding="utf-8"))
    sources = {a["source"] for a in packet["annotations"]}
    assert "ai" in sources and "human" in sources


def test_cache_avoids_second_paid_call(prepared, monkeypatch):
    """同一份源码不重复花钱（§5.5 缓存）。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub-not-a-real-key")
    monkeypatch.setenv("OPENAI_BASE_URL", prepared["stub_url"])
    args = [
        "ai-annotate",
        "--input", str(prepared["packet"]),
        "--config", str(prepared["cfg"]),
        "--src", str(prepared["src"]),
        "--out", str(prepared["out"]),
        "--cache-dir", str(prepared["cache"]),
        "--top-n", "2",
    ]
    assert main(args) == 0
    audit_lines = (prepared["out"] / "ai-audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    first_calls = sum(1 for line in audit_lines if '"llm.chat"' in line)

    assert main(args) == 0
    audit_lines = (prepared["out"] / "ai-audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    second_total = sum(1 for line in audit_lines if '"llm.chat"' in line)

    assert first_calls >= 1
    assert second_total == first_calls, "第二轮应全部命中缓存，不得再次调用模型"


def test_ai_down_degrades_without_touching_static_products(prepared, monkeypatch, capsys):
    """模型不可达 ⇒ 退出码 1 + 明确诊断，**静态产物保持完整**（§5.5 降级）。"""
    monkeypatch.setenv("OPENAI_BASE_URL", prepared["stub_url"])
    before = json.loads(prepared["packet"].read_text(encoding="utf-8"))

    code = main(
        [
            "ai-annotate",
            "--input", str(prepared["packet"]),
            "--config", str(prepared["cfg"]),
            "--src", str(prepared["src"]),
            "--out", str(prepared["out"]),
            "--cache-dir", str(prepared["cache"]),
            "--top-n", "1",
            "--base-url", "http://127.0.0.1:1/v1",
        ]
    )
    captured = capsys.readouterr()

    assert code == 1, "AI 未完成要能被 CI 感知（不是静默成功）"
    assert "AI_UNAVAILABLE" in captured.out
    after = json.loads(prepared["packet"].read_text(encoding="utf-8"))
    assert len(after["nodes"]) == len(before["nodes"]), "静态产物不得被 AI 失败改写"
    diagnostics = (prepared["out"] / "suggestions" / "diagnostics.md").read_text(encoding="utf-8")
    assert "AI_UNAVAILABLE" in diagnostics


def test_missing_packet_is_user_error(prepared, capsys):
    code = main(
        ["ai-annotate", "--input", str(prepared["out"] / "nope.json"), "--config", str(prepared["cfg"])]
    )
    assert code == 2 and "数据包不存在" in capsys.readouterr().err


def test_missing_model_is_reported(tmp_path, prepared, capsys, monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    cfg = tmp_path / "no-model.toml"
    cfg.write_text('[project]\nname = "x"\n', encoding="utf-8")
    code = main(["ai-annotate", "--input", str(prepared["packet"]), "--config", str(cfg)])
    assert code == 2 and "未指定模型" in capsys.readouterr().err


def test_write_back_auto_merges_without_touching_human(prepared, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub-not-a-real-key")
    monkeypatch.setenv("OPENAI_BASE_URL", prepared["stub_url"])
    assert main(
        [
            "ai-annotate",
            "--input", str(prepared["packet"]),
            "--config", str(prepared["cfg"]),
            "--src", str(prepared["src"]),
            "--out", str(prepared["out"]),
            "--cache-dir", str(prepared["cache"]),
            "--top-n", "1",
            "--write-back", "auto",
        ]
    ) == 0

    cfg_after = load_config(prepared["cfg"])
    human = [a for a in cfg_after.annotations if a.target == "mod::pkg/util.py"]
    assert human and human[0].source == "human"
    assert any(a.source == "ai" for a in cfg_after.annotations)


def test_annotate_bad_config_is_user_error(prepared, tmp_path, capsys):
    bad = tmp_path / "bad.toml"
    bad.write_text("[nope]\n", encoding="utf-8")
    code = main(["ai-annotate", "--input", str(prepared["packet"]),
                 "--config", str(bad), "--model", "m"])
    err = capsys.readouterr().err
    assert code == 2, err
    assert "error:" in err and "nope" in err, "要给可执行的错误信息，不是 traceback"


def test_agent_bad_config_is_user_error(prepared, tmp_path, capsys):
    bad = tmp_path / "bad.toml"
    bad.write_text("[nope]\n", encoding="utf-8")
    code = main(["agent", "--input", str(prepared["packet"]),
                 "--config", str(bad)])
    err = capsys.readouterr().err
    assert code == 2, err
    assert "error:" in err and "nope" in err
