"""Unit tests for .github/workflows/archon-search-release.yml structure.

These tests parse the YAML and assert structural invariants of the CI
configuration — no mocking, no network calls.
"""

from pathlib import Path

import pytest
import yaml

WORKFLOW_PATH = (
    Path(__file__).parent.parent
    / ".github"
    / "workflows"
    / "archon-search-release.yml"
)


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text())


@pytest.fixture(scope="module")
def workflow_text() -> str:
    return WORKFLOW_PATH.read_text()


@pytest.fixture(scope="module")
def docker_job(workflow: dict) -> dict:
    """The `docker` job that builds and pushes CPU + GPU images to GHCR."""
    return workflow["jobs"]["docker"]


@pytest.fixture(scope="module")
def docker_steps(docker_job: dict) -> list[dict]:
    return docker_job["steps"]


def test_yaml_is_valid(workflow: dict) -> None:
    """YAML must parse without errors."""
    assert isinstance(workflow, dict)


def test_github_release_job_present(workflow: dict) -> None:
    """jobs['github-release'] key must exist."""
    assert "github-release" in workflow["jobs"]


def test_github_release_needs_publish(workflow: dict) -> None:
    """github-release job must declare 'publish' in its needs."""
    needs = workflow["jobs"]["github-release"]["needs"]
    # needs can be a string or a list
    if isinstance(needs, str):
        assert needs == "publish"
    else:
        assert "publish" in needs


def test_github_release_permissions_contents_write(workflow: dict) -> None:
    """Job-level permissions must include contents: write and must NOT include id-token."""
    perms = workflow["jobs"]["github-release"]["permissions"]
    assert perms.get("contents") == "write", "contents must be 'write'"
    assert "id-token" not in perms, "id-token must not be set at github-release job level"


def test_github_release_if_condition(workflow: dict) -> None:
    """github-release job must have the correct if condition."""
    if_cond = workflow["jobs"]["github-release"]["if"]
    assert if_cond == "startsWith(github.ref, 'refs/tags/')"


def test_no_workflow_level_contents_write(workflow: dict) -> None:
    """Top-level permissions must NOT include contents: write."""
    top_perms = workflow.get("permissions")
    if top_perms is None:
        # No top-level permissions key at all — that satisfies the constraint.
        return
    if isinstance(top_perms, str):
        # e.g. permissions: read-all — contents: write is not set
        # write-all grants contents:write implicitly, so also reject it
        assert top_perms not in ("write", "write-all"), (
            f"Workflow top-level permissions must not grant contents: write, got: {top_perms!r}"
        )
        return
    assert top_perms.get("contents") != "write", (
        "Workflow top-level permissions must not grant contents: write"
    )


# ---------------------------------------------------------------------------
# `docker` job — builds CPU + GPU images and pushes them to GHCR on tag push.
# ---------------------------------------------------------------------------


def test_docker_job_present(workflow: dict) -> None:
    """jobs['docker'] key must exist."""
    assert "docker" in workflow["jobs"], (
        "release workflow must declare a `docker` job that builds and pushes "
        "CPU + GPU images to GHCR"
    )


def test_docker_job_needs_test(docker_job: dict) -> None:
    """docker job must run after the test job (not after publish).

    The image copies local source via `COPY .`; PyPI is irrelevant to the
    image build, so the docker job gates on the test job only.
    """
    needs = docker_job["needs"]
    if isinstance(needs, str):
        assert needs == "test"
    else:
        assert "test" in needs


def test_docker_job_runs_on_ubuntu(docker_job: dict) -> None:
    assert docker_job["runs-on"] == "ubuntu-latest"


def test_docker_job_uses_checkout_v4_with_full_history(docker_steps: list[dict]) -> None:
    """First step must check out the repo with fetch-depth: 0 so hatch-vcs
    can resolve the version from the tag, and so `COPY .` sees the tagged
    sources."""
    checkout_steps = [s for s in docker_steps if s.get("uses", "").startswith("actions/checkout")]
    assert checkout_steps, "docker job must include actions/checkout"
    first = checkout_steps[0]
    assert first["uses"] == "actions/checkout@v4"
    assert first.get("with", {}).get("fetch-depth") == 0


def test_docker_job_uses_buildx_setup(docker_steps: list[dict]) -> None:
    """docker/setup-buildx-action@v3 must be configured for multi-arch / cache."""
    uses = [s.get("uses", "") for s in docker_steps]
    assert any(u.startswith("docker/setup-buildx-action@v3") for u in uses), (
        "docker job must include docker/setup-buildx-action@v3"
    )


def test_docker_job_logs_in_to_ghcr(docker_steps: list[dict]) -> None:
    """docker/login-action@v3 must log in to ghcr.io with GITHUB_TOKEN."""
    login_steps = [
        s for s in docker_steps if s.get("uses", "").startswith("docker/login-action@v3")
    ]
    assert login_steps, "docker job must include docker/login-action@v3"
    login = login_steps[0]
    with_block = login.get("with", {})
    assert with_block.get("registry") == "ghcr.io"
    assert with_block.get("username") == "${{ github.actor }}"
    assert with_block.get("password") == "${{ secrets.GITHUB_TOKEN }}"


def test_docker_job_builds_cpu_image_with_base_image_python(workflow_text: str) -> None:
    """CPU build step must pass --build-arg BASE_IMAGE=python:3.12-slim."""
    assert "--build-arg BASE_IMAGE=python:3.12-slim" in workflow_text, (
        "docker job must build the CPU image with BASE_IMAGE=python:3.12-slim"
    )


def test_docker_job_builds_gpu_image_with_base_image_nvidia(workflow_text: str) -> None:
    """GPU build step must pass --build-arg BASE_IMAGE=nvidia/cuda:...-ubuntu22.04."""
    assert (
        "--build-arg BASE_IMAGE=nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04"
        in workflow_text
    ), (
        "docker job must build the GPU image with the verified NVIDIA CUDA tag "
        "(nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04)"
    )


def test_docker_job_passes_git_commit_to_both_builds(workflow_text: str) -> None:
    """Both build steps must pass --build-arg GIT_COMMIT=${{ github.sha }}.

    The Dockerfile uses this to set
    LABEL org.opencontainers.image.revision=$GIT_COMMIT so `docker inspect`
    surfaces which commit produced the image — required to detect stale
    `:gpu` tags when the GPU build silently fails.
    """
    # Expect at least two occurrences: one per build step (CPU and GPU).
    count = workflow_text.count("--build-arg GIT_COMMIT=${{ github.sha }}")
    assert count >= 2, (
        f"both CPU and GPU build steps must pass --build-arg GIT_COMMIT=${{{{ github.sha }}}}; "
        f"found {count} occurrence(s)"
    )


def test_docker_job_tags_cpu_with_tag_and_latest(workflow_text: str) -> None:
    """CPU build must push both the tag-specific and `:latest` tags."""
    assert (
        "ghcr.io/${{ github.repository_owner }}/archon-search:$TAG" in workflow_text
    ), "CPU build must tag the image with the release tag"
    assert (
        "ghcr.io/${{ github.repository_owner }}/archon-search:latest" in workflow_text
    ), "CPU build must also push the floating :latest tag"


def test_docker_job_tags_gpu_with_tag_gpu_and_gpu_floating(workflow_text: str) -> None:
    """GPU build must push both the tag-specific (`-gpu` suffix) and `:gpu` tags."""
    assert (
        "ghcr.io/${{ github.repository_owner }}/archon-search:$TAG-gpu" in workflow_text
    ), "GPU build must tag the image with the release tag suffixed with -gpu"
    assert (
        "ghcr.io/${{ github.repository_owner }}/archon-search:gpu" in workflow_text
    ), "GPU build must also push the floating :gpu tag"


def test_docker_job_pushes_images(workflow_text: str) -> None:
    """Both build commands must use `docker buildx build --push`."""
    # Expect at least two `--push` flags (one per build step).
    assert workflow_text.count("--push") >= 2, (
        "both CPU and GPU build steps must use `docker buildx build --push`"
    )


def test_docker_job_gpu_step_has_continue_on_error(docker_steps: list[dict]) -> None:
    """GPU build step must use `continue-on-error: true` so a missing GPU
    runner or transient NVIDIA registry failure does not abort the release.
    """
    gpu_build_steps = [
        s
        for s in docker_steps
        if "run" in s
        and "nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04" in s["run"]
        and "buildx build" in s["run"]
    ]
    assert gpu_build_steps, "GPU build step must exist (with `docker buildx build`)"
    assert any(s.get("continue-on-error") is True for s in gpu_build_steps), (
        "GPU build step must declare `continue-on-error: true`"
    )
    # Each GPU build step should have an id so a follow-up annotation step
    # can reference its outcome via `steps.<id>.outcome`.
    assert any("id" in s for s in gpu_build_steps), (
        "GPU build step must declare an `id` so a follow-up annotation step "
        "can detect failure via steps.<id>.outcome"
    )


def test_docker_job_annotates_gpu_failure(workflow_text: str) -> None:
    """A follow-up step must add a $GITHUB_STEP_SUMMARY annotation when the
    GPU build fails so operators are not surprised by a stale `:gpu` tag.
    """
    assert "$GITHUB_STEP_SUMMARY" in workflow_text, (
        "docker job must annotate a GPU build failure via $GITHUB_STEP_SUMMARY"
    )
    assert "GPU image build failed" in workflow_text, (
        "docker job must include the operator-facing failure message "
        "'GPU image build failed' in the summary annotation"
    )


def test_docker_job_only_runs_on_tag_push(docker_job: dict) -> None:
    """The docker job must be gated on tag refs.

    Without this, a `workflow_dispatch` from a branch would waste a runner
    (best case: the resolve-tag step exits 1) and the workflow contract
    would diverge from the `github-release` job which uses the same gate.
    """
    if_cond = docker_job.get("if")
    assert if_cond == "startsWith(github.ref, 'refs/tags/')", (
        "docker job must declare "
        "`if: startsWith(github.ref, 'refs/tags/')` to match the "
        "github-release gate and avoid running on branch-targeted "
        "workflow_dispatch invocations"
    )


def test_docker_job_has_packages_write_permission(docker_job: dict) -> None:
    """Pushing to GHCR requires `packages: write`.

    Without this, every release would fail with HTTP 403 on the first push.
    `contents: read` is also required so `actions/checkout` can fetch the
    tagged tree.
    """
    perms = docker_job.get("permissions", {})
    assert perms.get("packages") == "write", (
        "docker job must declare `packages: write` permission for GHCR push"
    )
    assert perms.get("contents") == "read", (
        "docker job must declare `contents: read` permission for checkout"
    )


def test_docker_job_gpu_annotation_step_is_conditional(docker_steps: list[dict]) -> None:
    """The GPU failure annotation must only fire when the GPU build failed.

    Without an `if:` gate on the annotation step, the annotation would be
    posted on every successful run too — defeating its purpose of flagging
    silently-failing GPU builds and conditioning operators to ignore the
    warning.
    """
    annotation_steps = [
        s
        for s in docker_steps
        if "run" in s and "GPU image build failed" in s.get("run", "")
    ]
    assert annotation_steps, "GPU failure annotation step must exist"
    assert len(annotation_steps) == 1, (
        "expected exactly one GPU failure annotation step"
    )
    annotation = annotation_steps[0]
    if_cond = annotation.get("if", "")
    assert "gpu_build" in if_cond and "failure" in if_cond, (
        "GPU annotation step must be gated on `steps.gpu_build.outcome == "
        "'failure'` (or equivalent expression referencing the gpu_build "
        f"step's failure outcome); got: {if_cond!r}"
    )


def test_docker_job_does_not_run_smoke_test(workflow: dict, workflow_text: str) -> None:
    """The docker smoke test (tests/test_docker_smoke.py / -m docker) is a
    developer tool, not a release gate. The plan explicitly excludes it
    from the release pipeline and requires the decision to be documented
    in the workflow.
    """
    # No step may shell out to pytest with the docker marker.
    for job_name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            run = step.get("run", "") or ""
            assert "-m docker" not in run, (
                f"job '{job_name}' step '{step.get('name', '?')}' must not invoke "
                f"the docker smoke test (`-m docker`)"
            )
            assert "test_docker_smoke" not in run, (
                f"job '{job_name}' step '{step.get('name', '?')}' must not invoke "
                f"`tests/test_docker_smoke.py`"
            )
    # A short comment must explain why the smoke test is intentionally skipped.
    assert "smoke" in workflow_text.lower(), (
        "release workflow must contain a comment explaining why the docker "
        "smoke test is intentionally excluded from CI"
    )
