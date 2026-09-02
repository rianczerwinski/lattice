"""Supervise semantic local durable runs beside provider invocation execution."""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .canonical import canonical_bytes, opaque_id, stamp
from .contracts import (
    DurableRunAttemptState,
    DurableRunState,
    DurableRunUnitState,
    RunArtifactClass,
    RunArtifactCreateV1,
    RunAttemptFinishV1,
    RunAttemptStartV1,
    RunUnitResultV1,
)
from .execution.process import terminate_process_group
from .run_executor import (
    BoundLocalRun,
    LocalRunLaunchRefused,
    SpawnedLocalRun,
    bind_declared_local_run,
    load_process_identity,
    release_local_run,
    spawn_local_run,
    terminate_and_prove_process_dead,
    verify_worker_artifact,
)
from .run_journal import DURABLE_RUNS_DIRECTORY, DurableRunJournal, acquire_journal_writer
from .run_protocol import (
    ArtifactEvent,
    CheckpointEvent,
    ContinuedEvent,
    MAX_WORKER_EVENT_BYTES,
    ProgressEvent,
    ResourceEvent,
    RunCompletedEvent,
    RunFailedEvent,
    RunWorkerEventV1,
    UnitCompletedEvent,
    UnitFailedEvent,
    UnitRefusedEvent,
    UnitStartedEvent,
    WaitingEvent,
    decode_worker_event,
)
from .store import LatticeConflict, LatticeNotFound, LatticeStore


class DurableRunProtocolError(RuntimeError):
    """A worker's structured event stream violated its admitted run contract."""


@dataclass
class _LocalExecution:
    run_id: str
    attempt_id: str
    bound: BoundLocalRun
    process: subprocess.Popen[bytes]
    event_read_fd: int
    control_write_fd: int
    journal: DurableRunJournal
    task: asyncio.Task[dict[str, Any]] | None = None
    stop_requested: bool = False
    safe_to_stop: bool = False
    suspend_terminalization: bool = False
    active_units: set[str] = field(default_factory=set)
    last_sequence: int = 0
    terminal_event: RunCompletedEvent | RunFailedEvent | None = None
    stop_signal_sent: bool = False
    # A decoded worker event may be awaiting store admission when stop arrives.
    worker_event_in_flight: bool = False


class DurableRunSupervisor:
    """Run local workers without sharing provider-supervisor state or ontology."""

    def __init__(self, store: LatticeStore, *, journal_root: Path | None = None) -> None:
        self.store = store
        self.boot_id = opaque_id("runboot")
        self.journal_root = (journal_root or store.config.data_root / "durable-runs").resolve()
        self._production_journals = self.journal_root == (store.config.data_root / DURABLE_RUNS_DIRECTORY).resolve()
        self._executions: dict[str, _LocalExecution] = {}
        self.startup_reconciliation: dict[str, list[str]] = {"adopted_attempt_ids": [], "orphaned_attempt_ids": [], "processes_proven_dead": []}

    async def start(self) -> dict[str, list[str]]:
        self.journal_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.journal_root, 0o700)
        proven_dead: list[str] = []
        for attempt in self.store.active_run_attempts():
            attempt_root = self.journal_root / attempt["run_id"] / attempt["attempt_id"]
            record_path = attempt_root / "process.json"
            if attempt["process_identity"] is None and not record_path.exists():
                # The exec gate cannot open before process.json is durable and
                # the database binding follows it. No record means no project
                # code from this attempt could have run.
                continue
            if not record_path.is_file():
                raise OSError(f"active durable run lost its process identity record: {attempt['attempt_id']}")
            identity = load_process_identity(record_path, run_id=attempt["run_id"], attempt_id=attempt["attempt_id"], attempt_root=attempt_root)
            if attempt["process_identity"] is not None and canonical_bytes(attempt["process_identity"]) != canonical_bytes(identity.model_dump(mode="json")):
                raise OSError(f"active durable-run ledger identity disagrees with its process record: {attempt['attempt_id']}")
            if await asyncio.to_thread(terminate_and_prove_process_dead, identity):
                proven_dead.append(attempt["attempt_id"])
        reconciliation = self.store.reconcile_run_orphans(self.boot_id)
        self.startup_reconciliation = {**reconciliation, "processes_proven_dead": proven_dead}
        return self.startup_reconciliation

    async def execute(self, run_id: str, idempotency_key: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        existing = next((attempt for attempt in run["attempts"] if attempt["idempotency_key"] == idempotency_key), None)
        if existing is not None:
            return existing
        try:
            bound = bind_declared_local_run(run)
        except LocalRunLaunchRefused as refusal:
            return self._record_prelaunch_refusal(run, idempotency_key, refusal)
        start = RunAttemptStartV1(
            idempotency_key=idempotency_key,
            executor_identity=run["declaration"]["executor_identity"],
            owner_boot_id=self.boot_id,
            executor_binding=bound.receipt(),
        )
        if run["state"] == DurableRunState.admitted.value:
            attempt = self.store.start_run_attempt(run_id, start)
        elif run["state"] in {DurableRunState.stopped.value, DurableRunState.failed.value}:
            attempt = self.store.resume_run(run_id, start)
        else:
            raise LatticeConflict(f"local durable-run executor cannot launch from {run['state']}")
        attempt_id = attempt["attempt_id"]
        if attempt_id in self._executions:
            return attempt
        if attempt["state"] != DurableRunAttemptState.running.value:
            return attempt
        attempt_root = self.journal_root / run_id / attempt_id
        attempt_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        journal: DurableRunJournal | None = None
        coordination_fd: int | None = None
        spawned: SpawnedLocalRun | None = None
        try:
            current = self.store.get_run(run_id)
            remaining_unit_keys = [unit["unit_key"] for unit in current["units"] if unit["state"] != DurableRunUnitState.completed.value]
            checkpoint = self.store.get_run_artifact(attempt["checkpoint_artifact_id"]) if attempt["checkpoint_artifact_id"] is not None else None
            checkpoint_read_path: Path | None = None
            if checkpoint is not None:
                if checkpoint["state"] != "present" or checkpoint["artifact_class"] != RunArtifactClass.checkpoint.value:
                    raise LocalRunLaunchRefused("checkpoint_binding_drift", "selected resume checkpoint is not a present checkpoint artifact")
                try:
                    if checkpoint["storage_locator"].startswith("lattice:blob/"):
                        checkpoint_read_path, checkpoint_bytes = self.store.blobs.verify_file(checkpoint["content_sha256"])
                        if checkpoint_bytes != checkpoint["byte_count"]:
                            raise LatticeConflict("checkpoint byte count disagrees with its Lattice CAS object")
                    else:
                        checkpoint_read_path = verify_worker_artifact(
                            checkpoint["storage_locator"],
                            checkpoint["content_sha256"],
                            checkpoint["byte_count"],
                            bound.artifact_roots,
                        )
                except (LatticeConflict, OSError) as error:
                    raise LocalRunLaunchRefused("checkpoint_binding_drift", f"selected resume checkpoint changed or is unavailable: {error}") from error
                if checkpoint_read_path is None:
                    raise LocalRunLaunchRefused("checkpoint_binding_drift", "selected resume checkpoint lacks a locally verifiable read path")
            launch_receipt = {
                "contract_version": "LocalRunLaunchReceipt/1.0",
                "run_id": run_id,
                "attempt_id": attempt_id,
                "attempt_number": attempt["attempt_number"],
                "started_at": attempt["started_at"],
                "binding": bound.receipt(),
                "checkpoint_artifact_id": attempt["checkpoint_artifact_id"],
            }
            if self._production_journals:
                coordination_fd = acquire_journal_writer(self.store.config.data_root)
            journal = DurableRunJournal(attempt_root, launch_receipt, coordination_fd=coordination_fd)
            coordination_fd = None
            unit_plan_path, _unit_plan_sha256 = journal.write_unit_plan(run_id=run_id, attempt_id=attempt_id, remaining_unit_keys=remaining_unit_keys)
            spawned = spawn_local_run(
                bound,
                run_id=run_id,
                attempt_id=attempt_id,
                attempt_root=attempt_root,
                unit_plan_path=unit_plan_path,
                checkpoint=checkpoint,
                checkpoint_read_path=checkpoint_read_path,
            )
            self.store.record_run_process(attempt_id, spawned.process_identity)
            release_local_run(spawned)
        except BaseException as error:
            if coordination_fd is not None:
                os.close(coordination_fd)
            if spawned is not None:
                try:
                    os.close(spawned.gate_write_fd)
                except OSError:
                    pass
                await asyncio.to_thread(terminate_process_group, spawned.process)
                for descriptor in (spawned.event_read_fd, spawned.control_write_fd):
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                for stream in (spawned.process.stdout, spawned.process.stderr):
                    if stream is not None:
                        stream.close()
            failure_kind = error.failure_kind if isinstance(error, LocalRunLaunchRefused) else "executor_setup"
            if journal is not None:
                journal.finish({"contract_version": "LocalRunTerminal/1.0", "run_id": run_id, "attempt_id": attempt_id, "state": "failed", "failure_kind": failure_kind, "error_text": f"{type(error).__name__}: {error}", "completed_at": stamp(), "stdout_bytes": journal.stdout_bytes, "stderr_bytes": journal.stderr_bytes})
                journal.close()
            return self.store.finish_run_attempt(attempt_id, RunAttemptFinishV1(state=DurableRunAttemptState.failed, failure_kind=failure_kind, error_text=f"{type(error).__name__}: {error}"))
        execution = _LocalExecution(
            run_id=run_id,
            attempt_id=attempt_id,
            bound=bound,
            process=spawned.process,
            event_read_fd=spawned.event_read_fd,
            control_write_fd=spawned.control_write_fd,
            journal=journal,
        )
        execution.task = asyncio.create_task(self._monitor(execution), name=f"durable-run:{attempt_id}")
        self._executions[attempt_id] = execution
        execution.task.add_done_callback(lambda _task, key=attempt_id: self._executions.pop(key, None))
        return self.store.get_run_attempt(attempt_id)

    def _record_prelaunch_refusal(self, run: dict[str, Any], idempotency_key: str, refusal: LocalRunLaunchRefused) -> dict[str, Any]:
        start = RunAttemptStartV1(
            idempotency_key=idempotency_key,
            executor_identity=run["declaration"]["executor_identity"],
            owner_boot_id=self.boot_id,
            executor_binding={"kind": "local-prelaunch-refusal-v1", "failure_kind": refusal.failure_kind},
        )
        if run["state"] == DurableRunState.admitted.value:
            attempt = self.store.start_run_attempt(run["run_id"], start)
        elif run["state"] in {DurableRunState.stopped.value, DurableRunState.failed.value}:
            attempt = self.store.resume_run(run["run_id"], start)
        else:
            raise LatticeConflict(f"local durable-run executor cannot record a refusal from {run['state']}")
        return self.store.finish_run_attempt(
            attempt["attempt_id"],
            RunAttemptFinishV1(state=DurableRunAttemptState.failed, failure_kind=refusal.failure_kind, error_text=str(refusal)),
        )

    async def wait(self, attempt_id: str) -> dict[str, Any]:
        execution = self._executions.get(attempt_id)
        if execution is None or execution.task is None:
            return self.store.get_run_attempt(attempt_id)
        return await execution.task

    async def request_stop(self, run_id: str) -> dict[str, Any]:
        run = self.store.request_run_stop(run_id)
        active = next((execution for execution in self._executions.values() if execution.run_id == run_id and execution.process.poll() is None), None)
        if active is None:
            return run
        active.stop_requested = True
        self._signal_stop(active)
        if (not active.active_units and not active.worker_event_in_flight) or active.safe_to_stop or run["state"] == DurableRunState.waiting.value:
            await self._terminate(active)
        return self.store.get_run(run_id)

    def open_attempt_log(self, run_id: str, attempt_id: str, stream: str) -> tuple[int, int]:
        """Open one bounded snapshot of an attempt journal after proving ownership."""

        if stream not in {"stdout", "stderr"}:
            raise LatticeConflict("durable-run log stream must be stdout or stderr")
        attempt = self.store.get_run_attempt(attempt_id)
        if attempt["run_id"] != run_id:
            raise LatticeConflict("durable-run attempt belongs to another run")
        try:
            attempt_root = (self.journal_root / run_id / attempt_id).resolve(strict=True)
        except FileNotFoundError as error:
            raise LatticeNotFound(f"{attempt_id}:{stream}") from error
        if attempt_root.parent.parent != self.journal_root:
            raise LatticeConflict("durable-run attempt journal escaped its configured root")
        path = attempt_root / f"{stream}.log"
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError as error:
            raise LatticeNotFound(f"{attempt_id}:{stream}") from error
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise OSError("durable-run attempt log is not a regular file")
            return descriptor, details.st_size
        except BaseException:
            os.close(descriptor)
            raise

    async def shutdown_for_restart(self) -> None:
        """Simulate service/process loss without manufacturing terminal receipts."""

        executions = [execution for execution in self._executions.values() if execution.process.poll() is None]
        for execution in executions:
            execution.suspend_terminalization = True
        await asyncio.gather(*(self._terminate(execution) for execution in executions), return_exceptions=False)
        await asyncio.gather(*(execution.task for execution in executions if execution.task is not None), return_exceptions=False)

    async def _terminate(self, execution: _LocalExecution) -> None:
        await asyncio.to_thread(terminate_process_group, execution.process)

    @staticmethod
    def _signal_stop(execution: _LocalExecution) -> None:
        if execution.stop_signal_sent:
            return
        try:
            os.write(execution.control_write_fd, b"S")
        except (BrokenPipeError, OSError):
            pass
        execution.stop_signal_sent = True

    async def _pump_stream(self, stream: Any, writer: Any) -> None:
        try:
            while True:
                chunk = await asyncio.to_thread(os.read, stream.fileno(), 64 * 1024)
                if not chunk:
                    return
                await asyncio.to_thread(writer, chunk)
        finally:
            stream.close()

    async def _read_worker_events(self, execution: _LocalExecution) -> None:
        stream = os.fdopen(execution.event_read_fd, "rb", buffering=0)
        try:
            while True:
                line = await asyncio.to_thread(stream.readline, MAX_WORKER_EVENT_BYTES + 2)
                if not line:
                    return
                if not line.endswith(b"\n") or len(line) - 1 > MAX_WORKER_EVENT_BYTES:
                    raise DurableRunProtocolError("worker emitted an unterminated or oversized structured event")
                raw = line[:-1]
                try:
                    event = decode_worker_event(raw)
                except (TypeError, ValueError) as error:
                    raise DurableRunProtocolError(f"invalid structured worker event: {error}") from error
                if event.sequence != execution.last_sequence + 1:
                    raise DurableRunProtocolError(f"worker event sequence jumped from {execution.last_sequence} to {event.sequence}")
                execution.last_sequence = event.sequence
                execution.worker_event_in_flight = True
                try:
                    await self._apply_event(execution, event)
                finally:
                    execution.worker_event_in_flight = False
                execution.journal.append_event(raw)
                if isinstance(event, (UnitCompletedEvent, UnitRefusedEvent, UnitFailedEvent, ArtifactEvent, CheckpointEvent)):
                    await asyncio.to_thread(execution.journal.sync_streams)
        finally:
            stream.close()

    def _normalize_worker_artifact(self, execution: _LocalExecution, artifact: RunArtifactCreateV1) -> RunArtifactCreateV1:
        """Capture durable file artifacts into verified Lattice custody before ledger admission."""

        source = verify_worker_artifact(
            artifact.storage_locator,
            artifact.content_sha256,
            artifact.byte_count,
            execution.bound.artifact_roots,
        )
        if source is None or artifact.artifact_class == RunArtifactClass.disposable:
            return artifact
        content_sha256, byte_count = self.store.blobs.put_file(
            source,
            expected_sha256=artifact.content_sha256,
            expected_bytes=artifact.byte_count,
        )
        if content_sha256 != artifact.content_sha256 or byte_count != artifact.byte_count:
            raise LatticeConflict("captured run artifact disagrees with its worker-emitted identity")
        _cas_path, cas_bytes = self.store.blobs.verify_file(content_sha256)
        if cas_bytes != artifact.byte_count:
            raise LatticeConflict("captured run artifact byte count disagrees with its Lattice CAS object")
        return artifact.model_copy(update={"storage_locator": f"lattice:blob/{content_sha256}"})

    async def _apply_event(self, execution: _LocalExecution, event: RunWorkerEventV1) -> None:
        if execution.terminal_event is not None:
            raise DurableRunProtocolError("worker emitted an event after its terminal disposition")
        if isinstance(event, UnitStartedEvent):
            self.store.start_run_unit(execution.run_id, execution.attempt_id, event.unit_key)
            execution.active_units.add(event.unit_key)
            execution.safe_to_stop = False
        elif isinstance(event, UnitCompletedEvent):
            self.store.finish_run_unit(execution.run_id, execution.attempt_id, event.unit_key, RunUnitResultV1(state=DurableRunUnitState.completed, output_sha256=event.output_sha256))
            execution.active_units.discard(event.unit_key)
        elif isinstance(event, UnitRefusedEvent):
            self.store.finish_run_unit(execution.run_id, execution.attempt_id, event.unit_key, RunUnitResultV1(state=DurableRunUnitState.refused, failure_kind=event.failure_kind, error_text=event.error_text))
            execution.active_units.discard(event.unit_key)
        elif isinstance(event, UnitFailedEvent):
            self.store.finish_run_unit(execution.run_id, execution.attempt_id, event.unit_key, RunUnitResultV1(state=DurableRunUnitState.failed, failure_kind=event.failure_kind, error_text=event.error_text))
            execution.active_units.discard(event.unit_key)
        elif isinstance(event, (ArtifactEvent, CheckpointEvent)):
            artifact = await asyncio.to_thread(self._normalize_worker_artifact, execution, event.artifact)
            self.store.record_run_artifact(execution.run_id, execution.attempt_id, event.unit_key, artifact)
        elif isinstance(event, ResourceEvent):
            self.store.record_run_progress_event(execution.attempt_id, kind="resource", payload={"worker_sequence": event.sequence, "monotonic_ns": event.monotonic_ns, "metrics": event.metrics})
        elif isinstance(event, ProgressEvent):
            execution.safe_to_stop = event.safe_to_stop is True
            self.store.record_run_progress_event(execution.attempt_id, kind="progress", payload={"worker_sequence": event.sequence, "monotonic_ns": event.monotonic_ns, "message": event.message, "completed": event.completed, "total": event.total, "safe_to_stop": event.safe_to_stop})
        elif isinstance(event, WaitingEvent):
            self.store.set_run_waiting(execution.attempt_id, event.reason)
            execution.safe_to_stop = True
        elif isinstance(event, ContinuedEvent):
            self.store.continue_run(execution.attempt_id)
            execution.safe_to_stop = False
        elif isinstance(event, (RunCompletedEvent, RunFailedEvent)):
            execution.terminal_event = event
        else:  # pragma: no cover - the discriminated contract makes this unreachable
            raise DurableRunProtocolError(f"unsupported durable-run event: {type(event).__name__}")
        if execution.stop_requested and (not execution.active_units or execution.safe_to_stop):
            await self._terminate(execution)

    async def _monitor(self, execution: _LocalExecution) -> dict[str, Any]:
        assert execution.process.stdout is not None and execution.process.stderr is not None
        stdout_task = asyncio.create_task(self._pump_stream(execution.process.stdout, execution.journal.write_stdout))
        stderr_task = asyncio.create_task(self._pump_stream(execution.process.stderr, execution.journal.write_stderr))
        protocol_failure: str | None = None
        try:
            await self._read_worker_events(execution)
        except BaseException as error:  # noqa: BLE001 - child-process trust boundary
            protocol_failure = f"{type(error).__name__}: {error}"
            await self._terminate(execution)
        returncode = await asyncio.to_thread(execution.process.wait)
        try:
            os.close(execution.control_write_fd)
        except OSError:
            pass
        stream_results = await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        stream_failures = [f"{type(value).__name__}: {value}" for value in stream_results if isinstance(value, BaseException)]
        if stream_failures and protocol_failure is None:
            protocol_failure = "; ".join(stream_failures)
        if execution.suspend_terminalization:
            await asyncio.to_thread(execution.journal.sync_streams)
            execution.journal.close()
            return self.store.get_run_attempt(execution.attempt_id)

        state: DurableRunAttemptState
        failure_kind: str | None = None
        error_text: str | None = None
        if protocol_failure is not None:
            state = DurableRunAttemptState.failed
            failure_kind = "worker_protocol"
            error_text = protocol_failure
        elif isinstance(execution.terminal_event, RunFailedEvent):
            state = DurableRunAttemptState.failed
            failure_kind = execution.terminal_event.failure_kind
            error_text = execution.terminal_event.error_text
        elif isinstance(execution.terminal_event, RunCompletedEvent) and returncode == 0:
            state = DurableRunAttemptState.completed
        elif execution.stop_requested or self.store.get_run(execution.run_id)["state"] == DurableRunState.stopping.value:
            state = DurableRunAttemptState.stopped
        elif returncode != 0:
            state = DurableRunAttemptState.failed
            failure_kind = "process_exit"
            error_text = f"worker exited with status {returncode} without a typed terminal event"
        else:
            state = DurableRunAttemptState.failed
            failure_kind = "missing_terminal_event"
            error_text = "worker exited successfully without declaring run_completed or run_failed"

        try:
            terminal = self.store.finish_run_attempt(execution.attempt_id, RunAttemptFinishV1(state=state, failure_kind=failure_kind, error_text=error_text))
        except LatticeConflict as error:
            state = DurableRunAttemptState.failed
            failure_kind = "worker_protocol"
            error_text = str(error)
            terminal = self.store.finish_run_attempt(execution.attempt_id, RunAttemptFinishV1(state=state, failure_kind=failure_kind, error_text=error_text))
        receipt = {
            "contract_version": "LocalRunTerminal/1.0",
            "run_id": execution.run_id,
            "attempt_id": execution.attempt_id,
            "state": terminal["state"],
            "returncode": returncode,
            "failure_kind": terminal["failure_kind"],
            "error_text": terminal["error_text"],
            "worker_event_count": execution.last_sequence,
            "stdout_bytes": execution.journal.stdout_bytes,
            "stderr_bytes": execution.journal.stderr_bytes,
            "completed_at": terminal["completed_at"],
        }
        execution.journal.finish(receipt)
        execution.journal.close()
        return terminal
