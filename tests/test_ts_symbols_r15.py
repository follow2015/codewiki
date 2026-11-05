"""round 15：TS 后端补符号 / JSX 使用边 / ``import type`` 标 type_only；缓存键的格式版本要对 TS/Go 生效。"""

from __future__ import annotations

from pathlib import Path

import pytest

from codewiki.analyzers import FORMAT_VERSIONS, build_registry, format_version_of
from codewiki.graph import build_graph
from codewiki.models import EdgeType
from codewiki.pipeline.analyze import _backend_of

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_typescript")


def _parse(path: str, src: str):
    return build_registry().get("typescript", "auto").parse(Path(path), src)


def _kinds(model) -> dict[str, str]:
    return {s.name: s.kind for s in model.symbols}


def test_arrow_functions_and_wrapped_components_are_functions():
    model = _parse(
        "x.tsx",
        "export const g = (x: number) => x;\n"
        "const Button = React.forwardRef<HTMLButtonElement, P>((props, ref) => null);\n"
        "export const Memo = memo(function Inner() { return null; });\n"
        "export const Single = x => x;\n",
    )
    kinds = _kinds(model)
    assert kinds["g"] == kinds["Button"] == kinds["Memo"] == kinds["Single"] == "function"
    sig = {s.name: s.signature for s in model.symbols}
    assert sig["g"] == "(x: number)" and sig["Single"] == "x"


def test_types_interfaces_enums_and_exported_constants():
    model = _parse(
        "x.ts",
        "export interface P { x: number }\nexport type T = string;\nexport enum E { A }\n"
        "export const Icons = { a: 1 };\nconst MAX = 3;\nconst lower = 2;\n",
    )
    assert _kinds(model) == {
        "P": "interface",
        "T": "type",
        "E": "enum",
        "Icons": "constant",
        "MAX": "constant",
    }


def test_local_declarations_are_not_symbols():
    model = _parse(
        "x.tsx",
        "export function outer() {\n  const inner = () => 1;\n  interface Hidden {}\n  return inner();\n}\n"
        "class C { m() { const local = () => 1; return local; } }\n",
    )
    names = {s.name for s in model.symbols}
    assert "inner" not in names and "Hidden" not in names and "local" not in names
    assert {"outer", "C", "m"} <= names


def test_jsx_component_usage_is_a_call_but_html_tags_are_not():
    model = _parse(
        "x.tsx",
        "export const Page = () => <div><Card /><Foo.Bar a={1}></Foo.Bar><span/></div>;\n",
    )
    calls = {(c.callee, c.receiver) for c in model.calls}
    assert ("Card", None) in calls and ("Bar", "Foo") in calls
    assert not any(c.callee in {"div", "span"} for c in model.calls)


def test_type_only_imports_are_flagged_in_every_form():
    model = _parse(
        "x.ts",
        'import type { A } from "./a";\nimport { type B, C } from "./b";\nimport D from "./d";\n'
        'import type * as NS from "./ns";\nexport type { E } from "./e";\nexport { F } from "./f";\n',
    )
    flags = {(i.module, i.name): i.type_only for i in model.imports}
    assert flags[("./a", "A")] and flags[("./b", "B")] and flags[("./e", "E")] and flags[("./ns", "*")]
    assert not flags[("./b", "C")] and not flags[("./d", "D")] and not flags[("./f", "F")]


def test_type_import_cycle_is_not_a_module_cycle_but_runtime_cycle_is():
    a_type = _parse("a.ts", 'import type { B } from "./b";\nexport const A = 1;\n')
    b = _parse("b.ts", 'import { A } from "./a";\nexport interface B { x: typeof A }\n')
    assert not any(d.code == "MODULE_CYCLE" for d in build_graph([a_type, b]).diagnostics)

    a_runtime = _parse("a.ts", 'import { B } from "./b";\nexport const A = 1;\n')
    assert any(d.code == "MODULE_CYCLE" for d in build_graph([a_runtime, b]).diagnostics)
    edge = next(
        e
        for e in build_graph([a_type, b]).edges
        if e.type == EdgeType.IMPORTS and e.source == "mod::a.ts" and e.target == "mod::b.ts"
    )
    assert edge.type_only


def test_component_import_resolves_to_the_symbol_and_jsx_use_makes_a_call_edge():
    button = _parse("ui/button.tsx", "export const Button = () => null;\n")
    page = _parse(
        "page.tsx",
        'import { Button } from "./ui/button";\nexport const Page = () => <Button />;\n',
    )
    result = build_graph([button, page])
    assert not any(d.code == "IMPORT_NAME_UNRESOLVED" for d in result.diagnostics)
    calls = {(e.source, e.target) for e in result.edges if e.type == EdgeType.CALLS}
    assert ("sym::page.tsx::Page", "sym::ui/button.tsx::Button") in calls


def test_cache_key_format_version_applies_to_typescript_and_go():
    """TS/Go 的 BACKEND 声明在基类模块里；只看实例所属模块会退回类名、版本恒为 0，升版本号永远不生效。"""
    registry = build_registry()
    for language in ("python", "typescript", "go"):
        try:
            analyzer = registry.get(language, "auto")
        except KeyError:
            continue
        backend = _backend_of(analyzer)
        assert backend in FORMAT_VERSIONS, f"{language}: 缓存键用的后端名 {backend!r} 不在 FORMAT_VERSIONS 里"
        assert format_version_of(backend) > 0
