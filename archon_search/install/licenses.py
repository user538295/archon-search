"""License gates (Jina, fasttext) plus the fasttext model download."""
from __future__ import annotations

import hashlib
import http.client
import logging
import shutil
import urllib.error
import urllib.request
from pathlib import Path

from archon_search._durable_io import atomic_write_bytes
from archon_search.profiles import JINA_RERANKER_MODEL, InstallProfile

from .errors import InstallError
from .provisioning import ArtifactSpec, LicenseDisposition, ProvisionFailureKind

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Jina license gate (Task C0-3.2)
# ---------------------------------------------------------------------------

def _requires_jina_license(profile: InstallProfile) -> bool:
    """Return True if *profile* uses the Jina reranker model (CC-BY-NC-4.0)."""
    return profile.reranker == JINA_RERANKER_MODEL


def _prompt_jina_license(non_interactive: bool, accept_jina_license: bool = False) -> None:
    """Print the Jina CC-BY-NC-4.0 warning and gate on user / flag acceptance.

    Raises SystemExit(1) if the license is not accepted.
    """
    print(
        "WARNING: jinaai/jina-reranker-v2-base-multilingual is licensed CC-BY-NC-4.0\n"
        "(non-commercial use only). Commercial use of multilingual profiles 2 and 3\n"
        "requires an alternative reranker. You will be required to confirm license\n"
        "acceptance before this model is downloaded."
    )

    if accept_jina_license:
        return

    if non_interactive:
        print(
            "Non-interactive mode: Jina license automatically declined. "
            "Use an English profile for commercial installs."
        )
        raise SystemExit(1)

    response = input("Type 'accept' to confirm license acceptance and continue, or anything else to abort: ")
    if response.strip().lower() == "accept":
        return
    print("License not accepted. Aborting.")
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# The one license rule (S39) — dispatches on ArtifactSpec.license_disposition;
# no per-model or per-name branching.
# ---------------------------------------------------------------------------


def apply_license_rule(
    spec: ArtifactSpec, non_interactive: bool, accept_license: bool = False
) -> None:
    """Disclose or gate on *spec*'s license per its ``license_disposition``.

    ``disclose_only`` prints the license and returns — no prompt, no SystemExit.
    ``prompt_for_acceptance`` gates on user / flag acceptance, raising SystemExit(1) if
    declined or non-interactive without an accept flag. Pattern mirrors
    _prompt_jina_license / _prompt_fasttext_license.
    """
    if spec.license_disposition == LicenseDisposition.disclose_only:
        print(f"{spec.name} is licensed {spec.license}.")
        return

    if spec.license_disposition != LicenseDisposition.prompt_for_acceptance:
        raise ValueError(f"Unrecognized license_disposition: {spec.license_disposition!r}")

    print(
        f"WARNING: {spec.name} is licensed {spec.license}, which restricts use.\n"
        "You will be required to confirm license acceptance before this artifact is downloaded."
    )

    if accept_license:
        return

    if non_interactive:
        print(f"Non-interactive mode: {spec.name} license automatically declined.")
        raise SystemExit(1)

    response = input("Type 'accept' to confirm license acceptance and continue, or anything else to abort: ")
    if response.strip().lower() == "accept":
        return
    print("License not accepted. Aborting.")
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# fasttext license gate (Task 4.1)
# ---------------------------------------------------------------------------


def _prompt_fasttext_license(non_interactive: bool, accept_fasttext_license: bool = False) -> None:
    """Print the fasttext CC-BY-SA 3.0 warning and gate on user / flag acceptance.

    Raises SystemExit(1) if the license is not accepted.
    Pattern mirrors _prompt_jina_license exactly.
    """
    print(
        "WARNING: lid.176.ftz (fasttext language identification model) is licensed CC-BY-SA 3.0.\n"
        "This model was created by Facebook Research and redistributed under CC-BY-SA 3.0.\n"
        "You must comply with its terms for any use."
    )

    if accept_fasttext_license:
        return

    if non_interactive:
        print("Non-interactive mode: fasttext license automatically declined.")
        raise SystemExit(1)

    response = input("Type 'accept' to confirm license acceptance and continue, or anything else to abort: ")
    if response.strip().lower() == "accept":
        return
    print("License not accepted. Aborting.")
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# fasttext model download (Task 4.2)
# ---------------------------------------------------------------------------

FASTTEXT_MODEL_URL = "https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.ftz"

# Digest and byte count generated from the bytes served at FASTTEXT_MODEL_URL (2026-08-31),
# never read off a model card. ``revision`` and ``attribution`` are unpopulated: the URL is a
# single fixed asset with no upstream revision concept, and we redistribute nothing — the file
# is fetched from its own URL and placed as-is. ``license_disposition`` is likewise inert here;
# the operator gate stays on the legacy ``_prompt_fasttext_license`` above.
FASTTEXT_ARTIFACT = ArtifactSpec(
    name="lid.176.ftz",
    url=FASTTEXT_MODEL_URL,
    revision="",
    sha256="8f3472cfe8738a7b6099e8e999c3cbfae0dcd15696aac7d7738a8039db603e83",
    size_bytes=938013,
    license="CC-BY-SA-3.0",
    license_disposition=LicenseDisposition.disclose_only,
    attribution="",
)


def _download_fasttext_model(models_dir: Path) -> None:
    """Download the fasttext language identification model to *models_dir*.

    Durable, verified, single-file provisioning for ``lid.176.ftz`` (BE-13): check
    free space -> download into memory (the file is under 1 MB, so buffering beats a
    chunked write) -> verify byte count and digest, as two distinct failure
    categories, BEFORE anything is placed on disk -> publish via
    :func:`archon_search._durable_io.atomic_write_bytes`, which stages the bytes in a
    temp file inside *models_dir* (same filesystem as the final path), fsyncs it,
    atomically renames it onto the target, then fsyncs the parent directory.

    - Creates *models_dir* (mode 0o700) if absent.
    - No-op if ``lid.176.ftz`` already exists and matches both
      ``FASTTEXT_ARTIFACT.sha256`` and ``FASTTEXT_ARTIFACT.size_bytes``; a mismatching
      file is deleted and re-downloaded (non-fatal, one time per install).
    - Uses ``urllib.request.urlopen`` with an explicit 120-second socket timeout
      instead of ``urlretrieve`` (which has no timeout).
    - Raises ``InstallError`` on insufficient disk space, network failure, a
      byte-count mismatch, or a digest mismatch — each a distinct failure category
      from :class:`ProvisionFailureKind`.
    """
    target = models_dir / "lid.176.ftz"

    if target.exists():
        try:
            # The file is under 1 MB, so one read beats a chunked hasher.
            existing = target.read_bytes()
            matches = (
                len(existing) == FASTTEXT_ARTIFACT.size_bytes
                and hashlib.sha256(existing).hexdigest() == FASTTEXT_ARTIFACT.sha256
            )
        except OSError:
            # Unreadable (e.g. a directory, permission-denied) — can't verify, so
            # fall through and re-download; atomic_write_bytes's os.replace will
            # overwrite it once the fresh download is verified.
            logger.warning("fasttext model at %s could not be read — re-downloading", target)
        else:
            if matches:
                logger.debug("fasttext model already present at %s — skipping download", target)
                return
            logger.warning(
                "fasttext model at %s does not match the pinned digest/size — re-downloading",
                target,
            )

    models_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    # mkdir(mode=...) is a no-op on an already-existing dir, so set it unconditionally
    # to correct a directory created by an earlier version (matches extras.py:281).
    models_dir.chmod(0o700)

    usage = shutil.disk_usage(models_dir)
    if usage.free < FASTTEXT_ARTIFACT.size_bytes:
        raise InstallError(
            f"Insufficient disk space for fasttext lid.176.ftz "
            f"({ProvisionFailureKind.insufficient_disk}): needs "
            f"~{FASTTEXT_ARTIFACT.size_bytes} bytes free at {models_dir}."
        )

    print("[4b/5] Downloading fasttext language model...")

    try:
        with urllib.request.urlopen(FASTTEXT_MODEL_URL, timeout=120) as response:
            # Bounded read: a mismatched size is caught below without buffering an
            # unbounded response from a hostile or misconfigured server.
            content = response.read(FASTTEXT_ARTIFACT.size_bytes + 1)
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        logger.warning("fasttext model download failed: %s", exc)
        raise InstallError(
            f"Failed to download fasttext lid.176.ftz model "
            f"({ProvisionFailureKind.download_failed}). "
            "Check your network connection and re-run install."
        ) from exc

    # Byte-count assert BEFORE the digest assert and BEFORE anything touches disk —
    # a short download and a digest mismatch are distinct failure categories (S37, S11).
    if len(content) != FASTTEXT_ARTIFACT.size_bytes:
        raise InstallError(
            f"fasttext model download appears corrupt "
            f"({ProvisionFailureKind.size_mismatch}): expected "
            f"{FASTTEXT_ARTIFACT.size_bytes} bytes, got {len(content)}; re-run install."
        )

    if hashlib.sha256(content).hexdigest() != FASTTEXT_ARTIFACT.sha256:
        raise InstallError(
            f"fasttext model digest verification failed "
            f"({ProvisionFailureKind.digest_mismatch}): the fetched bytes do not "
            "match the pinned sha256; re-run install."
        )

    # A stale .tmp from a prior crashed run of this same install path is our own
    # leftover, not a real conflict (concurrent installs of this file are not a
    # supported scenario) — atomic_write_bytes uses O_EXCL and will not unlink it.
    target.with_suffix(target.suffix + ".tmp").unlink(missing_ok=True)

    try:
        atomic_write_bytes(target, content, mode=0o644)
    except OSError as exc:
        logger.warning("fasttext model publish to %s failed: %s", target, exc)
        raise InstallError(
            "Failed to write fasttext lid.176.ftz model to disk. "
            "Check available disk space and permissions."
        ) from exc
