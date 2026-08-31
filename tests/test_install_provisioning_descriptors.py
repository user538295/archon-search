"""Tests for BE-3 — the two model-artifact descriptor shapes and the frozen C4 constants."""
from __future__ import annotations

import dataclasses

import pytest

from archon_search.install.extras import CODE_EXTRA_SIZE_ESTIMATE, GRAPH_MODEL_SIZE_ESTIMATE
from archon_search.install.licenses import FASTTEXT_ARTIFACT, FASTTEXT_MODEL_URL
from archon_search.install.provisioning import (
    DEVICE_PROBE_NOT_YET_VALIDATED,
    TORCH_DEVICE_CPU,
    TORCH_DEVICE_CUDA,
    TORCH_DEVICE_MPS,
    ArtifactSpec,
    LicenseDisposition,
    ProvisionFailureKind,
    SizeEstimateSpec,
)


def test_provisioned_descriptor_carries_digest_and_exact_bytes():
    """lid.176.ftz declares a real digest and exact byte count; the estimate shape declares neither."""
    assert isinstance(FASTTEXT_ARTIFACT, ArtifactSpec)
    assert FASTTEXT_ARTIFACT.url == FASTTEXT_MODEL_URL
    assert len(FASTTEXT_ARTIFACT.sha256) == 64
    assert FASTTEXT_ARTIFACT.sha256 == "8f3472cfe8738a7b6099e8e999c3cbfae0dcd15696aac7d7738a8039db603e83"
    assert FASTTEXT_ARTIFACT.size_bytes == 938013

    estimate_fields = {f.name for f in dataclasses.fields(SizeEstimateSpec)}
    assert "sha256" not in estimate_fields
    assert "size_bytes" not in estimate_fields
    assert estimate_fields == {"name", "declared_mb"}


def test_graph_model_uses_the_estimate_shape_not_provisioned():
    """The graph model reverts to declared-MB fidelity — no url/revision/digest."""
    assert isinstance(GRAPH_MODEL_SIZE_ESTIMATE, SizeEstimateSpec)
    assert not isinstance(GRAPH_MODEL_SIZE_ESTIMATE, ArtifactSpec)
    assert GRAPH_MODEL_SIZE_ESTIMATE.declared_mb > 0
    for absent in ("url", "revision", "sha256", "size_bytes"):
        assert not hasattr(GRAPH_MODEL_SIZE_ESTIMATE, absent)

    assert isinstance(CODE_EXTRA_SIZE_ESTIMATE, SizeEstimateSpec)


def test_failure_category_enum_has_exactly_eight_members():
    """The frozen set matches C4 so Frontend can code against it."""
    assert len(ProvisionFailureKind) == 8
    assert {member.value for member in ProvisionFailureKind} == {
        "download_failed",
        "digest_mismatch",
        "size_mismatch",
        "insufficient_disk",
        "extract_failed",
        "smoke_load_failed",
        "relations_not_supported",
        "conflicting_onnx_runtimes",
    }


def test_license_disposition_has_the_two_c4_members():
    """The license rule has exactly two dispositions."""
    assert {member.value for member in LicenseDisposition} == {
        "prompt_for_acceptance",
        "disclose_only",
    }


def test_device_probe_constants_are_frozen():
    """BE-14/FE-5 code against these symbols rather than re-inventing the literals."""
    assert (TORCH_DEVICE_CPU, TORCH_DEVICE_CUDA, TORCH_DEVICE_MPS) == ("cpu", "cuda", "mps")
    assert DEVICE_PROBE_NOT_YET_VALIDATED == "not_yet_validated"


def test_descriptor_shapes_are_immutable():
    """Both descriptors are frozen dataclasses."""
    for spec in (FASTTEXT_ARTIFACT, GRAPH_MODEL_SIZE_ESTIMATE):
        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.name = "mutated"  # type: ignore[misc]
