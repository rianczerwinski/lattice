#!/usr/bin/env python3
"""Assemble the exact evidence graph consumed by the independent closure verifier."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from lattice.canonical import canonical_bytes, generated_object, sha256_bytes, strict_json_loads, validate_generated_object_envelope


EVIDENCE_CONTRACTS = {
    "runtime_manifest": "LatticeInstalledRuntime/4.0",
    "consumer_registry": "ExecutionConsumerRegistry/1.0",
    "historical_execution_set": "HistoricalExecutionSet/1.0",
    "doctor": "LatticeDoctorReceipt/1.0",
    "qualification": "LatticeQualificationReceipt/1.0",
    "browser_acceptance": "LatticeBrowserAcceptanceReceipt/1.0",
    "restore_continuation": "LatticeRestoreContinuationReceipt/1.0",
    "findings": "LatticeExecutionConsolidationFindings/2.0",
}


class ClosureBundleFailure(RuntimeError):
    pass


def _evidence(path: Path, output_parent: Path, *, name: str, contract: str) -> tuple[dict[str, str], dict[str, Any]]:
    path = path.expanduser().resolve(strict=True)
    raw = path.read_bytes()
    value = strict_json_loads(raw)
    if not isinstance(value, dict) or canonical_bytes(value) != raw or value.get("contract_version") != contract:
        raise ClosureBundleFailure(f"closure evidence is not exact canonical {contract}: {name}")
    envelope = value.get("generated_object")
    try:
        validate_generated_object_envelope(envelope)
    except ValueError as error:
        raise ClosureBundleFailure(f"closure evidence has no valid durable identity: {name}") from error
    ref = f"file:{os.path.relpath(path, output_parent)}"
    return {"ref": ref, "artifact_id": envelope["artifact_id"], "sha256": sha256_bytes(raw)}, {"ref": ref, "revision": envelope["artifact_id"], "sha256": sha256_bytes(raw)}


def build_bundle(paths: dict[str, Path], output: Path) -> dict[str, Any]:
    if set(paths) != set(EVIDENCE_CONTRACTS):
        raise ClosureBundleFailure(f"closure evidence set is not exact: missing={sorted(set(EVIDENCE_CONTRACTS) - set(paths))} extra={sorted(set(paths) - set(EVIDENCE_CONTRACTS))}")
    output = output.expanduser().resolve()
    evidence: dict[str, dict[str, str]] = {}
    sources: list[dict[str, str]] = []
    for name, contract in EVIDENCE_CONTRACTS.items():
        item, source = _evidence(paths[name], output.parent, name=name, contract=contract)
        evidence[name] = item
        sources.append(source)
    return {
        "generated_object": generated_object(
            kind="lattice-execution-closure-bundle",
            schema_name="lattice-execution-closure-bundle",
            subject="lattice:execution-consolidation:0.3.1",
            role="canonical",
            sources=sources,
            producer_name="lattice-execution-closure-bundler",
            producer_version="1.0.0",
        ),
        "contract_version": "LatticeExecutionClosureBundle/1.0",
        "target_version": "0.3.1",
        "evidence": evidence,
    }


def write_bundle(paths: dict[str, Path], output: Path) -> dict[str, Any]:
    value = build_bundle(paths, output)
    raw = canonical_bytes(value)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        if output.is_symlink() or output.read_bytes() != raw:
            raise FileExistsError(f"closure bundle path already contains different bytes: {output}")
        return value
    with output.open("xb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    return value


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in EVIDENCE_CONTRACTS:
        parser.add_argument(f"--{name.replace('_', '-')}", dest=name, type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = arguments()
    paths = {name: getattr(args, name) for name in EVIDENCE_CONTRACTS}
    value = write_bundle(paths, args.output.expanduser().resolve())
    print(json.dumps({"output": str(args.output.expanduser().resolve()), "artifact_id": value["generated_object"]["artifact_id"], "evidence_count": len(value["evidence"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
