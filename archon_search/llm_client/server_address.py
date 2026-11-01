"""The address of a model server as a log line or an operator message may name it."""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import urlsplit

#: Stands in for the address when the configured URL does not parse. Every unparsable value shares it, so they are one
#: label: no digest of a value that may carry credentials is ever taken.
UNKNOWN_HOST: Final = "the configured host"

_DEFAULT_PORTS: Final = {"http": 80, "https": 443}
#: A value that starts with ``//`` or ``scheme://`` is a URL; anything else (``localhost:8080/v1``) is an authority and path.
_URL_PREFIX: Final = re.compile(r"([a-z][a-z0-9+.-]*:)?//", re.IGNORECASE)


def host_port(base_url: str) -> str:
    """``host:port`` of *base_url*: the label a log line names a server by, and the key of every WARNING kept per server.

    Built only from the parsed host name and port, never from the authority, so no userinfo reaches the label: the host is
    lower-cased with a trailing dot dropped, an IPv6 address stays in brackets, and the default port of ``http`` (80) or
    ``https`` (443) is filled in. A value without a scheme (``localhost:8080``) keeps its own port, or none. A value that
    does not parse (a bad port, an unclosed bracket, no host, or userinfo that breaks the authority such as ``u:p/s@h``)
    is ``UNKNOWN_HOST``.

    Spellings that give the same label are one server (``LOCALHOST:8080`` and ``localhost:8080``; ``http://localhost`` and
    ``http://localhost:80``). Different names of one machine (``localhost``, ``127.0.0.1``, ``[::1]``) are different labels,
    hence different servers; all unparsable values are one label.
    """
    try:
        parts = urlsplit(base_url if _URL_PREFIX.match(base_url) else f"//{base_url}")
        host, port = parts.hostname, parts.port
    except ValueError:  # an unclosed bracket, a port that is not a number in range
        return UNKNOWN_HOST
    host = (host or "").rstrip(".")
    if not host:
        return UNKNOWN_HOST
    if ":" in host:
        host = f"[{host}]"
    port = port if port is not None else _DEFAULT_PORTS.get(parts.scheme.lower())
    return host if port is None else f"{host}:{port}"
