"""ACL parsing utilities for archon-search chunk-level access control."""

import logging
import re
from typing import Any

from archon_search.constants import _NAMESPACE_RE

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
