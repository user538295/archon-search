"""Unit tests for the graph viewer's short-lived view tokens and the access-log credential filter (SecReview #8)."""
from __future__ import annotations

import logging

import pytest

from archon_search.constants import GRAPH_VIEW_TOKEN_TTL_SECONDS
from archon_search.logging_setup import REDACTED_VALUE, QueryCredentialRedactionFilter
from archon_search.server.graph_view_token import (
    GRAPH_VIEW_PATH_RE,
    GRAPH_VIEW_SECRET_BYTES,
    mint_view_token,
    verify_view_token,
)

_SECRET = b"s" * GRAPH_VIEW_SECRET_BYTES
_NOW = 1_700_000_000.0


def _mint(namespace: str = "ns-a", collection: str = "docs", *, now: float = _NOW) -> str:
    return mint_view_token(_SECRET, namespace, collection, now=now)


def test_a_minted_token_verifies_for_its_collection_and_returns_its_namespace() -> None:
    assert verify_view_token(_SECRET, _mint(), "docs", now=_NOW) == "ns-a"


def test_the_token_is_url_safe() -> None:
    token = _mint()
    assert token.startswith("gv1.")
    assert all(ch.isalnum() or ch in "._-" for ch in token)


def test_a_token_is_bound_to_one_collection() -> None:
    assert verify_view_token(_SECRET, _mint(collection="docs"), "other", now=_NOW) is None


def test_a_token_expires_after_the_documented_lifetime() -> None:
    token = _mint()

    assert verify_view_token(_SECRET, token, "docs", now=_NOW + GRAPH_VIEW_TOKEN_TTL_SECONDS - 1) == "ns-a"
    assert verify_view_token(_SECRET, token, "docs", now=_NOW + GRAPH_VIEW_TOKEN_TTL_SECONDS) is None


def test_a_token_from_another_secret_is_rejected() -> None:
    assert verify_view_token(b"x" * GRAPH_VIEW_SECRET_BYTES, _mint(), "docs", now=_NOW) is None


def test_a_tampered_namespace_is_rejected() -> None:
    import base64

    prefix, body, signature = _mint("ns-a").split(".")
    payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).replace(b"ns-a", b"ns-b")
    forged = f"{prefix}.{base64.urlsafe_b64encode(payload).decode().rstrip('=')}.{signature}"

    assert verify_view_token(_SECRET, forged, "docs", now=_NOW) is None


@pytest.mark.parametrize(
    "token",
    ["", "gv1", "gv1.x", "gv1.x.y.z", "xx1.AAAA.00", "gv1.!!!.00", "gv1.é.00", "0" * 64],
    ids=["empty", "prefix-only", "two-parts", "four-parts", "wrong-prefix", "bad-base64", "non-ascii", "api-key-shaped"],
)
def test_malformed_tokens_are_rejected_without_raising(token: str) -> None:
    assert verify_view_token(_SECRET, token, "docs", now=_NOW) is None


@pytest.mark.parametrize(
    ("path", "collection"),
    [("/graph/docs", "docs"), ("/graph/docs/view", "docs"), ("/graph/a-b_1/view", "a-b_1")],
)
def test_the_view_token_paths_are_the_viewer_and_its_data_only(path: str, collection: str) -> None:
    match = GRAPH_VIEW_PATH_RE.match(path)

    assert match is not None and match.group("collection") == collection


@pytest.mark.parametrize(
    "path",
    [
        "/graph/cross-collection",
        "/graph/cross-collection/view",
        "/graph/docs/impact/Foo",
        "/graph/docs/rebuild-communities",
        "/graph/docs/view-token",
        "/graph/docs/view/",
        "/graph/",
        "/search",
        "/keys",
    ],
)
def test_no_other_path_accepts_a_view_token(path: str) -> None:
    assert GRAPH_VIEW_PATH_RE.match(path) is None


# ---------------------------------------------------------------------------
# Access-log credential redaction
# ---------------------------------------------------------------------------


def _uvicorn_access_record(target: str) -> logging.LogRecord:
    """A record shaped like uvicorn's ``uvicorn.access`` line: (client, method, full_path, http_version, status)."""
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0, '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:5000", "GET", target, "1.1", 200), None
    )


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("/graph/docs/view?view_token=gv1.abc.def", f"/graph/docs/view?view_token={REDACTED_VALUE}"),
        ("/graph/docs/view?a=1&view_token=gv1.abc.def&b=2", f"/graph/docs/view?a=1&view_token={REDACTED_VALUE}&b=2"),
        ("/x?token=secret", f"/x?token={REDACTED_VALUE}"),
        ("/x?api_key=secret&y=1", f"/x?api_key={REDACTED_VALUE}&y=1"),
        ("/x?access_token=secret", f"/x?access_token={REDACTED_VALUE}"),
        ("/search", "/search"),
        ("/x?limit=5&notatoken=1", "/x?limit=5&notatoken=1"),
    ],
)
def test_access_log_target_never_carries_a_credential_value(target: str, expected: str) -> None:
    record = _uvicorn_access_record(target)

    assert QueryCredentialRedactionFilter().filter(record) is True

    line = record.getMessage()
    assert expected in line
    assert "gv1.abc" not in line and "secret" not in line


def test_the_filter_leaves_a_record_without_tuple_args_alone() -> None:
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 0, "plain message", None, None)

    assert QueryCredentialRedactionFilter().filter(record) is True
    assert record.getMessage() == "plain message"


def test_configure_logging_installs_the_filter_once_on_the_uvicorn_access_logger() -> None:
    from archon_search.config import SearchConfig
    from archon_search.logging_setup import configure_logging

    access_logger = logging.getLogger("uvicorn.access")
    before = list(access_logger.filters)
    try:
        configure_logging(SearchConfig())
        configure_logging(SearchConfig())
        installed = [f for f in access_logger.filters if isinstance(f, QueryCredentialRedactionFilter)]
        assert len(installed) == 1
    finally:
        access_logger.filters[:] = before
