"""Adapter-thinness scan (plan S56/S77, BE-30) and its unit tests.

After the service-layer slice the three Search adapters only convert transport input into a command and a result or
error into a wire form. This scan keeps them there: none may reach for the validators, the request-scoped HyDE, the
search budget or the telemetry entry factories, which ``SearchService`` owns (see :data:`FORBIDDEN_MODULES`).

The scope differs per adapter. ``routes_search.py`` and the OpenAI shim's ``routes_openai_shim.py`` are scanned
whole-module: no import of a forbidden module anywhere in the file. ``mcp.py`` keeps those imports for
``search_with_context`` and ``explain``, so only the ``search`` tool body is scanned: the names it uses that resolve to a
forbidden module (and any forbidden import written inside it). The boundary allowlist of
``tests/test_layer_import_boundary.py`` is not touched; this is a separate test.

Accepted blind spots (a syntactic scan): ``importlib.import_module``, ``__import__``, ``getattr`` on a module, ``from pkg import *``,
a module alias (``_v = validators``), a local name that shadows an import, and a forbidden module reached through an
attribute of an allowed one (``services.container`` re-exporting it). A violation whose module reads ``helper X`` means the
``search`` tool calls the function ``X``, which uses a forbidden module.
"""
from __future__ import annotations

import ast
import tempfile
import textwrap
from functools import cache
from pathlib import Path
from typing import NamedTuple

import pytest

from tests.test_layer_import_boundary import _REPO_ROOT, _absolute_module

_PKG = "archon_search"
FORBIDDEN_MODULES = (
    f"{_PKG}.services.validators",
    f"{_PKG}.services.request_hyde",
    f"{_PKG}.services.search_budget",
    f"{_PKG}.telemetry.entry",
)
_TELEMETRY = f"{_PKG}.telemetry"
_TELEMETRY_ENTRY = f"{_TELEMETRY}.entry"
_SERVER = f"{_PKG}/server"
_ROUTES_SEARCH = f"{_SERVER}/routes_search.py"
_SHIM = f"{_SERVER}/routes_openai_shim.py"
_MCP = f"{_SERVER}/mcp.py"
_MCP_SEARCH_TOOL = "search"
_WHOLE_MODULE_ADAPTERS = (_ROUTES_SEARCH, _SHIM)


class ThinnessViolation(NamedTuple):
    """A forbidden module an adapter uses in its scope: the file, the module and the scope that was scanned."""

    file: str
    module: str
    scope: str


@cache
def _telemetry_reexports() -> frozenset[str]:
    """Names ``archon_search.telemetry`` re-exports from ``telemetry.entry``: importing one is importing the entry module."""
    init = _REPO_ROOT / _PKG / "telemetry" / "__init__.py"
    tree = ast.parse(init.read_text(encoding="utf-8"), filename=str(init))
    return frozenset(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == _TELEMETRY_ENTRY
        for alias in node.names
    )


def _canonical(module: str) -> str:
    """`archon_search.telemetry.TelemetryEntry` (a re-export) is `archon_search.telemetry.entry.TelemetryEntry`."""
    package, _, name = module.rpartition(".")
    if package == _TELEMETRY and name in _telemetry_reexports():
        return f"{_TELEMETRY_ENTRY}.{name}"
    return module


def _forbidden_prefix(module: str) -> str | None:
    """The forbidden module that `module` is, or lives under; None when it is neither."""
    module = _canonical(module)
    return next((f for f in FORBIDDEN_MODULES if module == f or module.startswith(f"{f}.")), None)


def _import_targets(node: ast.Import | ast.ImportFrom, file: str) -> dict[str, str]:
    """The local name each alias of an import binds -> the absolute dotted path it stands for."""
    if isinstance(node, ast.Import):
        return {
            (alias.asname or alias.name.split(".")[0]): (alias.name if alias.asname else alias.name.split(".")[0])
            for alias in node.names
        }
    base = _absolute_module(file, node)
    return {(alias.asname or alias.name): f"{base}.{alias.name}" for alias in node.names if alias.name != "*"}


def _imported_modules(node: ast.Import | ast.ImportFrom, file: str) -> set[str]:
    """Every absolute module path an import statement names, a `from pkg import mod` form included."""
    if isinstance(node, ast.Import):
        return {alias.name for alias in node.names}
    base = _absolute_module(file, node)
    return {base, *(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")}


def _forbidden_imports(tree: ast.AST, file: str, scope: str) -> set[ThinnessViolation]:
    return {
        ThinnessViolation(file, prefix, scope)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for module in _imported_modules(node, file)
        if (prefix := _forbidden_prefix(module))
    }


def _dotted(node: ast.expr) -> str | None:
    """The dotted text of a pure ``a.b.c`` name chain, None for any other expression."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return ".".join([node.id, *reversed(parts)])


def _forbidden_uses(node: ast.AST, bound: dict[str, str], file: str, scope: str) -> set[ThinnessViolation]:
    """Names and attribute chains under `node` that `bound` (local name -> dotted path) resolves to a forbidden module."""
    found: set[ThinnessViolation] = set()
    for child in ast.walk(node):
        chain = _dotted(child) if isinstance(child, (ast.Name, ast.Attribute)) else None
        if not chain:
            continue
        root, _, rest = chain.partition(".")
        if root in bound and (prefix := _forbidden_prefix(f"{bound[root]}.{rest}" if rest else bound[root])):
            found.add(ThinnessViolation(file, prefix, scope))
    return found


def _module_level_nodes(tree: ast.Module) -> list[ast.stmt]:
    """Statements run when the module loads: the top level and the bodies of its ``if`` and ``try`` blocks."""
    found: list[ast.stmt] = []
    pending = list(tree.body)
    while pending:
        node = pending.pop()
        found.append(node)
        if isinstance(node, ast.If):
            pending.extend([*node.body, *node.orelse])
        elif isinstance(node, ast.Try):
            pending.extend([*node.body, *node.orelse, *node.finalbody, *(n for h in node.handlers for n in h.body)])
    return found


def _bindings(tree: ast.AST, file: str, *, module_level_only: bool = False) -> dict[str, str]:
    """Local name -> dotted path of every import under `tree` (only the ones run at load time when `module_level_only`)."""
    nodes = _module_level_nodes(tree) if module_level_only and isinstance(tree, ast.Module) else ast.walk(tree)
    bound: dict[str, str] = {}
    for node in nodes:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            bound.update(_import_targets(node, file))
    return bound


def whole_module_violations(source: str, file: str) -> set[ThinnessViolation]:
    """Forbidden modules `source` imports in any scope (relative and `from pkg import mod` forms too) or reaches by a name
    that an import binds to a package above one (``from archon_search import services`` then ``services.validators``)."""
    tree = ast.parse(source, filename=file)
    return _forbidden_imports(tree, file, "module") | _forbidden_uses(tree, _bindings(tree, file), file, "module")


def _find_function(tree: ast.AST, name: str, file: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    found = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name]
    if len(found) != 1:
        raise LookupError(f"{file}: expected exactly one function named {name!r}, found {len(found)}: the scan would check nothing")
    return found[0]


def _tainted_helpers(tree: ast.Module, target: ast.AST, module_bound: dict[str, str], file: str) -> set[str]:
    """Functions other than `target` and the ones enclosing it that use a forbidden module directly or by calling another
    tainted function (the old telemetry and validation wrappers). Each is judged by the module imports plus its own."""
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not target and target not in ast.walk(node)
    ]
    tainted = {
        node.name
        for node in functions
        if _forbidden_uses(node, {**module_bound, **_bindings(node, file)}, file, node.name) or _forbidden_imports(node, file, node.name)
    }
    while grown := {
        node.name for node in functions if node.name not in tainted and tainted & {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    }:
        tainted |= grown
    return tainted


def function_body_violations(source: str, file: str, function: str) -> set[ThinnessViolation]:
    """Forbidden modules used inside the one function named `function`: names it uses that a module-level import or an
    import in its body binds to a forbidden module, forbidden imports written in its body, and calls to a helper that
    itself uses one (directly or through other helpers). Imports only the other functions use do not count."""
    tree = ast.parse(source, filename=file)
    target = _find_function(tree, function, file)
    scope = f"function {function}"
    module_bound = _bindings(tree, file, module_level_only=True)
    found = _forbidden_uses(target, {**module_bound, **_bindings(target, file)}, file, scope) | _forbidden_imports(target, file, scope)
    used_names = {node.id for node in ast.walk(target) if isinstance(node, ast.Name)}
    found |= {ThinnessViolation(file, f"helper {name}", scope) for name in sorted(_tainted_helpers(tree, target, module_bound, file) & used_names)}
    return found


def scan_adapters(root: Path = _REPO_ROOT) -> set[ThinnessViolation]:
    """The thinness violations of the three Search adapters under `root`; a missing adapter file raises."""
    found: set[ThinnessViolation] = set()
    for file in _WHOLE_MODULE_ADAPTERS:
        found |= whole_module_violations((root / file).read_text(encoding="utf-8"), file)
    found |= function_body_violations((root / _MCP).read_text(encoding="utf-8"), _MCP, _MCP_SEARCH_TOOL)
    return found


def _make_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp(dir=tmp_path))
    for relative in (_ROUTES_SEARCH, _SHIM, _MCP):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(files.get(relative, ""), encoding="utf-8")
    return root


_MCP_WITH_OTHER_TOOLS_USING_FORBIDDEN = """\
from archon_search.services.request_hyde import RequestHyDE
from archon_search.services.search_budget import SearchBudget
from archon_search.services.validators import validate_top_k
from archon_search.telemetry.entry import TelemetryEntry


def create_app():
    @app.tool()
    async def search(query):
        return await service.search(query)

    @app.tool()
    async def search_with_context(query):
        hyde = RequestHyDE()
        budget = SearchBudget()
        validate_top_k(1)
        return TelemetryEntry, hyde, budget

    @app.tool()
    async def explain(query):
        return TelemetryEntry()
"""


def test_adapters_import_no_forbidden_module_in_scope() -> None:
    assert scan_adapters() == set()


def test_thinness_scan_fails_on_a_forbidden_import_in_a_scoped_body(tmp_path: Path) -> None:
    source = _MCP_WITH_OTHER_TOOLS_USING_FORBIDDEN.replace(
        "return await service.search(query)", "return RequestHyDE(), await service.search(query)"
    )
    found = scan_adapters(_make_tree(tmp_path, {_MCP: source}))
    assert found == {ThinnessViolation(_MCP, f"{_PKG}.services.request_hyde", "function search")}


def test_thinness_scan_ignores_imports_used_by_other_mcp_tools(tmp_path: Path) -> None:
    assert scan_adapters(_make_tree(tmp_path, {_MCP: _MCP_WITH_OTHER_TOOLS_USING_FORBIDDEN})) == set()


@pytest.mark.parametrize("file", _WHOLE_MODULE_ADAPTERS)
@pytest.mark.parametrize(
    ("statement", "module"),
    [
        ("from archon_search.services.validators import validate_top_k", "services.validators"),
        ("from archon_search.services import search_budget", "services.search_budget"),
        ("from ..services.request_hyde import RequestHyDE", "services.request_hyde"),
        ("from archon_search.telemetry import entry", "telemetry.entry"),
        ("from archon_search.telemetry import TelemetryEntry", "telemetry.entry"),
        ("from archon_search import telemetry\ntelemetry.FilterFlags", "telemetry.entry"),
        ("import archon_search.telemetry.entry as e", "telemetry.entry"),
        ("from archon_search import services\nservices.validators.validate_top_k(1)", "services.validators"),
    ],
)
def test_thinness_scan_fails_on_a_forbidden_import_anywhere_in_a_whole_module_adapter(
    tmp_path: Path, file: str, statement: str, module: str
) -> None:
    for source in (f"{statement}\n", f"def f():\n{textwrap.indent(statement, '    ')}\n"):
        found = scan_adapters(_make_tree(tmp_path, {file: source, _MCP: _MCP_WITH_OTHER_TOOLS_USING_FORBIDDEN}))
        assert {violation.module for violation in found} == {f"{_PKG}.{module}"}
        assert {violation.file for violation in found} == {file}


@pytest.mark.parametrize(
    ("header", "use", "module"),
    [
        ("import archon_search.services.validators as v", "v.validate_top_k(1)", "services.validators"),
        ("import archon_search.services.validators", "archon_search.services.validators.validate_top_k(1)", "services.validators"),
        ("from archon_search.services import validators", "validators.validate_top_k(1)", "services.validators"),
        ("from archon_search.telemetry.entry import TelemetryEntry as T", "T()", "telemetry.entry"),
        ("from archon_search.telemetry import TelemetryEntry", "TelemetryEntry()", "telemetry.entry"),
    ],
)
def test_thinness_scan_resolves_aliases_and_attribute_chains_in_the_search_tool(header: str, use: str, module: str) -> None:
    source = f"{header}\n\n\ndef create_app():\n    async def search(q):\n        {use}\n"
    assert function_body_violations(source, _MCP, "search") == {ThinnessViolation(_MCP, f"{_PKG}.{module}", "function search")}


def test_thinness_scan_flags_search_calling_a_module_level_helper_that_uses_a_forbidden_module() -> None:
    source = (
        "from archon_search.telemetry.entry import TelemetryEntry\n\n\n"
        "def _enqueue_internal_error():\n    return TelemetryEntry()\n\n\n"
        "def create_app():\n    async def search(q):\n        _enqueue_internal_error()\n\n"
        "    async def explain(q):\n        _enqueue_internal_error()\n"
    )
    assert function_body_violations(source, _MCP, "search") == {ThinnessViolation(_MCP, "helper _enqueue_internal_error", "function search")}
    assert function_body_violations(source, _MCP, "explain") == {ThinnessViolation(_MCP, "helper _enqueue_internal_error", "function explain")}


def test_thinness_scan_follows_nested_and_transitive_helpers_and_module_level_try_imports() -> None:
    source = (
        "try:\n    from archon_search.telemetry.entry import TelemetryEntry\nexcept ImportError:\n    TelemetryEntry = None\n\n\n"
        "def create_app():\n    def _leaf():\n        return TelemetryEntry()\n\n    def _middle():\n        return _leaf()\n\n"
        "    async def search(q):\n        _middle()\n"
    )
    assert function_body_violations(source, _MCP, "search") == {ThinnessViolation(_MCP, "helper _middle", "function search")}


def test_thinness_scan_ignores_a_helper_that_uses_an_allowed_module() -> None:
    source = (
        "from archon_search.telemetry import TelemetryReader\n\n\n"
        "def _reader():\n    return TelemetryReader()\n\n\ndef create_app():\n    async def search(q):\n        _reader()\n"
    )
    assert function_body_violations(source, _MCP, "search") == set()


def test_the_telemetry_package_reexports_the_entry_factories_the_scan_treats_as_forbidden() -> None:
    assert {"TelemetryEntry", "FilterFlags"} <= _telemetry_reexports()


def test_thinness_scan_flags_an_import_written_inside_the_search_tool_body() -> None:
    source = "def create_app():\n    async def search(q):\n        from archon_search.services.search_budget import SearchBudget\n"
    assert function_body_violations(source, _MCP, "search") == {
        ThinnessViolation(_MCP, f"{_PKG}.services.search_budget", "function search")
    }


def test_thinness_scan_ignores_a_name_the_body_never_uses_and_an_unrelated_module() -> None:
    source = (
        "import archon_search.services.container as c\nfrom archon_search.services.validators import x\n\n\n"
        "def other():\n    return x\n\n\ndef search(q):\n    return c.build(q)\n"
    )
    assert function_body_violations(source, _MCP, "search") == set()


@pytest.mark.parametrize("source", ["", "def search(q): ...\n\n\ndef other():\n    def search(q): ...\n"])
def test_thinness_scan_refuses_a_file_without_exactly_one_search_tool(source: str) -> None:
    with pytest.raises(LookupError, match="exactly one function named 'search'"):
        function_body_violations(source, _MCP, "search")


def test_thinness_scan_refuses_a_tree_without_an_adapter_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        scan_adapters(Path(tempfile.mkdtemp(dir=tmp_path)))
