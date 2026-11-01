"""The address of a model server as a log line or an operator message may name it."""

from __future__ import annotations

import re
from typing import Final, NamedTuple
from urllib.parse import urlsplit

#: Stands in for the address when the configured URL does not parse. Every unparsable value shares it, so they are one
#: label: no digest of a value that may carry credentials is ever taken.
UNKNOWN_HOST: Final = "the configured host"

_DEFAULT_PORTS: Final = {"http": 80, "https": 443}
#: A value that starts with ``//`` or ``scheme://`` is a URL; anything else (``localhost:8080/v1``) is an authority and path.
_URL_PREFIX: Final = re.compile(r"([a-z][a-z0-9+.-]*:)?//", re.IGNORECASE)


class ServerAddress(NamedTuple):
    """The host (lower-case, no trailing dot, an IPv6 address without brackets) and port of a model server."""

    host: str
    port: int | None


def parse_server_address(value: str) -> ServerAddress | None:
    """The one parser of a configured server URL: host and port, never the authority, so no userinfo reaches a caller.

    Accepts a URL or a scheme-less ``host:port`` (``//`` is prepended, because ``urlsplit`` reads ``localhost:11434`` as
    scheme ``localhost``). The port is the written one, else the default of ``http`` (80) or ``https`` (443), else
    ``None``. ``None`` when the value has no usable host: it does not parse (a bad port, an unclosed bracket), has no host,
    or has an ``@`` outside its authority (``u:p/s@h`` ends the authority at the ``/``, so the real userinfo sits in the
    path, query or fragment and what parses as host and port may be part of a credential).
    """
    try:
        parts = urlsplit(value if _URL_PREFIX.match(value) else f"//{value}")
        host, port = parts.hostname, parts.port
    except ValueError:  # an unclosed bracket, a port that is not a number in range
        return None
    if "@" in f"{parts.path}{parts.query}{parts.fragment}":
        return None
    host = (host or "").removesuffix(".")
    if not host:
        return None
    return ServerAddress(host, port if port is not None else _DEFAULT_PORTS.get(parts.scheme.lower()))


def host_port(base_url: str) -> str:
    """``host:port`` of *base_url*: the label a log line names a server by, and the key of every WARNING kept per server.

    Built only from :func:`parse_server_address`: the host is lower-cased with a trailing dot dropped, an IPv6 address stays
    in brackets, and the default port of ``http`` (80) or ``https`` (443) is filled in. A value without a scheme
    (``localhost:8080``) keeps its own port, or none. A value without a usable host (see ``parse_server_address``, e.g.
    userinfo that breaks the authority such as ``u:p/s@h``) is ``UNKNOWN_HOST``.

    Same label = same host and port; a path routes nothing here, so ``http://gw/a`` and ``http://gw/b`` behind one proxy
    are one server. Spellings that give the same label are one server (``LOCALHOST:8080`` and ``localhost:8080``;
    ``http://localhost`` and ``http://localhost:80``). Different names of one machine (``localhost``, ``127.0.0.1``,
    ``[::1]``) are different labels, hence different servers; all unparsable values are one label.
    """
    address = parse_server_address(base_url)
    if address is None:
        return UNKNOWN_HOST
    host = f"[{address.host}]" if ":" in address.host else address.host
    return host if address.port is None else f"{host}:{address.port}"
