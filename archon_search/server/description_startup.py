"""The description feature's startup findings (C5, C12; S36, S37, S38, S51, S54, S56, S75, S84).

One validator, called once from the lifespan, that tells the operator why descriptions will not work or what they send
where. It never blocks the boot and never raises (a failing check logs one WARNING naming the error type), and a disabled
``[description]`` logs nothing except the off-notice. The window WARNINGs belong to the shared client layer (C1); the
unreachable-local-server finding is the startup probe's (``description_backfill``), which shares one ``warn_once`` key
with this module's cloud-key and missing-package findings so the problem is logged once however it was found.

"Local" means a loopback host, never the provider's name: :func:`is_loopback_host` decides it for the privacy INFO and both
restricted-chunk WARNINGs.
"""

from __future__ import annotations

import functools
import ipaddress
import logging
from typing import Final, Protocol
from urllib.parse import urlsplit

from archon_search.collection_meta import CollectionMeta
from archon_search.config import DescriptionConfig
from archon_search.constants import RESTRICTED_CHUNK_PROVIDERS
from archon_search.llm_client.provider_key import provider_key_available
from archon_search.llm_client.server_address import host_port
from archon_search.server.description_backfill import (
    local_server_url,
    static_provider_problem,
    warn_description_unreachable,
)

logger = logging.getLogger(__name__)

_LOCALHOST: Final = "localhost"
#: Providers the operator hosts: their server may be this machine (loopback) or not, which the INFO and the restricted-chunk
#: WARNINGs report.
_LOCAL_PROVIDERS: Final = frozenset({"ollama", "llama_cpp"})
#: Providers that read ``[description].think``; every other provider has no verified switch.
_THINK_SWITCH_PROVIDERS: Final = frozenset({"ollama", "llama_cpp"})
_HYBRID_STRATEGY: Final = "hybrid"
_OFF_NOTICE_KEY_PROVIDER: Final = "anthropic"
_IPV6_VERSION: Final = 6


class _MetaStore(Protocol):
    async def get_all_collections_meta(self) -> list[CollectionMeta]: ...


def _hostname(value: str) -> str | None:
    """The host of a URL, or of a scheme-less ``host:port`` (``urlsplit`` reads ``localhost:11434`` as scheme ``localhost``
    and no host, so a value without ``//`` is split as ``//value``). ``None`` when there is none or it cannot be parsed."""
    try:
        return urlsplit(value if "//" in value else f"//{value}").hostname
    except ValueError:  # an unparsable value such as ``http://[::1``
        return None


def _ip_literal(value: str) -> str | None:
    """*value* when it is an IP literal (an IPv6 one may sit in one matched pair of brackets), else ``None``."""
    bracketed = value.startswith("[") and value.endswith("]")
    inner = value[1:-1] if bracketed else value
    try:
        address = ipaddress.ip_address(inner)
    except ValueError:
        return None
    if bracketed and address.version != _IPV6_VERSION:  # ``[127.0.0.1]`` is malformed, not loopback
        return None
    return inner


def is_loopback_host(base_url: str) -> bool:
    """Whether the host of *base_url* is a loopback address (S84), for a URL or a scheme-less ``host:port``.

    ``localhost`` (any case, one trailing dot allowed) and loopback IP literals qualify, an IPv4-mapped IPv6 address by
    its IPv4 form. Everything else is not loopback on purpose: ``0.0.0.0`` (a wildcard), ``host.docker.internal`` and
    every other name (DNS is never consulted), an empty or unparsable value (the operator then gets the INFO).
    """
    value = base_url.strip()
    # A bare IPv6 literal (``::1``) is all colons to ``urlsplit``, which reads a port into it: take the literal whole.
    host = _ip_literal(value) or _hostname(value)
    if not host:
        return False
    host = host.removesuffix(".")
    if host == _LOCALHOST:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback  # an IPv4-mapped IPv6 address counts by its IPv4 form (Python 3.13+)


def off_notice(*, section_present: bool) -> str | None:
    """The INFO text for an installation that has an Anthropic key and no ``[description]`` section, else ``None`` (S36).

    A section that is written, even ``enabled = false``, is an answer and silences it.
    """
    if section_present or not provider_key_available(_OFF_NOTICE_KEY_PROVIDER):
        return None
    return (
        "Collection descriptions are off: ANTHROPIC_API_KEY is set, so Claude could write a short description for each "
        "collection. To turn them on, add `[description]` with `enabled = true` to the server config (`archon-search "
        "wizard` asks for it); excerpts of document text are then sent to Anthropic. To silence this notice, write "
        "`[description]` with `enabled = false`."
    )


def log_description_startup(description: DescriptionConfig, *, section_present: bool) -> None:
    """Log every startup finding of the description feature. Never raises; each check is isolated, so one that fails
    costs only its own finding. *section_present* is whether the config file has a ``[description]`` table."""
    for check in _checks(description, section_present):
        try:
            check()
        except Exception as failure:  # noqa: BLE001 — startup validation never blocks the boot
            logger.warning(
                "Description startup check %s could not be completed (%s)", check.func.__name__, type(failure).__name__
            )


def _checks(description: DescriptionConfig, section_present: bool) -> list[functools.partial[None]]:
    if not description.enabled:
        return [functools.partial(_log_off_notice, section_present)]
    return [
        functools.partial(_warn_unusable, description),
        functools.partial(_warn_think_ignored, description),
        functools.partial(_warn_restricted_chunks, description),
        functools.partial(_log_privacy, description),
    ]


def _log_off_notice(section_present: bool) -> None:
    notice = off_notice(section_present=section_present)
    if notice is not None:
        logger.info("%s", notice)


def _warn_unusable(description: DescriptionConfig) -> None:
    problem = static_provider_problem(description)
    if problem is not None:
        warn_description_unreachable(description.provider, problem.reason, problem.remedy)


def _warn_think_ignored(description: DescriptionConfig) -> None:
    if description.think is None or description.provider in _THINK_SWITCH_PROVIDERS:
        return
    logger.warning(
        '[description].think = %s is ignored for provider %s, which has no thinking switch; set think to "default" '
        "(or remove it) to silence this",
        str(description.think).lower(),
        description.provider,
    )


def _warn_restricted_chunks(description: DescriptionConfig) -> None:
    if not description.include_restricted_chunks:
        return
    provider = description.provider
    if provider not in RESTRICTED_CHUNK_PROVIDERS:
        logger.warning(
            "[description].include_restricted_chunks = true is ignored for provider %s (document text leaves the "
            "machine); chunks with an access list stay out of the sample",
            provider,
        )
        return
    url = local_server_url(description)
    host = host_port(url)
    logger.warning(
        "[description].include_restricted_chunks = true: chunks with an access list may be sampled and sent to the %s "
        "server at %s; their text may then appear in descriptions that every reader of the collection can see",
        provider,
        host,
    )
    if not is_loopback_host(url):
        logger.warning(
            "[description].include_restricted_chunks = true and the %s server at %s is not loopback: the text of "
            "restricted chunks is sent over the network to it",
            provider,
            host,
        )


def _log_privacy(description: DescriptionConfig) -> None:
    provider = description.provider
    if provider in _LOCAL_PROVIDERS:
        url = local_server_url(description)
        if is_loopback_host(url):
            return
        destination = f"the {provider} server at {host_port(url)}"
    else:
        destination = f"provider {provider}"
    logger.info(
        "Collection descriptions send excerpts of document text to %s, so they leave the machine (a retry sends them again)",
        destination,
    )


async def warn_hybrid_without_descriptions(routing_strategy: str, store: _MetaStore) -> None:
    """With hybrid routing, WARN that collections without a description fall back to the centroid alone (S37).

    Independent of ``[description].enabled``: a manual description counts too. Never raises.
    """
    if routing_strategy != _HYBRID_STRATEGY:
        return
    try:
        metas = await store.get_all_collections_meta()
    except Exception as failure:  # noqa: BLE001 — startup validation never blocks the boot
        logger.warning(
            "Description startup check %s could not be completed (%s)",
            warn_hybrid_without_descriptions.__name__,
            type(failure).__name__,
        )
        return
    undescribed = sum(1 for meta in metas if not meta.description)
    if undescribed:
        logger.warning(
            'routing_strategy = "hybrid" but %d of %d collections have no description; routing uses the centroid alone '
            "for them (set one with `archon-search collection set-description`, or enable [description] and run "
            "`archon-search collection describe --all`)",
            undescribed,
            len(metas),
        )
