"""Query fingerprints in logs are HMACs keyed by the telemetry salt, not bare sha256 (SecReview #9c)."""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
from pathlib import Path

import pytest

from archon_search import _privacy
from archon_search._privacy import _query_fingerprint, configure_query_fingerprint_salt

_SALT_A = b"a" * 32
_SALT_B = b"b" * 32
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{16}$")


@pytest.fixture(autouse=True)
def _restore_fingerprint_key():
    saved = _privacy._fingerprint_key
    yield
    _privacy._fingerprint_key = saved


def test_fingerprint_is_not_the_unsalted_sha256_of_the_query() -> None:
    configure_query_fingerprint_salt(_SALT_A)

    assert _query_fingerprint("password") != hashlib.sha256(b"password").hexdigest()[:16]


def test_fingerprint_is_the_truncated_hmac_of_the_query_under_the_salt() -> None:
    configure_query_fingerprint_salt(_SALT_A)

    expected = hmac.digest(_SALT_A, b"how do I rotate keys", "sha256").hex()[:16]
    assert _query_fingerprint("how do I rotate keys") == expected
    assert _FINGERPRINT_RE.match(expected)


def test_fingerprint_is_deterministic_for_one_salt() -> None:
    configure_query_fingerprint_salt(_SALT_A)

    assert _query_fingerprint("same query") == _query_fingerprint("same query")
    assert _query_fingerprint("same query") != _query_fingerprint("another query")


def test_fingerprint_differs_across_salts() -> None:
    configure_query_fingerprint_salt(_SALT_A)
    under_a = _query_fingerprint("same query")
    configure_query_fingerprint_salt(_SALT_B)

    assert _query_fingerprint("same query") != under_a


def test_without_a_salt_fingerprints_use_a_random_per_process_key() -> None:
    configure_query_fingerprint_salt(None)
    first = _query_fingerprint("same query")
    assert _FINGERPRINT_RE.match(first)
    assert first != hashlib.sha256(b"same query").hexdigest()[:16]
    assert first == _query_fingerprint("same query")

    configure_query_fingerprint_salt(None)

    assert _query_fingerprint("same query") != first, "each unconfigured start draws a fresh key"


def test_fingerprint_never_contains_the_query() -> None:
    configure_query_fingerprint_salt(_SALT_A)
    query = "aaaaaaaaaaaaaaaa"

    assert query not in _query_fingerprint(query)


def test_the_telemetry_salt_loaded_at_startup_keys_the_fingerprints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The lifespan hands the telemetry salt to the fingerprint: same data dir, same fingerprint after a restart."""
    from tests.integration.conftest import make_real_app

    fingerprints: list[str] = []
    salts: list[bytes | None] = []
    for _ in range(2):
        with make_real_app(tmp_path, monkeypatch, telemetry_enabled=True, hash_doc_ids_enabled=True) as (client, _cfg, _key):
            salts.append(client.app.state.salt_bytes)
            fingerprints.append(_query_fingerprint("restart-stable query"))

    assert salts[0] is not None and salts[0] == salts[1]
    assert fingerprints[0] == fingerprints[1]
    assert fingerprints[0] == hmac.digest(salts[0], b"restart-stable query", "sha256").hex()[:16]


def test_logged_fingerprints_do_not_reveal_the_query(caplog: pytest.LogCaptureFixture) -> None:
    """The graph expander logs only the fingerprint; with a salt it cannot be dictionary-checked."""
    configure_query_fingerprint_salt(_SALT_A)
    logger = logging.getLogger("archon_search.test_privacy")

    with caplog.at_level(logging.INFO, logger=logger.name):
        logger.info("q=%s", _query_fingerprint("password"))

    line = caplog.records[0].getMessage()
    assert "password" not in line
    assert hashlib.sha256(b"password").hexdigest()[:16] not in line
