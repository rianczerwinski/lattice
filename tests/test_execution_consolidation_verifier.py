"""Adversarial checks for independent execution-consolidation closure."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from lattice.canonical import canonical_bytes, generated_object, sha256_bytes
from lattice.contracts import AttemptState, ExecutionSubmissionV1, OutputContractV2


VERIFIER_PATH = Path(__file__).parents[1] / "ops" / "verify_execution_consolidation.py"
SPEC = importlib.util.spec_from_file_location("verify_execution_consolidation", VERIFIER_PATH)
assert SPEC and SPEC.loader
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


def _blob(store, value: dict) -> dict:
    content = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    digest = store.blobs.put(content)
    return {"ref": f"lattice:blob/{digest}", "sha256": digest, "byte_count": len(content)}


def _completed_external(lattice_env) -> str:
    request = ExecutionSubmissionV1(
        external_project_key="fixture.consumer",
        operation_key="verified-operation",
        actor="fixture",
        workflow_role="fixture:worker",
        prompt="Return exact output.",
        route=lattice_env["route"],
        output_contract=OutputContractV2(mode="text"),
        idempotency_key="fixture:verified-operation",
    )
    operation = lattice_env["lattice"].seal_execution_submission(request)
    attempt = lattice_env["store"].create_attempt(operation["invocation_id"], "verified-attempt", "fixture-boot", 9_999_999_999)
    lattice_env["store"].mark_attempt_running(attempt["attempt_id"])
    request_file = _blob(lattice_env["store"], {"driver": "codex", "model": "gpt-5.6-sol"})
    checkpoint_file = _blob(lattice_env["store"], {"request_sha256": request_file["sha256"]})
    terminal_file = _blob(lattice_env["store"], {"status": "success"})
    receipt = {
        "kind": "lattice-model-runtime-receipt",
        "journal_path": "fixture:journal",
        "request_sha256": request_file["sha256"],
        "checkpoint_sha256": checkpoint_file["sha256"],
        "terminal_sha256": terminal_file["sha256"],
        "driver": "codex",
        "model": "gpt-5.6-sol",
        "cli_version": "fixture",
        "binary": "/fixture/codex",
        "isolation": {},
        "native_session": None,
        "observed": {},
        "terminal": {"status": "success"},
        "files": {"request.json": request_file, "checkpoint.json": checkpoint_file, "terminal.json": terminal_file},
    }
    lattice_env["store"].finish_successful_attempt(attempt["attempt_id"], response="exact output", artifacts=[{"name": "result.txt", "media_type": "text/plain", "content": "exact output"}], journal_path="fixture:journal", runtime_receipt=receipt)
    return operation["invocation_id"]


@pytest.mark.oracle
def test_independent_verifier_reconciles_external_execution_from_base_records(lattice_env) -> None:
    _completed_external(lattice_env)

    result = verifier.verify_database(lattice_env["config"].data_root)

    assert result["external_operations"] == 1
    assert result["referenced_blobs"] >= 5


@pytest.mark.regression
def test_negative_control_verifier_rejects_changed_runtime_receipt_blob(lattice_env) -> None:
    invocation_id = _completed_external(lattice_env)
    with sqlite3.connect(lattice_env["config"].database) as connection:
        receipt = json.loads(connection.execute("SELECT runtime_receipt_json FROM attempts WHERE invocation_id=?", (invocation_id,)).fetchone()[0])
    digest = receipt["files"]["terminal.json"]["sha256"]
    path = lattice_env["config"].blobs / digest[:2] / digest
    path.write_bytes(b'{"status":"failed"}\n')

    with pytest.raises(verifier.ConsolidationVerificationError, match="changed or disagrees"):
        verifier.verify_database(lattice_env["config"].data_root)


@pytest.mark.regression
def test_negative_control_verifier_rejects_external_operation_without_lattice_receipt(lattice_env) -> None:
    invocation_id = _completed_external(lattice_env)
    with lattice_env["store"]._connect() as connection:
        connection.create_function("lattice_replaying", 0, lambda: 1)
        connection.execute("UPDATE attempts SET runtime_receipt_json=? WHERE invocation_id=?", (canonical_bytes({"kind": "project-local-result"}).decode(), invocation_id))

    with pytest.raises(verifier.ConsolidationVerificationError, match="full Lattice runtime receipt"):
        verifier.verify_database(lattice_env["config"].data_root)


@pytest.mark.regression
def test_negative_control_consumer_census_rejects_direct_provider_launcher(tmp_path) -> None:
    canon = tmp_path / "sui/mission/projects/canon/dev/quaternary-exhaustion"
    survey = tmp_path / "sui/mission/typology/abduction/field-survey/research-pipeline"
    lattice = tmp_path / "axis/chariots/lattice"
    canon.mkdir(parents=True)
    survey.mkdir(parents=True)
    lattice.mkdir(parents=True)
    (lattice / "pyproject.toml").write_text('[project]\nname="lattice"\n', encoding="utf-8")
    (canon / "direct.py").write_text('import subprocess\nsubprocess.run(["claude", "-p", "prompt"])\n', encoding="utf-8")

    consumers = [
        {"consumer_id": "canon", "source_root": "sui/mission/projects/canon/dev/quaternary-exhaustion", "client_boundary": "sui/mission/projects/canon/dev/quaternary-exhaustion/direct.py", "forbidden_direct_authorities": ["provider-cli", "provider-sdk", "developer-api"]},
        {"consumer_id": "survey", "source_root": "sui/mission/typology/abduction/field-survey/research-pipeline", "client_boundary": "sui/mission/typology/abduction/field-survey/research-pipeline/client.py", "forbidden_direct_authorities": ["provider-cli", "provider-sdk", "developer-api"]},
    ]

    with pytest.raises(verifier.ConsolidationVerificationError, match="direct provider executable"):
        verifier.verify_consumer_census(tmp_path, consumers, require_retired_model_runner=False)


@pytest.mark.regression
def test_negative_control_forbidden_credential_canary_is_detected(tmp_path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    root.joinpath("receipt").write_bytes(b"prefix-planted-secret-suffix")

    with pytest.raises(verifier.ConsolidationVerificationError, match="credential canary"):
        verifier.scan_forbidden_bytes((root,), (b"planted-secret",))


@pytest.mark.regression
def test_negative_control_closure_requires_every_evidence_plane(lattice_env, tmp_path) -> None:
    bundle = {
        "generated_object": generated_object(
            kind="lattice-execution-closure-bundle",
            schema_name="lattice-execution-closure-bundle",
            subject="lattice:execution-consolidation",
            sources=[{"ref": "fixture:missing-evidence", "revision": "fixture", "sha256": None}],
        ),
        "contract_version": "LatticeExecutionClosureBundle/1.0",
        "target_version": "0.3.1",
        "evidence": {},
    }
    path = tmp_path / "bundle.json"
    path.write_bytes(canonical_bytes(bundle))

    with pytest.raises(verifier.ConsolidationVerificationError, match="omits or adds evidence"):
        verifier.verify(bundle_path=path, data_root=lattice_env["config"].data_root, axisvi_root=tmp_path, forbidden_files=[], rerun_suites=False, rerun_doctor=False)


@pytest.mark.regression
def test_negative_control_zero_historical_execution_set_cannot_pass(lattice_env, tmp_path) -> None:
    value = {
        "generated_object": generated_object(
            kind="lattice-historical-execution-set",
            schema_name="lattice-historical-execution-set",
            subject="lattice:historical-execution:field-survey",
            role="snapshot",
            sources=[{"ref": "fixture:history", "revision": "empty", "sha256": None}],
        ),
        "contract_version": "HistoricalExecutionSet/1.0",
        "external_project_key": "field-survey.research-pipeline",
        "source_root": "history",
        "run_root": ".",
        "runs": [],
    }
    (tmp_path / "history").mkdir()

    with pytest.raises(verifier.ConsolidationVerificationError, match="is empty"):
        verifier.verify_historical_execution_set(value, data_root=lattice_env["config"].data_root, axisvi_root=tmp_path)


@pytest.mark.regression
def test_negative_control_open_s2_prevents_final_attestation() -> None:
    value = {
        "generated_object": generated_object(
            kind="lattice-execution-consolidation-findings",
            schema_name="lattice-execution-consolidation-findings",
            subject="lattice:execution-consolidation-findings",
            sources=[{"ref": "fixture:findings", "revision": "one", "sha256": None}],
        ),
        "contract_version": "LatticeExecutionConsolidationFindings/2.0",
        "findings": [{"finding_id": "fixture-s2", "severity": "S2", "status": "implemented-pending-verification"}],
    }

    with pytest.raises(verifier.ConsolidationVerificationError, match="remain open"):
        verifier.verify_findings(value, allow_pending=False)
    assert verifier.verify_findings(value, allow_pending=True)["blockers"] == 1


@pytest.mark.regression
def test_negative_control_historical_import_must_still_resolve_to_exact_source_bytes(lattice_env, tmp_path) -> None:
    history_root = tmp_path / "history"
    run = history_root / "run-fixture"
    run.mkdir(parents=True)
    source = run / "terminal.json"
    source.write_text('{"status":"success"}\n', encoding="utf-8")
    files = [{"relative_path": "terminal.json", "content_sha256": verifier.sha256_file(source), "byte_count": source.stat().st_size}]
    request = {
        "contract_version": "HistoricalExecutionImport/1.0",
        "external_project_key": "fixture.history",
        "operation_key": "historical:run-fixture",
        "source_ref": "run-fixture",
        "source_census_sha256": sha256_bytes(canonical_bytes(files)),
        "files": files,
        "provider": "openai",
        "model": "historical-codex",
        "original_invocation_id": "fixture-run",
        "terminal_state": "success",
    }
    lattice_env["store"].import_historical_execution(request, sha256_bytes(canonical_bytes(request)))

    verified = verifier.verify_database(lattice_env["config"].data_root, historical_roots={"fixture.history": history_root})
    assert verified["historical_sources_verified"] == 1

    source.write_text('{"status":"rewritten"}\n', encoding="utf-8")
    with pytest.raises(verifier.ConsolidationVerificationError, match="no longer match"):
        verifier.verify_database(lattice_env["config"].data_root, historical_roots={"fixture.history": history_root})
