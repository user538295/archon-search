"""The provider package guard shared by the features that only warn about a missing package (C12; S16, S75).

HyDE and RAG Fusion refuse to start without their provider package (``_check_provider_deps``); graph enrichment and
collection descriptions do not: the server starts, and the feature reports the problem with the text this guard produces.
``llama_cpp`` needs no package (httpx is core) and ``claude_cli`` needs the ``claude`` binary, which the description probe
checks, so neither is ever guarded here.
"""

from __future__ import annotations

import dataclasses
import importlib
from typing import Final

from archon_search.uv_tool import extra_install_hint

#: Provider -> install extra that provides its package (the package is named like the provider).
PROVIDER_PACKAGE_EXTRA: Final[dict[str, str]] = {
    "anthropic": "hyde",
    "openai": "openai-provider",
    "ollama": "ollama",
}


@dataclasses.dataclass(frozen=True, slots=True)
class PackageProblem:
    """Why a provider's package cannot be used. ``detail`` reads after "the 'x' package ": ``is not installed`` or
    ``failed to import (ErrorType)``. ``install_hint`` is the install command, ``None`` when the package is present but
    broken (a command would not help)."""

    package: str
    detail: str
    install_hint: str | None

    def summary(self) -> str:
        """``the 'x' package <detail>``: the hint-free text, safe to show wherever a reason is shown (the install command
        of a ``uv tool`` install can replay index credentials and local paths from the receipt, so it is logged only)."""
        return f"the {self.package!r} package {self.detail}"


def check_provider_package(provider: str | None) -> PackageProblem | None:
    """The problem importing *provider*'s package, or ``None`` when it imports or the provider needs no package.

    Never raises: an SDK that raises while importing must not stop the boot.
    """
    if provider not in PROVIDER_PACKAGE_EXTRA:
        return None
    try:
        importlib.import_module(provider)
    except Exception as exc:  # noqa: BLE001 — an SDK that raises while importing must not stop the boot
        if isinstance(exc, ModuleNotFoundError) and exc.name == provider:
            return PackageProblem(provider, "is not installed", extra_install_hint(PROVIDER_PACKAGE_EXTRA[provider]))
        # A broken install, not a missing one: an install command would not help, so none is offered.
        return PackageProblem(provider, f"failed to import ({type(exc).__name__})", None)
    return None
