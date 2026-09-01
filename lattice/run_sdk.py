"""Tiny project-facing SDK for durable-run semantic events."""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import canonical_bytes, strict_json_loads
from .contracts import RunArtifactClass, RunArtifactCreateV1
from .exact_file import ExactFileCaptureError, capture_exact_file
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
)


EVENT_FD_ENV = "LATTICE_RUN_EVENT_FD"
CONTROL_FD_ENV = "LATTICE_RUN_CONTROL_FD"
RUN_ID_ENV = "LATTICE_RUN_ID"
ATTEMPT_ID_ENV = "LATTICE_RUN_ATTEMPT_ID"
UNIT_PLAN_PATH_ENV = "LATTICE_RUN_UNIT_PLAN_PATH"
CHECKPOINT_ID_ENV = "LATTICE_RUN_CHECKPOINT_ID"
CHECKPOINT_LOCATOR_ENV = "LATTICE_RUN_CHECKPOINT_LOCATOR"
CHECKPOINT_SHA256_ENV = "LATTICE_RUN_CHECKPOINT_SHA256"
CHECKPOINT_BYTES_ENV = "LATTICE_RUN_CHECKPOINT_BYTES"
CHECKPOINT_READ_PATH_ENV = "LATTICE_RUN_CHECKPOINT_READ_PATH"


class DurableRunChannelClosed(RuntimeError):
    """The owning Lattice process no longer receives this worker's events."""


class DurableRunCheckpointChanged(RuntimeError):
    """The selected resume checkpoint no longer matches its admitted bytes."""


@dataclass(frozen=True)
class ResumeCheckpoint:
    artifact_id: str
    storage_locator: str
    content_sha256: str
    byte_count: int
    read_path: Path

    def read_verified(self, *, max_bytes: int) -> bytes:
        """Capture exact checkpoint bytes once, rejecting drift or unbounded reads."""

        try:
            return capture_exact_file(
                self.read_path,
                expected_sha256=self.content_sha256,
                expected_bytes=self.byte_count,
                max_bytes=max_bytes,
            )
        except (ExactFileCaptureError, ValueError) as error:
            raise DurableRunCheckpointChanged(f"resume checkpoint is unavailable: {error}") from error


def _file_identity(path: Path) -> tuple[Path, str, int]:
    if path.is_symlink():
        raise OSError(f"durable-run artifact is a symlink: {path}")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise OSError(f"durable-run artifact is not a regular file: {resolved}")
    before = resolved.stat()
    digest = hashlib.sha256()
    byte_count = 0
    with resolved.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            byte_count += len(chunk)
    after = resolved.stat()
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or byte_count != after.st_size:
        raise OSError(f"durable-run artifact changed while hashing: {resolved}")
    return resolved, digest.hexdigest(), byte_count


class DurableRunSDK:
    """Emit canonical events without importing Lattice's ledger or UI internals."""

    def __init__(self, event_fd: int, control_fd: int, *, run_id: str, attempt_id: str, unit_plan_path: Path, resume_checkpoint: ResumeCheckpoint | None) -> None:
        if event_fd <= 2 or control_fd <= 2 or event_fd == control_fd:
            raise ValueError("durable-run event and control descriptors must be dedicated inherited descriptors")
        os.fstat(event_fd)
        os.fstat(control_fd)
        os.set_blocking(control_fd, False)
        self.event_fd = event_fd
        self.control_fd = control_fd
        self.run_id = run_id
        self.attempt_id = attempt_id
        raw_plan = unit_plan_path.read_bytes()
        if len(raw_plan) > 8 * 1024 * 1024:
            raise ValueError("durable-run unit plan exceeds its eight-megabyte limit")
        plan = strict_json_loads(raw_plan)
        if canonical_bytes(plan) != raw_plan or plan.get("contract_version") != "RunUnitPlan/1.0" or plan.get("run_id") != run_id or plan.get("attempt_id") != attempt_id:
            raise ValueError("durable-run unit plan identity is invalid")
        remaining = plan.get("remaining_unit_keys")
        if not isinstance(remaining, list) or any(not isinstance(key, str) for key in remaining) or len(remaining) != len(set(remaining)):
            raise ValueError("durable-run unit plan has invalid remaining units")
        self.remaining_unit_keys = tuple(remaining)
        self.resume_checkpoint = resume_checkpoint
        self._sequence = 0
        self._closed = False
        self._stop_requested = False

    @classmethod
    def from_environment(cls) -> "DurableRunSDK":
        try:
            event_fd = int(os.environ[EVENT_FD_ENV])
            control_fd = int(os.environ[CONTROL_FD_ENV])
            run_id = os.environ[RUN_ID_ENV]
            attempt_id = os.environ[ATTEMPT_ID_ENV]
            unit_plan_path = Path(os.environ[UNIT_PLAN_PATH_ENV])
        except (KeyError, ValueError) as error:
            raise RuntimeError("durable-run worker environment is incomplete") from error
        checkpoint_values = [os.environ.get(key) for key in (CHECKPOINT_ID_ENV, CHECKPOINT_LOCATOR_ENV, CHECKPOINT_SHA256_ENV, CHECKPOINT_BYTES_ENV, CHECKPOINT_READ_PATH_ENV)]
        if any(value is not None for value in checkpoint_values) and any(value is None for value in checkpoint_values):
            raise RuntimeError("durable-run resume checkpoint environment is incomplete")
        checkpoint = None
        if all(value is not None for value in checkpoint_values):
            try:
                checkpoint = ResumeCheckpoint(
                    artifact_id=checkpoint_values[0] or "",
                    storage_locator=checkpoint_values[1] or "",
                    content_sha256=checkpoint_values[2] or "",
                    byte_count=int(checkpoint_values[3] or ""),
                    read_path=Path(checkpoint_values[4] or ""),
                )
            except ValueError as error:
                raise RuntimeError("durable-run resume checkpoint environment is malformed") from error
            if not checkpoint.artifact_id or len(checkpoint.content_sha256) != 64 or checkpoint.byte_count < 0 or not checkpoint.read_path.is_absolute():
                raise RuntimeError("durable-run resume checkpoint environment is malformed")
        return cls(event_fd, control_fd, run_id=run_id, attempt_id=attempt_id, unit_plan_path=unit_plan_path, resume_checkpoint=checkpoint)

    def _base(self) -> dict[str, int]:
        self._sequence += 1
        return {"sequence": self._sequence, "monotonic_ns": time.monotonic_ns()}

    def emit(self, event: RunWorkerEventV1) -> None:
        if self._closed:
            raise DurableRunChannelClosed("durable-run event channel is closed")
        raw = canonical_bytes(event.model_dump(mode="json")) + b"\n"
        if len(raw) - 1 > MAX_WORKER_EVENT_BYTES:
            raise ValueError("durable-run worker event exceeds its byte limit")
        offset = 0
        try:
            while offset < len(raw):
                written = os.write(self.event_fd, raw[offset:])
                if written <= 0:
                    raise BrokenPipeError("durable-run event descriptor accepted no bytes")
                offset += written
        except (BrokenPipeError, OSError) as error:
            self._closed = True
            raise DurableRunChannelClosed("owning Lattice process no longer receives durable-run events") from error

    def unit_started(self, unit_key: str) -> None:
        self.emit(UnitStartedEvent(**self._base(), unit_key=unit_key))

    def unit_completed(self, unit_key: str, *, output_sha256: str | None = None) -> None:
        self.emit(UnitCompletedEvent(**self._base(), unit_key=unit_key, output_sha256=output_sha256))

    def unit_refused(self, unit_key: str, *, failure_kind: str, error_text: str | None = None) -> None:
        self.emit(UnitRefusedEvent(**self._base(), unit_key=unit_key, failure_kind=failure_kind, error_text=error_text))

    def unit_failed(self, unit_key: str, *, failure_kind: str, error_text: str | None = None) -> None:
        self.emit(UnitFailedEvent(**self._base(), unit_key=unit_key, failure_kind=failure_kind, error_text=error_text))

    def artifact(self, artifact: RunArtifactCreateV1, *, unit_key: str | None = None) -> None:
        self.emit(ArtifactEvent(**self._base(), unit_key=unit_key, artifact=artifact))

    def checkpoint(self, artifact: RunArtifactCreateV1, *, unit_key: str | None = None) -> None:
        self.emit(CheckpointEvent(**self._base(), unit_key=unit_key, artifact=artifact))

    def file_artifact(
        self,
        path: Path,
        *,
        artifact_key: str,
        name: str,
        media_type: str,
        artifact_class: RunArtifactClass = RunArtifactClass.durable_evidence,
        sensitive: bool = False,
        regeneration: dict[str, Any] | None = None,
        unit_key: str | None = None,
    ) -> RunArtifactCreateV1:
        resolved, digest, byte_count = _file_identity(path)
        artifact = RunArtifactCreateV1(
            artifact_key=artifact_key,
            name=name,
            artifact_class=artifact_class,
            sensitive=sensitive,
            media_type=media_type,
            byte_count=byte_count,
            content_sha256=digest,
            storage_locator=f"file:{resolved}",
            regeneration=regeneration,
        )
        if artifact_class == RunArtifactClass.checkpoint:
            self.checkpoint(artifact, unit_key=unit_key)
        else:
            self.artifact(artifact, unit_key=unit_key)
        return artifact

    def resource(self, metrics: dict[str, int | float | str | bool | None]) -> None:
        self.emit(ResourceEvent(**self._base(), metrics=metrics))

    def progress(self, message: str, *, completed: int | None = None, total: int | None = None, safe_to_stop: bool | None = None) -> None:
        self.emit(ProgressEvent(**self._base(), message=message, completed=completed, total=total, safe_to_stop=safe_to_stop))

    def waiting(self, reason: str) -> None:
        self.emit(WaitingEvent(**self._base(), reason=reason))

    def continued(self) -> None:
        self.emit(ContinuedEvent(**self._base()))

    def run_completed(self) -> None:
        self.emit(RunCompletedEvent(**self._base()))

    def run_failed(self, *, failure_kind: str, error_text: str | None = None) -> None:
        self.emit(RunFailedEvent(**self._base(), failure_kind=failure_kind, error_text=error_text))

    def stop_requested(self) -> bool:
        """Poll the supervisor's latched stop request at a semantic boundary."""

        if self._stop_requested:
            return True
        try:
            signal = os.read(self.control_fd, 4096)
        except BlockingIOError:
            return False
        self._stop_requested = signal == b"" or b"S" in signal
        return self._stop_requested

    def close(self) -> None:
        for descriptor_name in ("event_fd", "control_fd"):
            descriptor = getattr(self, descriptor_name)
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                setattr(self, descriptor_name, -1)
        self._closed = True

    def __enter__(self) -> "DurableRunSDK":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()
