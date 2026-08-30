"""Truth-typed checks for release-qualification receipt construction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lattice.canonical import canonical_bytes, generated_object
from ops import qualify_release


def _source(ref: str) -> list[dict[str, str | None]]:
    return [{"ref": ref, "revision": "fixture", "sha256": None}]


def _inputs(tmp_path: Path) -> dict:
    runtime = {
        "generated_object": generated_object(kind="lattice-installed-runtime", schema_name="lattice-installed-runtime", schema_version=4, subject="lattice:installed-runtime:0.3.1-fixture", sources=_source("fixture:runtime")),
        "contract_version": "LatticeInstalledRuntime/4.0",
        "runtime_id": "0.3.1-fixture",
    }
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_bytes(canonical_bytes(runtime))
    consumers = []
    for identity in ("canon.quaternary-exhaustion", "field-survey.research-pipeline"):
        root = tmp_path / identity
        root.mkdir()
        (root / "client.py").write_text("BOUNDARY = True\n", encoding="utf-8")
        consumers.append({"consumer_id": identity, "source_root": identity, "client_boundary": f"{identity}/client.py", "forbidden_direct_authorities": ["provider-cli", "provider-sdk", "developer-api"]})
    registry = {
        "generated_object": generated_object(kind="lattice-execution-consumer-registry", schema_name="lattice-execution-consumer-registry", subject="lattice:execution-consumers", sources=_source("fixture:registry")),
        "contract_version": "ExecutionConsumerRegistry/1.0",
        "consumers": consumers,
    }
    registry_path = tmp_path / "registry.json"
    registry_path.write_bytes(canonical_bytes(registry))
    suites = [{"suite_id": identity, "truth_kinds": ["contract"], "command": [["fixture"]], "cwd": str(tmp_path), "exit_code": 0, "stdout_sha256": "1" * 64, "stderr_sha256": "2" * 64} for identity in qualify_release.SUITES]
    providers = [{"provider": provider, "profile": profile, "state": "succeeded", "invocation_id": f"invocation_{index}", "manifest_sha256": "3" * 64, "output_sha256": "4" * 64, "provider_profile_id": "provider-profile:fixture", "runtime_receipt_sha256": "5" * 64} for index, (provider, profile) in enumerate(sorted(qualify_release.EXPECTED_CANARY_PROFILES))]
    consumer_canaries = [{"consumer_id": item["consumer_id"], "external_project_key": f"{item['consumer_id']}.canary", "invocation_id": f"invocation_{item['consumer_id']}", "manifest_sha256": "6" * 64, "report_path": str(tmp_path / f"{item['consumer_id']}.json"), "report_sha256": "7" * 64} for item in consumers]
    for item in consumer_canaries:
        Path(item["report_path"]).write_bytes(b"consumer-report")
        item["report_sha256"] = __import__("hashlib").sha256(b"consumer-report").hexdigest()
    interruption_source = tmp_path / "interruption.json"
    interruption_source.write_bytes(b"interruption")
    canary = tmp_path / "credential-canary"
    canary.write_bytes(b"LATTICE-SYNTHETIC-CREDENTIAL-CANARY")
    return {"runtime_manifest_path": runtime_path, "consumer_registry_path": registry_path, "axisvi_root": tmp_path, "suites": suites, "provider_canaries": providers, "consumer_canaries": consumer_canaries, "interruption": {"pre_restart_state": "running", "post_restart_state": "orphaned", "parent_invocation_id": "invocation_parent", "continuation_invocation_id": "invocation_continuation"}, "interruption_source": interruption_source, "credential_canary_files": [canary]}


@pytest.mark.contract
def test_qualification_receipt_is_snapshot_and_exactly_source_bound(tmp_path) -> None:
    inputs = _inputs(tmp_path)
    output = tmp_path / "qualification.json"

    value = qualify_release.write_receipt(output, **inputs)

    assert value["generated_object"]["role"] == "snapshot"
    assert value["contract_version"] == "LatticeQualificationReceipt/1.0"
    assert canonical_bytes(json.loads(output.read_bytes())) == output.read_bytes()
    assert set(value["consumer_source_inventories"]) == {"canon.quaternary-exhaustion", "field-survey.research-pipeline"}


@pytest.mark.regression
def test_negative_control_qualification_refuses_a_missing_provider_profile(tmp_path) -> None:
    inputs = _inputs(tmp_path)
    inputs["provider_canaries"].pop()

    with pytest.raises(qualify_release.QualificationFailure, match="provider-profile"):
        qualify_release.write_receipt(tmp_path / "must-not-exist.json", **inputs)


@pytest.mark.contract
def test_antigravity_canary_uses_the_exact_collision_regression_schema() -> None:
    request = qualify_release._submission("google", "compiled-fresh", route_profile="compiled-fresh", marker="MARKER")

    schema = request["output_contract"]["json_schema"]
    assert schema["required"] == ["response", "toolSummary", "toolAction"]
    assert set(schema["properties"]) == {"response", "toolSummary", "toolAction"}
