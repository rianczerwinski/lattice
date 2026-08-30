"""Assurance contracts for Lattice's public model-execution boundary."""

from __future__ import annotations

import sqlite3

import pytest

from lattice.canonical import sha256_json
from lattice.contracts import AttemptState, ExecutionSubmissionV1, HistoricalExecutionImportV1, OutputContractV2, ProviderRoute
from lattice.store import LatticeConflict
from lattice.execution import SubscriptionRouteUnavailable
from lattice.supervisor import ExecutionSupervisor


def _submission(lattice_env, *, operation: str = "one-call") -> ExecutionSubmissionV1:
    return ExecutionSubmissionV1(
        external_project_key="fixture.consumer",
        operation_key=operation,
        actor="fixture",
        workflow_role="fixture:worker",
        prompt="Return the exact fixture result.",
        route=lattice_env["route"],
        output_contract=OutputContractV2(mode="text"),
        idempotency_key=f"fixture:{operation}:stable",
    )


@pytest.mark.contract
def test_output_contract_modes_compile_to_lossless_provider_shapes() -> None:
    payload_schema = {"type": "object", "additionalProperties": False, "required": ["answer"], "properties": {"answer": {"type": "string"}}}

    assert OutputContractV2().provider_schema()["required"] == ["response", "artifacts"]
    assert OutputContractV2(mode="json", json_schema=payload_schema).provider_schema()["properties"]["payload"] == payload_schema
    assert OutputContractV2(mode="text").provider_schema()["required"] == ["content"]
    assert OutputContractV2(mode="markdown").media_type == "text/markdown"

    with pytest.raises(ValueError, match="JSON output requires"):
        OutputContractV2(mode="json")
    with pytest.raises(ValueError, match="cannot carry"):
        OutputContractV2(mode="text", json_schema=payload_schema)


@pytest.mark.property
def test_external_operation_is_exactly_once_and_terminalizes_with_attempt(lattice_env) -> None:
    request = _submission(lattice_env)
    first = lattice_env["lattice"].seal_execution_submission(request)
    repeated = lattice_env["lattice"].seal_execution_submission(request)
    assert first["invocation_id"] == repeated["invocation_id"]

    attempt = lattice_env["store"].create_attempt(first["invocation_id"], "fixture-external-attempt", "fixture-boot", 9_999_999_999)
    lattice_env["store"].mark_attempt_running(attempt["attempt_id"])
    lattice_env["store"].finish_successful_attempt(
        attempt["attempt_id"],
        response="exact output",
        artifacts=[{"name": "result.txt", "media_type": "text/plain", "content": "exact output"}],
        journal_path="fixture:journal",
        runtime_receipt={"observed": {"model": "gpt-5.6-sol"}},
    )
    assert lattice_env["store"].get_external_submission("fixture.consumer", "one-call")["state"] == "terminal"

    with sqlite3.connect(lattice_env["config"].database) as conn:
        changes_before = conn.execute("SELECT COUNT(*) FROM ledger_changes").fetchone()[0]
    assert lattice_env["store"].external_execution_result("fixture.consumer", "one-call")["invocation"]["state"] == "succeeded"
    with sqlite3.connect(lattice_env["config"].database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ledger_changes").fetchone()[0] == changes_before

    changed = request.model_copy(update={"prompt": "different bytes"})
    with pytest.raises(LatticeConflict, match="different bytes"):
        lattice_env["lattice"].seal_execution_submission(changed)


@pytest.mark.regression
def test_failed_attempt_terminalizes_external_operation(lattice_env) -> None:
    operation = lattice_env["lattice"].seal_execution_submission(_submission(lattice_env, operation="failed-call"))
    attempt = lattice_env["store"].create_attempt(operation["invocation_id"], "fixture-failed-attempt", "fixture-boot", 9_999_999_999)
    lattice_env["store"].finish_attempt(attempt["attempt_id"], state=AttemptState.failed, failure_kind="timeout", error_text="fixture timeout")

    assert lattice_env["store"].get_external_submission("fixture.consumer", "failed-call")["state"] == "terminal"


@pytest.mark.contract
def test_historical_import_binds_exact_file_census_and_is_immutable(lattice_env) -> None:
    files = [{"relative_path": "run/request.json", "content_sha256": "1" * 64, "byte_count": 17}]
    request = HistoricalExecutionImportV1(
        external_project_key="field-survey.research-pipeline",
        operation_key="history:fixture",
        source_ref="runtime/model-runs/fixture",
        source_census_sha256=sha256_json(files),
        files=files,
        provider="anthropic",
        model="opus",
        terminal_state="success",
    )
    material = request.model_dump(mode="json")
    imported = lattice_env["store"].import_historical_execution(material, sha256_json(material))
    replayed = lattice_env["store"].import_historical_execution(material, sha256_json(material))
    assert replayed["import_id"] == imported["import_id"]

    altered = request.model_copy(update={"terminal_state": "failed"}).model_dump(mode="json")
    with pytest.raises(LatticeConflict, match="different bytes"):
        lattice_env["store"].import_historical_execution(altered, sha256_json(altered))
    with lattice_env["store"]._connect() as conn, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE historical_execution_imports SET terminal_state='rewritten' WHERE import_id=?", (imported["import_id"],))


@pytest.mark.regression
def test_historical_import_rejects_forged_census_hash() -> None:
    with pytest.raises(ValueError, match="does not bind"):
        HistoricalExecutionImportV1(
            external_project_key="fixture",
            operation_key="forged",
            source_ref="runtime/model-runs/forged",
            source_census_sha256="0" * 64,
            files=[{"relative_path": "terminal.json", "content_sha256": "2" * 64, "byte_count": 2}],
        )


@pytest.mark.contract
def test_empty_historical_directory_is_preserved_as_explicitly_incomplete(lattice_env) -> None:
    request = HistoricalExecutionImportV1(
        external_project_key="field-survey.research-pipeline",
        operation_key="history:empty-fixture",
        source_ref="runtime/model-runs/empty-fixture",
        source_census_sha256=sha256_json([]),
        files=[],
        terminal_state="incomplete-empty",
    )
    material = request.model_dump(mode="json")

    imported = lattice_env["store"].import_historical_execution(material, sha256_json(material))

    assert imported["files"] == []
    assert imported["terminal_state"] == "incomplete-empty"


@pytest.mark.regression
def test_negative_control_empty_historical_directory_cannot_claim_success() -> None:
    with pytest.raises(ValueError, match="incomplete-empty"):
        HistoricalExecutionImportV1(
            external_project_key="field-survey.research-pipeline",
            operation_key="history:false-success",
            source_ref="runtime/model-runs/false-success",
            source_census_sha256=sha256_json([]),
            files=[],
            terminal_state="success",
        )


@pytest.mark.regression
def test_historical_provider_route_cannot_claim_or_regain_execution_authority(lattice_env) -> None:
    with pytest.raises(ValueError, match="cannot claim"):
        ProviderRoute(provider="anthropic", model="historical-opus", harness="claude", route_kind="historical-import", provider_profile_id=lattice_env["route"].provider_profile_id)

    route = ProviderRoute(provider="anthropic", model="historical-opus", harness="claude", route_kind="historical-import")
    with pytest.raises(SubscriptionRouteUnavailable, match="cannot execute"):
        ExecutionSupervisor(lattice_env["store"])._adapter(route.model_dump(mode="json"))


@pytest.mark.contract
def test_provider_qualification_receipts_are_append_only(lattice_env) -> None:
    profile_id = lattice_env["route"].provider_profile_id
    receipt = lattice_env["store"].record_provider_qualification(profile_id, "3" * 64, {"control_surface_qualified": True}, state="passed")
    assert lattice_env["store"].latest_provider_qualification(profile_id)["qualification_id"] == receipt["qualification_id"]

    with lattice_env["store"]._connect() as conn, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("DELETE FROM provider_qualifications WHERE qualification_id=?", (receipt["qualification_id"],))
