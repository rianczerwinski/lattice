"""Durable leased execution of sealed Loom invocations through model-runner."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from dataclasses import replace
from typing import Any

from axis_model_runner import (
    DEFAULT_ADAPTERS,
    SubscriptionInvocationFailed,
    SubscriptionRouteUnavailable,
    invoke_subscription,
)

from .canonical import sha256_bytes
from .contracts import AttemptState, ExecuteRequest
from .manifest_reader import verify_and_reconstruct
from .store import LoomConflict, LoomStore


def _runtime_receipt(journal: Any) -> dict[str, Any]:
    root = __import__("pathlib").Path(journal)
    request_path = root / "request.json"
    checkpoint_path = root / "checkpoint.json"
    terminal_path = root / "terminal.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    terminal = json.loads(terminal_path.read_text(encoding="utf-8")) if terminal_path.is_file() else None
    return {
        "kind": "loom-model-runtime-receipt",
        "journal_path": str(root),
        "request_sha256": sha256_bytes(request_path.read_bytes()),
        "checkpoint_sha256": sha256_bytes(checkpoint_path.read_bytes()),
        "terminal_sha256": sha256_bytes(terminal_path.read_bytes()) if terminal_path.is_file() else None,
        "driver": request["driver"],
        "model": request["model"],
        "cli_version": request["metadata"].get("cli_version"),
        "binary": request["metadata"].get("binary"),
        "isolation": request["metadata"].get("isolation"),
        "native_session": checkpoint.get("native_session"),
        "observed": checkpoint.get("observed"),
        "terminal": terminal,
    }


class ExecutionSupervisor:
    def __init__(self, store: LoomStore) -> None:
        self.store = store
        self.boot_id = f"boot_{uuid.uuid4().hex}"
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancellations: dict[str, threading.Event] = {}

    async def start(self) -> int:
        attempts = await asyncio.to_thread(self.store.reconcile_orphans, self.boot_id)
        commands = await asyncio.to_thread(self.store.reconcile_workflow_commands, self.boot_id)
        return attempts + commands

    async def stop(self) -> None:
        for cancel in self._cancellations.values():
            cancel.set()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    def _adapter(self, route: dict[str, Any]):
        base = next((item for item in DEFAULT_ADAPTERS if item.provider_code == route["provider"]), None)
        if base is None or base.cli != route["harness"]:
            raise SubscriptionRouteUnavailable("provider and official CLI route are not qualified together")
        return replace(base, model_code=route["model"])

    async def execute(self, invocation_id: str, request: ExecuteRequest) -> dict[str, Any]:
        expires_at = int(time.time()) + self.store.config.lease_seconds
        attempt = await asyncio.to_thread(self.store.create_attempt, invocation_id, request.idempotency_key, self.boot_id, expires_at)
        if attempt["state"] == AttemptState.leased.value and attempt["attempt_id"] not in self._tasks:
            cancel = threading.Event()
            self._cancellations[attempt["attempt_id"]] = cancel
            task = asyncio.create_task(self._run(attempt["attempt_id"], cancel), name=f"loom-{attempt['attempt_id']}")
            self._tasks[attempt["attempt_id"]] = task
            task.add_done_callback(lambda _task, attempt_id=attempt["attempt_id"]: self._complete_task(attempt_id))
        return self.store.get_attempt(attempt["attempt_id"])

    def _complete_task(self, attempt_id: str) -> None:
        self._tasks.pop(attempt_id, None)
        self._cancellations.pop(attempt_id, None)

    async def cancel(self, attempt_id: str) -> dict[str, Any]:
        attempt = self.store.get_attempt(attempt_id)
        cancel = self._cancellations.get(attempt_id)
        if cancel is None:
            if attempt["state"] in {AttemptState.succeeded.value, AttemptState.failed.value, AttemptState.interrupted.value, AttemptState.orphaned.value}:
                return attempt
            raise LoomConflict("attempt is not owned by this Loom process")
        cancel.set()
        return attempt

    async def _run(self, attempt_id: str, cancel: threading.Event) -> None:
        attempt = self.store.get_attempt(attempt_id)
        invocation = self.store.get_invocation(attempt["invocation_id"])
        project_id = invocation["project_id"]
        try:
            self.store.mark_attempt_running(attempt_id)
            prompt = verify_and_reconstruct(self.store, invocation["manifest"]).decode("utf-8")
            adapter = self._adapter(invocation["route"])

            def on_chunk(channel: str, chunk: bytes) -> None:
                digest = self.store.blobs.put(chunk)
                self.store.record_event(project_id=project_id, event_type="attempt.chunk", subject_type="attempt", subject_id=attempt_id, payload={"channel": channel, "sha256": digest, "byte_count": len(chunk), "preview": chunk.decode("utf-8", errors="replace")[:1000]})
                self.store.heartbeat_attempt(attempt_id, int(time.time()) + self.store.config.lease_seconds)

            payload, journal = await asyncio.to_thread(
                invoke_subscription,
                adapter,
                prompt=prompt,
                schema=invocation["response_schema"],
                runs_dir=self.store.config.runs,
                subject=invocation["invocation_id"],
                effort=invocation["route"]["effort"],
                timeout=float(self.store.config.lease_seconds),
                cancel_event=cancel,
                on_chunk=on_chunk,
                session_policy=invocation["route"]["session_policy"],
                tools=invocation["route"]["tools"],
                permissions=invocation["route"]["permissions"],
                workspace=invocation["route"]["workspace"],
                network_policy=invocation["route"]["network_policy"],
            )
            response = payload.get("response")
            if not isinstance(response, str):
                raise SubscriptionInvocationFailed("malformed_output", "provider response lacked response text", journal=journal)
            artifacts = payload.get("artifacts")
            if not isinstance(artifacts, list) or any(not isinstance(item, dict) or set(item) != {"name", "media_type", "content"} or not all(isinstance(item[key], str) for key in item) for item in artifacts):
                raise SubscriptionInvocationFailed("malformed_output", "provider response contained invalid artifacts", journal=journal)
            self.store.finish_successful_attempt(attempt_id, response=response, artifacts=artifacts, journal_path=str(journal), runtime_receipt=_runtime_receipt(journal))
        except SubscriptionInvocationFailed as error:
            terminal = AttemptState.interrupted if error.kind == "operator_interruption" else AttemptState.failed
            receipt = _runtime_receipt(error.journal) if error.journal and (error.journal / "request.json").is_file() else None
            self.store.finish_attempt(attempt_id, state=terminal, failure_kind=error.kind, error_text=str(error), journal_path=str(error.journal) if error.journal else None, runtime_receipt=receipt)
        except BaseException as error:  # execution boundary must always terminalize
            self.store.finish_attempt(attempt_id, state=AttemptState.failed, failure_kind="harness_failure", error_text=f"{type(error).__name__}: {error}")
