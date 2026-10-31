"""FE-9 (C4, S72): ``config`` knows the ``[description]`` section; the shipped TOML template and manual document it."""
from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import tomlkit
from click.testing import CliRunner

from archon_search.cli.config_cmd import _default_toml
from archon_search.cli.config_cmd import config as config_group
from archon_search.config import DescriptionConfig, load_config
from archon_search.install.wizard import OLLAMA_CONTEXT_SUGGESTION_TOKENS

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "archon-search.toml.example"
MANUAL = REPO_ROOT / "Documentation" / "UserManual" / "30_configuration.md"
CONTEXT_SECTIONS = ("hyde", "rag_fusion", "graph", "description")
# The one value the template shows that is not the default: the suggested Ollama window.
SUGGESTED_OLLAMA_CONTEXT_TOKENS = OLLAMA_CONTEXT_SUGGESTION_TOKENS
COMMENTED_KEY = re.compile(r"^# (\w+) = (.+)$", flags=re.MULTILINE)


def _template_section(name: str) -> str:
    """Return the template text from ``[name]`` up to the next table header."""
    text = TEMPLATE.read_text(encoding="utf-8")
    match = re.search(rf"^\[{name}\]\n(.*?)(?=^\[|\Z)", text, flags=re.DOTALL | re.MULTILINE)
    assert match, f"[{name}] missing from the template"
    return match.group(1)


def _manual_section(heading: str) -> str:
    """Return the manual text under the ``### <heading>`` line, up to the next heading."""
    text = MANUAL.read_text(encoding="utf-8")
    match = re.search(rf"^### {re.escape(heading)}\n(.*?)(?=^##|\Z)", text, flags=re.DOTALL | re.MULTILINE)
    assert match, f"{heading} missing from the manual"
    return match.group(1)


def test_default_toml_includes_description_enabled_false() -> None:
    doc = tomlkit.parse(_default_toml())

    assert doc["description"]["enabled"] is False


def test_config_set_description_enabled_validates(tmp_path: Path) -> None:
    path = tmp_path / "archon-search.toml"
    runner = CliRunner()

    accepted = runner.invoke(config_group, ["set", "description.enabled", "true", "--config", str(path)])
    assert accepted.exit_code == 0, accepted.output
    assert load_config(path).description.enabled is True

    before = path.read_text(encoding="utf-8")
    rejected = runner.invoke(config_group, ["set", "description.enabled", "maybe", "--config", str(path)])
    assert rejected.exit_code == 1
    assert "[description].enabled" in rejected.stderr
    assert path.read_text(encoding="utf-8") == before


def test_config_set_description_think_accepts_booleans_and_default_and_rejects_other_text(tmp_path: Path) -> None:
    path = tmp_path / "archon-search.toml"
    runner = CliRunner()

    assert runner.invoke(config_group, ["set", "description.think", "false", "--config", str(path)]).exit_code == 0
    assert load_config(path).description.think is False
    assert runner.invoke(config_group, ["set", "description.think", "default", "--config", str(path)]).exit_code == 0
    assert load_config(path).description.think is None

    before = path.read_text(encoding="utf-8")
    rejected = runner.invoke(config_group, ["set", "description.think", "sometimes", "--config", str(path)])
    assert rejected.exit_code == 1
    assert "[description].think" in rejected.stderr
    assert path.read_text(encoding="utf-8") == before


def test_template_loads_and_leaves_description_off() -> None:
    assert load_config(TEMPLATE).description == DescriptionConfig()


def test_template_description_section_lists_every_key() -> None:
    section = _template_section("description")

    for field in dataclasses.fields(DescriptionConfig):
        assert re.search(rf"^#? ?{field.name} = ", section, flags=re.MULTILINE), f"[description].{field.name} not in template"


def test_template_commented_description_examples_load_and_match_the_defaults(tmp_path: Path) -> None:
    """Uncommenting every example line keeps the file loadable and shows the defaults (except the suggested window)."""
    section = _template_section("description")
    fields = {field.name for field in dataclasses.fields(DescriptionConfig)}
    examples: dict[str, str] = {}
    for key, value in COMMENTED_KEY.findall(section):
        try:
            tomlkit.parse(f"{key} = {value}")
        except tomlkit.exceptions.TOMLKitError:
            continue  # prose that merely starts like an assignment
        if key in fields:
            examples[key] = value
    body = "\n".join(f"{key} = {value}" for key, value in examples.items())
    path = tmp_path / "archon-search.toml"
    path.write_text(f"[description]\nenabled = false\n{body}\n", encoding="utf-8")

    assert load_config(path).description == dataclasses.replace(
        DescriptionConfig(), ollama_context_tokens=SUGGESTED_OLLAMA_CONTEXT_TOKENS
    )


def test_template_description_privacy_paragraph_names_only_the_cloud_providers() -> None:
    section = _template_section("description")
    paragraph = re.search(r"^# PRIVACY:.*?(?=^#$)", section, flags=re.DOTALL | re.MULTILINE)
    assert paragraph, "no PRIVACY paragraph"
    leaving = re.search(r"leave this machine when provider = (.*?\")\.", paragraph.group(0), flags=re.DOTALL)
    assert leaving, "the paragraph does not say which providers send excerpts away"

    assert all(provider in leaving.group(1) for provider in ("anthropic", "openai", "claude_cli"))
    assert not any(provider in leaving.group(1) for provider in ("ollama", "llama_cpp"))


def test_template_documents_ollama_context_tokens_in_every_context_section() -> None:
    for name in CONTEXT_SECTIONS:
        section = _template_section(name)

        assert f"# ollama_context_tokens = {SUGGESTED_OLLAMA_CONTEXT_TOKENS}" in section, name
        assert "4096" in section, f"[{name}] does not state the assumed window"


def test_template_graph_claude_cli_comment_lists_all_five_providers_and_allows_the_model_to_be_left_out() -> None:
    graph = _template_section("graph")
    provider_block = graph.split("# provider = ", 1)[0].rsplit("# Optional: enrichment provider", 1)[1]
    model_block = graph.split("# extraction_model = ", 1)[0].rsplit("# Model used for community summarisation", 1)[1]

    for provider in ("anthropic", "ollama", "openai", "llama_cpp", "claude_cli"):
        assert f'"{provider}"' in provider_block, provider
    assert "four" not in provider_block.lower()
    assert re.search(r'For "claude_cli".*may be\s+#\s+left out', model_block, flags=re.DOTALL)


def _manual_default_cell(value: object) -> str:
    """Render *value* the way the manual's Default column does (``unset`` for ``None``)."""
    if value is None:
        return "unset"
    return f"`{str(value).lower() if isinstance(value, bool) else value}`"


def test_manual_description_table_lists_every_key_with_its_default() -> None:
    section = _manual_section("`[description]`")
    defaults = dataclasses.asdict(DescriptionConfig())

    for name, value in defaults.items():
        cell = "`default`" if name == "think" else _manual_default_cell(value)  # "default" is the file spelling of None
        assert f"| `{name}` | {cell} |" in section, f"[description].{name} default not in the manual"


def test_manual_documents_ollama_context_tokens_in_every_context_section() -> None:
    for heading in ("`[hyde]` and `[rag_fusion]`", "`[description]`", "`[graph]`"):
        assert "| `ollama_context_tokens` |" in _manual_section(heading), heading
