"""Whatever an operator (or a hostile config file) writes as a model-server URL, the label a log line names the server by is
``UNKNOWN_HOST`` or a short printable ``host:port``: no control character, no zone id, no userinfo, path or query, and it never raises.

The label is built from the host alone, so a value that smuggles a control character (log injection), a secret in an IPv6 zone
id, or thousands of characters into the host must not reach a log line through it.
"""

from __future__ import annotations

import itertools
import re

import pytest

from archon_search.llm_client.server_address import (
    MAX_HOST_LENGTH,
    UNKNOWN_HOST,
    host_port,
    parse_server_address,
)
from archon_search.server.description_backfill import probed_server_label
from archon_search.server.description_startup import is_loopback_host

_SECRET = "SECRETpw"
_MAX_PORT = 65535
_PROVIDERS = ("llama_cpp", "ollama")

# What a label may look like: a hostname of ASCII letters, digits, ``-`` and ``_`` between dots, or a bracketed IPv6 literal
# with no zone id, then an optional port. Nothing else is printable enough for a log line.
_LABEL = re.compile(r"(?:[a-z0-9_-]+(?:\.[a-z0-9_-]+)*|\[[0-9a-f:.]+\])(?::\d{1,5})?", re.ASCII)

# One hostile fragment each; the value templates below put every one of them in every position of a URL.
_HOSTILE_FRAGMENTS = (
    "\x1b",
    "\x00",
    "\x07",
    "\x7f",
    "\t",
    "\n",
    "\r",
    "\u2028",
    "\u2029",
    "\u202e",
    "\u200b",
    "\u3002",
    "\uff0e",
    "\uff61",
    "\\",
    "%",
    "%25",
    "%2e",
    "é",
    "ｌｏｃａｌｈｏｓｔ",
    " ",
    "..",
    "[",
    "]",
    "@",
    "#",
    "?",
    "*",
)
_TEMPLATES = (
    "http://{f}h.test:8080/v1",
    "http://h{f}.test:8080/v1",
    "http://h.test{f}:8080/v1",
    "http://h.test:8080{f}/v1",
    "http://[::1{f}]:8080",
    "localhost{f}:8080",
    "h.test:80{f}",
)
_HOSTILE_VALUES = tuple(template.format(f=fragment) for template, fragment in itertools.product(_TEMPLATES, _HOSTILE_FRAGMENTS))
_OTHER_HOSTILE_VALUES = (
    f"http://[::1%25{_SECRET}]:8080",
    f"http://[fe80::1%25{_SECRET}]/v1",
    f"[::1%25{_SECRET}]:8080",
    f"http://[::1%{_SECRET}]:8080",
    "http://[::1%25lo]:8080",
    "[::1%25lo]",
    "http://" + "a" * 5000 + ":8080",
    "http://" + "a" * (MAX_HOST_LENGTH + 1) + ".test",
    "http://" + ".".join(["a" * 63] * 5),
    "http://" + "a" * 64 + ".test",
    "http://[" + "1:" * 4000 + "1]",
    "http://h.test:0",
    "http://h.test:65536",
    "http://h.test:" + "9" * 5000,
    "http://h.test:８０８０",
    "http://h.test:+80",
    "http://-h.test",
    "http://h-.test",
    "http://a..test",
    "http://" + "\x1b[31m" + "h.test",
    "\x1b]0;title\x07",
    "http://h.test/\x1b[2J",
    "x" * 5000,
    "\u202ehttp://h.test",
)
_VALUES = (*_HOSTILE_VALUES, *_OTHER_HOSTILE_VALUES)


def test_the_corpus_of_hostile_values_is_at_least_two_hundred_spellings() -> None:
    assert len(set(_VALUES)) >= 200


@pytest.mark.parametrize("value", _VALUES, ids=lambda value: ascii(value)[:60])
def test_no_hostile_value_puts_more_than_a_short_printable_host_and_port_in_a_label(value: str) -> None:
    labels = {host_port(value), *(probed_server_label(provider, value) for provider in _PROVIDERS)}
    parse_server_address(value)
    is_loopback_host(value)

    for label in labels:
        assert label == UNKNOWN_HOST or (
            _LABEL.fullmatch(label) is not None and len(label) <= MAX_HOST_LENGTH + len(":65535") and _port_is_in_range(label)
        ), ascii(label)
        assert _SECRET.lower() not in label.lower()


def _port_is_in_range(label: str) -> bool:
    port = label.rpartition(":")[2] if re.search(r":\d+$", label) else None
    return port is None or 1 <= int(port) <= _MAX_PORT


@pytest.mark.parametrize(
    "value",
    [
        "http://[::1%25lo]:8080",
        "[::1%25lo]",
        "http://[::1%lo]:8080",
        f"http://[::1%25{_SECRET}]:8080",
        "::1%lo",
        "http://[::ffff:127.0.0.1%25lo]:8080",
    ],
)
def test_a_zone_id_loopback_is_neither_a_label_nor_a_loopback(value: str) -> None:
    """A zone id may carry anything, and ``urlsplit`` hands it to ``ipaddress`` as part of the host: the value is refused whole."""
    assert host_port(value) == UNKNOWN_HOST
    assert probed_server_label("ollama", value) == UNKNOWN_HOST
    assert parse_server_address(value) is None
    assert is_loopback_host(value) is False


def test_a_host_of_the_longest_legal_length_is_named_and_one_character_more_is_not() -> None:
    longest = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 61])

    assert len(longest) == MAX_HOST_LENGTH
    assert host_port(f"http://{longest}:8080") == f"{longest}:8080"
    assert host_port(f"http://{longest}a:8080") == UNKNOWN_HOST


@pytest.mark.parametrize("value", ["http://h.test:0", "h.test:0", "http://h.test:65536"])
def test_a_port_outside_1_to_65535_is_not_a_server(value: str) -> None:
    assert host_port(value) == UNKNOWN_HOST


@pytest.mark.parametrize(
    ("value", "label"),
    [
        ("http://my_service:8080", "my_service:8080"),
        ("http://xn--bcher-kva.example:8080", "xn--bcher-kva.example:8080"),
        ("http://127.0.0.1:1", "127.0.0.1:1"),
        ("http://[fe80::1]:65535", "[fe80::1]:65535"),
        ("http://[::ffff:127.0.0.1]:11434", "[::ffff:127.0.0.1]:11434"),
    ],
)
def test_an_ordinary_host_keeps_its_label(value: str, label: str) -> None:
    assert host_port(value) == label
