"""Cheap runtime and closure identities for health and operational qualification."""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from .canonical import canonical_bytes, sha256_bytes, strict_json_loads, validate_generated_object_envelope


@lru_cache(maxsize=4)
def installed_runtime_identity(runtime_id: str) -> dict[str, Any]:
    if runtime_id == "source":
        return {
            "runtime_id": "source",
            "runtime_artifact_id": None,
            "runtime_tree_digest": None,
            "provider_attestation_digest": None,
            "runtime_manifest_path": None,
            "runtime_manifest_sha256": None,
            "provider_attestations": None,
        }
    environment = Path(sys.prefix).resolve()
    executable = Path(sys.executable)
    runtime_root = environment.parent
    try:
        executable.relative_to(environment)
    except ValueError as error:
        raise RuntimeError(f"executing Python does not belong to its selected environment: {executable}") from error
    if environment.name != ".venv" or runtime_root.name != runtime_id or runtime_root.parent.name != "versions":
        raise RuntimeError(f"executing Python environment is not selected runtime {runtime_id}: {environment}")
    manifest_path = runtime_root / "runtime.json"
    raw = manifest_path.read_bytes()
    manifest = strict_json_loads(raw)
    if not isinstance(manifest, dict) or canonical_bytes(manifest) != raw or manifest.get("contract_version") != "LatticeInstalledRuntime/4.0" or manifest.get("runtime_id") != runtime_id:
        raise RuntimeError("selected runtime manifest is not exact canonical LatticeInstalledRuntime/4.0 JSON")
    envelope = manifest.get("generated_object")
    validate_generated_object_envelope(envelope)
    if envelope.get("role") != "canonical" or envelope.get("kind") != "lattice-installed-runtime":
        raise RuntimeError("selected runtime generated-object identity is invalid")
    return {
        "runtime_id": runtime_id,
        "runtime_artifact_id": envelope["artifact_id"],
        "runtime_tree_digest": sha256_bytes(canonical_bytes(manifest["source_trees"]["lattice"])),
        "provider_attestation_digest": sha256_bytes(canonical_bytes(manifest["provider_attestations"])),
        "runtime_manifest_path": str(manifest_path),
        "runtime_manifest_sha256": sha256_bytes(raw),
        "provider_attestations": manifest["provider_attestations"],
    }


def latest_closure_state(data_root: Path) -> dict[str, Any]:
    evidence_root = data_root / "acceptance" / "execution-closure"
    candidates = sorted(evidence_root.glob("*-closure-attestation.json"), reverse=True) if evidence_root.is_dir() else []
    if not candidates:
        return {"state": "unqualified", "artifact_id": None, "bundle_artifact_id": None, "attestation_sha256": None, "path": None}
    path = candidates[0]
    raw = path.read_bytes()
    value = strict_json_loads(raw)
    if not isinstance(value, dict) or canonical_bytes(value) != raw or value.get("contract_version") != "LatticeExecutionClosureAttestation/2.0":
        raise RuntimeError(f"latest closure attestation is invalid: {path}")
    envelope = value.get("generated_object")
    validate_generated_object_envelope(envelope)
    if envelope["role"] != "attestation" or envelope["result"] != "pass":
        raise RuntimeError(f"latest closure attestation does not pass: {path}")
    return {
        "state": value.get("closure_state"),
        "artifact_id": envelope["artifact_id"],
        "bundle_artifact_id": envelope["binding"]["artifact_id"],
        "attestation_sha256": sha256_bytes(raw),
        "path": str(path),
    }
