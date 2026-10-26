"""Runtime-identity verification binds Lattice bytes exactly and treats provider CLI identities as retained evidence.

Governing decision: dev/decisions/003-provider-readiness.md. The execution-closure
verifier must keep rejecting modified installed Lattice application bytes and
malformed or dishonest provider attestations, but must not refuse closure because an
optional external CLI (Claude Code, Codex, Gemini, ...) self-updated or was removed
after installation. Passing these tests does not prove any provider route is
available; doctor and provider qualification own that claim.

These tests use only synthetic fixtures under tmp_path: no installed runtime, no
real provider executable, no data root.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path

import pytest

# LATTICE_SOURCE_ROOT lets an isolated copy of this file exercise a copied or the
# live verifier read-only; inside the checkout it is unset and inert.
REPOSITORY_ROOT = Path(os.environ.get("LATTICE_SOURCE_ROOT") or Path(__file__).parents[1])
VERIFIER_PATH = REPOSITORY_ROOT / "ops" / "verify_execution_consolidation.py"
SPEC = importlib.util.spec_from_file_location("verify_execution_consolidation_runtime_identity", VERIFIER_PATH)
assert SPEC and SPEC.loader
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)

ConsolidationVerificationError = verifier.ConsolidationVerificationError

DEFINITION_KEYS = (
    "contract_version",
    "provider_code",
    "harness",
    "official_cli",
    "default_model",
    "supported_models",
    "supported_efforts",
    "auth_surface",
    "response_mode",
    "isolation",
    "proof_limits",
    "qualified_profiles",
    "exact_context_qualified",
    "qualification_blocker",
    "capability_compiler",
    "parser_revision",
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _attestation(provider_code: str, *, installed: bool, binary: object, binary_sha256: object) -> dict:
    definition = {
        "contract_version": "LatticeProviderProfile/1.0",
        "provider_code": provider_code,
        "harness": f"{provider_code}-harness",
        "official_cli": f"{provider_code}-cli",
        "default_model": f"{provider_code}-model",
        "supported_models": [f"{provider_code}-model"],
        "supported_efforts": ["high"],
        "auth_surface": "subscription-cli",
        "response_mode": "json",
        "isolation": "workspace",
        "proof_limits": ["fixture"],
        "qualified_profiles": [],
        "exact_context_qualified": False,
        "qualification_blocker": None,
        "capability_compiler": "fixture-compiler",
        "parser_revision": "fixture-parser",
    }
    assert set(definition) == set(DEFINITION_KEYS)
    profile_id = f"provider-profile:{verifier.sha256_bytes(verifier.canonical_bytes(definition))}"
    return {
        **definition,
        "profile_id": profile_id,
        "model_code": f"{provider_code}-model",
        "binary": binary,
        "binary_sha256": binary_sha256,
        "installed": installed,
        "qualified": installed,
        "operational": True if installed else None,
        "version": "fixture" if installed else None,
        "control_surface_qualified": None,
        "control_surface_detail": None,
    }


class RuntimeFixture:
    """A synthetic installed runtime: Lattice package tree, one external CLI, exact manifest."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.package_root = root / "runtime" / ".venv" / "lib" / "site-packages" / "lattice"
        self.package_root.mkdir(parents=True)
        self.package_root.joinpath("__init__.py").write_bytes(b"")
        self.package_root.joinpath("cli.py").write_bytes(b"# installed application\n")
        self.cli_path = root / "bin" / "fixture-cli"
        self.cli_path.parent.mkdir(parents=True)
        self.cli_bytes = b"#!/bin/sh\necho installed-cli-v1\n"
        self.cli_path.write_bytes(self.cli_bytes)
        self.manifest_path = root / "runtime" / "runtime.json"

    def inventory(self) -> list[dict]:
        rows = []
        for candidate in sorted(self.package_root.rglob("*")):
            if candidate.is_file():
                rows.append({"path": candidate.relative_to(self.package_root).as_posix(), "sha256": _sha256(candidate.read_bytes()), "byte_count": candidate.stat().st_size})
        return rows

    def attestations(self) -> list[dict]:
        return [
            _attestation("anthropic", installed=True, binary=str(self.cli_path), binary_sha256=_sha256(self.cli_bytes)),
            _attestation("meta", installed=False, binary=None, binary_sha256=None),
        ]

    def write_manifest(self, *, attestations: list[dict] | None = None) -> Path:
        manifest = {
            "generated_object": {"artifact_id": "artifact:fixture-runtime", "generated_at": "20261026T000000"},
            "contract_version": "LatticeInstalledRuntime/4.0",
            "runtime_id": "0.3.1-fixture",
            "python": "3.12-fixture",
            "packages": ["lattice==fixture"],
            "module_locations": {"lattice": str(self.package_root / "__init__.py")},
            "source_trees": {"lattice": self.inventory()},
            "provider_attestations": self.attestations() if attestations is None else attestations,
        }
        self.manifest_path.write_bytes(verifier.canonical_bytes(manifest))
        return self.manifest_path


@pytest.fixture
def runtime(tmp_path: Path) -> RuntimeFixture:
    return RuntimeFixture(tmp_path)


def test_unchanged_fixture_runtime_verifies(runtime: RuntimeFixture) -> None:
    manifest = runtime.write_manifest()
    result = verifier.verify_runtime_manifest(manifest)
    assert result["runtime_id"] == "0.3.1-fixture"
    assert result["runtime_artifact_id"] == "artifact:fixture-runtime"
    assert result["package_files"] == 2
    assert result["provider_profiles"] == 2
    assert result["provider_attestation_digest"] == verifier.sha256_bytes(verifier.canonical_bytes(runtime.attestations()))


@pytest.mark.regression
def test_changed_external_cli_bytes_remain_closure_evidence(runtime: RuntimeFixture) -> None:
    """Falsifier for the rejected global rehash: the old verifier raised 'installed provider binary changed' here."""

    manifest = runtime.write_manifest()
    runtime.cli_path.write_bytes(b"#!/bin/sh\necho self-updated-cli-v2\n")
    assert _sha256(runtime.cli_path.read_bytes()) != runtime.attestations()[0]["binary_sha256"]
    result = verifier.verify_runtime_manifest(manifest)
    # The attestation digest still binds the install-time identity as evidence.
    assert result["provider_attestation_digest"] == verifier.sha256_bytes(verifier.canonical_bytes(runtime.attestations()))


@pytest.mark.regression
def test_absent_historical_cli_remains_closure_evidence(runtime: RuntimeFixture) -> None:
    """A retired or relocated external CLI does not refuse closure.

    This establishes only that the recorded identity is well-formed evidence. It does
    not prove the route is available; doctor and provider qualification own that.
    The old verifier failed here with FileNotFoundError from resolve(strict=True).
    """

    retired = runtime.root / "bin" / "retired-cli"
    assert not retired.exists()
    attestations = runtime.attestations()
    attestations[0]["binary"] = str(retired)
    manifest = runtime.write_manifest(attestations=attestations)
    result = verifier.verify_runtime_manifest(manifest)
    assert result["provider_profiles"] == 2


def test_changed_lattice_package_bytes_still_refused(runtime: RuntimeFixture) -> None:
    manifest = runtime.write_manifest()
    runtime.package_root.joinpath("cli.py").write_bytes(b"# tampered application\n")
    with pytest.raises(ConsolidationVerificationError, match="installed Lattice bytes differ"):
        verifier.verify_runtime_manifest(manifest)


def test_added_lattice_package_file_still_refused(runtime: RuntimeFixture) -> None:
    manifest = runtime.write_manifest()
    runtime.package_root.joinpath("extra.py").write_bytes(b"# unattested module\n")
    with pytest.raises(ConsolidationVerificationError, match="installed Lattice bytes differ"):
        verifier.verify_runtime_manifest(manifest)


@pytest.mark.parametrize(
    ("binary", "binary_sha256"),
    [
        pytest.param("bin/relative-cli", "a" * 64, id="relative-path"),
        pytest.param(None, "a" * 64, id="missing-path"),
        pytest.param(42, "a" * 64, id="non-string-path"),
        pytest.param("/abs/fixture-cli", None, id="missing-digest"),
        pytest.param("/abs/fixture-cli", "a" * 63, id="short-digest"),
        pytest.param("/abs/fixture-cli", "A" * 64, id="uppercase-digest"),
        pytest.param("/abs/fixture-cli", "g" * 64, id="non-hex-digest"),
    ],
)
def test_malformed_installed_cli_evidence_refused(runtime: RuntimeFixture, binary: object, binary_sha256: object) -> None:
    attestations = runtime.attestations()
    attestations[0]["binary"] = binary
    attestations[0]["binary_sha256"] = binary_sha256
    manifest = runtime.write_manifest(attestations=attestations)
    with pytest.raises(ConsolidationVerificationError, match="installed provider binary attestation is malformed: anthropic"):
        verifier.verify_runtime_manifest(manifest)


@pytest.mark.parametrize(
    ("binary", "binary_sha256"),
    [
        pytest.param("/abs/ghost-cli", "b" * 64, id="path-and-digest"),
        pytest.param("/abs/ghost-cli", None, id="path-only"),
        pytest.param(None, "b" * 64, id="digest-only"),
    ],
)
def test_uninstalled_provider_claiming_binary_refused(runtime: RuntimeFixture, binary: object, binary_sha256: object) -> None:
    attestations = runtime.attestations()
    attestations[1]["binary"] = binary
    attestations[1]["binary_sha256"] = binary_sha256
    manifest = runtime.write_manifest(attestations=attestations)
    with pytest.raises(ConsolidationVerificationError, match="uninstalled provider claims binary evidence: meta"):
        verifier.verify_runtime_manifest(manifest)


def test_profile_identity_still_exact(runtime: RuntimeFixture) -> None:
    attestations = runtime.attestations()
    attestations[0]["default_model"] = "silently-upgraded-model"
    manifest = runtime.write_manifest(attestations=attestations)
    with pytest.raises(ConsolidationVerificationError, match="installed provider profile is not exact: anthropic"):
        verifier.verify_runtime_manifest(manifest)
