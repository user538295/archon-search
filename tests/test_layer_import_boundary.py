"""Layer import-boundary scanner (brief C11, plan C1) and its unit tests.

:func:`scan_tree` parses every ``.py`` file under ``archon_search/services``, ``server`` and ``cli`` and
:func:`scan_source` turns each import into an :class:`Edge` (importing file, imported module, scope).
:func:`violations` returns the edges that break rules 1-4 of C1 (see
``Documentation/Backlog/interface-parity/00_service_layer_foundation-c1-import-boundary.tsp``):

1. ``services/`` imports nothing from ``archon_search.server`` / ``archon_search.cli`` or from ``fastapi``,
   ``starlette``, ``fastmcp``, ``click``, ``httpx`` and ``uvicorn`` in any scope, and does not import ``anthropic``
   at module level (ADR-C4: the optional dependency stays lazy, so function-local and type-only imports pass).
2. No file under ``server/`` imports a ``routes_*`` module (the OpenAI shim is one) or ``server.mcp``.
3. A ``cli/`` file imports no other ``cli/`` module except ``_helpers``; ``cli/main.py`` registers the commands.
4. A ``cli/`` file imports nothing from ``archon_search.services`` or ``archon_search.server``.

The permanent exceptions in :data:`_PERMANENT_EXCEPTIONS` are hard-coded and never go stale. :data:`_ALLOWLIST` holds the
known rule 2 violations keyed by edge (file, module, scope); :func:`compare_to_allowlist` fails the real-tree test on a
violation that is not listed and on a listed edge that is no longer a violation. The scan needs
nothing from the repo conftest: the repo root comes from ``__file__`` and the unit tests build their own trees.

Accepted blind spots (a syntactic scan of import statements): ``importlib.import_module`` and ``__import__``;
a ``TYPE_CHECKING`` alias (``from typing import TYPE_CHECKING as TC``) and the negated guard
(``if not TYPE_CHECKING:`` runs its body at run time, so its ``else`` branch is scanned as a run-time import;
both err on the strict side); ``from pkg import *``, which is an edge to ``pkg`` only.

The same file hosts the S31 scan (:func:`scan_moved_authz_imports`): no file under ``archon_search/`` or ``tests/`` imports a
moved authz name from ``archon_search.server.authz``, and that module defines no ``__all__``. Its blind spots are listed
in :func:`moved_authz_imports`.
"""
from __future__ import annotations

import ast
import tempfile
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PKG = "archon_search"
_SCANNED_TREES = (f"{_PKG}/services", f"{_PKG}/server", f"{_PKG}/cli")
_TYPE_CHECKING = "TYPE_CHECKING"
_STAR = "*"


class Scope(StrEnum):
    MODULE_LEVEL = "moduleLevel"
    FUNCTION_LOCAL = "functionLocal"
    TYPE_CHECKING_ONLY = "typeCheckingOnly"


class Edge(NamedTuple):
    """One import: the importing file (repo-relative, POSIX), the absolute imported module and the scope."""

    file: str
    module: str
    scope: Scope


def _module_exists(root: Path, module: str) -> bool:
    *parents, leaf = module.split(".")
    base = root.joinpath(*parents)
    if not base.is_dir():
        return False
    names = {entry.name for entry in base.iterdir()}  # exact names: `is_file()` ignores case on a macOS filesystem
    return f"{leaf}.py" in names or (leaf in names and (base / leaf / "__init__.py").is_file())


def _package_of(file: str) -> list[str]:
    """The dotted parts of the package a file's relative imports are resolved against."""
    return file.split("/")[:-1]  # `pkg/mod.py` and `pkg/__init__.py` both resolve against `pkg`


class _ImportCollector(ast.NodeVisitor):
    """Collects the import edges of one file; a stack of enclosing function and ``TYPE_CHECKING`` blocks sets the scope."""

    def __init__(self, file: str, root: Path) -> None:
        self._file = file
        self._root = root
        self._stack: list[Scope] = []
        self.edges: set[Edge] = set()

    @property
    def _scope(self) -> Scope:
        if Scope.TYPE_CHECKING_ONLY in self._stack:
            return Scope.TYPE_CHECKING_ONLY
        return Scope.FUNCTION_LOCAL if self._stack else Scope.MODULE_LEVEL

    def _add(self, module: str) -> None:
        self.edges.add(Edge(self._file, module, self._scope))

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._stack.append(Scope.FUNCTION_LOCAL)
        self.generic_visit(node)
        self._stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_If(self, node: ast.If) -> None:
        test = node.test
        if not (
            (isinstance(test, ast.Name) and test.id == _TYPE_CHECKING)
            or (isinstance(test, ast.Attribute) and test.attr == _TYPE_CHECKING)
        ):
            self.generic_visit(node)
            return
        self._stack.append(Scope.TYPE_CHECKING_ONLY)
        for statement in node.body:
            self.visit(statement)
        self._stack.pop()
        for statement in node.orelse:  # the `else` branch runs at run time
            self.visit(statement)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._add(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = _absolute_module(self._file, node)
        is_internal = base == _PKG or base.startswith(f"{_PKG}.")
        for alias in node.names:
            candidate = f"{base}.{alias.name}"
            submodule = alias.name != _STAR and is_internal and _module_exists(self._root, candidate)
            self._add(candidate if submodule else base)


def _absolute_module(file: str, node: ast.ImportFrom) -> str:
    """The absolute module a `from` import of the repo-relative `file` names; a relative one is resolved, never guessed."""
    if node.level == 0:
        return node.module or ""
    package = _package_of(file)
    kept = len(package) - (node.level - 1)
    if kept < 1:
        raise ValueError(f"{file}: relative import of level {node.level} climbs above its top-level package")
    return ".".join([*package[:kept], *(node.module.split(".") if node.module else [])])


def scan_source(source: str, file: str, root: Path) -> set[Edge]:
    """Every import edge of `source`, written as the repo-relative `file`; `root` decides what is a submodule."""
    collector = _ImportCollector(file, root)
    collector.visit(ast.parse(source, filename=file))
    return collector.edges


def _python_files(root: Path, tree: str) -> list[Path]:
    """Every ``.py`` file under `root`/`tree`; a tree with none (missing, misspelled or a wrong `root`) raises."""
    paths = sorted((root / tree).rglob("*.py"))
    if not paths:
        raise FileNotFoundError(f"no Python file under {root / tree}: the scan would check nothing")
    return paths


def scan_tree(root: Path = _REPO_ROOT) -> set[Edge]:
    """Every import edge of every ``.py`` file in the scanned trees (services, server, cli) under `root`.

    A tree with no Python file (missing, misspelled or a wrong `root`) raises instead of scanning nothing.
    """
    edges: set[Edge] = set()
    for tree in _SCANNED_TREES:
        for path in _python_files(root, tree):
            edges |= scan_source(path.read_text(encoding="utf-8"), path.relative_to(root).as_posix(), root)
    return edges


# S31: the authz policy moved to `archon_search.services.authz`; `server/authz.py` keeps only the FastAPI half and
# re-exports nothing, so every import of a moved name from it is a stale path (the names that stay are
# `_STAYING_AUTHZ_NAMES`). The scan covers `archon_search/` and `tests/`.
_SERVER_AUTHZ = f"{_PKG}.server.authz"
_SERVER_AUTHZ_FILE = f"{_PKG}/server/authz.py"
_SERVICES_AUTHZ_FILE = f"{_PKG}/services/authz.py"
_MOVED_AUTHZ_NAMES = frozenset({
    "AuthIdentity",
    "ForbiddenError",
    "FORBIDDEN_CODE",
    "ADMIN_REQUIRED_DETAIL",
    "ADMIN_KEY_GRANT_DETAIL",
    "FOREIGN_NAMESPACE_DETAIL",
    "require_admin",
    "authorize_key_create",
    "authorize_key_listing_namespace",
    "can_manage_key",
    "caller_may_revoke",
})
_STAYING_AUTHZ_NAMES = frozenset({"identity_of", "require_admin_dependency", "caller_identity", "ADMIN_ONLY_RESPONSES"})
_AUTHZ_SCAN_TREES = (_PKG, "tests")
_DUNDER_ALL = "__all__"


class MovedAuthzImport(NamedTuple):
    """One moved name (or ``*``) imported from ``archon_search.server.authz`` by the repo-relative `file`."""

    file: str
    name: str


def moved_authz_imports(source: str, file: str) -> set[MovedAuthzImport]:
    """Every moved authz name that `source` imports from ``archon_search.server.authz`` in any scope.

    Absolute and relative `from` imports are covered, a name once per statement whatever its alias; a star import is
    flagged too, because it would pull the moved names through the module. Blind spots (a syntactic scan of import
    statements): ``import archon_search.server.authz`` or ``from archon_search.server import authz`` followed by
    attribute access, and a dotted string target such as ``monkeypatch.setattr("archon_search.server.authz.<name>")``.
    """
    found: set[MovedAuthzImport] = set()
    for node in ast.walk(ast.parse(source, filename=file)):
        if not isinstance(node, ast.ImportFrom) or _absolute_module(file, node) != _SERVER_AUTHZ:
            continue
        found |= {MovedAuthzImport(file, a.name) for a in node.names if a.name in _MOVED_AUTHZ_NAMES or a.name == _STAR}
    return found


def scan_moved_authz_imports(root: Path = _REPO_ROOT) -> set[MovedAuthzImport]:
    """:func:`moved_authz_imports` over every ``.py`` file of ``archon_search/`` and ``tests/`` under `root`.

    A tree with no Python file raises instead of scanning nothing.
    """
    return {
        found
        for tree in _AUTHZ_SCAN_TREES
        for path in _python_files(root, tree)
        for found in moved_authz_imports(path.read_text(encoding="utf-8"), path.relative_to(root).as_posix())
    }


def defines_dunder_all(source: str) -> bool:
    """Whether `source` mentions ``__all__`` as a name anywhere (assigned, unpacked, extended or in a branch)."""
    return any(isinstance(node, ast.Name) and node.id == _DUNDER_ALL for node in ast.walk(ast.parse(source)))


def top_level_names(source: str) -> set[str]:
    """The names `source` defines at module level: functions, classes and plain or annotated assignments."""
    names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def imported_names(source: str) -> set[str]:
    """The names the module-level imports of `source` bind (`*` for a star import): what a re-export would use."""
    names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            names |= {(a.asname or a.name.partition(".")[0]) for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names |= {(a.asname or a.name) for a in node.names}
    return names


_SERVICES_DENIED_INTERNAL = (f"{_PKG}.server", f"{_PKG}.cli")
_SERVICES_DENIED_THIRD_PARTY = ("fastapi", "starlette", "fastmcp", "click", "httpx", "uvicorn")
_SERVICES_DENIED_AT_MODULE_LEVEL = ("anthropic",)
_SERVER_FORBIDDEN_PATTERNS = (f"{_PKG}.server.routes_*", f"{_PKG}.server.mcp")
_CLI_PACKAGE = f"{_PKG}.cli"
_CLI_SHARED_MODULE = f"{_PKG}.cli._helpers"
_CLI_REGISTRATION_FILE = f"{_PKG}/cli/main.py"
_CLI_FORBIDDEN_PACKAGES = (f"{_PKG}.services", f"{_PKG}.server")


class PermanentException(NamedTuple):
    """An allowed edge that is part of the architecture, not a violation to burn down: it never goes stale."""

    file: str
    module_patterns: tuple[str, ...]
    scopes: frozenset[Scope]
    reason: str


_PERMANENT_EXCEPTIONS = (
    PermanentException(
        f"{_PKG}/server/app.py",
        (f"{_PKG}.server.routes_*", f"{_PKG}.server.mcp"),  # its own literals: widening rule 2 must not exempt `app.py`
        frozenset(Scope),
        "the composition root registers every route module and the MCP app",
    ),
    PermanentException(
        f"{_PKG}/cli/serve.py",
        (f"{_PKG}.server.app",),
        frozenset({Scope.FUNCTION_LOCAL}),
        "the lazy `run_server` import: `serve` starts the server in this process",
    ),
)


def _matches(module: str, pattern: str, *, include_children: bool = True) -> bool:
    """`pkg*` is a prefix match; `pkg` matches the module itself and, with `include_children`, everything below it."""
    if pattern.endswith("*"):
        return module.startswith(pattern[:-1])
    return module == pattern or (include_children and module.startswith(f"{pattern}."))


def _is_permanent_exception(edge: Edge) -> bool:
    """A permanent exception names an exact module or a `*` prefix, as the C1 contract states."""
    return any(
        edge.file == exception.file
        and edge.scope in exception.scopes
        and any(_matches(edge.module, pattern, include_children=False) for pattern in exception.module_patterns)
        for exception in _PERMANENT_EXCEPTIONS
    )


def broken_rule(edge: Edge) -> str | None:
    """The C1 rule `edge` breaks, or None when it is allowed (including by a permanent exception)."""
    if _is_permanent_exception(edge):
        return None
    module = edge.module
    top_level = module.split(".")[0]
    if edge.file.startswith(f"{_PKG}/services/"):
        if any(_matches(module, package) for package in _SERVICES_DENIED_INTERNAL):
            return "rule 1: services import nothing from server or cli"
        if top_level in _SERVICES_DENIED_THIRD_PARTY:
            return "rule 1: services import no web, CLI or HTTP-client package"
        if edge.scope is Scope.MODULE_LEVEL and top_level in _SERVICES_DENIED_AT_MODULE_LEVEL:
            return "rule 1 (ADR-C4): services import the optional dependency only lazily"
    elif edge.file.startswith(f"{_PKG}/server/"):
        if any(_matches(module, pattern) for pattern in _SERVER_FORBIDDEN_PATTERNS):
            return "rule 2: a server module imports no route module and not server.mcp"
    elif edge.file.startswith(f"{_PKG}/cli/"):
        if any(_matches(module, package) for package in _CLI_FORBIDDEN_PACKAGES):
            return "rule 4: the CLI imports nothing from services or server"
        if (
            _matches(module, _CLI_PACKAGE)
            and module != _CLI_SHARED_MODULE
            and edge.file != _CLI_REGISTRATION_FILE
        ):
            return "rule 3: a CLI module imports only cli._helpers"
    return None


def violations(edges: set[Edge]) -> set[Edge]:
    """The edges that break a C1 rule and are not permanent exceptions."""
    return {edge for edge in edges if broken_rule(edge) is not None}


_SERVER = f"{_PKG}/server"
_ROUTES = f"{_PKG}.server.routes_"
_ALLOWLIST: dict[Edge, str] = {
    Edge(f"{_SERVER}/_collection_job.py", f"{_ROUTES}jobs", Scope.MODULE_LEVEL): "takes error details and `_persist_job_status` from the jobs route",
    Edge(f"{_SERVER}/mcp.py", f"{_ROUTES}explain", Scope.MODULE_LEVEL): "takes the explain models and `EXPLAIN_FAILED_DETAIL` from the explain route",
    Edge(f"{_SERVER}/mcp.py", f"{_ROUTES}export", Scope.MODULE_LEVEL): "takes `is_ingesting_cancel_leftover` from the export route",
    Edge(f"{_SERVER}/mcp.py", f"{_ROUTES}collections", Scope.FUNCTION_LOCAL): "takes `_all_collection_paths` from the collections route",
    Edge(f"{_SERVER}/routes_collections.py", f"{_ROUTES}jobs", Scope.MODULE_LEVEL): "takes job helpers and `IngestRequest` from the jobs route",
    Edge(f"{_SERVER}/routes_graph.py", f"{_ROUTES}jobs", Scope.MODULE_LEVEL): "takes `_persist_job_status` and an error detail from the jobs route",
    Edge(f"{_SERVER}/routes_sync.py", f"{_ROUTES}jobs", Scope.MODULE_LEVEL): "takes `_persist_job_status` and error details from the jobs route",
    Edge(f"{_SERVER}/routes_status.py", f"{_ROUTES}collections", Scope.MODULE_LEVEL): "takes `_all_collection_paths` from the collections route",
}  # the eight rule 2 violations of the current tree (plan C1); the list only shrinks, each slice deletes what it resolves


def compare_to_allowlist(found: set[Edge], allowlist: dict[Edge, str]) -> tuple[set[Edge], set[Edge]]:
    """(new, stale): violations not in the allowlist, and allowlist entries whose edge is no longer a violation."""
    return found - allowlist.keys(), allowlist.keys() - found


# Modules the snippets import as submodules: `from pkg import name` targets `pkg.name` only when the file exists.
_STUB_MODULES = (
    "archon_search/__init__.py",
    "archon_search/hyde.py",
    "archon_search/services/__init__.py",
    "archon_search/services/validators.py",
    "archon_search/server/__init__.py",
    "archon_search/server/app.py",
    "archon_search/server/authz.py",
    "archon_search/server/mcp.py",
    "archon_search/server/mcp_schemas.py",
    "archon_search/server/schemas.py",
    "archon_search/server/routes_a.py",
    "archon_search/server/routes_b.py",
    "archon_search/server/routes_jobs.py",
    "archon_search/server/routes_openai_shim.py",
    "archon_search/cli/__init__.py",
    "archon_search/cli/_helpers.py",
    "archon_search/cli/collection.py",
    "archon_search/cli/main.py",
    "archon_search/cli/serve.py",
    "archon_search/cli/sync.py",
    "archon_search/install/__init__.py",
    "archon_search/install/wizard.py",
)

_SERVICES = "archon_search/services/search.py"
_ROUTE = "archon_search/server/routes_a.py"
_APP = "archon_search/server/app.py"
_CLI = "archon_search/cli/status.py"
_MAIN = "archon_search/cli/main.py"
_SERVE = "archon_search/cli/serve.py"
_RUN_SERVER_IMPORT = "from archon_search.server.app import run_server"

_MODULE, _LOCAL, _TYPE = Scope.MODULE_LEVEL, Scope.FUNCTION_LOCAL, Scope.TYPE_CHECKING_ONLY


def _make_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    """A fresh tree under `tmp_path`: the stub modules, then `files` (repo-relative path -> source)."""
    root = Path(tempfile.mkdtemp(dir=tmp_path))
    for relative, source in {**dict.fromkeys(_STUB_MODULES, ""), **files}.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return root


def _tree_violations(tmp_path: Path, files: dict[str, str]) -> set[Edge]:
    return violations(scan_tree(_make_tree(tmp_path, files)))


def _local(statement: str) -> str:
    return f"def _f():\n    {statement}\n"


def _type_only(statement: str) -> str:
    return f"from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    {statement}\n"


def _in_every_scope(statement: str) -> tuple[tuple[str, Scope], ...]:
    return ((statement + "\n", _MODULE), (_local(statement), _LOCAL), (_type_only(statement), _TYPE))


def _assert_violation_in_scopes(
    tmp_path: Path, importer: str, statement: str, module: str, rule: str, scopes: frozenset[Scope] = frozenset(Scope)
) -> None:
    """`statement` in `importer` is an edge to `module` that breaks `rule` in each of `scopes` and passes in the others."""
    for source, scope in _in_every_scope(statement):
        found = _tree_violations(tmp_path, {importer: source})
        edge = Edge(importer, module, scope)
        assert found == ({edge} if scope in scopes else set()), (importer, statement, scope)
        if scope in scopes:
            assert (broken_rule(edge) or "").startswith(rule), (importer, statement, broken_rule(edge))


def test_services_importing_fastapi_server_or_cli_is_a_violation(tmp_path: Path) -> None:
    denied = {
        "import fastapi": "fastapi",
        "from fastapi import FastAPI": "fastapi",
        "from starlette.requests import Request": "starlette.requests",
        "import fastmcp": "fastmcp",
        "import click": "click",
        "import httpx": "httpx",
        "import uvicorn": "uvicorn",
        "import archon_search.server": "archon_search.server",
        "from archon_search.server.authz import identity_of": "archon_search.server.authz",
        "from archon_search.server import mcp": "archon_search.server.mcp",
        "from ..server import authz": "archon_search.server.authz",  # relative, resolved against `services/`
        "from archon_search.cli._helpers import resolve_api_key": "archon_search.cli._helpers",
        "import archon_search.cli": "archon_search.cli",
    }
    for statement, module in denied.items():  # the verdict does not depend on the scope
        _assert_violation_in_scopes(tmp_path, _SERVICES, statement, module, "rule 1")

    # ADR-C4: `anthropic` is an optional dependency, so only the module-level import fails
    lazy = {
        "import anthropic": "anthropic",
        "from anthropic import Anthropic": "anthropic",
        "from anthropic.types import Message": "anthropic.types",
    }
    for statement, module in lazy.items():
        _assert_violation_in_scopes(tmp_path, _SERVICES, statement, module, "rule 1 (ADR-C4)", frozenset({_MODULE}))

    # positive control: nothing matches by string prefix, and the core and the services themselves stay importable
    allowed = (
        "import json\n"
        "import fastapix\n"  # a different top-level package whose name starts with a denied one
        "import anthropicx\n"
        "from archon_search import hyde\n"
        "from archon_search.services import validators\n"
        "from archon_search.server_tools import helper\n"  # not `archon_search.server`
    )
    assert _tree_violations(tmp_path, {_SERVICES: allowed}) == set()


def test_route_importing_route_or_mcp_is_a_violation(tmp_path: Path) -> None:
    cases = (
        (_ROUTE, "from archon_search.server import routes_b", "archon_search.server.routes_b"),
        (_ROUTE, "from archon_search.server.routes_b import thing", "archon_search.server.routes_b"),
        (_ROUTE, "from . import routes_b", "archon_search.server.routes_b"),  # relative, resolved against `server/`
        (_ROUTE, "import archon_search.server.routes_openai_shim", "archon_search.server.routes_openai_shim"),
        (_ROUTE, "from archon_search.server import mcp", "archon_search.server.mcp"),
        (_ROUTE, "from archon_search.server.mcp import mcp_server", "archon_search.server.mcp"),
        # the OpenAI shim is a route module and may not import MCP either
        ("archon_search/server/routes_openai_shim.py", "from archon_search.server import mcp", "archon_search.server.mcp"),
        # a non-route importer of a route module
        (
            "archon_search/server/_collection_job.py",
            "from archon_search.server.routes_jobs import run_job",
            "archon_search.server.routes_jobs",
        ),
        # MCP is held to the same rule as the routes
        ("archon_search/server/mcp.py", "from archon_search.server import routes_b", "archon_search.server.routes_b"),
    )
    for importer, statement, module in cases:
        _assert_violation_in_scopes(tmp_path, importer, statement, module, "rule 2")

    # wire-model modules are not route modules, and the composition root is a permanent exception in every scope
    schemas = "from archon_search.server import authz, mcp_schemas, schemas\nfrom archon_search.server.mcp_schemas import X\n"
    adapters = ("from archon_search.server import mcp, routes_a", "import archon_search.server.routes_openai_shim")
    app = "".join(source for statement in adapters for source, _ in _in_every_scope(statement))
    assert _tree_violations(tmp_path, {_ROUTE: schemas, _APP: app}) == set()


def test_cli_importing_services_server_or_sibling_command_is_a_violation(tmp_path: Path) -> None:
    sibling, forbidden = "rule 3", "rule 4"  # a sibling command module; the services or the server
    cases = {
        "from archon_search.cli import sync": ("archon_search.cli.sync", sibling),
        "from archon_search.cli.collection import resolve_api_key": ("archon_search.cli.collection", sibling),
        "import archon_search.cli.sync": ("archon_search.cli.sync", sibling),
        "from . import sync": ("archon_search.cli.sync", sibling),  # relative, resolved against `cli/`
        "from .sync import sync": ("archon_search.cli.sync", sibling),
        "from archon_search.services import validators": ("archon_search.services.validators", forbidden),
        "from archon_search.services.validators import validate_scope_filter": (
            "archon_search.services.validators",
            forbidden,
        ),
        "from ..services import validators": ("archon_search.services.validators", forbidden),
        "import archon_search.server": ("archon_search.server", forbidden),
        "from archon_search.server.mcp import mcp_server": ("archon_search.server.mcp", forbidden),
        "from ..server import mcp": ("archon_search.server.mcp", forbidden),
    }
    for statement, (module, rule) in cases.items():
        _assert_violation_in_scopes(tmp_path, _CLI, statement, module, rule)

    # only `cli/_helpers.py` and the core are allowed, and `cli/main.py` registers the commands
    allowed = (
        "from archon_search.cli import _helpers\n"
        "from archon_search.cli._helpers import resolve_api_key\n"
        "from . import _helpers as helpers\n"
        "from archon_search import hyde\n"
        "import click\n"
    )
    registrations = "from archon_search.cli.sync import sync\nfrom archon_search.cli import collection\n"
    assert _tree_violations(tmp_path, {_CLI: allowed, _MAIN: registrations}) == set()
    # the exemption of `main.py` covers rule 3 only: it still may not import the server or the services
    assert _tree_violations(tmp_path, {_MAIN: "import archon_search.services\n"}) == {
        Edge(_MAIN, "archon_search.services", _MODULE)
    }


def _edges(root: Path, source: str, file: str = _CLI) -> set[Edge]:
    return scan_source(source, file, root)


def _assert_scopes_are_classified(root: Path) -> None:
    """Module level (also a class body and a try block), function-local (a method, a nested and an async function),
    TYPE_CHECKING-only (bare and `typing.`-qualified; the `else` branch is runtime; a function inside it is type-only)."""
    source = (
        "import typing\n"
        "from typing import TYPE_CHECKING\n"
        "try:\n"
        "    import in_try\n"
        "except ImportError:\n"
        "    in_try = None\n"
        "class K:\n"
        "    import in_class_body\n"
        "    def method(self):\n"
        "        import in_method\n"
        "        def inner():\n"
        "            import in_nested\n"
        "async def coroutine():\n"
        "    import in_async\n"
        "if TYPE_CHECKING:\n"
        "    import type_only\n"
        "    def f():\n"
        "        import type_only_in_function\n"
        "else:\n"
        "    import runtime_branch\n"
        "if typing.TYPE_CHECKING:\n"
        "    import type_only_attribute\n"
        "def g():\n"
        "    if TYPE_CHECKING:\n"
        "        import type_only_inside_function\n"
    )
    assert {(e.module, e.scope) for e in _edges(root, source)} == {
        ("typing", _MODULE),
        ("in_try", _MODULE),
        ("in_class_body", _MODULE),
        ("in_method", _LOCAL),
        ("in_nested", _LOCAL),
        ("in_async", _LOCAL),
        ("type_only", _TYPE),
        ("type_only_in_function", _TYPE),
        ("runtime_branch", _MODULE),
        ("type_only_attribute", _TYPE),
        ("type_only_inside_function", _TYPE),
    }


def _assert_import_forms_are_edges(root: Path) -> None:
    """Plain, aliased, dotted, several names and star; the edge carries the importing file."""
    assert _edges(root, "import os, sys as system\nimport a.b as ab\nfrom c.d import e, f\nfrom g import *\n") == {
        Edge(_CLI, "os", _MODULE),
        Edge(_CLI, "sys", _MODULE),
        Edge(_CLI, "a.b", _MODULE),
        Edge(_CLI, "c.d", _MODULE),
        Edge(_CLI, "g", _MODULE),
    }


def _assert_from_package_import_targets_the_submodule(root: Path) -> None:
    """`from pkg import name` targets `pkg.name` when its file exists (a module or a package), else `pkg`."""
    source = (
        "from archon_search.server import routes_a, not_a_module, MCP\n"  # `MCP` is not `mcp.py`, whatever the filesystem
        "from archon_search.services import validators\n"
        "from archon_search import server\n"
        "from os import path\n"
    )
    assert _edges(root, source) == {
        Edge(_CLI, "archon_search.server.routes_a", _MODULE),
        Edge(_CLI, "archon_search.server", _MODULE),
        Edge(_CLI, "archon_search.services.validators", _MODULE),
        Edge(_CLI, "os", _MODULE),
    }


def _assert_statements_with_the_same_key_are_one_edge(root: Path) -> None:
    source = "import json\nimport json\nfrom json import loads\n" + _local("import json")
    assert _edges(root, source) == {Edge(_CLI, "json", _MODULE), Edge(_CLI, "json", _LOCAL)}


def _assert_relative_imports_are_resolved(root: Path) -> None:
    """Resolved to the absolute module first, from a module and from a package `__init__`; never guessed."""
    source = (
        "from . import sync\nfrom .sync import sync as run\nfrom .. import hyde\nfrom ..server import routes_a\n"
        "from .. import not_a_module\n"
    )
    assert _edges(root, source) == {
        Edge(_CLI, "archon_search.cli.sync", _MODULE),
        Edge(_CLI, "archon_search.hyde", _MODULE),
        Edge(_CLI, "archon_search.server.routes_a", _MODULE),
        Edge(_CLI, "archon_search", _MODULE),
    }
    init = "archon_search/install/__init__.py"
    assert _edges(root, "from . import wizard\nfrom .wizard import run\nfrom . import other\n", init) == {
        Edge(init, "archon_search.install.wizard", _MODULE),
        Edge(init, "archon_search.install", _MODULE),
    }
    with pytest.raises(ValueError, match="status.py"):  # climbs above the top-level package
        _edges(root, "from ... import x\n")


def _assert_scope_changes_a_verdict_only_under_the_anthropic_clause(root: Path) -> None:
    for statement, denied_in in (("import fastapi", {_MODULE, _LOCAL, _TYPE}), ("import anthropic", {_MODULE})):
        found = {scope for source, scope in _in_every_scope(statement) if violations(_edges(root, source, _SERVICES))}
        assert found == denied_in, statement


def _make_tree_without_python_files_in(tmp_path: Path, subtree: str) -> Path:
    root = _make_tree(tmp_path, {})
    for path in (root / subtree).rglob("*.py"):
        path.unlink()
    return root


def _assert_scan_walks_nested_packages_and_refuses_a_tree_without_python_files(tmp_path: Path) -> None:
    deep = "archon_search/server/sub/deep.py"
    root = _make_tree(tmp_path, {deep: "from .. import mcp\nfrom ... import hyde\n"})
    assert {edge for edge in scan_tree(root) if edge.file == deep} == {
        Edge(deep, "archon_search.server.mcp", _MODULE),
        Edge(deep, "archon_search.hyde", _MODULE),
    }
    assert Edge(deep, "archon_search.server.mcp", _MODULE) in violations(scan_tree(root))
    for tree in _SCANNED_TREES:  # every scanned tree is guarded, not only the first
        with pytest.raises(FileNotFoundError, match=tree):
            scan_tree(_make_tree_without_python_files_in(tmp_path, tree))
    with pytest.raises(FileNotFoundError):
        scan_tree(tmp_path / "no-such-root")


def test_function_local_type_checking_relative_and_from_package_imports_are_classified(tmp_path: Path) -> None:
    root = _make_tree(tmp_path, {})
    _assert_scopes_are_classified(root)
    _assert_import_forms_are_edges(root)
    _assert_from_package_import_targets_the_submodule(root)
    _assert_statements_with_the_same_key_are_one_edge(root)
    _assert_relative_imports_are_resolved(root)
    _assert_scope_changes_a_verdict_only_under_the_anthropic_clause(root)
    _assert_scan_walks_nested_packages_and_refuses_a_tree_without_python_files(tmp_path)


def test_run_server_import_allowed_only_in_cli_serve(tmp_path: Path) -> None:
    lazy = "def serve():\n    " + _RUN_SERVER_IMPORT + "\n    run_server()\n"
    assert _tree_violations(tmp_path, {_SERVE: lazy}) == set()

    # the same import from any other CLI file fails, `main.py` included
    other = "archon_search/cli/start.py"
    assert _tree_violations(tmp_path, {other: lazy}) == {Edge(other, "archon_search.server.app", _LOCAL)}
    assert _tree_violations(tmp_path, {_MAIN: lazy}) == {Edge(_MAIN, "archon_search.server.app", _LOCAL)}

    # the exception is pinned to the module and the scope, not to the file
    assert _tree_violations(tmp_path, {_SERVE: _local("from archon_search.server import mcp")}) == {
        Edge(_SERVE, "archon_search.server.mcp", _LOCAL)
    }
    assert _tree_violations(tmp_path, {_SERVE: _RUN_SERVER_IMPORT + "\n"}) == {
        Edge(_SERVE, "archon_search.server.app", _MODULE)
    }
    assert _tree_violations(tmp_path, {_SERVE: _type_only(_RUN_SERVER_IMPORT)}) == {
        Edge(_SERVE, "archon_search.server.app", _TYPE)
    }

    # a permanent exception has no presence requirement (unlike an allowlist entry, which goes stale): without the import nothing fails
    assert _tree_violations(tmp_path, {_SERVE: "def serve():\n    pass\n"}) == set()


def test_real_tree_has_no_unlisted_boundary_violation() -> None:
    """S5: the real tree breaks rules 1-4 in exactly the allowlisted edges; a new one and a stale entry both fail (S7)."""
    new, stale = compare_to_allowlist(violations(scan_tree()), _ALLOWLIST)
    assert not (new or stale), (
        f"new violations (fix the import; never extend the allowlist): {sorted(new)}; "
        f"stale allowlist entries (the violation is fixed; delete the entry): {sorted((e, _ALLOWLIST[e]) for e in stale)}"
    )


def test_stale_allowlist_entry_fails(tmp_path: Path) -> None:
    route, target = "archon_search/server/routes_a.py", "archon_search.server.routes_b"
    module_entry, local_entry = Edge(route, target, _MODULE), Edge(route, target, _LOCAL)
    allowlist = {module_entry: "module-level", local_entry: "function-local"}
    both = "import archon_search.server.routes_b\n" + _local("import archon_search.server.routes_b")

    def compare(source: str, listed: dict[Edge, str]) -> tuple[set[Edge], set[Edge]]:
        return compare_to_allowlist(_tree_violations(tmp_path, {route: source}), listed)

    # every violation listed: clean
    assert compare(both, allowlist) == (set(), set())
    # the module-level import is fixed, the function-local one stays: only the module-level entry is stale (scope counts)
    assert compare(_local("import archon_search.server.routes_b"), allowlist) == (set(), {module_entry})
    # the function-local import is fixed, the module-level one stays: only the function-local entry is stale
    assert compare("import archon_search.server.routes_b\n", allowlist) == (set(), {local_entry})
    # a type-only import is its own key: listed it is clean, and fixed it goes stale
    type_entry = Edge(route, target, _TYPE)
    type_only = _type_only("import archon_search.server.routes_b")
    assert compare(type_only, {type_entry: "type-only"}) == (set(), set())
    assert compare("x = 1\n", {type_entry: "type-only"}) == (set(), {type_entry})
    # both fixed: both entries are stale
    assert compare("x = 1\n", allowlist) == (set(), {module_entry, local_entry})
    # a violation that is not listed is new, and a list that is empty keeps nothing stale
    assert compare(both, {module_entry: "module-level"}) == ({local_entry}, set())
    assert compare("x = 1\n", {}) == (set(), set())
    # an entry whose edge is now allowed (a permanent exception) is stale too
    app = "archon_search/server/app.py"
    assert compare_to_allowlist(
        _tree_violations(tmp_path, {app: both}), {Edge(app, target, _MODULE): "composition root"}
    ) == (set(), {Edge(app, target, _MODULE)})


_AUTHZ_SNIPPET_FILE = "archon_search/server/routes_x.py"


def test_moved_authz_name_imported_from_server_authz_is_flagged() -> None:
    """S31: plain, aliased, multi-name, function-local, relative and star imports of a moved name are all found."""
    file = _AUTHZ_SNIPPET_FILE

    def flagged(source: str, importer: str = file) -> set[MovedAuthzImport]:
        return moved_authz_imports(source, importer)

    def hit(name: str, importer: str = file) -> set[MovedAuthzImport]:
        return {MovedAuthzImport(importer, name)}

    # positive control: a scanner that matches nothing would pass every other test, so each moved name must be found
    for name in sorted(_MOVED_AUTHZ_NAMES):
        assert flagged(f"from {_SERVER_AUTHZ} import {name}\n") == hit(name), name
    assert flagged(f"from {_SERVER_AUTHZ} import ForbiddenError as Refusal\n") == hit("ForbiddenError")
    assert flagged(f"from {_SERVER_AUTHZ} import (\n    AuthIdentity,\n    identity_of,\n    require_admin,\n)\n") == (
        hit("AuthIdentity") | hit("require_admin")
    )
    assert flagged(f"from {_SERVER_AUTHZ} import caller_identity, FORBIDDEN_CODE as C, can_manage_key\n") == (
        hit("FORBIDDEN_CODE") | hit("can_manage_key")
    )
    assert flagged(f"from {_SERVER_AUTHZ} import *\n") == hit("*")
    # every scope
    statement = f"from {_SERVER_AUTHZ} import AuthIdentity"
    for source, _ in _in_every_scope(statement):
        assert flagged(source) == hit("AuthIdentity"), source
    # a relative import is resolved against the importing file
    assert flagged("from .authz import caller_may_revoke\n") == hit("caller_may_revoke")
    other = "archon_search/services/search.py"
    assert flagged("from ..server.authz import AuthIdentity\n", other) == hit("AuthIdentity", other)
    assert flagged("from .authz import AuthIdentity\n", other) == set()  # `services.authz` is the new home


def test_names_that_stay_in_server_authz_are_not_flagged() -> None:
    file = _AUTHZ_SNIPPET_FILE
    staying = sorted(_STAYING_AUTHZ_NAMES)
    for name in staying:
        assert moved_authz_imports(f"from {_SERVER_AUTHZ} import {name}\n", file) == set(), name
    assert moved_authz_imports(f"from {_SERVER_AUTHZ} import {', '.join(staying)}\n", file) == set()
    # the moved names taken from their new home, and unrelated modules that define the same names, are fine
    assert moved_authz_imports("from archon_search.services.authz import AuthIdentity, require_admin\n", file) == set()
    assert moved_authz_imports("from archon_search.services import authz\nimport archon_search.server.authz\n", file) == set()
    assert moved_authz_imports("from other.authz import AuthIdentity\nfrom archon_search.server import authz\n", file) == set()
    assert moved_authz_imports("from . import authz\n", file) == set()


def test_no_code_or_test_imports_moved_authz_name_from_server_authz() -> None:
    """S31: the real tree is clean and `server/authz.py` defines no `__all__` (which would re-export moved names)."""
    # positive controls first, so neither check below can pass by matching nothing
    for source in ('__all__ = ["identity_of"]\n', "__all__: list[str] = []\n", "__all__ += ['x']\n", "__all__, y = [], 1\n",
                   "if True:\n    __all__ = []\n", "__all__.append('x')\n"):
        assert defines_dunder_all(source), source
    assert not defines_dunder_all("x = 1\n")
    found = scan_moved_authz_imports()
    assert not found, f"import these from archon_search.services.authz, never from server.authz: {sorted(found)}"
    assert not defines_dunder_all((_REPO_ROOT / _SERVER_AUTHZ_FILE).read_text(encoding="utf-8"))


def test_authz_name_sets_match_the_real_modules() -> None:
    """The scan's name sets cannot drift: moved names live in `services/authz.py`, staying names in `server/authz.py`."""
    spec = {
        "AuthIdentity", "ForbiddenError", "FORBIDDEN_CODE", "ADMIN_REQUIRED_DETAIL", "ADMIN_KEY_GRANT_DETAIL",
        "FOREIGN_NAMESPACE_DETAIL", "require_admin", "authorize_key_create", "authorize_key_listing_namespace",
        "can_manage_key", "caller_may_revoke",
    }  # the C3 contract's moved names, spelled out so that a dropped or renamed entry fails here
    assert _MOVED_AUTHZ_NAMES == spec
    server_source = (_REPO_ROOT / _SERVER_AUTHZ_FILE).read_text(encoding="utf-8")
    services = top_level_names((_REPO_ROOT / _SERVICES_AUTHZ_FILE).read_text(encoding="utf-8"))
    server = top_level_names(server_source) | imported_names(server_source)  # a re-export by import counts too
    assert _MOVED_AUTHZ_NAMES <= services
    assert _STAYING_AUTHZ_NAMES <= server
    assert not _MOVED_AUTHZ_NAMES & server
    assert _STAR not in server
    # positive control: the re-export forms the guard exists for are seen
    reexport = "from archon_search.services.authz import ForbiddenError, require_admin as ra\nfrom x import *\nimport y.z\n"
    assert imported_names(reexport) == {"ForbiddenError", "ra", _STAR, "y"}


def test_authz_scan_covers_both_trees_and_refuses_a_tree_without_python_files(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        scan_moved_authz_imports(tmp_path)
    (tmp_path / "archon_search").mkdir()
    (tmp_path / "archon_search" / "a.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError):  # tests/ is still missing
        scan_moved_authz_imports(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t.py").write_text(f"from {_SERVER_AUTHZ} import ForbiddenError\n", encoding="utf-8")
    (tmp_path / "archon_search" / "deep").mkdir()
    (tmp_path / "archon_search" / "deep" / "b.py").write_text(f"from {_SERVER_AUTHZ} import require_admin\n", encoding="utf-8")
    assert scan_moved_authz_imports(tmp_path) == {
        MovedAuthzImport("tests/t.py", "ForbiddenError"),
        MovedAuthzImport("archon_search/deep/b.py", "require_admin"),
    }
