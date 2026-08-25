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
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConsolidationVerificationError(f"{label} is not valid UTF-8 JSON") from error


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
    if not isinstance(expected_files, list) or not expected_files:
        raise ConsolidationVerificationError(f"{label} source census is empty or malformed")
    actual: list[dict[str, Any]] = []
    for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()):
        if path.is_symlink():
            raise ConsolidationVerificationError(f"{label} source contains a symlink: {path}")
        if path.is_file():
            actual.append({"relative_path": path.relative_to(source).as_posix(), "content_sha256": sha256_file(path), "byte_count": path.stat().st_size})
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
    return {"runtime_id": manifest["runtime_id"], "provider_profiles": len(attestations), "package_files": len(actual)}


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


def verify_consumer_census(axisvi_root: Path) -> dict[str, Any]:
    roots = (
        axisvi_root / "sui/mission/projects/canon/dev/quaternary-exhaustion",
        axisvi_root / "sui/mission/typology/abduction/field-survey/research-pipeline",
    )
    forbidden_files = {
        "domain_research/generation/antigravity.py",
        "domain_research/generation/claude.py",
        "domain_research/generation/codex.py",
        "domain_research/generation/grok.py",
    }
    violations: list[str] = []
    scanned = 0
    for root in roots:
        if not root.is_dir():
            raise ConsolidationVerificationError(f"execution consumer is missing: {root}")
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
    if violations:
        raise ConsolidationVerificationError("consumer census found competing execution authority:\n" + "\n".join(violations))
    return {"consumer_roots": len(roots), "source_files_scanned": scanned, "violations": 0}


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


def verify(*, data_root: Path, runtime_manifest: Path | None, axisvi_root: Path, forbidden_files: list[Path]) -> dict[str, Any]:
    if runtime_manifest is None:
        raise ConsolidationVerificationError("an installed runtime manifest is required for lattice_execution_ready")
    historical_roots = {
        "field-survey.research-pipeline": axisvi_root / "sui/mission/typology/abduction/field-survey/research-pipeline",
    }
    database = verify_database(data_root, historical_roots=historical_roots)
    census = verify_consumer_census(axisvi_root)
    runtime = verify_runtime_manifest(runtime_manifest, database_profiles=_database_profiles(data_root))
    forbidden = [path.read_bytes().rstrip(b"\r\n") for path in forbidden_files]
    secret_scan = scan_forbidden_bytes((data_root / "lattice.sqlite3", data_root / "blobs", data_root / "model-runs"), forbidden) if forbidden else {"files": 0, "bytes": 0, "canaries": 0}
    sources = [
        {"ref": f"file:{data_root / 'lattice.sqlite3'}", "revision": None, "sha256": sha256_file(data_root / "lattice.sqlite3")},
        {"ref": f"tree:{axisvi_root}", "revision": None, "sha256": sha256_bytes(canonical_bytes(census))},
    ]
    sources.append({"ref": f"file:{runtime_manifest}", "revision": None, "sha256": sha256_file(runtime_manifest)})
    generated_at = time.strftime("%Y%m%dT%H%M%S", time.localtime())
    return {
        "generated_object": {
            "contract_version": 1,
            "role": "canonical",
            "kind": "lattice-execution-consolidation-verification",
            "schema": {"name": "lattice-execution-consolidation-verification", "version": 1},
            "artifact_id": f"artifact:{uuid.uuid4()}",
            "generated_at": generated_at,
            "producer": {"name": "independent-lattice-consolidation-verifier", "version": "1.0.0"},
            "subject": "lattice:execution-consolidation",
            "sources": sources,
        },
        "contract_version": "LatticeExecutionConsolidationVerification/1.0",
        "verdict": "pass",
        "closure_state": "lattice_execution_ready",
        "database": database,
        "runtime": runtime,
        "consumer_census": census,
        "credential_canary_scan": secret_scan,
        "proof_limits": ["Credential absence is proven only for supplied exact canary bytes; arbitrary unknown secrets cannot be recognized by content alone."],
    }


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--axisvi-root", type=Path, required=True)
    parser.add_argument("--forbidden-byte-file", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = arguments()
    try:
        report = verify(data_root=args.data_root.resolve(), runtime_manifest=args.runtime_manifest.resolve() if args.runtime_manifest else None, axisvi_root=args.axisvi_root.resolve(), forbidden_files=args.forbidden_byte_file)
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
