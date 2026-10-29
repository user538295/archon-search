"""A failed model download never writes the hub endpoint's credentials to a log destination (S939).

``HF_ENDPOINT`` may carry credentials (``http://user:password@host`` or a bare token as the userinfo). When the
download fails, ``huggingface_hub`` raises an error whose message embeds the request URL
(``Client error '404 Not Found' for url 'http://user:password@host/api/models/...'``). ``model_store`` scrubs that
text in its WARNING, but the raw error stays reachable through the exception chain
(``ModelDownloadError.__cause__``), so every ``exc_info`` record that follows prints it: the DEBUG record of
``model_store._log_download_failure``, the WARNING ``warm-up: embedder failed`` / ``warm-up: reranker failed`` records
of ``SearchPipeline.warmup_models`` and the DEBUG ``embedder probe failed`` / ``reranker probe failed`` records of
``validate_device``. Third-party loggers (``httpx`` INFO, ``huggingface_hub`` DEBUG) print the credentialed request
URL as well; in production only the ``archon_search`` logger tree has handlers.

These tests run the real production paths (``ensure_model``, ``SearchPipeline.warmup_models``, ``validate_device`` and
``validate_models_async``, real ``huggingface_hub``) against a local HTTP server that answers every request with an
HTTP error status, with the endpoint credentialed. Logging is configured by the real ``configure_logging`` (log file
plus the container stderr handler, text and JSON, INFO and DEBUG) with no root handler, as in production, and the
assertion reads what those destinations actually received: the log file and the captured stderr.

``huggingface_hub`` reads ``HF_ENDPOINT`` once, at import, so each test also points the library's already-evaluated
endpoint constants at the stand-in server, exactly as ``tests/test_data_dir_model_cache_private.py`` does.
"""

from __future__ import annotations

import contextlib
import json
import logging
import socket
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock

import huggingface_hub.constants as hub_constants
import pytest

from archon_search.chunker import DocumentChunker
from archon_search.config import SearchConfig
from archon_search.embedder import Embedder, ModelEmbedder
from archon_search.logging_setup import configure_logging
from archon_search.model_specs import EMBEDDER_SPECS
from archon_search.model_store import ModelDownloadError, ensure_model
from archon_search.model_validation import validate_device, validate_models_async
from archon_search.parser import DocumentParser
from archon_search.pipeline import SearchPipeline
from archon_search.reranker import ModelReranker, Reranker

# The embedder / reranker construct their real backends; the failing download aborts before torch is imported.
pytestmark = pytest.mark.real_torch_backends

_LOOPBACK = "127.0.0.1"
_EMBEDDER_NAME = "BAAI/bge-small-en-v1.5"
_RERANKER_NAME = "Xenova/ms-marco-MiniLM-L-6-v2"
_EMBEDDER = EMBEDDER_SPECS[_EMBEDDER_NAME]
_USER = "hubuser-7f3a"
_PASSWORD = "p4ss-9c1e-s3cret"
_TOKEN = "hf_T0kenOnlyUserinfo8d2b"
#: ``userinfo`` of the endpoint URL; every secret part of it must stay out of the logs.
_USERINFO_AND_SECRETS: dict[str, tuple[str, tuple[str, ...]]] = {
    "user-and-password": (f"{_USER}:{_PASSWORD}", (_USER, _PASSWORD)),
    "token-only": (_TOKEN, (_TOKEN,)),
}


class _AlwaysFails(BaseHTTPRequestHandler):
    """Answers every request (HEAD and GET) with ``status`` and an empty body."""

    status: int

    def log_message(self, format: str, *args: object) -> None:
        """Keep pytest output quiet."""

    def _fail(self) -> None:
        self.send_response(self.status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_HEAD = _fail


@pytest.fixture
def point_hub_at(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[[str], None]:
    """Make the real ``huggingface_hub`` talk to *endpoint*, as ``HF_ENDPOINT=<endpoint>`` does at process start.

    The data dir is fresh and empty: a model another test left in the shared per-worker data dir would be a cache
    hit, and no download would fail.
    """
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NO_PROXY", _LOOPBACK)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)

    def point(endpoint: str) -> None:
        monkeypatch.setenv("HF_ENDPOINT", endpoint)
        monkeypatch.setattr(hub_constants, "ENDPOINT", endpoint)
        monkeypatch.setattr(
            hub_constants, "HUGGINGFACE_CO_URL_TEMPLATE", endpoint + "/{repo_id}/resolve/{revision}/{filename}"
        )

    return point


@pytest.fixture
def failing_hub(point_hub_at: Callable[[str], None]) -> Iterator[Callable[[int, str], None]]:
    """Point the hub at ``http://<userinfo>@127.0.0.1:<port>``, a server answering every request with HTTP *status*."""
    servers: list[ThreadingHTTPServer] = []

    def serve(status: int, userinfo: str) -> None:
        handler = type("FailingHub", (_AlwaysFails,), {"status": status})
        server = ThreadingHTTPServer((_LOOPBACK, 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        point_hub_at(f"http://{userinfo}@{_LOOPBACK}:{server.server_port}")

    yield serve
    for server in servers:
        server.shutdown()
        server.server_close()


#: ``[logging].level`` and ``[logging].format`` combinations the server can run with.
_LOG_SETUPS = [("INFO", "text"), ("INFO", "json"), ("DEBUG", "text"), ("DEBUG", "json")]


class ServerLogs:
    """The destinations ``configure_logging`` builds (log file and container stderr handler) and what they received."""

    def __init__(self, log_file: Path, level: str, log_format: str, capsys: pytest.CaptureFixture[str]) -> None:
        self.log_file = log_file
        self.level = level
        self.log_format = log_format
        self._capsys = capsys
        self._stderr = ""

    @contextlib.contextmanager
    def running(self) -> Iterator[None]:
        """Run with the server's logging: ``configure_logging`` and no root handler (pytest's would capture third-party loggers).

        Configured inside the test, not in the fixture: the stderr handler binds ``sys.stderr`` at that moment, and
        pytest swaps the ``capsys`` stream between the setup and the call phase.
        """
        root = logging.getLogger()
        saved = root.handlers[:]
        root.handlers[:] = []
        configure_logging(
            SearchConfig(log_file=str(self.log_file), level=self.level, log_format=self.log_format)
        )
        try:
            yield
        finally:
            root.handlers[:] = saved

    def file_text(self) -> str:
        return self.log_file.read_text(encoding="utf-8") if self.log_file.exists() else ""

    def stderr_text(self) -> str:
        self._stderr += self._capsys.readouterr().err
        return self._stderr

    def everything(self) -> str:
        return self.file_text() + self.stderr_text()

    def leaks(self, secrets: tuple[str, ...]) -> list[str]:
        """One ``"<destination>: <line> leaks [...]"`` per output line that holds a secret."""
        found: list[str] = []
        for destination, text in (("log file", self.file_text()), ("stderr", self.stderr_text())):
            for line in text.splitlines():
                hits = [secret for secret in secrets if secret in line]
                if hits:
                    found.append(f"{destination}: {line[:140]!r} leaks {hits}")
        return found

    def warnings_named(self, logger_name: str, text: str) -> list[str]:
        """Lines of the log file holding a WARNING of *logger_name* whose message contains *text*."""
        lines = []
        for line in self.file_text().splitlines():
            if self.log_format == "json":
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                matches = (
                    record.get("level") == "WARNING"
                    and record.get("logger") == logger_name
                    and text in record.get("message", "")
                )
            else:
                matches = f" WARNING {logger_name} " in line and text in line
            if matches:
                lines.append(line)
        return lines


@pytest.fixture(params=_LOG_SETUPS, ids=[f"{level}-{fmt}" for level, fmt in _LOG_SETUPS])
def server_logs(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Iterator[ServerLogs]:
    """The log destinations ``configure_logging`` builds (a log file and the container stderr handler)."""
    level, log_format = request.param
    monkeypatch.setenv("ARCHON_SEARCH_CONTAINER", "1")
    yield ServerLogs(tmp_path / "logs" / "archon-search.log", level, log_format, capsys)
    logger = logging.getLogger("archon_search")
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    logger.propagate = True
    logger.setLevel(logging.NOTSET)


def _assert_no_leak(logs: ServerLogs, secrets: tuple[str, ...]) -> None:
    leaks = logs.leaks(secrets)
    assert not leaks, f"{len(leaks)} log line(s) carry the hub endpoint credentials:\n" + "\n".join(leaks)


def _real_pipeline() -> SearchPipeline:
    """A pipeline whose embedder and reranker are the real, cold, lazily downloading backends."""
    return SearchPipeline(
        store=MagicMock(),
        embedder=Embedder(ModelEmbedder(_EMBEDDER_NAME, device="cpu")),
        reranker=Reranker(ModelReranker(_RERANKER_NAME, device="cpu")),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )


@pytest.mark.parametrize("status", [404, 401, 500])
@pytest.mark.parametrize("userinfo, secrets", _USERINFO_AND_SECRETS.values(), ids=_USERINFO_AND_SECRETS.keys())
def test_failed_download_logs_no_endpoint_credentials(
    failing_hub: Callable[[int, str], None],
    server_logs: ServerLogs,
    status: int,
    userinfo: str,
    secrets: tuple[str, ...],
) -> None:
    failing_hub(status, userinfo)

    with server_logs.running(), pytest.raises(ModelDownloadError):
        ensure_model(_EMBEDDER)

    assert server_logs.warnings_named("archon_search.model_store", "model download failed"), "the failure was not logged"
    _assert_no_leak(server_logs, secrets)


def test_failed_download_keeps_the_error_class_and_traceback_for_debugging(
    failing_hub: Callable[[int, str], None], server_logs: ServerLogs
) -> None:
    """Redaction removes the credentials only: the failing class and the endpoint host stay in the log."""
    userinfo, secrets = _USERINFO_AND_SECRETS["user-and-password"]
    failing_hub(404, userinfo)

    with server_logs.running(), pytest.raises(ModelDownloadError):
        ensure_model(_EMBEDDER)

    [warning] = server_logs.warnings_named("archon_search.model_store", f"model download failed for {_EMBEDDER_NAME!r}")
    assert "404" in warning
    assert f"{_LOOPBACK}:" in warning
    _assert_no_leak(server_logs, secrets)
    if server_logs.level == "DEBUG":
        assert "Traceback" in server_logs.file_text(), "the DEBUG record keeps the traceback"


def test_hub_library_loggers_reach_no_server_destination(
    failing_hub: Callable[[int, str], None], server_logs: ServerLogs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``httpx`` (INFO) and ``huggingface_hub`` / ``httpcore`` / ``urllib3`` (DEBUG) print the credentialed URL themselves.

    The server configures handlers on the ``archon_search`` logger tree only, so nothing they log reaches the log
    file or the stderr handler, even with their levels raised.
    """
    userinfo, secrets = _USERINFO_AND_SECRETS["user-and-password"]
    failing_hub(404, userinfo)

    with server_logs.running():
        for name in ("huggingface_hub", "httpx", "httpcore", "urllib3"):
            monkeypatch.setattr(logging.getLogger(name), "level", logging.DEBUG)
        with pytest.raises(ModelDownloadError):
            ensure_model(_EMBEDDER)

    assert "HTTP Request" not in server_logs.everything(), "a hub library logger reached a server destination"
    _assert_no_leak(server_logs, secrets)


@pytest.mark.parametrize("userinfo, secrets", _USERINFO_AND_SECRETS.values(), ids=_USERINFO_AND_SECRETS.keys())
async def test_failed_warmup_logs_no_endpoint_credentials(
    failing_hub: Callable[[int, str], None],
    server_logs: ServerLogs,
    userinfo: str,
    secrets: tuple[str, ...],
) -> None:
    failing_hub(404, userinfo)
    pipeline = _real_pipeline()

    with server_logs.running():
        warm = await pipeline.warmup_models()

    assert warm is False
    assert server_logs.warnings_named("archon_search.pipeline", "warm-up: embedder failed"), "no embedder warm-up record"
    assert server_logs.warnings_named("archon_search.pipeline", "warm-up: reranker failed"), "no reranker warm-up record"
    assert "Traceback" in server_logs.file_text(), "the warm-up record keeps the traceback"
    assert "ModelDownloadError" in server_logs.file_text(), "the exception class must stay in the log"
    _assert_no_leak(server_logs, secrets)


@pytest.mark.parametrize("userinfo, secrets", _USERINFO_AND_SECRETS.values(), ids=_USERINFO_AND_SECRETS.keys())
def test_failed_probe_logs_no_endpoint_credentials(
    failing_hub: Callable[[int, str], None],
    server_logs: ServerLogs,
    userinfo: str,
    secrets: tuple[str, ...],
) -> None:
    failing_hub(404, userinfo)

    with server_logs.running():
        embedder_ok, reranker_ok, _warnings = validate_device("cpu", _EMBEDDER_NAME, _RERANKER_NAME)

    assert (embedder_ok, reranker_ok) == (False, False)
    for probe in ("embedder probe failed", "reranker probe failed"):
        assert server_logs.warnings_named("archon_search.model_validation", probe), f"no {probe!r} record"
    _assert_no_leak(server_logs, secrets)


async def test_failed_startup_validation_logs_no_endpoint_credentials(
    failing_hub: Callable[[int, str], None], server_logs: ServerLogs
) -> None:
    userinfo, secrets = _USERINFO_AND_SECRETS["user-and-password"]
    failing_hub(404, userinfo)
    config = SearchConfig(embedding_model=_EMBEDDER_NAME, reranker_model=_RERANKER_NAME, device="cpu")

    with server_logs.running():
        result = await validate_models_async(config, timeout_seconds=30)

    assert (result.embedder_ok, result.reranker_ok) == (False, False)
    assert server_logs.warnings_named("archon_search.model_validation", "probe failed"), "no probe failure record"
    _assert_no_leak(server_logs, secrets)


def test_refused_connection_logs_no_endpoint_credentials(
    point_hub_at: Callable[[str], None], server_logs: ServerLogs
) -> None:
    """A refused connection puts no URL in the hub error, so it keeps logging no credentials (guards over-fixing)."""
    with socket.socket() as sock:  # reserve a port, then close it: nothing listens there
        sock.bind((_LOOPBACK, 0))
        port = sock.getsockname()[1]
    userinfo, secrets = _USERINFO_AND_SECRETS["user-and-password"]
    point_hub_at(f"http://{userinfo}@{_LOOPBACK}:{port}")

    with server_logs.running(), pytest.raises(ModelDownloadError):
        ensure_model(_EMBEDDER)

    assert server_logs.warnings_named("archon_search.model_store", "model download failed"), "the failure was not logged"
    _assert_no_leak(server_logs, secrets)
