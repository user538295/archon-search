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


def test_restart_raises_not_implemented(service: WindowsSearchService) -> None:
    with pytest.raises(NotImplementedError):
        service.restart()


def test_register_raises_not_implemented(service: WindowsSearchService) -> None:
    with pytest.raises(NotImplementedError):
        service.register(config_path="/etc/archon-search/archon-search.toml")


def test_unregister_raises_not_implemented(service: WindowsSearchService) -> None:
    with pytest.raises(NotImplementedError):
        service.unregister()


def test_status_returns_stopped(service: WindowsSearchService) -> None:
    result = service.status()
    assert result == ServiceStatus(running=False, pid=None, uptime_seconds=None)


def test_validate_registration_refuses_like_register(service: WindowsSearchService) -> None:
    """The installer's up-front check fails on Windows with register()'s message, before any download.

    NotImplementedError is a RuntimeError, which the check catches.
    """
    with pytest.raises(NotImplementedError) as refused:
        service.validate_registration(config_path="/etc/archon-search/archon-search.toml")
    with pytest.raises(NotImplementedError) as registered:
        service.register(config_path="/etc/archon-search/archon-search.toml")
    assert str(refused.value) == str(registered.value)
    assert isinstance(refused.value, RuntimeError)
