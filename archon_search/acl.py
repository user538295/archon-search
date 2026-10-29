"""ACL parsing utilities for archon-search chunk-level access control."""

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from archon_search.constants import _NAMESPACE_RE

_ACL_SIDECAR_MAX_BYTES = 65536

logger = logging.getLogger(__name__)


@dataclass
class AclResolutionResult:
    """Result of resolving the effective ACL for a document.

    Attributes:
        acl: None (no ACL declared: open to every namespace), [] (deny-all) or the
            list of allowed namespace names. A declared ACL that cannot be used is
            deny-all, never None — see ``parse_acl_value``.
        source: 'frontmatter', 'sidecar', or None (no rule).
        sidecar_path: absolute path to the sidecar file when source='sidecar'; None otherwise.
        warnings: human-readable warning strings (e.g. unusable sidecar, shadowing).
    """

    acl: list[str] | None
    source: str | None
    sidecar_path: Path | None
    warnings: list[str] = field(default_factory=list)


def is_acl_namespace_valid(name: str) -> bool:
    """Return True if name matches _NAMESPACE_RE and is not 'deny-all'."""
    return _NAMESPACE_RE.fullmatch(name) is not None and name != "deny-all"


def _warn(warnings: list[str], message: str) -> None:
    """Log *message* at WARNING level and append it to *warnings* (surfaced in the ingest result)."""
    logger.warning("%s", message)
    warnings.append(message)


def parse_acl_value(raw: Any, doc_path: str) -> tuple[list[str] | None, list[str]]:
    """Normalize and validate an _acl YAML value.

    An author who declared an ACL intended a restriction. Silently opening the
    document because the declaration is unusable (wrong type, typo'd names) would
    leak restricted content, whereas denying is recoverable: fix the value,
    reindex, and the warning is surfaced in the ingest result. So a declared but
    unusable value fails closed (``[]``); only an absent or blank value is open.

    Returns:
        A tuple of ``(acl_entries, warnings)``:

        - ``acl_entries``:
            - ``None``: no restriction declared (key absent/null or a blank scalar)
            - ``[]``: deny-all (no namespace may access this chunk): an explicit empty
              list, the sole entry ``deny-all``, or a declaration that is unusable —
              a bool/int/dict/other type, a list with no string element, or no valid
              namespace name (including ``deny-all`` mixed with only invalid names)
            - ``[str, ...]``: the valid namespace names that may access the chunk
              (partially valid lists narrow to their valid names; ``deny-all`` mixed
              with valid names is dropped)
        - ``warnings``: human-readable warning strings; non-empty whenever a
          declared value was invalid, ambiguous or partly dropped.
    """
    if raw is None:
        return None, []

    warnings: list[str] = []
    candidates: list[str]

    if isinstance(raw, str):
        tokens = re.split(r"[,\n]", raw.strip())
        candidates = [t.strip() for t in tokens if t.strip()]
        if not candidates:
            return None, warnings  # blank scalar: no restriction declared

    elif isinstance(raw, list):
        if len(raw) == 0:
            return [], []
        candidates = [item for item in raw if isinstance(item, str)]
        non_str_count = len(raw) - len(candidates)
        if non_str_count and not candidates:
            _warn(
                warnings,
                f"_acl in {doc_path} has only non-string elements ({non_str_count} dropped); chunk denied",
            )
            return [], warnings
        if non_str_count:
            _warn(warnings, f"_acl in {doc_path} has {non_str_count} non-string element(s) (dropped)")

    else:
        # bool, int, float, dict, ... (bool is a subclass of int; neither is a list or str)
        _warn(
            warnings,
            f"_acl in {doc_path} has invalid type {type(raw).__name__} (ignored); chunk denied",
        )
        return [], warnings

    deny_all_entries = [c for c in candidates if c == "deny-all"]
    non_deny_all = [c for c in candidates if c != "deny-all"]
    valid = [c for c in non_deny_all if is_acl_namespace_valid(c)]
    invalid_count = len(non_deny_all) - len(valid)

    if valid:
        if invalid_count:
            _warn(warnings, f"_acl in {doc_path} has {invalid_count} invalid namespace names (dropped)")
        if deny_all_entries:
            # deny-all mixed with valid names — drop deny-all, use valid names
            _warn(
                warnings,
                f"_acl in {doc_path} contains 'deny-all' mixed with valid namespaces; "
                "'deny-all' dropped, using valid namespaces only",
            )
        return valid, warnings

    if not non_deny_all:
        # deny-all is the sole kind of entry — interpret as deny-all
        _warn(
            warnings,
            f"_acl in {doc_path} contains the reserved word 'deny-all' as a namespace name; "
            "interpreting as deny-all (acl: [])",
        )
        return [], warnings

    if deny_all_entries:
        _warn(
            warnings,
            f"_acl in {doc_path} contains 'deny-all' mixed with invalid names; "
            "ambiguous — chunk denied",
        )
    else:
        _warn(
            warnings,
            f"_acl in {doc_path} has {invalid_count} invalid namespace names and no valid one; chunk denied",
        )
    return [], warnings


def read_acl_sidecar(
    doc_path: Path,
) -> tuple[list[str] | None, str | None, Path | None, list[str]]:
    """Read ACL from a sidecar file (<doc_path>.acl).

    A sidecar that exists declares a restriction, so one that cannot be used fails
    closed (``[]``), for the same reason as ``parse_acl_value``: opening the
    document on a symlink, size, encoding, I/O or typo problem would leak restricted
    content, while denying is recoverable (fix the file, reindex) and the warning is
    surfaced in the ingest result.

    Returns a tuple of ``(acl_entries, source, sidecar_path, warnings)``:

    - ``acl_entries``:
        - ``None``: no sidecar, or an empty one (no restriction declared)
        - ``[]``: deny-all — the ``deny-all`` sentinel, or an unusable sidecar
          (symlink, over the size limit, not valid UTF-8, unreadable, or no valid
          namespace name on any line)
        - ``[str, ...]``: the valid namespace names (invalid lines are dropped)
    - ``source``: ``'sidecar'`` when a sidecar file exists (even if unusable);
      ``None`` when no sidecar file is present.
    - ``sidecar_path``: absolute path to the sidecar when source='sidecar'; ``None`` otherwise.
    - ``warnings``: human-readable warning strings; non-empty when the sidecar is
      unusable or contains invalid namespace names.
    """
    sidecar = doc_path.parent / (doc_path.name + ".acl")
    warnings: list[str] = []

    # Before exists(): a dangling symlink does not "exist" but still declares a sidecar.
    if sidecar.is_symlink():
        _warn(warnings, f"ACL sidecar {sidecar} is a symlink (never read); chunk denied")
        return [], "sidecar", sidecar, warnings

    if not sidecar.exists():
        return None, None, None, []

    try:
        raw_bytes = sidecar.read_bytes()
    except OSError as exc:
        _warn(warnings, f"ACL sidecar {sidecar} is unreadable ({exc.strerror or type(exc).__name__}); chunk denied")
        return [], "sidecar", sidecar, warnings

    if len(raw_bytes) > _ACL_SIDECAR_MAX_BYTES:
        _warn(
            warnings,
            f"ACL sidecar {sidecar} exceeds {_ACL_SIDECAR_MAX_BYTES // 1024} KB limit "
            f"({len(raw_bytes)} bytes); chunk denied",
        )
        return [], "sidecar", sidecar, warnings

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        _warn(warnings, f"ACL sidecar {sidecar} is not valid UTF-8; chunk denied")
        return [], "sidecar", sidecar, warnings

    # Strip UTF-8 BOM if present
    text = text.lstrip("﻿")

    lines = [line.strip() for line in text.splitlines()]
    non_empty = [line for line in lines if line]

    if not non_empty:
        return None, "sidecar", sidecar, []

    first = non_empty[0]
    if first.upper() == "DENY-ALL":
        if len(non_empty) > 1:
            logger.warning(
                "ACL sidecar %s has content after 'deny-all' sentinel; extra lines ignored",
                sidecar,
            )
        return [], "sidecar", sidecar, []

    valid: list[str] = []
    for line in non_empty:
        if is_acl_namespace_valid(line):
            valid.append(line)
        else:
            _warn(warnings, f"ACL sidecar {sidecar}: invalid namespace name {line!r} (dropped)")

    if not valid:
        _warn(warnings, f"ACL sidecar {sidecar} has no valid namespace name; chunk denied")
        return [], "sidecar", sidecar, warnings
    return valid, "sidecar", sidecar, warnings




def is_acl_allowed(acl: list[str] | None, namespace: str) -> bool:
    """Return True if the given namespace is permitted by acl.

    Rules:
        - acl is None → True (default-open)
        - acl == []   → False (deny-all)
        - not namespace → False (empty namespace fails closed for protected chunks)
        - namespace in acl → True; otherwise False
    Comparison is case-sensitive.
    """
    if acl is None:
        return True
    if not namespace:
        return False
    return namespace in acl


def apply_acl_filter[T](
    items: list[T],
    get_acl: Callable[[T], list[str] | None],
    namespace: str,
) -> tuple[list[T], bool]:
    """Filter items by ACL, returning (passing_items, any_were_dropped)."""
    passing: list[T] = []
    dropped = False
    for item in items:
        if is_acl_allowed(get_acl(item), namespace):
            passing.append(item)
        else:
            dropped = True
    return passing, dropped


@dataclass(frozen=True)
class ChunkVisibility:
    """The chunks and documents of ONE collection that a namespace may NOT see.

    Derived from the stored per-chunk ``acl`` with the same rule search applies
    (``is_acl_allowed``), for surfaces that expose derived data instead of chunks —
    the document list and the graph (entities, edges, ``source_chunk_ids``, symbols).
    Only the hidden side is stored: an id this object has never heard of (a mention
    whose chunk was deleted, a node with no chunk at all) carries no ACL and is visible.

    Attributes:
        hidden_chunk_ids: chunks whose ACL denies the namespace.
        hidden_doc_ids: documents ALL of whose chunks are hidden. A document with at
            least one visible chunk is visible (ACL is resolved per document, so in
            practice its chunks agree).
    """

    hidden_chunk_ids: frozenset[str] = frozenset()
    hidden_doc_ids: frozenset[str] = frozenset()

    @classmethod
    def from_chunk_acls(
        cls, rows: Iterable[tuple[str, str, list[str] | None]], namespace: str
    ) -> ChunkVisibility:
        """Build from ``(doc_id, chunk_id, acl)`` rows, one per stored chunk."""
        hidden_chunks: set[str] = set()
        hidden_docs: set[str] = set()
        visible_docs: set[str] = set()
        for doc_id, chunk_id, acl in rows:
            if is_acl_allowed(acl, namespace):
                visible_docs.add(doc_id)
            else:
                hidden_chunks.add(chunk_id)
                hidden_docs.add(doc_id)
        return cls(frozenset(hidden_chunks), frozenset(hidden_docs - visible_docs))

    def chunk_visible(self, chunk_id: str) -> bool:
        return chunk_id not in self.hidden_chunk_ids

    def doc_visible(self, doc_id: str) -> bool:
        return doc_id not in self.hidden_doc_ids


class ChunkVisibilitySource(Protocol):
    """Provider of per-collection ``ChunkVisibility`` (satisfied by ``SearchStore``)."""

    async def get_chunk_visibility(self, collection: str, namespace: str) -> ChunkVisibility: ...


def resolve_acl(
    doc_path: Path, front_matter_acl: Any, *, allow_sidecar: bool = True
) -> AclResolutionResult:
    """Resolve the effective ACL for a document.

    Precedence: front-matter _acl key > sidecar file.

    Args:
        doc_path: path to the document.
        front_matter_acl: value of the _acl key from front-matter, or None if
            the key was absent.
        allow_sidecar: whether ``doc_path`` names a real file whose ``.acl``
            sidecar may be read. ``False`` for inline-ingested documents (S293),
            whose ``doc_path`` is a client-supplied logical identity — probing the
            server filesystem there would apply a stranger's ACL to the document
            and leak whether an arbitrary server path exists.

    Returns:
        An ``AclResolutionResult`` with:

        - ``acl``: None (no ACL declared), [] (deny-all, including a declared ACL that
          is unusable) or list of allowed namespace names.
        - ``source``: ``'frontmatter'``, ``'sidecar'``, or ``None`` (no rule).
        - ``sidecar_path``: absolute path to the sidecar when source='sidecar'; None otherwise.
        - ``warnings``: human-readable warning strings (unusable sidecar, shadowing, etc.).
    """
    sidecar = doc_path.parent / (doc_path.name + ".acl")

    if front_matter_acl is not None:
        acl, parse_warnings = parse_acl_value(front_matter_acl, str(doc_path))
        warnings = list(parse_warnings)
        if allow_sidecar and sidecar.exists():
            shadow_msg = (
                f"Both front-matter _acl and sidecar {sidecar} exist for {doc_path}; "
                "front-matter takes precedence"
            )
            logger.warning(
                "Both front-matter _acl and sidecar %s exist for %s; "
                "front-matter takes precedence",
                sidecar,
                doc_path,
            )
            warnings.append(shadow_msg)
        return AclResolutionResult(
            acl=acl,
            source="frontmatter",
            sidecar_path=None,
            warnings=warnings,
        )

    if not allow_sidecar:
        return AclResolutionResult(acl=None, source=None, sidecar_path=None, warnings=[])

    acl, source, sidecar_path, sidecar_warnings = read_acl_sidecar(doc_path)
    return AclResolutionResult(
        acl=acl,
        source=source,
        sidecar_path=sidecar_path,
        warnings=sidecar_warnings,
    )
