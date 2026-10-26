"""Read recovery CAS membership from a caller-owned SQLite snapshot.

Only known structured roots are decoded. A reference found inside one of those
objects retains its target bytes; it does not turn arbitrary payload into another
metadata document. Native proofs follow the journal fields read by
``native_recovery_proof._journal``, without interpreting the journals themselves.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


BLOB_PREFIXES = ("lattice:blob/", "loom:blob/")


def collect_blob_refs(value: Any, refs: set[str]) -> None:
    if isinstance(value, str):
        for prefix in BLOB_PREFIXES:
            if value.startswith(prefix):
                digest = value[len(prefix):]
                if len(digest) == 64 and all(char in "0123456789abcdef" for char in digest):
                    refs.add(digest)
                return
    elif isinstance(value, dict):
        for child in value.values():
            collect_blob_refs(child, refs)
    elif isinstance(value, list):
        for child in value:
            collect_blob_refs(child, refs)


def _structured_blob(blobs: Path, digest: str) -> Any:
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise OSError(f"invalid structured Lattice blob identity: {digest}")
    path = blobs / digest[:2] / digest
    if path.is_symlink() or not path.is_file():
        raise OSError(f"missing blob required by Lattice state: {digest}")
    try:
        raw = path.read_bytes()
    except FileNotFoundError as error:
        raise OSError(f"missing blob required by Lattice state: {digest}") from error
    # Hash the exact bytes we interpret, before trusting any dependency they name.
    if hashlib.sha256(raw).hexdigest() != digest:
        raise OSError(f"Lattice blob bytes do not match identity: {digest}")
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise OSError(f"structured Lattice blob is not valid JSON: {digest}") from error


def referenced_blob_digests(connection: sqlite3.Connection, blobs: Path) -> set[str]:
    """Enumerate recovery dependencies without opening, migrating or writing a DB.

    The caller owns connection lifetime and transaction consistency. Optional
    tables are absent in historical schemas. Both tuple and sqlite3.Row results
    work, including on a sealed, immutable read-only database image. Opaque leaf
    existence/hash validation remains the copying or diagnostic consumer's job.
    """
    refs: set[str] = set()
    structured: set[str] = set()
    native_proofs: set[str] = set()
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
        if table in {"workflow_commands", "conversation_imports", "workflow_transitions"}:
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
        ("conversation_rounds", "request_json"),
        ("conversation_executions", "recovery_json"),
        ("conversation_checkpoints", "content_json"),
        ("provider_qualifications", "receipt_json"),
    ):
        if table not in present_tables:
            continue
        for row in connection.execute(f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'):
            value = json.loads(row[0])
            collect_blob_refs(value, refs)
            if table == "provider_qualifications" and isinstance(value, dict) and value.get("contract_version") == "ConversationRecoveryQualification/1.0":
                evidence = value.get("evidence_refs", {})
                if isinstance(evidence, dict):
                    for boundary in ("completed", "interrupted"):
                        proof_ref = evidence.get(boundary)
                        if isinstance(proof_ref, str):
                            collect_blob_refs(proof_ref, native_proofs)
    if "workflow_triggers" in present_tables:
        for row in connection.execute("SELECT result_ref FROM workflow_triggers WHERE result_ref IS NOT NULL"):
            collect_blob_refs(row[0], refs)
    if "run_artifacts" in present_tables:
        for row in connection.execute("SELECT storage_locator,record_json,regeneration_json FROM run_artifacts WHERE state='present'"):
            collect_blob_refs(row[0], refs)
            collect_blob_refs(json.loads(row[1]), refs)
            if row[2] is not None:
                collect_blob_refs(json.loads(row[2]), refs)
    for digest in sorted(structured | native_proofs):
        value = _structured_blob(blobs, digest)
        if digest in structured:
            collect_blob_refs(value, refs)
        if digest in native_proofs:
            if not isinstance(value, dict):
                raise OSError(f"structured Lattice native proof is not an object: {digest}")
            for part in ("first", "continued"):
                journal = value.get(part, {})
                if not isinstance(journal, dict):
                    raise OSError(f"structured Lattice native proof journal is not an object: {digest}/{part}")
                for channel in ("request", "prompt", "stdout", "terminal", "checkpoint"):
                    leaf_ref = journal.get(channel)
                    if isinstance(leaf_ref, str):
                        collect_blob_refs(leaf_ref, refs)
    return refs
