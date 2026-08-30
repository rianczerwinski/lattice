#!/usr/bin/env python3
"""Independently verify Lattice execution consolidation from durable base evidence."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

import rfc8785


EXPECTED_SCHEMA_VERSION = 12
TERMINAL_INVOCATION_STATES = {"succeeded", "failed", "interrupted", "imported"}
TERMINAL_ATTEMPT_STATES = {"succeeded", "failed", "interrupted", "orphaned"}
PROVIDER_HARNESSES = {"anthropic": "claude", "openai": "codex", "google": "antigravity", "moonshot": "kimi", "xai": "grok"}
PROVIDER_EXECUTABLES = {"claude", "codex", "agy", "grok"}
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
ARTIFACT_ID = re.compile(r"artifact:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
REQUIRED_EVIDENCE = {
    "runtime_manifest": "LatticeInstalledRuntime/4.0",
    "consumer_registry": "ExecutionConsumerRegistry/1.0",
    "historical_execution_set": "HistoricalExecutionSet/1.0",
    "doctor": "LatticeDoctorReceipt/1.0",
    "qualification": "LatticeQualificationReceipt/1.0",
    "browser_acceptance": "LatticeBrowserAcceptanceReceipt/1.0",
    "restore_continuation": "LatticeRestoreContinuationReceipt/1.0",
    "findings": "LatticeExecutionConsolidationFindings/2.0",
}
REQUIRED_SUITES = {"lattice-backend", "lattice-frontend", "lattice-browser", "canon", "field-survey", "slate-mirror"}
REQUIRED_BROWSER_ACTIONS = {
    "blank-project",
    "claude-send",
    "codex-side-quest",
    "fan-out",
    "exact-excerpt-graft",
    "reciprocal-cross-feed",
    "fan-in",
    "context-inspection",
    "dormant-branch-return",
    "slate-tag",
}
REQUIRED_PROVIDER_PROFILES = {
    ("anthropic", "compiled-fresh"),
    ("anthropic", "workspace-agent"),
    ("anthropic", "native-continuation:resume"),
    ("anthropic", "native-continuation:fork"),
    ("anthropic", "portable-fallback"),
    ("openai", "compiled-fresh"),
    ("openai", "workspace-agent"),
    ("openai", "native-continuation:resume"),
    ("openai", "native-continuation:fork"),
    ("openai", "portable-fallback"),
    ("google", "compiled-fresh"),
    ("google", "research-fresh"),
    ("xai", "compiled-fresh"),
    ("xai", "research-fresh"),
}


class ConsolidationVerificationError(RuntimeError):
    """A closure claim disagrees with independently read evidence."""


def canonical_bytes(value: Any) -> bytes:
    return rfc8785.dumps(value)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def parse_json(raw: bytes | str, *, label: str) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, child in pairs:
            if key in value:
                raise ConsolidationVerificationError(f"{label} contains duplicate key {key!r}")
            value[key] = child
        return value

    try:
        return json.loads(raw, object_pairs_hook=unique_object, parse_constant=lambda value: (_ for _ in ()).throw(ConsolidationVerificationError(f"{label} contains non-finite number {value}")))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConsolidationVerificationError(f"{label} is not valid UTF-8 JSON") from error


def exact_json(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    value = parse_json(raw, label=label)
    if not isinstance(value, dict) or canonical_bytes(value) != raw:
        raise ConsolidationVerificationError(f"{label} is not exact canonical JSON")
    return value, raw


def verify_generated_object(value: dict[str, Any], *, role: str, label: str, binding: tuple[str, str] | None = None) -> dict[str, Any]:
    envelope = value.get("generated_object")
    if not isinstance(envelope, dict) or envelope.get("contract_version") != 1 or envelope.get("role") != role:
        raise ConsolidationVerificationError(f"{label} has the wrong generated-object role")
    artifact_id = envelope.get("artifact_id")
    if not isinstance(artifact_id, str) or ARTIFACT_ID.fullmatch(artifact_id) is None:
        raise ConsolidationVerificationError(f"{label} has an invalid artifact identity")
    if role in {"canonical", "snapshot"}:
        sources = envelope.get("sources")
        if not isinstance(sources, list) or not sources:
            raise ConsolidationVerificationError(f"{label} has no exact source bindings")
        for source in sources:
            if not isinstance(source, dict) or set(source) != {"ref", "revision", "sha256"} or source.get("revision") is None and source.get("sha256") is None:
                raise ConsolidationVerificationError(f"{label} has a malformed source binding")
            digest = source.get("sha256")
            if digest is not None:
                require_digest(digest, label=f"{label} source digest")
    if role == "attestation":
        bound = envelope.get("binding")
        validator = envelope.get("validator")
        if binding is None or bound != {"artifact_id": binding[0], "sha256": binding[1]}:
            raise ConsolidationVerificationError(f"{label} is not bound to the exact target artifact")
        if not isinstance(validator, dict) or not validator.get("name") or not validator.get("version") or envelope.get("result") not in {"pass", "fail"}:
            raise ConsolidationVerificationError(f"{label} has an invalid validator or result")
    return envelope


def resolve_evidence(bundle_path: Path, item: Any, *, label: str) -> tuple[Path, dict[str, Any], bytes]:
    if not isinstance(item, dict) or set(item) != {"ref", "artifact_id", "sha256"}:
        raise ConsolidationVerificationError(f"closure bundle evidence entry is malformed: {label}")
    digest = require_digest(item["sha256"], label=f"{label} evidence digest")
    ref = item["ref"]
    if not isinstance(ref, str) or not ref.startswith("file:"):
        raise ConsolidationVerificationError(f"{label} evidence must use an exact file ref")
    candidate = Path(ref.removeprefix("file:"))
    path = candidate if candidate.is_absolute() else bundle_path.parent / candidate
    path = path.resolve(strict=True)
    value, raw = exact_json(path, label=label)
    if sha256_bytes(raw) != digest or value.get("generated_object", {}).get("artifact_id") != item["artifact_id"]:
        raise ConsolidationVerificationError(f"{label} evidence bytes or artifact identity disagree with the bundle")
    return path, value, raw


def require_digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or HEX64.fullmatch(value) is None:
        raise ConsolidationVerificationError(f"{label} is not a lowercase SHA-256 digest")
    return value


def verify_blob(data_root: Path, digest: str) -> int:
    require_digest(digest, label="blob identity")
    path = data_root / "blobs" / digest[:2] / digest
    if path.is_symlink() or not path.is_file():
        raise ConsolidationVerificationError(f"referenced Lattice blob is missing: {digest}")
    before = path.stat()
    actual = sha256_file(path)
    after = path.stat()
    if actual != digest or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ConsolidationVerificationError(f"referenced Lattice blob changed or disagrees with its identity: {digest}")
    return after.st_size


def _json_rows(connection: sqlite3.Connection, table: str, column: str, identity: str) -> Iterable[tuple[str, Any]]:
    for row in connection.execute(f'SELECT "{identity}","{column}" FROM "{table}" WHERE "{column}" IS NOT NULL ORDER BY "{identity}"'):
        yield str(row[0]), parse_json(row[1], label=f"{table}.{column}:{row[0]}")


def _verify_provider_profiles(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    for row in connection.execute("SELECT * FROM provider_profiles ORDER BY profile_id"):
        profile_id = str(row["profile_id"])
        definition = parse_json(row["definition_json"], label=f"provider profile {profile_id}")
        if canonical_bytes(definition).decode("utf-8") != row["definition_json"] or sha256_bytes(canonical_bytes(definition)) != row["definition_sha256"]:
            raise ConsolidationVerificationError(f"provider profile bytes are not exact: {profile_id}")
        without_identity = {key: value for key, value in definition.items() if key != "profile_id"}
        expected_id = f"provider-profile:{sha256_bytes(canonical_bytes(without_identity))}"
        if definition.get("profile_id") != profile_id or profile_id != expected_id:
            raise ConsolidationVerificationError(f"provider profile identity is self-inconsistent: {profile_id}")
        if row["provider_code"] != definition.get("provider_code") or row["harness"] != definition.get("harness") or row["model_code"] != definition.get("default_model"):
            raise ConsolidationVerificationError(f"provider profile columns disagree with its definition: {profile_id}")
        if PROVIDER_HARNESSES.get(str(row["provider_code"])) != row["harness"] or definition.get("auth_surface") != "consumer_subscription_cli":
            raise ConsolidationVerificationError(f"provider profile is not an official subscription route: {profile_id}")
        profiles[profile_id] = definition
    if not profiles:
        raise ConsolidationVerificationError("no immutable provider profiles exist")
    for identity, receipt in _json_rows(connection, "provider_qualifications", "receipt_json", "qualification_id"):
        row = connection.execute("SELECT * FROM provider_qualifications WHERE qualification_id=?", (identity,)).fetchone()
        if row["profile_id"] not in profiles or sha256_bytes(canonical_bytes(receipt)) != row["receipt_sha256"]:
            raise ConsolidationVerificationError(f"provider qualification is unbound or corrupt: {identity}")
        require_digest(row["binary_sha256"], label=f"provider qualification binary {identity}")
    return profiles


def _verify_route(route: Any, profiles: dict[str, dict[str, Any]], *, label: str) -> None:
    if not isinstance(route, dict):
        raise ConsolidationVerificationError(f"{label} route is not an object")
    provider = route.get("provider")
    harness = route.get("harness")
    if PROVIDER_HARNESSES.get(provider) != harness:
        raise ConsolidationVerificationError(f"{label} does not use the provider's official subscription harness")
    if route.get("route_kind", "execution") == "historical-import":
        if route.get("provider_profile_id") is not None or not str(route.get("model", "")).startswith("historical-"):
            raise ConsolidationVerificationError(f"{label} historical route claims executable authority")
        return
    profile_id = route.get("provider_profile_id")
    definition = profiles.get(profile_id)
    if definition is None:
        raise ConsolidationVerificationError(f"{label} has no immutable provider profile")
    if definition.get("provider_code") != provider or definition.get("harness") != harness or definition.get("default_model") != route.get("model"):
        raise ConsolidationVerificationError(f"{label} route disagrees with its provider profile")


def _collect_blob_refs(value: Any, refs: set[str]) -> None:
    if isinstance(value, str) and value.startswith("lattice:blob/"):
        refs.add(require_digest(value.removeprefix("lattice:blob/"), label="embedded blob reference"))
    elif isinstance(value, dict):
        for child in value.values():
            _collect_blob_refs(child, refs)
    elif isinstance(value, list):
        for child in value:
            _collect_blob_refs(child, refs)


def _verify_runtime_receipt(data_root: Path, receipt: Any, route: dict[str, Any], *, label: str) -> set[str]:
    if not isinstance(receipt, dict) or receipt.get("kind") != "lattice-model-runtime-receipt":
        raise ConsolidationVerificationError(f"{label} lacks a full Lattice runtime receipt")
    if receipt.get("driver") != route.get("harness") or receipt.get("model") != route.get("model"):
        raise ConsolidationVerificationError(f"{label} runtime receipt used a different provider or model")
    files = receipt.get("files")
    if not isinstance(files, dict) or not {"request.json", "checkpoint.json", "terminal.json"}.issubset(files):
        raise ConsolidationVerificationError(f"{label} runtime receipt lacks bound control files")
    refs: set[str] = set()
    decoded: dict[str, Any] = {}
    for name, item in files.items():
        if not isinstance(item, dict) or set(item) != {"ref", "sha256", "byte_count"}:
            raise ConsolidationVerificationError(f"{label} runtime file receipt is malformed: {name}")
        digest = require_digest(item["sha256"], label=f"{label}:{name}")
        if item["ref"] != f"lattice:blob/{digest}" or verify_blob(data_root, digest) != item["byte_count"]:
            raise ConsolidationVerificationError(f"{label} runtime file receipt disagrees with its blob: {name}")
        refs.add(digest)
        if name in {"request.json", "checkpoint.json", "terminal.json"}:
            path = data_root / "blobs" / digest[:2] / digest
            decoded[name] = parse_json(path.read_bytes(), label=f"{label}:{name}")
    if receipt.get("request_sha256") != files["request.json"]["sha256"] or receipt.get("checkpoint_sha256") != files["checkpoint.json"]["sha256"] or receipt.get("terminal_sha256") != files["terminal.json"]["sha256"]:
        raise ConsolidationVerificationError(f"{label} top-level receipt hashes disagree with its files")
    request = decoded["request.json"]
    checkpoint = decoded["checkpoint.json"]
    terminal = decoded["terminal.json"]
    if request.get("driver") != route.get("harness") or request.get("model") != route.get("model") or checkpoint.get("request_sha256") != files["request.json"]["sha256"]:
        raise ConsolidationVerificationError(f"{label} checkpoint does not bind the executed request")
    if terminal.get("status") != "success":
        raise ConsolidationVerificationError(f"{label} successful attempt has a non-success terminal journal")
    return refs


def _verify_historical_source(source: Path, expected_files: Any, *, label: str) -> None:
    if source.is_symlink() or not source.is_dir():
        raise ConsolidationVerificationError(f"{label} source directory is missing or indirect: {source}")
    if not isinstance(expected_files, list):
        raise ConsolidationVerificationError(f"{label} source census is malformed")
    modern = any(isinstance(item, dict) and "file_type" in item for item in expected_files)
    actual: list[dict[str, Any]] = []
    for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()):
        if path.is_symlink():
            target = os.readlink(path).encode("utf-8")
            item = {"relative_path": path.relative_to(source).as_posix(), "content_sha256": sha256_bytes(target), "byte_count": len(target)}
            if modern:
                item["file_type"] = "symlink"
            actual.append(item)
            continue
        if path.is_file():
            item = {"relative_path": path.relative_to(source).as_posix(), "content_sha256": sha256_file(path), "byte_count": path.stat().st_size}
            if modern:
                item["file_type"] = "regular"
            actual.append(item)
    if actual != expected_files:
        raise ConsolidationVerificationError(f"{label} source bytes no longer match the imported census")


def verify_database(data_root: Path, *, require_terminal_external: bool = True, historical_roots: dict[str, Path] | None = None) -> dict[str, Any]:
    database = data_root / "lattice.sqlite3"
    connection = sqlite3.connect(f"file:{database.resolve(strict=True)}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    refs: set[str] = set()
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ConsolidationVerificationError("SQLite quick_check failed")
        foreign = list(connection.execute("PRAGMA foreign_key_check"))
        if foreign:
            raise ConsolidationVerificationError(f"SQLite foreign-key violations exist: {foreign[:3]}")
        schema = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        if schema != EXPECTED_SCHEMA_VERSION:
            raise ConsolidationVerificationError(f"database schema is {schema}, expected {EXPECTED_SCHEMA_VERSION}")
        profiles = _verify_provider_profiles(connection)
        invocations: dict[str, sqlite3.Row] = {}
        routes: dict[str, dict[str, Any]] = {}
        for row in connection.execute("SELECT * FROM invocations ORDER BY invocation_id"):
            invocation_id = str(row["invocation_id"])
            manifest = parse_json(row["manifest_json"], label=f"invocation manifest {invocation_id}")
            route = parse_json(row["route_json"], label=f"invocation route {invocation_id}")
            expected_manifest = sha256_bytes(canonical_bytes({key: value for key, value in manifest.items() if key != "manifest_sha256"}))
            if row["manifest_sha256"] != expected_manifest or manifest.get("manifest_sha256") != expected_manifest or manifest.get("execution") != route:
                raise ConsolidationVerificationError(f"invocation manifest is not self-consistent: {invocation_id}")
            _verify_route(route, profiles, label=f"invocation {invocation_id}")
            refs.add(require_digest(row["compiled_prompt_sha256"], label=f"compiled prompt {invocation_id}"))
            _collect_blob_refs(manifest, refs)
            invocations[invocation_id] = row
            routes[invocation_id] = route
        external_count = 0
        for row in connection.execute("SELECT * FROM external_operations ORDER BY external_project_key,operation_key"):
            external_count += 1
            label = f"external operation {row['external_project_key']}:{row['operation_key']}"
            request = parse_json(row["request_json"], label=label)
            if sha256_bytes(canonical_bytes(request)) != row["request_sha256"] or request.get("idempotency_key") != row["idempotency_key"]:
                raise ConsolidationVerificationError(f"{label} request identity is corrupt")
            invocation_id = row["invocation_id"]
            if invocation_id is None or invocation_id not in invocations:
                raise ConsolidationVerificationError(f"{label} is not bound to exactly one invocation")
            if request.get("route") != routes[invocation_id]:
                raise ConsolidationVerificationError(f"{label} invocation route differs from the submitted route")
            invocation = invocations[invocation_id]
            if require_terminal_external and (row["state"] != "terminal" or invocation["state"] not in TERMINAL_INVOCATION_STATES):
                raise ConsolidationVerificationError(f"{label} is not terminal")
            attempts = list(connection.execute("SELECT * FROM attempts WHERE invocation_id=? ORDER BY attempt_number", (invocation_id,)))
            if not attempts or attempts[-1]["state"] not in TERMINAL_ATTEMPT_STATES:
                raise ConsolidationVerificationError(f"{label} has no terminal attempt")
            terminal = attempts[-1]
            if terminal["state"] == "succeeded":
                refs.add(require_digest(terminal["output_sha256"], label=f"{label} output"))
                receipt = parse_json(terminal["runtime_receipt_json"], label=f"{label} runtime receipt") if terminal["runtime_receipt_json"] else None
                refs.update(_verify_runtime_receipt(data_root, receipt, routes[invocation_id], label=label))
        for row in connection.execute("SELECT * FROM artifacts ORDER BY artifact_id"):
            refs.add(require_digest(row["content_sha256"], label=f"artifact {row['artifact_id']}"))
            _collect_blob_refs(parse_json(row["generated_json"], label=f"artifact {row['artifact_id']}"), refs)
        historical_count = 0
        historical_sources_verified = 0
        for row in connection.execute("SELECT * FROM historical_execution_imports ORDER BY import_id"):
            historical_count += 1
            files = parse_json(row["census_json"], label=f"historical import {row['import_id']}")
            if sha256_bytes(canonical_bytes(files)) != row["source_census_sha256"]:
                raise ConsolidationVerificationError(f"historical execution census is corrupt: {row['import_id']}")
            request_material = {
                "contract_version": "HistoricalExecutionImport/1.0",
                "external_project_key": row["external_project_key"],
                "operation_key": row["operation_key"],
                "source_ref": row["source_ref"],
                "source_census_sha256": row["source_census_sha256"],
                "files": files,
                "provider": row["provider"],
                "model": row["model"],
                "original_invocation_id": row["original_invocation_id"],
                "terminal_state": row["terminal_state"],
            }
            if sha256_bytes(canonical_bytes(request_material)) != row["request_sha256"]:
                raise ConsolidationVerificationError(f"historical execution request identity is corrupt: {row['import_id']}")
            base = historical_roots.get(row["external_project_key"]) if historical_roots is not None else None
            if base is None:
                raise ConsolidationVerificationError(f"historical execution source has no verifier-owned root: {row['import_id']}")
            source_ref = Path(row["source_ref"])
            if source_ref.is_absolute():
                source = source_ref.resolve(strict=True)
            else:
                base = base.resolve(strict=True)
                source = (base / source_ref).resolve(strict=True)
                if not source.is_relative_to(base):
                    raise ConsolidationVerificationError(f"historical execution source escapes its verifier-owned root: {row['import_id']}")
            _verify_historical_source(source, files, label=f"historical import {row['import_id']}")
            historical_sources_verified += 1
        for table, column in (("context_blocks", "content_sha256"), ("attempts", "output_sha256"), ("workflow_commands", "result_sha256"), ("conversation_imports", "result_sha256"), ("workflow_transitions", "payload_sha256"), ("workflow_instances", "pack_definition_sha256")):
            for row in connection.execute(f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'):
                refs.add(require_digest(row[0], label=f"{table}.{column}"))
        for table, column, identity in (("attempts", "runtime_receipt_json", "attempt_id"), ("events", "payload_json", "seq"), ("workflow_commands", "receipt_json", "command_id")):
            for _identity, value in _json_rows(connection, table, column, identity):
                _collect_blob_refs(value, refs)
    finally:
        connection.close()
    blob_bytes = sum(verify_blob(data_root, digest) for digest in sorted(refs))
    return {"schema_version": EXPECTED_SCHEMA_VERSION, "provider_profiles": len(profiles), "invocations": len(invocations), "external_operations": external_count, "historical_imports": historical_count, "historical_sources_verified": historical_sources_verified, "referenced_blobs": len(refs), "referenced_blob_bytes": blob_bytes}


def verify_runtime_manifest(path: Path, *, database_profiles: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    raw = path.read_bytes()
    manifest = parse_json(raw, label="installed runtime manifest")
    if canonical_bytes(manifest) != raw or not isinstance(manifest, dict) or manifest.get("contract_version") != "LatticeInstalledRuntime/4.0":
        raise ConsolidationVerificationError("installed runtime manifest is not exact canonical v4")
    if set(manifest) != {"generated_object", "contract_version", "runtime_id", "python", "packages", "module_locations", "source_trees", "provider_attestations"}:
        raise ConsolidationVerificationError("installed runtime manifest fields are malformed")
    locations = manifest["module_locations"]
    if not isinstance(locations, dict) or set(locations) != {"lattice"}:
        raise ConsolidationVerificationError("installed runtime has competing package authorities")
    package_root = Path(locations["lattice"]).resolve(strict=True).parent
    expected = manifest["source_trees"].get("lattice") if isinstance(manifest["source_trees"], dict) else None
    if not isinstance(expected, list) or not expected:
        raise ConsolidationVerificationError("installed runtime lacks a package-byte inventory")
    actual: list[dict[str, Any]] = []
    for candidate in sorted(package_root.rglob("*")):
        relative = candidate.relative_to(package_root)
        if "__pycache__" in relative.parts or candidate.suffix == ".pyc":
            continue
        if candidate.is_symlink():
            raise ConsolidationVerificationError(f"installed package contains a symlink: {candidate}")
        if candidate.is_file():
            actual.append({"path": relative.as_posix(), "sha256": sha256_file(candidate), "byte_count": candidate.stat().st_size})
    if actual != expected:
        raise ConsolidationVerificationError("installed Lattice bytes differ from the runtime attestation")
    attestations = manifest["provider_attestations"]
    if not isinstance(attestations, list) or not attestations:
        raise ConsolidationVerificationError("installed runtime lacks provider attestations")
    seen: set[str] = set()
    for item in attestations:
        if not isinstance(item, dict) or item.get("provider_code") in seen:
            raise ConsolidationVerificationError("installed runtime provider attestation set is malformed")
        seen.add(item["provider_code"])
        profile_id = item.get("profile_id")
        definition_keys = {"contract_version", "provider_code", "harness", "official_cli", "default_model", "supported_models", "supported_efforts", "auth_surface", "response_mode", "isolation", "proof_limits", "qualified_profiles", "exact_context_qualified", "qualification_blocker", "capability_compiler", "parser_revision", "profile_id"}
        definition = {key: item.get(key) for key in definition_keys}
        expected_profile_id = f"provider-profile:{sha256_bytes(canonical_bytes({key: value for key, value in definition.items() if key != 'profile_id'}))}"
        if profile_id != expected_profile_id or database_profiles is not None and database_profiles.get(profile_id) != definition:
            raise ConsolidationVerificationError(f"installed provider profile is not exact: {item.get('provider_code')}")
        binary = item.get("binary")
        if item.get("installed"):
            if not isinstance(binary, str) or not Path(binary).is_absolute() or sha256_file(Path(binary).resolve(strict=True)) != item.get("binary_sha256"):
                raise ConsolidationVerificationError(f"installed provider binary changed: {item.get('provider_code')}")
        elif binary is not None or item.get("binary_sha256") is not None:
            raise ConsolidationVerificationError(f"uninstalled provider claims binary evidence: {item.get('provider_code')}")
    runtime_tree_digest = sha256_bytes(canonical_bytes(expected))
    provider_attestation_digest = sha256_bytes(canonical_bytes(attestations))
    return {
        "runtime_id": manifest["runtime_id"],
        "runtime_artifact_id": manifest["generated_object"]["artifact_id"],
        "runtime_tree_digest": runtime_tree_digest,
        "provider_attestation_digest": provider_attestation_digest,
        "provider_profiles": len(attestations),
        "package_files": len(actual),
    }


def _source_inventory(root: Path) -> list[dict[str, Any]]:
    excluded_parts = {".git", ".venv", "node_modules", "__pycache__", "runtime", "results", "archive", "dist", ".svelte-kit", ".pytest_cache"}
    inventory: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if any(part in excluded_parts for part in relative.parts) or path.is_symlink() or not path.is_file():
            continue
        inventory.append({"path": relative.as_posix(), "sha256": sha256_file(path), "byte_count": path.stat().st_size})
    return inventory


def verify_consumer_registry(axisvi_root: Path, value: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    verify_generated_object(value, role="canonical", label="execution consumer registry")
    if value.get("contract_version") != "ExecutionConsumerRegistry/1.0":
        raise ConsolidationVerificationError("execution consumer registry contract is unsupported")
    consumers = value.get("consumers")
    if not isinstance(consumers, list) or not consumers:
        raise ConsolidationVerificationError("execution consumer registry is empty")
    identities: set[str] = set()
    inventories: dict[str, list[dict[str, Any]]] = {}
    for consumer in consumers:
        if not isinstance(consumer, dict) or set(consumer) != {"consumer_id", "source_root", "client_boundary", "forbidden_direct_authorities"}:
            raise ConsolidationVerificationError("execution consumer registry entry is malformed")
        identity = consumer["consumer_id"]
        if not isinstance(identity, str) or not identity or identity in identities:
            raise ConsolidationVerificationError("execution consumer registry identity is missing or duplicated")
        identities.add(identity)
        root = (axisvi_root / consumer["source_root"]).resolve(strict=True)
        boundary = (axisvi_root / consumer["client_boundary"]).resolve(strict=True)
        if not root.is_relative_to(axisvi_root.resolve()) or not boundary.is_relative_to(root) or not boundary.is_file():
            raise ConsolidationVerificationError(f"consumer boundary does not resolve beneath its registered root: {identity}")
        if consumer["forbidden_direct_authorities"] != ["provider-cli", "provider-sdk", "developer-api"]:
            raise ConsolidationVerificationError(f"consumer authority prohibition is incomplete: {identity}")
        inventory = _source_inventory(root)
        if not inventory:
            raise ConsolidationVerificationError(f"consumer source inventory is empty: {identity}")
        inventories[identity] = inventory
    return consumers, inventories


def verify_historical_execution_set(value: dict[str, Any], *, data_root: Path, axisvi_root: Path) -> dict[str, Any]:
    verify_generated_object(value, role="snapshot", label="historical execution set")
    if value.get("contract_version") != "HistoricalExecutionSet/1.0" or value.get("external_project_key") != "field-survey.research-pipeline":
        raise ConsolidationVerificationError("historical execution set contract or consumer is unsupported")
    source_root_value = value.get("source_root")
    run_root_value = value.get("run_root")
    if not isinstance(source_root_value, str) or not isinstance(run_root_value, str):
        raise ConsolidationVerificationError("historical execution set has no source or run root")
    source_root = (axisvi_root / source_root_value).resolve(strict=True)
    if not source_root.is_relative_to(axisvi_root.resolve()) or not source_root.is_dir():
        raise ConsolidationVerificationError("historical execution set source root is invalid")
    runs = value.get("runs")
    if not isinstance(runs, list) or not runs:
        raise ConsolidationVerificationError("historical execution set is empty")
    run_root = (source_root / run_root_value).resolve(strict=True)
    if not run_root.is_relative_to(source_root) or not run_root.is_dir():
        raise ConsolidationVerificationError("historical execution set run root is invalid")
    expected: dict[tuple[str, str], dict[str, Any]] = {}
    source_directories = {path.relative_to(source_root).as_posix() for path in run_root.iterdir() if path.is_dir() and not path.is_symlink()}
    for run in runs:
        if not isinstance(run, dict):
            raise ConsolidationVerificationError("historical execution set contains a malformed run")
        required = {"operation_key", "source_ref", "source_census_sha256", "files", "provider", "model", "original_invocation_id", "terminal_state"}
        if set(run) != required:
            raise ConsolidationVerificationError("historical execution set run fields are malformed")
        source_ref = run["source_ref"]
        if not isinstance(source_ref, str) or Path(source_ref).is_absolute():
            raise ConsolidationVerificationError("historical execution set source ref must be relative")
        source = (source_root / source_ref).resolve(strict=True)
        if not source.is_relative_to(source_root):
            raise ConsolidationVerificationError("historical execution set source escapes its root")
        _verify_historical_source(source, run["files"], label=f"historical set {run['operation_key']}")
        digest = sha256_bytes(canonical_bytes(run["files"]))
        if digest != run["source_census_sha256"]:
            raise ConsolidationVerificationError(f"historical execution set census digest is wrong: {run['operation_key']}")
        key = (run["operation_key"], source_ref)
        if key in expected:
            raise ConsolidationVerificationError(f"historical execution set duplicates a run: {key}")
        expected[key] = run
    represented_directories = {run["source_ref"] for run in runs}
    if represented_directories != source_directories:
        missing = sorted(source_directories - represented_directories)
        extra = sorted(represented_directories - source_directories)
        raise ConsolidationVerificationError(f"historical execution set does not census every run directory; missing={missing[:5]} extra={extra[:5]}")
    connection = sqlite3.connect(f"file:{(data_root / 'lattice.sqlite3').resolve(strict=True)}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        actual_rows = list(connection.execute("SELECT operation_key,source_ref,source_census_sha256,provider,model,original_invocation_id,terminal_state FROM historical_execution_imports WHERE external_project_key=? ORDER BY operation_key,source_ref", (value["external_project_key"],)))
    finally:
        connection.close()
    actual = {(row["operation_key"], row["source_ref"]): dict(row) for row in actual_rows}
    if set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        raise ConsolidationVerificationError(f"historical execution imports do not equal the frozen set; missing={missing[:5]} extra={extra[:5]}")
    for key, run in expected.items():
        row = actual[key]
        for field in ("source_census_sha256", "provider", "model", "original_invocation_id", "terminal_state"):
            if row[field] != run[field]:
                raise ConsolidationVerificationError(f"historical execution import disagrees with frozen set: {key}:{field}")
    return {"expected_runs": len(expected), "imported_runs": len(actual), "incomplete_runs": sum(run["terminal_state"] != "success" for run in runs)}


def verify_doctor_receipt(value: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    verify_generated_object(value, role="snapshot", label="doctor receipt")
    if value.get("contract_version") != "LatticeDoctorReceipt/1.0" or value.get("status") != "pass":
        raise ConsolidationVerificationError("doctor receipt is not a passing v1 receipt")
    if value.get("runtime_artifact_id") != runtime["runtime_artifact_id"] or value.get("runtime_tree_digest") != runtime["runtime_tree_digest"] or value.get("provider_attestation_digest") != runtime["provider_attestation_digest"]:
        raise ConsolidationVerificationError("doctor receipt belongs to a different installed runtime")
    checks = value.get("checks")
    expected_checks = {"outport", "launchd", "service", "database-blobs-and-journals", "runtime", "provider-adapters", "backup-and-recovery", "closure"}
    if not isinstance(checks, list) or {item.get("name") for item in checks if isinstance(item, dict)} != expected_checks or any(item.get("state") != "pass" for item in checks):
        raise ConsolidationVerificationError("doctor receipt does not contain every passing check")
    return {"checks": len(checks), "runtime_id": value.get("runtime_id")}


def verify_qualification_receipt(value: dict[str, Any], *, runtime: dict[str, Any], consumer_inventories: dict[str, list[dict[str, Any]]], data_root: Path) -> dict[str, Any]:
    verify_generated_object(value, role="snapshot", label="qualification receipt")
    if value.get("contract_version") != "LatticeQualificationReceipt/1.0" or value.get("runtime_artifact_id") != runtime["runtime_artifact_id"]:
        raise ConsolidationVerificationError("qualification receipt belongs to a different runtime")
    suites = value.get("suites")
    if not isinstance(suites, list) or {item.get("suite_id") for item in suites if isinstance(item, dict)} != REQUIRED_SUITES:
        raise ConsolidationVerificationError("qualification receipt omits a required source or consumer suite")
    for suite in suites:
        if set(suite) != {"suite_id", "truth_kinds", "command", "cwd", "exit_code", "stdout_sha256", "stderr_sha256"} or suite["exit_code"] != 0:
            raise ConsolidationVerificationError(f"qualification suite did not pass exactly: {suite.get('suite_id')}")
        require_digest(suite["stdout_sha256"], label=f"{suite['suite_id']} stdout")
        require_digest(suite["stderr_sha256"], label=f"{suite['suite_id']} stderr")
        if not isinstance(suite["truth_kinds"], list) or not suite["truth_kinds"]:
            raise ConsolidationVerificationError(f"qualification suite has no truth kind: {suite['suite_id']}")
    if value.get("consumer_source_inventories") != consumer_inventories:
        raise ConsolidationVerificationError("qualification receipt consumer sources are stale or incomplete")
    canaries = value.get("provider_canaries")
    if not isinstance(canaries, list):
        raise ConsolidationVerificationError("qualification receipt has no provider canaries")
    observed = {(item.get("provider"), item.get("profile")) for item in canaries if isinstance(item, dict) and item.get("state") == "succeeded"}
    if observed != REQUIRED_PROVIDER_PROFILES:
        raise ConsolidationVerificationError(f"qualified provider profile set is incomplete: missing={sorted(REQUIRED_PROVIDER_PROFILES - observed)} extra={sorted(observed - REQUIRED_PROVIDER_PROFILES)}")
    connection = sqlite3.connect(f"file:{(data_root / 'lattice.sqlite3').resolve(strict=True)}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        for item in canaries:
            invocation = connection.execute("SELECT state,manifest_sha256 FROM invocations WHERE invocation_id=?", (item.get("invocation_id"),)).fetchone()
            if invocation is None or invocation["state"] != "succeeded" or invocation["manifest_sha256"] != item.get("manifest_sha256"):
                raise ConsolidationVerificationError(f"provider canary does not resolve to exact successful execution: {item.get('provider')}:{item.get('profile')}")
        for consumer in ("canon.quaternary-exhaustion", "field-survey.research-pipeline"):
            row = connection.execute("SELECT invocation_id,state FROM external_operations WHERE external_project_key=? ORDER BY updated_at DESC LIMIT 1", (consumer,)).fetchone()
            if row is None or row["state"] != "terminal":
                raise ConsolidationVerificationError(f"consumer canary is absent or nonterminal: {consumer}")
    finally:
        connection.close()
    interruption = value.get("interruption")
    if not isinstance(interruption, dict) or interruption.get("pre_restart_state") not in {"leased", "running"} or interruption.get("post_restart_state") not in {"interrupted", "orphaned", "failed"} or not interruption.get("continuation_invocation_id"):
        raise ConsolidationVerificationError("installed active-work interruption was not proven")
    if value.get("kimi") != {"state": "unavailable", "reason": "unqualified"}:
        raise ConsolidationVerificationError("Kimi availability is not represented truthfully")
    return {"suites": len(suites), "provider_canaries": len(canaries), "consumers": len(consumer_inventories)}


def verify_browser_receipt(value: dict[str, Any], *, data_root: Path) -> dict[str, Any]:
    verify_generated_object(value, role="snapshot", label="browser acceptance receipt")
    if value.get("contract_version") != "LatticeBrowserAcceptanceReceipt/1.0":
        raise ConsolidationVerificationError("browser acceptance receipt contract is unsupported")
    actions = value.get("actions")
    if not isinstance(actions, list) or {item.get("action") for item in actions if isinstance(item, dict)} != REQUIRED_BROWSER_ACTIONS:
        raise ConsolidationVerificationError("browser acceptance did not traverse the complete conversational journey")
    connection = sqlite3.connect(f"file:{(data_root / 'lattice.sqlite3').resolve(strict=True)}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        project_id = value.get("project_id")
        if connection.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone() is None:
            raise ConsolidationVerificationError("browser project does not exist in the base ledger")
        referenced_invocations: set[str] = set()
        for action in actions:
            for invocation_id in action.get("invocation_ids", []):
                referenced_invocations.add(invocation_id)
        manifests = value.get("manifest_sha256s")
        if not isinstance(manifests, dict) or set(manifests) != referenced_invocations:
            raise ConsolidationVerificationError("browser receipt does not bind every referenced invocation manifest")
        for invocation_id, expected in manifests.items():
            row = connection.execute("SELECT project_id,manifest_sha256 FROM invocations WHERE invocation_id=?", (invocation_id,)).fetchone()
            if row is None or row["project_id"] != project_id or row["manifest_sha256"] != expected:
                raise ConsolidationVerificationError(f"browser invocation does not resolve exactly: {invocation_id}")
        slate = value.get("slate")
        if not isinstance(slate, dict):
            raise ConsolidationVerificationError("browser receipt has no live Slate evidence")
        slice_row = connection.execute("SELECT external_ref FROM slices WHERE slice_id=?", (slate.get("slice_id"),)).fetchone()
        edge_row = connection.execute("SELECT 1 FROM edges WHERE relation='concerns' AND source_id=? AND target_id=?", (slate.get("invocation_id"), slate.get("external_ref"))).fetchone()
        if slice_row is None or slice_row["external_ref"] != slate.get("external_ref") or edge_row is None or slate.get("copied_transcript_bytes") != 0:
            raise ConsolidationVerificationError("browser Slate claim is not a live concern-backed slice")
        excerpt = value.get("excerpt")
        if not isinstance(excerpt, dict):
            raise ConsolidationVerificationError("browser receipt has no exact excerpt evidence")
        block = connection.execute("SELECT source_sha256,content_sha256,transformation_json FROM context_blocks WHERE block_id=?", (excerpt.get("block_id"),)).fetchone()
        transformation = parse_json(block["transformation_json"], label="browser excerpt transformation") if block is not None and block["transformation_json"] else None
        expected_transformation = {key: excerpt[key] for key in ("source_block_id", "source_sha256", "start_byte", "end_byte", "excerpt_sha256")}
        if block is None or block["source_sha256"] != excerpt.get("source_sha256") or block["content_sha256"] != excerpt.get("excerpt_sha256") or not isinstance(transformation, dict) or transformation.get("kind") != "exact-utf8-byte-range-v1" or {key: transformation.get(key) for key in expected_transformation} != expected_transformation:
            raise ConsolidationVerificationError("browser excerpt does not resolve to exact stored byte ranges")
    finally:
        connection.close()
    return {"actions": len(actions), "invocations": len(referenced_invocations), "slate_slice": value["slate"]["slice_id"]}


def verify_restore_receipt(value: dict[str, Any], *, historical_roots: dict[str, Path]) -> dict[str, Any]:
    verify_generated_object(value, role="snapshot", label="restore continuation receipt")
    if value.get("contract_version") != "LatticeRestoreContinuationReceipt/1.0":
        raise ConsolidationVerificationError("restore continuation receipt contract is unsupported")
    restored_root = Path(str(value.get("restored_root", ""))).resolve(strict=True)
    if not restored_root.is_dir() or value.get("source_state_digest") != value.get("restored_state_digest"):
        raise ConsolidationVerificationError("restore continuation did not preserve the source state identity")
    verified = verify_database(restored_root, require_terminal_external=False, historical_roots=historical_roots)
    connection = sqlite3.connect(f"file:{(restored_root / 'lattice.sqlite3').resolve(strict=True)}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        child = connection.execute("SELECT manifest_json,manifest_sha256,state FROM invocations WHERE invocation_id=?", (value.get("child_invocation_id"),)).fetchone()
        if child is None or child["state"] != "succeeded" or child["manifest_sha256"] != value.get("child_manifest_sha256"):
            raise ConsolidationVerificationError("restored continuation child does not resolve exactly")
        manifest = parse_json(child["manifest_json"], label="restored child manifest")
        parent_id = value.get("parent_invocation_id")
        parents = {segment.get("producing_invocation_id") for segment in manifest.get("segments", []) if isinstance(segment, dict)}
        if parent_id not in parents:
            raise ConsolidationVerificationError("restored continuation did not consume restored ancestry")
        if connection.execute("SELECT 1 FROM invocations WHERE invocation_id=?", (parent_id,)).fetchone() is None:
            raise ConsolidationVerificationError("restored continuation parent is absent")
    finally:
        connection.close()
    return {"restored_root": str(restored_root), "child_invocation_id": value["child_invocation_id"], "database": verified}


def verify_findings(value: dict[str, Any], *, allow_pending: bool) -> dict[str, Any]:
    verify_generated_object(value, role="canonical", label="finding ledger")
    if value.get("contract_version") != "LatticeExecutionConsolidationFindings/2.0":
        raise ConsolidationVerificationError("finding ledger generation is stale")
    findings = value.get("findings")
    if not isinstance(findings, list) or not findings:
        raise ConsolidationVerificationError("finding ledger is empty")
    blockers = [item for item in findings if item.get("severity") in {"S0", "S1", "S2"} and item.get("status") != "closed"]
    if blockers and not allow_pending:
        raise ConsolidationVerificationError(f"in-scope findings remain open: {[item.get('finding_id') for item in blockers]}")
    if allow_pending and any(item.get("status") not in {"implemented-pending-verification", "closed"} for item in blockers):
        raise ConsolidationVerificationError("retirement readiness contains an unimplemented in-scope finding")
    return {"findings": len(findings), "blockers": len(blockers)}


def _provider_launches(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError) as error:
        raise ConsolidationVerificationError(f"consumer source is unreadable Python: {path}") from error
    findings: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        function = node.func
        call_name = function.attr if isinstance(function, ast.Attribute) else function.id if isinstance(function, ast.Name) else ""
        if call_name not in {"run", "Popen", "create_subprocess_exec", "create_subprocess_shell"}:
            continue
        command = node.args[0]
        values = command.elts if isinstance(command, (ast.List, ast.Tuple)) else [command]
        first = values[0] if values else None
        if isinstance(first, ast.Constant) and first.value in PROVIDER_EXECUTABLES:
            findings.append(f"{path}:{node.lineno}: direct provider executable {first.value}")
        elif isinstance(first, ast.Name) and re.fullmatch(r"(?:claude|codex|agy|grok)(?:_bin)?", first.id):
            findings.append(f"{path}:{node.lineno}: direct provider executable variable {first.id}")
    return findings


def verify_consumer_census(axisvi_root: Path, consumers: list[dict[str, Any]], *, require_retired_model_runner: bool) -> dict[str, Any]:
    forbidden_files = {
        "domain_research/generation/antigravity.py",
        "domain_research/generation/claude.py",
        "domain_research/generation/codex.py",
        "domain_research/generation/grok.py",
    }
    violations: list[str] = []
    scanned = 0
    for consumer in consumers:
        root = axisvi_root / consumer["source_root"]
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            if any(part in {".git", "runtime", "results", "archive", "tests", "__pycache__"} for part in path.relative_to(root).parts) or not path.is_file():
                continue
            if relative in forbidden_files:
                violations.append(f"retired provider launcher remains: {path}")
            if path.suffix not in {".py", ".toml", ".sh"}:
                continue
            scanned += 1
            content = path.read_text(encoding="utf-8", errors="strict")
            if "axis_model_runner" in content:
                violations.append(f"retired sibling execution package remains active: {path}")
            if path.suffix == ".py" and "test" not in path.relative_to(root).parts:
                violations.extend(_provider_launches(path))
    pyproject = axisvi_root / "axis/chariots/lattice/pyproject.toml"
    if "axis-model-runner" in pyproject.read_text(encoding="utf-8"):
        violations.append("Lattice packaging still depends on the sibling model runner")
    model_runner = axisvi_root / "axis/tools/model-runner"
    if require_retired_model_runner and model_runner.exists():
        violations.append(f"retired sibling execution tree remains: {model_runner}")
    if not require_retired_model_runner and model_runner.exists():
        references = []
        for path in sorted(axisvi_root.rglob("*.py")):
            relative = path.relative_to(axisvi_root)
            if any(part in {".git", ".venv", "node_modules", "recollection", "runtime", "archive", "tests", "__pycache__"} for part in relative.parts) or path.is_relative_to(model_runner):
                continue
            if "axis_model_runner" in path.read_text(encoding="utf-8", errors="strict"):
                references.append(str(path))
        if references:
            violations.extend(f"retired sibling execution package remains active: {path}" for path in references)

    excluded = {".git", ".venv", "node_modules", "recollection", "runtime", "archive", "results", "tests", "fixtures", "__pycache__", "dist", "build", ".svelte-kit", ".pytest_cache"}
    lattice_root = axisvi_root / "axis/chariots/lattice"
    scanned_global = 0
    for path in sorted(axisvi_root.rglob("*")):
        relative = path.relative_to(axisvi_root)
        if any(part in excluded for part in relative.parts) or path.is_relative_to(lattice_root) or path.is_relative_to(model_runner) or not path.is_file():
            continue
        if path.suffix == ".py":
            scanned_global += 1
            violations.extend(_provider_launches(path))
        elif path.suffix in {".js", ".mjs", ".cjs", ".ts", ".sh", ".zsh"}:
            scanned_global += 1
            content = path.read_text(encoding="utf-8", errors="strict")
            provider = r"(?:claude|codex|agy|grok)"
            patterns = (
                rf"(?:spawn|execFile|execFileSync)\s*\(\s*['\"]{provider}['\"]",
                rf"(?:^|[;&|]\s*){provider}(?:\s|$)",
            )
            if any(re.search(pattern, content, re.MULTILINE) for pattern in patterns):
                violations.append(f"{path}: direct provider executable outside Lattice")
    if violations:
        raise ConsolidationVerificationError("consumer census found competing execution authority:\n" + "\n".join(violations))
    return {"consumer_roots": len(consumers), "consumer_source_files_scanned": scanned, "repository_source_files_scanned": scanned_global, "model_runner_retired": not model_runner.exists(), "violations": 0}


def scan_forbidden_bytes(roots: Iterable[Path], forbidden: Iterable[bytes]) -> dict[str, int]:
    needles = [item for item in forbidden if item]
    scanned_files = 0
    scanned_bytes = 0
    for root in roots:
        paths = [root] if root.is_file() else sorted(path for path in root.rglob("*") if path.is_file() and not path.is_symlink())
        for path in paths:
            content = path.read_bytes()
            scanned_files += 1
            scanned_bytes += len(content)
            if any(needle in content for needle in needles):
                raise ConsolidationVerificationError(f"forbidden credential canary entered durable state: {path}")
    return {"files": scanned_files, "bytes": scanned_bytes, "canaries": len(needles)}


def _database_profiles(data_root: Path) -> dict[str, dict[str, Any]]:
    connection = sqlite3.connect(f"file:{(data_root / 'lattice.sqlite3').resolve(strict=True)}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return _verify_provider_profiles(connection)
    finally:
        connection.close()


def _fixed_suite_commands(axisvi_root: Path) -> dict[str, tuple[Path, list[list[str]]]]:
    lattice = axisvi_root / "axis/chariots/lattice"
    return {
        "lattice-backend": (lattice, [[str(lattice / ".venv/bin/python"), "-m", "pytest", "-q"]]),
        "lattice-frontend": (lattice / "web", [["npm", "run", "check"], ["npm", "test", "--", "--run"]]),
        "lattice-browser": (lattice / "web", [["npm", "run", "test:browser"]]),
        "canon": (axisvi_root / "sui/mission/projects/canon/dev/quaternary-exhaustion", [["uv", "run", "pytest", "-q", "tests/test_quaternary_workflow.py"]]),
        "field-survey": (axisvi_root / "sui/mission/typology/abduction/field-survey/research-pipeline", [["uv", "run", "pytest", "-q"]]),
        "slate-mirror": (axisvi_root / "axis/tools/slate-mirror", [["node", "--test", "server.test.js"]]),
    }


def rerun_source_suites(axisvi_root: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    fixed = _fixed_suite_commands(axisvi_root)
    declared = {item["suite_id"]: item for item in receipt["suites"]}
    results: dict[str, Any] = {}
    for suite_id, (cwd, commands) in fixed.items():
        if declared[suite_id]["command"] != commands or Path(declared[suite_id]["cwd"]).resolve() != cwd.resolve():
            raise ConsolidationVerificationError(f"qualification receipt changed the fixed suite command: {suite_id}")
        outputs: list[dict[str, Any]] = []
        for command in commands:
            completed = subprocess.run(command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            outputs.append({"command": command, "exit_code": completed.returncode, "stdout_sha256": sha256_bytes(completed.stdout), "stderr_sha256": sha256_bytes(completed.stderr)})
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).decode("utf-8", errors="replace")[-4000:]
                raise ConsolidationVerificationError(f"independent suite rerun failed: {suite_id}: {detail}")
        results[suite_id] = outputs
    return results


def rerun_live_doctor(runtime_manifest_path: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    executable = runtime_manifest_path.parent / ".venv/bin/lattice"
    if not executable.is_file():
        raise ConsolidationVerificationError("installed Lattice executable is absent beside the runtime manifest")
    completed = subprocess.run([str(executable), "doctor", "--json"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).decode("utf-8", errors="replace")[-4000:]
        raise ConsolidationVerificationError(f"live installed doctor failed: {detail}")
    live = parse_json(completed.stdout, label="live doctor output")
    if not isinstance(live, dict) or live.get("status") != "pass":
        raise ConsolidationVerificationError("live installed doctor did not report pass")
    expected = receipt.get("doctor_output")
    if not isinstance(expected, dict) or expected.get("status") != "pass" or {item.get("name") for item in expected.get("checks", [])} != {item.get("name") for item in live.get("checks", [])}:
        raise ConsolidationVerificationError("doctor receipt does not represent the live doctor check set")
    return {"status": "pass", "checks": len(live["checks"])}


def verify(
    *,
    bundle_path: Path,
    data_root: Path,
    axisvi_root: Path,
    forbidden_files: list[Path],
    allow_pending: bool = False,
    require_retired_model_runner: bool = True,
    rerun_suites: bool = True,
    rerun_doctor: bool = True,
) -> dict[str, Any]:
    bundle, bundle_raw = exact_json(bundle_path, label="execution closure bundle")
    bundle_envelope = verify_generated_object(bundle, role="canonical", label="execution closure bundle")
    if bundle.get("contract_version") != "LatticeExecutionClosureBundle/1.0" or bundle.get("target_version") != "0.3.1":
        raise ConsolidationVerificationError("execution closure bundle contract or target version is wrong")
    evidence = bundle.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != set(REQUIRED_EVIDENCE):
        raise ConsolidationVerificationError("execution closure bundle omits or adds evidence planes")
    resolved: dict[str, tuple[Path, dict[str, Any], bytes]] = {}
    for name, contract in REQUIRED_EVIDENCE.items():
        resolved[name] = resolve_evidence(bundle_path, evidence[name], label=name)
        if resolved[name][1].get("contract_version") != contract:
            raise ConsolidationVerificationError(f"closure evidence has the wrong contract: {name}")
    expected_sources = sorted((item["ref"], item["sha256"]) for item in evidence.values())
    bound_sources = sorted((item["ref"], item["sha256"]) for item in bundle_envelope.get("sources", []))
    if bound_sources != expected_sources:
        raise ConsolidationVerificationError("closure bundle envelope does not bind every evidence byte exactly once")

    historical_roots = {"field-survey.research-pipeline": axisvi_root / "sui/mission/typology/abduction/field-survey/research-pipeline"}
    runtime_manifest_path, _runtime_manifest, _runtime_raw = resolved["runtime_manifest"]
    runtime = verify_runtime_manifest(runtime_manifest_path, database_profiles=_database_profiles(data_root))
    if not runtime["runtime_id"].startswith("0.3.1-"):
        raise ConsolidationVerificationError("installed runtime is not Lattice 0.3.1")
    database = verify_database(data_root, historical_roots=historical_roots)
    consumers, inventories = verify_consumer_registry(axisvi_root, resolved["consumer_registry"][1])
    census = verify_consumer_census(axisvi_root, consumers, require_retired_model_runner=require_retired_model_runner)
    historical = verify_historical_execution_set(resolved["historical_execution_set"][1], data_root=data_root, axisvi_root=axisvi_root)
    doctor = verify_doctor_receipt(resolved["doctor"][1], runtime)
    qualification = verify_qualification_receipt(resolved["qualification"][1], runtime=runtime, consumer_inventories=inventories, data_root=data_root)
    browser = verify_browser_receipt(resolved["browser_acceptance"][1], data_root=data_root)
    restore = verify_restore_receipt(resolved["restore_continuation"][1], historical_roots=historical_roots)
    findings = verify_findings(resolved["findings"][1], allow_pending=allow_pending)

    forbidden = [path.read_bytes().rstrip(b"\r\n") for path in forbidden_files]
    expected_canaries = sorted(resolved["qualification"][1].get("credential_canary_sha256s", []))
    if not forbidden or sorted(sha256_bytes(item) for item in forbidden) != expected_canaries:
        raise ConsolidationVerificationError("exact isolated credential canaries are missing or disagree with qualification")
    secret_scan = scan_forbidden_bytes((data_root / "lattice.sqlite3", data_root / "blobs", data_root / "model-runs", *[item[0] for item in resolved.values()]), forbidden)
    suite_rerun = rerun_source_suites(axisvi_root, resolved["qualification"][1]) if rerun_suites else {"state": "not-rerun-in-this-call"}
    live_doctor = rerun_live_doctor(runtime_manifest_path, resolved["doctor"][1]) if rerun_doctor else {"state": "not-rerun-in-this-call"}

    generated_at = time.strftime("%Y%m%dT%H%M%S", time.localtime())
    bundle_digest = sha256_bytes(bundle_raw)
    return {
        "generated_object": {
            "contract_version": 1,
            "role": "attestation",
            "kind": "lattice-execution-closure-attestation",
            "schema": {"name": "lattice-execution-closure-attestation", "version": 2},
            "artifact_id": f"artifact:{uuid.uuid4()}",
            "generated_at": generated_at,
            "producer": {"name": "independent-lattice-consolidation-verifier", "version": "2.0.0"},
            "subject": "lattice:execution-consolidation",
            "binding": {"artifact_id": bundle_envelope["artifact_id"], "sha256": bundle_digest},
            "validator": {"name": "independent-lattice-consolidation-verifier", "version": "2.0.0"},
            "result": "pass",
        },
        "contract_version": "LatticeExecutionClosureAttestation/2.0",
        "closure_state": "retirement_ready" if allow_pending else "lattice_execution_ready",
        "database": database,
        "runtime": runtime,
        "consumer_census": census,
        "historical_execution_set": historical,
        "doctor": doctor,
        "qualification": qualification,
        "browser_acceptance": browser,
        "restore_continuation": restore,
        "findings": findings,
        "credential_canary_scan": secret_scan,
        "suite_rerun": suite_rerun,
        "live_doctor": live_doctor,
        "proof_limits": ["Credential absence is proven only for the exact isolated canary bytes; arbitrary unknown secrets cannot be recognized by content alone.", "Provider-owned ambient context remains bounded by each adapter's explicit proof limits."],
    }


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--axisvi-root", type=Path, required=True)
    parser.add_argument("--forbidden-byte-file", type=Path, action="append", default=[])
    parser.add_argument("--retirement-readiness", action="store_true", help="permit implemented-pending-verification findings and the inert legacy tree")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = arguments()
    try:
        report = verify(
            bundle_path=args.bundle.resolve(),
            data_root=args.data_root.resolve(),
            axisvi_root=args.axisvi_root.resolve(),
            forbidden_files=args.forbidden_byte_file,
            allow_pending=args.retirement_readiness,
            require_retired_model_runner=not args.retirement_readiness,
        )
    except (ConsolidationVerificationError, OSError, sqlite3.Error, ValueError) as error:
        print(f"execution consolidation verification failed: {error}", file=sys.stderr)
        return 1
    output = canonical_bytes(report)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(f".{args.output.name}.{os.getpid()}.pending")
        temporary.write_bytes(output)
        os.replace(temporary, args.output)
    sys.stdout.buffer.write(output + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
