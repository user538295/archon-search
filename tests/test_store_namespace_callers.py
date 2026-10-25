"""Every package call of an ownership-checked ``SearchStore`` method names the caller's namespace (SecReview #2).

``ensure_collection``, ``ingest_chunks`` and ``delete_document`` default ``namespace`` to ``default`` so a
single-namespace deployment and the tests stay terse. That default would let a new call site that forgets the
argument act as the default namespace — so this guard fails the build for any call in the package that does not
pass one.
"""
from __future__ import annotations

import ast
from pathlib import Path

import archon_search

_PACKAGE_ROOT = Path(archon_search.__file__).parent
#: method name -> number of positional arguments after which ``namespace`` is passed positionally
_POSITIONAL_NAMESPACE_INDEX: dict[str, int | None] = {
    "ensure_collection": None,
    "ingest_chunks": None,
    "delete_document": 2,
    "delete_by_source_path": 2,
    "drop_collection": None,
    "count_chunks": 1,
}


def _calls_missing_namespace(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    missing: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        method = node.func.attr
        if method not in _POSITIONAL_NAMESPACE_INDEX:
            continue
        if any(kw.arg == "namespace" or kw.arg is None for kw in node.keywords):
            continue
        position = _POSITIONAL_NAMESPACE_INDEX[method]
        if position is not None and len(node.args) > position:
            continue
        missing.append(f"{path.relative_to(_PACKAGE_ROOT)}:{node.lineno} {method}()")
    return missing


def test_every_package_call_of_an_ownership_checked_store_method_passes_the_namespace() -> None:
    offenders: list[str] = []
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        if path.name == "store.py":
            continue  # the definitions, and the store's own internal calls
        offenders.extend(_calls_missing_namespace(path))

    assert not offenders, "calls that act as the default namespace by omission:\n" + "\n".join(offenders)
