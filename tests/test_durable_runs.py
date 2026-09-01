"""Focused contracts and state properties for first-class durable runs."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from lattice import LatticeConfig, LatticeStore
from lattice.canonical import canonical_bytes, sha256_bytes
from lattice.contracts import (
    DurableRunAuthorityV1,
    DurableRunDeclarationV1,
    DurableRunUnitState,
    RunArtifactClass,
    RunArtifactCreateV1,
    RunAttemptFinishV1,
    RunAttemptStartV1,
    RunUnitDeclarationV1,
    RunUnitResultV1,
)
from lattice.diagnostics import assess_data_root
from lattice.store import LatticeConflict


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _declaration(lattice_env, *, configuration: str = "configuration-a", operation_key: str = "three-units") -> DurableRunDeclarationV1:
    units = [
        RunUnitDeclarationV1(unit_key="one", input_sha256=_digest("input-one")),
        RunUnitDeclarationV1(unit_key="two", input_sha256=_digest("input-two"), depends_on=["one"]),
        RunUnitDeclarationV1(unit_key="three", input_sha256=_digest("input-three"), depends_on=["two"]),
    ]
    unit_manifest_sha256 = sha256_bytes(canonical_bytes({"unit_namespace": "fixture", "units": [unit.model_dump(mode="json") for unit in units]}))
    return DurableRunDeclarationV1(
        project_id=lattice_env["project"]["project_id"],
        external_project_key="fixture-project",
        operation_key=operation_key,
        name="Three deterministic units",
        executable_ref="tests/fixtures/deterministic-worker",
        executable_sha256=_digest("worker-bytes"),
        code_revision="fixture-revision",
        configuration_sha256=_digest(configuration),
        inputs_sha256=_digest("all-inputs"),
        environment_profile_sha256=_digest("environment"),
        executor_identity="lattice-local-process-v1",
        executor_sha256=_digest("executor"),
        unit_namespace="fixture",
        resume_strategy="repeat-active-unit-from-project-checkpoint",
        unit_manifest_sha256=unit_manifest_sha256,
        units=units,
        authority=DurableRunAuthorityV1(kind="operator", actor_ref="test-operator"),
    )


def _start(key: str) -> RunAttemptStartV1:
    return RunAttemptStartV1(
        idempotency_key=key,
        executor_identity="lattice-local-process-v1",
        owner_boot_id=f"boot-{key}",
        executor_binding={"transport": "fixture"},
    )


@pytest.mark.contract
def test_contract_durable_run_declaration_binds_a_valid_semantic_population(lattice_env) -> None:
    declaration = _declaration(lattice_env)
    invalid_manifest = declaration.model_dump(mode="json")
    invalid_manifest["unit_manifest_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="unit manifest hash"):
        DurableRunDeclarationV1.model_validate(invalid_manifest)

    cyclic = declaration.model_dump(mode="json")
    cyclic["units"][0]["depends_on"] = ["three"]
    cyclic["unit_manifest_sha256"] = sha256_bytes(canonical_bytes({"unit_namespace": "fixture", "units": cyclic["units"]}))
    with pytest.raises(ValidationError, match="contain a cycle"):
        DurableRunDeclarationV1.model_validate(cyclic)

    with pytest.raises(ValidationError, match="regeneration method"):
        RunArtifactCreateV1(
            artifact_key="cache",
            name="Recomputable cache",
            artifact_class=RunArtifactClass.disposable,
            media_type="application/octet-stream",
            byte_count=4,
            content_sha256=_digest("cache"),
            storage_locator="project:cache/cache.bin",
        )


@pytest.mark.contract
def test_contract_run_operation_and_attempt_identities_are_byte_bound(lattice_env) -> None:
    store = lattice_env["store"]
    declaration = _declaration(lattice_env)
    first = store.declare_run(declaration)
    retry = store.declare_run(declaration)

    assert retry["run_id"] == first["run_id"]
    assert retry["declaration_sha256"] == first["declaration_sha256"]
    with pytest.raises(LatticeConflict, match="different bound bytes"):
        store.declare_run(_declaration(lattice_env, configuration="configuration-b"))

    store.admit_run(first["run_id"])
    attempt = store.start_run_attempt(first["run_id"], _start("attempt-one"))
    assert store.start_run_attempt(first["run_id"], _start("attempt-one"))["attempt_id"] == attempt["attempt_id"]
    assert store.reconcile_run_orphans("adopting-boot", live_attempt_ids=(attempt["attempt_id"],)) == {"adopted_attempt_ids": [attempt["attempt_id"]], "orphaned_attempt_ids": []}
    assert store.get_run_attempt(attempt["attempt_id"])["owner_boot_id"] == "adopting-boot"
    changed = _start("attempt-one").model_copy(update={"executor_binding": {"transport": "changed"}})
    with pytest.raises(LatticeConflict, match="different bound bytes"):
        store.start_run_attempt(first["run_id"], changed)
    store.request_run_stop(first["run_id"])
    store.finish_run_attempt(attempt["attempt_id"], RunAttemptFinishV1(state="stopped"))


@pytest.mark.property
def test_property_resume_preserves_completed_units_and_rebuilds_the_same_state(lattice_env, tmp_path) -> None:
    store = lattice_env["store"]
    run = store.declare_run(_declaration(lattice_env, operation_key="resume-property"))
    store.admit_run(run["run_id"])
    first_attempt = store.start_run_attempt(run["run_id"], _start("resume-first"))

    store.start_run_unit(run["run_id"], first_attempt["attempt_id"], "one")
    store.finish_run_unit(run["run_id"], first_attempt["attempt_id"], "one", RunUnitResultV1(state=DurableRunUnitState.completed, output_sha256=_digest("result-one")))
    checkpoint_bytes = b'{"completed":["one"]}'
    checkpoint_sha = store.blobs.put(checkpoint_bytes)
    checkpoint = store.record_run_artifact(
        run["run_id"],
        first_attempt["attempt_id"],
        "one",
        RunArtifactCreateV1(
            artifact_key="checkpoint-one",
            name="Checkpoint after unit one",
            artifact_class=RunArtifactClass.checkpoint,
            media_type="application/json",
            byte_count=len(checkpoint_bytes),
            content_sha256=checkpoint_sha,
            storage_locator=f"lattice:blob/{checkpoint_sha}",
        ),
    )
    scratch_path = tmp_path / "scratch-one.bin"
    scratch_path.write_bytes(b"scratch")
    disposable = store.record_run_artifact(
        run["run_id"],
        first_attempt["attempt_id"],
        "one",
        RunArtifactCreateV1(
            artifact_key="scratch-one",
            name="Recomputable scratch result",
            artifact_class=RunArtifactClass.disposable,
            media_type="application/octet-stream",
            byte_count=7,
            content_sha256=_digest("scratch"),
            storage_locator=f"file:{scratch_path}",
            regeneration={"method": "rerun-unit", "unit_key": "one"},
        ),
    )
    with pytest.raises(LatticeConflict, match="cannot enter the disposable removal path"):
        store.mark_run_artifact_removed(checkpoint["artifact_id"], reason="planted invalid cleanup")
    assert store.mark_run_artifact_removed(disposable["artifact_id"], reason="scratch bytes removed by fixture")["state"] == "removed"
    store.start_run_unit(run["run_id"], first_attempt["attempt_id"], "two")
    assert assess_data_root(lattice_env["config"].data_root)["active_work"]["runs"] == 1
    reconciled = store.reconcile_run_orphans("boot-after-restart")
    assert reconciled == {"adopted_attempt_ids": [], "orphaned_attempt_ids": [first_attempt["attempt_id"]]}

    stopped = store.get_run(run["run_id"])
    assert [unit["state"] for unit in stopped["units"]] == ["completed", "eligible", "pending"]
    assert sum(stopped["unit_counts"].values()) == 3
    assert stopped["attempts"][0]["state"] == "orphaned"

    second_attempt = store.resume_run(run["run_id"], _start("resume-second"))
    assert second_attempt["checkpoint_artifact_id"] == checkpoint["artifact_id"]
    store.start_run_unit(run["run_id"], second_attempt["attempt_id"], "two")
    store.finish_run_unit(run["run_id"], second_attempt["attempt_id"], "two", RunUnitResultV1(state="completed", output_sha256=_digest("result-two")))
    store.start_run_unit(run["run_id"], second_attempt["attempt_id"], "three")
    store.finish_run_unit(run["run_id"], second_attempt["attempt_id"], "three", RunUnitResultV1(state="completed", output_sha256=_digest("result-three")))
    store.finish_run_attempt(second_attempt["attempt_id"], RunAttemptFinishV1(state="completed"))

    completed = store.get_run(run["run_id"])
    assert completed["state"] == "completed"
    assert completed["unit_counts"]["completed"] == 3
    assert len(completed["attempts"]) == 2
    assert completed["units"][0]["last_attempt_id"] == first_attempt["attempt_id"]
    assert completed["units"][1]["last_attempt_id"] == second_attempt["attempt_id"]
    assert completed["present_artifact_bytes"]["checkpoint"] == len(checkpoint_bytes)
    assert completed["present_artifact_bytes"]["disposable"] == 0

    expected_digest = store.state_digest()
    destination = tmp_path / "rebuilt-runs" / "lattice.sqlite3"
    assert store.rebuild_database(destination) == expected_digest
    rebuilt = LatticeStore(LatticeConfig(data_root=destination.parent, port=9010))
    assert rebuilt.state_digest() == expected_digest
    assert rebuilt.get_run(run["run_id"])["unit_counts"]["completed"] == 3
