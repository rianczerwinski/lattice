"""Canonical bytes, immutable identifiers, and durable generated-object envelopes."""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from typing import Any

import rfc8785


MAX_CANONICAL_NODES = 100_000
MAX_CANONICAL_DEPTH = 256


def stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%S", time.localtime())


def opaque_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _validate_json_tree(value: Any) -> None:
    stack: list[tuple[Any, int]] = [(value, 0)]
    visited = 0
    while stack:
        current, depth = stack.pop()
        visited += 1
        if visited > MAX_CANONICAL_NODES:
            raise ValueError(f"canonical JSON exceeds {MAX_CANONICAL_NODES} structural nodes")
        if depth > MAX_CANONICAL_DEPTH:
            raise ValueError(f"canonical JSON exceeds depth {MAX_CANONICAL_DEPTH}")
        if isinstance(current, float) and not math.isfinite(current):
            raise ValueError("canonical JSON rejects non-finite numbers")
        if isinstance(current, dict):
            for key, child in current.items():
                if not isinstance(key, str):
                    raise ValueError("canonical JSON object keys must be strings")
                stack.append((child, depth + 1))
        elif isinstance(current, (list, tuple)):
            stack.extend((child, depth + 1) for child in current)


def canonical_bytes(value: Any) -> bytes:
    _validate_json_tree(value)
    return rfc8785.dumps(value)


def strict_json_loads(raw: bytes | str) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, child in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON object key: {key}")
            value[key] = child
        return value

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number: {value}")

    try:
        value = json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except RecursionError as error:
        raise ValueError("JSON exceeds the parser's structural depth") from error
    _validate_json_tree(value)
    return value


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
    artifact_id: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if not sources:
        raise ValueError("canonical Loom artifacts require exact sources")
    envelope: dict[str, Any] = {
        "contract_version": 1,
        "role": "canonical",
        "kind": kind,
        "schema": {"name": schema_name, "version": schema_version},
        "artifact_id": artifact_id or f"artifact:{uuid.uuid4()}",
        "generated_at": generated_at or stamp(),
        "producer": {"name": "axis-loom", "version": "0.1.0"},
        "subject": subject,
        "sources": sources,
    }
    if parent_artifact_id is not None:
        envelope["parent_artifact_id"] = parent_artifact_id
    return envelope
