"""Structural guard (S493): every fastembed model construction in ``archon_search/``
passes ``cache_dir=str(get_fastembed_cache_dir())``.

fastembed honours ``FASTEMBED_CACHE_PATH`` only when ``cache_dir`` is ``None``
(``fastembed/common/utils.py`` ``define_cache_dir``). A site that passes any other
directory (BE-26's ``get_models_dir()``) silently ignores the operator's export; a site
that omits ``cache_dir`` lands in ``<tempdir>/fastembed_cache``. Either way the wizard's
pre-warm, the startup probes and the runtime backends stop agreeing on one cache.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import archon_search

_PACKAGE_DIR = Path(archon_search.__file__).parent
_FASTEMBED_PACKAGE = "fastembed"
# fastembed's model classes: TextEmbedding, SparseTextEmbedding, ImageEmbedding, ...,
# and TextCrossEncoder.
_MODEL_CLASS_SUFFIXES = ("Embedding", "CrossEncoder")
_REQUIRED_CACHE_DIR = "str(get_fastembed_cache_dir())"
# embedder.py, reranker.py, model_validation.py x2, install/prewarm.py x2 — the
# presence anchor: a scan that silently matches nothing must not pass.
_EXPECTED_SITE_COUNT = 6

_Scope = ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda
_SCOPE_TYPES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_NESTED_SCOPE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _is_model_class(name: str) -> bool:
    return name.endswith(_MODEL_CLASS_SUFFIXES)


def _fastembed_class_aliases(tree: ast.Module) -> dict[str, str]:
    """Local name -> fastembed class for every ``from fastembed[...] import X [as Y]``."""
    return {
        alias.asname or alias.name: alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.split(".")[0] == _FASTEMBED_PACKAGE
        for alias in node.names
        if _is_model_class(alias.name)
    }


def _is_fastembed_construction(call: ast.Call, aliases: dict[str, str]) -> bool:
    if isinstance(call.func, ast.Name):
        return call.func.id in aliases
    # ``fastembed.TextEmbedding(...)`` after ``import fastembed``.
    return isinstance(call.func, ast.Attribute) and _is_model_class(call.func.attr)


def _parents(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _enclosing_scopes(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> list[_Scope]:
    """Innermost first, ending with the module."""
    scopes: list[_Scope] = []
    current: ast.AST | None = parents.get(node)
    while current is not None:
        if isinstance(current, _SCOPE_TYPES):
            scopes.append(current)
        current = parents.get(current)
    return scopes


def _own_nodes(scope: _Scope) -> list[ast.AST]:
    """Nodes of *scope* itself, not of the functions/classes nested in it."""
    found: list[ast.AST] = []
    pending = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        found.append(node)
        if not isinstance(node, _NESTED_SCOPE_TYPES):
            pending.extend(ast.iter_child_nodes(node))
    return found


def _parameter_names(scope: _Scope) -> set[str]:
    if isinstance(scope, ast.Module):
        return set()
    args = scope.args
    every = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
    return {arg.arg for arg in every if arg is not None}


def _resolve_name(name: ast.Name, parents: dict[ast.AST, ast.AST]) -> str:
    """What a ``cache_dir=<name>`` passes: the binding in the innermost scope that binds
    *name* (Python's closure rule). Resolves to the required expression only when every
    binding there is exactly ``<name> = str(get_fastembed_cache_dir())``."""
    for scope in _enclosing_scopes(name, parents):
        if name.id in _parameter_names(scope):
            return f"{name.id} (a parameter)"
        stores = [
            node
            for node in _own_nodes(scope)
            if isinstance(node, ast.Name) and node.id == name.id and isinstance(node.ctx, ast.Store)
        ]
        if not stores:
            continue
        values = set()
        for store in stores:
            binding = parents[store]
            if isinstance(binding, ast.Assign) and binding.targets == [store]:
                values.add(ast.unparse(binding.value))
            else:  # augmented, annotated, tuple, loop or with-target binding
                values.add(ast.unparse(binding))
        if values == {_REQUIRED_CACHE_DIR}:
            return _REQUIRED_CACHE_DIR
        return f"{name.id} bound to {sorted(values)}"
    return f"{name.id} (unbound)"


def _sites_in(source: str, label: str) -> list[tuple[str, int, str | None]]:
    """(label, line, resolved cache_dir value or None) for every fastembed construction."""
    tree = ast.parse(source, filename=label)
    aliases = _fastembed_class_aliases(tree)
    parents = _parents(tree)
    sites: list[tuple[str, int, str | None]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_fastembed_construction(node, aliases)):
            continue
        value = next((kw.value for kw in node.keywords if kw.arg == "cache_dir"), None)
        if value is None:
            resolved = None
        elif isinstance(value, ast.Name):
            resolved = _resolve_name(value, parents)
        else:
            resolved = ast.unparse(value)
        sites.append((label, node.lineno, resolved))
    return sites


def _fastembed_sites() -> list[tuple[str, int, str | None]]:
    sites: list[tuple[str, int, str | None]] = []
    for path in sorted(_PACKAGE_DIR.rglob("*.py")):
        label = str(path.relative_to(_PACKAGE_DIR.parent))
        sites.extend(_sites_in(path.read_text(encoding="utf-8"), label))
    return sites


def test_every_fastembed_construction_passes_the_fastembed_cache_dir() -> None:
    sites = _fastembed_sites()

    assert len(sites) == _EXPECTED_SITE_COUNT, (
        f"expected {_EXPECTED_SITE_COUNT} fastembed construction sites, found {sites}; "
        "update the count only after checking each new site's cache_dir"
    )
    offenders = [site for site in sites if site[2] != _REQUIRED_CACHE_DIR]
    assert offenders == [], (
        f"fastembed constructions must pass cache_dir={_REQUIRED_CACHE_DIR} so "
        f"FASTEMBED_CACHE_PATH is honoured (S493): {offenders}"
    )


# ---------------------------------------------------------------------------
# The scanner itself: the shapes a regression could hide behind
# ---------------------------------------------------------------------------

_HOISTED = """
from fastembed import TextEmbedding
def load():
    cache_dir = str(get_fastembed_cache_dir())
    return lambda: TextEmbedding("m", cache_dir=cache_dir)
"""


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(_HOISTED, [_REQUIRED_CACHE_DIR], id="hoisted-into-a-closure"),
        pytest.param(
            "from fastembed import TextEmbedding as TE\n"
            "TE('m', cache_dir=str(get_models_dir()))\n",
            ["str(get_models_dir())"],
            id="aliased-import",
        ),
        pytest.param(
            "from fastembed.sparse import SparseTextEmbedding\nSparseTextEmbedding('m')\n",
            [None],
            id="other-model-class-without-cache-dir",
        ),
        pytest.param(
            "from fastembed import TextEmbedding\n"
            "def load():\n"
            "    cache_dir = str(get_fastembed_cache_dir())\n"
            "    cache_dir = str(get_models_dir())\n"
            "    TextEmbedding('m', cache_dir=cache_dir)\n",
            ["cache_dir bound to ['str(get_fastembed_cache_dir())', 'str(get_models_dir())']"],
            id="reassigned",
        ),
        pytest.param(
            "from fastembed import TextEmbedding\n"
            "cache_dir = str(get_fastembed_cache_dir())\n"
            "def load(cache_dir):\n"
            "    TextEmbedding('m', cache_dir=cache_dir)\n",
            ["cache_dir (a parameter)"],
            id="shadowed-by-a-parameter",
        ),
        pytest.param(
            "from fastembed import TextEmbedding\n"
            "def a():\n"
            "    cache_dir = str(get_fastembed_cache_dir())\n"
            "def b():\n"
            "    TextEmbedding('m', cache_dir=cache_dir)\n",
            ["cache_dir (unbound)"],
            id="bound-in-another-function",
        ),
    ],
)
def test_scanner_resolves_aliases_and_function_local_bindings(
    source: str, expected: list[str | None]
) -> None:
    assert [resolved for _label, _line, resolved in _sites_in(source, "<src>")] == expected
