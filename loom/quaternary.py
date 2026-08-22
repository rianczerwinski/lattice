"""Governed bridge to the quaternary system's own command and storage boundary."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from .canonical import canonical_bytes, sha256_bytes, stamp, strict_json_loads
from .context_modules import ContextModuleError, read_bounded_local_file


class QuaternaryBoundaryError(RuntimeError):
    pass


class QuaternaryOutcomeUnknown(QuaternaryBoundaryError):
    """The subprocess was dispatched but no authoritative terminal receipt returned."""


class _CapturedOutputLimit(RuntimeError):
    def __init__(self, stdout: bytes, stderr: bytes):
        super().__init__("quaternary command exceeded its bounded output allowance")
        self.stdout = stdout
        self.stderr = stderr


class QuaternaryAdapter:
    """Read typed state directly; mutate only through gatherctl's public commands."""

    READ_OPERATIONS = {"language-status", "peek", "next-language"}
    WRITE_OPERATIONS = {
        "prepare-language",
        "qualify-language",
        "retry-model-action",
        "seal-language-evaluation",
        "freeze-language-profile",
        "review-language-contributions",
        "freeze-language-query-plan",
        "preflight-language",
        "create-gather",
        "stop-language-preparation",
        "record-language-authority",
    }
    ARGUMENTS = {
        "language": "--language",
        "preparation_id": "--preparation-id",
        "effort": "--effort",
        "input": "--input",
        "evaluator": "--evaluator",
        "reviewer": "--reviewer",
        "checker": "--checker",
        "reason": "--reason",
        "last": "--last",
        "timeout": "--timeout",
        "action_id": "--action-id",
        "language_code": "--language-code",
        "locator": "--locator",
        "body_path": "--body",
        "media_type": "--media-type",
        "recorder": "--recorder",
    }
    MAX_CAPTURE_BYTES = 64 * 1024 * 1024
    MAX_INSPECT_ACTIONS = 100

    def __init__(self, database: Path, gatherctl: Path, *, runner: Callable[..., subprocess.CompletedProcess[str]] | None = None, allowed_input_roots: tuple[Path, ...] | None = None, blob_reader: Callable[[str], bytes] | None = None, blob_writer: Callable[[bytes], str] | None = None):
        self.database = database.resolve()
        self.gatherctl = gatherctl.resolve()
        self.runner = runner
        self.allowed_input_roots = tuple(path.resolve() for path in (allowed_input_roots or (self.database.parent,)))
        self.blob_reader = blob_reader
        self.blob_writer = blob_writer

    def inspect(self, selector: dict[str, Any]) -> dict[str, Any]:
        preparation_id = selector.get("preparation_id")
        language = selector.get("language", "de")
        if preparation_id is not None and (not isinstance(preparation_id, str) or not preparation_id.startswith("qep_")):
            raise QuaternaryBoundaryError("quaternary preparation_id is malformed")
        if not isinstance(language, str) or not language or len(language) > 35:
            raise QuaternaryBoundaryError("quaternary language selector is malformed")
        with self._readonly() as conn:
            conn.execute("BEGIN")
            schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if preparation_id:
                rows = conn.execute("SELECT * FROM qe_language_preparations WHERE preparation_id=? LIMIT 1", (preparation_id,)).fetchall()
                matching_preparations = len(rows)
            else:
                matching_preparations = int(conn.execute("SELECT count(*) FROM qe_language_preparations WHERE language_code=?", (language,)).fetchone()[0])
                rows = conn.execute("SELECT * FROM qe_language_preparations WHERE language_code=? ORDER BY generation DESC,preparation_id DESC LIMIT 1", (language,)).fetchall()
            preparations = []
            for row in rows:
                item = dict(row)
                prep_id = str(row["preparation_id"])
                raw_profile_id = row["profile_id"] if "profile_id" in row.keys() else None
                profile_id = str(raw_profile_id) if raw_profile_id is not None else None
                if "qe_preparation_actions" in tables:
                    actions = [dict(value) for value in conn.execute("SELECT action_id,action_kind,role,status,request_sha256,created_at,completed_at FROM qe_preparation_actions WHERE preparation_id=? ORDER BY created_at DESC,action_id DESC LIMIT ?", (prep_id, self.MAX_INSPECT_ACTIONS + 1))]
                    item["actions"] = actions[: self.MAX_INSPECT_ACTIONS]
                    item["action_coverage"] = {
                        "state": "partial" if len(actions) > self.MAX_INSPECT_ACTIONS else "complete",
                        "returned": min(len(actions), self.MAX_INSPECT_ACTIONS),
                        **self._rowset_receipt(conn, tables, "qe_preparation_actions", "t.preparation_id=?", (prep_id,)),
                    }
                else:
                    item["actions"] = None
                    item["action_coverage"] = None
                item["readiness"] = self._one(conn, "SELECT state,dependency_set_sha256,proof_limits_json,verified_at,verifier FROM qe_language_readiness WHERE preparation_id=?", (prep_id,)) if "qe_language_readiness" in tables else None
                item["profile_counts"] = None if profile_id is None else {
                    "lanes": self._count(conn, tables, "qe_language_profile_lanes", "SELECT count(*) FROM qe_language_profile_lanes WHERE profile_id=?", (profile_id,)),
                    "forms": self._count(conn, tables, "qe_language_profile_forms", "SELECT count(*) FROM qe_language_profile_forms WHERE profile_id=?", (profile_id,)),
                    "lexemes": self._count(conn, tables, "qe_language_lexemes", "SELECT count(*) FROM qe_language_lexemes WHERE profile_id=?", (profile_id,)),
                    "false_friends": self._count(conn, tables, "qe_language_false_friends", "SELECT count(*) FROM qe_language_false_friends WHERE profile_id=?", (profile_id,)),
                    "source_bindings": self._count(conn, tables, "qe_language_profile_source_bindings", "SELECT count(*) FROM qe_language_profile_source_bindings WHERE profile_id=?", (profile_id,)),
                }
                item["evidence_counts"] = {
                    "authority_receipts": self._count(conn, tables, "qe_authority_receipts", "SELECT count(*) FROM qe_authority_receipts WHERE preparation_id=?", (prep_id,)),
                    "authority_language_evidence": self._count(conn, tables, "qe_authority_language_evidence", "SELECT count(*) FROM qe_authority_language_evidence WHERE preparation_id=?", (prep_id,)),
                    "proposed_contributions": self._count(conn, tables, "qe_validated_model_contributions", "SELECT count(*) FROM qe_validated_model_contributions WHERE preparation_id=? AND validation_state='proposed'", (prep_id,)),
                    "validated_contributions": self._count(conn, tables, "qe_validated_model_contributions", "SELECT count(*) FROM qe_validated_model_contributions WHERE preparation_id=? AND validation_state='validated'", (prep_id,)),
                    "rejected_contributions": self._count(conn, tables, "qe_validated_model_contributions", "SELECT count(*) FROM qe_validated_model_contributions WHERE preparation_id=? AND validation_state='rejected'", (prep_id,)),
                }
                action_subquery = "SELECT action_id FROM qe_preparation_actions WHERE preparation_id=?"
                item["preflight_counts"] = {
                    "receipts": self._count(conn, tables, {"qe_preparation_actions", "qe_preflight_receipts"}, f"SELECT count(*) FROM qe_preflight_receipts WHERE action_id IN ({action_subquery})", (prep_id,)),
                    "results": self._count(conn, tables, {"qe_preparation_actions", "qe_preflight_results"}, f"SELECT count(*) FROM qe_preflight_results WHERE action_id IN ({action_subquery})", (prep_id,)),
                }
                item["readiness_evidence_counts"] = {
                    "checks": self._group_counts(conn, tables, "qe_language_readiness_checks", "SELECT result,count(*) FROM qe_language_readiness_checks WHERE preparation_id=? GROUP BY result", (prep_id,)),
                    "oracles": self._group_counts(conn, tables, "qe_language_readiness_oracles", "SELECT result,count(*) FROM qe_language_readiness_oracles WHERE preparation_id=? GROUP BY result", (prep_id,)),
                    "qualifications": self._group_counts(conn, tables, "qe_language_qualifications", "SELECT result,count(*) FROM qe_language_qualifications WHERE preparation_id=? GROUP BY result", (prep_id,)),
                }
                item["rowset_receipts"] = {
                    "authority_receipts": self._rowset_receipt(conn, tables, "qe_authority_receipts", "t.preparation_id=?", (prep_id,)),
                    "authority_language_evidence": self._rowset_receipt(conn, tables, "qe_authority_language_evidence", "t.preparation_id=?", (prep_id,)),
                    "validated_model_contributions": self._rowset_receipt(conn, tables, "qe_validated_model_contributions", "t.preparation_id=?", (prep_id,)),
                    "readiness_checks": self._rowset_receipt(conn, tables, "qe_language_readiness_checks", "t.preparation_id=?", (prep_id,)),
                    "readiness_oracles": self._rowset_receipt(conn, tables, "qe_language_readiness_oracles", "t.preparation_id=?", (prep_id,)),
                    "language_qualifications": self._rowset_receipt(conn, tables, "qe_language_qualifications", "t.preparation_id=?", (prep_id,)),
                    "preflight_receipts": self._rowset_receipt(conn, tables, "qe_preflight_receipts", "a.preparation_id=?", (prep_id,), join_sql="JOIN qe_preparation_actions a ON a.action_id=t.action_id", required={"qe_preflight_receipts", "qe_preparation_actions"}),
                    "preflight_results": self._rowset_receipt(conn, tables, "qe_preflight_results", "a.preparation_id=?", (prep_id,), join_sql="JOIN qe_preparation_actions a ON a.action_id=t.action_id", required={"qe_preflight_results", "qe_preparation_actions"}),
                    "profile_lanes": self._rowset_receipt(conn, tables, "qe_language_profile_lanes", "t.profile_id=?", (profile_id,)) if profile_id is not None else None,
                    "profile_forms": self._rowset_receipt(conn, tables, "qe_language_profile_forms", "t.profile_id=?", (profile_id,)) if profile_id is not None else None,
                    "profile_lexemes": self._rowset_receipt(conn, tables, "qe_language_lexemes", "t.profile_id=?", (profile_id,)) if profile_id is not None else None,
                    "profile_false_friends": self._rowset_receipt(conn, tables, "qe_language_false_friends", "t.profile_id=?", (profile_id,)) if profile_id is not None else None,
                    "profile_source_bindings": self._rowset_receipt(conn, tables, "qe_language_profile_source_bindings", "t.profile_id=?", (profile_id,)) if profile_id is not None else None,
                }
                preparations.append(item)
            required_tables = {"qe_language_preparations", "qe_preparation_actions", "qe_language_readiness", "qe_authority_receipts", "qe_authority_language_evidence", "qe_preflight_receipts", "qe_preflight_results", "qe_language_readiness_checks", "qe_language_readiness_oracles", "qe_language_qualifications", "qe_validated_model_contributions", "qe_language_profile_lanes", "qe_language_profile_forms", "qe_language_lexemes", "qe_language_false_friends", "qe_language_profile_source_bindings"}
            resolved_preparation_id = str(rows[0]["preparation_id"]) if rows else None
            state = {
                "language": language,
                "preparation_id": preparation_id,
                "resolved_preparation_id": resolved_preparation_id,
                "preparation_coverage": {
                    "state": "partial" if preparation_id is None and matching_preparations > len(rows) else "complete",
                    "matching": matching_preparations,
                    "returned": len(rows),
                    "selection": "exact" if preparation_id is not None else "latest-generation",
                },
                "preparations": preparations,
                "schema_missing": sorted(required_tables - tables),
            }
            state_sha256 = sha256_bytes(canonical_bytes(state))
            conn.commit()
        return {"kind": "quaternary-state-receipt", "source_ref": f"sqlite:{self.database}#loom-quaternary-state-v3", "query_contract": "loom-quaternary-state-v3", "sqlite_schema_version": schema_version, "state_sha256": state_sha256, **state, "observed_at": stamp()}

    def command(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if operation not in self.READ_OPERATIONS | self.WRITE_OPERATIONS:
            raise QuaternaryBoundaryError(f"unsupported quaternary operation: {operation}")
        undeclared = sorted(set(arguments) - set(self.ARGUMENTS))
        if undeclared:
            raise QuaternaryBoundaryError(f"undeclared argument: {undeclared[0]}")
        resolved_arguments = dict(arguments)
        execution_arguments = dict(arguments)
        temporary_inputs: list[Path] = []
        input_receipts: list[dict[str, Any]] = []
        bound_inputs: list[tuple[str, bytes]] = []
        for path_key in ("input", "body_path"):
            if path_key not in resolved_arguments:
                continue
            value = str(resolved_arguments[path_key])
            if value.startswith("loom:blob/"):
                digest = value.removeprefix("loom:blob/")
                if self.blob_reader is None:
                    raise QuaternaryBoundaryError(f"quaternary {path_key} has no Loom blob reader")
                try:
                    content = self.blob_reader(digest)
                except (FileNotFoundError, OSError, ValueError) as error:
                    raise QuaternaryBoundaryError(f"quaternary {path_key} blob is unavailable") from error
                if sha256_bytes(content) != digest:
                    raise QuaternaryBoundaryError(f"quaternary {path_key} blob identity is invalid")
                source_ref = value
            else:
                try:
                    input_path, content = read_bounded_local_file(value, allowed_roots=self.allowed_input_roots, max_bytes=64 * 1024 * 1024)
                except ContextModuleError as error:
                    raise QuaternaryBoundaryError(f"quaternary {path_key} escaped or failed its approved local file boundary: {error}") from error
                source_ref = f"file:{input_path}"
                resolved_arguments[path_key] = str(input_path)
            bound_inputs.append((path_key, content))
            input_receipts.append({"argument": path_key, "source_ref": source_ref, "content_sha256": sha256_bytes(content), "byte_count": len(content)})
        try:
            for path_key, content in bound_inputs:
                descriptor, snapshot_name = tempfile.mkstemp(prefix=f"loom-{path_key}-", suffix=".bound")
                snapshot = Path(snapshot_name)
                temporary_inputs.append(snapshot)
                try:
                    os.fchmod(descriptor, 0o600)
                    view = memoryview(content)
                    while view:
                        written = os.write(descriptor, view)
                        if written <= 0:
                            raise OSError("workflow input snapshot write made no progress")
                        view = view[written:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                execution_arguments[path_key] = str(snapshot)
        except BaseException:
            for snapshot in temporary_inputs:
                snapshot.unlink(missing_ok=True)
            raise
        try:
            command = [sys.executable, str(self.gatherctl), "--database", str(self.database), operation]
            for key in self.ARGUMENTS:
                if key in execution_arguments:
                    command.extend([self.ARGUMENTS[key], str(execution_arguments[key])])
            driver_before = self._driver_manifest()
            process_timeout = int(resolved_arguments.get("timeout", 3600)) + 120 if operation == "qualify-language" else 600
        except BaseException:
            for snapshot in temporary_inputs:
                snapshot.unlink(missing_ok=True)
            raise
        try:
            result = self._run_bounded(command, process_timeout) if self.runner is None else self.runner(command, text=True, capture_output=True, check=False, timeout=process_timeout)
        except subprocess.TimeoutExpired as error:
            driver_after = self._post_dispatch_driver_manifest(operation, driver_before)
            timeout_receipt = {"kind": "quaternary-command-timeout", "command_contract": "loom-quaternary-command-v2", "operation": operation, "arguments_sha256": sha256_bytes(canonical_bytes(resolved_arguments)), "argument_keys": sorted(resolved_arguments), "input_receipts": input_receipts, "driver_before": driver_before, "driver_after_sha256": driver_after["tree_sha256"], "timeout_seconds": process_timeout, "completed_at": stamp()}
            raise QuaternaryOutcomeUnknown(json.dumps(timeout_receipt, ensure_ascii=False)) from error
        except _CapturedOutputLimit as error:
            driver_after = self._post_dispatch_driver_manifest(operation, driver_before)
            limit_receipt = {"kind": "quaternary-command-output-limit", "command_contract": "loom-quaternary-command-v2", "operation": operation, "arguments_sha256": sha256_bytes(canonical_bytes(resolved_arguments)), "argument_keys": sorted(resolved_arguments), "input_receipts": input_receipts, "driver_before": driver_before, "driver_after_sha256": driver_after["tree_sha256"], "stdout_sha256": sha256_bytes(error.stdout), "stderr_sha256": sha256_bytes(error.stderr), "capture_limit_bytes": self.MAX_CAPTURE_BYTES, "completed_at": stamp()}
            failure = QuaternaryBoundaryError if operation in self.READ_OPERATIONS else QuaternaryOutcomeUnknown
            raise failure(json.dumps(limit_receipt, ensure_ascii=False)) from error
        finally:
            for snapshot in temporary_inputs:
                snapshot.unlink(missing_ok=True)
        driver_after = self._post_dispatch_driver_manifest(operation, driver_before)
        if driver_after["tree_sha256"] != driver_before["tree_sha256"]:
            drift_receipt = {"kind": "quaternary-command-driver-drift", "command_contract": "loom-quaternary-command-v2", "operation": operation, "arguments_sha256": sha256_bytes(canonical_bytes(resolved_arguments)), "driver_before": driver_before, "driver_after": driver_after, "exit_code": result.returncode, "stdout_sha256": sha256_bytes(result.stdout.encode()), "stderr_sha256": sha256_bytes(result.stderr.encode()), "completed_at": stamp()}
            raise QuaternaryOutcomeUnknown(json.dumps(drift_receipt, ensure_ascii=False))
        receipt = {"kind": "quaternary-command-receipt", "command_contract": "loom-quaternary-command-v2", "operation": operation, "arguments_sha256": sha256_bytes(canonical_bytes(resolved_arguments)), "argument_keys": sorted(resolved_arguments), "input_receipts": input_receipts, "driver_manifest": driver_before, "interpreter": {"executable": sys.executable, "version": sys.version}, "exit_code": result.returncode, "stdout_sha256": sha256_bytes(result.stdout.encode()), "stderr_sha256": sha256_bytes(result.stderr.encode()), "completed_at": stamp()}
        if result.returncode != 0:
            failure = QuaternaryBoundaryError if operation in self.READ_OPERATIONS else QuaternaryOutcomeUnknown
            raise failure(json.dumps({**receipt, "stderr": result.stderr[-4000:]}, ensure_ascii=False))
        try:
            output = strict_json_loads(result.stdout)
        except (json.JSONDecodeError, ValueError) as error:
            failure = QuaternaryBoundaryError if operation in self.READ_OPERATIONS else QuaternaryOutcomeUnknown
            raise failure(json.dumps({**receipt, "parse_error": "quaternary command did not return JSON"}, ensure_ascii=False)) from error
        if not isinstance(output, dict):
            failure = QuaternaryBoundaryError if operation in self.READ_OPERATIONS else QuaternaryOutcomeUnknown
            raise failure(json.dumps({**receipt, "parse_error": "quaternary command did not return a JSON object"}, ensure_ascii=False))
        return {"receipt": receipt, "output": output, "stdout": result.stdout, "stderr": result.stderr}

    def _post_dispatch_driver_manifest(self, operation: str, driver_before: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._driver_manifest()
        except BaseException as error:
            receipt = {
                "kind": "quaternary-command-postflight-unavailable",
                "command_contract": "loom-quaternary-command-v2",
                "operation": operation,
                "driver_before": driver_before,
                "attestation_error": f"{type(error).__name__}: {error}",
                "completed_at": stamp(),
            }
            failure = QuaternaryBoundaryError if operation in self.READ_OPERATIONS else QuaternaryOutcomeUnknown
            raise failure(json.dumps(receipt, ensure_ascii=False)) from error

    def _run_bounded(self, command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        """Drain both pipes concurrently and stop the process group at the exact output bound."""
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        if process.stdout is None or process.stderr is None:
            raise QuaternaryBoundaryError("quaternary command pipes were not created")
        streams = {process.stdout: bytearray(), process.stderr: bytearray()}
        selector = selectors.DefaultSelector()
        for stream in streams:
            selector.register(stream, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._stop_process_group(process)
                    raise subprocess.TimeoutExpired(command, timeout, output=bytes(streams[process.stdout]), stderr=bytes(streams[process.stderr]))
                for key, _mask in selector.select(min(remaining, 0.25)):
                    chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    admitted = self.MAX_CAPTURE_BYTES - sum(len(value) for value in streams.values())
                    streams[key.fileobj].extend(chunk[:admitted])
                    if len(chunk) > admitted:
                        self._stop_process_group(process)
                        raise _CapturedOutputLimit(bytes(streams[process.stdout]), bytes(streams[process.stderr]))
            return_code = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except BaseException:
            self._stop_process_group(process)
            raise
        finally:
            selector.close()
            process.stdout.close()
            process.stderr.close()
        try:
            stdout = bytes(streams[process.stdout]).decode("utf-8", errors="strict")
            stderr = bytes(streams[process.stderr]).decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            failure = QuaternaryBoundaryError if command[4] in self.READ_OPERATIONS else QuaternaryOutcomeUnknown
            raise failure("quaternary command output was not exact UTF-8") from error
        return subprocess.CompletedProcess(command, return_code, stdout=stdout, stderr=stderr)

    @staticmethod
    def _stop_process_group(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=1)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()

    def _readonly(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    def _driver_manifest(self) -> dict[str, Any]:
        """Bind the executable Python and migration sources that define gatherctl semantics."""
        roots = (self.gatherctl.parent, self.gatherctl.parent.parent / "migrations")
        paths = sorted({path.resolve() for root in roots if root.is_dir() for path in root.rglob("*") if path.is_file() and path.suffix in {".py", ".sql"}})
        if self.gatherctl not in paths:
            raise QuaternaryBoundaryError("quaternary gatherctl is outside its bound driver tree")
        entries = []
        for path in paths:
            source, content = read_bounded_local_file(str(path), allowed_roots=(self.gatherctl.parent.parent,), max_bytes=8 * 1024 * 1024)
            digest = sha256_bytes(content)
            if self.blob_writer is not None and self.blob_writer(content) != digest:
                raise QuaternaryBoundaryError("quaternary driver source changed while entering the Loom blob store")
            entries.append({"path": str(source.relative_to(self.gatherctl.parent.parent)), "content_sha256": digest, "byte_count": len(content), "source_ref": f"loom:blob/{digest}" if self.blob_writer is not None else f"file:{source}"})
        identity = {"kind": "quaternary-driver-manifest-v1", "entries": entries}
        return {**identity, "tree_sha256": sha256_bytes(canonical_bytes(identity))}

    @staticmethod
    def _one(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _count(conn: sqlite3.Connection, tables: set[str], required: str | set[str], sql: str, params: tuple[Any, ...]) -> int | None:
        required_tables = {required} if isinstance(required, str) else required
        if not required_tables.issubset(tables):
            return None
        return int(conn.execute(sql, params).fetchone()[0])

    @staticmethod
    def _group_counts(conn: sqlite3.Connection, tables: set[str], required: str, sql: str, params: tuple[Any, ...]) -> dict[str, int] | None:
        if required not in tables:
            return None
        return {str(row[0]): int(row[1]) for row in conn.execute(sql, params)}

    @staticmethod
    def _rowset_receipt(
        conn: sqlite3.Connection,
        tables: set[str],
        table: str,
        where_sql: str,
        params: tuple[Any, ...],
        *,
        join_sql: str = "",
        required: set[str] | None = None,
    ) -> dict[str, Any] | None:
        """Hash a scoped row multiset in semantic column order without materializing it."""
        required = required or {table}
        if not required.issubset(tables):
            return None
        columns = [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')]
        if not columns:
            return None
        quoted = [name.replace('"', '""') for name in columns]
        order = ",".join(f't."{name}"' for name in quoted)
        query = f'SELECT t.* FROM "{table}" t {join_sql} WHERE {where_sql} ORDER BY {order}'
        digest = hashlib.sha256()
        count = 0
        for row in conn.execute(query, params):
            material = {
                key: {"sqlite_type": "blob", "byte_count": len(value), "sha256": sha256_bytes(value)} if isinstance(value, bytes) else value
                for key, value in dict(row).items()
            }
            encoded = canonical_bytes(material)
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            count += 1
        return {"row_count": count, "rows_sha256": digest.hexdigest()}
