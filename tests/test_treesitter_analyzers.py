"""TS/Go 后端判据（§4.6）。

tree-sitter 是**可选依赖**，所以整组用例在未安装时跳过 —— 但不能因此不写：
可选依赖一旦装上就要有行为保证，否则"装了也没验证过"。

每条断言都对着**事实的形状**而不是"解析没崩"：TS 的 re-export 别名、Go 的
方法 qualname（``Service.Run``）这种跨语言对齐点最容易被写错。
"""

from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter", reason="需要 [treesitter] extra")
pytest.importorskip("tree_sitter_typescript", reason="需要 tree-sitter-typescript")
pytest.importorskip("tree_sitter_go", reason="需要 tree-sitter-go")

from pathlib import Path  # noqa: E402

from codewiki.analyzers.go_analyzer import GoAnalyzer  # noqa: E402
from codewiki.analyzers.ts_analyzer import TypeScriptAnalyzer  # noqa: E402

TS_SOURCE = """
import Default from "./base";
import { Named, Aliased as A } from "./mod";
import * as NS from "./star";

/** 服务基类 */
export abstract class Base {
  abstract run(): void;
}

export class Service extends Base implements Runnable {
  private repo: Repo;

  constructor(repo: Repo) {
    this.repo = repo;
  }

  run(): void {
    this.repo.load();
    helper();
  }
}

export function helper(): number {
  return 1;
}
"""

TS_REEXPORT = """
export { Service as Svc } from "./service";
export * from "./all";
"""

GO_SOURCE = """package demo

import (
\t"errors"
\talias "strings"
)

// Repo 是仓储。
type Repo struct {
\tname string
}

type Runner interface {
\tRun() error
}

func NewRepo(name string) *Repo {
\treturn &Repo{name: name}
}

func (r *Repo) Load(id int) error {
\tif id == 0 {
\t\treturn errors.New("bad id")
\t}
\treturn nil
}
"""


def test_ts_imports_cover_default_named_and_namespace():
    model = TypeScriptAnalyzer().parse(Path("svc.ts"), TS_SOURCE)

    modules = {i.module for i in model.imports}
    assert modules == {"./base", "./mod", "./star"}, modules
    named = {(i.name, i.alias) for i in model.imports if i.module == "./mod"}
    assert ("Named", None) in named and ("Aliased", "A") in named
    assert any(i.name == "*" for i in model.imports), "namespace import 要能识别"


def test_ts_symbols_and_inheritance():
    model = TypeScriptAnalyzer().parse(Path("svc.ts"), TS_SOURCE)

    classes = {s.name for s in model.symbols if s.kind == "class"}
    assert {"Base", "Service"} <= classes
    methods = {s.qualname for s in model.symbols if s.kind == "method"}
    assert "Service.run" in methods, methods
    assert {i.child for i in model.inherits} == {"Service"}
    assert {"Base", "Runnable"} <= {i.base for i in model.inherits}


def test_ts_calls_are_named():
    model = TypeScriptAnalyzer().parse(Path("svc.ts"), TS_SOURCE)
    callees = {c.callee for c in model.calls}
    assert "load" in callees and "helper" in callees


def test_ts_docstring_and_language_tag():
    model = TypeScriptAnalyzer().parse(Path("svc.ts"), TS_SOURCE)
    base = next(s for s in model.symbols if s.name == "Base")
    assert base.docstring and "服务基类" in base.docstring
    assert model.language == "typescript"
    assert TypeScriptAnalyzer().parse(Path("a.tsx"), TS_SOURCE).language == "tsx"


def test_ts_reexport_alias_and_star():
    model = TypeScriptAnalyzer().parse(Path("index.ts"), TS_REEXPORT)
    by_module = {i.module: i for i in model.imports}

    assert by_module["./service"].name == "Service" and by_module["./service"].alias == "Svc"
    assert by_module["./all"].is_star is True


def test_go_imports_and_exports():
    model = GoAnalyzer().parse(Path("repo.go"), GO_SOURCE)

    modules = {i.module for i in model.imports}
    assert modules == {"errors", "strings"}, modules
    assert any(i.alias == "alias" for i in model.imports)
    assert "NewRepo" in model.exports and "Load" in model.exports
    assert "Repo" in model.exports


def test_go_struct_is_class_and_method_has_qualname():
    model = GoAnalyzer().parse(Path("repo.go"), GO_SOURCE)

    classes = {s.name for s in model.symbols if s.kind == "class"}
    assert {"Repo", "Runner"} <= classes, classes
    methods = {s.qualname for s in model.symbols if s.kind == "method"}
    assert "Repo.Load" in methods, f"方法 qualname 必须带接收者: {methods}"


def test_go_signature_includes_result_type():
    model = GoAnalyzer().parse(Path("repo.go"), GO_SOURCE)
    load = next(s for s in model.symbols if s.qualname == "Repo.Load")
    assert load.signature and "id" in load.signature


def test_go_docstring_comes_from_comment():
    model = GoAnalyzer().parse(Path("repo.go"), GO_SOURCE)
    repo = next(s for s in model.symbols if s.name == "Repo")
    assert repo.docstring and "仓储" in repo.docstring


def test_provenance_records_backend():
    model = GoAnalyzer().parse(Path("repo.go"), GO_SOURCE)
    assert model.provenance is not None and model.provenance.backend == "treesitter"


def test_polyglot_repo_parses_all_languages(mini_py=None):
    """混合仓库端到端：注册生效 ⇒ 三种语言都能解析出模块与符号。"""
    from pathlib import Path

    from codewiki.analyzers import REGISTRY, register_defaults
    from codewiki.graph import build_graph
    from codewiki.render.jsonio import packet_from_parts
    from codewiki.repo import iter_source_files

    register_defaults()
    root = Path("tests/fixtures/polyglot")
    models = []
    for source in iter_source_files(root):
        models.append(REGISTRY.get(source.language, "auto").parse(Path(source.path), source.content))

    assert len(models) == 3
    assert {m.language for m in models} == {"python", "typescript", "go"}, [m.language for m in models]
    assert all(m.symbols for m in models), "每个文件都应解析出符号"

    result = build_graph(models)
    packet = packet_from_parts(
        title="polyglot", layers=[], nodes=result.nodes, edges=result.edges, annotations=[], diagnostics=[]
    )
    languages = {n.language for n in packet.nodes if n.type.value == "Module"}
    assert {"python", "typescript", "go"} <= languages, languages
