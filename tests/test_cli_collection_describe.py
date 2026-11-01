"""FE-11: ``collection describe <name> [--wait]`` and ``collection describe --all`` (S29, S31, S32, S58, S59, C10).

The HTTP side is a small in-memory API behind ``httpx.get`` / ``httpx.post``: a collection list, per-collection answers
to the describe POST, and jobs that are already terminal when first polled.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import unquote

import httpx
import pytest
from _cli_server_stub import server_reported_stopped
from click.testing import CliRunner, Result

from archon_search.cli._helpers import _poll_job_to_end
from archon_search.cli.collection import collection
from archon_search.constants import (
    DESCRIBE_MANUAL_DETAIL,
    DESCRIBE_NOT_CONFIGURED_DETAIL,
)
from archon_search.description_lifecycle import GenerationResult

_KEY = "test-key"
_ESC = "\x1b"
_BEL = "\x07"
_JOB_ID = "job-1"
_SUCCESS_OUTCOMES = ("generated", "alreadyRunning")
_FAILING_OUTCOMES = tuple(
    member.value
    for member in GenerationResult
    if member.value not in (*_SUCCESS_OUTCOMES, GenerationResult.SHUTTING_DOWN.value)
)


def _resp(status: int, body: object | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body if body is not None else {}
    resp.text = str(body)
    resp.headers = {}
    return resp


def _entry(
    name: str, *, docs: int = 3, description: str = "", source: str | None = None, status: str | None = None
) -> dict[str, Any]:
    return {
        "name": name,
        "doc_count": docs,
        "description": description,
        "description_source": source,
        "description_status": status,
    }


def _job(
    status: str = "DONE",
    outcome: str | None = "generated",
    description_status: str | None = "ok",
    error: str | None = None,
) -> dict[str, Any]:
    result = None if outcome is None else {"outcome": outcome, "description_status": description_status}
    return {"job_id": _JOB_ID, "status": status, "result": result, "error": error}


class _Api:
    """Answers the calls the describe verb makes; every POST URL is recorded."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self.post_answers: dict[str, MagicMock] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.details: dict[str, dict[str, Any]] = {}
        self.posts: list[str] = []
        self.gets: list[str] = []
        self.status_answer: MagicMock | None = None

    def post_for(self, name: str, answer: MagicMock) -> None:
        self.post_answers[name] = answer

    def job_for(self, name: str, job: dict[str, Any]) -> None:
        job_id = f"job-{name}"
        self.jobs[job_id] = {**job, "job_id": job_id}
        self.post_answers[name] = _resp(202, {"job_id": job_id, "status": "RUNNING"})

    def get(self, url: str, **_kwargs: Any) -> MagicMock:
        self.gets.append(url)
        if url.endswith("/status"):
            if self.status_answer is None:
                return _resp(200, {})  # an older server: no `description` field
            return self.status_answer
        if url.endswith("/collections/"):
            return _resp(200, self.entries)
        if "/jobs/" in url:
            job = self.jobs.get(url.rsplit("/", 1)[1])
            return _resp(200, job) if job else _resp(404, {"detail": "no job"})
        return _resp(200, self.details.get(url.rsplit("/", 1)[1], {}))

    def post(self, url: str, **_kwargs: Any) -> MagicMock:
        self.posts.append(url)
        segment = url.split("/collections/", 1)[1].removesuffix("/describe")
        return self.post_answers[unquote(segment)]


def _run(api: _Api, *args: str) -> Result:
    with patch("httpx.get", side_effect=api.get), patch("httpx.post", side_effect=api.post):
        return CliRunner().invoke(collection, ["describe", *args, "--api-key", _KEY])


def _posted_names(api: _Api) -> list[str]:
    return [url.split("/collections/", 1)[1].removesuffix("/describe") for url in api.posts]


def _single(outcome: str | None, *, status: str = "DONE", error: str | None = None, wait: bool = True) -> Result:
    api = _Api()
    api.job_for("docs", _job(status, outcome, error=error))
    return _run(api, "docs", *(["--wait"] if wait else []))


# ---------------------------------------------------------------------------
# describe <name>
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", _SUCCESS_OUTCOMES)
def test_describe_exit_code_per_outcome_success(outcome: str) -> None:
    """C10: ``generated`` and ``alreadyRunning`` exit 0 and the outcome is printed."""
    result = _single(outcome)

    assert result.exit_code == 0, result.output + result.stderr
    assert outcome in result.output


@pytest.mark.parametrize("outcome", _FAILING_OUTCOMES)
def test_describe_exit_code_per_outcome_failure(outcome: str) -> None:
    """C10: every other outcome the job result can carry exits 1 and names the outcome on stderr."""
    result = _single(outcome)

    assert result.exit_code == 1, result.output + result.stderr
    assert outcome in result.stderr


def test_describe_exit_code_per_outcome_unknown_outcome_exits_1() -> None:
    result = _single("somethingNew")

    assert result.exit_code == 1
    assert "somethingNew" in result.stderr


def test_describe_exit_code_per_outcome_success_words_are_lifecycle_results() -> None:
    """The two success words are real ``GenerationResult`` values; every other value the job result can carry
    (``shuttingDown`` never does: the job ends FAILED) is tested as a failure above."""
    values = {member.value for member in GenerationResult}

    assert set(_SUCCESS_OUTCOMES) <= values
    assert GenerationResult.SHUTTING_DOWN.value not in _FAILING_OUTCOMES
    assert values - {GenerationResult.SHUTTING_DOWN.value} == set(_SUCCESS_OUTCOMES) | set(_FAILING_OUTCOMES)


def test_describe_exit_code_per_outcome_a_done_job_without_a_result_exits_1() -> None:
    result = _single(None)

    assert result.exit_code == 1


def test_describe_prints_the_status_the_description_ended_in() -> None:
    result = _single("failed")

    assert "description_status: ok" in result.output + result.stderr


def test_describe_failed_job_exits_1_with_the_job_error() -> None:
    result = _single(None, status="FAILED", error="the server is shutting down")

    assert result.exit_code == 1
    assert "the server is shutting down" in result.stderr


def test_describe_without_wait_prints_the_job_id_and_does_not_poll() -> None:
    api = _Api()
    api.job_for("docs", _job())

    result = _run(api, "docs")

    assert result.exit_code == 0, result.stderr
    assert "job-docs" in result.output
    assert not any("/jobs/" in url for url in api.gets)


def test_describe_wait_shows_the_generated_description_and_source() -> None:
    """S29: the generated text is shown with source ``generated``."""
    api = _Api()
    api.job_for("docs", _job())
    api.details["docs"] = {
        "description": "Planning documents.",
        "description_source": "generated",
        "description_status": "ok",
    }

    result = _run(api, "docs", "--wait")

    assert result.exit_code == 0
    assert "Planning documents." in result.output
    assert "description_source: generated" in result.output


def test_describe_manual_409_prints_clear_first_and_exits_1() -> None:
    api = _Api()
    api.post_for("docs", _resp(409, {"detail": DESCRIBE_MANUAL_DETAIL}))

    result = _run(api, "docs", "--wait")

    assert result.exit_code == 1
    assert "clear it first" in result.stderr
    assert "collection clear-description docs" in result.stderr
    assert not any("/jobs/" in url for url in api.gets)


def test_describe_not_configured_409_prints_the_setup_message_and_exits_1() -> None:
    """S32: the server's setup message, printed as it is."""
    api = _Api()
    api.post_for("docs", _resp(409, {"detail": DESCRIBE_NOT_CONFIGURED_DETAIL}))

    result = _run(api, "docs")

    assert result.exit_code == 1
    assert DESCRIBE_NOT_CONFIGURED_DETAIL in result.stderr


def test_describe_not_found_exits_1() -> None:
    api = _Api()
    api.post_for("ghost", _resp(404, {"detail": "Collection not found"}))

    result = _run(api, "ghost")

    assert result.exit_code == 1
    assert "'ghost' not found" in result.stderr


def test_describe_server_down_prints_the_connect_failure_message() -> None:
    with patch("httpx.post", side_effect=httpx.ConnectError("refused")), server_reported_stopped():
        result = CliRunner().invoke(collection, ["describe", "docs", "--api-key", _KEY])

    assert result.exit_code == 1
    assert "not running" in result.stderr


def test_describe_sends_the_name_as_one_encoded_path_segment() -> None:
    api = _Api()
    api.job_for("a/b..", _job())

    _run(api, "a/b..")

    assert api.posts == ["http://localhost:8765/collections/a%2Fb%2E%2E/describe"]


def test_describe_refuses_an_empty_name_without_a_request() -> None:
    api = _Api()

    result = _run(api, "")

    assert result.exit_code == 1
    assert api.posts == []


def test_describe_strips_terminal_controls_from_every_printed_server_text() -> None:
    dirty = f"bad{_ESC}[31m{_BEL}"
    api = _Api()
    api.job_for("docs", _job("FAILED", None, error=dirty))

    result = _run(api, "docs", "--wait")

    assert _ESC not in result.stderr + result.output
    assert _BEL not in result.stderr + result.output


def test_describe_strips_terminal_controls_from_an_unknown_outcome() -> None:
    api = _Api()
    api.job_for("docs", _job(outcome=f"odd{_ESC}x"))

    result = _run(api, "docs", "--wait")

    assert "odd x" in result.stderr
    assert _ESC not in result.stderr + result.output


def test_describe_strips_terminal_controls_from_the_409_setup_message() -> None:
    api = _Api()
    api.post_for("docs", _resp(409, {"detail": f"setup{_ESC}[2J"}))

    result = _run(api, "docs")

    assert "setup [2J" in result.stderr
    assert _ESC not in result.stderr


@pytest.mark.parametrize("args", [[], ["docs", "--all"]])
def test_describe_takes_a_name_xor_all(args: list[str]) -> None:
    api = _Api()

    result = _run(api, *args)

    assert result.exit_code == 2
    assert api.posts == []


# ---------------------------------------------------------------------------
# describe --all
# ---------------------------------------------------------------------------


def _mixed_namespace() -> _Api:
    api = _Api()
    api.entries = [
        _entry("empty"),
        _entry("failed-one", description="old text", source="generated", status="failed"),
        _entry("manual-one", description="by hand", source="manual", status="ok"),
        _entry("ok-one", description="fine", source="generated", status="ok"),
        _entry("no-docs", docs=0),
    ]
    api.job_for("empty", _job())
    api.job_for("failed-one", _job())
    return api


def test_describe_all_fills_empty_or_failed_and_skips_manual_and_ok() -> None:
    """S31, S59: only the empty and the failed collections run; the others are listed with a reason."""
    api = _mixed_namespace()

    result = _run(api, "--all")

    assert result.exit_code == 0, result.output + result.stderr
    assert _posted_names(api) == ["empty", "failed-one"]
    out = result.output
    assert "manual-one" in out and "manual description" in out
    assert "ok-one" in out and "already described" in out
    assert "no-docs" in out and "no documents" in out
    assert "filled 2" in out and "skipped 3" in out and "failed 0" in out


def test_describe_all_lists_a_manual_collection_without_documents_as_manual() -> None:
    api = _Api()
    api.entries = [_entry("m", docs=0, description="x", source="manual", status="ok")]

    result = _run(api, "--all")

    assert result.exit_code == 0
    assert api.posts == []
    assert "Skipped 'm': manual description." in result.output


def test_describe_all_stops_on_not_configured_409() -> None:
    """S32: the setup message once, exit 1, no further collection is tried."""
    api = _Api()
    api.entries = [_entry("one"), _entry("two")]
    api.post_for("one", _resp(409, {"detail": DESCRIBE_NOT_CONFIGURED_DETAIL}))
    api.post_for("two", _resp(409, {"detail": DESCRIBE_NOT_CONFIGURED_DETAIL}))

    result = _run(api, "--all")

    assert result.exit_code == 1
    assert _posted_names(api) == ["one"]
    assert result.stderr.count(DESCRIBE_NOT_CONFIGURED_DETAIL) == 1


def _status(description: object) -> MagicMock:
    return _resp(200, {"description": description})


_CONFIGURED_BLOCK = {"provider": "anthropic"}


def test_describe_all_sends_the_key_to_the_status_preflight() -> None:
    api = _Api()
    api.entries = [_entry("ok", description="fine", source="generated", status="ok")]
    calls: list[dict[str, Any]] = []

    def get(url: str, **kwargs: Any) -> MagicMock:
        if url.endswith("/status"):
            calls.append(kwargs)
        return api.get(url, **kwargs)

    with patch("httpx.get", side_effect=get), patch("httpx.post", side_effect=api.post):
        result = CliRunner().invoke(collection, ["describe", "--all", "--api-key", _KEY])

    assert result.exit_code == 0, result.output + result.stderr
    assert [call["headers"] for call in calls] == [{"Authorization": f"Bearer {_KEY}"}]


def test_describe_all_not_configured_with_an_empty_namespace_still_exits_1() -> None:
    api = _Api()
    api.status_answer = _status(None)

    result = _run(api, "--all")

    assert result.exit_code == 1
    assert result.stderr.count(DESCRIBE_NOT_CONFIGURED_DETAIL) == 1


def test_describe_all_status_body_not_json_falls_back_to_the_loop() -> None:
    api = _Api()
    api.entries = [_entry("ok", description="fine", source="generated", status="ok")]
    broken = _resp(200)
    broken.json.side_effect = ValueError("not json")
    api.status_answer = broken

    result = _run(api, "--all")

    assert result.exit_code == 0, result.output + result.stderr
    assert "Skipped 'ok'" in result.output


def test_describe_single_never_calls_status() -> None:
    api = _Api()
    api.job_for("docs", _job())

    result = _run(api, "docs", "--wait")

    assert result.exit_code == 0, result.output + result.stderr
    assert not any(url.endswith("/status") for url in api.gets)


@pytest.mark.parametrize(
    "entries",
    [
        [_entry("ok", description="fine", source="generated", status="ok"), _entry("m", description="x", source="manual", status="ok"), _entry("e", docs=0)],
        [_entry("one"), _entry("two")],
    ],
    ids=["everything skippable", "fillable"],
)
def test_describe_all_not_configured_status_prints_setup_message_and_exits_1_before_touching_anything(entries: list[dict[str, Any]]) -> None:
    """S32: the status block is null (descriptions off): the setup message once, exit 1, no POST, no collection list."""
    api = _Api()
    api.entries = entries
    api.status_answer = _status(None)

    result = _run(api, "--all")

    assert result.exit_code == 1, result.output + result.stderr
    assert result.stderr.count(DESCRIBE_NOT_CONFIGURED_DETAIL) == 1
    assert api.posts == []
    assert not any(url.endswith("/collections/") for url in api.gets)
    assert "Skipped" not in result.output and "Summary" not in result.output


def test_describe_all_configured_with_nothing_to_fill_exits_0_with_the_skip_list() -> None:
    api = _Api()
    api.entries = [_entry("ok", description="fine", source="generated", status="ok")]
    api.status_answer = _status(_CONFIGURED_BLOCK)

    result = _run(api, "--all")

    assert result.exit_code == 0, result.output + result.stderr
    assert "Skipped 'ok'" in result.output
    assert api.posts == []


@pytest.mark.parametrize(
    "answer",
    [
        _resp(500, {"detail": "boom"}),
        _resp(401, {"detail": "no"}),
        _resp(503, {"description": None}),
        _resp(200, {"other": 1}),
        _resp(200, ["x"]),
    ],
    ids=["500", "401", "non-200 body saying null", "field absent", "not an object"],
)
def test_describe_all_status_unusable_falls_back_to_the_409_loop(answer: MagicMock) -> None:
    """A failing status call or a server without the field changes nothing: the per-collection 409 still stops the run."""
    api = _Api()
    api.entries = [_entry("one"), _entry("two")]
    api.status_answer = answer
    api.post_for("one", _resp(409, {"detail": DESCRIBE_NOT_CONFIGURED_DETAIL}))

    result = _run(api, "--all")

    assert result.exit_code == 1
    assert _posted_names(api) == ["one"]
    assert result.stderr.count(DESCRIBE_NOT_CONFIGURED_DETAIL) == 1


def test_describe_all_status_transport_error_falls_back_to_the_loop() -> None:
    api = _Api()
    api.entries = [_entry("ok", description="fine", source="generated", status="ok")]
    real_get = api.get

    def get(url: str, **kwargs: Any) -> MagicMock:
        if url.endswith("/status"):
            raise httpx.ReadTimeout("slow")
        return real_get(url, **kwargs)

    with patch("httpx.get", side_effect=get), patch("httpx.post", side_effect=api.post):
        result = CliRunner().invoke(collection, ["describe", "--all", "--api-key", _KEY])

    assert result.exit_code == 0, result.output + result.stderr
    assert "Skipped 'ok'" in result.output


def test_describe_all_exits_1_when_a_collection_ends_failed_and_goes_on() -> None:
    api = _Api()
    api.entries = [_entry("one"), _entry("two")]
    api.job_for("one", _job(outcome="failed", description_status="failed"))
    api.job_for("two", _job())

    result = _run(api, "--all")

    assert result.exit_code == 1
    assert _posted_names(api) == ["one", "two"]
    assert "filled 1" in result.output and "failed 1" in result.output


def test_describe_all_goes_on_after_a_failed_job_and_counts_it() -> None:
    api = _Api()
    api.entries = [_entry("one"), _entry("two")]
    api.job_for("one", _job("FAILED", None, error="internal error"))
    api.job_for("two", _job())

    result = _run(api, "--all")

    assert result.exit_code == 1
    assert "internal error" in result.stderr
    assert _posted_names(api) == ["one", "two"]
    assert "filled 1" in result.output and "failed 1" in result.output


def test_describe_all_names_the_job_it_waits_for() -> None:
    api = _Api()
    api.entries = [_entry("one")]
    api.job_for("one", _job())

    result = _run(api, "--all")

    assert "Describing 'one' (job job-one)" in result.output


def test_describe_all_skips_a_collection_that_turned_manual_meanwhile() -> None:
    api = _Api()
    api.entries = [_entry("one"), _entry("two")]
    api.post_for("one", _resp(409, {"detail": DESCRIBE_MANUAL_DETAIL}))
    api.job_for("two", _job())

    result = _run(api, "--all")

    assert result.exit_code == 0
    assert _posted_names(api) == ["one", "two"]
    assert "skipped 1" in result.output


def test_describe_all_treats_already_running_as_not_a_failure() -> None:
    api = _Api()
    api.entries = [_entry("one")]
    api.job_for("one", _job(outcome="alreadyRunning", description_status="pending"))

    result = _run(api, "--all")

    assert result.exit_code == 0
    assert "alreadyRunning" in result.output


def test_describe_all_with_nothing_to_fill_exits_0() -> None:
    api = _Api()
    api.entries = [_entry("ok", description="fine", source="generated", status="ok")]

    result = _run(api, "--all")

    assert result.exit_code == 0
    assert api.posts == []


def test_describe_all_an_empty_namespace_exits_0() -> None:
    result = _run(_Api(), "--all")

    assert result.exit_code == 0
    assert "No collections" in result.output


def test_describe_all_prints_every_collection_name_without_terminal_controls() -> None:
    api = _Api()
    api.entries = [_entry(f"x{_ESC}[31my", description="fine", source="generated", status="ok")]

    result = _run(api, "--all")

    assert _ESC not in result.output


def test_describe_all_accepts_wait_because_it_always_waits() -> None:
    api = _mixed_namespace()

    result = _run(api, "--all", "--wait")

    assert result.exit_code == 0


def test_describe_all_list_error_exits_1() -> None:
    with patch("httpx.get", return_value=_resp(500, {"detail": "boom"})):
        result = CliRunner().invoke(collection, ["describe", "--all", "--api-key", _KEY])

    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Wait, interrupt and malformed answers
# ---------------------------------------------------------------------------


def test_describe_wait_prints_the_job_id_before_it_polls() -> None:
    """An interrupted wait leaves the operator an id to track."""
    api = _Api()
    api.job_for("docs", _job())

    result = _run(api, "docs", "--wait")

    assert "Describe job started: job-docs" in result.output


def test_describe_wait_interrupted_exits_1_without_an_outcome() -> None:
    api = _Api()
    api.job_for("docs", _job())

    with patch("archon_search.cli.collection._poll_job_to_end", return_value={}):
        result = _run(api, "docs", "--wait")

    assert result.exit_code == 1
    assert "outcome" not in result.output + result.stderr


def test_describe_all_interrupted_stops_and_says_how_many_were_not_started() -> None:
    api = _Api()
    api.entries = [_entry("one"), _entry("two"), _entry("three"), _entry("done", description="x", status="ok")]
    for name in ("one", "two", "three"):
        api.job_for(name, _job())

    with patch("archon_search.cli.collection._poll_job_to_end", return_value={}):
        result = _run(api, "--all")

    assert result.exit_code == 1
    assert _posted_names(api) == ["one"]
    assert "2 more not started" in result.stderr


def test_describe_all_counts_a_refused_request_as_failed_and_goes_on() -> None:
    api = _Api()
    api.entries = [_entry("gone"), _entry("two")]
    api.post_for("gone", _resp(404, {"detail": "Collection not found"}))
    api.job_for("two", _job())

    result = _run(api, "--all")

    assert result.exit_code == 1
    assert _posted_names(api) == ["gone", "two"]
    assert "'gone' not found" in result.stderr
    assert "filled 1" in result.output and "failed 1" in result.output


def test_describe_exits_1_when_the_server_accepts_without_a_job_id() -> None:
    api = _Api()
    api.post_for("docs", _resp(202, {}))

    result = _run(api, "docs", "--wait")

    assert result.exit_code == 1
    assert "no job id" in result.stderr


@pytest.mark.parametrize("body", [{"not": "a list"}, "text"])
def test_describe_all_unexpected_collection_list_exits_1(body: object) -> None:
    with patch("httpx.get", return_value=_resp(200, body)):
        result = CliRunner().invoke(collection, ["describe", "--all", "--api-key", _KEY])

    assert result.exit_code == 1
    assert "unexpected collection list" in result.stderr


def test_describe_prints_none_for_a_status_the_server_did_not_store() -> None:
    api = _Api()
    api.job_for("docs", _job(outcome="alreadyRunning", description_status=None))

    result = _run(api, "docs", "--wait")

    assert "description_status: none" in result.output


def test_describe_not_found_prints_the_name_without_terminal_controls() -> None:
    name = f"gh{_ESC}[31mo"
    api = _Api()
    api.post_for(name, _resp(404, {"detail": "Collection not found"}))

    result = _run(api, name)

    assert _ESC not in result.stderr
    assert "not found" in result.stderr


@pytest.mark.parametrize("status", ["FAILED", "CANCELLED", "FAILED_EXPIRED"])
def test_poll_job_to_end_returns_a_job_in_any_terminal_status_without_exiting(status: str) -> None:
    job = {"job_id": "j", "status": status, "error": "boom"}

    with patch("httpx.get", return_value=_resp(200, job)):
        final = _poll_job_to_end("j", "http://localhost:8765", {})

    assert final == job
