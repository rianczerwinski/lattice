"""Supervise semantic local durable runs beside provider invocation execution."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import subprocess
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .canonical import canonical_bytes, opaque_id
from .durability import sync_directory
from .contracts import (
    DurableRunAttemptState,
    DurableRunState,
    DurableRunUnitState,
    RunArtifactClass,
    RunArtifactCreateV1,
    RunAttemptFinishV1,
    RunAttemptStartV1,
    RunProcessIdentityV2,
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


# Keep the existing store chunk limit. The timer starts at the first observed
# byte, not the latest read, so continuous small writes cannot postpone admission.
STREAM_BATCH_BYTES = 64 * 1024
STREAM_BATCH_SECONDS = 0.05
# Once the protocol ends, project work must exit its foreground guardian.
# Exit grace bounds project cleanup; drain grace budgets post-exit pipe waiting,
# excluding Lattice's own admission work. Dispatched storage must settle.
WORKER_EXIT_SECONDS = 2.0
WORKER_DRAIN_SECONDS = 2.0


async def _read_pipe(descriptor: int, capacity: int) -> bytes:
    """Selector-driven reads: cancellation removes readiness ownership, not a thread.

    Nonblocking os.read plus loop.add_reader is the Unix asyncio pipe primitive.
    Regular-file fixtures complete on the first read without selector registration.
    """
    os.set_blocking(descriptor, False)
    loop = asyncio.get_running_loop()
    while True:
        try:
            return os.read(descriptor, capacity)
        except BlockingIOError:
            ready = loop.create_future()
            def wake() -> None:
                if not ready.done():
                    ready.set_result(None)
            loop.add_reader(descriptor, wake)
            try:
                await ready
            finally:
                loop.remove_reader(descriptor)


async def _complete_io(function: Any, *args: Any, **kwargs: Any) -> Any:
    """Settle dispatched I/O through cancellation before relinquishing ownership.

    A filesystem/SQLite call that never returns remains an explicit proof limit;
    no terminal boundary may race past a dispatched admission transaction.
    """
    job = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(job)
            break
        except asyncio.CancelledError:
            if job.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


@dataclass
class _StreamBuffer:
    pending: bytearray = field(default_factory=bytearray)
    oldest: float | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class _PipeDrain:
    phase: str = "not_started"
    wait_seconds: float = 0.0
    reads: int = 0
    byte_count: int = 0


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
    pending_evidence: dict[str, Any] | None = None
    terminal_event_ref: str | None = None
    stream_prefixes: dict[str, int] = field(default_factory=lambda: {"stdout": 0, "stderr": 0})
    stop_signal_sent: bool = False
    # A decoded worker event may be awaiting store admission when stop arrives.
    worker_event_in_flight: bool = False
    process_identity: RunProcessIdentityV2 | None = None
    evidence_failure: str | None = None
    evidence_failed: asyncio.Event = field(default_factory=asyncio.Event)
    stream_buffers: dict[str, _StreamBuffer] = field(default_factory=lambda: {"stdout": _StreamBuffer(), "stderr": _StreamBuffer()})
    termination_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stream_observed_bytes: dict[str, int] = field(default_factory=lambda: {"stdout": 0, "stderr": 0})
    terminal_admitted: asyncio.Event = field(default_factory=asyncio.Event)
    lifecycle_phase: str = "monitor"
    terminal_receipt: dict[str, Any] | None = None
    process_exited: asyncio.Event = field(default_factory=asyncio.Event)
    process_exited_at: float | None = None
    pipe_drains: dict[str, _PipeDrain] = field(default_factory=lambda: {channel: _PipeDrain() for channel in ("events", "stdout", "stderr")})


class DurableRunSupervisor:
    """Run local workers without sharing provider-supervisor state or ontology."""

    def __init__(self, store: LatticeStore, *, journal_root: Path | None = None) -> None:
        self.store = store
        self.boot_id = opaque_id("runboot")
        self.journal_root = (journal_root or store.config.data_root / "durable-runs").resolve()
        self._production_journals = self.journal_root == (store.config.data_root / DURABLE_RUNS_DIRECTORY).resolve()
        self._executions: dict[str, _LocalExecution] = {}
        self._failures: dict[str, dict[str, Any]] = {}
        self._failed_executions: dict[str, _LocalExecution] = {}
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
            return self.inspect_attempt(existing["attempt_id"])
        for previous in run["attempts"]:
            if previous["attempt_id"] in self._failures:
                await self.reconcile_attempt(previous["attempt_id"])
        run = self.store.get_run(run_id)
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
        for directory in (attempt_root.parent, self.journal_root, self.journal_root.parent):
            sync_directory(directory)
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
            controls = []
            for name in ("launch.json", "unit-plan.json", "target-environment.json", "process.json"):
                path = attempt_root / name
                digest, byte_count = self.store.blobs.put_file(path)
                controls.append({"name": name, "path": str(path), "content_ref": f"lattice:blob/{digest}", "byte_count": byte_count})
            self.store.record_run_process(attempt_id, spawned.process_identity, controls=controls)
            release_local_run(spawned)
        except BaseException as error:
            if coordination_fd is not None:
                os.close(coordination_fd)
            setup_prefixes = {"stdout": 0, "stderr": 0}
            failed_execution = None
            if spawned is not None:
                try:
                    os.close(spawned.gate_write_fd)
                except OSError:
                    pass
                if journal is not None:
                    failed_execution = _LocalExecution(run_id=run_id, attempt_id=attempt_id, bound=bound, process=spawned.process, event_read_fd=spawned.event_read_fd, control_write_fd=spawned.control_write_fd, journal=journal, process_identity=spawned.process_identity)
                    failed_execution.lifecycle_phase = "setup_cleanup"
                    try:
                        drain_errors = await self._cleanup_setup(failed_execution)
                    except BaseException as cleanup_error:
                        self._remember_failure(failed_execution, cleanup_error)
                        journal.close()
                        if isinstance(cleanup_error, asyncio.CancelledError):
                            raise
                        return self.inspect_attempt(attempt_id)
                    if drain_errors:
                        error = RuntimeError(f"{error}; setup drain failed: {'; '.join(drain_errors)}")
                    setup_prefixes = self.store.run_attempt_evidence_boundary(attempt_id)["stream_prefixes"]
            failure_kind = error.failure_kind if isinstance(error, LocalRunLaunchRefused) else "executor_setup"
            try:
                if failed_execution is not None:
                    failed_execution.lifecycle_phase = "terminal_store"
                terminal = self.store.finish_run_attempt(attempt_id, RunAttemptFinishV1(state=DurableRunAttemptState.failed, failure_kind=failure_kind, error_text=f"{type(error).__name__}: {error}"), evidence={**self.store.run_attempt_evidence_boundary(attempt_id), "returncode": spawned.process.returncode if spawned is not None else None})
                if journal is not None:
                    receipt = {"contract_version": "LocalRunTerminal/1.0", "run_id": run_id, "attempt_id": attempt_id, "state": "failed", "failure_kind": failure_kind, "error_text": terminal["error_text"], "completed_at": terminal["completed_at"], "stdout_bytes": setup_prefixes["stdout"], "stderr_bytes": setup_prefixes["stderr"]}
                    if failed_execution is not None:
                        failed_execution.lifecycle_phase = "projection"
                        failed_execution.terminal_receipt = receipt
                    journal.finish(receipt)
                return terminal
            except Exception as terminal_error:
                if failed_execution is None:
                    raise
                self._remember_failure(failed_execution, terminal_error)
                return self.inspect_attempt(attempt_id)
            finally:
                if journal is not None:
                    journal.close()
        execution = _LocalExecution(
            run_id=run_id,
            attempt_id=attempt_id,
            bound=bound,
            process=spawned.process,
            event_read_fd=spawned.event_read_fd,
            control_write_fd=spawned.control_write_fd,
            journal=journal,
            process_identity=spawned.process_identity,
        )
        execution.task = asyncio.create_task(self._monitor(execution), name=f"durable-run:{attempt_id}")
        self._executions[attempt_id] = execution
        execution.task.add_done_callback(lambda task: self._execution_done(execution, task))
        return self.store.get_run_attempt(attempt_id)

    async def _drain_setup_events(self, execution: _LocalExecution) -> None:
        # The gate never released: these are diagnostics, never worker semantics.
        try:
            with os.fdopen(execution.event_read_fd, "rb", buffering=0) as events:
                while chunk := await self._read_channel(execution, "events", events.fileno(), 64 * 1024):
                    await _complete_io(execution.journal.append_event, chunk, framed=True)
        finally:
            execution.event_read_fd = -1

    async def _cleanup_setup(self, execution: _LocalExecution) -> list[str]:
        children = []
        try:
            await self._terminate(execution)
            self._mark_process_exited(execution)
            for channel, stream, writer in (("stdout", execution.process.stdout, execution.journal.write_stdout), ("stderr", execution.process.stderr, execution.journal.write_stderr)):
                if stream is not None:
                    children.append(asyncio.create_task(self._pump_stream(execution, channel, stream, writer)))
            children.append(asyncio.create_task(self._drain_setup_events(execution)))
            results = await asyncio.gather(*children, return_exceptions=True)
            errors = [f"{type(result).__name__}: {result}" for result in results if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError)]
            if any(isinstance(result, DurableRunProtocolError) for result in results):
                errors.append("setup drain deadline exceeded; diagnostic tails incomplete")
            return errors
        finally:
            for task in children:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*children, return_exceptions=True)
            # No thread can still be reading these descriptors after joining.
            for descriptor in (execution.event_read_fd, execution.control_write_fd):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            execution.event_read_fd = execution.control_write_fd = -1
            for stream in (execution.process.stdout, execution.process.stderr):
                if stream is not None:
                    stream.close()

    def _execution_done(self, execution: _LocalExecution, task: asyncio.Task) -> None:
        try:
            task.result()
        except BaseException as error:
            self._remember_failure(execution, error)
        finally:
            self._executions.pop(execution.attempt_id, None)

    def _remember_failure(self, execution: _LocalExecution, error: BaseException) -> None:
        self._failures[execution.attempt_id] = {
            "phase": execution.lifecycle_phase,
            "error_text": f"{type(error).__name__}: {error}",
            "reconciliation": "pending",
            "process_exited": execution.process.poll() is not None,
            "projection_status": "failed" if execution.lifecycle_phase == "projection" else "unpublished",
            "lifecycle_diagnostics": self._lifecycle_diagnostics(execution),
        }
        self._failed_executions[execution.attempt_id] = execution
        logging.getLogger(__name__).error("durable-run supervision failed for %s: %s", execution.attempt_id, self._failures[execution.attempt_id])

    def inspect_attempt(self, attempt_id: str) -> dict[str, Any]:
        attempt = self.store.get_run_attempt(attempt_id)
        if attempt_id in self._failures:
            attempt["supervision_failure"] = dict(self._failures[attempt_id])
        return attempt

    def inspect_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        run["attempts"] = [self.inspect_attempt(attempt["attempt_id"]) for attempt in run["attempts"]]
        return run

    def _inspect_terminal_projection(self, attempt: dict[str, Any], *, repair: bool) -> dict[str, Any]:
        """Generation-local hook for historical attempts; never touches processes.

        A future restored-generation resolver must supply that generation's
        store and journal_root. Current paths alone are not restore receipts.
        """
        descriptor = None
        try:
            source = self.store.run_terminal_projection_source(attempt["attempt_id"])
            if source["status"] != "known":
                return source
            root = (self.journal_root / attempt["run_id"] / attempt["attempt_id"]).resolve()
            if root.parent.parent != self.journal_root:
                raise LatticeConflict("terminal projection escaped its configured journal root")
            details = {"source": source["source"], "stream_prefixes": source["stream_prefixes"],
                       "logs": "admitted-cas", "proof_limit": "stream extents are ledger claims; log downloads verify each required CAS chunk"}
            if repair:
                if self._production_journals:
                    descriptor = acquire_journal_writer(self.store.config.data_root)
                DurableRunJournal.recover_terminal_projection(root, source["receipt"], source["source"])
            return {**details, "status": DurableRunJournal.terminal_projection_status(root, source["receipt"])}
        except Exception as error:
            logging.getLogger(__name__).warning("terminal projection inspection failed for %s: %s", attempt["attempt_id"], error)
            return {"status": "failed", "error_text": f"{type(error).__name__}: {error}", "retryable": True}
        finally:
            if descriptor is not None:
                os.close(descriptor)

    async def inspect_public_attempt(self, run_id: str, attempt_id: str, *, repair: bool = False) -> dict[str, Any]:
        attempt = await _complete_io(self.inspect_attempt, attempt_id)
        if attempt["run_id"] != run_id:
            raise LatticeConflict("durable-run attempt belongs to another run")
        if attempt_id in self._executions:
            # Semantic commit precedes journal.finish; do not race its writer.
            attempt["terminal_projection"] = {"status": "pending", "reason": "current supervisor still owns publication"}
        else:
            failed = self._failed_executions.get(attempt_id)
            if repair and failed is not None and failed.terminal_receipt is not None:
                # This boot still knows the richer diagnostic receipt. Preserve
                # it, and resolve its overlay without signalling any process.
                try:
                    await _complete_io(failed.journal.reconcile_terminal, failed.terminal_receipt)
                except Exception as error:
                    logging.getLogger(__name__).warning("terminal projection retry failed for %s: %s", attempt_id, error)
                    attempt["terminal_projection"] = {"status": "failed", "error_text": f"{type(error).__name__}: {error}", "retryable": True}
                    return attempt
                self._failures[attempt_id].update(projection_status="repaired", reconciliation="resolved")
                self._failures[attempt_id].pop("reconciliation_error", None)
                self._failed_executions.pop(attempt_id, None)
                attempt["supervision_failure"] = dict(self._failures[attempt_id])
            attempt["terminal_projection"] = await _complete_io(self._inspect_terminal_projection, attempt, repair=repair)
        return attempt

    async def inspect_public_run(self, run_id: str) -> dict[str, Any]:
        """Inspect one terminal projection, with explicit lazy historical work.

        Startup never enumerates terminal attempts or their CAS. The existing
        run representation includes history; older projections have their own
        focused endpoint so per-request filesystem work does not grow with it.
        """
        run = await _complete_io(self.inspect_run, run_id)
        for attempt in run["attempts"]:
            attempt["terminal_projection"] = {"status": "pending", "reason": "inspect the focused attempt endpoint"}
        if run["attempts"]:
            latest = run["attempts"][-1]
            run["attempts"][-1] = await self.inspect_public_attempt(run_id, latest["attempt_id"])
        return run

    async def reconcile_attempt(self, attempt_id: str) -> dict[str, Any]:
        """Retry failed terminal storage after proven exit; never undo completion.

        Runtime errors survive task removal. Restart handles active processes
        through orphan reconciliation and terminal projections through retained
        admission evidence, without signalling historical process identities.
        """
        execution = self._failed_executions.get(attempt_id)
        if execution is None:
            attempt = self.store.get_run_attempt(attempt_id)
            return await self.inspect_public_attempt(attempt["run_id"], attempt_id, repair=True)
        try:
            await self._terminate(execution)
        except Exception as error:
            self._failures[attempt_id]["reconciliation_error"] = f"{type(error).__name__}: {error}"
            return self.inspect_attempt(attempt_id)
        attempt = self.store.get_run_attempt(attempt_id)
        if attempt["state"] in {"running", "waiting", "stopping"}:
            try:
                self.store.finish_run_attempt(attempt_id, RunAttemptFinishV1(state="failed", failure_kind="supervisor_lifecycle", error_text=self._failures[attempt_id]["error_text"]), evidence={**self.store.run_attempt_evidence_boundary(attempt_id), "returncode": execution.process.returncode})
            except Exception as error:
                self._failures[attempt_id]["reconciliation_error"] = f"{type(error).__name__}: {error}"
                return self.inspect_attempt(attempt_id)
        if execution.terminal_receipt is not None:
            try:
                await _complete_io(execution.journal.reconcile_terminal, execution.terminal_receipt)
            except Exception as error:
                self._failures[attempt_id]["reconciliation_error"] = f"{type(error).__name__}: {error}"
                return self.inspect_attempt(attempt_id)
            self._failures[attempt_id]["projection_status"] = "repaired"
        # A projection error does not erase the database terminal or units. Its
        # cause remains visible even after ledger reconciliation is resolved.
        self._failures[attempt_id]["reconciliation"] = "resolved"
        self._failures[attempt_id].pop("reconciliation_error", None)
        self._failed_executions.pop(attempt_id, None)
        return self.inspect_attempt(attempt_id)

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
        if execution is not None and execution.task is not None:
            await asyncio.gather(asyncio.shield(execution.task), return_exceptions=True)
            # Also publish before returning if the done callback is still queued.
            if execution.task.done() and attempt_id in self._executions:
                self._execution_done(execution, execution.task)
        return self.inspect_attempt(attempt_id)

    async def request_stop(self, run_id: str) -> dict[str, Any]:
        run = self.inspect_run(run_id)
        failures = [attempt for attempt in run["attempts"] if attempt.get("supervision_failure")]
        if failures and not any(execution.run_id == run_id for execution in self._executions.values()):
            for attempt in failures:
                await self.reconcile_attempt(attempt["attempt_id"])
            return self.inspect_run(run_id)
        run = self.store.request_run_stop(run_id)
        active = next((execution for execution in self._executions.values() if execution.run_id == run_id and execution.process.poll() is None), None)
        if active is None:
            return self.inspect_run(run_id)
        active.stop_requested = True
        await self._flush_streams(active)
        self._signal_stop(active)
        if not active.worker_event_in_flight and (not active.active_units or active.safe_to_stop or run["state"] == DurableRunState.waiting.value):
            await self._terminate(active)
        return self.inspect_run(run_id)

    def open_attempt_log(self, run_id: str, attempt_id: str, stream: str, *, diagnostic: bool = False) -> tuple[int, int]:
        """Materializing compatibility helper, not the production HTTP read path.

        Legacy attempts expose their historical raw projection with the proof
        limit returned by ``attempt_log_status``. New attempts never promote
        unaccepted raw tails, even when the raw file is available.
        """

        if stream not in {"stdout", "stderr"}:
            raise LatticeConflict("durable-run log stream must be stdout or stderr")
        attempt = self.store.get_run_attempt(attempt_id)
        if attempt["run_id"] != run_id:
            raise LatticeConflict("durable-run attempt belongs to another run")
        evidence = self.store.run_log_evidence(attempt_id, stream) if not diagnostic else None
        if evidence is not None and evidence["admission_known"]:
            with tempfile.TemporaryFile(mode="w+b") as handle:
                for chunk in evidence["chunks"]:
                    handle.write(self.store._evidence_bytes(chunk))
                handle.flush()
                handle.seek(0)
                return os.dup(handle.fileno()), evidence["admitted_bytes"]
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

    def _legacy_log_extent(self, run_id: str, attempt_id: str, stream: str) -> tuple[int, int, int]:
        descriptor, size = self.open_attempt_log(run_id, attempt_id, stream, diagnostic=True)
        try:
            details = os.fstat(descriptor)
            return size, details.st_dev, details.st_ino
        finally:
            os.close(descriptor)

    def _legacy_log_chunk(self, run_id: str, attempt_id: str, stream: str, extent: tuple[int, int, int], offset: int) -> bytes:
        descriptor, size = self.open_attempt_log(run_id, attempt_id, stream, diagnostic=True)
        try:
            details = os.fstat(descriptor)
            if (details.st_dev, details.st_ino) != extent[1:] or size < extent[0]:
                raise LatticeConflict("legacy diagnostic run log was replaced or truncated")
            count = min(STREAM_BATCH_BYTES, extent[0] - offset)
            raw = os.pread(descriptor, count, offset)
            if len(raw) != count:
                raise LatticeConflict("legacy diagnostic run log ended before its pinned extent")
            return raw
        finally:
            os.close(descriptor)

    async def stream_attempt_log(self, run_id: str, attempt_id: str, stream: str) -> tuple[int, AsyncIterator[bytes]]:
        """Finite HTTP log stream: one receipt page and one verified chunk at a time.

        Each off-loop operation closes its SQLite/file handles before yielding.
        A disconnect settles the current bounded I/O operation; no read task or
        descriptor is detached. Appends cannot move the committed sequence/byte
        ceiling. Legacy raw logs retain their diagnostic-only compatibility.
        """
        boundary = await _complete_io(self.store.run_log_boundary, attempt_id, stream, run_id=run_id)
        if not boundary["admission_known"]:
            extent = await _complete_io(self._legacy_log_extent, run_id, attempt_id, stream)
            async def legacy() -> AsyncIterator[bytes]:
                offset = 0
                while offset < extent[0]:
                    raw = await _complete_io(self._legacy_log_chunk, run_id, attempt_id, stream, extent, offset)
                    offset += len(raw)
                    yield raw
            return extent[0], legacy()

        async def admitted() -> AsyncIterator[bytes]:
            after, offset = 0, 0
            while True:
                page = await _complete_io(self.store.run_log_receipt_page, boundary, after)
                if not page:
                    if offset != boundary["admitted_bytes"]:
                        raise LatticeConflict("admitted run log ended before its pinned extent")
                    return
                for sequence, receipt in page:
                    end = self.store._validate_log_receipt(receipt, attempt_id, stream, offset)
                    if end > boundary["admitted_bytes"]:
                        raise LatticeConflict("admitted run log exceeds its pinned extent")
                    raw = await _complete_io(self.store.read_run_log_chunk, receipt)
                    after, offset = sequence, end
                    yield raw
        return boundary["admitted_bytes"], admitted()

    def attempt_log_status(self, run_id: str, attempt_id: str, stream: str) -> dict[str, Any]:
        """Materializing compatibility inspection; not used by HTTP log serving."""
        attempt = self.store.get_run_attempt(attempt_id)
        if attempt["run_id"] != run_id:
            raise LatticeConflict("durable-run attempt belongs to another run")
        evidence = self.store.run_log_evidence(attempt_id, stream)
        try:
            descriptor, size = self.open_attempt_log(run_id, attempt_id, stream, diagnostic=True)
        except LatticeNotFound:
            size = None
        else:
            os.close(descriptor)
        active = self._executions.get(attempt_id)
        buffered = len(active.stream_buffers[stream].pending) if active else 0
        observed = active.stream_observed_bytes[stream] if active else None
        return {key: value for key, value in evidence.items() if key != "chunks"} | {
            "raw_bytes": size,
            "buffered_bytes": buffered,
            "observed_bytes": observed,
            "unretained_observed_bytes": max(0, observed - size) if observed is not None and size is not None else None,
            "unadmitted_raw_bytes": max(0, size - evidence["admitted_bytes"]) if size is not None and evidence["admission_known"] else None,
            "admission_open": active is not None and active.evidence_failure is None,
            "diagnostic_offset": evidence["admitted_bytes"] if evidence["admission_known"] else 0,
            "evidence_failure": active.evidence_failure if active else attempt.get("error_text"),
            "batch_max_bytes": STREAM_BATCH_BYTES,
            "batch_target_seconds": STREAM_BATCH_SECONDS,
            "ordering_limit": "prefixes and boundary flushes cover bytes observed per descriptor; cross-descriptor worker-write ordering is unproven",
        }

    async def shutdown_for_restart(self) -> None:
        """Simulate service/process loss without manufacturing terminal receipts."""

        executions = list(self._executions.values())
        for execution in executions:
            execution.suspend_terminalization = True
        await asyncio.gather(*(self._terminate(execution) for execution in executions), return_exceptions=False)
        await asyncio.gather(*(execution.task for execution in executions if execution.task is not None), return_exceptions=False)

    async def _terminate(self, execution: _LocalExecution) -> None:
        async with execution.termination_lock:
            if execution.process_identity is not None:
                await asyncio.to_thread(terminate_and_prove_process_dead, execution.process_identity)
                # Guardian lock release can precede Popen's observable exit.
                # Waiting on our own child never grants signal authority.
                await asyncio.to_thread(execution.process.wait, timeout=2)
            elif execution.process.poll() is None:
                # Direct trusted callers/tests have only their still-live Popen.
                # Never reuse a reaped process's numeric group as kill authority.
                await asyncio.to_thread(terminate_process_group, execution.process)

    @staticmethod
    def _evidence_failed(execution: _LocalExecution, error: BaseException) -> None:
        if execution.evidence_failure is None:
            execution.evidence_failure = f"{type(error).__name__}: {error}"
            logging.getLogger(__name__).error("durable-run evidence failed for %s: %s", execution.attempt_id, execution.evidence_failure)
            execution.evidence_failed.set()

    async def _stop_after_evidence_failure(self, execution: _LocalExecution) -> None:
        await execution.evidence_failed.wait()
        await self._terminate(execution)

    @staticmethod
    def _mark_process_exited(execution: _LocalExecution) -> None:
        if execution.process_exited_at is None:
            execution.process_exited_at = asyncio.get_running_loop().time()
            execution.process_exited.set()

    @staticmethod
    def _lifecycle_diagnostics(execution: _LocalExecution) -> dict[str, Any]:
        return {
            "returncode": execution.process.returncode,
            "last_worker_sequence": execution.last_sequence,
            "worker_event_in_flight": execution.worker_event_in_flight,
            "acknowledged_stream_prefixes": dict(execution.stream_prefixes),
            "buffered_stream_bytes": {channel: len(buffer.pending) for channel, buffer in execution.stream_buffers.items()},
            "observed_stream_bytes": dict(execution.stream_observed_bytes),
            "pipes": {channel: {"phase": state.phase, "post_exit_wait_seconds": state.wait_seconds, "reads": state.reads, "bytes": state.byte_count} for channel, state in execution.pipe_drains.items()},
        }

    async def _read_channel(self, execution: _LocalExecution, channel: str, descriptor: int, capacity: int) -> bytes:
        """Budget peer waiting, never time spent publishing already-read bytes.

        Each channel has a cumulative post-exit read-wait budget. It is not
        reset by tiny reads. Storage work runs outside this interval, and a
        timed-out readiness read is cancellable without detaching any I/O.
        """
        state = execution.pipe_drains[channel]
        loop = asyncio.get_running_loop()
        started = loop.time()
        state.phase = "reading"

        async def read() -> bytes:
            try:
                chunk = await _read_pipe(descriptor, capacity)
                state.reads += 1
                state.byte_count += len(chunk)
                state.phase = "processing" if chunk else "eof"
                return chunk
            finally:
                if execution.process_exited_at is not None:
                    state.wait_seconds += max(0, loop.time() - max(started, execution.process_exited_at))

        reader = asyncio.create_task(read())
        exited = asyncio.create_task(execution.process_exited.wait())
        try:
            if not execution.process_exited.is_set():
                await asyncio.wait((reader, exited), return_when=asyncio.FIRST_COMPLETED)
            if not reader.done():
                elapsed = max(0, loop.time() - max(started, execution.process_exited_at))
                remaining = max(0, WORKER_DRAIN_SECONDS - state.wait_seconds - elapsed)
                done, _pending = await asyncio.wait((reader,), timeout=remaining)
                if not done:
                    state.phase = "timed_out"
                    logging.getLogger(__name__).error("durable-run drain expired for %s channel %s: %s", execution.attempt_id, channel, self._lifecycle_diagnostics(execution))
                    raise DurableRunProtocolError(f"worker drain deadline exceeded waiting for {channel}; diagnostic tails incomplete")
            return reader.result()
        finally:
            for task in (reader, exited):
                if not task.done():
                    task.cancel()
            await asyncio.gather(reader, exited, return_exceptions=True)

    @staticmethod
    def _signal_stop(execution: _LocalExecution) -> None:
        if execution.stop_signal_sent:
            return
        try:
            os.write(execution.control_write_fd, b"S")
        except (BrokenPipeError, OSError):
            pass
        execution.stop_signal_sent = True

    async def _pump_stream(self, execution: _LocalExecution, channel: str, stream: Any, writer: Any) -> None:
        buffer = execution.stream_buffers[channel]
        raw_available = True
        read_task = None
        try:
            while True:
                if read_task is None:
                    capacity = STREAM_BATCH_BYTES - len(buffer.pending)
                    read_task = asyncio.create_task(self._read_channel(execution, channel, stream.fileno(), capacity))
                timeout = None if buffer.oldest is None else max(0, buffer.oldest + STREAM_BATCH_SECONDS - asyncio.get_running_loop().time())
                done, _pending = await asyncio.wait((read_task,), timeout=timeout)
                if not done:
                    await self._flush_stream(execution, channel)
                    continue
                chunk = read_task.result()
                read_task = None
                if not chunk:
                    await self._flush_stream(execution, channel)
                    return
                execution.stream_observed_bytes[channel] += len(chunk)
                observed = asyncio.get_running_loop().time()
                async with buffer.lock:
                    if raw_available:
                        try:
                            await _complete_io(writer, chunk)
                        except Exception as error:
                            raw_available = False
                            self._evidence_failed(execution, error)
                    if execution.evidence_failure is None:
                        if not buffer.pending:
                            buffer.oldest = observed
                        buffer.pending.extend(chunk)
                if len(buffer.pending) == STREAM_BATCH_BYTES:
                    await self._flush_stream(execution, channel)
        except Exception as error:
            if not isinstance(error, DurableRunProtocolError):
                self._evidence_failed(execution, error)
            raise
        finally:
            if read_task is not None:
                read_task.cancel()
                await asyncio.gather(read_task, return_exceptions=True)
            stream.close()

    async def _flush_stream(self, execution: _LocalExecution, channel: str) -> None:
        buffer = execution.stream_buffers[channel]
        async with buffer.lock:
            if not buffer.pending:
                return
            if execution.evidence_failure is not None:
                buffer.pending.clear()
                buffer.oldest = None
                return
            chunk = bytes(buffer.pending)
            try:
                digest = await _complete_io(self.store.blobs.put, chunk)
                if execution.evidence_failure is None:
                    evidence = {"content_ref": f"lattice:blob/{digest}", "byte_count": len(chunk)}
                    await _complete_io(self.store.record_run_stream_chunk, execution.attempt_id, channel, execution.stream_prefixes[channel], evidence)
                    execution.stream_prefixes[channel] += len(chunk)
            except Exception as error:
                # A failed acknowledgement may follow a committed transaction.
                # Terminal membership comes from SQLite, never this counter.
                self._evidence_failed(execution, error)
            finally:
                buffer.pending.clear()
                buffer.oldest = None

    async def _flush_streams(self, execution: _LocalExecution) -> None:
        for channel in ("stdout", "stderr"):
            await self._flush_stream(execution, channel)

    async def _read_worker_events(self, execution: _LocalExecution) -> None:
        stream = os.fdopen(execution.event_read_fd, "rb", buffering=0)
        buffered = bytearray()
        try:
            while True:
                while b"\n" not in buffered and len(buffered) < MAX_WORKER_EVENT_BYTES + 2:
                    chunk = await self._read_channel(execution, "events", stream.fileno(), MAX_WORKER_EVENT_BYTES + 2 - len(buffered))
                    if not chunk:
                        break
                    buffered.extend(chunk)
                end = buffered.find(b"\n")
                size = end + 1 if end >= 0 else len(buffered)
                line = bytes(buffered[:size])
                del buffered[:size]
                if not line:
                    return
                execution.worker_event_in_flight = True
                try:
                    # Mark the frame in flight before asynchronous journaling,
                    # so a previous safe boundary cannot overtake its admission.
                    # After admission closes, retain diagnostics without a
                    # barrier per rejected frame; finish() syncs the journal.
                    offset = await _complete_io(execution.journal.append_event, line, framed=True, sync=execution.evidence_failure is None)
                    if execution.evidence_failure is not None:
                        continue
                    await self._flush_streams(execution)
                    if execution.evidence_failure is not None:
                        continue
                    if not line.endswith(b"\n") or len(line) - 1 > MAX_WORKER_EVENT_BYTES:
                        raise DurableRunProtocolError("worker emitted an unterminated or oversized structured event")
                    try:
                        event = decode_worker_event(line[:-1])
                    except (TypeError, ValueError) as error:
                        raise DurableRunProtocolError(f"invalid structured worker event: {error}") from error
                    digest = await _complete_io(self.store.blobs.put, line)
                    if execution.evidence_failure is not None:
                        continue
                    execution.pending_evidence = {"attempt_id": execution.attempt_id, "worker_sequence": event.sequence, "raw_offset": offset, "byte_count": len(line), "content_ref": f"lattice:blob/{digest}"}
                    await self._apply_event(execution, event)
                    execution.last_sequence = max(execution.last_sequence, event.sequence)
                finally:
                    execution.pending_evidence = None
                    execution.worker_event_in_flight = False
        finally:
            stream.close()
            execution.event_read_fd = -1

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
        if execution.pending_evidence is not None and event.sequence <= execution.last_sequence:
            self.store.replay_run_worker_event(execution.attempt_id, execution.pending_evidence)
            return
        if execution.terminal_event is not None:
            raise DurableRunProtocolError("worker emitted an event after its terminal disposition")
        if isinstance(event, UnitStartedEvent):
            self.store.start_run_unit(
                execution.run_id,
                execution.attempt_id,
                event.unit_key,
                allow_stopping=execution.stop_requested and execution.worker_event_in_flight,
                evidence=execution.pending_evidence,
            )
            execution.active_units.add(event.unit_key)
            execution.safe_to_stop = False
        elif isinstance(event, UnitCompletedEvent):
            self.store.finish_run_unit(execution.run_id, execution.attempt_id, event.unit_key, RunUnitResultV1(state=DurableRunUnitState.completed, output_sha256=event.output_sha256), evidence=execution.pending_evidence)
            execution.active_units.discard(event.unit_key)
        elif isinstance(event, UnitRefusedEvent):
            self.store.finish_run_unit(execution.run_id, execution.attempt_id, event.unit_key, RunUnitResultV1(state=DurableRunUnitState.refused, failure_kind=event.failure_kind, error_text=event.error_text), evidence=execution.pending_evidence)
            execution.active_units.discard(event.unit_key)
        elif isinstance(event, UnitFailedEvent):
            self.store.finish_run_unit(execution.run_id, execution.attempt_id, event.unit_key, RunUnitResultV1(state=DurableRunUnitState.failed, failure_kind=event.failure_kind, error_text=event.error_text), evidence=execution.pending_evidence)
            execution.active_units.discard(event.unit_key)
        elif isinstance(event, (ArtifactEvent, CheckpointEvent)):
            artifact = await asyncio.to_thread(self._normalize_worker_artifact, execution, event.artifact)
            if execution.evidence_failure is not None:
                return
            self.store.record_run_artifact(execution.run_id, execution.attempt_id, event.unit_key, artifact, evidence=execution.pending_evidence)
        elif isinstance(event, ResourceEvent):
            self.store.record_run_progress_event(execution.attempt_id, kind="resource", payload={"worker_sequence": event.sequence, "monotonic_ns": event.monotonic_ns, "metrics": event.metrics}, evidence=execution.pending_evidence)
        elif isinstance(event, ProgressEvent):
            self.store.record_run_progress_event(execution.attempt_id, kind="progress", payload={"worker_sequence": event.sequence, "monotonic_ns": event.monotonic_ns, "message": event.message, "completed": event.completed, "total": event.total, "safe_to_stop": event.safe_to_stop}, evidence=execution.pending_evidence)
            execution.safe_to_stop = event.safe_to_stop is True
        elif isinstance(event, WaitingEvent):
            self.store.set_run_waiting(execution.attempt_id, event.reason, evidence=execution.pending_evidence)
            execution.safe_to_stop = True
        elif isinstance(event, ContinuedEvent):
            self.store.continue_run(execution.attempt_id, evidence=execution.pending_evidence)
            execution.safe_to_stop = False
        elif isinstance(event, (RunCompletedEvent, RunFailedEvent)):
            if execution.pending_evidence is not None:
                self.store.record_run_terminal_event(execution.attempt_id, execution.pending_evidence)
                execution.terminal_event_ref = execution.pending_evidence["content_ref"]
            execution.terminal_event = event
            execution.terminal_admitted.set()
        else:  # pragma: no cover - the discriminated contract makes this unreachable
            raise DurableRunProtocolError(f"unsupported durable-run event: {type(event).__name__}")
        if execution.stop_requested and (not execution.active_units or execution.safe_to_stop):
            await self._terminate(execution)

    async def _monitor(self, execution: _LocalExecution) -> dict[str, Any]:
        assert execution.process.stdout is not None and execution.process.stderr is not None
        async def exited() -> int:
            # poll() reaps our own child without leaving a blocking wait thread.
            while execution.process.poll() is None:
                await asyncio.sleep(.02)
            self._mark_process_exited(execution)
            return execution.process.returncode

        jobs = [self._pump_stream(execution, "stdout", execution.process.stdout, execution.journal.write_stdout), self._pump_stream(execution, "stderr", execution.process.stderr, execution.journal.write_stderr), self._stop_after_evidence_failure(execution), self._read_worker_events(execution), exited(), execution.terminal_admitted.wait()]
        children = [asyncio.create_task(job, name=f"run-child:{execution.attempt_id}:{index}") for index, job in enumerate(jobs)]
        try:
            return await self._monitor_children(execution, *children)
        finally:
            # Exceptional monitor paths retain no unobserved child errors or
            # blocked reader threads. Pipes have cancellable readiness waits.
            try:
                if execution.process.poll() is None:
                    await self._terminate(execution)
            finally:
                for child in children:
                    if not child.done():
                        child.cancel()
                await asyncio.gather(*children, return_exceptions=True)
                for descriptor in (execution.event_read_fd, execution.control_write_fd):
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                execution.event_read_fd = execution.control_write_fd = -1
                for stream in (execution.process.stdout, execution.process.stderr):
                    if stream is not None:
                        stream.close()
                execution.journal.close()

    async def _monitor_children(self, execution, stdout_task, stderr_task, failure_task, reader_task, exit_task, terminal_task) -> dict[str, Any]:
        protocol_failure: str | None = None
        try:
            done, _pending = await asyncio.wait((reader_task, failure_task, exit_task, terminal_task), return_when=asyncio.FIRST_COMPLETED)
            if failure_task in done:
                failure_task.result()
            if reader_task in done:
                reader_task.result()
            if exit_task not in done:
                # Both an admitted terminal and unexpected event EOF end the
                # worker protocol; neither permits indefinite project cleanup.
                exited, _ = await asyncio.wait((exit_task,), timeout=WORKER_EXIT_SECONDS)
                if not exited:
                    protocol_failure = "worker exit deadline exceeded after terminal admission or event EOF"
                    await self._terminate(execution)
            returncode = await exit_task
            drains = (reader_task, stdout_task, stderr_task)
            results = await asyncio.gather(*drains, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    raise result
        except BaseException as error:  # noqa: BLE001 - child-process trust boundary
            if isinstance(error, asyncio.CancelledError):
                raise
            protocol_failure = f"{type(error).__name__}: {error}"
            if not isinstance(error, DurableRunProtocolError):
                self._evidence_failed(execution, error)
            await self._terminate(execution)
            returncode = execution.process.returncode
            self._mark_process_exited(execution)
            await asyncio.gather(reader_task, stdout_task, stderr_task, return_exceptions=True)
        if execution.evidence_failure is not None:
            await failure_task
            protocol_failure = execution.evidence_failure
        else:
            failure_task.cancel()
            await asyncio.gather(failure_task, return_exceptions=True)
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
            error_text = f"worker exited with status {returncode}" + (" after run_completed" if isinstance(execution.terminal_event, RunCompletedEvent) else " without a typed terminal event")
        else:
            state = DurableRunAttemptState.failed
            failure_kind = "missing_terminal_event"
            error_text = "worker exited successfully without declaring run_completed or run_failed"

        try:
            execution.lifecycle_phase = "terminal_store"
            evidence = {**self.store.run_attempt_evidence_boundary(execution.attempt_id), "returncode": returncode}
            try:
                terminal = self.store.finish_run_attempt(execution.attempt_id, RunAttemptFinishV1(state=state, failure_kind=failure_kind, error_text=error_text), evidence=evidence)
            except LatticeConflict as error:
                state = DurableRunAttemptState.failed
                failure_kind = "worker_protocol"
                error_text = str(error)
                terminal = self.store.finish_run_attempt(execution.attempt_id, RunAttemptFinishV1(state=state, failure_kind=failure_kind, error_text=error_text), evidence=evidence)
            receipt = {
                "contract_version": "LocalRunTerminal/1.0",
                "run_id": execution.run_id,
                "attempt_id": execution.attempt_id,
                "state": terminal["state"],
                "returncode": returncode,
                "failure_kind": terminal["failure_kind"],
                "error_text": terminal["error_text"],
                "worker_event_count": evidence["worker_event_count"],
                "stdout_bytes": evidence["stream_prefixes"]["stdout"],
                "stderr_bytes": evidence["stream_prefixes"]["stderr"],
                "raw_stdout_bytes": execution.journal.stdout_bytes,
                "raw_stderr_bytes": execution.journal.stderr_bytes,
                "observed_stdout_bytes": execution.stream_observed_bytes["stdout"],
                "observed_stderr_bytes": execution.stream_observed_bytes["stderr"],
                "unretained_stdout_bytes": max(0, execution.stream_observed_bytes["stdout"] - execution.journal.stdout_bytes),
                "unretained_stderr_bytes": max(0, execution.stream_observed_bytes["stderr"] - execution.journal.stderr_bytes),
                "terminal_event_ref": evidence["terminal_event_ref"],
                "completed_at": terminal["completed_at"],
                "lifecycle_diagnostics": self._lifecycle_diagnostics(execution),
            }
            if terminal["state"] == DurableRunAttemptState.failed.value:
                logging.getLogger(__name__).error("durable-run attempt failed for %s: %s; lifecycle: %s", execution.attempt_id, terminal["error_text"], receipt["lifecycle_diagnostics"])
            execution.terminal_receipt = receipt
            execution.lifecycle_phase = "projection"
            execution.journal.finish(receipt)
            return terminal
        except Exception:
            logging.getLogger(__name__).exception("durable-run terminal persistence failed for %s after worker exit %s; evidence failure: %s", execution.attempt_id, returncode, execution.evidence_failure)
            raise
        finally:
            execution.journal.close()
