"""Canonical bytes, immutable identifiers, and durable generated-object envelopes."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from typing import Any

import rfc8785


MAX_CANONICAL_NODES = 100_000
MAX_CANONICAL_DEPTH = 256
SHA256 = re.compile(r"[0-9a-f]{64}")
ARTIFACT_ID = re.compile(r"artifact:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
GENERATED_KIND = re.compile(r"[a-z][a-z0-9.-]*")
SEMVER = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
TRACKING_TIMESTAMP = re.compile(r"[0-9]{8}T[0-9]{6}(?:Z[+-][0-9]{2})?")
GENERATED_SOURCE_KEYS = {"ref", "revision", "sha256"}


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


def validate_generated_object_envelope(envelope: Any) -> None:
    if not isinstance(envelope, dict):
        raise ValueError("generated-object envelope must be an object")
    role = envelope.get("role")
    required = {"contract_version", "role", "kind", "schema", "artifact_id", "generated_at", "producer", "subject", "sources"}
    allowed = required | ({"parent_artifact_id"} if role == "canonical" else set())
    if role not in {"canonical", "snapshot"} or set(envelope) != required and set(envelope) != allowed:
        raise ValueError("source-bound generated-object envelope has invalid fields or role")
    if envelope["contract_version"] != 1 or not isinstance(envelope["kind"], str) or GENERATED_KIND.fullmatch(envelope["kind"]) is None:
        raise ValueError("generated-object contract or kind is invalid")
    schema = envelope["schema"]
    if not isinstance(schema, dict) or set(schema) != {"name", "version"} or not isinstance(schema["name"], str) or GENERATED_KIND.fullmatch(schema["name"]) is None or not isinstance(schema["version"], int) or isinstance(schema["version"], bool) or schema["version"] < 1:
        raise ValueError("generated-object schema is invalid")
    producer = envelope["producer"]
    if not isinstance(producer, dict) or set(producer) != {"name", "version"} or not isinstance(producer["name"], str) or not producer["name"] or not isinstance(producer["version"], str) or SEMVER.fullmatch(producer["version"]) is None:
        raise ValueError("generated-object producer is invalid")
    if not isinstance(envelope["artifact_id"], str) or ARTIFACT_ID.fullmatch(envelope["artifact_id"]) is None or not isinstance(envelope["generated_at"], str) or TRACKING_TIMESTAMP.fullmatch(envelope["generated_at"]) is None or not isinstance(envelope["subject"], str) or not envelope["subject"]:
        raise ValueError("generated-object durable identity is invalid")
    parent = envelope.get("parent_artifact_id")
    if parent is not None and (not isinstance(parent, str) or ARTIFACT_ID.fullmatch(parent) is None):
        raise ValueError("generated-object parent artifact identity is invalid")
    sources = envelope["sources"]
    if not isinstance(sources, list) or not sources:
        raise ValueError("generated-object requires exact sources")
    for source in sources:
        if not isinstance(source, dict) or set(source) != GENERATED_SOURCE_KEYS:
            raise ValueError("generated-object sources require exactly ref, revision, and sha256")
        if not isinstance(source["ref"], str) or not source["ref"]:
            raise ValueError("generated-object source refs must be non-empty strings")
        revision = source["revision"]
        digest = source["sha256"]
        if revision is not None and (not isinstance(revision, str) or not revision):
            raise ValueError("generated-object source revisions must be non-empty strings or null")
        if digest is not None and (not isinstance(digest, str) or SHA256.fullmatch(digest) is None):
            raise ValueError("generated-object source digests must be lowercase SHA-256 or null")
        if revision is None and digest is None:
            raise ValueError("generated-object sources require a revision or SHA-256")


def generated_object(
    *,
    kind: str,
    schema_name: str,
    subject: str,
    sources: list[dict[str, str | None]],
    schema_version: int = 1,
    role: str = "canonical",
    producer_name: str = "lattice",
    producer_version: str = "0.1.0",
    parent_artifact_id: str | None = None,
    artifact_id: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "contract_version": 1,
        "role": role,
        "kind": kind,
        "schema": {"name": schema_name, "version": schema_version},
        "artifact_id": artifact_id or f"artifact:{uuid.uuid4()}",
        "generated_at": generated_at or stamp(),
        "producer": {"name": producer_name, "version": producer_version},
        "subject": subject,
        "sources": sources,
    }
    if parent_artifact_id is not None:
        envelope["parent_artifact_id"] = parent_artifact_id
    validate_generated_object_envelope(envelope)
    return envelope
