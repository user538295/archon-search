"""``host_port``: the one label a model server goes by in a log line, shared by every WARNING that is keyed per server."""

from __future__ import annotations

import pytest

from archon_search.llm_client.server_address import (
    UNKNOWN_HOST,
    ServerAddress,
    display_url,
    host_port,
    is_url,
    parse_server_address,
)


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        (" http://localhost:8080", "localhost:8080"),
        ("\thttps://llama.test/v1\n", "llama.test:443"),
        ("  localhost:11434  ", "localhost:11434"),
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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://h:1", True),
        ("HTTPS://h", True),
        ("//h:1", True),
        ("ftp://files.test/pub", True),
        ("  http://h:1  ", True),
        ("h:1", False),
        ("localhost:11434/a//b", False),  # a ``//`` inside the path does not make a URL of an authority
        ("user:secret@h:1", False),
        ("[::1]:8080", False),
        ("", False),
    ],
)
def test_is_url_tells_a_url_from_an_authority_and_path(value: str, expected: bool) -> None:
    assert is_url(value) is expected


@pytest.mark.parametrize("value", [" http://localhost:8080", "http://localhost:8080 ", "\thttp://localhost:8080\n"])
def test_parse_server_address_strips_the_value_once(value: str) -> None:
    assert parse_server_address(value) == ServerAddress("localhost", 8080)


@pytest.mark.parametrize("value", ["http://localhost:8080/v1/@x", "http://localhost:8080/?mail=a@b", "localhost:8080#a@b"])
def test_an_at_sign_after_the_authority_is_refused_even_where_it_is_legal(value: str) -> None:
    """Deliberate fail-safe: ``localhost/x@evil.test`` and ``localhost:8080/v1/@x`` cannot be told apart without guessing which
    client reads them how, so neither is parsed: the label is the unknown host."""
    assert parse_server_address(value) is None
    assert host_port(value) == UNKNOWN_HOST


def test_one_path_routed_gateway_is_one_server() -> None:
    """The identity of a server is its ``host:port``: two paths behind one gateway share one label, hence one WARNING."""
    assert host_port("http://gw.test:8080/llama-a/v1") == host_port("http://gw.test:8080/llama-b/v1") == "gw.test:8080"


def test_two_different_refused_values_are_one_label() -> None:
    assert host_port("http://localhost:8080/v1/@x") == host_port("http://other.test:9090/v1/@y") == UNKNOWN_HOST


def test_two_different_servers_with_an_at_sign_after_the_authority_share_one_label() -> None:
    """The accepted limitation the documentation states: the startup probe asks the first of them and the second takes its verdict."""
    assert host_port("http://a.test:1/v1/@x") == host_port("http://b.test:2/v1/@y") == UNKNOWN_HOST


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        ("http://localhost:11434", "http://localhost:11434"),
        ("http://localhost:11434/", "http://localhost:11434"),
        ("  http://127.0.0.1:9  ", "http://127.0.0.1:9"),
        ("http://u:p@h.test:1/v1/?t=k#f", "http://h.test:1/v1"),
        ("https://h.test", "https://h.test"),
        ("//h.test:1/x?t=k", "//h.test:1/x"),
        ("h.test:1/x?t=k", "h.test:1/x"),
        ("u:p@h.test", "h.test"),
        ("http://[::1]:8080/v1", "http://[::1]:8080/v1"),
        ("http://u:p@[fe80::1]", "http://[fe80::1]"),
        ("HTTP://LocalHost:80/", "http://localhost:80"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_display_url_keeps_scheme_host_port_and_path_only(value: str, shown: str) -> None:
    assert display_url(value) == shown


@pytest.mark.parametrize(
    "value",
    ["u:12345/x@h.test", "http://h.test/svc:pw@x", "http://h.test/?a=b@c", "http://[::1", "http://h.test:99999", "http://h\x1b.test", "http://[::1%25lo]"],
)
def test_display_url_names_a_value_the_parser_refuses_as_the_unknown_host(value: str) -> None:
    assert display_url(value) == UNKNOWN_HOST
