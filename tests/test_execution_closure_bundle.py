"""Contract and omission mutation for the immutable execution-closure bundle."""

from __future__ import annotations

from pathlib import Path

import pytest

from lattice.canonical import canonical_bytes, generated_object
from ops import build_execution_closure_bundle as bundler


def _inputs(tmp_path: Path) -> dict[str, Path]:
    paths = {}
    for index, (name, contract) in enumerate(bundler.EVIDENCE_CONTRACTS.items()):
        role = "canonical" if name in {"runtime_manifest", "consumer_registry", "findings"} else "snapshot"
        value = {
            "generated_object": generated_object(kind=f"fixture-{name.replace('_', '-')}", schema_name=f"fixture-{name.replace('_', '-')}", subject=f"fixture:{name}", role=role, sources=[{"ref": f"fixture:{name}", "revision": "fixture", "sha256": f"{index:x}" * 64}]),
            "contract_version": contract,
        }
        path = tmp_path / f"{name}.json"
        path.write_bytes(canonical_bytes(value))
        paths[name] = path
    return paths


@pytest.mark.contract
def test_closure_bundle_is_canonical_and_binds_every_evidence_byte_once(tmp_path: Path) -> None:
    paths = _inputs(tmp_path)
    output = tmp_path / "bundle.json"

    value = bundler.write_bundle(paths, output)

    assert value["generated_object"]["role"] == "canonical"
    assert value["contract_version"] == "LatticeExecutionClosureBundle/1.0"
    assert set(value["evidence"]) == set(bundler.EVIDENCE_CONTRACTS)
    assert {item["sha256"] for item in value["evidence"].values()} == {item["sha256"] for item in value["generated_object"]["sources"]}
    assert output.read_bytes() == canonical_bytes(value)


@pytest.mark.regression
def test_negative_control_closure_bundle_rejects_one_omitted_evidence_plane(tmp_path: Path) -> None:
    paths = _inputs(tmp_path)
    paths.pop("browser_acceptance")

    with pytest.raises(bundler.ClosureBundleFailure, match="missing=.*browser_acceptance"):
        bundler.build_bundle(paths, tmp_path / "must-not-exist.json")


@pytest.mark.regression
def test_negative_control_closure_bundle_rejects_changed_noncanonical_evidence(tmp_path: Path) -> None:
    paths = _inputs(tmp_path)
    paths["doctor"].write_bytes(paths["doctor"].read_bytes() + b"\n")

    with pytest.raises(bundler.ClosureBundleFailure, match="not exact canonical"):
        bundler.build_bundle(paths, tmp_path / "must-not-exist.json")
