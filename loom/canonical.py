"""Canonical bytes, immutable identifiers, and durable generated-object envelopes."""

from __future__ import annotations

import hashlib
import math
import time
import uuid
from typing import Any

import rfc8785


def stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%S", time.localtime())


def opaque_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _reject_non_finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("canonical JSON rejects non-finite numbers")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("canonical JSON object keys must be strings")
            _reject_non_finite(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_non_finite(child)


def canonical_bytes(value: Any) -> bytes:
    _reject_non_finite(value)
    return rfc8785.dumps(value)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def generated_object(
    *,
    kind: str,
    schema_name: str,
    subject: str,
    sources: list[dict[str, str | None]],
    schema_version: int = 1,
    parent_artifact_id: str | None = None,
) -> dict[str, Any]:
    if not sources:
        raise ValueError("canonical Loom artifacts require exact sources")
    envelope: dict[str, Any] = {
        "contract_version": 1,
        "role": "canonical",
        "kind": kind,
        "schema": {"name": schema_name, "version": schema_version},
        "artifact_id": f"artifact:{uuid.uuid4()}",
        "generated_at": stamp(),
        "producer": {"name": "axis-loom", "version": "0.1.0"},
        "subject": subject,
        "sources": sources,
    }
    if parent_artifact_id is not None:
        envelope["parent_artifact_id"] = parent_artifact_id
    return envelope
