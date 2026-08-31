"""Tests for BE-5 — the one generic license rule in archon_search/install/licenses.py.

Dispatches on ArtifactSpec.license_disposition: disclose_only artifacts are disclosed with
no prompt; prompt_for_acceptance artifacts gate on user/flag acceptance, mirroring
_prompt_jina_license / _prompt_fasttext_license's accept/decline/non-interactive semantics.
"""
from __future__ import annotations

import dataclasses
import inspect

import pytest

from archon_search.install.licenses import apply_license_rule
from archon_search.install.provisioning import ArtifactSpec, LicenseDisposition

PERMISSIVE_SPEC = ArtifactSpec(
    name="synthetic-permissive-model",
    url="https://example.invalid/permissive.bin",
    revision="rev1",
    sha256="0" * 64,
    size_bytes=123,
    license="Apache-2.0",
    license_disposition=LicenseDisposition.disclose_only,
    attribution="",
)

RESTRICTIVE_SPEC = ArtifactSpec(
    name="synthetic-restrictive-model",
    url="https://example.invalid/restrictive.bin",
    revision="rev1",
    sha256="1" * 64,
    size_bytes=456,
    license="CC-BY-NC-4.0",
    license_disposition=LicenseDisposition.prompt_for_acceptance,
    attribution="",
)


def test_permissive_descriptor_discloses_without_prompting(monkeypatch, capsys):
    """An Apache-2.0 / disclose_only descriptor gates nothing: no prompt, no SystemExit."""
    called = []
    monkeypatch.setattr("builtins.input", lambda _: called.append(True) or "")
    apply_license_rule(PERMISSIVE_SPEC, non_interactive=True, accept_license=False)
    assert called == [], "input() must not be called for a disclose_only descriptor"
    captured = capsys.readouterr()
    assert "Apache-2.0" in captured.out


@pytest.mark.parametrize(
    ("non_interactive", "accept_license", "input_value", "expect_exit"),
    [
        (False, True, None, False),  # accepted via flag
        (True, True, None, False),  # accepted via flag, non-interactive
        (True, False, None, True),  # non-interactive, no flag -> declined
        (False, False, "accept", False),  # interactive accept
        (False, False, "no", True),  # interactive decline
    ],
)
def test_restrictive_descriptor_prompts_for_acceptance(
    monkeypatch, capsys, non_interactive, accept_license, input_value, expect_exit
):
    """A prompt_for_acceptance descriptor gates on accept/decline/non-interactive, like the legacy prompts."""
    if input_value is not None:
        monkeypatch.setattr("builtins.input", lambda _: input_value)

    if expect_exit:
        with pytest.raises(SystemExit) as exc_info:
            apply_license_rule(
                RESTRICTIVE_SPEC, non_interactive=non_interactive, accept_license=accept_license
            )
        assert exc_info.value.code == 1
    else:
        apply_license_rule(
            RESTRICTIVE_SPEC, non_interactive=non_interactive, accept_license=accept_license
        )

    captured = capsys.readouterr()
    assert "WARNING" in captured.out
    assert "CC-BY-NC-4.0" in captured.out
    if input_value == "no":
        assert "License not accepted. Aborting." in captured.out


def test_one_rule_disciplines_both_synthetic_descriptors(monkeypatch):
    """Both a permissive and a restrictive synthetic ArtifactSpec run through the SAME rule
    function, with no per-model/per-name branching in the implementation."""
    calls = []
    monkeypatch.setattr("builtins.input", lambda prompt: calls.append(prompt) or "accept")

    # The permissive call must never touch input() at all.
    apply_license_rule(PERMISSIVE_SPEC, non_interactive=False, accept_license=False)
    assert calls == [], "disclose_only must not prompt"

    # The restrictive call must prompt (and, given 'accept', must not raise).
    apply_license_rule(RESTRICTIVE_SPEC, non_interactive=False, accept_license=False)
    assert len(calls) == 1, "prompt_for_acceptance must prompt exactly once"

    # Structural guard: the implementation may only branch on license_disposition — no
    # per-model/per-name comparisons (e.g. spec.name == ... or a hardcoded model/license string).
    source = inspect.getsource(apply_license_rule)
    assert "spec.name ==" not in source
    assert "spec.license ==" not in source
    for forbidden in ("synthetic-permissive-model", "synthetic-restrictive-model", "Apache-2.0", "CC-BY-NC-4.0"):
        assert forbidden not in source


def test_unrecognized_disposition_raises():
    """A license_disposition outside the known enum members raises rather than silently
    falling through to the restrictive prompt branch."""
    bogus_spec = dataclasses.replace(RESTRICTIVE_SPEC, license_disposition="unknown_disposition")
    with pytest.raises((ValueError, AssertionError)):
        apply_license_rule(bogus_spec, non_interactive=True, accept_license=False)
