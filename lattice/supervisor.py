"""Durable leased execution of sealed Lattice invocations."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .execution import (
    DEFAULT_ADAPTERS,
    SubscriptionInvocationFailed,
    SubscriptionRouteUnavailable,
    invoke_subscription,
    require_current_adapter_qualification,
)

from .canonical import canonical_bytes
from .contracts import AttemptState, ExecuteRequest, OutputContractV2, execution_policy
from .manifest_reader import verify_and_reconstruct
from .store import LatticeConflict, LatticeStore
from .observations import ObservationStore
from . import native_approvals
from .execution.native_approval import NativeApprovalCallback


MAX_RUNTIME_CONTROL_BYTES = 1024 * 1024


def _read_runtime_control(path: Any) -> bytes:
    size = path.stat().st_size
    if size > MAX_RUNTIME_CONTROL_BYTES:
        raise OSError(f"model journal control file exceeds {MAX_RUNTIME_CONTROL_BYTES} bytes: {path.name}")
    content = path.read_bytes()
    if len(content) != size:
        raise OSError(f"model journal control file changed while being read: {path.name}")
    return content


def _runtime_receipt(journal: Any, blob_store) -> dict[str, Any]:
    root = __import__("pathlib").Path(journal)
    files: dict[str, dict[str, Any]] = {}
    raw_files: dict[str, bytes] = {}
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if not path.is_file() or path.is_symlink():
            continue
        digest, byte_count = blob_store.put_file(path)
        files[path.name] = {"ref": f"lattice:blob/{digest}", "sha256": digest, "byte_count": byte_count}
        if path.name in {"request.json", "checkpoint.json", "terminal.json"}:
            raw_files[path.name] = _read_runtime_control(path)
    if "request.json" not in raw_files or "checkpoint.json" not in raw_files:
        raise OSError("model journal is missing its bound request or checkpoint")
    request = json.loads(raw_files["request.json"].decode("utf-8", errors="strict"))
    checkpoint = json.loads(raw_files["checkpoint.json"].decode("utf-8", errors="strict"))
    terminal = json.loads(raw_files["terminal.json"].decode("utf-8", errors="strict")) if "terminal.json" in raw_files else None
    return {
        "kind": "lattice-model-runtime-receipt",
        "journal_path": str(root),
        "request_sha256": files["request.json"]["sha256"],
        "checkpoint_sha256": files["checkpoint.json"]["sha256"],
        "terminal_sha256": files.get("terminal.json", {}).get("sha256"),
        "driver": request["driver"],
        "model": request["model"],
        "cli_version": request["metadata"].get("cli_version"),
        "binary": request["metadata"].get("binary"),
        "isolation": request["metadata"].get("isolation"),
        "native_session": checkpoint.get("native_session"),
        "observed": checkpoint.get("observed"),
        "terminal": terminal,
        "files": files,
    }


class ExecutionSupervisor:
    def __init__(self, store: LatticeStore) -> None:
        self.store = store
        self.boot_id = f"boot_{uuid.uuid4().hex}"
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancellations: dict[str, threading.Event] = {}
        self._conversation_dispatch_tasks: set[asyncio.Task] = set()
        self._conversation_dispatch_lock = asyncio.Lock()
        self._stopping = False
        self.startup_recovery_failures: list[dict[str, str]] = []

    async def start(self) -> int:
        await asyncio.to_thread(self._reconcile_observations)
        attempts = await asyncio.to_thread(self.store.reconcile_orphans, self.boot_id)
        commands = await asyncio.to_thread(self.store.reconcile_workflow_commands, self.boot_id)
        undispatched = await asyncio.to_thread(self.store.list_undispatched_batch_members)
        dispatched = 0
        for member in undispatched:
            try:
                await self.execute(
                    member["invocation_id"],
                    ExecuteRequest(idempotency_key=f"batch:{member['batch_id']}:{member['spec_id']}"),
                )
                dispatched += 1
            except Exception as error:
                failure = {"batch_id": member["batch_id"], "spec_id": member["spec_id"], "invocation_id": member["invocation_id"], "error": f"{type(error).__name__}: {error}"}
                self.startup_recovery_failures.append(failure)
                from .conversation_service import ConversationService
                ConversationService(self.store).dispatch_failed(member['invocation_id'],error)
                try:
                    await asyncio.to_thread(self.store.record_event, project_id=member["project_id"], event_type="invocation-batch.recovery-failed", subject_type="invocation", subject_id=member["invocation_id"], payload=failure)
                except Exception as receipt_error:
                    failure["receipt_error"] = f"{type(receipt_error).__name__}: {receipt_error}"
        await self._dispatch_queued_conversations()
        return attempts + commands + dispatched

    def kick_conversations(self) -> None:
        if self._stopping:
            return
        task = asyncio.create_task(self._dispatch_queued_conversations(), name='lattice-conversation-admission')
        self._conversation_dispatch_tasks.add(task)
        task.add_done_callback(self._conversation_dispatch_tasks.discard)

    async def _dispatch_queued_conversations(self) -> None:
        from .conversation_service import ConversationService
        from .conversations import current, bump
        async with self._conversation_dispatch_lock:
            with self.store._connect() as conn:
                queued = [dict(row) for row in conn.execute("SELECT conversation_id,round_id FROM conversation_rounds WHERE state='queued' ORDER BY created_at,ordinal")]
            for row in queued:
                if self._stopping:
                    return
                try:
                    await ConversationService(self.store,self).dispatch(row['conversation_id'])
                except Exception as error:
                    with self.store.transaction() as conn:
                        changed = conn.execute("UPDATE conversation_rounds SET state='needs_attention' WHERE round_id=? AND state='queued'",(row['round_id'],)).rowcount
                        if changed:
                            conversation=current(self.store,conn,row['conversation_id'])
                            bump(self.store,conn,conversation,'dispatch-blocked',{'round_id':row['round_id'],'detail':f'{type(error).__name__}: {error}'})
                        else:
                            conversation=current(self.store,conn,row['conversation_id'])
                            self.store._event(conn,project_id=conversation['project_id'],event_type='conversation.dispatch-failed',subject_type='conversation',subject_id=row['conversation_id'],payload={'round_id':row['round_id'],'detail':f'{type(error).__name__}: {error}'})

    def _reconcile_observations(self) -> None:
        with self.store._connect() as conn:
            rows = [dict(row) for row in conn.execute("SELECT e.subject_id,e.payload_json FROM events e JOIN attempts a ON a.attempt_id=e.subject_id WHERE e.event_type='attempt.journal' AND a.state IN ('leased','running')")]
        for row in rows:
            payload = json.loads(row["payload_json"])
            root = self.store.config.runs / Path(payload['path']).name
            # Preserve the historical absolute reference; snapshots retain the
            # same journal identity under the restored runtime root.
            request = self.store.blobs.get(payload["request_ref"].removeprefix("lattice:blob/"))
            if (root / "request.json").read_bytes() != request:
                raise LatticeConflict("attempt journal request binding changed")
            ObservationStore(self.store).reconcile_journal(row["subject_id"], root)

    async def stop(self) -> None:
        self._stopping = True
        if self._conversation_dispatch_tasks:
            await asyncio.gather(*list(self._conversation_dispatch_tasks),return_exceptions=True)
        owned = list(self._cancellations.items())
        errors = []
        try:
            for attempt_id, cancel in owned:
                try:
                    await self._close_approvals(attempt_id, "supervisor stopping")
                except Exception as error:
                    errors.append(error)
                finally:
                    cancel.set()
        finally:
            # Even interruption of shutdown itself must signal every owned job.
            for _attempt_id, cancel in owned:
                cancel.set()
            if self._tasks:
                await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        if errors:
            raise ExceptionGroup("Native approval closure failed during shutdown", errors)

    def _adapter(self, route: dict[str, Any]):
        if route.get("route_kind", "execution") != "execution":
            raise SubscriptionRouteUnavailable("historical imports are evidence records and cannot execute")
        base = next((item for item in DEFAULT_ADAPTERS if item.provider_code == route["provider"]), None)
        if base is None or base.cli != route["harness"]:
            raise SubscriptionRouteUnavailable("provider and official CLI route are not qualified together")
        selected = base.select_model(route["model"])
        if route.get("provider_profile_id") not in {None, selected.profile_id}:
            raise SubscriptionRouteUnavailable("sealed provider profile identity disagrees with the installed execution profile")
        require_current_adapter_qualification(selected)
        return selected

    async def execute(self, invocation_id: str, request: ExecuteRequest) -> dict[str, Any]:
        expires_at = int(time.time()) + self.store.config.lease_seconds
        attempt = await asyncio.to_thread(self.store.create_attempt, invocation_id, request.idempotency_key, self.boot_id, expires_at)
        # Capture the replay cursor before scheduling provider work so the cockpit
        # cannot miss a chunk emitted between the HTTP response and EventSource.
        event_cursor = await asyncio.to_thread(self.store.latest_event_seq)
        if attempt["state"] == AttemptState.leased.value and attempt["attempt_id"] not in self._tasks:
            cancel = threading.Event()
            self._cancellations[attempt["attempt_id"]] = cancel
            task = asyncio.create_task(self._run(attempt["attempt_id"], cancel), name=f"lattice-{attempt['attempt_id']}")
            self._tasks[attempt["attempt_id"]] = task
            task.add_done_callback(lambda _task, attempt_id=attempt["attempt_id"]: self._complete_task(attempt_id))
        result = self.store.get_attempt(attempt["attempt_id"])
        result["event_cursor"] = event_cursor
        return result

    def _complete_task(self, attempt_id: str) -> None:
        self._tasks.pop(attempt_id, None)
        self._cancellations.pop(attempt_id, None)
        self.kick_conversations()

    async def cancel(self, attempt_id: str) -> dict[str, Any]:
        cancel = self._cancellations.get(attempt_id)
        if cancel is None:
            attempt = self.store.get_attempt(attempt_id)
            if attempt["state"] in {AttemptState.succeeded.value, AttemptState.failed.value, AttemptState.interrupted.value, AttemptState.orphaned.value}:
                return attempt
            raise LatticeConflict("attempt is not owned by this Lattice process")
        try:
            attempt = self.store.get_attempt(attempt_id)
            await self._close_approvals(attempt_id, "operator cancelled attempt")
        finally:
            cancel.set()
        return attempt

    async def _close_approvals(self, attempt_id: str, reason: str) -> None:
        attempt = self.store.get_attempt(attempt_id)
        invocation = self.store.get_invocation(attempt["invocation_id"])
        if invocation["route"]["approval_policy"] == "on-request":
            await asyncio.to_thread(native_approvals.close, self.store, attempt_id, owner_boot_id=self.boot_id, reason=reason)

    async def _run(self, attempt_id: str, cancel: threading.Event) -> None:
        attempt = self.store.get_attempt(attempt_id)
        invocation = self.store.get_invocation(attempt["invocation_id"])
        project_id = invocation["project_id"]
        journal: Path | None = None

        def bound_runtime_receipt(path: Path) -> dict[str, Any]:
            receipt = _runtime_receipt(path, self.store.blobs)
            from .native_recovery import capture_boundary
            receipt['conversation_native_boundary'] = capture_boundary(self.store, invocation, receipt)
            return receipt

        def recover_runtime_receipt(path: Path | None) -> dict[str, Any] | None:
            if path is None or not (path / "request.json").is_file():
                return None
            try:
                return bound_runtime_receipt(path)
            except BaseException as receipt_error:
                return {"kind": "lattice-runtime-receipt-failure", "error": f"{type(receipt_error).__name__}: {receipt_error}"}

        try:
            self.store.mark_attempt_running(attempt_id)
            if invocation["manifest"].get("contract_version") not in {"LatticeContextManifest/2.0", "LatticeContextManifest/3.0", "LatticeContextManifest/4.0", "LatticeContextManifest/5.0", "LatticeContextManifest/6.0", "LatticeContextManifest/7.0", "LatticeContextManifest/8.0", "LatticeContextManifest/9.0", "LatticeContextManifest/10.0", "LatticeContextManifest/11.0", "LoomContextManifest/2.0", "LoomContextManifest/3.0"} or invocation["manifest"].get("response_contract") != invocation["response_schema"]:
                raise LatticeConflict("invocation response contract is not execution-bound by an executable context manifest")
            prompt = verify_and_reconstruct(self.store, invocation["manifest"]).decode("utf-8")
            declared_system = invocation["manifest"].get("provider_boundary", {}).get("system_instructions")
            system_prompt = declared_system["content"] if isinstance(declared_system, dict) else None
            adapter = self._adapter(invocation["route"])
            output_contract = OutputContractV2.from_stored(invocation["response_schema"])

            def on_chunk(channel: str, chunk: bytes) -> None:
                ObservationStore(self.store).append(attempt_id, channel, chunk)
                self.store.heartbeat_attempt(attempt_id, int(time.time()) + self.store.config.lease_seconds)

            def on_journal(root: Path) -> None:
                nonlocal journal
                journal = root
                digest = self.store.blobs.put((root / "request.json").read_bytes())
                self.store.record_event(project_id=project_id, event_type="attempt.journal", subject_type="attempt", subject_id=attempt_id, payload={"invocation_id":invocation["invocation_id"], "path":str(root), "request_ref":f"lattice:blob/{digest}"})

            payload, journal = await asyncio.to_thread(
                invoke_subscription,
                adapter,
                prompt=prompt,
                system_prompt=system_prompt,
                schema=output_contract.provider_schema(),
                runs_dir=self.store.config.runs,
                subject=invocation["invocation_id"],
                effort=invocation["route"]["effort"],
                timeout=float(self.store.config.lease_seconds),
                cancel_event=cancel,
                on_chunk=on_chunk,
                on_journal=on_journal,
                **({"native_approval_callback": NativeApprovalCallback(self.store, attempt_id, owner_boot_id=self.boot_id, cancel_event=cancel)} if invocation["route"]["approval_policy"] == "on-request" else {}),
                **execution_policy(invocation["route"]),
            )
            if output_contract.mode == "conversation":
                response = payload.get("response")
                if not isinstance(response, str):
                    raise SubscriptionInvocationFailed("malformed_output", "provider response lacked response text", journal=journal)
                artifacts = payload.get("artifacts")
                if not isinstance(artifacts, list) or any(
                    not isinstance(item, dict)
                    or set(item) != {"name", "media_type", "content"}
                    or not all(isinstance(item[key], str) for key in ("name", "media_type", "content"))
                    for item in artifacts
                ):
                    raise SubscriptionInvocationFailed("malformed_output", "provider response contained invalid artifacts", journal=journal)
            elif output_contract.mode == "json":
                if set(payload) != {"payload"}:
                    raise SubscriptionInvocationFailed("malformed_output", "provider response lacked the contracted JSON payload", journal=journal)
                response = canonical_bytes(payload["payload"]).decode("utf-8")
                artifacts = [{"name": "result.json", "media_type": "application/json", "content": response}]
            else:
                response = payload.get("content")
                if not isinstance(response, str):
                    raise SubscriptionInvocationFailed("malformed_output", f"provider response lacked contracted {output_contract.mode} content", journal=journal)
                extension = "md" if output_contract.mode == "markdown" else "txt"
                artifacts = [{"name": f"result.{extension}", "media_type": output_contract.media_type or "text/plain", "content": response}]
            self.store.finish_successful_attempt(attempt_id, response=response, artifacts=artifacts, journal_path=str(journal), runtime_receipt=bound_runtime_receipt(journal))
        except SubscriptionInvocationFailed as error:
            terminal = AttemptState.interrupted if error.kind == "operator_interruption" else AttemptState.failed
            receipt = recover_runtime_receipt(error.journal)
            self.store.finish_attempt(attempt_id, state=terminal, failure_kind=error.kind, error_text=str(error), journal_path=str(error.journal) if error.journal else None, runtime_receipt=receipt)
        except BaseException as error:  # execution boundary must always terminalize
            self.store.finish_attempt(
                attempt_id,
                state=AttemptState.failed,
                failure_kind="harness_failure",
                error_text=f"{type(error).__name__}: {error}",
                journal_path=str(journal) if journal else None,
                runtime_receipt=recover_runtime_receipt(journal),
            )
