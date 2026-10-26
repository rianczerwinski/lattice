"""Discriminating end-to-end oracle for the local durable-run executor."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import lattice.run_supervisor as run_supervisor_runtime
import lattice.run_executor as run_executor_runtime
from lattice import LatticeConfig, LatticeStore
from lattice.canonical import canonical_bytes, sha256_bytes
from lattice.contracts import (
    DurableRunAuthorityV1,
    DurableRunDeclarationV2,
    LocalFileBindingV1,
    LocalFileClosureV1,
    LocalRunEnvironmentLiteralV1,
    LocalRunLaunchProfileV1,
    RunArtifactClass,
    RunArtifactCreateV1,
    RunAttemptStartV1,
    RunProcessIdentityV2,
    RunUnitDeclarationV1,
)
from lattice.run_executor import LOCAL_EXECUTOR_IDENTITY, executor_implementation_manifest, executor_implementation_sha256, sha256_executable, sha256_file
from lattice.run_protocol import UnitStartedEvent
from lattice.run_supervisor import DurableRunSupervisor
from lattice.snapshot import create_snapshot, restore_snapshot
from lattice.store import LatticeConflict


WORKER = Path(__file__).parent / "fixtures" / "durable_run_worker.py"
CRASH_HOST = Path(__file__).parent / "fixtures" / "durable_run_crash_host.py"
REPO_ROOT = Path(__file__).parents[1]


def _unit_manifest(units: list[RunUnitDeclarationV1]) -> str:
    return sha256_bytes(canonical_bytes({"unit_namespace": "three-unit-oracle", "units": [unit.model_dump(mode="json") for unit in units]}))


def _bound_fixture(
    lattice_env,
    tmp_path: Path,
    *,
    operation_key: str,
    behavior: str,
    block_phase: str = "before-work",
    environment_overrides: dict[str, str] | None = None,
    additional_code_paths: tuple[Path, ...] = (),
    max_parallel_units: int = 1,
) -> tuple[DurableRunDeclarationV2, Path, Path]:
    work_root = tmp_path / "project-state"
    work_root.mkdir()
    worker_path = work_root / "worker.py"
    shutil.copyfile(WORKER, worker_path)
    input_manifest = work_root / "inputs.manifest.json"
    input_manifest.write_bytes(canonical_bytes({"fixture": "independent-three-unit-inputs", "version": 1}))
    release_file = tmp_path / "release-two"
    executable = Path(sys.executable).absolute()
    command = [
        str(executable),
        str(worker_path.resolve(strict=True)),
        "--work-root",
        str(work_root),
        "--block-unit",
        "two",
        "--release-file",
        str(release_file),
        "--block-phase",
        block_phase,
        "--behavior",
        behavior,
    ]
    environment = {"PYTHONPATH": str(REPO_ROOT), "PYTHONUNBUFFERED": "1"}
    environment.update(environment_overrides or {})
    configuration_sha256 = sha256_bytes(canonical_bytes({"argv": command[1:]}))
    environment_profile_sha256 = sha256_bytes(canonical_bytes({"cwd": str(work_root.resolve()), "environment": environment, "artifact_roots": [str(work_root.resolve())]}))
    worker_digest, worker_bytes = sha256_file(worker_path)
    input_digest, input_bytes = sha256_file(input_manifest)
    code_bindings = [LocalFileBindingV1(path=str(worker_path.resolve()), content_sha256=worker_digest, byte_count=worker_bytes)]
    for path in additional_code_paths:
        digest, byte_count = sha256_file(path)
        code_bindings.append(LocalFileBindingV1(path=str(path.resolve()), content_sha256=digest, byte_count=byte_count))
    code_closure = LocalFileClosureV1(bindings=code_bindings)
    input_closure = LocalFileClosureV1(bindings=[LocalFileBindingV1(binding_kind="manifest", path=str(input_manifest.resolve()), content_sha256=input_digest, byte_count=input_bytes)])
    units = [
        RunUnitDeclarationV1(unit_key="one", input_sha256=sha256_bytes(b"input-one")),
        RunUnitDeclarationV1(unit_key="two", input_sha256=sha256_bytes(b"input-two"), depends_on=["one"]),
        RunUnitDeclarationV1(unit_key="three", input_sha256=sha256_bytes(b"input-three"), depends_on=["two"]),
    ]
    declaration = DurableRunDeclarationV2(
        project_id=lattice_env["project"]["project_id"],
        external_project_key=f"fixture:{operation_key}",
        operation_key=operation_key,
        name="Independent three-unit worker",
        executable_ref=str(executable),
        executable_sha256=sha256_executable(executable)[0],
        code_revision=code_closure.identity_sha256(),
        configuration_sha256=configuration_sha256,
        inputs_sha256=input_closure.identity_sha256(),
        environment_profile_sha256=environment_profile_sha256,
        executor_identity=LOCAL_EXECUTOR_IDENTITY,
        executor_sha256=executor_implementation_sha256(),
        unit_namespace="three-unit-oracle",
        resume_strategy="project-checkpoint-repeat-active-unit",
        max_parallel_units=max_parallel_units,
        unit_manifest_sha256=_unit_manifest(units),
        units=units,
        authority=DurableRunAuthorityV1(kind="operator", actor_ref="durable-run-test"),
        code_closure=code_closure,
        input_closure=input_closure,
        local_launch_profile=LocalRunLaunchProfileV1(
            command=command,
            cwd=str(work_root.resolve()),
            environment={key: LocalRunEnvironmentLiteralV1(exposure="persistable_non_secret", value=value) for key, value in environment.items()},
            artifact_roots=[str(work_root.resolve())],
        ),
    )
    return declaration, work_root, release_file


async def _wait_until(predicate, *, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("durable-run oracle condition was not reached")


@pytest.mark.property
def test_executor_identity_tracks_a_transitive_local_source_mutation(tmp_path) -> None:
    source_root = tmp_path / "executor-closure"
    source_root.mkdir()
    source_root.joinpath("__init__.py").write_text("", encoding="utf-8")
    source_root.joinpath("entry.py").write_text("from . import dependency\n", encoding="utf-8")
    dependency = source_root / "dependency.py"
    dependency.write_text("VALUE = 1\n", encoding="utf-8")

    before_manifest = executor_implementation_manifest(source_root, ("entry.py",))
    before_digest = executor_implementation_sha256(source_root, ("entry.py",))
    dependency.write_text("VALUE = 2\n", encoding="utf-8")
    after_digest = executor_implementation_sha256(source_root, ("entry.py",))

    assert [item["path"] for item in before_manifest] == ["__init__.py", "dependency.py", "entry.py"]
    assert after_digest != before_digest


@pytest.mark.asyncio
@pytest.mark.contract
async def test_parallel_start_respects_the_declaration_bound_limit(lattice_env, tmp_path) -> None:
    declaration, _work_root, _release_file = _bound_fixture(
        lattice_env,
        tmp_path,
        operation_key="one-active-unit",
        behavior="normal",
    )
    independent_units = [
        RunUnitDeclarationV1(unit_key="alpha", input_sha256=sha256_bytes(b"input-alpha")),
        RunUnitDeclarationV1(unit_key="beta", input_sha256=sha256_bytes(b"input-beta")),
        RunUnitDeclarationV1(unit_key="gamma", input_sha256=sha256_bytes(b"input-gamma")),
    ]
    declaration = DurableRunDeclarationV2.model_validate(
        {
            **declaration.model_dump(mode="json"),
            "units": [unit.model_dump(mode="json") for unit in independent_units],
            "max_parallel_units": 2,
            "unit_manifest_sha256": _unit_manifest(independent_units),
        }
    )
    run = lattice_env["store"].declare_run(declaration)
    lattice_env["store"].admit_run(run["run_id"])
    attempt = lattice_env["store"].start_run_attempt(
        run["run_id"],
        RunAttemptStartV1(
            idempotency_key="one-active-unit-attempt",
            executor_identity=LOCAL_EXECUTOR_IDENTITY,
            owner_boot_id="parallel-start-test",
            executor_binding={"kind": "parallel-start-test"},
        ),
    )
    stores = (LatticeStore(lattice_env["config"]), LatticeStore(lattice_env["config"]))
    barrier = threading.Barrier(3)

    def start(store: LatticeStore, unit_key: str):
        barrier.wait()
        return store.start_run_unit(run["run_id"], attempt["attempt_id"], unit_key)

    outcomes = await asyncio.gather(
        asyncio.to_thread(start, stores[0], "alpha"),
        asyncio.to_thread(start, stores[1], "beta"),
        asyncio.to_thread(start, LatticeStore(lattice_env["config"]), "gamma"),
        return_exceptions=True,
    )

    assert len([outcome for outcome in outcomes if isinstance(outcome, dict)]) == 2
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, LatticeConflict)]
    assert len(conflicts) == 1
    assert "declared limit is 2" in str(conflicts[0])
    states = [unit["state"] for unit in lattice_env["store"].get_run(run["run_id"])["units"]]
    assert sorted(states) == ["eligible", "running", "running"]


@pytest.mark.contract
def test_disposable_file_artifact_keeps_its_regenerable_external_locator(lattice_env, tmp_path) -> None:
    source = tmp_path / "recomputable-cache.bin"
    source.write_bytes(b"recomputable disposable bytes\n")
    digest, byte_count = sha256_file(source)
    artifact = RunArtifactCreateV1(
        artifact_key="recomputable-cache",
        name="Recomputable cache",
        artifact_class=RunArtifactClass.disposable,
        media_type="application/octet-stream",
        byte_count=byte_count,
        content_sha256=digest,
        storage_locator=f"file:{source.resolve()}",
        regeneration={"method": "rerun-unit", "unit_key": "alpha"},
    )
    supervisor = DurableRunSupervisor(lattice_env["store"], journal_root=tmp_path / "journals")
    execution = SimpleNamespace(bound=SimpleNamespace(artifact_roots=(tmp_path.resolve(),)))

    normalized = supervisor._normalize_worker_artifact(execution, artifact)

    assert normalized == artifact
    assert normalized.storage_locator == f"file:{source.resolve()}"
    assert not lattice_env["store"].blobs.path_for(digest).exists()


@pytest.mark.contract
def test_durable_artifact_capture_rejects_a_source_swap_after_initial_verification(lattice_env, tmp_path, monkeypatch) -> None:
    source = tmp_path / "durable-evidence.bin"
    source.write_bytes(b"original\n")
    digest, byte_count = sha256_file(source)
    artifact = RunArtifactCreateV1(
        artifact_key="durable-evidence",
        name="Durable evidence",
        artifact_class=RunArtifactClass.durable_evidence,
        media_type="application/octet-stream",
        byte_count=byte_count,
        content_sha256=digest,
        storage_locator=f"file:{source.resolve()}",
    )
    supervisor = DurableRunSupervisor(lattice_env["store"], journal_root=tmp_path / "journals")
    execution = SimpleNamespace(bound=SimpleNamespace(artifact_roots=(tmp_path.resolve(),)))
    original_verify = run_supervisor_runtime.verify_worker_artifact

    def swap_after_verify(*args, **kwargs):
        verified = original_verify(*args, **kwargs)
        source.write_bytes(b"mutated!\n")
        return verified

    monkeypatch.setattr(run_supervisor_runtime, "verify_worker_artifact", swap_after_verify)

    with pytest.raises(OSError, match="admitted identity"):
        supervisor._normalize_worker_artifact(execution, artifact)
    assert not lattice_env["store"].blobs.path_for(digest).exists()
    assert list(lattice_env["store"].blobs.root.rglob("*.pending")) == []


async def _interrupt_during_two(lattice_env, tmp_path: Path, *, operation_key: str, behavior: str):
    declaration, work_root, release_file = _bound_fixture(lattice_env, tmp_path, operation_key=operation_key, behavior=behavior)
    run = lattice_env["store"].declare_run(declaration)
    lattice_env["store"].admit_run(run["run_id"])
    journals = tmp_path / "journals"
    first = DurableRunSupervisor(lattice_env["store"], journal_root=journals)
    await first.start()
    attempt = await first.execute(run["run_id"], f"{operation_key}-attempt-one")
    await _wait_until(lambda: (work_root / "reached-two").exists() and lattice_env["store"].get_run_unit(run["run_id"], "two")["state"] == "running")
    unit_one_path = work_root / "result-one.txt"
    unit_one_before = unit_one_path.read_bytes()
    unit_one_identity = sha256_file(unit_one_path)
    await first.shutdown_for_restart()
    assert lattice_env["store"].get_run_attempt(attempt["attempt_id"])["state"] == "running"

    restart = DurableRunSupervisor(lattice_env["store"], journal_root=journals)
    reconciled = await restart.start()
    assert reconciled["orphaned_attempt_ids"] == [attempt["attempt_id"]]
    stopped = lattice_env["store"].get_run(run["run_id"])
    assert [unit["state"] for unit in stopped["units"]] == ["completed", "eligible", "pending"]
    return run, work_root, release_file, journals, unit_one_before, unit_one_identity


@pytest.mark.asyncio
@pytest.mark.oracle
async def test_oracle_restart_repeats_only_the_interrupted_semantic_unit(lattice_env, tmp_path) -> None:
    run, work_root, release_file, journals, unit_one_before, unit_one_identity = await _interrupt_during_two(lattice_env, tmp_path, operation_key="resume-oracle", behavior="normal")
    expected_stopped_digest = lattice_env["store"].state_digest()
    destination = tmp_path / "rebuilt" / "lattice.sqlite3"
    assert lattice_env["store"].rebuild_database(destination) == expected_stopped_digest
    rebuilt = LatticeStore(LatticeConfig(data_root=destination.parent, port=19010, axisvi_root=None))
    resumed = DurableRunSupervisor(rebuilt, journal_root=journals)
    assert await resumed.start() == {"adopted_attempt_ids": [], "orphaned_attempt_ids": [], "processes_proven_dead": []}
    release_file.write_text("continue\n", encoding="utf-8")
    second = await resumed.execute(run["run_id"], "resume-oracle-attempt-two")
    terminal = await resumed.wait(second["attempt_id"])

    assert terminal["state"] == "completed", json.dumps(terminal, sort_keys=True)
    completed = rebuilt.get_run(run["run_id"])
    assert completed["unit_counts"]["completed"] == 3
    assert (work_root / "result-one.txt").read_bytes() == unit_one_before
    assert sha256_file(work_root / "result-one.txt") == unit_one_identity
    assert [item["run_id"] for item in rebuilt.list_runs(completed["project_id"], limit=10)] == [run["run_id"]]
    assert rebuilt.get_run_artifact(completed["artifacts"][0]["artifact_id"])["content_sha256"] == completed["artifacts"][0]["content_sha256"]
    events = rebuilt.run_events_after(run["run_id"], 0, 1000)
    starts = [event["subject_id"] for event in events if event["event_type"] == "durable-run-unit.started"]
    finishes = [event["subject_id"] for event in events if event["event_type"] == "durable-run-unit.completed"]
    assert starts.count(f"{run['run_id']}:one") == 1
    assert starts.count(f"{run['run_id']}:two") == 2
    assert starts.count(f"{run['run_id']}:three") == 1
    assert finishes.count(f"{run['run_id']}:one") == finishes.count(f"{run['run_id']}:two") == finishes.count(f"{run['run_id']}:three") == 1
    assert not (journals / run["run_id"] / completed["attempts"][0]["attempt_id"] / "terminal.json").exists()
    assert (journals / run["run_id"] / second["attempt_id"] / "terminal.json").is_file()


@pytest.mark.asyncio
@pytest.mark.contract
async def test_negative_control_worker_cannot_repeat_a_completed_unit(lattice_env, tmp_path) -> None:
    run, work_root, release_file, journals, unit_one_before, unit_one_identity = await _interrupt_during_two(lattice_env, tmp_path, operation_key="repeat-negative", behavior="repeat-completed-one")
    release_file.write_text("continue\n", encoding="utf-8")
    supervisor = DurableRunSupervisor(lattice_env["store"], journal_root=journals)
    await supervisor.start()
    second = await supervisor.execute(run["run_id"], "repeat-negative-attempt-two")
    terminal = await supervisor.wait(second["attempt_id"])

    assert terminal["state"] == "failed"
    assert terminal["failure_kind"] == "worker_protocol"
    assert "cannot start from completed" in terminal["error_text"]
    assert (work_root / "result-one.txt").read_bytes() == unit_one_before
    assert sha256_file(work_root / "result-one.txt") == unit_one_identity
    events = lattice_env["store"].events_after(run["project_id"], 0, 1000)
    assert sum(event["event_type"] == "durable-run-unit.started" and event["subject_id"] == f"{run['run_id']}:one" for event in events) == 1


@pytest.mark.asyncio
@pytest.mark.contract
async def test_stop_waits_for_the_worker_declared_unit_boundary(lattice_env, tmp_path) -> None:
    declaration, work_root, release_file = _bound_fixture(lattice_env, tmp_path, operation_key="boundary-stop", behavior="normal")
    run = lattice_env["store"].declare_run(declaration)
    lattice_env["store"].admit_run(run["run_id"])
    supervisor = DurableRunSupervisor(lattice_env["store"], journal_root=tmp_path / "journals")
    await supervisor.start()
    attempt = await supervisor.execute(run["run_id"], "boundary-stop-attempt-one")
    await _wait_until(lambda: (work_root / "reached-two").exists() and lattice_env["store"].get_run_unit(run["run_id"], "two")["state"] == "running")

    assert (await supervisor.request_stop(run["run_id"]))["state"] == "stopping"
    await asyncio.sleep(0.05)
    assert lattice_env["store"].get_run_attempt(attempt["attempt_id"])["state"] == "stopping"
    release_file.write_text("finish active unit\n", encoding="utf-8")
    assert (await supervisor.wait(attempt["attempt_id"]))["state"] == "stopped"
    stopped = lattice_env["store"].get_run(run["run_id"])
    assert [unit["state"] for unit in stopped["units"]] == ["completed", "completed", "eligible"]
    assert not (work_root / "result-three.txt").exists()


@pytest.mark.asyncio
@pytest.mark.oracle
async def test_stop_admits_a_unit_start_event_already_in_flight(lattice_env, tmp_path, monkeypatch) -> None:
    declaration, work_root, release_file = _bound_fixture(lattice_env, tmp_path, operation_key="inflight-boundary-stop", behavior="normal")
    run = lattice_env["store"].declare_run(declaration)
    lattice_env["store"].admit_run(run["run_id"])
    supervisor = DurableRunSupervisor(lattice_env["store"], journal_root=tmp_path / "journals")
    await supervisor.start()
    observed_inflight_start = asyncio.Event()
    admit_inflight_start = asyncio.Event()
    apply_event = supervisor._apply_event

    async def delay_second_start(execution, event):
        if isinstance(event, UnitStartedEvent) and event.unit_key == "two":
            observed_inflight_start.set()
            await admit_inflight_start.wait()
        await apply_event(execution, event)

    monkeypatch.setattr(supervisor, "_apply_event", delay_second_start)
    attempt = await supervisor.execute(run["run_id"], "inflight-boundary-stop-attempt-one")
    await asyncio.wait_for(observed_inflight_start.wait(), timeout=5)

    assert lattice_env["store"].get_run_unit(run["run_id"], "two")["state"] == "eligible"
    assert (await supervisor.request_stop(run["run_id"]))["state"] == "stopping"
    with pytest.raises(LatticeConflict, match="matching active run and attempt"):
        lattice_env["store"].start_run_unit(run["run_id"], attempt["attempt_id"], "two")
    admit_inflight_start.set()
    await _wait_until(lambda: lattice_env["store"].get_run_unit(run["run_id"], "two")["state"] == "running")
    release_file.write_text("finish active unit\n", encoding="utf-8")

    assert (await supervisor.wait(attempt["attempt_id"]))["state"] == "stopped"
    stopped = lattice_env["store"].get_run(run["run_id"])
    assert [unit["state"] for unit in stopped["units"]] == ["completed", "completed", "eligible"]
    assert not (work_root / "result-three.txt").exists()


@pytest.mark.asyncio
@pytest.mark.contract
async def test_v2_refuses_secret_environment_shapes_and_binding_drift(lattice_env, tmp_path) -> None:
    declaration, _work_root, _release_file = _bound_fixture(lattice_env, tmp_path, operation_key="binding-refusal", behavior="normal")
    profile = declaration.local_launch_profile.model_dump(mode="json")
    profile["environment"] = {"SECRET_TOKEN": {"exposure": "secret", "value": "must-not-enter-ledger"}}
    with pytest.raises(ValidationError, match="persistable_non_secret"):
        LocalRunLaunchProfileV1.model_validate(profile)
    profile = declaration.local_launch_profile.model_dump(mode="json")
    profile["process_contract"] = "detached-service"
    with pytest.raises(ValidationError, match="foreground-process-group"):
        LocalRunLaunchProfileV1.model_validate(profile)

    drifted = DurableRunDeclarationV2.model_validate({**declaration.model_dump(mode="json"), "executable_sha256": "0" * 64})
    run = lattice_env["store"].declare_run(drifted)
    lattice_env["store"].admit_run(run["run_id"])
    supervisor = DurableRunSupervisor(lattice_env["store"], journal_root=tmp_path / "journals")
    await supervisor.start()
    refused = await supervisor.execute(run["run_id"], "binding-refusal-attempt")
    assert refused["state"] == "failed"
    assert refused["failure_kind"] == "binding_drift"
    projected = lattice_env["store"].get_run(run["run_id"])
    assert projected["state"] == "failed"
    assert projected["attempts"] == [refused]


@pytest.mark.contract
@pytest.mark.asyncio
async def test_code_closure_refuses_changed_interpreter_worker_bytes_before_launch(lattice_env, tmp_path) -> None:
    declaration, work_root, _release_file = _bound_fixture(lattice_env, tmp_path, operation_key="worker-binding-drift", behavior="normal")
    run = lattice_env["store"].declare_run(declaration)
    lattice_env["store"].admit_run(run["run_id"])
    (work_root / "worker.py").write_bytes((work_root / "worker.py").read_bytes() + b"\n# changed after declaration\n")

    supervisor = DurableRunSupervisor(lattice_env["store"], journal_root=tmp_path / "journals")
    await supervisor.start()
    refused = await supervisor.execute(run["run_id"], "worker-binding-drift-attempt")

    assert refused["state"] == "failed"
    assert refused["failure_kind"] == "code_binding_drift"
    assert refused["process_identity"] is None
    assert "closure bytes changed" in refused["error_text"]


@pytest.mark.oracle
@pytest.mark.asyncio
async def test_project_python_hook_cannot_execute_before_process_identity_is_durable(lattice_env, tmp_path, monkeypatch) -> None:
    hostile_root = tmp_path / "hostile-python-path"
    hostile_root.mkdir()
    preimport_marker = tmp_path / "sitecustomize-ran"
    sitecustomize = hostile_root / "sitecustomize.py"
    sitecustomize.write_text(
        "from pathlib import Path\n"
        f"Path({str(preimport_marker)!r}).write_text('target import ran\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    declaration, _work_root, release_file = _bound_fixture(
        lattice_env,
        tmp_path,
        operation_key="pre-gate-environment-oracle",
        behavior="normal",
        environment_overrides={"PYTHONPATH": os.pathsep.join((str(hostile_root), str(REPO_ROOT)))},
        additional_code_paths=(sitecustomize,),
    )
    run = lattice_env["store"].declare_run(declaration)
    lattice_env["store"].admit_run(run["run_id"])
    release_file.write_text("continue\n", encoding="utf-8")
    observed_release = run_supervisor_runtime.release_local_run

    def release_after_identity(spawned) -> None:
        attempt = lattice_env["store"].get_run_attempt(spawned.process_identity.attempt_id)
        assert attempt["process_identity"] == spawned.process_identity.model_dump(mode="json")
        assert Path(spawned.process_identity.record_path).is_file()
        assert not preimport_marker.exists()
        observed_release(spawned)

    monkeypatch.setattr(run_supervisor_runtime, "release_local_run", release_after_identity)
    supervisor = DurableRunSupervisor(lattice_env["store"], journal_root=tmp_path / "journals")
    await supervisor.start()
    attempt = await supervisor.execute(run["run_id"], "pre-gate-environment-attempt")
    terminal = await supervisor.wait(attempt["attempt_id"])

    assert terminal["state"] == "completed"
    assert preimport_marker.read_text(encoding="utf-8") == "target import ran\n"


@pytest.mark.oracle
@pytest.mark.asyncio
async def test_checkpoint_reobservation_is_idempotent_and_ledger_still_repeats_active_unit(lattice_env, tmp_path) -> None:
    declaration, work_root, release_file = _bound_fixture(
        lattice_env,
        tmp_path,
        operation_key="artifact-crash-window",
        behavior="normal",
        block_phase="after-checkpoint",
    )
    run = lattice_env["store"].declare_run(declaration)
    lattice_env["store"].admit_run(run["run_id"])
    journals = tmp_path / "journals"
    first = DurableRunSupervisor(lattice_env["store"], journal_root=journals)
    await first.start()
    first_attempt = await first.execute(run["run_id"], "artifact-crash-window-first")

    def checkpoint_two_is_durable() -> bool:
        current = lattice_env["store"].get_run(run["run_id"])
        return (work_root / "reached-two-after-checkpoint").exists() and any(item["artifact_key"] == "checkpoint-two" for item in current["artifacts"])

    await _wait_until(checkpoint_two_is_durable)
    await first.shutdown_for_restart()
    restart = DurableRunSupervisor(lattice_env["store"], journal_root=journals)
    reconciled = await restart.start()
    assert reconciled["orphaned_attempt_ids"] == [first_attempt["attempt_id"]]
    assert lattice_env["store"].get_run_unit(run["run_id"], "two")["state"] == "eligible"

    release_file.write_text("continue\n", encoding="utf-8")
    second_attempt = await restart.execute(run["run_id"], "artifact-crash-window-second")
    terminal = await restart.wait(second_attempt["attempt_id"])

    assert terminal["state"] == "completed"
    completed = lattice_env["store"].get_run(run["run_id"])
    result_two = next(item for item in completed["artifacts"] if item["artifact_key"] == "result-two")
    checkpoint_two = next(item for item in completed["artifacts"] if item["artifact_key"] == "checkpoint-two")
    assert result_two["observed_attempt_ids"] == [first_attempt["attempt_id"], second_attempt["attempt_id"]]
    assert checkpoint_two["observed_attempt_ids"] == [first_attempt["attempt_id"], second_attempt["attempt_id"]]
    assert (work_root / f"resumed-from-{checkpoint_two['artifact_id']}.txt").read_bytes() == b"verified\n"
    events = lattice_env["store"].run_events_after(run["run_id"], 0, 1000)
    starts = [event["subject_id"] for event in events if event["event_type"] == "durable-run-unit.started"]
    assert starts.count(f"{run['run_id']}:two") == 2


@pytest.mark.oracle
@pytest.mark.asyncio
async def test_durable_artifacts_survive_source_mutation_and_checkpoint_source_deletion(lattice_env, tmp_path) -> None:
    declaration, work_root, release_file = _bound_fixture(
        lattice_env,
        tmp_path,
        operation_key="cas-artifact-custody",
        behavior="normal",
        block_phase="after-checkpoint",
    )
    run = lattice_env["store"].declare_run(declaration)
    lattice_env["store"].admit_run(run["run_id"])
    journals = tmp_path / "journals"
    first = DurableRunSupervisor(lattice_env["store"], journal_root=journals)
    await first.start()
    first_attempt = await first.execute(run["run_id"], "cas-artifact-custody-first")

    def checkpoint_two_is_durable() -> bool:
        current = lattice_env["store"].get_run(run["run_id"])
        return (work_root / "reached-two-after-checkpoint").exists() and any(item["artifact_key"] == "checkpoint-two" for item in current["artifacts"])

    await _wait_until(checkpoint_two_is_durable)
    admitted = lattice_env["store"].get_run(run["run_id"])
    result_one = next(item for item in admitted["artifacts"] if item["artifact_key"] == "result-one")
    checkpoint_two = next(item for item in admitted["artifacts"] if item["artifact_key"] == "checkpoint-two")
    assert result_one["storage_locator"] == f"lattice:blob/{result_one['content_sha256']}"
    assert checkpoint_two["storage_locator"] == f"lattice:blob/{checkpoint_two['content_sha256']}"
    result_one_bytes = b"deterministic-result:one\n"
    checkpoint_two_bytes = canonical_bytes({"completed": ["one", "two"]})
    assert lattice_env["store"].blobs.get(result_one["content_sha256"]) == result_one_bytes
    assert lattice_env["store"].blobs.get(checkpoint_two["content_sha256"]) == checkpoint_two_bytes

    (work_root / "result-one.txt").write_bytes(b"mutated after durable admission\n")
    (work_root / "checkpoint-two.json").unlink()
    assert lattice_env["store"].blobs.get(result_one["content_sha256"]) == result_one_bytes
    assert lattice_env["store"].blobs.get(checkpoint_two["content_sha256"]) == checkpoint_two_bytes

    await first.shutdown_for_restart()
    restart = DurableRunSupervisor(lattice_env["store"], journal_root=journals)
    reconciled = await restart.start()
    assert reconciled["orphaned_attempt_ids"] == [first_attempt["attempt_id"]]
    release_file.write_text("continue\n", encoding="utf-8")
    second_attempt = await restart.execute(run["run_id"], "cas-artifact-custody-second")
    terminal = await restart.wait(second_attempt["attempt_id"])

    assert terminal["state"] == "completed"
    completed = lattice_env["store"].get_run(run["run_id"])
    final_checkpoint = next(item for item in completed["artifacts"] if item["artifact_key"] == "checkpoint-two")
    assert final_checkpoint["observed_attempt_ids"] == [first_attempt["attempt_id"], second_attempt["attempt_id"]]
    assert lattice_env["store"].blobs.get(final_checkpoint["content_sha256"]) == checkpoint_two_bytes
    assert (work_root / f"resumed-from-{final_checkpoint['artifact_id']}.txt").read_bytes() == b"verified\n"


@pytest.mark.oracle
@pytest.mark.asyncio
async def test_recovery_does_not_signal_a_reused_process_group_after_guardian_lock_release(lattice_env, tmp_path, monkeypatch) -> None:
    declaration, _work_root, _release_file = _bound_fixture(
        lattice_env,
        tmp_path,
        operation_key="free-lock-reused-pgid",
        behavior="normal",
    )
    run = lattice_env["store"].declare_run(declaration)
    lattice_env["store"].admit_run(run["run_id"])
    attempt = lattice_env["store"].start_run_attempt(
        run["run_id"],
        RunAttemptStartV1(
            idempotency_key="free-lock-reused-pgid-attempt",
            executor_identity=LOCAL_EXECUTOR_IDENTITY,
            owner_boot_id="dead-supervisor-boot",
            executor_binding={"transport": "fixture"},
        ),
    )
    journals = tmp_path / "journals"
    attempt_root = journals / run["run_id"] / attempt["attempt_id"]
    attempt_root.mkdir(parents=True)
    nonce = "a" * 64
    lock_path = attempt_root / "process.lock"
    record_path = attempt_root / "process.json"
    target_environment_path = attempt_root / "target-environment.json"
    lock_path.write_text(f"{nonce}\n", encoding="ascii")
    target_environment = canonical_bytes({})
    target_environment_path.write_bytes(target_environment)
    identity = RunProcessIdentityV2(
        run_id=run["run_id"],
        attempt_id=attempt["attempt_id"],
        pid=424_242,
        process_group_id=424_242,
        nonce=nonce,
        lock_path=str(lock_path),
        record_path=str(record_path),
        target_environment_path=str(target_environment_path),
        target_environment_sha256=sha256_bytes(target_environment),
        launcher_environment_sha256="b" * 64,
    )
    record_path.write_bytes(canonical_bytes(identity.model_dump(mode="json")))
    lattice_env["store"].record_run_process(attempt["attempt_id"], identity)
    observed_signals: list[int] = []

    def reused_group_probe(_process_group_id: int, signal_number: int) -> None:
        observed_signals.append(signal_number)
        raise AssertionError("recovery consulted or signaled an unbound reused process group")

    monkeypatch.setattr(run_executor_runtime.os, "killpg", reused_group_probe)
    recovering = DurableRunSupervisor(lattice_env["store"], journal_root=journals)

    reconciled = await recovering.start()

    assert observed_signals == []
    assert reconciled["processes_proven_dead"] == []
    assert reconciled["orphaned_attempt_ids"] == [attempt["attempt_id"]]
    assert lattice_env["store"].get_run_attempt(attempt["attempt_id"])["state"] == "orphaned"


@pytest.mark.oracle
@pytest.mark.asyncio
async def test_hard_supervisor_loss_kills_old_child_before_requeue_and_resume(lattice_env, tmp_path) -> None:
    declaration, work_root, release_file = _bound_fixture(
        lattice_env,
        tmp_path,
        operation_key="hard-crash-oracle",
        behavior="close-non-protocol-fds",
    )
    run = lattice_env["store"].declare_run(declaration)
    lattice_env["store"].admit_run(run["run_id"])
    journals = tmp_path / "journals"
    ready_file = tmp_path / "crash-host-ready.json"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    host = subprocess.Popen(
        [
            sys.executable,
            str(CRASH_HOST),
            "--data-root",
            str(lattice_env["config"].data_root),
            "--journal-root",
            str(journals),
            "--run-id",
            run["run_id"],
            "--idempotency-key",
            "hard-crash-oracle-first",
            "--worker-marker",
            str(work_root / "reached-two"),
            "--ready-file",
            str(ready_file),
        ],
        cwd=REPO_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    await _wait_until(ready_file.exists, timeout=15.0)
    returncode = await asyncio.to_thread(host.wait, 10)
    assert returncode == 91
    planted = json.loads(ready_file.read_text(encoding="utf-8"))
    first_attempt_id = planted["attempt_id"]
    old_pid = planted["process_identity"]["pid"]
    os.kill(old_pid, 0)
    with Path(planted["process_identity"]["lock_path"]).open("r+b", buffering=0) as process_lock:
        with pytest.raises(BlockingIOError):
            fcntl.flock(process_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    recovering = DurableRunSupervisor(lattice_env["store"], journal_root=journals)
    reconciled = await recovering.start()
    assert reconciled["processes_proven_dead"] == [first_attempt_id]
    assert reconciled["orphaned_attempt_ids"] == [first_attempt_id]
    with Path(planted["process_identity"]["lock_path"]).open("r+b", buffering=0) as process_lock:
        fcntl.flock(process_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    release_file.write_text("continue\n", encoding="utf-8")
    second = await recovering.execute(run["run_id"], "hard-crash-oracle-second")
    terminal = await recovering.wait(second["attempt_id"])

    assert terminal["state"] == "completed"
    assert not (work_root / "concurrent-two.txt").exists()
    events = lattice_env["store"].run_events_after(run["run_id"], 0, 1000)
    starts = [event["subject_id"] for event in events if event["event_type"] == "durable-run-unit.started"]
    assert starts.count(f"{run['run_id']}:one") == 1
    assert starts.count(f"{run['run_id']}:two") == 2
    assert starts.count(f"{run['run_id']}:three") == 1


@pytest.mark.oracle
@pytest.mark.asyncio
async def test_snapshot_and_rebuild_preserve_production_journals_and_log_projection(lattice_env, tmp_path) -> None:
    declaration, _work_root, release_file = _bound_fixture(
        lattice_env,
        tmp_path,
        operation_key="journal-continuity",
        behavior="normal",
    )
    run = lattice_env["store"].declare_run(declaration)
    lattice_env["store"].admit_run(run["run_id"])
    release_file.write_text("continue\n", encoding="utf-8")
    supervisor = DurableRunSupervisor(lattice_env["store"])
    await supervisor.start()
    attempt = await supervisor.execute(run["run_id"], "journal-continuity-attempt")
    terminal = await supervisor.wait(attempt["attempt_id"])
    assert terminal["state"] == "completed", json.dumps(terminal, sort_keys=True)

    source_attempt_root = supervisor.journal_root / run["run_id"] / attempt["attempt_id"]
    source_bytes = {
        path.relative_to(source_attempt_root).as_posix(): path.read_bytes()
        for path in sorted(source_attempt_root.rglob("*"))
        if path.is_file()
    }
    assert {"events.jsonl", "launch.json", "stdout.log", "stderr.log", "terminal.json"}.issubset(source_bytes)
    state_digest = lattice_env["store"].state_digest()
    snapshot = create_snapshot(
        lattice_env["config"].data_root,
        backup_root=tmp_path / "backups",
        reason="durable-run-journal-continuity",
        source_revision=state_digest,
        schema_version=15,
        state_digest=state_digest,
    )
    restored_root = tmp_path / "restored"
    restore_snapshot(snapshot, restored_root)
    restored_store = LatticeStore(LatticeConfig(data_root=restored_root, port=19012, axisvi_root=None))
    restored_supervisor = DurableRunSupervisor(restored_store)
    await restored_supervisor.start()

    restored_attempt_root = restored_supervisor.journal_root / run["run_id"] / attempt["attempt_id"]
    restored_bytes = {
        path.relative_to(restored_attempt_root).as_posix(): path.read_bytes()
        for path in sorted(restored_attempt_root.rglob("*"))
        if path.is_file()
    }
    assert restored_bytes == source_bytes
    descriptor, byte_count = restored_supervisor.open_attempt_log(run["run_id"], attempt["attempt_id"], "stdout")
    try:
        assert os.read(descriptor, byte_count + 1) == source_bytes["stdout.log"]
    finally:
        os.close(descriptor)

    rebuilt_database = tmp_path / "rebuilt" / "lattice.sqlite3"
    assert lattice_env["store"].rebuild_database(rebuilt_database) == state_digest
    rebuilt_store = LatticeStore(LatticeConfig(data_root=rebuilt_database.parent, port=19013, axisvi_root=None))
    rebuilt_supervisor = DurableRunSupervisor(rebuilt_store)
    await rebuilt_supervisor.start()
    rebuilt_attempt_root = rebuilt_supervisor.journal_root / run["run_id"] / attempt["attempt_id"]
    rebuilt_bytes = {
        path.relative_to(rebuilt_attempt_root).as_posix(): path.read_bytes()
        for path in sorted(rebuilt_attempt_root.rglob("*"))
        if path.is_file()
    }
    assert rebuilt_bytes == source_bytes
