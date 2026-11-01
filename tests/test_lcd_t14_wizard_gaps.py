"""LCD T-14: closes the cheap gaps the T-8 read-through found (checklist section 7) in the wizard's
collection-description step and the graph `claude_cli` / `llama_cpp` legs.

The yes/no questions follow one repo convention: Enter is not an answer, so the question is refused
with `Please answer y or n.` and asked again, and only a closed stdin means no
(`install/prompts.py:ask_yes_no`, used for HyDE / RAG Fusion, graph, descriptions and the backfill
question through the wizard's `_ask_yn`). The plan's "defaults no" (S44, S45)
therefore means "a closed stdin is no", and these tests pin that for every one of those questions.

Everything runs through the real wizard (`main wizard`); only the model-host lookups, the package
installer subprocess boundary (`extras._uv_or_pip_install`) and the process-wide infrastructure of
`_patched_wizard` are replaced, so the suite stays offline.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner, Result

from archon_search.config import LLAMA_CPP_BASE_URL_DEFAULT, load_config
from archon_search.install import InstallError
from archon_search.install.extras import _install_query_expansion_extras
from archon_search.install.prompts import INVALID_ANSWER_MESSAGE
from archon_search.install.wizard import (
    OLLAMA_CONTEXT_SUGGESTION_TOKENS,
    _prompt_optional_features,
)
from archon_search.platform.service import get_secrets_env_file
from archon_search.profiles import ENGLISH_PROFILES
from tests.test_e2e_wizard_optional_features import (
    _CONTEXT_PROMPT,
    _DESCRIPTION_BACKFILL,
    _DESCRIPTION_LABEL,
    _DESCRIPTION_QUESTION,
    _EXTRAS_PASS,
    _INSTALLER,
    _MODEL_PICK_PROMPT,
    _PROVIDER_PROMPT,
    _run_minimal_wizard_interactive,
    _written,
)

_HYDE_QUESTION = "Enable AI query expansion (HyDE + RAG Fusion)? [y/n]: "
_GRAPH_QUESTION = "Enable LLM-backed graph enrichment? [y/n]: "
_GRAPH_LABEL = "graph enrichment"
_PROCEED_PROMPT = "Proceed? [y/n]: "
_FREETEXT_MODEL_PROMPT = "Model name for {} (required for non-Anthropic providers): "
_CLAUDE_MODEL_PROMPT = "Model for {} (number, name, or blank): "
_OLLAMA_URL_PROMPT = "Ollama base URL for {} [http://localhost:11434]: "
_LLAMA_CPP_URL_PROMPT = "llama-server base URL for {} [" + LLAMA_CPP_BASE_URL_DEFAULT + "]: "
_SHARED_MODEL_WARNING = "Warning: these features share the Ollama model"
_RELOAD_SENTENCE = "reloads the model"
_BULLET_HYDE = "• HyDE: enabled (provider: {})"
_BULLET_RAG_FUSION = "• RAG Fusion: enabled (provider: {})"
_BULLET_DESCRIPTION = "• Collection descriptions: enabled (provider: {})"
_BULLET_GRAPH = "• LLM graph enrichment: enabled (provider: {})"
_PACKAGE_PREFIX = "archon-search"
_HYDE_EXTRA = f"{_PACKAGE_PREFIX}[hyde]"
_OLLAMA_EXTRA = f"{_PACKAGE_PREFIX}[ollama]"
_OPENAI_EXTRA = f"{_PACKAGE_PREFIX}[openai-provider]"
_TYPED_MODEL = "custom:7b"
_OPENAI_MODEL = "gpt-4o-mini"
_CUSTOM_LLAMA_CPP_URL = "http://box:8080"
_STORED_WINDOWS = {"HyDE": 4096, "RAG Fusion": 5120, _GRAPH_LABEL: 6144}

Answers = dict[str, str | tuple[str, ...]]


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _own_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite shares one data dir per worker; `.secrets.env` lives in it, so give each test its own."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path / "data"))


def _wizard(
    runner: CliRunner,
    config_path: Path,
    answers: Answers,
    *,
    ollama_models: Sequence[str] = ("m1",),
    llama_cpp_models: Sequence[str] = ("m1",),
    install: MagicMock | None = None,
    wizard_patches: dict[str, Any] | None = None,
) -> Result:
    """Run the interactive minimal-profile wizard with scripted *answers*.

    With *install* the REAL extras pass and `_install_extra` run, and *install* replaces only the
    pip/uv subprocess boundary (`extras._uv_or_pip_install`); its calls are `(["archon-search[x]"], [], label)`.
    """
    patches = dict(wizard_patches or {})
    with (
        patch("archon_search.install.wizard._fetch_ollama_models", return_value=list(ollama_models)),
        patch("archon_search.install.wizard._fetch_llama_cpp_models", return_value=list(llama_cpp_models)),
        patch("archon_search.install.extras.find_receipt", return_value=None),
        patch("archon_search.install.extras._uv_or_pip_install", install or MagicMock()),
    ):
        if install is not None:
            patches[_EXTRAS_PASS] = _install_query_expansion_extras
        return _run_minimal_wizard_interactive(runner, config_path, answers=answers, wizard_patches=patches)


def _installed(install: MagicMock) -> list[str]:
    return [call.args[0][0] for call in install.call_args_list]


def _failing(*extras: str) -> MagicMock:
    """A `_uv_or_pip_install` stand-in that fails for the given packages and records every call."""

    def _run(packages: list[str], extra_args: list[str], fail_label: str) -> None:
        if packages[0] in extras:
            raise InstallError("boom")

    return MagicMock(side_effect=_run)


def _assert_in_order(output: str, prompts: Sequence[str]) -> None:
    position = -1
    for prompt in prompts:
        found = output.find(prompt, position + 1)
        assert found > position, f"{prompt!r} not found after offset {position} in:\n{output}"
        position = found


def _describe(provider: str, backfill: str = "n") -> Answers:
    """Descriptions on *provider* with the model answered the way each provider asks for it."""
    answers: Answers = {_DESCRIPTION_QUESTION: "y", _PROVIDER_PROMPT.format(_DESCRIPTION_LABEL): provider}
    if provider == "ollama":
        answers[_MODEL_PICK_PROMPT] = "1"
    elif provider == "llama_cpp":
        answers[_LLAMA_CPP_URL_PROMPT.format(_DESCRIPTION_LABEL)] = _CUSTOM_LLAMA_CPP_URL
        answers[_MODEL_PICK_PROMPT] = "1"
    elif provider == "openai":
        answers[_FREETEXT_MODEL_PROMPT.format(_DESCRIPTION_LABEL)] = _OPENAI_MODEL
    elif provider == "claude_cli":
        answers[_CLAUDE_MODEL_PROMPT.format(_DESCRIPTION_LABEL)] = "haiku"
    answers[_DESCRIPTION_BACKFILL] = backfill
    return answers


# ---------------------------------------------------------------------------
# S44 / S45: "defaults no" is the repo's y/n convention (Enter re-asks, closed stdin is no)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(
    "question",
    [_HYDE_QUESTION, _GRAPH_QUESTION, _DESCRIPTION_QUESTION],
    ids=["hyde-rag-fusion", "graph", "description"],
)
def test_enter_at_a_feature_question_is_refused_and_reasked(runner: CliRunner, tmp_path: Path, question: str) -> None:
    """Enter is not an answer to the HyDE / graph / description question: it is refused once and the `n` typed
    next is what counts. Descriptions behave exactly like the questions that already existed."""
    config_path = tmp_path / "archon-search.toml"

    result = _wizard(runner, config_path, {question: ("", "n")})

    assert result.exit_code == 0, f"Exit {result.exit_code}:\n{result.output}"
    assert result.output.count(question) == 2
    assert result.output.count(INVALID_ANSWER_MESSAGE) == 1
    assert _written(config_path)["description"] == {"enabled": False}


@pytest.mark.integration
def test_enter_at_the_description_question_then_yes_continues_into_the_follow_ups(
    runner: CliRunner, tmp_path: Path
) -> None:
    """The re-asked description question still takes a `y` and goes on to the provider question."""
    config_path = tmp_path / "archon-search.toml"
    answers = _describe("anthropic")
    answers[_DESCRIPTION_QUESTION] = ("", "y")

    result = _wizard(runner, config_path, answers)

    assert result.exit_code == 0, f"Exit {result.exit_code}:\n{result.output}"
    assert result.output.count(INVALID_ANSWER_MESSAGE) == 1
    _assert_in_order(
        result.output,
        [_DESCRIPTION_QUESTION, INVALID_ANSWER_MESSAGE, _DESCRIPTION_QUESTION, _PROVIDER_PROMPT.format(_DESCRIPTION_LABEL)],
    )
    assert _written(config_path)["description"] == {"enabled": True, "backfill_on_startup": False}


@pytest.mark.integration
@pytest.mark.parametrize("typed", ["y", "n"])
def test_enter_at_the_backfill_question_is_refused_and_reasked(runner: CliRunner, tmp_path: Path, typed: str) -> None:
    """Enter at the backfill question is refused and asked again; the answer typed next is what is stored."""
    config_path = tmp_path / "archon-search.toml"
    answers = _describe("anthropic")
    answers[_DESCRIPTION_BACKFILL] = ("", typed)

    result = _wizard(runner, config_path, answers)

    assert result.exit_code == 0, f"Exit {result.exit_code}:\n{result.output}"
    assert result.output.count(_DESCRIPTION_BACKFILL) == 2
    assert result.output.count(INVALID_ANSWER_MESSAGE) == 1
    assert _written(config_path)["description"]["backfill_on_startup"] is (typed == "y")
    assert load_config(config_path).description.backfill_on_startup is (typed == "y")


@pytest.mark.parametrize(
    "answered_before_eof",
    [[], ["n", "n", "y", "anthropic"]],  # HyDE n, graph n, description y, its provider, then the stdin closes
    ids=["question", "backfill"],
)
def test_closed_stdin_answers_no_to_every_feature_question(answered_before_eof: list[str]) -> None:
    """A closed stdin is the only way a y/n question defaults: no, for HyDE / RAG Fusion, graph, the description
    question (nothing asked after it) and the backfill question (asked, then no)."""
    prompts: list[str] = []
    answers = iter(answered_before_eof)

    def _input(prompt: str = "") -> str:
        prompts.append(prompt)
        try:
            return next(answers)
        except StopIteration:
            raise EOFError from None

    with patch("builtins.input", _input):
        features = _prompt_optional_features(
            non_interactive=False,
            profile=ENGLISH_PROFILES["minimal"],
            install_code=False,
            disable_reranker=False,
            enable_watch=False,
            enable_telemetry=False,
            eager_load=False,
            routing_strategy="centroid",
            log_format="text",
        )

    assert [p for p in prompts if p.startswith("Enable")][:3] == [_HYDE_QUESTION, _GRAPH_QUESTION, _DESCRIPTION_QUESTION]
    assert features.enable_hyde is False
    assert features.enable_rag_fusion is False
    assert features.graph_provider == ""
    assert features.description_backfill_on_startup is False
    assert features.description_enabled is bool(answered_before_eof)
    if answered_before_eof:
        assert prompts[-1] == _DESCRIPTION_BACKFILL


# ---------------------------------------------------------------------------
# S45 / S46: prompt order and the written keys, per provider, end to end
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(
    ("provider", "expected_prompts", "absent_prompts", "expected_section"),
    [
        pytest.param(
            "ollama",
            [
                _PROVIDER_PROMPT.format(_DESCRIPTION_LABEL),
                _OLLAMA_URL_PROMPT.format(_DESCRIPTION_LABEL),  # the host comes first: the model list is fetched from it
                _MODEL_PICK_PROMPT,
                _CONTEXT_PROMPT.format(_DESCRIPTION_LABEL, OLLAMA_CONTEXT_SUGGESTION_TOKENS),
                _DESCRIPTION_BACKFILL,
            ],
            [],
            {
                "enabled": True,
                "provider": "ollama",
                "model": "m1",
                "ollama_context_tokens": OLLAMA_CONTEXT_SUGGESTION_TOKENS,
                "backfill_on_startup": False,
            },
            id="ollama",
        ),
        pytest.param(
            "llama_cpp",
            [
                _PROVIDER_PROMPT.format(_DESCRIPTION_LABEL),
                _LLAMA_CPP_URL_PROMPT.format(_DESCRIPTION_LABEL),
                _MODEL_PICK_PROMPT,
                _DESCRIPTION_BACKFILL,
            ],
            ["Ollama context window for"],  # llama_cpp has no window question
            {
                "enabled": True,
                "provider": "llama_cpp",
                "model": "m1",
                "llama_cpp_base_url": _CUSTOM_LLAMA_CPP_URL,
                "backfill_on_startup": False,
            },
            id="llama_cpp",
        ),
        pytest.param(
            "anthropic",
            [_PROVIDER_PROMPT.format(_DESCRIPTION_LABEL), _DESCRIPTION_BACKFILL],
            ["Model for", "Model name for", "Select a model", "base URL", "Ollama context window for"],
            {"enabled": True, "backfill_on_startup": False},  # anthropic is the default provider and asks no model
            id="anthropic",
        ),
    ],
)
def test_description_prompts_follow_provider_order_and_write_exact_keys(
    runner: CliRunner,
    tmp_path: Path,
    provider: str,
    expected_prompts: list[str],
    absent_prompts: list[str],
    expected_section: dict[str, Any],
) -> None:
    """S45/S46: after the description yes the follow-ups come in the provider's order (host before model for
    ollama and llama_cpp, none for anthropic), and the `[description]` table holds exactly those answers."""
    config_path = tmp_path / "archon-search.toml"

    result = _wizard(runner, config_path, _describe(provider))

    assert result.exit_code == 0, f"Exit {result.exit_code}:\n{result.output}"
    _assert_in_order(result.output, [_DESCRIPTION_QUESTION, *expected_prompts])
    description_part = result.output[result.output.index(_DESCRIPTION_QUESTION) :]
    for absent in absent_prompts:
        assert absent not in description_part
    assert _written(config_path)["description"] == expected_section
    loaded = load_config(config_path).description
    assert (loaded.enabled, loaded.provider) == (True, provider)


@pytest.mark.integration
@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_cloud_description_keeps_the_key_out_of_the_toml_and_creates_secrets_env(
    runner: CliRunner, tmp_path: Path, provider: str
) -> None:
    """S46/S47: a cloud provider for descriptions writes no secret into the TOML; the wizard creates the empty
    `.secrets.env` (mode 600) and names the variable the operator must put in it."""
    config_path = tmp_path / "archon-search.toml"
    secrets_path = get_secrets_env_file()
    assert not secrets_path.exists()

    result = _wizard(runner, config_path, _describe(provider))

    assert result.exit_code == 0, f"Exit {result.exit_code}:\n{result.output}"
    text = config_path.read_text().lower()
    assert "api_key" not in text
    assert "secret" not in text
    assert "sk-" not in text
    assert secrets_path.exists()
    assert secrets_path.stat().st_mode & 0o777 == 0o600
    assert secrets_path.read_text() == ""
    assert f"Created:    {secrets_path}" in result.output
    assert "load it at start time for collection descriptions." in result.output


@pytest.mark.integration
@pytest.mark.parametrize("provider", ["ollama", "llama_cpp", "claude_cli"])
def test_keyless_description_provider_creates_no_secrets_env(runner: CliRunner, tmp_path: Path, provider: str) -> None:
    """The file is created only when a written feature reads a key: ollama, llama_cpp and claude_cli need none."""
    result = _wizard(runner, tmp_path / "archon-search.toml", _describe(provider))

    assert result.exit_code == 0, f"Exit {result.exit_code}:\n{result.output}"
    assert not get_secrets_env_file().exists()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("provider", "typed_answers", "ollama_models", "llama_cpp_models", "expected_section"),
    [
        pytest.param(
            "openai",
            {_FREETEXT_MODEL_PROMPT.format(_DESCRIPTION_LABEL): _OPENAI_MODEL},
            ("m1",),
            ("m1",),
            {"enabled": True, "provider": "openai", "model": _OPENAI_MODEL, "backfill_on_startup": False},
            id="openai",
        ),
        pytest.param(
            "ollama",
            {_FREETEXT_MODEL_PROMPT.format(_DESCRIPTION_LABEL): _TYPED_MODEL},
            (),  # no model list from the server: free-text entry
            ("m1",),
            {
                "enabled": True,
                "provider": "ollama",
                "model": _TYPED_MODEL,
                "ollama_context_tokens": OLLAMA_CONTEXT_SUGGESTION_TOKENS,
                "backfill_on_startup": False,
            },
            id="ollama-free-text",
        ),
        pytest.param(
            "llama_cpp",
            {_FREETEXT_MODEL_PROMPT.format(_DESCRIPTION_LABEL): _TYPED_MODEL},
            ("m1",),
            (),  # nothing in the local GGUF caches: free-text entry
            {"enabled": True, "provider": "llama_cpp", "model": _TYPED_MODEL, "backfill_on_startup": False},
            id="llama_cpp-free-text",
        ),
    ],
)
def test_typed_model_for_descriptions_is_written(
    runner: CliRunner,
    tmp_path: Path,
    provider: str,
    typed_answers: Answers,
    ollama_models: tuple[str, ...],
    llama_cpp_models: tuple[str, ...],
    expected_section: dict[str, Any],
) -> None:
    """S45: a model typed at the free-text prompt (openai always, ollama and llama_cpp without a model list)
    reaches the TOML and loads."""
    config_path = tmp_path / "archon-search.toml"
    answers: Answers = {_DESCRIPTION_QUESTION: "y", _PROVIDER_PROMPT.format(_DESCRIPTION_LABEL): provider}
    answers |= typed_answers

    result = _wizard(runner, config_path, answers, ollama_models=ollama_models, llama_cpp_models=llama_cpp_models)

    assert result.exit_code == 0, f"Exit {result.exit_code}:\n{result.output}"
    assert _written(config_path)["description"] == expected_section
    assert load_config(config_path).description.model == expected_section["model"]


@pytest.mark.integration
def test_blank_openai_model_leaves_descriptions_off_through_the_wizard(runner: CliRunner, tmp_path: Path) -> None:
    """S45: openai with no model typed (blank twice) leaves descriptions disabled, says so and asks no backfill."""
    config_path = tmp_path / "archon-search.toml"
    answers: Answers = {
        _DESCRIPTION_QUESTION: "y",
        _PROVIDER_PROMPT.format(_DESCRIPTION_LABEL): "openai",
        _FREETEXT_MODEL_PROMPT.format(_DESCRIPTION_LABEL): ("", ""),
    }

    result = _wizard(runner, config_path, answers)

    assert result.exit_code == 0, f"Exit {result.exit_code}:\n{result.output}"
    assert "collection descriptions are left disabled" in result.output
    assert _DESCRIPTION_BACKFILL not in result.output
    assert _written(config_path)["description"] == {"enabled": False}


@pytest.mark.integration
@pytest.mark.parametrize("provider", ["openai", "ollama", "llama_cpp"])
@pytest.mark.parametrize("feature", ["HyDE", "RAG Fusion"])
def test_blank_model_leaves_hyde_or_rag_fusion_off_and_exits_zero(
    runner: CliRunner, tmp_path: Path, feature: str, provider: str
) -> None:
    """A blank model (Enter at every model prompt) for openai / ollama / llama_cpp used to be written as `model = ""`,
    and the wizard then exited 1 with `[hyde].model must be a non-empty string`, leaving that config on disk. Now the
    feature is left disabled like graph and descriptions: message, exit 0, only `enabled = false` written, the
    other feature keeps its answer, and the written file loads."""
    config_path = tmp_path / "archon-search.toml"
    other = "RAG Fusion" if feature == "HyDE" else "HyDE"
    answers: Answers = {
        _HYDE_QUESTION: "y",
        _PROVIDER_PROMPT.format(feature): provider,
        _PROVIDER_PROMPT.format(other): "anthropic",
        _FREETEXT_MODEL_PROMPT.format(feature): ("", ""),
    }

    result = _wizard(runner, config_path, answers, ollama_models=(), llama_cpp_models=())

    assert result.exit_code == 0, f"Exit {result.exit_code}:\n{result.output}"
    assert f"No model name was given for {provider}, so {feature} is left disabled." in result.output
    assert _CONTEXT_PROMPT.format(feature, OLLAMA_CONTEXT_SUGGESTION_TOKENS) not in result.output
    written = _written(config_path)
    section, other_section = ("hyde", "rag_fusion") if feature == "HyDE" else ("rag_fusion", "hyde")
    assert written[section] == {"enabled": False}
    assert written[other_section] == {"enabled": True}
    cfg = load_config(config_path)
    assert getattr(cfg, section).enabled is False
    assert getattr(cfg, other_section).enabled is True


@pytest.mark.integration
def test_blank_model_on_a_rerun_keeps_the_earlier_provider_block(runner: CliRunner, tmp_path: Path) -> None:
    """A re-run whose HyDE model is blank must not clobber the model an earlier run stored: only `enabled`
    flips to false, the provider and model keys stay."""
    config_path = tmp_path / "archon-search.toml"
    first = _wizard(
        runner,
        config_path,
        {
            _HYDE_QUESTION: "y",
            _PROVIDER_PROMPT.format("HyDE"): "openai",
            _FREETEXT_MODEL_PROMPT.format("HyDE"): _OPENAI_MODEL,
            _PROVIDER_PROMPT.format("RAG Fusion"): "anthropic",
        },
    )
    assert first.exit_code == 0, f"Exit {first.exit_code}:\n{first.output}"

    rerun = _wizard(
        runner,
        config_path,
        {
            _HYDE_QUESTION: "y",
            _PROVIDER_PROMPT.format("HyDE"): "openai",
            _FREETEXT_MODEL_PROMPT.format("HyDE"): ("", ""),
            _PROVIDER_PROMPT.format("RAG Fusion"): "anthropic",
        },
    )

    assert rerun.exit_code == 0, f"Exit {rerun.exit_code}:\n{rerun.output}"
    assert _written(config_path)["hyde"] == {"enabled": False, "provider": "openai", "model": _OPENAI_MODEL}


# ---------------------------------------------------------------------------
# S47: the install summary and the real extras pass
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_install_summary_lists_description_and_graph_bullets_before_proceed(runner: CliRunner, tmp_path: Path) -> None:
    """S47: the summary the wizard prints before `Proceed?` names the description provider and the graph provider."""
    answers = _describe("claude_cli")
    answers |= {
        _GRAPH_QUESTION: "y",
        _PROVIDER_PROMPT.format(_GRAPH_LABEL): "llama_cpp",
        _MODEL_PICK_PROMPT: ("1",),
    }

    result = _wizard(runner, tmp_path / "archon-search.toml", answers)

    assert result.exit_code == 0, f"Exit {result.exit_code}:\n{result.output}"
    _assert_in_order(
        result.output, [_BULLET_GRAPH.format("llama_cpp"), _BULLET_DESCRIPTION.format("claude_cli"), _PROCEED_PROMPT]
    )


@pytest.mark.integration
def test_install_summary_has_no_description_or_graph_bullet_when_declined(runner: CliRunner, tmp_path: Path) -> None:
    result = _wizard(runner, tmp_path / "archon-search.toml", {})

    assert result.exit_code == 0, f"Exit {result.exit_code}:\n{result.output}"
    assert "Collection descriptions: enabled" not in result.output
    assert "LLM graph enrichment: enabled" not in result.output


@pytest.mark.integration
@pytest.mark.parametrize(
    ("provider", "graph_answers", "expected_packages"),
    [
        pytest.param("anthropic", {_FREETEXT_MODEL_PROMPT.format(_GRAPH_LABEL): "haiku-x"}, [_HYDE_EXTRA], id="anthropic"),
        pytest.param("claude_cli", {_CLAUDE_MODEL_PROMPT.format(_GRAPH_LABEL): "haiku"}, [], id="claude_cli"),
        pytest.param("llama_cpp", {_MODEL_PICK_PROMPT: "1"}, [], id="llama_cpp"),
    ],
)
def test_graph_enrichment_installs_a_package_only_for_a_provider_that_has_one(
    runner: CliRunner, tmp_path: Path, provider: str, graph_answers: Answers, expected_packages: list[str]
) -> None:
    """S71: through the real extras pass and `_install_extra`, graph enrichment on anthropic installs the `hyde` extra
    once while `claude_cli` and `llama_cpp` install nothing (the anthropic row proves the install path is live)."""
    install = MagicMock()
    answers: Answers = {_GRAPH_QUESTION: "y", _PROVIDER_PROMPT.format(_GRAPH_LABEL): provider} | graph_answers

    result = _wizard(runner, tmp_path / "archon-search.toml", answers, install=install)

    assert result.exit_code == 0, f"Exit {result.exit_code}:\n{result.output}"
    assert _installed(install) == expected_packages
    assert _BULLET_GRAPH.format(provider) in result.output


@pytest.mark.integration
@pytest.mark.parametrize(
    ("provider", "extra"),
    [("anthropic", _HYDE_EXTRA), ("openai", _OPENAI_EXTRA), ("ollama", _OLLAMA_EXTRA)],
)
def test_failed_description_extra_prints_its_label_and_reverts_the_section(
    runner: CliRunner, tmp_path: Path, provider: str, extra: str
) -> None:
    """S47: a failed install of the description provider's package prints the `collection descriptions install
    failed` line naming the package, and leaves `[description]` as `enabled = false`."""
    config_path = tmp_path / "archon-search.toml"
    install = _failing(extra)

    result = _wizard(runner, config_path, _describe(provider), install=install)

    assert result.exit_code == 0, f"Exit {result.exit_code}:\n{result.output}"
    assert f"Warning: collection descriptions install failed for {extra}: boom" in result.output
    assert _installed(install) == [extra]
    assert "collection descriptions have been disabled" in result.output
    assert _written(config_path)["description"] == {"enabled": False}


@pytest.mark.integration
@pytest.mark.parametrize(
    ("failing_extra", "hyde_enabled", "description_enabled"),
    [(_OLLAMA_EXTRA, True, False), (_HYDE_EXTRA, False, True)],
    ids=["description-fails-hyde-stays", "hyde-fails-description-stays"],
)
def test_failed_extra_reverts_only_the_sections_that_needed_it_with_descriptions(
    runner: CliRunner, tmp_path: Path, failing_extra: str, hyde_enabled: bool, description_enabled: bool
) -> None:
    """S47: HyDE + RAG Fusion on anthropic (`hyde` extra) and descriptions on ollama (`ollama` extra) install one
    package each; the failed one reverts only its own sections."""
    config_path = tmp_path / "archon-search.toml"
    install = _failing(failing_extra)
    answers = _describe("ollama") | {
        _HYDE_QUESTION: "y",
        _PROVIDER_PROMPT.format("HyDE"): "anthropic",
        _PROVIDER_PROMPT.format("RAG Fusion"): "anthropic",
    }

    result = _wizard(runner, config_path, answers, install=install)

    assert result.exit_code == 0, f"Exit {result.exit_code}:\n{result.output}"
    assert _installed(install) == [_HYDE_EXTRA, _OLLAMA_EXTRA]
    written = _written(config_path)
    assert written["hyde"]["enabled"] is hyde_enabled
    assert written["rag_fusion"]["enabled"] is hyde_enabled
    assert written["description"]["enabled"] is description_enabled
    if description_enabled:
        assert (written["description"]["provider"], written["description"]["model"]) == ("ollama", "m1")


@pytest.mark.integration
@pytest.mark.parametrize("provider", ["anthropic", "openai", "llama_cpp", "claude_cli"])
@pytest.mark.parametrize(
    ("exit_code", "patches", "typed", "ended_by"),
    [
        pytest.param(
            1,
            {f"{_INSTALLER}._check_disk_space": MagicMock(side_effect=InstallError("not enough disk space"))},
            {},
            "not enough disk space",
            id="disk-space",
        ),
        pytest.param(1, {}, {_PROCEED_PROMPT: "n"}, "Installation aborted.", id="declined"),
    ],
)
def test_description_exits_revert_for_every_provider(
    runner: CliRunner,
    tmp_path: Path,
    provider: str,
    exit_code: int,
    patches: dict[str, Any],
    typed: dict[str, str],
    ended_by: str,
) -> None:
    """S47: the disk-space abort and the declined `Proceed?` leave `[description]` as `enabled = false`, with the
    provider block (model, llama-server address, backfill) gone, whichever provider was chosen."""
    config_path = tmp_path / "archon-search.toml"
    answers = _describe(provider, backfill="y") | typed

    result = _wizard(runner, config_path, answers, wizard_patches=patches | {_EXTRAS_PASS: MagicMock(return_value=[])})

    assert result.exit_code == exit_code, f"Exit {result.exit_code}:\n{result.output}"
    assert ended_by in result.output
    assert _written(config_path)["description"] == {"enabled": False}
    assert "collection descriptions have been disabled" in result.output


def _all_four_features() -> Answers:
    """HyDE on ollama, RAG Fusion on openai, graph on llama_cpp, descriptions on claude_cli (backfill yes)."""
    return (
        {
            _HYDE_QUESTION: "y",
            _PROVIDER_PROMPT.format("HyDE"): "ollama",
            _PROVIDER_PROMPT.format("RAG Fusion"): "openai",
            _FREETEXT_MODEL_PROMPT.format("RAG Fusion"): _OPENAI_MODEL,
            _GRAPH_QUESTION: "y",
            _PROVIDER_PROMPT.format(_GRAPH_LABEL): "llama_cpp",
            _MODEL_PICK_PROMPT: ("1", "1"),  # HyDE's Ollama model, then graph's llama.cpp model
        }
        | _describe("claude_cli", backfill="y")
    )


def _assert_every_section_reverted(config_path: Path, output: str) -> None:
    """The file holds none of the four features: HyDE, RAG Fusion and descriptions `enabled = false` with the
    provider block gone, no graph enrichment key."""
    written = _written(config_path)
    assert written["hyde"] == {"enabled": False}
    assert written["rag_fusion"] == {"enabled": False}
    assert written["description"] == {"enabled": False}
    assert not {"provider", "extraction_model", "llama_cpp_base_url"} & written.get("graph", {}).keys()
    for warning in (
        "HyDE / RAG Fusion have been disabled",
        "collection descriptions have been disabled",
        "LLM-backed graph enrichment has been disabled",
    ):
        assert warning in output


@pytest.mark.integration
def test_a_failed_shared_hyde_extra_reverts_all_four_features_it_served(runner: CliRunner, tmp_path: Path) -> None:
    """S47: HyDE, RAG Fusion, graph enrichment and descriptions all on anthropic share the `hyde` extra, which is
    installed once under one label naming all three needs; when it fails, EVERY one of the four sections is reverted
    in the written file (the model and backfill answers gone), with nothing else installed."""
    config_path = tmp_path / "archon-search.toml"
    install = _failing(_HYDE_EXTRA)
    answers: Answers = (
        {
            _HYDE_QUESTION: "y",
            _PROVIDER_PROMPT.format("HyDE"): "anthropic",
            _PROVIDER_PROMPT.format("RAG Fusion"): "anthropic",
            _GRAPH_QUESTION: "y",
            _PROVIDER_PROMPT.format(_GRAPH_LABEL): "anthropic",
            _FREETEXT_MODEL_PROMPT.format(_GRAPH_LABEL): "haiku-x",
        }
        | _describe("anthropic", backfill="y")
    )

    result = _wizard(runner, config_path, answers, install=install)

    assert result.exit_code == 0, f"Exit {result.exit_code}:\n{result.output}"
    assert _installed(install) == [_HYDE_EXTRA]
    assert (
        "Warning: AI query expansion, graph enrichment provider and collection descriptions install failed for "
        f"{_HYDE_EXTRA}: boom"
    ) in result.output
    _assert_every_section_reverted(config_path, result.output)


@pytest.mark.integration
def test_install_summary_lists_all_four_provider_bullets_in_order_before_proceed(
    runner: CliRunner, tmp_path: Path
) -> None:
    """S47: a run that wrote HyDE, RAG Fusion, graph enrichment and descriptions prints the four provider bullets,
    in that order, before `Proceed?`."""
    result = _wizard(runner, tmp_path / "archon-search.toml", _all_four_features())

    assert result.exit_code == 0, f"Exit {result.exit_code}:\n{result.output}"
    _assert_in_order(
        result.output,
        [
            _BULLET_HYDE.format("ollama"),
            _BULLET_RAG_FUSION.format("openai"),
            _BULLET_GRAPH.format("llama_cpp"),
            _BULLET_DESCRIPTION.format("claude_cli"),
            _PROCEED_PROMPT,
        ],
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("patches", "typed", "ended_by"),
    [
        pytest.param(
            {f"{_INSTALLER}._check_disk_space": MagicMock(side_effect=InstallError("not enough disk space"))},
            {},
            "not enough disk space",
            id="disk-space",
        ),
        pytest.param({}, {_PROCEED_PROMPT: "n"}, "Installation aborted.", id="declined"),
    ],
)
def test_exits_revert_every_section_of_a_run_that_wrote_hyde_graph_and_descriptions(
    runner: CliRunner, tmp_path: Path, patches: dict[str, Any], typed: dict[str, str], ended_by: str
) -> None:
    """S47: the disk-space abort and the declined `Proceed?` of a run that wrote HyDE, RAG Fusion, graph enrichment and
    descriptions together leave all four reverted, and install nothing."""
    config_path = tmp_path / "archon-search.toml"
    install = MagicMock()

    result = _wizard(runner, config_path, _all_four_features() | typed, install=install, wizard_patches=patches)

    assert result.exit_code == 1, f"Exit {result.exit_code}:\n{result.output}"
    assert ended_by in result.output
    assert _installed(install) == []
    _assert_every_section_reverted(config_path, result.output)


# ---------------------------------------------------------------------------
# S48 / S71: the shared-model warning and the stored window as the default
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_four_features_on_one_ollama_model_print_exactly_one_warning_block(runner: CliRunner, tmp_path: Path) -> None:
    """S48: HyDE, RAG Fusion, graph and descriptions share one Ollama model with four different windows; the
    wizard prints ONE warning block naming all four and the reload sentence once."""
    config_path = tmp_path / "archon-search.toml"
    answers: Answers = {
        _HYDE_QUESTION: "y",
        _PROVIDER_PROMPT.format("HyDE"): "ollama",
        _PROVIDER_PROMPT.format("RAG Fusion"): "ollama",
        _GRAPH_QUESTION: "y",
        _PROVIDER_PROMPT.format(_GRAPH_LABEL): "ollama",
        _DESCRIPTION_QUESTION: "y",
        _PROVIDER_PROMPT.format(_DESCRIPTION_LABEL): "ollama",
        _MODEL_PICK_PROMPT: ("1", "1", "1", "1"),
        _CONTEXT_PROMPT.format("HyDE", OLLAMA_CONTEXT_SUGGESTION_TOKENS): "4096",
        _CONTEXT_PROMPT.format("RAG Fusion", OLLAMA_CONTEXT_SUGGESTION_TOKENS): "",
        _CONTEXT_PROMPT.format(_GRAPH_LABEL, OLLAMA_CONTEXT_SUGGESTION_TOKENS): "16384",
        _CONTEXT_PROMPT.format(_DESCRIPTION_LABEL, OLLAMA_CONTEXT_SUGGESTION_TOKENS): "12288",
    }

    result = _wizard(runner, config_path, answers)

    assert result.exit_code == 0, f"Exit {result.exit_code}:\n{result.output}"
    assert result.output.count(_SHARED_MODEL_WARNING) == 1
    assert result.output.count(_RELOAD_SENTENCE) == 1
    assert (
        f"HyDE 4096, RAG Fusion {OLLAMA_CONTEXT_SUGGESTION_TOKENS}, graph enrichment 16384, "
        "collection descriptions 12288"
    ) in result.output
    cfg = load_config(config_path)
    assert (
        cfg.hyde.ollama_context_tokens,
        cfg.rag_fusion.ollama_context_tokens,
        cfg.graph.ollama_context_tokens,
        cfg.description.ollama_context_tokens,
    ) == (4096, OLLAMA_CONTEXT_SUGGESTION_TOKENS, 16384, 12288)


@pytest.mark.integration
def test_one_window_for_every_feature_prints_no_warning(runner: CliRunner, tmp_path: Path) -> None:
    """S48 negative: four features on one model with the SAME window never print the reload warning."""
    answers: Answers = {
        _HYDE_QUESTION: "y",
        _PROVIDER_PROMPT.format("HyDE"): "ollama",
        _PROVIDER_PROMPT.format("RAG Fusion"): "ollama",
        _GRAPH_QUESTION: "y",
        _PROVIDER_PROMPT.format(_GRAPH_LABEL): "ollama",
        _DESCRIPTION_QUESTION: "y",
        _PROVIDER_PROMPT.format(_DESCRIPTION_LABEL): "ollama",
        _MODEL_PICK_PROMPT: ("1", "1", "1", "1"),
    }

    result = _wizard(runner, tmp_path / "archon-search.toml", answers)

    assert result.exit_code == 0, f"Exit {result.exit_code}:\n{result.output}"
    assert _SHARED_MODEL_WARNING not in result.output


@pytest.mark.integration
def test_rerun_offers_the_stored_window_for_hyde_rag_fusion_and_graph(runner: CliRunner, tmp_path: Path) -> None:
    """S48/S71: on a re-run each Ollama feature's context question shows the window stored by the first run as its
    default (the scripted prompt text carries the value, so a wrong default fails as "never asked"), and Enter
    keeps it."""
    config_path = tmp_path / "archon-search.toml"

    def _answers(windows: dict[str, str], shown: dict[str, int | None]) -> Answers:
        return {
            _HYDE_QUESTION: "y",
            _PROVIDER_PROMPT.format("HyDE"): "ollama",
            _PROVIDER_PROMPT.format("RAG Fusion"): "ollama",
            _GRAPH_QUESTION: "y",
            _PROVIDER_PROMPT.format(_GRAPH_LABEL): "ollama",
            _MODEL_PICK_PROMPT: ("1", "1", "1"),
            **{
                _CONTEXT_PROMPT.format(label, shown[label] or OLLAMA_CONTEXT_SUGGESTION_TOKENS): typed
                for label, typed in windows.items()
            },
        }

    first = _wizard(
        runner,
        config_path,
        _answers({label: str(tokens) for label, tokens in _STORED_WINDOWS.items()}, dict.fromkeys(_STORED_WINDOWS)),
    )
    assert first.exit_code == 0, f"Exit {first.exit_code}:\n{first.output}"

    rerun = _wizard(runner, config_path, _answers(dict.fromkeys(_STORED_WINDOWS, ""), _STORED_WINDOWS))

    assert rerun.exit_code == 0, f"Exit {rerun.exit_code}:\n{rerun.output}"
    cfg = load_config(config_path)
    assert (cfg.hyde.ollama_context_tokens, cfg.rag_fusion.ollama_context_tokens, cfg.graph.ollama_context_tokens) == (
        _STORED_WINDOWS["HyDE"],
        _STORED_WINDOWS["RAG Fusion"],
        _STORED_WINDOWS[_GRAPH_LABEL],
    )
