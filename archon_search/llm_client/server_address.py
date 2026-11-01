"""The address of a model server as a log line or an operator message may name it."""

from __future__ import annotations

from typing import Final
from urllib.parse import urlsplit

#: Stands in for the address when the configured URL does not parse.
UNKNOWN_HOST: Final = "the configured host"


def host_port(base_url: str) -> str:
    """``host:port`` of *base_url*: what a log line may name (no credentials, path or query).

    A value without a scheme (``localhost:8080``) reads as an authority, a value that does not parse as ``UNKNOWN_HOST``.
    The context-window lookup and the description probe both key their one WARNING per server on this label, so they must
    agree on it for every spelling of one URL.
    """
    try:
        authority = urlsplit(base_url if "//" in base_url else f"//{base_url}").netloc.rpartition("@")[2]
    except ValueError:  # an unparsable value such as ``http://[::1``
        return UNKNOWN_HOST
    return authority or UNKNOWN_HOST
