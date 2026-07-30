"""S04 regression: GET /health must be unreachable after the service stops.

Bug (``Documentation/Backlog/S04-health_unreachable_after_stop.md``): after
``archon-search stop`` the CLI reported success while ``GET /health`` still
returned 200. Root cause — the platform ``stop()`` implementations issued the
launchctl/systemctl stop command and returned immediately, before the server
process had exited and released its listening socket, so a health probe run
right after ``stop()`` returned still hit the dying server.

This test spawns a real ``archon-search serve`` subprocess (dedicated to this
test — never the shared session ``smoke_server``, since it kills it) and drives
the *real* current-platform ``SearchServiceLifecycle.stop()`` against it. Only
the OS-binding seams are redirected onto the real subprocess:

* ``_run`` — the stop verb (``launchctl unload`` / ``systemctl stop``) begins an
  asynchronous, deterministically *delayed* termination of the subprocess.
* ``status`` — reflects whether the real subprocess is still alive.

The production code under test is the ``stop()`` -> ``_wait_until_stopped()``
poll loop added for S04. Before the fix, ``stop()`` returns while the delayed
termination is still pending and ``GET /health`` answers 200 (test fails).
After the fix, ``stop()`` blocks until the subprocess is gone, so ``GET
/health`` is refused once ``stop()`` returns.

This test models the supervisor's ``status()`` as exactly tracking process
liveness (``proc.poll()``). It therefore exercises the production
``stop()`` -> ``_wait_until_stopped()`` poll loop end-to-end, but does not cover
the residual window where a supervisor could report not-running while the socket
is still briefly held; that gap is a property of the real launchd/systemd
timing, not of the wait loop under test.

Module-level ``pytestmark`` serialises this file onto one xdist worker (shared
smoke-suite convention).
"""

from __future__ import annotations

import contextlib
import secrets
import subprocess
import threading
from unittest.mock import patch

import httpx
import pytest

from archon_search.platform.runtime import get_search_service
from archon_search.platform.service import ServiceStatus
from tests.smoke.conftest import _free_port, _start_server, _terminate

pytestmark = pytest.mark.xdist_group("smoke_e2e")

# The stop verb schedules termination this many seconds in the future, giving a
# deterministic window in which the server is still alive right after stop() is
# issued — long enough that the buggy (no-wait) stop() reliably observes a live
# /health, and comfortably inside the stop() wait-loop timeout (10s).
_TERMINATION_DELAY_S = 1.0


def test_health_unreachable_after_service_stop(tmp_path_factory) -> None:
    """After ``stop()`` returns, ``GET /health`` must be refused, not 200 (S04)."""
    port = _free_port()
    data_dir = tmp_path_factory.mktemp("s04_data")
    api_key = secrets.token_hex(32)
    base_url = f"http://127.0.0.1:{port}"

    proc = _start_server(port=port, data_dir=data_dir, api_key=api_key)
    timers: list[threading.Timer] = []

    try:
        # Precondition: a freshly started server answers /health with 200.
        assert httpx.get(f"{base_url}/health", timeout=5).status_code == 200

        def _delayed_terminate() -> None:
            def _kill() -> None:
                if proc.poll() is None:
                    proc.terminate()

            timer = threading.Timer(_TERMINATION_DELAY_S, _kill)
            timers.append(timer)
            timer.start()

        def _fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            # The platform stop verb (launchctl unload / systemctl stop) kicks
            # off asynchronous termination — modelled here as a delayed SIGTERM.
            if "unload" in cmd or "stop" in cmd:
                _delayed_terminate()
            return subprocess.CompletedProcess(cmd, 0, "", "")

        def _fake_status() -> ServiceStatus:
            return ServiceStatus(running=proc.poll() is None, pid=proc.pid, uptime_seconds=None)

        svc = get_search_service()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(svc, "_run", side_effect=_fake_run))
            stack.enter_context(patch.object(svc, "status", side_effect=_fake_status))
            # macOS gates stop() on _is_loaded(); Linux has no such method.
            if hasattr(svc, "_is_loaded"):
                stack.enter_context(patch.object(svc, "_is_loaded", return_value=True))

            svc.stop()

        # stop() has returned. The server must be gone: /health is refused.
        with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)):
            httpx.get(f"{base_url}/health", timeout=2)
    finally:
        # Cancel any delayed-terminate timer that has not yet fired so no stray
        # thread outlives the test, then ensure the subprocess is gone.
        for timer in timers:
            timer.cancel()
        _terminate(proc)
