"""Static linting of the docker-compose.yml + .env.example deliverables (Task 4.2).

These tests do not invoke `docker compose` — they only inspect the on-disk
files so they are safe to run on machines without a Docker daemon. They
guard the structural invariants required by the C9 container-support plan:
three services (dev/test/prod) with separate named volumes, unique port
mappings, `stop_grace_period: 30s`, API key via variable substitution
(never hardcoded), and the LanceDB single-writer / no-volume caveats
documented in comments.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

EXPECTED_SERVICES = ("archon-dev", "archon-test", "archon-prod")
EXPECTED_VOLUMES = ("archon-dev-data", "archon-test-data", "archon-prod-data")
EXPECTED_PORT_MAP = {
    "archon-dev": ("18765", "8765"),
    "archon-test": ("18766", "8765"),
    "archon-prod": ("8765", "8765"),
}


@pytest.fixture(scope="module")
def compose_text() -> str:
    return COMPOSE_FILE.read_text()


@pytest.fixture(scope="module")
def compose_doc(compose_text: str) -> dict:
    return yaml.safe_load(compose_text)


@pytest.fixture(scope="module")
def env_example_text() -> str:
    return ENV_EXAMPLE.read_text()


# ---------------------------------------------------------------------------
# Existence
# ---------------------------------------------------------------------------


def test_compose_file_exists() -> None:
    assert COMPOSE_FILE.exists(), f"{COMPOSE_FILE} not found"


def test_env_example_exists() -> None:
    # The plan mandates a `.env.example` companion file so operators know
    # which variables they can override.
    assert ENV_EXAMPLE.exists(), f"{ENV_EXAMPLE} not found"


# ---------------------------------------------------------------------------
# Services (spec: three named services dev/test/prod)
# ---------------------------------------------------------------------------


def test_compose_has_three_services(compose_doc: dict) -> None:
    services = compose_doc.get("services", {})
    assert isinstance(services, dict), "`services` must be a mapping"
    for name in EXPECTED_SERVICES:
        assert name in services, f"service `{name}` must be declared"


# ---------------------------------------------------------------------------
# Volumes (spec: separate named volumes per service)
# ---------------------------------------------------------------------------


def test_compose_separate_named_volumes(compose_doc: dict) -> None:
    services = compose_doc["services"]
    mounted: dict[str, str] = {}
    for svc_name in EXPECTED_SERVICES:
        volumes = services[svc_name].get("volumes", [])
        assert volumes, f"service `{svc_name}` must mount at least one volume"
        # Find the entry that targets `/data` (the canonical data mount).
        # Iterating instead of indexing [0] guards against future edits
        # that reorder the volumes list.
        data_mount: str | None = None
        for entry in volumes:
            assert isinstance(entry, str), (
                f"service `{svc_name}` must use the short-syntax volume entry"
            )
            name, _, container_path = entry.partition(":")
            # Strip any options suffix (e.g., `:ro`) before comparing.
            container_path = container_path.split(":")[0]
            if container_path == "/data":
                data_mount = name
                break
        assert data_mount is not None, (
            f"service `{svc_name}` must mount a named volume at /data exactly "
            f"(got {volumes!r})"
        )
        mounted[svc_name] = data_mount
    # All three names must be distinct.
    assert len(set(mounted.values())) == 3, (
        f"each service must mount its own named volume; got {mounted}"
    )


def test_compose_volumes_declared(compose_doc: dict) -> None:
    top_level_volumes = compose_doc.get("volumes", {})
    assert isinstance(top_level_volumes, dict), (
        "top-level `volumes:` block must be a mapping"
    )
    for name in EXPECTED_VOLUMES:
        assert name in top_level_volumes, (
            f"named volume `{name}` must be declared at the top level"
        )


# ---------------------------------------------------------------------------
# Stop grace period (spec invariant: 30s on every service)
# ---------------------------------------------------------------------------


def test_compose_stop_grace_period(compose_doc: dict) -> None:
    services = compose_doc["services"]
    for svc_name in EXPECTED_SERVICES:
        svc = services[svc_name]
        grace = svc.get("stop_grace_period")
        assert grace == "30s", (
            f"service `{svc_name}` must declare `stop_grace_period: 30s` "
            f"(got {grace!r})"
        )


# ---------------------------------------------------------------------------
# Port mapping (dev 18765, test 18766, prod 8765)
# ---------------------------------------------------------------------------


def test_compose_port_mappings(compose_doc: dict) -> None:
    services = compose_doc["services"]
    for svc_name, (host_port, container_port) in EXPECTED_PORT_MAP.items():
        ports = services[svc_name].get("ports", [])
        assert ports, f"service `{svc_name}` must declare a port mapping"
        first = ports[0]
        assert isinstance(first, str), (
            f"service `{svc_name}` must use short-syntax port mapping"
        )
        host_side, _, container_side = first.partition(":")
        assert host_side == host_port and container_side == container_port, (
            f"service `{svc_name}` must map {host_port}:{container_port}, "
            f"got `{first}`"
        )


# ---------------------------------------------------------------------------
# API key via variable substitution (spec invariant: never hardcoded)
# ---------------------------------------------------------------------------


def test_compose_api_key_uses_variable_substitution(compose_text: str) -> None:
    # The YAML parser resolves `${VAR}` to its default at load time, so we
    # must inspect the raw file content to verify the substitution syntax
    # is actually present (a hardcoded key would still parse to a string).
    assert "${ARCHON_SEARCH_API_KEY" in compose_text, (
        "docker-compose.yml must reference ARCHON_SEARCH_API_KEY via "
        "`${ARCHON_SEARCH_API_KEY:-...}` variable substitution"
    )
    # Every occurrence of `${ARCHON_SEARCH_API_KEY...}` MUST use the
    # empty-default form `${VAR:-}`. The bare form `${VAR}` would make
    # `docker compose up` *fail* when no key is set — breaking the
    # documented "auto-generate on first boot" contract. A non-empty
    # default would either bake a production secret into the file or
    # pin the key to a known literal. Both fail; only `:-` (empty) passes.
    bracket = re.compile(r"\$\{ARCHON_SEARCH_API_KEY([^}]*)\}")
    occurrences = bracket.findall(compose_text)
    assert occurrences, (
        "docker-compose.yml must reference ${ARCHON_SEARCH_API_KEY...} "
        "in at least one place"
    )
    for suffix in occurrences:
        # `suffix` is whatever sits between `ARCHON_SEARCH_API_KEY` and
        # the closing `}`. The only legal value is `:-` (empty default).
        assert suffix == ":-", (
            "docker-compose.yml must use `${ARCHON_SEARCH_API_KEY:-}` exactly. "
            f"Bare `${{ARCHON_SEARCH_API_KEY}}` makes compose fail when the "
            f"key is unset; `${{ARCHON_SEARCH_API_KEY:-<value>}}` bakes a "
            f"default into the file. Got suffix {suffix!r}."
        )


# ---------------------------------------------------------------------------
# DATA_DIR (spec invariant: all services read state from /data)
# ---------------------------------------------------------------------------


def test_compose_data_dir_set_to_slash_data(compose_doc: dict) -> None:
    services = compose_doc["services"]
    for svc_name in EXPECTED_SERVICES:
        env = services[svc_name].get("environment", {})
        # `environment` may be a list (`KEY=VALUE`) or a mapping. Normalize.
        if isinstance(env, list):
            env_map = {}
            for entry in env:
                key, _, value = entry.partition("=")
                env_map[key] = value
        else:
            env_map = dict(env)
        assert env_map.get("ARCHON_SEARCH_DATA_DIR") == "/data", (
            f"service `{svc_name}` must set ARCHON_SEARCH_DATA_DIR=/data"
        )


# ---------------------------------------------------------------------------
# Image (spec invariant: variable-substituted image with GHCR default)
# ---------------------------------------------------------------------------


def test_compose_image_uses_variable_substitution(compose_text: str) -> None:
    assert "${ARCHON_SEARCH_IMAGE" in compose_text, (
        "docker-compose.yml must use `${ARCHON_SEARCH_IMAGE:-...}` so "
        "operators can override the registry path without editing the file"
    )


# ---------------------------------------------------------------------------
# Restart policy (spec: prod = unless-stopped; dev/test = default)
# ---------------------------------------------------------------------------


def test_compose_prod_restart_unless_stopped(compose_doc: dict) -> None:
    prod = compose_doc["services"]["archon-prod"]
    assert prod.get("restart") == "unless-stopped", (
        "service `archon-prod` must declare `restart: unless-stopped`"
    )


def test_compose_dev_test_no_restart_policy(compose_doc: dict) -> None:
    for svc_name in ("archon-dev", "archon-test"):
        svc = compose_doc["services"][svc_name]
        assert "restart" not in svc, (
            f"service `{svc_name}` must NOT declare a restart policy "
            "(plan: default `no` for dev/test)"
        )


# ---------------------------------------------------------------------------
# Operator-facing comments (spec: must surface known limitations)
# ---------------------------------------------------------------------------


def test_compose_documents_tls_caveat(compose_text: str) -> None:
    # YAML comments are stripped by the parser, so we must inspect the raw
    # file. The plan mandates an operator-facing reminder that TLS
    # termination is not handled by this stack.
    assert "TLS termination" in compose_text, (
        "docker-compose.yml must include a comment noting that TLS "
        "termination is the operator's responsibility"
    )


def test_compose_documents_single_writer_caveat(compose_text: str) -> None:
    assert "single-writer" in compose_text.lower() or "LanceDB" in compose_text, (
        "docker-compose.yml must warn operators about the LanceDB "
        "single-writer invariant for the shared volume"
    )


def test_compose_documents_key_regeneration_caveat(compose_text: str) -> None:
    assert "regenerate" in compose_text.lower() or "regenerates" in compose_text, (
        "docker-compose.yml must warn that the API key regenerates on every "
        "start without a persistent volume"
    )


def test_compose_mentions_fastembed_cache_volume(compose_text: str) -> None:
    # The plan asks for a commented-out `archon-model-cache` volume with
    # an explanation. We only verify the marker word; the comment body is
    # operator-facing prose.
    assert "fastembed" in compose_text.lower(), (
        "docker-compose.yml must document the optional fastembed model "
        "cache volume (commented-out template)"
    )


def test_compose_fastembed_cache_path_is_commented_out(compose_doc: dict) -> None:
    # The plan specifies `FASTEMBED_CACHE_PATH` as an *optional* opt-in,
    # not an always-on env var. If a future edit silently activates it
    # without also wiring up the `archon-model-cache` volume, every
    # service would fail at startup ("undefined volume" or a writable
    # path inside an ephemeral layer). Guard against that drift.
    services = compose_doc["services"]
    for svc_name in EXPECTED_SERVICES:
        env = services[svc_name].get("environment", {})
        if isinstance(env, list):
            env_map = {}
            for entry in env:
                key, _, value = entry.partition("=")
                env_map[key] = value
        else:
            env_map = dict(env)
        assert "FASTEMBED_CACHE_PATH" not in env_map, (
            f"service `{svc_name}` must NOT activate FASTEMBED_CACHE_PATH "
            "by default — the cache wiring is an opt-in template only "
            "(uncomment the env line AND the archon-model-cache volume)"
        )


def test_compose_archon_model_cache_volume_commented_out(
    compose_doc: dict, compose_text: str
) -> None:
    # Parallel guard to `test_compose_fastembed_cache_path_is_commented_out`:
    # the parsed YAML must NOT declare `archon-model-cache` as an active
    # top-level volume, but the raw text MUST mention it (as a commented
    # template). This locks in the "opt-in only" contract on both sides.
    top_level_volumes = compose_doc.get("volumes", {}) or {}
    assert "archon-model-cache" not in top_level_volumes, (
        "Top-level `volumes:` must NOT declare `archon-model-cache` as "
        "active — it ships as a commented-out template that operators "
        "uncomment alongside FASTEMBED_CACHE_PATH"
    )
    assert "archon-model-cache" in compose_text, (
        "docker-compose.yml must mention `archon-model-cache` in a "
        "commented-out template block so operators know how to wire it"
    )


# ---------------------------------------------------------------------------
# .env.example contents
# ---------------------------------------------------------------------------


def test_env_example_has_api_key_placeholder(env_example_text: str) -> None:
    assert "ARCHON_SEARCH_API_KEY" in env_example_text, (
        ".env.example must list ARCHON_SEARCH_API_KEY so operators know "
        "how to inject their key"
    )


def test_env_example_has_commented_local_image_override(env_example_text: str) -> None:
    # The plan mandates a commented-out local-build override so operators
    # can pivot from the GHCR default without editing docker-compose.yml.
    assert "ARCHON_SEARCH_IMAGE" in env_example_text, (
        ".env.example must mention ARCHON_SEARCH_IMAGE so operators can "
        "override the image path (e.g. for a local build)"
    )
