"""ACL parsing utilities for archon-search chunk-level access control."""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from archon_search.constants import _NAMESPACE_RE

_ACL_SIDECAR_MAX_BYTES = 65536

logger = logging.getLogger(__name__)


@dataclass
class AclResolutionResult:
    """Result of resolving the effective ACL for a document.

    Attributes:
        acl: None (fail-open), [] (deny-all), or list of allowed namespace names.
        source: 'frontmatter', 'sidecar', or None (no rule).
        sidecar_path: absolute path to the sidecar file when source='sidecar'; None otherwise.
        warnings: human-readable warning strings (e.g. oversized sidecar, shadowing).
    """

    acl: list[str] | None
    source: str | None
    sidecar_path: Path | None
    warnings: list[str] = field(default_factory=list)


def is_acl_namespace_valid(name: str) -> bool:
    """Return True if name matches _NAMESPACE_RE and is not 'deny-all'."""
    return _NAMESPACE_RE.fullmatch(name) is not None and name != "deny-all"


def parse_acl_value(raw: Any, doc_path: str) -> tuple[list[str] | None, list[str]]:
    """Normalize and validate an _acl YAML value.

    Returns:
        A tuple of ``(acl_entries, warnings)``:

        - ``acl_entries``:
            - ``None``: fail-open (no ACL restriction)
            - ``[]``: deny-all (no namespace may access this chunk)
            - ``[str, ...]``: list of valid namespace names that may access the chunk
        - ``warnings``: list of human-readable warning strings; non-empty when a
          fail-open branch is triggered due to an invalid or ambiguous value.
    """
    if raw is None:
        return None, []

    # Build candidate list from the raw value
    candidates: list[str]
    warnings: list[str] = []

    if isinstance(raw, bool):
        # bool must be checked before int (bool is subclass of int)
        msg = (
            f"_acl in {doc_path} has invalid type {type(raw).__name__} (ignored); "
            "chunk defaults to open"
        )
        logger.warning(
            "_acl in %s has invalid type %s (ignored); chunk defaults to open",
            doc_path,
            type(raw).__name__,
        )
        return None, [msg]

    if isinstance(raw, str):
        tokens = re.split(r"[,\n]", raw.strip())
        candidates = [t.strip() for t in tokens if t.strip()]

    elif isinstance(raw, list):
        if len(raw) == 0:
            return [], []
        non_str_count = sum(1 for item in raw if not isinstance(item, str))
        if non_str_count:
            msg = (
                f"_acl in {doc_path} has {non_str_count} non-string element(s) (dropped); "
                "chunk defaults to open"
            )
            logger.warning(
                "_acl in %s has %d non-string element(s) (dropped); chunk defaults to open",
                doc_path,
                non_str_count,
            )
            warnings.append(msg)
        candidates = [item for item in raw if isinstance(item, str)]

    else:
        msg = (
            f"_acl in {doc_path} has invalid type {type(raw).__name__} (ignored); "
            "chunk defaults to open"
        )
        logger.warning(
            "_acl in %s has invalid type %s (ignored); chunk defaults to open",
            doc_path,
            type(raw).__name__,
        )
        return None, [msg]

    # Separate deny-all entries from the rest
    deny_all_entries = [c for c in candidates if c == "deny-all"]
    non_deny_all = [c for c in candidates if c != "deny-all"]

    # Validate non-deny-all candidates
    valid = [c for c in non_deny_all if is_acl_namespace_valid(c)]
    invalid_count = len(non_deny_all) - len(valid)

    if invalid_count:
        msg = (
            f"_acl in {doc_path} has {invalid_count} invalid namespace names (dropped); "
            "chunk defaults to open"
        )
        logger.warning(
            "_acl in %s has %d invalid namespace names (dropped); chunk defaults to open",
            doc_path,
            invalid_count,
        )
        warnings.append(msg)

    if deny_all_entries:
        if valid:
            # deny-all mixed with valid names — drop deny-all, use valid names
            msg = (
                f"_acl in {doc_path} contains 'deny-all' mixed with valid namespaces; "
                "'deny-all' dropped, using valid namespaces only"
            )
            logger.warning(
                "_acl in %s contains 'deny-all' mixed with valid namespaces; "
                "'deny-all' dropped, using valid namespaces only",
                doc_path,
            )
            warnings.append(msg)
            return valid, warnings
        else:
            # deny-all with no valid names
            if non_deny_all:
                # deny-all mixed with only invalid names — fail-open (not deny-all)
                msg = (
                    f"_acl in {doc_path} contains 'deny-all' mixed with invalid names; "
                    "ambiguous — chunk defaults to open"
                )
                logger.warning(
                    "_acl in %s contains 'deny-all' mixed with invalid names; "
                    "ambiguous — chunk defaults to open",
                    doc_path,
                )
                warnings.append(msg)
                return None, warnings
            else:
                # deny-all is the sole kind of entry — interpret as deny-all
                msg = (
                    f"_acl in {doc_path} contains the reserved word 'deny-all' as a namespace name; "
                    "interpreting as deny-all (acl: [])"
                )
                logger.warning(
                    "_acl in %s contains the reserved word 'deny-all' as a namespace name; "
                    "interpreting as deny-all (acl: [])",
                    doc_path,
                )
                warnings.append(msg)
                return [], warnings

    # No deny-all entries
    if valid:
        return valid, warnings

    # No valid names at all — fail-open
    return None, warnings


def read_acl_sidecar(
    doc_path: Path,
) -> tuple[list[str] | None, str | None, Path | None, list[str]]:
    """Read ACL from a sidecar file (<doc_path>.acl).

    Returns a tuple of ``(acl_entries, source, sidecar_path, warnings)``:

    - ``acl_entries``:
        - ``None``: no sidecar, empty sidecar, or unreadable (fail-open)
        - ``[]``: deny-all sentinel found
        - ``[str, ...]``: list of valid namespace names
    - ``source``: ``'sidecar'`` when a sidecar file exists (even if skipped/invalid);
      ``None`` when no sidecar file is present.
    - ``sidecar_path``: absolute path to the sidecar when source='sidecar'; ``None`` otherwise.
    - ``warnings``: list of human-readable warning strings; non-empty when any
      of the following occur: (1) sidecar exceeds the 64 KB size limit,
      (2) sidecar is a symlink, (3) sidecar is not valid UTF-8,
      (4) sidecar contains invalid namespace names.
    """
    sidecar = doc_path.parent / (doc_path.name + ".acl")

    if not sidecar.exists():
        return None, None, None, []

    if sidecar.is_symlink():
        warning_msg = f"ACL sidecar {sidecar} is a symlink; ignoring (ACL not applied)"
        logger.warning("ACL sidecar %s is a symlink; ignoring", sidecar)
        return None, "sidecar", sidecar, [warning_msg]

    raw_bytes = sidecar.read_bytes()
    if len(raw_bytes) > _ACL_SIDECAR_MAX_BYTES:
        warning_msg = (
            f"ACL sidecar {sidecar} exceeds {_ACL_SIDECAR_MAX_BYTES // 1024} KB limit "
            f"({len(raw_bytes)} bytes); ACL not applied"
        )
        logger.warning(
            "ACL sidecar %s exceeds %d bytes; ignoring",
            sidecar,
            _ACL_SIDECAR_MAX_BYTES,
        )
        return None, "sidecar", sidecar, [warning_msg]

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        warning_msg = f"ACL sidecar {sidecar} is not valid UTF-8; ignoring (ACL not applied)"
        logger.warning("ACL sidecar %s is not valid UTF-8; ignoring", sidecar)
        return None, "sidecar", sidecar, [warning_msg]

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
    sidecar_warnings: list[str] = []
    for line in non_empty:
        if is_acl_namespace_valid(line):
            valid.append(line)
        else:
            warning_msg = f"ACL sidecar {sidecar}: invalid namespace name {line!r} (dropped)"
            logger.warning(
                "ACL sidecar %s: invalid namespace name %r (dropped)", sidecar, line
            )
            sidecar_warnings.append(warning_msg)

    return (valid if valid else None), "sidecar", sidecar, sidecar_warnings


_T = TypeVar("_T")


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


def apply_acl_filter(
    items: list[_T],
    get_acl: Callable[[_T], list[str] | None],
    namespace: str,
) -> tuple[list[_T], bool]:
    """Filter items by ACL, returning (passing_items, any_were_dropped)."""
    passing: list[_T] = []
    dropped = False
    for item in items:
        if is_acl_allowed(get_acl(item), namespace):
            passing.append(item)
        else:
            dropped = True
    return passing, dropped


def resolve_acl(doc_path: Path, front_matter_acl: Any) -> AclResolutionResult:
    """Resolve the effective ACL for a document.

    Precedence: front-matter _acl key > sidecar file.

    Args:
        doc_path: path to the document.
        front_matter_acl: value of the _acl key from front-matter, or None if
            the key was absent.

    Returns:
        An ``AclResolutionResult`` with:

        - ``acl``: None (fail-open), [] (deny-all), or list of allowed namespace names.
        - ``source``: ``'frontmatter'``, ``'sidecar'``, or ``None`` (no rule).
        - ``sidecar_path``: absolute path to the sidecar when source='sidecar'; None otherwise.
        - ``warnings``: human-readable warning strings (oversized sidecar, shadowing, etc.).
    """
    sidecar = doc_path.parent / (doc_path.name + ".acl")

    if front_matter_acl is not None:
        acl, parse_warnings = parse_acl_value(front_matter_acl, str(doc_path))
        warnings = list(parse_warnings)
        if sidecar.exists():
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

    acl, source, sidecar_path, sidecar_warnings = read_acl_sidecar(doc_path)
    return AclResolutionResult(
        acl=acl,
        source=source,
        sidecar_path=sidecar_path,
        warnings=sidecar_warnings,
    )
