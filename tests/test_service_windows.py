"""Tests for WindowsSearchService."""
import pytest

from archon_search.platform.service import ServiceStatus
from archon_search.platform.windows import WindowsSearchService


@pytest.fixture
def service() -> WindowsSearchService:
    return WindowsSearchService()


def test_start_raises_not_implemented(service: WindowsSearchService) -> None:
    with pytest.raises(NotImplementedError):
        service.start()


def test_stop_raises_not_implemented(service: WindowsSearchService) -> None:
    with pytest.raises(NotImplementedError):
        service.stop()


def test_register_raises_not_implemented(service: WindowsSearchService) -> None:
    with pytest.raises(NotImplementedError):
        service.register()


def test_unregister_raises_not_implemented(service: WindowsSearchService) -> None:
    with pytest.raises(NotImplementedError):
        service.unregister()


def test_status_returns_stopped(service: WindowsSearchService) -> None:
    result = service.status()
    assert result == ServiceStatus(running=False, pid=None, uptime_seconds=None)
