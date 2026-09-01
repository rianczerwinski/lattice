"""Canonical child-to-Lattice events for semantic durable-run progress."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter, field_validator, model_validator

from .canonical import canonical_bytes, strict_json_loads
from .contracts import RunArtifactClass, RunArtifactCreateV1, StrictModel


MAX_WORKER_EVENT_BYTES = 1024 * 1024


class WorkerEventBase(StrictModel):
    contract_version: Literal["DurableRunWorkerEvent/1.0"] = "DurableRunWorkerEvent/1.0"
    sequence: int = Field(ge=1)
    monotonic_ns: int = Field(ge=0)


class UnitStartedEvent(WorkerEventBase):
    event_type: Literal["unit_started"] = "unit_started"
    unit_key: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,299}$")


class UnitCompletedEvent(WorkerEventBase):
    event_type: Literal["unit_completed"] = "unit_completed"
    unit_key: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,299}$")
    output_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class UnitRefusedEvent(WorkerEventBase):
    event_type: Literal["unit_refused"] = "unit_refused"
    unit_key: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,299}$")
    failure_kind: str = Field(min_length=1, max_length=200)
    error_text: str | None = Field(default=None, max_length=20_000)


class UnitFailedEvent(WorkerEventBase):
    event_type: Literal["unit_failed"] = "unit_failed"
    unit_key: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,299}$")
    failure_kind: str = Field(min_length=1, max_length=200)
    error_text: str | None = Field(default=None, max_length=20_000)


class ArtifactEvent(WorkerEventBase):
    event_type: Literal["artifact"] = "artifact"
    unit_key: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,299}$")
    artifact: RunArtifactCreateV1

    @model_validator(mode="after")
    def ordinary_artifact_is_not_a_checkpoint(self) -> "ArtifactEvent":
        if self.artifact.artifact_class == RunArtifactClass.checkpoint:
            raise ValueError("checkpoint artifacts require the checkpoint event type")
        return self


class CheckpointEvent(WorkerEventBase):
    event_type: Literal["checkpoint"] = "checkpoint"
    unit_key: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,299}$")
    artifact: RunArtifactCreateV1

    @model_validator(mode="after")
    def artifact_is_a_checkpoint(self) -> "CheckpointEvent":
        if self.artifact.artifact_class != RunArtifactClass.checkpoint:
            raise ValueError("checkpoint events require checkpoint-class artifacts")
        return self


class ResourceEvent(WorkerEventBase):
    event_type: Literal["resource"] = "resource"
    metrics: dict[str, int | float | str | bool | None]

    @field_validator("metrics")
    @classmethod
    def metric_names_are_printable(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > 1000 or any(not key or len(key) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in key) for key in value):
            raise ValueError("resource metric names must be bounded printable text")
        return value


class ProgressEvent(WorkerEventBase):
    event_type: Literal["progress"] = "progress"
    message: str = Field(min_length=1, max_length=20_000)
    completed: int | None = Field(default=None, ge=0)
    total: int | None = Field(default=None, ge=0)
    safe_to_stop: bool | None = None

    @model_validator(mode="after")
    def counts_are_coherent(self) -> "ProgressEvent":
        if self.completed is not None and self.total is not None and self.completed > self.total:
            raise ValueError("progress completed count cannot exceed total")
        return self


class WaitingEvent(WorkerEventBase):
    event_type: Literal["waiting"] = "waiting"
    reason: str = Field(min_length=1, max_length=20_000)


class ContinuedEvent(WorkerEventBase):
    event_type: Literal["continued"] = "continued"


class RunCompletedEvent(WorkerEventBase):
    event_type: Literal["run_completed"] = "run_completed"


class RunFailedEvent(WorkerEventBase):
    event_type: Literal["run_failed"] = "run_failed"
    failure_kind: str = Field(min_length=1, max_length=200)
    error_text: str | None = Field(default=None, max_length=20_000)


RunWorkerEventV1 = Annotated[
    UnitStartedEvent
    | UnitCompletedEvent
    | UnitRefusedEvent
    | UnitFailedEvent
    | ArtifactEvent
    | CheckpointEvent
    | ResourceEvent
    | ProgressEvent
    | WaitingEvent
    | ContinuedEvent
    | RunCompletedEvent
    | RunFailedEvent,
    Field(discriminator="event_type"),
]

WORKER_EVENT_ADAPTER = TypeAdapter(RunWorkerEventV1)


def decode_worker_event(raw: bytes) -> RunWorkerEventV1:
    """Reject non-canonical, oversized, or structurally ambiguous worker events."""

    if not raw or len(raw) > MAX_WORKER_EVENT_BYTES:
        raise ValueError("durable-run worker event is empty or exceeds its byte limit")
    value = strict_json_loads(raw)
    if canonical_bytes(value) != raw:
        raise ValueError("durable-run worker event is not canonical JSON")
    return WORKER_EVENT_ADAPTER.validate_python(value)
