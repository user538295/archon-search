"""``host_port``: the one label a model server goes by in a log line, shared by every WARNING that is keyed per server."""

from __future__ import annotations

import pytest

from archon_search.llm_client.server_address import UNKNOWN_HOST, host_port


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("http://user:secret@gpu-box.internal:11434/api?token=x", "gpu-box.internal:11434"),
        ("http://localhost:8080", "localhost:8080"),
        ("localhost:11434", "localhost:11434"),
        ("http://[::1]:8080/v1", "[::1]:8080"),
        ("http://[::1", UNKNOWN_HOST),
        ("", UNKNOWN_HOST),
    ],
)
def test_host_port_names_host_and_port_only(base_url: str, expected: str) -> None:
    assert host_port(base_url) == expected


def test_every_spelling_of_one_server_has_one_label() -> None:
    spellings = ["http://llama.test:8080", "http://llama.test:8080/v1/", "https://u:p@llama.test:8080/?x=1", "llama.test:8080"]

    assert {host_port(spelling) for spelling in spellings} == {"llama.test:8080"}
