"""``host_port``: the one label a model server goes by in a log line, shared by every WARNING that is keyed per server."""

from __future__ import annotations

import pytest

from archon_search.llm_client.server_address import (
    UNKNOWN_HOST,
    ServerAddress,
    host_port,
    parse_server_address,
)


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("http://user:secret@gpu-box.internal:11434/api?token=x", "gpu-box.internal:11434"),
        ("http://localhost:8080", "localhost:8080"),
        ("localhost:11434", "localhost:11434"),
        ("http://[::1]:8080/v1", "[::1]:8080"),
        ("[::1]:8080", "[::1]:8080"),
        ("LOCALHOST:8080", "localhost:8080"),
        ("http://LocalHost:8080", "localhost:8080"),
        ("http://llama.test.:8080", "llama.test:8080"),
        ("http://localhost", "localhost:80"),
        ("HTTP://localhost/v1", "localhost:80"),
        ("https://llama.test/v1", "llama.test:443"),
        ("https://llama.test:8443", "llama.test:8443"),
        ("localhost", "localhost"),
        ("localhost/v1", "localhost"),
        ("localhost:8080/v1//models", "localhost:8080"),
        ("//localhost:8080", "localhost:8080"),
        ("http://[::1]", "[::1]:80"),
    ],
)
def test_host_port_names_host_and_port_only(base_url: str, expected: str) -> None:
    assert host_port(base_url) == expected


@pytest.mark.parametrize(
    "base_url",
    ["http://[::1", "", "http://", "http://:8080", "http://host:notaport", "http://host:99999", "http://.:80", "http://host:-1"],
)
def test_host_port_of_a_value_that_does_not_parse_is_the_unknown_host(base_url: str) -> None:
    assert host_port(base_url) == UNKNOWN_HOST


@pytest.mark.parametrize(
    "base_url",
    [
        "http://user:pa/ss@h.test:8080",
        "http://user:pa?ss@h.test:8080",
        "http://user:pa#ss@h.test:8080",
        "user:pa/ss@h.test:8080",
        "http://user:pa%2Fss@h.test:8080",
        "http://user:12345/x@h.test:80",
        "http://user/x@h.test:80",
        "http://tok?en@h.test:8080",
        "http://tok#en@h.test:8080",
        "tok/en@h.test:80",
        "http://h.test:8080/a@b",
        "http://h.test:8080?mail=a@b",
    ],
)
def test_host_port_never_carries_a_password_whatever_the_userinfo_holds(base_url: str) -> None:
    label = host_port(base_url)

    assert "pa" not in label.replace("h.test", "")
    assert "ss" not in label.replace("h.test", "")
    assert label in {"h.test:8080", UNKNOWN_HOST}


@pytest.mark.parametrize(
    "spellings",
    [
        ["http://llama.test:8080", "http://llama.test:8080/v1/", "https://u:p@llama.test:8080/?x=1", "llama.test:8080"],
        ["LOCALHOST:8080", "localhost:8080", "http://localhost.:8080/", "http://LOCALHOST:8080/v1"],
        ["http://localhost", "http://localhost:80", "http://localhost/v1", "HTTP://LocalHost:80"],
        ["https://llama.test", "https://llama.test:443/"],
    ],
)
def test_spellings_that_normalise_to_one_label_are_one_server(spellings: list[str]) -> None:
    assert len({host_port(spelling) for spelling in spellings}) == 1


def test_different_names_of_one_machine_are_different_servers() -> None:
    labels = {host_port(f"http://{name}:8080") for name in ("localhost", "127.0.0.1", "[::1]")}

    assert len(labels) == 3


def test_every_unparsable_value_shares_the_unknown_host() -> None:
    assert {host_port(value) for value in ("http://[::1", "http://a:b", "")} == {UNKNOWN_HOST}


def test_a_scheme_default_port_is_not_filled_for_an_unknown_scheme() -> None:
    assert host_port("ftp://files.test/pub") == "files.test"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://u:p@Gpu.Test.:11434/x", ServerAddress("gpu.test", 11434)),
        ("http://localhost", ServerAddress("localhost", 80)),
        ("localhost:11434/a//b", ServerAddress("localhost", 11434)),
        ("[::1]:8080", ServerAddress("::1", 8080)),
        ("localhost", ServerAddress("localhost", None)),
        ("ftp://files.test/pub", ServerAddress("files.test", None)),
    ],
)
def test_parse_server_address_is_the_one_parser_of_host_and_port(value: str, expected: ServerAddress) -> None:
    assert parse_server_address(value) == expected


@pytest.mark.parametrize("value", ["http://[::1", "", "http://user/x@h.test:80", "http://:80"])
def test_parse_server_address_is_none_for_a_value_without_a_usable_host(value: str) -> None:
    assert parse_server_address(value) is None
