"""CI guard: no test module installs a FastMCP stub that can outlive the test.

Test modules used to swap ``fastmcp.FastMCP`` (or the whole ``sys.modules["fastmcp"]`` entry)
for a stub with raw assignments and ``importlib.reload(archon_search.server.mcp)``. Nothing
undoes a raw assignment when a test fails, and an ``if "fastmcp" not in sys.modules`` install
is never undone at all, so the stub leaks into every later test in the same process:
``create_app`` then calls ``FastMCP("archon-search")`` on the stub and fails with
``TypeError: FastMCP() takes no arguments`` (reproduced by running ``tests/test_mcp_export.py``
before ``tests/integration/test_mcp_slice2_be7.py`` with ``-n0``).

The sanctioned forms patch only the ``FastMCP`` name on ``archon_search.server.mcp`` and are
undone on exit: ``stubbed_fastmcp()``, ``stub_mcp_tools()`` and ``stub_mcp_tool()``
(``tests/_fastmcp_stub.py``), ``patch.object(mcp_module, "FastMCP", ...)`` / ``patch(...)`` /
``patch.multiple(mcp_module, FastMCP=...)`` contexts, and
``monkeypatch.setattr(mcp_module, "FastMCP", ...)``.

Flagged patterns (see :data:`_PATTERN_IDS`). The guarded ``sys.modules`` keys are ``fastmcp``,
``fastmcp.*`` and ``archon_search.server.mcp``; ``sys.modules`` is recognised through
``import sys as s``, ``from sys import modules [as m]`` and ``name = sys.modules`` bindings.
Call arguments are read positionally or by keyword (``patch.dict(..., values=...)``,
``monkeypatch.setitem(dic=..., name=...)``, ``patch.object(target=..., attribute=...)``,
``import_module(name=...)``, ``reload(module=...)``). A mapping of keys is a dict literal, a
literal list / tuple / set of ``(key, value)`` pairs, ``dict(...)`` over either, or keywords:

- ``sys_modules_assign`` — ``sys.modules[key] = ...`` (plain, annotated or augmented),
  ``sys.modules |= <mapping>`` and ``sys.modules.update`` / ``.setdefault`` / ``.__setitem__``;
- ``sys_modules_remove`` — ``del sys.modules[key]`` / ``sys.modules.pop(key)`` /
  ``sys.modules.__delitem__(key)``, which forces a re-import that rebinds ``FastMCP`` and splits
  the module object other tests hold;
- ``sys_modules_patch`` — ``monkeypatch.setitem`` / ``monkeypatch.delitem(sys.modules, key)`` and
  ``patch.dict(sys.modules, <mapping>)``; the same calls on any other dict are not flagged. Both
  are undone at teardown, but not completely: a module first imported while the patch is active
  (``archon_search.server.mcp`` binding the stub's ``FastMCP``) stays cached after a
  ``monkeypatch`` undo, which restores only the patched key; ``patch.dict`` restores the whole
  dict and so evicts it from ``sys.modules``, while the ``archon_search.server`` package attribute and every earlier importer keep the stub-bound copy;
- ``fastmcp_attr_assign`` — any ``<expr>.FastMCP = ...`` assignment or ``del <expr>.FastMCP``,
  ``<expr>.__dict__["FastMCP"]`` / ``vars(<expr>)["FastMCP"]`` assigned or deleted, and the
  builtins ``setattr`` / ``delattr(<expr>, "FastMCP", ...)``, bare or as ``builtins.setattr``.
  Deliberately strict: it also flags ``X.FastMCP = ...`` on a throwaway local ``ModuleType`` that
  could never leak — use a sanctioned form there too;
- ``fastmcp_module_patch`` — replacing ``FastMCP`` on the ``fastmcp`` package or a submodule
  through a patcher: ``patch("fastmcp[.<sub>].FastMCP", ...)``, ``monkeypatch.setattr`` /
  ``.delattr("fastmcp[.<sub>].FastMCP", ...)``, ``patch.object`` / ``monkeypatch.setattr`` /
  ``.delattr(<fastmcp module>, "FastMCP", ...)`` and ``patch.multiple(<fastmcp module or its dotted
  name>, FastMCP=...)``, where the module is a dotted name, an import alias or an attribute chain
  below one (``f.server.server``), ``sys.modules[...]`` / ``sys.modules.get(...)`` /
  ``import_module(...)`` inline, or a name bound to any of these. Undone at teardown, but
  ``archon_search.server.mcp`` first imported meanwhile keeps the stub, as above;
- ``mcp_module_reload`` — ``reload(<archon_search.server.mcp>)`` through any importlib alias or
  ``from importlib import reload [as r]``, where the module is named in any of the ways above.

"Bound" means a plain (``a = b = ...``), annotated or walrus (``:=``) assignment to a bare name,
resolved through chains (``mods = sys.modules`` then ``m = mods["fastmcp"]``) to a fixed point. A
name bound to several string literals is guarded if ANY of them is a guarded key. Name bindings
are file-global (scope-blind), which is conservative.

Accepted blind spots:

- a key, attribute name or module reached through anything other than a string literal, a
  bound name, a mapping above, or the aliases above — e.g. ``vars(sys.modules)[k] = ...``,
  ``patch.dict(sys.modules, dict.fromkeys(keys, stub))``, ``patch.dict(sys.modules, helper())``,
  ``patch.multiple(fastmcp, **{"FastMCP": stub})``, ``mod.__dict__.update(FastMCP=stub)``,
  ``getattr(sys, "modules")``, ``import builtins as b; b.setattr(...)``, or a name bound any other
  way — tuple unpacking, a ``for`` / ``with ... as`` / comprehension target, a parameter or a
  ``match`` capture (e.g. ``for key in ("fastmcp",): sys.modules[key] = ...``);
- ``reload()`` of a ``fastmcp`` module. It installs no stub: it builds a second real ``FastMCP``
  class, so ``isinstance`` checks against the other copy fail — a different defect;
- ``patch.object(mcp_module, "FastMCP", Stub).start()`` without a matching ``stop()``. Detecting
  it needs data flow from the patcher to a ``stop()`` / ``addCleanup`` / ``patch.stopall`` that
  may live in another function or fixture; a syntactic check would both miss the stored-patcher
  form and false-positive on correctly-paired ``start()`` calls. Use a ``with`` block instead.
"""
from __future__ import annotations

import ast
import textwrap
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import pytest

_TESTS_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_ROOT.parent
# Eval corpus files are retrieval fixtures, not test code (and must never be imported).
_EXCLUDED_DIRS = (_TESTS_ROOT / "eval" / "corpus",)
# Well below the ~700 files scanned today; a broken rglob/exclusion must not pass by scanning nothing.
_MIN_SCANNED_FILES = 500

_MCP_MODULE = "archon_search.server.mcp"
_MCP_PARENT_PACKAGE, _, _MCP_LEAF = _MCP_MODULE.rpartition(".")
_FASTMCP_PACKAGE = "fastmcp"
_STUBBED_ATTR = "FastMCP"
_SYS_MODULE = "sys"
_SYS_MODULES_ATTR = "modules"
_SYS_MODULES_DOTTED = f"{_SYS_MODULE}.{_SYS_MODULES_ATTR}"
_IMPORTLIB = "importlib"
_RELOAD = "reload"
_IMPORT_MODULE = "import_module"
_BUILTINS_MODULE = "builtins"
_ATTR_BUILTINS = frozenset({"setattr", "delattr"})
_NAMESPACE_DICT_ATTR = "__dict__"
_VARS_BUILTIN = "vars"
_DICT_BUILTIN = "dict"
_SYS_MODULES_GETTER = "get"
_SYS_MODULES_SETTERS = frozenset({"setdefault", "__setitem__"})
_SYS_MODULES_REMOVERS = frozenset({"pop", "__delitem__"})
_MONKEYPATCH_ITEM_METHODS = frozenset({"setitem", "delitem"})
_PATCH_DICT_ATTR = "dict"
_PATCH_OBJECT_ATTR = "object"
_PATCH_MULTIPLE_ATTR = "multiple"
# ``patch("pkg.mod.Attr")`` / ``monkeypatch.setattr("pkg.mod.Attr", value)``: one dotted target string.
_DOTTED_TARGET_PATCHERS = frozenset({"patch", "setattr", "delattr"})
# ``patch.object(module, "Attr")`` / ``monkeypatch.setattr(module, "Attr", value)``.
_MODULE_ATTR_PATCHERS = frozenset({_PATCH_OBJECT_ATTR, "setattr", "delattr"})


class Violation(NamedTuple):
    pattern_id: str
    lineno: int


def _is_fastmcp_key(key: str) -> bool:
    return key == _FASTMCP_PACKAGE or key.startswith(f"{_FASTMCP_PACKAGE}.")


def _is_guarded_key(key: str) -> bool:
    return key == _MCP_MODULE or _is_fastmcp_key(key)


def _is_fastmcp_attr_path(path: str) -> bool:
    module, _, attr = path.rpartition(".")
    return attr == _STUBBED_ATTR and _is_fastmcp_key(module)


def _call_arg(node: ast.Call, index: int, keyword: str) -> ast.expr | None:
    """The argument at positional ``index``, else the one passed as ``keyword=``."""
    if len(node.args) > index:
        return node.args[index]
    return next((kw.value for kw in node.keywords if kw.arg == keyword), None)


def _is_attr_builtin(func: ast.expr) -> bool:
    """``func`` is the builtin ``setattr`` / ``delattr``, bare or as ``builtins.<name>``."""
    if isinstance(func, ast.Name):
        return func.id in _ATTR_BUILTINS
    return (
        isinstance(func, ast.Attribute)
        and func.attr in _ATTR_BUILTINS
        and isinstance(func.value, ast.Name)
        and func.value.id == _BUILTINS_MODULE
    )


def _is_namespace_dict(node: ast.expr) -> bool:
    """``node`` is an object's attribute dict: ``<expr>.__dict__`` or ``vars(<expr>)``."""
    if isinstance(node, ast.Attribute):
        return node.attr == _NAMESPACE_DICT_ATTR
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == _VARS_BUILTIN


def _mapping_keys(mapping: ast.expr) -> Iterator[ast.expr]:
    """Key expressions of a dict literal or a literal sequence of ``(key, value)`` pairs."""
    if isinstance(mapping, ast.Dict):
        yield from (key for key in mapping.keys if key is not None)
    elif isinstance(mapping, (ast.List, ast.Tuple, ast.Set)):
        for pair in mapping.elts:
            if isinstance(pair, (ast.Tuple, ast.List)) and len(pair.elts) == 2:
                yield pair.elts[0]


def _callee_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _name_bindings(node: ast.AST) -> Iterator[tuple[str, ast.expr]]:
    """``(name, value)`` for each bare-name target of an assignment, annotated or walrus."""
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                yield target.id, node.value
    elif (
        isinstance(node, (ast.AnnAssign, ast.NamedExpr))
        and isinstance(node.target, ast.Name)
        and node.value is not None
    ):
        yield node.target.id, node.value


def _assign_targets(node: ast.AST) -> list[ast.expr]:
    if isinstance(node, ast.Assign):
        return list(node.targets)
    if isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        return [node.target]
    return []


@dataclass(frozen=True)
class _FileFacts:
    """Names a file binds to string literals, ``sys``, ``sys.modules``, ``reload`` and the guarded modules."""

    strings: dict[str, frozenset[str]]
    sys_names: frozenset[str]
    sys_modules_names: frozenset[str]
    reload_names: frozenset[str]
    import_module_names: frozenset[str]
    mcp_names: frozenset[str]
    fastmcp_names: frozenset[str]

    def keys(self, node: ast.expr) -> frozenset[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return frozenset({node.value})
        if isinstance(node, ast.Name):
            return self.strings.get(node.id, frozenset())
        return frozenset()

    def is_guarded(self, node: ast.expr) -> bool:
        return any(_is_guarded_key(key) for key in self.keys(node))

    def is_sys_modules(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.sys_modules_names
        return (
            isinstance(node, ast.Attribute)
            and node.attr == _SYS_MODULES_ATTR
            and isinstance(node.value, ast.Name)
            and node.value.id in self.sys_names
        )

    def is_guarded_sys_modules_subscript(self, node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Subscript)
            and self.is_sys_modules(node.value)
            and self.is_guarded(node.slice)
        )

    def is_import_module_call(self, node: ast.expr) -> bool:
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        named = isinstance(func, ast.Name) and func.id in self.import_module_names
        return named or (isinstance(func, ast.Attribute) and func.attr == _IMPORT_MODULE)

    def _is_module(self, node: ast.expr, aliases: frozenset[str], matches: Callable[[str], bool]) -> bool:
        """``node`` names a module ``matches`` accepts: an alias, the dotted name, or a lookup."""
        if isinstance(node, ast.Name) and node.id in aliases:
            return True
        if matches(ast.unparse(node)):
            return True
        if isinstance(node, ast.Subscript) and self.is_sys_modules(node.value):
            return any(matches(key) for key in self.keys(node.slice))
        if not isinstance(node, ast.Call):
            return False
        if self.sys_modules_method(node) == _SYS_MODULES_GETTER:
            name = node.args[0] if node.args else None
        elif self.is_import_module_call(node):
            name = _call_arg(node, 0, "name")
        else:
            return False
        return name is not None and any(matches(key) for key in self.keys(name))

    def is_mcp_module(self, node: ast.expr) -> bool:
        return self._is_module(node, self.mcp_names, _MCP_MODULE.__eq__)

    def is_fastmcp_module(self, node: ast.expr) -> bool:
        """Also any attribute chain below one — ``f.server.server`` under ``import fastmcp as f``."""
        if self._is_module(node, self.fastmcp_names, _is_fastmcp_key):
            return True
        return isinstance(node, ast.Attribute) and self.is_fastmcp_module(node.value)

    def is_reload(self, func: ast.expr) -> bool:
        if isinstance(func, ast.Name):
            return func.id in self.reload_names
        return isinstance(func, ast.Attribute) and func.attr == _RELOAD

    def sys_modules_method(self, node: ast.AST) -> str | None:
        """The method name when ``node`` is a call ``<sys.modules>.<method>(...)``."""
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and self.is_sys_modules(node.func.value)
        ):
            return node.func.attr
        return None

    def has_guarded_mapping_key(self, mappings: list[ast.expr], keywords: list[ast.keyword]) -> bool:
        """Any guarded key among ``mappings`` or in ``keywords`` (``fastmcp=...``).

        A mapping is a dict literal, a literal sequence of ``(key, value)`` pairs, or a
        ``dict(...)`` call over either or keywords.
        """
        if any(kw.arg is not None and _is_guarded_key(kw.arg) for kw in keywords):
            return True
        if any(self.is_guarded(key) for mapping in mappings for key in _mapping_keys(mapping)):
            return True
        return any(
            self.has_guarded_mapping_key(mapping.args, mapping.keywords)
            for mapping in mappings
            if isinstance(mapping, ast.Call)
            and isinstance(mapping.func, ast.Name)
            and mapping.func.id == _DICT_BUILTIN
        )


# ---------------------------------------------------------------------------
# Fact collection: imports, string bindings, then alias resolution to a fixed point.
# ---------------------------------------------------------------------------


@dataclass
class _Aliases:
    """Mutable name sets gathered while scanning one file; frozen into :class:`_FileFacts`."""

    sys_names: set[str] = field(default_factory=lambda: {_SYS_MODULE})
    sys_modules_names: set[str] = field(default_factory=set)
    reload_names: set[str] = field(default_factory=lambda: {_RELOAD})
    import_module_names: set[str] = field(default_factory=lambda: {_IMPORT_MODULE})
    mcp_names: set[str] = field(default_factory=set)
    fastmcp_names: set[str] = field(default_factory=set)

    def module_alias_count(self) -> int:
        return len(self.sys_modules_names) + len(self.mcp_names) + len(self.fastmcp_names)

    def freeze(self, strings: dict[str, frozenset[str]]) -> _FileFacts:
        return _FileFacts(
            strings=strings,
            sys_names=frozenset(self.sys_names),
            sys_modules_names=frozenset(self.sys_modules_names),
            reload_names=frozenset(self.reload_names),
            import_module_names=frozenset(self.import_module_names),
            mcp_names=frozenset(self.mcp_names),
            fastmcp_names=frozenset(self.fastmcp_names),
        )


def _record_import(node: ast.Import, aliases: _Aliases) -> None:
    for alias in node.names:
        if alias.name == _SYS_MODULE and alias.asname:
            aliases.sys_names.add(alias.asname)
        elif alias.name == _MCP_MODULE and alias.asname:
            aliases.mcp_names.add(alias.asname)
        elif _is_fastmcp_key(alias.name):
            aliases.fastmcp_names.add(alias.asname or _FASTMCP_PACKAGE)


def _record_import_from(node: ast.ImportFrom, aliases: _Aliases) -> None:
    module = node.module or ""
    if _is_fastmcp_key(module):
        aliases.fastmcp_names.update(alias.asname or alias.name for alias in node.names)
        return
    from_names = {
        _SYS_MODULE: {_SYS_MODULES_ATTR: aliases.sys_modules_names},
        _IMPORTLIB: {_RELOAD: aliases.reload_names, _IMPORT_MODULE: aliases.import_module_names},
        _MCP_PARENT_PACKAGE: {_MCP_LEAF: aliases.mcp_names},
    }.get(module, {})
    for alias in node.names:
        if alias.name in from_names:
            from_names[alias.name].add(alias.asname or alias.name)


def _string_bindings(nodes: list[ast.AST]) -> dict[str, frozenset[str]]:
    strings: defaultdict[str, set[str]] = defaultdict(set)
    for node in nodes:
        for name, value in _name_bindings(node):
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                strings[name].add(value.value)
    return {name: frozenset(values) for name, values in strings.items()}


def _resolve_module_aliases(
    nodes: list[ast.AST], aliases: _Aliases, strings: dict[str, frozenset[str]]
) -> _FileFacts:
    """Grow ``aliases`` through name bindings until stable: ``mods = sys.modules`` then ``m = mods[...]``."""
    bindings = [binding for node in nodes for binding in _name_bindings(node)]
    while True:
        facts = aliases.freeze(strings)
        size = aliases.module_alias_count()
        for name, value in bindings:
            if facts.is_sys_modules(value):
                aliases.sys_modules_names.add(name)
            elif facts.is_mcp_module(value):
                aliases.mcp_names.add(name)
            elif facts.is_fastmcp_module(value):
                aliases.fastmcp_names.add(name)
        if aliases.module_alias_count() == size:
            return facts


def _collect_facts(tree: ast.Module) -> _FileFacts:
    nodes = list(ast.walk(tree))
    aliases = _Aliases()
    for node in nodes:
        if isinstance(node, ast.Import):
            _record_import(node, aliases)
        elif isinstance(node, ast.ImportFrom):
            _record_import_from(node, aliases)
    return _resolve_module_aliases(nodes, aliases, _string_bindings(nodes))


# ---------------------------------------------------------------------------
# One predicate per pattern id.
# ---------------------------------------------------------------------------


def _sys_modules_assign(node: ast.AST, facts: _FileFacts) -> bool:
    if any(facts.is_guarded_sys_modules_subscript(t) for t in _assign_targets(node)):
        return True
    if isinstance(node, ast.AugAssign) and facts.is_sys_modules(node.target):
        return facts.has_guarded_mapping_key([node.value], [])
    method = facts.sys_modules_method(node)
    if not isinstance(node, ast.Call) or method is None:
        return False
    if method in _SYS_MODULES_SETTERS:
        return bool(node.args) and facts.is_guarded(node.args[0])
    return method == "update" and facts.has_guarded_mapping_key(node.args, node.keywords)


def _sys_modules_remove(node: ast.AST, facts: _FileFacts) -> bool:
    if isinstance(node, ast.Delete):
        return any(facts.is_guarded_sys_modules_subscript(t) for t in node.targets)
    return (
        isinstance(node, ast.Call)
        and facts.sys_modules_method(node) in _SYS_MODULES_REMOVERS
        and bool(node.args)
        and facts.is_guarded(node.args[0])
    )


def _sys_modules_patch(node: ast.AST, facts: _FileFacts) -> bool:
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    method = node.func.attr
    if method in _MONKEYPATCH_ITEM_METHODS:
        target, key = _call_arg(node, 0, "dic"), _call_arg(node, 1, "name")
        return target is not None and key is not None and facts.is_sys_modules(target) and facts.is_guarded(key)
    target = _call_arg(node, 0, "in_dict")
    if method != _PATCH_DICT_ATTR or target is None:
        return False
    if not (facts.is_sys_modules(target) or _SYS_MODULES_DOTTED in facts.keys(target)):
        return False
    values = [kw.value for kw in node.keywords if kw.arg == "values"]
    return facts.has_guarded_mapping_key(node.args[1:] + values, node.keywords)


def _fastmcp_attr_assign(node: ast.AST, facts: _FileFacts) -> bool:
    targets = node.targets if isinstance(node, ast.Delete) else _assign_targets(node)
    if any(isinstance(t, ast.Attribute) and t.attr == _STUBBED_ATTR for t in targets):
        return True
    if any(
        isinstance(t, ast.Subscript) and _is_namespace_dict(t.value) and _STUBBED_ATTR in facts.keys(t.slice)
        for t in targets
    ):
        return True
    return (
        isinstance(node, ast.Call)
        and _is_attr_builtin(node.func)
        and len(node.args) >= 2
        and _STUBBED_ATTR in facts.keys(node.args[1])
    )


def _fastmcp_module_patch(node: ast.AST, facts: _FileFacts) -> bool:
    if not isinstance(node, ast.Call):
        return False
    patcher, target = _callee_name(node), _call_arg(node, 0, "target")
    # Builtin ``setattr`` / ``delattr`` (bare or ``builtins.``) belong to ``fastmcp_attr_assign``.
    if target is None or _is_attr_builtin(node.func):
        return False
    if patcher in _DOTTED_TARGET_PATCHERS and any(_is_fastmcp_attr_path(k) for k in facts.keys(target)):
        return True
    if patcher == _PATCH_MULTIPLE_ATTR and isinstance(node.func, ast.Attribute):
        replaces_fastmcp = any(kw.arg == _STUBBED_ATTR for kw in node.keywords)
        names_fastmcp = facts.is_fastmcp_module(target) or any(_is_fastmcp_key(k) for k in facts.keys(target))
        return replaces_fastmcp and names_fastmcp
    if not (isinstance(node.func, ast.Attribute) and patcher in _MODULE_ATTR_PATCHERS):
        return False
    attribute = _call_arg(node, 1, "attribute" if patcher == _PATCH_OBJECT_ATTR else "name")
    return attribute is not None and _STUBBED_ATTR in facts.keys(attribute) and facts.is_fastmcp_module(target)


def _mcp_module_reload(node: ast.AST, facts: _FileFacts) -> bool:
    if not (isinstance(node, ast.Call) and facts.is_reload(node.func)):
        return False
    module = _call_arg(node, 0, "module")
    return module is not None and facts.is_mcp_module(module)


_PATTERNS: dict[str, Callable[[ast.AST, _FileFacts], bool]] = {
    "sys_modules_assign": _sys_modules_assign,
    "sys_modules_remove": _sys_modules_remove,
    "sys_modules_patch": _sys_modules_patch,
    "fastmcp_attr_assign": _fastmcp_attr_assign,
    "fastmcp_module_patch": _fastmcp_module_patch,
    "mcp_module_reload": _mcp_module_reload,
}
_PATTERN_IDS = frozenset(_PATTERNS)


def _scan(source: str) -> list[Violation]:
    """Return every leaking FastMCP-stub install in ``source``, in line order."""
    tree = ast.parse(source)
    facts = _collect_facts(tree)
    found = [
        Violation(pattern_id, node.lineno)
        for node in ast.walk(tree)
        for pattern_id, matches in _PATTERNS.items()
        if matches(node, facts)
    ]
    return sorted(found, key=lambda v: (v.lineno, v.pattern_id))


def _test_files() -> list[Path]:
    return sorted(
        path
        for path in _TESTS_ROOT.rglob("*.py")
        if not any(path.is_relative_to(excluded) for excluded in _EXCLUDED_DIRS)
    )


# ---------------------------------------------------------------------------
# Meta-tests: the scan fires on each leaking form and stays silent on sanctioned ones.
# ---------------------------------------------------------------------------


_VIOLATIONS: list[tuple[str, str]] = [
    # sys_modules_assign — subscript assignment, every target kind and sys.modules spelling.
    ("sys_modules_assign", 'import sys\nsys.modules["fastmcp"] = stub\n'),
    ("sys_modules_assign", 'import sys\n_M = "fastmcp"\nsys.modules[_M] = stub\n'),
    ("sys_modules_assign", 'import sys\n_M = "fastmcp"\n_M = "openai"\nsys.modules[_M] = stub\n'),
    ("sys_modules_assign", 'import sys\n_M: str = "fastmcp"\nsys.modules[_M] = stub\n'),
    ("sys_modules_assign", 'import sys\nsys.modules["fastmcp.server"] = stub\n'),
    ("sys_modules_assign", 'import sys\nsys.modules["archon_search.server.mcp"] = stub\n'),
    ("sys_modules_assign", 'import sys\nsys.modules["fastmcp"]: object = stub\n'),
    ("sys_modules_assign", 'import sys\nsys.modules |= {"fastmcp": stub}\n'),
    ("sys_modules_assign", 'import sys as s\ns.modules["fastmcp"] = stub\n'),
    ("sys_modules_assign", 'from sys import modules\nmodules["fastmcp"] = stub\n'),
    ("sys_modules_assign", 'from sys import modules as m\nm["fastmcp"] = stub\n'),
    ("sys_modules_assign", 'import sys\nmods = sys.modules\nmods["fastmcp"] = stub\n'),
    ("sys_modules_assign", 'import sys\nsys.modules.update({"fastmcp": stub})\n'),
    ("sys_modules_assign", "import sys\nsys.modules.update(fastmcp=stub)\n"),
    ("sys_modules_assign", 'import sys\nsys.modules.setdefault("fastmcp", stub)\n'),
    ("sys_modules_assign", 'import sys\nsys.modules.__setitem__("fastmcp", stub)\n'),
    ("sys_modules_assign", 'import sys\nsys.modules.update([("fastmcp", stub)])\n'),
    ("sys_modules_assign", 'import sys\nsys.modules |= (("fastmcp", stub),)\n'),
    ("sys_modules_assign", "import sys\nsys.modules.update(dict(fastmcp=stub))\n"),
    # sys_modules_remove
    ("sys_modules_remove", 'import sys\nsys.modules.pop("archon_search.server.mcp", None)\n'),
    ("sys_modules_remove", 'import sys\n_M = "fastmcp"\ndel sys.modules[_M]\n'),
    ("sys_modules_remove", 'import sys as s\ndel s.modules["fastmcp"]\n'),
    ("sys_modules_remove", 'import sys\nsys.modules.__delitem__("fastmcp")\n'),
    # sys_modules_patch — undone at teardown, but a module first imported while it is active keeps
    # (or is split from) the stub's FastMCP for the rest of the process.
    ("sys_modules_patch", 'import sys\ndef t(monkeypatch):\n    monkeypatch.setitem(sys.modules, "fastmcp", stub)\n'),
    (
        "sys_modules_patch",
        'import sys\ndef t(monkeypatch):\n    monkeypatch.setitem(dic=sys.modules, name="fastmcp", value=stub)\n',
    ),
    (
        "sys_modules_patch",
        'import sys\ndef t(monkeypatch):\n    monkeypatch.delitem(sys.modules, "archon_search.server.mcp")\n',
    ),
    (
        "sys_modules_patch",
        'import sys\ndef t(monkeypatch):\n    monkeypatch.delitem(sys.modules, name="archon_search.server.mcp")\n',
    ),
    ("sys_modules_patch", 'import sys\nwith patch.dict(sys.modules, {"fastmcp": stub}):\n    pass\n'),
    ("sys_modules_patch", 'import sys\nwith patch.dict(sys.modules, values={"fastmcp": stub}):\n    pass\n'),
    ("sys_modules_patch", 'import sys\nwith patch.dict(in_dict=sys.modules, values={"fastmcp": stub}):\n    pass\n'),
    ("sys_modules_patch", 'with patch.dict("sys.modules", {"fastmcp.server": stub}):\n    pass\n'),
    ("sys_modules_patch", "import sys\nwith patch.dict(sys.modules, fastmcp=stub):\n    pass\n"),
    ("sys_modules_patch", 'import sys\nwith patch.dict(sys.modules, [("fastmcp", stub)]):\n    pass\n'),
    ("sys_modules_patch", "import sys\nwith patch.dict(sys.modules, dict(fastmcp=stub)):\n    pass\n"),
    # fastmcp_attr_assign — strict: flags ``X.FastMCP = ...`` even on a throwaway local ModuleType.
    ("fastmcp_attr_assign", 'import sys\nsys.modules["fastmcp"].FastMCP = Stub\n'),
    ("fastmcp_attr_assign", "mod.FastMCP = type('FastMCP', (), {})\n"),
    ("fastmcp_attr_assign", "mod.FastMCP: type = Stub\n"),
    ("fastmcp_attr_assign", 'setattr(mod, "FastMCP", Stub)\n'),
    ("fastmcp_attr_assign", '_A = "FastMCP"\nsetattr(mod, _A, Stub)\n'),
    ("fastmcp_attr_assign", "import fastmcp\ndel fastmcp.FastMCP\n"),
    ("fastmcp_attr_assign", 'delattr(mod, "FastMCP")\n'),
    # The builtin on a real fastmcp module is still this pattern, never also ``fastmcp_module_patch``.
    ("fastmcp_attr_assign", 'import fastmcp\nsetattr(fastmcp, "FastMCP", Stub)\n'),
    ("fastmcp_attr_assign", 'import builtins\nbuiltins.setattr(mod, "FastMCP", Stub)\n'),
    ("fastmcp_attr_assign", 'import builtins\nimport fastmcp\nbuiltins.delattr(fastmcp, "FastMCP")\n'),
    ("fastmcp_attr_assign", 'mod.__dict__["FastMCP"] = Stub\n'),
    ("fastmcp_attr_assign", '_A = "FastMCP"\nvars(mod)[_A] = Stub\n'),
    ("fastmcp_attr_assign", 'del mod.__dict__["FastMCP"]\n'),
    # fastmcp_module_patch — a patcher replacing FastMCP on the fastmcp package or a submodule.
    ("fastmcp_module_patch", 'from unittest.mock import patch\nwith patch("fastmcp.FastMCP", Stub):\n    pass\n'),
    ("fastmcp_module_patch", '@patch("fastmcp.server.server.FastMCP", new=Stub)\ndef t():\n    pass\n'),
    ("fastmcp_module_patch", '_T = "fastmcp.FastMCP"\nwith patch(target=_T, new=Stub):\n    pass\n'),
    ("fastmcp_module_patch", "import fastmcp\nwith patch.object(fastmcp, 'FastMCP', Stub):\n    pass\n"),
    ("fastmcp_module_patch", "import fastmcp as f\nwith patch.object(f, 'FastMCP', Stub):\n    pass\n"),
    (
        "fastmcp_module_patch",
        "import fastmcp.server.server\nwith patch.object(fastmcp.server.server, 'FastMCP', Stub):\n    pass\n",
    ),
    (
        "fastmcp_module_patch",
        "import fastmcp.server.server as srv\nwith patch.object(target=srv, attribute='FastMCP', new=Stub):\n    pass\n",
    ),
    ("fastmcp_module_patch", "from fastmcp.server import server\nwith patch.object(server, 'FastMCP', Stub):\n    pass\n"),
    (
        "fastmcp_module_patch",
        'import sys\nwith patch.object(sys.modules["fastmcp"], "FastMCP", Stub):\n    pass\n',
    ),
    (
        "fastmcp_module_patch",
        'import importlib\nm = importlib.import_module("fastmcp")\nwith patch.object(m, "FastMCP", Stub):\n    pass\n',
    ),
    (
        "fastmcp_module_patch",
        'import importlib\nm = importlib.import_module(name="fastmcp")\nwith patch.object(m, "FastMCP", Stub):\n    pass\n',
    ),
    (
        "fastmcp_module_patch",
        'import sys\nm = sys.modules.get("fastmcp")\nwith patch.object(m, "FastMCP", Stub):\n    pass\n',
    ),
    # Needs a second alias-resolution pass: ``mods`` is unknown when ``m = mods[...]`` is first seen.
    (
        "fastmcp_module_patch",
        'import sys\nmods = sys.modules\nm = mods["fastmcp"]\nwith patch.object(m, "FastMCP", Stub):\n    pass\n',
    ),
    (
        "fastmcp_module_patch",
        'import sys\nif (m := sys.modules["fastmcp"]):\n    with patch.object(m, "FastMCP", Stub):\n        pass\n',
    ),
    ("fastmcp_module_patch", "import fastmcp as f\nwith patch.object(f.server.server, 'FastMCP', Stub):\n    pass\n"),
    ("fastmcp_module_patch", "import fastmcp as f\nsrv = f.server.server\nwith patch.object(srv, 'FastMCP', Stub):\n    pass\n"),
    ("fastmcp_module_patch", "import fastmcp\nwith patch.multiple(fastmcp, FastMCP=Stub):\n    pass\n"),
    ("fastmcp_module_patch", "with patch.multiple('fastmcp.server.server', FastMCP=Stub):\n    pass\n"),
    ("fastmcp_module_patch", 'import fastmcp\ndef t(monkeypatch):\n    monkeypatch.setattr(fastmcp, "FastMCP", Stub)\n'),
    (
        "fastmcp_module_patch",
        'import fastmcp\ndef t(monkeypatch):\n    monkeypatch.setattr(target=fastmcp, name="FastMCP", value=Stub)\n',
    ),
    ("fastmcp_module_patch", 'def t(monkeypatch):\n    monkeypatch.setattr("fastmcp.FastMCP", Stub)\n'),
    ("fastmcp_module_patch", 'import fastmcp\ndef t(monkeypatch):\n    monkeypatch.delattr(fastmcp, "FastMCP")\n'),
    ("fastmcp_module_patch", 'def t(monkeypatch):\n    monkeypatch.delattr("fastmcp.server.server.FastMCP")\n'),
    # mcp_module_reload — every reload spelling and every way to name the module.
    ("mcp_module_reload", "import importlib\nimport archon_search.server.mcp as m\nimportlib.reload(m)\n"),
    ("mcp_module_reload", "import importlib\nfrom archon_search.server import mcp\nimportlib.reload(mcp)\n"),
    (
        "mcp_module_reload",
        'import importlib\nm = importlib.import_module("archon_search.server.mcp")\nimportlib.reload(m)\n',
    ),
    (
        "mcp_module_reload",
        (
            "import importlib\nfrom types import ModuleType\n"
            'm: ModuleType = importlib.import_module("archon_search.server.mcp")\nimportlib.reload(m)\n'
        ),
    ),
    (
        "mcp_module_reload",
        'import importlib\nimport sys\nm = sys.modules["archon_search.server.mcp"]\nimportlib.reload(m)\n',
    ),
    (
        "mcp_module_reload",
        'import importlib\nimport sys\nimportlib.reload(sys.modules["archon_search.server.mcp"])\n',
    ),
    (
        "mcp_module_reload",
        "import importlib\nimport archon_search.server.mcp\nimportlib.reload(archon_search.server.mcp)\n",
    ),
    ("mcp_module_reload", "import importlib as il\nfrom archon_search.server import mcp\nil.reload(mcp)\n"),
    ("mcp_module_reload", "from importlib import reload\nfrom archon_search.server import mcp\nreload(mcp)\n"),
    ("mcp_module_reload", "from importlib import reload as r\nfrom archon_search.server import mcp\nr(mcp)\n"),
    ("mcp_module_reload", "import importlib\nfrom archon_search.server import mcp\nimportlib.reload(module=mcp)\n"),
    (
        "mcp_module_reload",
        'import importlib\nm = importlib.import_module(name="archon_search.server.mcp")\nimportlib.reload(m)\n',
    ),
    (
        "mcp_module_reload",
        'import importlib\nimport sys\nimportlib.reload(sys.modules.get("archon_search.server.mcp"))\n',
    ),
]


def test_every_pattern_id_is_exercised_by_a_violation_case() -> None:
    assert {pattern_id for pattern_id, _ in _VIOLATIONS} == _PATTERN_IDS


@pytest.mark.parametrize(("pattern_id", "source"), _VIOLATIONS)
def test_each_violation_fires_exactly_its_own_pattern(pattern_id: str, source: str) -> None:
    assert {v.pattern_id for v in _scan(source)} == {pattern_id}


def test_scan_reports_line_number_inside_a_function() -> None:
    source = textwrap.dedent("""\
        import sys

        def helper():
            if "fastmcp" not in sys.modules:
                sys.modules["fastmcp"] = object()
    """)
    assert _scan(source) == [Violation("sys_modules_assign", 5)]


_SANCTIONED = [
    "from tests._fastmcp_stub import stubbed_fastmcp\nwith stubbed_fastmcp():\n    pass\n",
    'from tests._fastmcp_stub import stub_mcp_tool\nfn = stub_mcp_tool("search", pipeline, "col1")\n',
    'def t(monkeypatch, mcp_module):\n    monkeypatch.setattr(mcp_module, "FastMCP", Stub)\n',
    "from unittest.mock import patch\nwith patch.object(mcp_module, 'FastMCP', Stub):\n    pass\n",
    'from unittest.mock import patch\nwith patch("archon_search.server.mcp.FastMCP", new=Stub):\n    pass\n',
    'import sys\nsys.modules["openai"] = stub\nsys.modules.pop("openai", None)\n',
    'import sys\n_M = "openai"\nsys.modules[_M] = stub\n',
    'import sys\nsys.modules.update({"openai": stub})\n',
    'import sys\ndef t(monkeypatch):\n    monkeypatch.setitem(sys.modules, "openai", stub)\n',
    'import sys\ndef t(monkeypatch):\n    monkeypatch.delitem(sys.modules, "openai")\n',
    'import sys\nwith patch.dict(sys.modules, {"openai": stub}):\n    pass\n',
    'with patch.dict("sys.modules", {"trafilatura": stub}):\n    pass\n',
    'mods = {}\nmods["fastmcp"] = stub\n',
    'setattr(mod, "Context", Stub)\n',
    "import importlib\nimport archon_search.hyde as hyde_mod\nimportlib.reload(hyde_mod)\n",
    'import importlib\nimport sys\nm = sys.modules["archon_search.hyde"]\nimportlib.reload(m)\n',
    'import sys\nreal = sys.modules["fastmcp"].FastMCP\n',
    # The shared helper's own install (tests/_fastmcp_stub.py) targets the MCP module, not fastmcp.
    (
        "import importlib\nfrom unittest.mock import patch\n_MCP_MODULE = 'archon_search.server.mcp'\n"
        "with patch.object(importlib.import_module(_MCP_MODULE), 'FastMCP', Stub):\n    pass\n"
    ),
    'def t(monkeypatch):\n    monkeypatch.setattr("archon_search.server.mcp.FastMCP", Stub)\n',
    'def t(monkeypatch, mcp_module):\n    monkeypatch.delattr(mcp_module, "FastMCP")\n',
    "import fastmcp\nwith patch.object(fastmcp, 'Context', Stub):\n    pass\n",
    'with patch("fastmcp.Context", Stub):\n    pass\n',
    'with patch("fastmcp_extras.FastMCP", Stub):\n    pass\n',
    'import sys\nwith patch.dict(sys.modules, values={"openai": stub}):\n    pass\n',
    'import sys\ndef t(monkeypatch):\n    monkeypatch.setitem(dic=sys.modules, name="openai", value=stub)\n',
    'delattr(mod, "Context")\n',
    'from tests._fastmcp_stub import stub_mcp_tools\ntools = stub_mcp_tools(pipeline, "col1")\n',
    # The sys_modules_patch target gates: a guarded key in any other dict is not a sys.modules edit.
    'import os\nwith patch.dict(os.environ, {"fastmcp": x}):\n    pass\n',
    'with patch.dict("os.environ", {"fastmcp": x}):\n    pass\n',
    'import os\ndef t(monkeypatch):\n    monkeypatch.setitem(os.environ, "fastmcp", x)\n',
    'import os\ndef t(monkeypatch):\n    monkeypatch.delitem(os.environ, "fastmcp")\n',
    'import sys\nsys.modules.update([("openai", stub)])\n',
    'import sys\nwith patch.dict(sys.modules, dict(openai=stub)):\n    pass\n',
    "with patch.multiple('archon_search.server.mcp', FastMCP=Stub):\n    pass\n",
    "import fastmcp\nwith patch.multiple(fastmcp, Context=Stub):\n    pass\n",
    'import builtins\nbuiltins.setattr(mod, "Context", Stub)\n',
    'mod.__dict__["Context"] = Stub\n',
    'import sys\nm = sys.modules.get("openai")\nwith patch.object(m, "FastMCP", Stub):\n    pass\n',
    "import importlib\nimport archon_search.hyde as hyde_mod\nimportlib.reload(module=hyde_mod)\n",
]


@pytest.mark.parametrize("source", _SANCTIONED)
def test_scan_ignores_sanctioned_forms(source: str) -> None:
    assert _scan(source) == []


# ---------------------------------------------------------------------------
# The guard itself.
# ---------------------------------------------------------------------------


def test_scanned_file_list_covers_the_mcp_test_modules() -> None:
    scanned = _test_files()
    assert _TESTS_ROOT / "test_mcp.py" in scanned
    assert _TESTS_ROOT / "conftest.py" in scanned
    assert len(scanned) > _MIN_SCANNED_FILES


def test_no_test_module_leaks_a_fastmcp_stub() -> None:
    offenders = [
        f"{path.relative_to(_REPO_ROOT)}:{v.lineno}: {v.pattern_id}"
        for path in _test_files()
        for v in _scan(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "FastMCP stub installs that can leak across test modules — use `stubbed_fastmcp()`, "
        "`stub_mcp_tools()` or `stub_mcp_tool()` (tests/_fastmcp_stub.py); for a one-off, "
        f"patch.object / monkeypatch.setattr on {_MCP_MODULE}.FastMCP. Each pattern id and why it "
        f"leaks is documented in the module docstring of {Path(__file__).relative_to(_REPO_ROOT)}. "
        "There is no allowlist: rewrite the install, do not exempt it.\n" + "\n".join(offenders)
    )
