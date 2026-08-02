"""S168 regression: docs must give GET /ready checks.storage/checks.models values
in the lowercase form the server actually emits.

The wire format is lowercase (``ok``/``warn``/``fail``/``pending``) — the
``CheckStatus`` enum values below and the literal JSON body at
``Documentation/OperatorGuide/20_monitoring_and_alerts.md:23`` are authoritative.
Two user-facing pages previously showed the values in UPPERCASE, producing tests
that asserted ``"OK"`` and failed against the correct ``"ok"``. These tests pin
the docs to the wire format.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOCS = _REPO_ROOT / "Documentation"

# Exact check-status value tokens (word-bounded so job statuses like FAILED are
# not matched). Only the four CheckStatus values.
_UPPER_TOKENS = ("OK", "WARN", "FAIL", "PENDING")

# Backtick-wrapped (`OK`) or double-quote-wrapped ("OK") uppercase literals — the
# forms that read as the literal wire value. Bold prose emphasis (**FAIL**) is
# intentionally NOT matched: it is allowed as a state-name in priority prose.
_LITERAL_UPPER = re.compile(
    r"`(?:" + "|".join(_UPPER_TOKENS) + r")`"
    r'|"(?:' + "|".join(_UPPER_TOKENS) + r')"'
)

# User-facing pages that describe checks.storage / checks.models values.
_USER_FACING_DOCS = (
    _DOCS / "OperatorGuide" / "90_incident_runbook.md",
    _DOCS / "UserManual" / "160_troubleshooting.md",
)


def test_ready_check_status_wire_values_are_lowercase():
    """The CheckStatus enum (what /ready serialises) uses lowercase values."""
    from archon_search.server.schemas import CheckStatus

    values = {member.value for member in CheckStatus}
    assert values == {"ok", "fail", "pending", "warn"}
    for value in values:
        assert value == value.lower(), f"{value!r} is not lowercase"


def test_user_docs_use_lowercase_check_status_literals():
    """No user-facing doc may present a check-status value as an UPPERCASE literal.

    Scans for backtick- or quote-wrapped OK/WARN/FAIL/PENDING — the forms that
    read as the literal wire value the server returns. Bold prose emphasis
    (``**FAIL**``) is allowed and not matched.
    """
    offenders: list[str] = []
    for doc in _USER_FACING_DOCS:
        assert doc.exists(), f"missing doc: {doc}"
        for lineno, line in enumerate(doc.read_text().splitlines(), start=1):
            for match in _LITERAL_UPPER.finditer(line):
                offenders.append(f"{doc.relative_to(_REPO_ROOT)}:{lineno}: {match.group(0)}")
    assert not offenders, (
        "Docs present check-status values as UPPERCASE literals; the server emits "
        "lowercase (ok/warn/fail/pending):\n" + "\n".join(offenders)
    )
