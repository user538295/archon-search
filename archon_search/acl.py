"""ACL parsing utilities for archon-search chunk-level access control."""

import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from archon_search.constants import _NAMESPACE_RE

_ACL_SIDECAR_MAX_BYTES = 65536

logger = logging.getLogger("archon_search")


def is_acl_namespace_valid(name: str) -> bool:
    """Return True if name matches _NAMESPACE_RE and is not 'deny-all'."""
    return _NAMESPACE_RE.fullmatch(name) is not None and name != "deny-all"


def parse_acl_value(raw: Any, doc_path: str) -> list[str] | None:
    """Normalize and validate an _acl YAML value.

    Returns:
        - None: fail-open (no ACL restriction)
        - []: deny-all (no namespace may access this chunk)
        - [str, ...]: list of valid namespace names that may access the chunk
    """
    if raw is None:
        return None

    # Build candidate list from the raw value
    candidates: list[str]

    if isinstance(raw, bool):
        # bool must be checked before int (bool is subclass of int)
        logger.warning(
            "_acl in %s has invalid type %s (ignored); chunk defaults to open",
            doc_path,
            type(raw).__name__,
        )
        return None

    if isinstance(raw, str):
        tokens = re.split(r"[,\n]", raw.strip())
        candidates = [t.strip() for t in tokens if t.strip()]

    elif isinstance(raw, list):
        if len(raw) == 0:
            return []
        non_str_count = sum(1 for item in raw if not isinstance(item, str))
        if non_str_count:
            logger.warning(
                "_acl in %s has %d non-string element(s) (dropped); chunk defaults to open",
                doc_path,
                non_str_count,
            )
        candidates = [item for item in raw if isinstance(item, str)]

    else:
        logger.warning(
            "_acl in %s has invalid type %s (ignored); chunk defaults to open",
            doc_path,
            type(raw).__name__,
        )
        return None

    # Separate deny-all entries from the rest
    deny_all_entries = [c for c in candidates if c == "deny-all"]
    non_deny_all = [c for c in candidates if c != "deny-all"]

    # Validate non-deny-all candidates
    valid = [c for c in non_deny_all if is_acl_namespace_valid(c)]
    invalid_count = len(non_deny_all) - len(valid)

    if invalid_count:
        logger.warning(
            "_acl in %s has %d invalid namespace names (dropped); chunk defaults to open",
            doc_path,
            invalid_count,
        )

    if deny_all_entries:
        if valid:
            # deny-all mixed with valid names — drop deny-all, use valid names
            logger.warning(
                "_acl in %s contains 'deny-all' mixed with valid namespaces; "
                "'deny-all' dropped, using valid namespaces only",
                doc_path,
            )
            return valid
        else:
            # deny-all with no valid names
            if non_deny_all:
                # deny-all mixed with only invalid names — fail-open (not deny-all)
                logger.warning(
                    "_acl in %s contains 'deny-all' mixed with invalid names; "
                    "ambiguous — chunk defaults to open",
                    doc_path,
                )
                return None
            else:
                # deny-all is the sole kind of entry — interpret as deny-all
                logger.warning(
                    "_acl in %s contains the reserved word 'deny-all' as a namespace name; "
                    "interpreting as deny-all (acl: [])",
                    doc_path,
                )
                return []

    # No deny-all entries
    if valid:
        return valid

    # No valid names at all — fail-open
    return None


def read_acl_sidecar(doc_path: Path) -> list[str] | None:
    """Read ACL from a sidecar file (<doc_path>.acl).

    Returns:
        - None: no sidecar, empty sidecar, or unreadable (fail-open)
        - []: deny-all sentinel found
        - [str, ...]: list of valid namespace names
    """
    sidecar = doc_path.parent / (doc_path.name + ".acl")

    if not sidecar.exists():
        return None

    if sidecar.is_symlink():
        logger.warning("ACL sidecar %s is a symlink; ignoring", sidecar)
        return None

    raw_bytes = sidecar.read_bytes()
    if len(raw_bytes) > _ACL_SIDECAR_MAX_BYTES:
        logger.warning(
            "ACL sidecar %s exceeds %d bytes; ignoring",
            sidecar,
            _ACL_SIDECAR_MAX_BYTES,
        )
        return None

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("ACL sidecar %s is not valid UTF-8; ignoring", sidecar)
        return None

    # Strip UTF-8 BOM if present
    text = text.lstrip("﻿")

    lines = [line.strip() for line in text.splitlines()]
    non_empty = [line for line in lines if line]

    if not non_empty:
        return None

    first = non_empty[0]
    if first.upper() == "DENY-ALL":
        if len(non_empty) > 1:
            logger.warning(
                "ACL sidecar %s has content after 'deny-all' sentinel; extra lines ignored",
                sidecar,
            )
        return []

    valid: list[str] = []
    for line in non_empty:
        if is_acl_namespace_valid(line):
            valid.append(line)
        else:
            logger.warning(
                "ACL sidecar %s: invalid namespace name %r (dropped)", sidecar, line
            )

    return valid if valid else None


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


def resolve_acl(doc_path: Path, front_matter_acl: Any) -> list[str] | None:
    """Resolve the effective ACL for a document.

    Precedence: front-matter _acl key > sidecar file.

    Args:
        doc_path: path to the document.
        front_matter_acl: value of the _acl key from front-matter, or None if
            the key was absent.

    Returns:
        - None: fail-open (no ACL restriction)
        - []: deny-all
        - [str, ...]: allowed namespace names
    """
    sidecar = doc_path.parent / (doc_path.name + ".acl")

    if front_matter_acl is not None:
        if sidecar.exists():
            logger.warning(
                "Both front-matter _acl and sidecar %s exist for %s; "
                "front-matter takes precedence",
                sidecar,
                doc_path,
            )
        return parse_acl_value(front_matter_acl, str(doc_path))

    return read_acl_sidecar(doc_path)
