"""Read-only diagnostics over canonical or restored Lattice state."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .execution import load_checkpoint

from .canonical import canonical_bytes
from .snapshot import materialized_snapshot_database, verify_snapshot
from .store import CHANGE_TABLES, LatticeStore, SCHEMA_VERSION


CURRENT_BLOB_PREFIXES = ("lattice:blob/", "loom:blob/")


def _connect_read_only(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{database.resolve(strict=True)}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _state_digest(connection: sqlite3.Connection, tables: tuple[str, ...]) -> str:
    digest = hashlib.sha256()

    def bind(value: Any) -> None:
        encoded = canonical_bytes(value)
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)

    bind({"kind": "LatticeStateDigest", "version": 2})
    for table in tables:
        _columns, primary = LatticeStore._table_contract(connection, table)
        order = ",".join(f'"{name}"' for name in primary)
        bind({"stream": "current-view", "table": table})
        for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY {order}'):
            bind(dict(row))
    for name, query in (
        ("events", "SELECT * FROM events ORDER BY seq"),
        ("ledger-changes", "SELECT * FROM ledger_changes ORDER BY seq"),
        ("append-integrity", "SELECT * FROM append_integrity ORDER BY seq"),
    ):
        bind({"stream": name})
        for row in connection.execute(query):
            bind(dict(row))
    return digest.hexdigest()


def _tracked_tables(connection: sqlite3.Connection) -> tuple[str, ...]:
    tables = tuple(sorted(str(row[0]) for row in connection.execute("SELECT tbl_name FROM sqlite_master WHERE type='trigger' AND name LIKE 'ledger_%_insert'")))
    unknown = sorted(set(tables) - set(CHANGE_TABLES))
    if unknown:
        raise OSError(f"Lattice database declares unknown change-ledger tables: {unknown}")
    if not tables:
        raise OSError("Lattice database has no installed change-ledger table contracts")
    return tables


def _collect_blob_refs(value: Any, refs: set[str]) -> None:
    if isinstance(value, str):
        for prefix in CURRENT_BLOB_PREFIXES:
            if value.startswith(prefix):
                digest = value.removeprefix(prefix)
                if len(digest) == 64 and all(character in "0123456789abcdef" for character in digest):
                    refs.add(digest)
                return
    elif isinstance(value, dict):
        for child in value.values():
            _collect_blob_refs(child, refs)
    elif isinstance(value, list):
        for child in value:
            _collect_blob_refs(child, refs)


def _referenced_blobs(connection: sqlite3.Connection, blobs: Path) -> set[str]:
    refs: set[str] = set()
    structured: set[str] = set()
    present_tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, column in (
        ("context_blocks", "content_sha256"),
        ("invocations", "compiled_prompt_sha256"),
        ("attempts", "output_sha256"),
        ("artifacts", "content_sha256"),
        ("workflow_commands", "result_sha256"),
        ("conversation_imports", "result_sha256"),
        ("workflow_transitions", "payload_sha256"),
        ("workflow_instances", "pack_definition_sha256"),
    ):
        if table not in present_tables:
            continue
        values = {str(row[0]) for row in connection.execute(f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL')}
        refs.update(values)
        if (table, column) in {("workflow_commands", "result_sha256"), ("conversation_imports", "result_sha256"), ("workflow_transitions", "payload_sha256")}:
            structured.update(values)
    for table, column in (
        ("invocations", "manifest_json"),
        ("artifacts", "generated_json"),
        ("context_blocks", "transformation_json"),
        ("workflow_commands", "receipt_json"),
        ("workflow_commands", "arguments_json"),
        ("attempts", "runtime_receipt_json"),
        ("runs", "declaration_json"),
        ("runs", "authority_json"),
        ("run_attempts", "request_json"),
        ("run_attempts", "executor_binding_json"),
        ("run_units", "declaration_json"),
        ("events", "payload_json"),
    ):
        if table not in present_tables:
            continue
        for row in connection.execute(f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'):
            _collect_blob_refs(json.loads(row[0]), refs)
    for row in connection.execute("SELECT result_ref FROM workflow_triggers WHERE result_ref LIKE 'lattice:blob/%' OR result_ref LIKE 'loom:blob/%'"):
        _collect_blob_refs(row[0], refs)
    if "run_artifacts" in present_tables:
        for row in connection.execute("SELECT storage_locator FROM run_artifacts WHERE state='present' AND storage_locator LIKE 'lattice:blob/%'"):
            _collect_blob_refs(row[0], refs)
        for row in connection.execute("SELECT record_json,regeneration_json FROM run_artifacts WHERE state='present'"):
            _collect_blob_refs(json.loads(row["record_json"]), refs)
            if row["regeneration_json"] is not None:
                _collect_blob_refs(json.loads(row["regeneration_json"]), refs)
    for digest in sorted(structured):
        path = blobs / digest[:2] / digest
        try:
            value = json.loads(path.read_bytes())
        except FileNotFoundError as error:
            raise OSError(f"missing blob required by Lattice state: {digest}") from error
        except json.JSONDecodeError as error:
            raise OSError(f"structured Lattice blob is not valid JSON: {digest}") from error
        _collect_blob_refs(value, refs)
    return refs


def _verify_blob(path: Path, expected: str) -> int:
    if path.is_symlink() or not path.is_file():
        raise OSError(f"missing blob required by Lattice state: {expected}")
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            byte_count += len(chunk)
    if digest.hexdigest() != expected:
        raise OSError(f"Lattice blob bytes do not match identity: {expected}")
    return byte_count


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise OSError(f"invalid model-run journal JSON: {path}") from error
    if not isinstance(value, dict):
        raise OSError(f"model-run journal JSON is not an object: {path}")
    return value


def _assess_model_runs(runs_root: Path) -> dict[str, int]:
    if not runs_root.exists():
        return {"run_count": 0, "terminal_count": 0, "journal_bytes": 0}
    if runs_root.is_symlink() or not runs_root.is_dir():
        raise OSError(f"model-run journal root is not a real directory: {runs_root}")
    run_count = 0
    terminal_count = 0
    journal_bytes = 0
    for run_root in sorted(runs_root.iterdir()):
        if run_root.is_symlink() or not run_root.is_dir():
            raise OSError(f"model-run journal entry is not a real directory: {run_root}")
        for child in run_root.rglob("*"):
            if child.is_symlink() or (not child.is_file() and not child.is_dir()):
                raise OSError(f"model-run journal contains an unsupported filesystem object: {child}")
            if child.is_file():
                journal_bytes += child.stat().st_size
        start = _read_json_object(run_root / "start.json")
        if start.get("event") != "start" or start.get("invocation_id") != run_root.name:
            raise OSError(f"model-run start record disagrees with its directory identity: {run_root}")
        stdout = run_root / str(start.get("stdout", "stdout.log"))
        stderr = run_root / str(start.get("stderr", "stderr.log"))
        progress = run_root / str(start.get("progress", "progress.jsonl"))
        if any(path.is_symlink() or not path.is_file() for path in (stdout, stderr, progress)):
            raise OSError(f"model-run journal is missing a required raw stream: {run_root}")
        for line_number, line in enumerate(progress.read_text(encoding="utf-8").splitlines(), start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise OSError(f"model-run progress journal has invalid JSON at {progress}:{line_number}") from error
            if not isinstance(event, dict) or event.get("channel") not in {"stdout", "stderr"} and not str(event.get("channel", "")).startswith("sidecar-"):
                raise OSError(f"model-run progress journal has an invalid event at {progress}:{line_number}")
        request = run_root / "request.json"
        checkpoint_path = run_root / "checkpoint.json"
        if request.exists() or checkpoint_path.exists():
            if request.is_symlink() or checkpoint_path.is_symlink() or not request.is_file() or not checkpoint_path.is_file():
                raise OSError(f"model-run checkpoint pair is incomplete: {run_root}")
            try:
                checkpoint = load_checkpoint(run_root)
            except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise OSError(f"model-run checkpoint binding failed: {run_root}") from error
            observed = checkpoint.get("observed")
            if not isinstance(observed, dict) or observed.get("stdout_bytes") != stdout.stat().st_size or observed.get("stderr_bytes") != stderr.stat().st_size:
                raise OSError(f"model-run checkpoint stream counts disagree with raw journals: {run_root}")
            continuation_path = run_root / "continuation.json"
            continuation = _read_json_object(continuation_path)
            binding = continuation.get("generated_object", {}).get("binding") if isinstance(continuation.get("generated_object"), dict) else None
            checkpoint_bytes = (json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            if not isinstance(binding, dict) or binding.get("artifact_id") != checkpoint.get("generated_object", {}).get("artifact_id") or binding.get("sha256") != hashlib.sha256(checkpoint_bytes).hexdigest():
                raise OSError(f"model-run continuation is not bound to the current checkpoint: {run_root}")
        terminal_path = run_root / "terminal.json"
        if terminal_path.exists():
            terminal = _read_json_object(terminal_path)
            if terminal.get("event") != "finish" or terminal.get("invocation_id") != run_root.name or terminal.get("stdout_bytes") != stdout.stat().st_size or terminal.get("stderr_bytes") != stderr.stat().st_size:
                raise OSError(f"model-run terminal receipt disagrees with raw journals: {run_root}")
            terminal_count += 1
        run_count += 1
    return {"run_count": run_count, "terminal_count": terminal_count, "journal_bytes": journal_bytes}


def assess_data_root(data_root: Path, *, database_path: Path | None = None) -> dict[str, Any]:
    data_root = data_root.expanduser().resolve(strict=True)
    database = database_path or data_root / "lattice.sqlite3"
    connection = _connect_read_only(database)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise OSError(f"SQLite integrity check failed: {integrity}")
        foreign_keys = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
        if foreign_keys:
            raise OSError(f"SQLite foreign-key check failed: {foreign_keys[:5]}")
        LatticeStore._verify_integrity_chain(connection)
        schema_version = int(connection.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0])
        tracked_tables = _tracked_tables(connection)
        if schema_version >= SCHEMA_VERSION and set(tracked_tables) != set(CHANGE_TABLES):
            raise OSError(f"current Lattice schema is missing change-ledger contracts: {sorted(set(CHANGE_TABLES) - set(tracked_tables))}")
        LatticeStore._verify_current_views_against_change_ledger(connection, tracked_tables)
        state_digest = _state_digest(connection, tracked_tables)
        refs = _referenced_blobs(connection, data_root / "blobs")
        blob_bytes = sum(_verify_blob(data_root / "blobs" / digest[:2] / digest, digest) for digest in sorted(refs))
        model_runs = _assess_model_runs(data_root / "model-runs")
        present_tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        durable_runs = {
            "total": int(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]) if "runs" in present_tables else 0,
            "active": int(connection.execute("SELECT COUNT(*) FROM runs WHERE state IN ('running','waiting','stopping')").fetchone()[0]) if "runs" in present_tables else 0,
            "units": int(connection.execute("SELECT COUNT(*) FROM run_units").fetchone()[0]) if "run_units" in present_tables else 0,
            "present_artifact_bytes": int(connection.execute("SELECT COALESCE(SUM(byte_count),0) FROM run_artifacts WHERE state='present'").fetchone()[0]) if "run_artifacts" in present_tables else 0,
        }
        active_work = {
            "attempts": int(connection.execute("SELECT COUNT(*) FROM attempts WHERE state IN ('leased','running')").fetchone()[0]),
            "runs": durable_runs["active"],
            "commands": int(connection.execute("SELECT COUNT(*) FROM workflow_commands WHERE state='running'").fetchone()[0]),
            "imports": int(connection.execute("SELECT COUNT(*) FROM conversation_imports WHERE state='running'").fetchone()[0]),
            "leases": int(connection.execute("SELECT COUNT(*) FROM leases WHERE state='active'").fetchone()[0]),
        }
    finally:
        connection.close()
    return {
        "data_root": str(data_root),
        "database": str(database),
        "schema_version": schema_version,
        "expected_schema_version": SCHEMA_VERSION,
        "state_digest": state_digest,
        "tracked_table_count": len(tracked_tables),
        "referenced_blob_count": len(refs),
        "referenced_blob_bytes": blob_bytes,
        "model_runs": model_runs,
        "durable_runs": durable_runs,
        "active_work": active_work,
    }


def latest_backup(backups: Path) -> Path | None:
    if not backups.is_dir():
        return None
    candidates = sorted((path for path in backups.iterdir() if path.is_dir() and (path / "snapshot.json").is_file()), reverse=True)
    return candidates[0] if candidates else None


def assess_backup(snapshot_root: Path) -> dict[str, Any]:
    manifest = verify_snapshot(snapshot_root)
    with materialized_snapshot_database(snapshot_root, manifest) as database:
        data = assess_data_root(snapshot_root, database_path=database)
    data["database"] = str(snapshot_root / manifest.get("database", {}).get("path", "lattice.sqlite3"))
    expected = manifest.get("state_digest")
    if expected is not None and data["state_digest"] != expected:
        raise OSError(f"snapshot logical state changed: expected {expected}, got {data['state_digest']}")
    return {"snapshot_root": str(snapshot_root.resolve()), "generated_at": manifest["generated_object"]["generated_at"], "artifact_id": manifest["generated_object"]["artifact_id"], **data}
