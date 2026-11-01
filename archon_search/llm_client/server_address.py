"""The address of a model server as a log line or an operator message may name it."""

from __future__ import annotations

import ipaddress
import re
from typing import Final, NamedTuple
from urllib.parse import urlsplit

#: Stands in for the address when the configured URL does not parse. Every unparsable value shares it, so they are one
#: label: no digest of a value that may carry credentials is ever taken.
UNKNOWN_HOST: Final = "the configured host"
#: The longest host a label names (RFC 1035: a name of at most 253 characters), so a label stays short however long a value is.
MAX_HOST_LENGTH: Final = 253
_MIN_PORT: Final = 1

_DEFAULT_PORTS: Final = {"http": 80, "https": 443}
#: A value that starts with ``//`` or ``scheme://`` is a URL; anything else (``localhost:8080/v1``) is an authority and path.
_URL_PREFIX: Final = re.compile(r"([a-z][a-z0-9+.-]*:)?//", re.IGNORECASE)
#: A DNS name as a log line may carry it: dot-separated labels of 1 to 63 ASCII letters, digits, ``-`` and ``_`` (a compose
#: service name has underscores), not starting or ending a label with ``-``. ``urlsplit`` has lower-cased the host already.
_HOSTNAME: Final = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?(?:\.[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?)*")


class ServerAddress(NamedTuple):
    """The host (lower-case, no trailing dot, an IPv6 address without brackets) and port of a model server."""

    host: str
    port: int | None

    @property
    def label(self) -> str:
        """``host:port`` (``host`` alone without a port; an IPv6 host in brackets): the name a log line gives the server."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return host if self.port is None else f"{host}:{self.port}"


def is_url(value: str) -> bool:
    """Whether *value* is a URL (it starts with ``//`` or ``scheme://``) and not an authority and path (``localhost:8080/v1``).

    The one answer to that question for everything that reads a configured server address: a ``//`` later in the path
    (``localhost:11434/a//b``) does not make a URL of an authority.
    """
    return _URL_PREFIX.match(value.strip()) is not None


def parse_server_address(value: str) -> ServerAddress | None:
    """The one parser of a configured server URL: host and port, never the authority, so no userinfo reaches a caller.

    Accepts a URL or a scheme-less ``host:port`` (``//`` is prepended, because ``urlsplit`` reads ``localhost:11434`` as
    scheme ``localhost``); surrounding whitespace is stripped once, here. The port is the written one, else the default of
    ``http`` (80) or ``https`` (443), else ``None``. ``None`` when the value has no usable host: it does not parse (a bad
    port, an unclosed bracket), has no host, has an ``@`` outside its authority, contains a character that is not printable
    (a control character, U+2028, U+202E), or names a host that is not a plain one: only an ASCII DNS name of at most
    ``MAX_HOST_LENGTH`` characters or an IP literal without a zone id (``[::1%25secret]``) is named, and a port is 1 to 65535.
    So the label built from the result is short and printable whatever the value holds.

    The ``@`` rule is deliberate and fails safe (an accepted limitation): ``u:p/s@h`` ends the authority at the ``/``, so
    the real userinfo may sit in the path, query or fragment and what parses as host and port (``u:p``) may be part of a
    credential. A value that is an ordinary URL with a legal ``@`` in its path (``http://localhost:8080/v1/@x``) is refused
    with it, because the two cannot be told apart without guessing which client reads the value how (``localhost/x@evil.test``
    reaches ``localhost`` for one and ``evil.test`` for another); a refused value is labelled ``UNKNOWN_HOST`` and is not
    loopback (see ``is_loopback_host``), the cautious answers. The cost: two different servers that both write an ``@`` after
    the authority (``http://a.test:1/v1/@x`` and ``http://b.test:2/v1/@y``) share the one label ``UNKNOWN_HOST``, so the startup
    probe asks only the first and the second takes its verdict.
    """
    text = value.strip()
    if not text.isprintable():
        return None
    try:
        parts = urlsplit(text if is_url(text) else f"//{text}")
        host, port = parts.hostname, parts.port
    except ValueError:  # an unclosed bracket, a port that is not a number in range
        return None
    if "@" in f"{parts.path}{parts.query}{parts.fragment}" or (port is not None and port < _MIN_PORT):
        return None
    host = (host or "").removesuffix(".")
    if not _is_plain_host(host):
        return None
    return ServerAddress(host, port if port is not None else _DEFAULT_PORTS.get(parts.scheme.lower()))


def _is_plain_host(host: str) -> bool:
    """Whether *host* is a name or an IP literal that is short and printable: an ASCII DNS name or an IPv6 address without zone id."""
    if not host or len(host) > MAX_HOST_LENGTH:
        return False
    if ":" not in host:
        return _HOSTNAME.fullmatch(host) is not None
    try:
        return ipaddress.IPv6Address(host).scope_id is None
    except ValueError:
        return False


def host_port(base_url: str) -> str:
    """``host:port`` of *base_url*: the label a log line names a server by, and the key of every WARNING kept per server.

    Built only from :func:`parse_server_address`: the host is lower-cased with a trailing dot dropped, an IPv6 address stays
    in brackets, and the default port of ``http`` (80) or ``https`` (443) is filled in. A value without a scheme
    (``localhost:8080``) keeps its own port, or none. A value without a usable host (see ``parse_server_address``, e.g.
    userinfo that breaks the authority such as ``u:p/s@h``, or any ``@`` after the authority) is ``UNKNOWN_HOST``.

    The identity of a server is its ``host:port`` and nothing else: a path routes nothing here, so ``http://gw/a`` and
    ``http://gw/b`` behind one path-routed gateway are one server and share one WARNING and one probe verdict. Spellings
    that give the same label are one server (``LOCALHOST:8080`` and ``localhost:8080``; ``http://localhost`` and
    ``http://localhost:80``). Different names of one machine (``localhost``, ``127.0.0.1``, ``[::1]``) are different
    labels, hence different servers; all unparsable values are one label (``UNKNOWN_HOST``), so two different broken URLs
    are one server too.
    """
    address = parse_server_address(base_url)
    return UNKNOWN_HOST if address is None else address.label


def display_url(value: str) -> str:
    """*value* as a prompt or an operator message may show it: scheme, host, port and path, never the userinfo, the query or
    the fragment (``http://u:p@h:1/v1/?t=k`` shows as ``http://h:1/v1``; a value without a scheme shows without one).

    ``UNKNOWN_HOST`` when :func:`parse_server_address` refuses the value (a hostile host, or an ``@`` after the authority,
    where the path may hold the credential), and ``""`` for an empty value, which is no address to name. Only what is shown
    changes: the caller keeps and stores the value itself.
    """
    text = value.strip()
    if not text:
        return ""
    if parse_server_address(text) is None:
        return UNKNOWN_HOST
    parts = urlsplit(text if is_url(text) else f"//{text}")
    host = parts.hostname or ""  # not empty: the parser accepted the value
    host = f"[{host}]" if ":" in host else host
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    shown = f"{netloc}{parts.path.rstrip('/')}"
    if not is_url(text):
        return shown
    return f"{parts.scheme}://{shown}" if parts.scheme else f"//{shown}"
