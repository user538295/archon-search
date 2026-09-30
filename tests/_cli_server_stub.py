"""Shared test helper: make ``_server_connect_fail_msg`` deterministic.

CLI commands print a friendly message on a connection failure via
``archon_search.cli._helpers._server_connect_fail_msg``. For the default local
URL that helper first probes ``{base_url}/ready`` with a real ``httpx.get`` and,
when the probe fails, consults the local service manager
(``_get_service().status()``). A test that patches only the *command* module's
``httpx`` leaves both of those live, so the message flips to "starting up"
whenever a real archon-search instance is loading on the host — making the
assertions flaky.

``server_reported_stopped()`` stubs both dependencies so the helper always
resolves to ``_SERVER_NOT_RUNNING_MSG`` regardless of any real server/service:
the ``/ready`` probe raises ``ConnectError`` and the service manager reports the
process as not running.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator
from unittest.mock import MagicMock, patch

import httpx


@contextmanager
def server_reported_stopped() -> Iterator[None]:
    """Force the connect-failure message to the deterministic "not running" text."""
    stopped = MagicMock()
    stopped.status.return_value.running = False
    with (
        patch("archon_search.cli._helpers.httpx.get", side_effect=httpx.ConnectError("refused")),
        patch("archon_search.cli._helpers._get_service", return_value=stopped),
    ):
        yield
