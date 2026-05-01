"""Windows WindowsSearchService — stub (not yet supported)."""
from __future__ import annotations

from archon_search.platform.service import SearchServiceLifecycle, ServiceStatus

_MSG = "Windows service management not yet supported — run archon-search start manually"


class WindowsSearchService(SearchServiceLifecycle):
    def start(self) -> None:
        raise NotImplementedError(_MSG)

    def stop(self) -> None:
        raise NotImplementedError(_MSG)

    def restart(self) -> None:
        raise NotImplementedError(_MSG)

    def register(self) -> None:
        raise NotImplementedError(_MSG)

    def unregister(self) -> None:
        raise NotImplementedError(_MSG)

    def status(self) -> ServiceStatus:
        return ServiceStatus(running=False, pid=None, uptime_seconds=None)
