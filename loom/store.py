"""Append-only Loom ledger and its transactionally derived current views."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .blob_store import BlobStore
from .canonical import canonical_bytes, opaque_id, sha256_bytes, stamp
from .config import LoomConfig
from .contracts import (
    AttemptState,
    BlockStatus,
    ContextBlockCreate,
    DraftCreate,
    DraftPatch,
    DraftState,
    EdgeRelation,
    InvocationState,
    ProjectCreate,
    RoleCreate,
    RoleSessionCreate,
    SliceCreate,
)


DDL = """
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_migrations(
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS events(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    project_id TEXT,
    event_type TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
    occurred_at TEXT NOT NULL
) STRICT;

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'Loom events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'Loom events are append-only'); END;

CREATE TABLE IF NOT EXISTS ledger_changes(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    change_id TEXT NOT NULL UNIQUE,
    table_name TEXT NOT NULL,
    operation TEXT NOT NULL CHECK(operation IN ('baseline','insert','update','delete')),
    primary_key_json TEXT NOT NULL CHECK(json_valid(primary_key_json)),
    row_json TEXT CHECK(row_json IS NULL OR json_valid(row_json)),
    occurred_at TEXT NOT NULL
) STRICT;

CREATE TRIGGER IF NOT EXISTS ledger_changes_no_update BEFORE UPDATE ON ledger_changes
BEGIN SELECT RAISE(ABORT, 'Loom change ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_changes_no_delete BEFORE DELETE ON ledger_changes
BEGIN SELECT RAISE(ABORT, 'Loom change ledger is append-only'); END;

CREATE TABLE IF NOT EXISTS projects(
    project_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    purpose TEXT NOT NULL,
    workflow_pack TEXT,
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS roles(
    role_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    name TEXT NOT NULL,
    instructions_block_id TEXT NOT NULL,
    incentives_json TEXT NOT NULL CHECK(json_valid(incentives_json)),
    authority_limits_json TEXT NOT NULL CHECK(json_valid(authority_limits_json)),
    created_at TEXT NOT NULL,
    UNIQUE(project_id, name)
) STRICT;

CREATE TABLE IF NOT EXISTS role_sessions(
    role_session_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    role_id TEXT NOT NULL REFERENCES roles(role_id),
    label TEXT NOT NULL,
    route_revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS context_blocks(
    block_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    semantic_type TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
    byte_count INTEGER NOT NULL CHECK(byte_count>=0),
    initial_status TEXT NOT NULL,
    producing_invocation_id TEXT,
    source_ref TEXT,
    source_sha256 TEXT,
    authority TEXT NOT NULL,
    evidence_level TEXT NOT NULL,
    sensitivity TEXT NOT NULL,
    transformation_json TEXT CHECK(transformation_json IS NULL OR json_valid(transformation_json)),
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS block_status(
    block_id TEXT PRIMARY KEY REFERENCES context_blocks(block_id),
    status TEXT NOT NULL,
    revision INTEGER NOT NULL,
    event_id TEXT NOT NULL REFERENCES events(event_id)
) STRICT;

CREATE TABLE IF NOT EXISTS drafts(
    draft_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    role_session_id TEXT NOT NULL REFERENCES role_sessions(role_session_id),
    prompt_block_id TEXT NOT NULL REFERENCES context_blocks(block_id),
    route_json TEXT NOT NULL CHECK(json_valid(route_json)),
    selections_json TEXT NOT NULL CHECK(json_valid(selections_json)),
    parents_json TEXT NOT NULL CHECK(json_valid(parents_json)),
    relation TEXT NOT NULL,
    response_schema_json TEXT NOT NULL CHECK(json_valid(response_schema_json)),
    revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL,
    invocation_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS invocations(
    invocation_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    role_session_id TEXT NOT NULL REFERENCES role_sessions(role_session_id),
    draft_id TEXT REFERENCES drafts(draft_id),
    manifest_artifact_id TEXT NOT NULL UNIQUE,
    manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64),
    manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)),
    compiled_prompt_sha256 TEXT NOT NULL CHECK(length(compiled_prompt_sha256)=64),
    route_json TEXT NOT NULL CHECK(json_valid(route_json)),
    response_schema_json TEXT NOT NULL CHECK(json_valid(response_schema_json)),
    state TEXT NOT NULL,
    output_block_id TEXT REFERENCES context_blocks(block_id),
    terminal_kind TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS edges(
    edge_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    relation TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    target_revision TEXT NOT NULL,
    metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)),
    created_at TEXT NOT NULL,
    UNIQUE(relation, source_type, source_id, source_revision, target_type, target_id, target_revision)
) STRICT;

CREATE TABLE IF NOT EXISTS attempts(
    attempt_id TEXT PRIMARY KEY,
    invocation_id TEXT NOT NULL REFERENCES invocations(invocation_id),
    attempt_number INTEGER NOT NULL CHECK(attempt_number>0),
    idempotency_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    owner_boot_id TEXT,
    lease_id TEXT,
    journal_path TEXT,
    output_sha256 TEXT,
    failure_kind TEXT,
    error_text TEXT,
    runtime_receipt_json TEXT CHECK(runtime_receipt_json IS NULL OR json_valid(runtime_receipt_json)),
    started_at TEXT NOT NULL,
    heartbeat_at TEXT,
    completed_at TEXT,
    UNIQUE(invocation_id, attempt_number)
) STRICT;

CREATE TABLE IF NOT EXISTS leases(
    lease_id TEXT PRIMARY KEY,
    invocation_id TEXT NOT NULL REFERENCES invocations(invocation_id),
    attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(attempt_id),
    owner_boot_id TEXT NOT NULL,
    state TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    released_at TEXT
) STRICT;

CREATE UNIQUE INDEX IF NOT EXISTS one_active_invocation_lease
ON leases(invocation_id) WHERE state='active';

CREATE TABLE IF NOT EXISTS slices(
    slice_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    name TEXT NOT NULL,
    query_json TEXT NOT NULL CHECK(json_valid(query_json)),
    external_ref TEXT UNIQUE,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS artifacts(
    artifact_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    artifact_kind TEXT NOT NULL,
    generated_json TEXT NOT NULL CHECK(json_valid(generated_json)),
    content_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS workflow_commands(
    command_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    pack_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    arguments_json TEXT NOT NULL CHECK(json_valid(arguments_json)),
    idempotency_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    owner_boot_id TEXT NOT NULL,
    result_sha256 TEXT,
    receipt_json TEXT CHECK(receipt_json IS NULL OR json_valid(receipt_json)),
    error_text TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT
) STRICT;

CREATE UNIQUE INDEX IF NOT EXISTS one_running_workflow_command
ON workflow_commands(project_id) WHERE state='running';

CREATE TABLE IF NOT EXISTS workflow_instances(
    project_id TEXT PRIMARY KEY REFERENCES projects(project_id),
    pack_id TEXT NOT NULL,
    state TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision>0),
    automatic_grants_json TEXT NOT NULL CHECK(json_valid(automatic_grants_json)),
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS workflow_transitions(
    transition_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    pack_id TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    revision INTEGER NOT NULL,
    evidence_block_ids_json TEXT NOT NULL CHECK(json_valid(evidence_block_ids_json)),
    payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, revision)
) STRICT;

CREATE TABLE IF NOT EXISTS workflow_triggers(
    trigger_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    trigger_name TEXT NOT NULL,
    required_action TEXT NOT NULL,
    state TEXT NOT NULL,
    source_transition_id TEXT NOT NULL REFERENCES workflow_transitions(transition_id),
    result_ref TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS role_context_defaults(
    role_id TEXT NOT NULL REFERENCES roles(role_id),
    block_id TEXT NOT NULL REFERENCES context_blocks(block_id),
    module_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    PRIMARY KEY(role_id, block_id)
) STRICT;

CREATE INDEX IF NOT EXISTS events_project_seq ON events(project_id, seq);
CREATE INDEX IF NOT EXISTS invocations_project_created ON invocations(project_id, created_at);
CREATE INDEX IF NOT EXISTS edges_source ON edges(source_type, source_id);
CREATE INDEX IF NOT EXISTS edges_target ON edges(target_type, target_id);
"""


CHANGE_TABLES = (
    "projects", "context_blocks", "block_status", "roles", "role_sessions", "drafts", "invocations", "edges",
    "attempts", "leases", "slices", "artifacts", "workflow_commands", "workflow_instances", "workflow_transitions",
    "workflow_triggers", "role_context_defaults",
)


class LoomConflict(RuntimeError):
    pass


class LoomNotFound(KeyError):
    pass


class LoomStore:
    def __init__(self, config: LoomConfig) -> None:
        self.config = config
        self.config.data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.config.data_root, 0o700)
        self.blobs = BlobStore(config.blobs)
        self._write_lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript(DDL)
            attempt_columns = {row[1] for row in conn.execute("PRAGMA table_info(attempts)")}
            if "runtime_receipt_json" not in attempt_columns:
                conn.execute("ALTER TABLE attempts ADD COLUMN runtime_receipt_json TEXT CHECK(runtime_receipt_json IS NULL OR json_valid(runtime_receipt_json))")
            self._install_change_triggers(conn)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(1,?)",
                (stamp(),),
            )
            self._backfill_change_ledger(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.config.database, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.create_function("loom_stamp", 0, stamp)
        conn.create_function("loom_replaying", 0, lambda: 0)
        return conn

    @staticmethod
    def _table_contract(conn: sqlite3.Connection, table: str) -> tuple[list[str], list[str]]:
        columns = [dict(row) for row in conn.execute(f'PRAGMA table_info("{table}")')]
        names = [row["name"] for row in columns]
        primary = [row["name"] for row in sorted(columns, key=lambda item: item["pk"]) if row["pk"]]
        if not primary:
            raise RuntimeError(f"change-ledger table has no primary key: {table}")
        return names, primary

    @classmethod
    def _install_change_triggers(cls, conn: sqlite3.Connection) -> None:
        for table in CHANGE_TABLES:
            columns, primary = cls._table_contract(conn, table)
            for operation, ref in (("insert", "NEW"), ("update", "NEW"), ("delete", "OLD")):
                pk_args = ",".join(f"'{name}',{ref}.\"{name}\"" for name in primary)
                row_expr = "NULL" if operation == "delete" else "json_object(" + ",".join(f"'{name}',{ref}.\"{name}\"" for name in columns) + ")"
                conn.execute(f'''CREATE TRIGGER IF NOT EXISTS "ledger_{table}_{operation}" AFTER {operation.upper()} ON "{table}" WHEN loom_replaying()=0 BEGIN
                    INSERT INTO ledger_changes(change_id,table_name,operation,primary_key_json,row_json,occurred_at)
                    VALUES(lower(hex(randomblob(16))),'{table}','{operation}',json_object({pk_args}),{row_expr},loom_stamp());
                END''')

    @classmethod
    def _backfill_change_ledger(cls, conn: sqlite3.Connection) -> None:
        if conn.execute("SELECT 1 FROM ledger_changes LIMIT 1").fetchone() is not None:
            return
        for table in CHANGE_TABLES:
            _columns, primary = cls._table_contract(conn, table)
            for row in conn.execute(f'SELECT * FROM "{table}"'):
                value = dict(row)
                conn.execute("INSERT INTO ledger_changes(change_id,table_name,operation,primary_key_json,row_json,occurred_at) VALUES(?,?,'baseline',?,?,?)", (opaque_id("change"), table, canonical_bytes({key: value[key] for key in primary}).decode(), canonical_bytes(value).decode(), stamp()))

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()

    def _event(
        self,
        conn: sqlite3.Connection,
        *,
        project_id: str | None,
        event_type: str,
        subject_type: str,
        subject_id: str,
        payload: dict[str, Any],
    ) -> str:
        event_id = opaque_id("evt")
        raw = canonical_bytes(payload)
        conn.execute(
            """INSERT INTO events(event_id,project_id,event_type,subject_type,subject_id,payload_json,payload_sha256,occurred_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (event_id, project_id, event_type, subject_type, subject_id, raw.decode(), sha256_bytes(raw), stamp()),
        )
        return event_id

    @staticmethod
    def _row(row: sqlite3.Row | None, label: str) -> dict[str, Any]:
        if row is None:
            raise LoomNotFound(label)
        return dict(row)

    def create_project(self, value: ProjectCreate) -> dict[str, Any]:
        project_id = opaque_id("project")
        now = stamp()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO projects(project_id,title,purpose,workflow_pack,created_at) VALUES(?,?,?,?,?)",
                (project_id, value.title, value.purpose, value.workflow_pack, now),
            )
            self._event(conn, project_id=project_id, event_type="project.created", subject_type="project", subject_id=project_id, payload=value.model_dump(mode="json"))
        return self.get_project(project_id)

    def get_project(self, project_id: str) -> dict[str, Any]:
        blackboard_id: str | None = None
        with self._connect() as conn:
            project = self._row(conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone(), project_id)
            project["roles"] = [dict(row) for row in conn.execute("SELECT * FROM roles WHERE project_id=? ORDER BY created_at", (project_id,))]
            project["sessions"] = [dict(row) for row in conn.execute("SELECT * FROM role_sessions WHERE project_id=? ORDER BY created_at", (project_id,))]
            workflow = conn.execute("SELECT * FROM workflow_instances WHERE project_id=?", (project_id,)).fetchone()
            project["workflow"] = dict(workflow) if workflow else None
            if project["workflow"]:
                project["workflow"]["automatic_grants"] = json.loads(project["workflow"].pop("automatic_grants_json"))
                project["workflow"]["triggers"] = [dict(row) for row in conn.execute("SELECT * FROM workflow_triggers WHERE project_id=? ORDER BY created_at", (project_id,))]
                blackboard = conn.execute("SELECT block_id FROM context_blocks WHERE project_id=? AND semantic_type='workflow-blackboard-snapshot' ORDER BY created_at DESC LIMIT 1", (project_id,)).fetchone()
                blackboard_id = blackboard["block_id"] if blackboard else None
            project["trailheads"] = [dict(row) for row in conn.execute(
                """SELECT i.* FROM invocations i
                   WHERE i.project_id=? AND NOT EXISTS(
                     SELECT 1 FROM edges e WHERE e.relation='inherited_context_from'
                     AND e.target_type='invocation' AND e.target_id=i.invocation_id)
                   ORDER BY i.created_at DESC LIMIT 3""", (project_id,))]
        if project["workflow"] and blackboard_id:
            project["workflow"]["blackboard"] = self.get_block(blackboard_id)
        return project

    def list_projects(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM projects ORDER BY created_at DESC")]

    def create_workflow_project_atomic(
        self,
        *,
        title: str,
        purpose: str,
        workflow_pack: str,
        initial_state: str,
        role_specs: list[dict[str, Any]],
        blackboard_content: bytes,
        blackboard_source_ref: str,
        slice_external_ref: str | None,
        context_modules: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Install a workflow project and its first authoritative snapshot as one ledger transition."""
        project_id = opaque_id("project")
        now = stamp()
        blackboard_id = opaque_id("block")
        blackboard_sha = self.blobs.put(blackboard_content)
        prepared_roles = []
        for spec in role_specs:
            instructions = spec["instructions"].encode("utf-8")
            prepared_roles.append((spec, opaque_id("role"), opaque_id("session"), opaque_id("block"), self.blobs.put(instructions), instructions))
        prepared_modules = []
        for module in context_modules or []:
            content = module["content"]
            prepared_modules.append((module, opaque_id("block"), self.blobs.put(content)))
        slice_id = opaque_id("slice") if slice_external_ref else None
        with self.transaction() as conn:
            conn.execute("INSERT INTO projects(project_id,title,purpose,workflow_pack,created_at) VALUES(?,?,?,?,?)", (project_id, title, purpose, workflow_pack, now))
            self._event(conn, project_id=project_id, event_type="project.created", subject_type="project", subject_id=project_id, payload={"title": title, "purpose": purpose, "workflow_pack": workflow_pack})
            conn.execute("INSERT INTO workflow_instances(project_id,pack_id,state,revision,automatic_grants_json,updated_at) VALUES(?,?,?,1,'[]',?)", (project_id, workflow_pack, initial_state, now))
            self._event(conn, project_id=project_id, event_type="workflow.instance-created", subject_type="workflow-instance", subject_id=project_id, payload={"pack_id": workflow_pack, "state": initial_state, "revision": 1, "automatic_grants": []})
            for spec, role_id, session_id, instruction_id, instruction_sha, instructions in prepared_roles:
                conn.execute("INSERT INTO context_blocks(block_id,project_id,semantic_type,content_sha256,byte_count,initial_status,authority,evidence_level,sensitivity,created_at) VALUES(?,?,?,?,?,'active','workflow-pack','governing-instruction','private',?)", (instruction_id, project_id, "role-instruction", instruction_sha, len(instructions), now))
                instruction_event = self._event(conn, project_id=project_id, event_type="context-block.created", subject_type="context-block", subject_id=instruction_id, payload={"sha256": instruction_sha, "semantic_type": "role-instruction", "status": "active"})
                conn.execute("INSERT INTO block_status(block_id,status,revision,event_id) VALUES(?,'active',1,?)", (instruction_id, instruction_event))
                conn.execute("INSERT INTO roles(role_id,project_id,name,instructions_block_id,incentives_json,authority_limits_json,created_at) VALUES(?,?,?,?,?,?,?)", (role_id, project_id, spec["name"], instruction_id, "[]", canonical_bytes([spec["authority"]]).decode(), now))
                self._event(conn, project_id=project_id, event_type="role.created", subject_type="role", subject_id=role_id, payload={"name": spec["name"], "instructions_block_id": instruction_id})
                conn.execute("INSERT INTO role_sessions(role_session_id,project_id,role_id,label,created_at) VALUES(?,?,?,?,?)", (session_id, project_id, role_id, spec["name"], now))
                self._event(conn, project_id=project_id, event_type="role-session.created", subject_type="role-session", subject_id=session_id, payload={"project_id": project_id, "role_id": role_id, "label": spec["name"]})
            for module, block_id, content_sha in prepared_modules:
                definition = module["definition"]
                receipt = module["receipt"]
                content = module["content"]
                conn.execute("""INSERT INTO context_blocks(block_id,project_id,semantic_type,content_sha256,byte_count,initial_status,source_ref,source_sha256,authority,evidence_level,sensitivity,transformation_json,created_at)
                             VALUES(?,?,?,?,?,'active',?,?,?,'module-receipt','private',?,?)""", (block_id, project_id, f"context-module:{definition['module_id']}", content_sha, len(content), receipt["source_ref"], receipt["content_sha256"], definition["module_id"], canonical_bytes(receipt).decode(), now))
                module_event = self._event(conn, project_id=project_id, event_type="context-block.created", subject_type="context-block", subject_id=block_id, payload={"sha256": content_sha, "semantic_type": f"context-module:{definition['module_id']}", "status": "active", "mode": definition["mode"]})
                conn.execute("INSERT INTO block_status(block_id,status,revision,event_id) VALUES(?,'active',1,?)", (block_id, module_event))
                if definition["mode"] in {"role_default", "workflow_required"}:
                    for _spec, role_id, *_rest in prepared_roles:
                        conn.execute("INSERT INTO role_context_defaults(role_id,block_id,module_id,mode,active) VALUES(?,?,?,?,1)", (role_id, block_id, definition["module_id"], definition["mode"]))
            conn.execute("""INSERT INTO context_blocks(block_id,project_id,semantic_type,content_sha256,byte_count,initial_status,source_ref,source_sha256,authority,evidence_level,sensitivity,created_at)
                         VALUES(?,?,?,?,?,'active',?,?,?,'authoritative-read','private',?)""", (blackboard_id, project_id, "workflow-blackboard-snapshot", blackboard_sha, len(blackboard_content), blackboard_source_ref, blackboard_sha, workflow_pack, now))
            blackboard_event = self._event(conn, project_id=project_id, event_type="context-block.created", subject_type="context-block", subject_id=blackboard_id, payload={"sha256": blackboard_sha, "semantic_type": "workflow-blackboard-snapshot", "status": "active"})
            conn.execute("INSERT INTO block_status(block_id,status,revision,event_id) VALUES(?,'active',1,?)", (blackboard_id, blackboard_event))
            if slice_external_ref and slice_id:
                self._insert_edge(conn, project_id=project_id, relation=EdgeRelation.concerns, source_type="context-block", source_id=blackboard_id, source_revision=blackboard_sha, target_type="external-concern", target_id=slice_external_ref, target_revision=slice_external_ref, metadata={"workflow_pack": workflow_pack})
                query = {"concern_ref": slice_external_ref, "object_types": ["invocation", "context-block"]}
                conn.execute("INSERT INTO slices(slice_id,project_id,name,query_json,external_ref,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (slice_id, project_id, title, canonical_bytes(query).decode(), slice_external_ref, now, now))
                self._event(conn, project_id=project_id, event_type="slice.created", subject_type="slice", subject_id=slice_id, payload={"project_id": project_id, "name": title, "query": query, "external_ref": slice_external_ref})
        return {"project": self.get_project(project_id), "blackboard": self.get_block(blackboard_id, include_content=False), "slice": self.get_slice(slice_id) if slice_id else None}

    def list_role_context_defaults(self, role_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM role_context_defaults WHERE role_id=? AND active=1 ORDER BY module_id,block_id", (role_id,))]

    def remove_role_context_default(self, role_id: str, block_id: str) -> None:
        with self.transaction() as conn:
            row = self._row(conn.execute("SELECT r.project_id,d.mode,d.active FROM role_context_defaults d JOIN roles r USING(role_id) WHERE d.role_id=? AND d.block_id=?", (role_id, block_id)).fetchone(), block_id)
            if row["mode"] != "role_default":
                raise LoomConflict("workflow-required context cannot be removed")
            if not row["active"]:
                return
            conn.execute("UPDATE role_context_defaults SET active=0 WHERE role_id=? AND block_id=?", (role_id, block_id))
            self._event(conn, project_id=row["project_id"], event_type="role-context.removed", subject_type="context-block", subject_id=block_id, payload={"role_id": role_id})

    def create_context_block(self, value: ContextBlockCreate) -> dict[str, Any]:
        block_id = opaque_id("block")
        content = value.content.encode("utf-8")
        digest = self.blobs.put(content)
        now = stamp()
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM projects WHERE project_id=?", (value.project_id,)).fetchone() is None:
                raise LoomNotFound(value.project_id)
            conn.execute(
                """INSERT INTO context_blocks(block_id,project_id,semantic_type,content_sha256,byte_count,initial_status,
                   producing_invocation_id,source_ref,source_sha256,authority,evidence_level,sensitivity,transformation_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (block_id, value.project_id, value.semantic_type, digest, len(content), value.status.value,
                 value.producing_invocation_id, value.source_ref, value.source_sha256, value.authority,
                 value.evidence_level, value.sensitivity,
                 canonical_bytes(value.transformation).decode() if value.transformation is not None else None, now),
            )
            event_id = self._event(conn, project_id=value.project_id, event_type="context-block.created", subject_type="context-block", subject_id=block_id, payload={"sha256": digest, "semantic_type": value.semantic_type, "status": value.status.value})
            conn.execute("INSERT INTO block_status(block_id,status,revision,event_id) VALUES(?,?,1,?)", (block_id, value.status.value, event_id))
        return self.get_block(block_id, include_content=False)

    def get_block(self, block_id: str, *, include_content: bool = True) -> dict[str, Any]:
        with self._connect() as conn:
            row = self._row(conn.execute(
                """SELECT b.*,s.status,s.revision AS status_revision FROM context_blocks b
                   JOIN block_status s USING(block_id) WHERE block_id=?""", (block_id,)).fetchone(), block_id)
        if include_content:
            row["content"] = self.blobs.get(row["content_sha256"]).decode("utf-8")
        return row

    def set_block_status(self, block_id: str, status: BlockStatus) -> dict[str, Any]:
        with self.transaction() as conn:
            row = self._row(conn.execute("SELECT b.project_id,s.revision FROM context_blocks b JOIN block_status s USING(block_id) WHERE block_id=?", (block_id,)).fetchone(), block_id)
            revision = int(row["revision"]) + 1
            event_id = self._event(conn, project_id=row["project_id"], event_type="context-block.status", subject_type="context-block", subject_id=block_id, payload={"status": status.value, "revision": revision})
            conn.execute("UPDATE block_status SET status=?,revision=?,event_id=? WHERE block_id=?", (status.value, revision, event_id, block_id))
        return self.get_block(block_id, include_content=False)

    def create_role(self, project_id: str, value: RoleCreate) -> dict[str, Any]:
        instructions = value.instructions.encode("utf-8")
        instruction_sha = self.blobs.put(instructions)
        instruction_id = opaque_id("block")
        role_id = opaque_id("role")
        now = stamp()
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone() is None:
                raise LoomNotFound(project_id)
            conn.execute(
                """INSERT INTO context_blocks(block_id,project_id,semantic_type,content_sha256,byte_count,initial_status,
                   authority,evidence_level,sensitivity,created_at) VALUES(?,?,?,?,?,'active','workflow-or-operator','governing-instruction','private',?)""",
                (instruction_id, project_id, "role-instruction", instruction_sha, len(instructions), now),
            )
            instruction_event = self._event(conn, project_id=project_id, event_type="context-block.created", subject_type="context-block", subject_id=instruction_id, payload={"sha256": instruction_sha, "semantic_type": "role-instruction", "status": "active"})
            conn.execute("INSERT INTO block_status(block_id,status,revision,event_id) VALUES(?,'active',1,?)", (instruction_id, instruction_event))
            conn.execute(
                "INSERT INTO roles(role_id,project_id,name,instructions_block_id,incentives_json,authority_limits_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (role_id, project_id, value.name, instruction_id, canonical_bytes(value.incentives).decode(), canonical_bytes(value.authority_limits).decode(), now),
            )
            self._event(conn, project_id=project_id, event_type="role.created", subject_type="role", subject_id=role_id, payload={"name": value.name, "instructions_block_id": instruction_id})
        return self.get_role(role_id)

    def get_role(self, role_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            return self._row(conn.execute("SELECT * FROM roles WHERE role_id=?", (role_id,)).fetchone(), role_id)

    def find_role_by_name(self, project_id: str, name: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM roles WHERE project_id=? AND name=?", (project_id, name)).fetchone()
        return dict(row) if row else None

    def create_role_session(self, value: RoleSessionCreate) -> dict[str, Any]:
        session_id = opaque_id("session")
        with self.transaction() as conn:
            role = self._row(conn.execute("SELECT project_id FROM roles WHERE role_id=?", (value.role_id,)).fetchone(), value.role_id)
            if role["project_id"] != value.project_id:
                raise LoomConflict("role does not belong to project")
            conn.execute("INSERT INTO role_sessions(role_session_id,project_id,role_id,label,created_at) VALUES(?,?,?,?,?)", (session_id, value.project_id, value.role_id, value.label, stamp()))
            self._event(conn, project_id=value.project_id, event_type="role-session.created", subject_type="role-session", subject_id=session_id, payload=value.model_dump(mode="json"))
        return self.get_role_session(session_id)

    def get_role_session(self, session_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            return self._row(conn.execute("SELECT * FROM role_sessions WHERE role_session_id=?", (session_id,)).fetchone(), session_id)

    def create_draft(self, value: DraftCreate) -> dict[str, Any]:
        session = self.get_role_session(value.role_session_id)
        if session["project_id"] != value.project_id:
            raise LoomConflict("role session does not belong to project")
        for parent_id in value.parent_invocation_ids:
            parent = self.get_invocation(parent_id)
            if parent["project_id"] != value.project_id:
                raise LoomConflict("parent invocation does not belong to project")
        prompt_block = self.create_context_block(ContextBlockCreate(project_id=value.project_id, semantic_type="user-prompt", content=value.prompt, evidence_level="operator-intent"))
        draft_id = opaque_id("draft")
        now = stamp()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO drafts(draft_id,project_id,role_session_id,prompt_block_id,route_json,selections_json,parents_json,relation,response_schema_json,revision,state,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?)""",
                (draft_id, value.project_id, value.role_session_id, prompt_block["block_id"],
                 canonical_bytes(value.route.model_dump(mode="json")).decode(), canonical_bytes([item.model_dump(mode="json") for item in value.selections]).decode(),
                 canonical_bytes(value.parent_invocation_ids).decode(), value.relation.value, canonical_bytes(value.response_schema).decode(),
                 DraftState.editable.value, now, now),
            )
            self._event(conn, project_id=value.project_id, event_type="draft.created", subject_type="draft", subject_id=draft_id, payload={"prompt_block_id": prompt_block["block_id"], "parents": value.parent_invocation_ids})
        return self.get_draft(draft_id)

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = self._row(conn.execute("SELECT * FROM drafts WHERE draft_id=?", (draft_id,)).fetchone(), draft_id)
        for key in ("route_json", "selections_json", "parents_json", "response_schema_json"):
            row[key.removesuffix("_json")] = json.loads(row.pop(key))
        return row

    def patch_draft(self, draft_id: str, patch: DraftPatch) -> dict[str, Any]:
        current = self.get_draft(draft_id)
        if current["state"] != DraftState.editable.value:
            raise LoomConflict("sealed or abandoned drafts are immutable")
        if current["revision"] != patch.expected_revision:
            raise LoomConflict("draft revision changed")
        prompt_block_id = current["prompt_block_id"]
        if patch.prompt is not None:
            prompt_block_id = self.create_context_block(ContextBlockCreate(project_id=current["project_id"], semantic_type="user-prompt", content=patch.prompt, evidence_level="operator-intent"))["block_id"]
        route = patch.route.model_dump(mode="json") if patch.route else current["route"]
        selections = [item.model_dump(mode="json") for item in patch.selections] if patch.selections is not None else current["selections"]
        parents = patch.parent_invocation_ids if patch.parent_invocation_ids is not None else current["parents"]
        revision = current["revision"] + 1
        with self.transaction() as conn:
            changed = conn.execute(
                """UPDATE drafts SET prompt_block_id=?,route_json=?,selections_json=?,parents_json=?,revision=?,updated_at=?
                   WHERE draft_id=? AND state='editable' AND revision=?""",
                (prompt_block_id, canonical_bytes(route).decode(), canonical_bytes(selections).decode(), canonical_bytes(parents).decode(), revision, stamp(), draft_id, patch.expected_revision),
            ).rowcount
            if changed != 1:
                raise LoomConflict("draft changed during update")
            self._event(conn, project_id=current["project_id"], event_type="draft.revised", subject_type="draft", subject_id=draft_id, payload={"revision": revision})
        return self.get_draft(draft_id)

    def insert_invocation(self, draft: dict[str, Any], manifest: dict[str, Any], manifest_sha: str, compiled_prompt_sha: str) -> dict[str, Any]:
        invocation_id = manifest["invocation_id"]
        now = stamp()
        with self.transaction() as conn:
            current = self._row(conn.execute("SELECT state,revision FROM drafts WHERE draft_id=?", (draft["draft_id"],)).fetchone(), draft["draft_id"])
            if current["state"] != DraftState.editable.value or current["revision"] != draft["revision"]:
                raise LoomConflict("draft changed before sealing")
            conn.execute(
                """INSERT INTO invocations(invocation_id,project_id,role_session_id,draft_id,manifest_artifact_id,manifest_sha256,manifest_json,compiled_prompt_sha256,route_json,response_schema_json,state,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (invocation_id, draft["project_id"], draft["role_session_id"], draft["draft_id"], manifest["generated_object"]["artifact_id"], manifest_sha,
                 canonical_bytes(manifest).decode(), compiled_prompt_sha, canonical_bytes(draft["route"]).decode(), canonical_bytes(draft["response_schema"]).decode(), InvocationState.sealed.value, now),
            )
            conn.execute("UPDATE drafts SET state='sealed',invocation_id=?,updated_at=? WHERE draft_id=?", (invocation_id, now, draft["draft_id"]))
            self._event(conn, project_id=draft["project_id"], event_type="invocation.sealed", subject_type="invocation", subject_id=invocation_id, payload={"manifest_artifact_id": manifest["generated_object"]["artifact_id"], "manifest_sha256": manifest_sha})
            for parent_id in draft["parents"]:
                self._insert_edge(conn, project_id=draft["project_id"], relation=EdgeRelation.inherited_context_from, source_type="invocation", source_id=invocation_id, source_revision=manifest_sha, target_type="invocation", target_id=parent_id, target_revision=self._invocation_revision(conn, parent_id), metadata={})
        return self.get_invocation(invocation_id)

    @staticmethod
    def _invocation_revision(conn: sqlite3.Connection, invocation_id: str) -> str:
        row = conn.execute("SELECT manifest_sha256 FROM invocations WHERE invocation_id=?", (invocation_id,)).fetchone()
        if row is None:
            raise LoomNotFound(invocation_id)
        return str(row[0])

    def _insert_edge(self, conn: sqlite3.Connection, *, project_id: str, relation: EdgeRelation, source_type: str, source_id: str, source_revision: str, target_type: str, target_id: str, target_revision: str, metadata: dict[str, Any]) -> str:
        edge_id = opaque_id("edge")
        conn.execute(
            """INSERT INTO edges(edge_id,project_id,relation,source_type,source_id,source_revision,target_type,target_id,target_revision,metadata_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (edge_id, project_id, relation.value, source_type, source_id, source_revision, target_type, target_id, target_revision, canonical_bytes(metadata).decode(), stamp()),
        )
        self._event(conn, project_id=project_id, event_type="edge.created", subject_type="edge", subject_id=edge_id, payload={"relation": relation.value, "source": source_id, "target": target_id})
        return edge_id

    def add_edge(self, **kwargs: Any) -> str:
        with self.transaction() as conn:
            return self._insert_edge(conn, **kwargs)

    def get_invocation(self, invocation_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = self._row(conn.execute("SELECT * FROM invocations WHERE invocation_id=?", (invocation_id,)).fetchone(), invocation_id)
            row["attempts"] = [dict(item) for item in conn.execute("SELECT * FROM attempts WHERE invocation_id=? ORDER BY attempt_number", (invocation_id,))]
            row["edges"] = [dict(item) for item in conn.execute("SELECT * FROM edges WHERE source_id=? OR target_id=? ORDER BY created_at", (invocation_id, invocation_id))]
            row["artifacts"] = [dict(item) for item in conn.execute(
                """SELECT a.* FROM edges e JOIN artifacts a ON a.artifact_id=e.target_id
                   WHERE e.relation='produced' AND e.source_type='invocation' AND e.source_id=? AND e.target_type='artifact'
                   ORDER BY a.created_at,a.artifact_id""", (invocation_id,)
            )]
        row["manifest"] = json.loads(row.pop("manifest_json"))
        row["route"] = json.loads(row.pop("route_json"))
        row["response_schema"] = json.loads(row.pop("response_schema_json"))
        for attempt in row["attempts"]:
            attempt["runtime_receipt"] = json.loads(attempt.pop("runtime_receipt_json")) if attempt.get("runtime_receipt_json") else None
        if row["output_block_id"]:
            row["output_block"] = self.get_block(row["output_block_id"])
        return row

    def finish_successful_attempt(self, attempt_id: str, *, response: str, artifacts: list[dict[str, str]], journal_path: str, runtime_receipt: dict[str, Any]) -> dict[str, Any]:
        """Persist the complete provider result and terminal receipt as one ledger transition."""
        response_bytes = response.encode("utf-8")
        response_sha = self.blobs.put(response_bytes)
        prepared_artifacts = []
        for artifact in artifacts:
            content = artifact["content"].encode("utf-8")
            prepared_artifacts.append((artifact, opaque_id("artifact"), self.blobs.put(content), len(content)))
        with self.transaction() as conn:
            attempt = self._row(conn.execute("SELECT a.*,i.project_id,i.manifest_sha256 FROM attempts a JOIN invocations i USING(invocation_id) WHERE attempt_id=?", (attempt_id,)).fetchone(), attempt_id)
            if attempt["state"] not in {AttemptState.leased.value, AttemptState.running.value}:
                raise LoomConflict("terminal attempts are immutable")
            now = stamp()
            block_id = opaque_id("block")
            conn.execute(
                """INSERT INTO context_blocks(block_id,project_id,semantic_type,content_sha256,byte_count,initial_status,
                   producing_invocation_id,authority,evidence_level,sensitivity,created_at)
                   VALUES(?,?,?,?,?,'active',?,'model-proposal','generated','private',?)""",
                (block_id, attempt["project_id"], "model-response", response_sha, len(response_bytes), attempt["invocation_id"], now),
            )
            block_event = self._event(conn, project_id=attempt["project_id"], event_type="context-block.created", subject_type="context-block", subject_id=block_id, payload={"sha256": response_sha, "semantic_type": "model-response", "status": "active"})
            conn.execute("INSERT INTO block_status(block_id,status,revision,event_id) VALUES(?,'active',1,?)", (block_id, block_event))
            self._insert_edge(conn, project_id=attempt["project_id"], relation=EdgeRelation.produced, source_type="invocation", source_id=attempt["invocation_id"], source_revision=attempt["manifest_sha256"], target_type="context-block", target_id=block_id, target_revision=response_sha, metadata={})
            for artifact, artifact_id, content_sha, byte_count in prepared_artifacts:
                record = {"name": artifact["name"], "media_type": artifact["media_type"], "content_sha256": content_sha, "byte_count": byte_count, "producing_invocation_id": attempt["invocation_id"]}
                conn.execute("INSERT INTO artifacts(artifact_id,project_id,artifact_kind,generated_json,content_sha256,created_at) VALUES(?,?,?,?,?,?)", (artifact_id, attempt["project_id"], "provider-output", canonical_bytes(record).decode(), content_sha, now))
                self._event(conn, project_id=attempt["project_id"], event_type="artifact.created", subject_type="artifact", subject_id=artifact_id, payload=record)
                self._insert_edge(conn, project_id=attempt["project_id"], relation=EdgeRelation.produced, source_type="invocation", source_id=attempt["invocation_id"], source_revision=attempt["manifest_sha256"], target_type="artifact", target_id=artifact_id, target_revision=content_sha, metadata={"name": artifact["name"], "media_type": artifact["media_type"]})
            conn.execute("UPDATE attempts SET state='succeeded',journal_path=?,output_sha256=?,runtime_receipt_json=?,completed_at=? WHERE attempt_id=?", (journal_path, response_sha, canonical_bytes(runtime_receipt).decode(), now, attempt_id))
            conn.execute("UPDATE leases SET state='released',released_at=? WHERE attempt_id=? AND state='active'", (now, attempt_id))
            conn.execute("UPDATE invocations SET state='succeeded',output_block_id=?,terminal_kind='succeeded',completed_at=? WHERE invocation_id=?", (block_id, now, attempt["invocation_id"]))
            self._event(conn, project_id=attempt["project_id"], event_type="attempt.succeeded", subject_type="attempt", subject_id=attempt_id, payload={"output_block_id": block_id, "artifact_ids": [item[1] for item in prepared_artifacts]})
        return self.get_invocation(attempt["invocation_id"])

    def list_invocations(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute("SELECT invocation_id,project_id,role_session_id,state,manifest_sha256,output_block_id,terminal_kind,created_at,completed_at FROM invocations WHERE project_id=? ORDER BY created_at", (project_id,))]

    def create_attempt(self, invocation_id: str, idempotency_key: str, boot_id: str, expires_at: int) -> dict[str, Any]:
        with self.transaction() as conn:
            prior = conn.execute("SELECT * FROM attempts WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if prior is not None:
                if prior["invocation_id"] != invocation_id:
                    raise LoomConflict("idempotency key belongs to another invocation")
                return dict(prior)
            invocation = self._row(conn.execute("SELECT project_id,state FROM invocations WHERE invocation_id=?", (invocation_id,)).fetchone(), invocation_id)
            if invocation["state"] not in {InvocationState.sealed.value, InvocationState.failed.value, InvocationState.interrupted.value}:
                raise LoomConflict(f"invocation cannot execute from {invocation['state']}")
            active = conn.execute("SELECT 1 FROM leases WHERE invocation_id=? AND state='active'", (invocation_id,)).fetchone()
            if active:
                raise LoomConflict("invocation already has an active lease")
            number = conn.execute("SELECT COALESCE(MAX(attempt_number),0)+1 FROM attempts WHERE invocation_id=?", (invocation_id,)).fetchone()[0]
            attempt_id = opaque_id("attempt")
            lease_id = opaque_id("lease")
            now = stamp()
            conn.execute("INSERT INTO attempts(attempt_id,invocation_id,attempt_number,idempotency_key,state,owner_boot_id,lease_id,started_at,heartbeat_at) VALUES(?,?,?,?,?,?,?,?,?)", (attempt_id, invocation_id, number, idempotency_key, AttemptState.leased.value, boot_id, lease_id, now, now))
            conn.execute("INSERT INTO leases(lease_id,invocation_id,attempt_id,owner_boot_id,state,acquired_at,expires_at) VALUES(?,?,?,?,?,?,?)", (lease_id, invocation_id, attempt_id, boot_id, "active", now, expires_at))
            conn.execute("UPDATE invocations SET state=? WHERE invocation_id=?", (InvocationState.leased.value, invocation_id))
            self._event(conn, project_id=invocation["project_id"], event_type="attempt.leased", subject_type="attempt", subject_id=attempt_id, payload={"invocation_id": invocation_id, "lease_id": lease_id, "attempt_number": number})
            return dict(conn.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone())

    def mark_attempt_running(self, attempt_id: str, journal_path: str | None = None) -> None:
        with self.transaction() as conn:
            attempt = self._row(conn.execute("SELECT a.*,i.project_id FROM attempts a JOIN invocations i USING(invocation_id) WHERE attempt_id=?", (attempt_id,)).fetchone(), attempt_id)
            if attempt["state"] != AttemptState.leased.value:
                raise LoomConflict("attempt is not leased")
            conn.execute("UPDATE attempts SET state='running',journal_path=?,heartbeat_at=? WHERE attempt_id=?", (journal_path, stamp(), attempt_id))
            conn.execute("UPDATE invocations SET state='running' WHERE invocation_id=?", (attempt["invocation_id"],))
            self._event(conn, project_id=attempt["project_id"], event_type="attempt.running", subject_type="attempt", subject_id=attempt_id, payload={"journal_path": journal_path})

    def heartbeat_attempt(self, attempt_id: str, expires_at: int) -> None:
        with self.transaction() as conn:
            changed = conn.execute("UPDATE attempts SET heartbeat_at=? WHERE attempt_id=? AND state='running'", (stamp(), attempt_id)).rowcount
            if changed:
                conn.execute("UPDATE leases SET expires_at=? WHERE attempt_id=? AND state='active'", (expires_at, attempt_id))

    def finish_attempt(self, attempt_id: str, *, state: AttemptState, output_block_id: str | None = None, output_sha256: str | None = None, failure_kind: str | None = None, error_text: str | None = None, journal_path: str | None = None, runtime_receipt: dict[str, Any] | None = None) -> dict[str, Any]:
        if state not in {AttemptState.succeeded, AttemptState.failed, AttemptState.interrupted, AttemptState.orphaned}:
            raise ValueError("attempt terminal state required")
        with self.transaction() as conn:
            attempt = self._row(conn.execute("SELECT a.*,i.project_id FROM attempts a JOIN invocations i USING(invocation_id) WHERE attempt_id=?", (attempt_id,)).fetchone(), attempt_id)
            if attempt["state"] not in {AttemptState.leased.value, AttemptState.running.value}:
                raise LoomConflict("terminal attempts are immutable")
            now = stamp()
            conn.execute("UPDATE attempts SET state=?,journal_path=COALESCE(?,journal_path),output_sha256=?,failure_kind=?,error_text=?,runtime_receipt_json=?,completed_at=? WHERE attempt_id=?", (state.value, journal_path, output_sha256, failure_kind, error_text, canonical_bytes(runtime_receipt).decode() if runtime_receipt else None, now, attempt_id))
            conn.execute("UPDATE leases SET state='released',released_at=? WHERE attempt_id=? AND state='active'", (now, attempt_id))
            invocation_state = InvocationState.succeeded if state == AttemptState.succeeded else InvocationState.interrupted if state in {AttemptState.interrupted, AttemptState.orphaned} else InvocationState.failed
            conn.execute("UPDATE invocations SET state=?,output_block_id=COALESCE(?,output_block_id),terminal_kind=?,completed_at=? WHERE invocation_id=?", (invocation_state.value, output_block_id, failure_kind or state.value, now, attempt["invocation_id"]))
            self._event(conn, project_id=attempt["project_id"], event_type=f"attempt.{state.value}", subject_type="attempt", subject_id=attempt_id, payload={"output_block_id": output_block_id, "failure_kind": failure_kind})
        return self.get_invocation(attempt["invocation_id"])

    def attach_imported_output(self, invocation_id: str, output_block_id: str) -> dict[str, Any]:
        block = self.get_block(output_block_id, include_content=False)
        with self.transaction() as conn:
            invocation = self._row(conn.execute("SELECT project_id,state,manifest_sha256 FROM invocations WHERE invocation_id=?", (invocation_id,)).fetchone(), invocation_id)
            if invocation["state"] != InvocationState.sealed.value:
                raise LoomConflict("only newly sealed invocations can become imported history")
            if invocation["project_id"] != block["project_id"]:
                raise LoomConflict("imported output belongs to another project")
            conn.execute("UPDATE invocations SET state='imported',output_block_id=?,terminal_kind='historical-import',completed_at=? WHERE invocation_id=?", (output_block_id, stamp(), invocation_id))
            self._insert_edge(conn, project_id=invocation["project_id"], relation=EdgeRelation.produced, source_type="invocation", source_id=invocation_id, source_revision=invocation["manifest_sha256"], target_type="context-block", target_id=output_block_id, target_revision=block["content_sha256"], metadata={"historical_import": True})
            self._event(conn, project_id=invocation["project_id"], event_type="invocation.imported", subject_type="invocation", subject_id=invocation_id, payload={"output_block_id": output_block_id})
        return self.get_invocation(invocation_id)

    def reconcile_orphans(self, boot_id: str) -> int:
        with self._connect() as conn:
            rows = [dict(row) for row in conn.execute("SELECT attempt_id FROM attempts WHERE state IN ('leased','running') AND COALESCE(owner_boot_id,'')<>?", (boot_id,))]
        for row in rows:
            self.finish_attempt(row["attempt_id"], state=AttemptState.orphaned, failure_kind="process_restart", error_text="Loom restarted before the attempt reached a terminal receipt")
        return len(rows)

    def create_slice(self, value: SliceCreate) -> dict[str, Any]:
        slice_id = opaque_id("slice")
        now = stamp()
        with self.transaction() as conn:
            conn.execute("INSERT INTO slices(slice_id,project_id,name,query_json,external_ref,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (slice_id, value.project_id, value.name, canonical_bytes(value.query).decode(), value.external_ref, now, now))
            self._event(conn, project_id=value.project_id, event_type="slice.created", subject_type="slice", subject_id=slice_id, payload=value.model_dump(mode="json"))
        return self.get_slice(slice_id)

    def get_slice(self, slice_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = self._row(conn.execute("SELECT * FROM slices WHERE slice_id=?", (slice_id,)).fetchone(), slice_id)
        row["query"] = json.loads(row.pop("query_json"))
        row["items"] = self.evaluate_slice(row)
        return row

    def get_slice_by_external_ref(self, external_ref: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT slice_id FROM slices WHERE external_ref=?", (external_ref,)).fetchone()
        return self.get_slice(row["slice_id"]) if row else None

    def evaluate_slice(self, slice_row: dict[str, Any]) -> list[dict[str, Any]]:
        query = slice_row.get("query") or json.loads(slice_row["query_json"])
        concern = query.get("concern_ref")
        object_types = set(query.get("object_types", ["invocation"]))
        items: list[dict[str, Any]] = []
        with self._connect() as conn:
            if "invocation" in object_types and concern:
                rows = conn.execute(
                    """SELECT i.invocation_id,i.state,i.role_session_id,i.output_block_id,i.created_at
                       FROM edges e JOIN invocations i ON i.invocation_id=e.source_id
                       WHERE e.relation='concerns' AND e.target_id=? AND i.project_id=? ORDER BY i.created_at""",
                    (concern, slice_row["project_id"]),
                )
                items.extend({**dict(row), "object_type": "invocation", "object_id": row["invocation_id"]} for row in rows)
            elif "invocation" in object_types:
                rows = conn.execute("SELECT invocation_id,state,role_session_id,output_block_id,created_at FROM invocations WHERE project_id=? ORDER BY created_at", (slice_row["project_id"],))
                items.extend({**dict(row), "object_type": "invocation", "object_id": row["invocation_id"]} for row in rows)
            if "context-block" in object_types:
                params: list[Any] = [slice_row["project_id"]]
                sql = """SELECT b.block_id,b.semantic_type,b.content_sha256,b.created_at,s.status
                         FROM context_blocks b JOIN block_status s USING(block_id)"""
                if concern:
                    sql += " JOIN edges e ON e.source_type='context-block' AND e.source_id=b.block_id AND e.relation='concerns' AND e.target_id=?"
                    params.append(concern)
                sql += " WHERE b.project_id=?" if not concern else " WHERE b.project_id=?"
                if concern:
                    params = [concern, slice_row["project_id"]]
                for row in conn.execute(sql, params):
                    items.append({**dict(row), "object_type": "context-block", "object_id": row["block_id"], "response": self.blobs.get(row["content_sha256"]).decode("utf-8")})
        for item in items:
            if item.get("output_block_id"):
                block = self.get_block(item["output_block_id"])
                item["response"] = block["content"]
                item["response_sha256"] = block["content_sha256"]
        return items

    def events_after(self, project_id: str, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM events WHERE project_id=? AND seq>? ORDER BY seq LIMIT ?", (project_id, after, limit))]
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_json"))
        return rows

    def state_digest(self) -> str:
        state: dict[str, list[dict[str, Any]]] = {}
        with self._connect() as conn:
            for table in CHANGE_TABLES:
                _columns, primary = self._table_contract(conn, table)
                order = ",".join(f'"{name}"' for name in primary)
                state[table] = [dict(row) for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY {order}')]
        return sha256_bytes(canonical_bytes(state))

    def rebuild_database(self, destination: Path) -> str:
        """Reconstruct every derived Loom view from the append-only row-change ledger."""
        destination = destination.resolve()
        if destination.exists():
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(destination, isolation_level=None)
        target.row_factory = sqlite3.Row
        target.create_function("loom_stamp", 0, stamp)
        target.create_function("loom_replaying", 0, lambda: 1)
        try:
            target.executescript(DDL)
            target.execute("PRAGMA foreign_keys=ON")
            target.execute("BEGIN IMMEDIATE")
            with self._connect() as source:
                for row in source.execute("SELECT * FROM events ORDER BY seq"):
                    value = dict(row)
                    columns = list(value)
                    target.execute(f"INSERT INTO events({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", tuple(value[name] for name in columns))
                for change in source.execute("SELECT * FROM ledger_changes ORDER BY seq"):
                    table = change["table_name"]
                    if table not in CHANGE_TABLES:
                        raise RuntimeError(f"change ledger names unsupported table: {table}")
                    primary = json.loads(change["primary_key_json"])
                    if change["operation"] == "delete":
                        where = " AND ".join(f'"{name}"=?' for name in primary)
                        target.execute(f'DELETE FROM "{table}" WHERE {where}', tuple(primary.values()))
                    else:
                        row = json.loads(change["row_json"])
                        columns = list(row)
                        if change["operation"] in {"baseline", "insert"}:
                            target.execute(f'INSERT INTO "{table}"({",".join(f"\"{name}\"" for name in columns)}) VALUES({",".join("?" for _ in columns)})', tuple(row[name] for name in columns))
                        else:
                            non_primary = [name for name in columns if name not in primary]
                            assignments = ",".join(f'"{name}"=?' for name in non_primary)
                            where = " AND ".join(f'"{name}"=?' for name in primary)
                            target.execute(f'UPDATE "{table}" SET {assignments} WHERE {where}', tuple(row[name] for name in non_primary) + tuple(primary.values()))
                for row in source.execute("SELECT * FROM ledger_changes ORDER BY seq"):
                    value = dict(row)
                    columns = list(value)
                    target.execute(f"INSERT INTO ledger_changes({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", tuple(value[name] for name in columns))
            target.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(1,?)", (stamp(),))
            target.commit()
        except BaseException:
            target.rollback()
            raise
        finally:
            target.close()
        rebuilt = LoomStore(LoomConfig(data_root=destination.parent, host=self.config.host, port=self.config.port, octopus_base_url=self.config.octopus_base_url, lease_seconds=self.config.lease_seconds))
        if rebuilt.config.database != destination:
            raise RuntimeError("rebuild destination must be named loom.sqlite3 inside its data root")
        for source_blob in self.blobs.root.glob("*/*"):
            if source_blob.is_file():
                content = source_blob.read_bytes()
                if rebuilt.blobs.put(content) != source_blob.name:
                    raise OSError(f"blob identity changed during rebuild: {source_blob}")
        return rebuilt.state_digest()

    def record_event(self, *, project_id: str | None, event_type: str, subject_type: str, subject_id: str, payload: dict[str, Any]) -> str:
        with self.transaction() as conn:
            return self._event(conn, project_id=project_id, event_type=event_type, subject_type=subject_type, subject_id=subject_id, payload=payload)

    def claim_workflow_command(self, *, project_id: str, pack_id: str, operation: str, arguments: dict[str, Any], idempotency_key: str, boot_id: str) -> dict[str, Any]:
        arguments_json = canonical_bytes(arguments).decode()
        with self.transaction() as conn:
            prior = conn.execute("SELECT * FROM workflow_commands WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if prior is not None:
                if prior["project_id"] != project_id or prior["pack_id"] != pack_id or prior["operation"] != operation or prior["arguments_json"] != arguments_json:
                    raise LoomConflict("workflow idempotency key was reused for another command")
                return dict(prior)
            project = self._row(conn.execute("SELECT workflow_pack FROM projects WHERE project_id=?", (project_id,)).fetchone(), project_id)
            if project["workflow_pack"] != pack_id:
                raise LoomConflict("project is not governed by this workflow pack")
            command_id = opaque_id("workflow_command")
            now = stamp()
            conn.execute("INSERT INTO workflow_commands(command_id,project_id,pack_id,operation,arguments_json,idempotency_key,state,owner_boot_id,started_at) VALUES(?,?,?,?,?,?,'running',?,?)", (command_id, project_id, pack_id, operation, arguments_json, idempotency_key, boot_id, now))
            self._event(conn, project_id=project_id, event_type="workflow.command-started", subject_type="workflow-command", subject_id=command_id, payload={"pack_id": pack_id, "operation": operation, "arguments": arguments, "idempotency_key": idempotency_key})
            return dict(conn.execute("SELECT * FROM workflow_commands WHERE command_id=?", (command_id,)).fetchone())

    def complete_workflow_command(self, command_id: str, *, result: dict[str, Any]) -> dict[str, Any]:
        result_bytes = canonical_bytes(result)
        result_sha = self.blobs.put(result_bytes)
        with self.transaction() as conn:
            command = self._row(conn.execute("SELECT * FROM workflow_commands WHERE command_id=?", (command_id,)).fetchone(), command_id)
            if command["state"] != "running":
                raise LoomConflict("workflow command is already terminal")
            now = stamp()
            receipt = result.get("receipt", {})
            conn.execute("UPDATE workflow_commands SET state='completed',result_sha256=?,receipt_json=?,completed_at=? WHERE command_id=?", (result_sha, canonical_bytes(receipt).decode(), now, command_id))
            self._event(conn, project_id=command["project_id"], event_type="workflow.command-completed", subject_type="workflow-command", subject_id=command_id, payload={"operation": command["operation"], "result_sha256": result_sha, "receipt": receipt})
        return self.get_workflow_command(command_id, include_result=True)

    def fail_workflow_command(self, command_id: str, *, error_text: str, outcome_unknown: bool = False) -> dict[str, Any]:
        with self.transaction() as conn:
            command = self._row(conn.execute("SELECT * FROM workflow_commands WHERE command_id=?", (command_id,)).fetchone(), command_id)
            if command["state"] != "running":
                raise LoomConflict("workflow command is already terminal")
            state = "outcome_unknown" if outcome_unknown else "failed"
            now = stamp()
            conn.execute("UPDATE workflow_commands SET state=?,error_text=?,completed_at=? WHERE command_id=?", (state, error_text, now, command_id))
            self._event(conn, project_id=command["project_id"], event_type=f"workflow.command-{state.replace('_', '-')}", subject_type="workflow-command", subject_id=command_id, payload={"operation": command["operation"], "error": error_text})
        return self.get_workflow_command(command_id)

    def get_workflow_command(self, command_id: str, *, include_result: bool = False) -> dict[str, Any]:
        with self._connect() as conn:
            row = self._row(conn.execute("SELECT * FROM workflow_commands WHERE command_id=?", (command_id,)).fetchone(), command_id)
        row["arguments"] = json.loads(row.pop("arguments_json"))
        row["receipt"] = json.loads(row.pop("receipt_json")) if row.get("receipt_json") else None
        if include_result and row.get("result_sha256"):
            row["result"] = json.loads(self.blobs.get(row["result_sha256"]))
        return row

    def reconcile_workflow_commands(self, boot_id: str) -> int:
        with self._connect() as conn:
            command_ids = [row[0] for row in conn.execute("SELECT command_id FROM workflow_commands WHERE state='running' AND owner_boot_id<>?", (boot_id,))]
        for command_id in command_ids:
            self.fail_workflow_command(command_id, error_text="Loom restarted after dispatch; authoritative external outcome must be inspected before retry", outcome_unknown=True)
        return len(command_ids)

    def advance_workflow(self, *, project_id: str, pack_id: str, expected_revision: int, from_state: str, to_state: str, evidence_block_ids: list[str], payload: dict[str, Any], trigger: dict[str, Any] | None) -> dict[str, Any]:
        payload_bytes = canonical_bytes(payload)
        payload_sha = self.blobs.put(payload_bytes)
        frozen_query_sha = None
        if to_state == "frozen-query":
            query_bytes = payload.get("query_bytes")
            if not isinstance(query_bytes, str) or not query_bytes:
                raise LoomConflict("freezing a query requires the exact accepted query bytes")
            frozen_query_sha = self.blobs.put(query_bytes.encode("utf-8"))
        with self.transaction() as conn:
            instance = self._row(conn.execute("SELECT * FROM workflow_instances WHERE project_id=?", (project_id,)).fetchone(), project_id)
            if instance["pack_id"] != pack_id or instance["state"] != from_state or instance["revision"] != expected_revision:
                raise LoomConflict("workflow state changed before transition")
            for block_id in evidence_block_ids:
                block = self._row(conn.execute("SELECT project_id FROM context_blocks WHERE block_id=?", (block_id,)).fetchone(), block_id)
                if block["project_id"] != project_id:
                    raise LoomConflict("workflow evidence belongs to another project")
            if from_state == "closure-candidate" and to_state == "blinded-challenge":
                challenger = conn.execute("SELECT result_ref FROM workflow_triggers WHERE project_id=? AND trigger_name='closure-proposed' AND state='completed' ORDER BY created_at DESC LIMIT 1", (project_id,)).fetchone()
                if challenger is None:
                    raise LoomConflict("closure cannot advance before the blinded challenger completes")
                invocation = conn.execute("SELECT state FROM invocations WHERE invocation_id=?", (challenger["result_ref"],)).fetchone()
                if invocation is None or invocation["state"] != InvocationState.succeeded.value:
                    raise LoomConflict("blinded challenger has not produced an independently recomputed result")
            revision = expected_revision + 1
            transition_id = opaque_id("workflow_transition")
            now = stamp()
            conn.execute("INSERT INTO workflow_transitions(transition_id,project_id,pack_id,from_state,to_state,revision,evidence_block_ids_json,payload_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (transition_id, project_id, pack_id, from_state, to_state, revision, canonical_bytes(evidence_block_ids).decode(), payload_sha, now))
            conn.execute("UPDATE workflow_instances SET state=?,revision=?,updated_at=? WHERE project_id=?", (to_state, revision, now, project_id))
            self._event(conn, project_id=project_id, event_type="workflow.transitioned", subject_type="workflow-transition", subject_id=transition_id, payload={"pack_id": pack_id, "from": from_state, "to": to_state, "revision": revision, "evidence_block_ids": evidence_block_ids, "payload_sha256": payload_sha})
            trigger_row = None
            if trigger:
                trigger_id = opaque_id("workflow_trigger")
                trigger_state = "completed" if trigger["trigger"] == "query-accepted" else "pending"
                result_ref = f"loom:blob/{frozen_query_sha}" if frozen_query_sha else None
                completed_at = now if trigger_state == "completed" else None
                conn.execute("INSERT INTO workflow_triggers(trigger_id,project_id,trigger_name,required_action,state,source_transition_id,result_ref,created_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?)", (trigger_id, project_id, trigger["trigger"], trigger["required_action"], trigger_state, transition_id, result_ref, now, completed_at))
                self._event(conn, project_id=project_id, event_type=f"workflow.trigger-{trigger_state}", subject_type="workflow-trigger", subject_id=trigger_id, payload={"trigger": trigger["trigger"], "required_action": trigger["required_action"], "result_ref": result_ref})
                trigger_row = dict(conn.execute("SELECT * FROM workflow_triggers WHERE trigger_id=?", (trigger_id,)).fetchone())
        return {"workflow": self.get_workflow_instance(project_id), "transition_id": transition_id, "trigger": trigger_row}

    def get_workflow_instance(self, project_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = self._row(conn.execute("SELECT * FROM workflow_instances WHERE project_id=?", (project_id,)).fetchone(), project_id)
            row["triggers"] = [dict(item) for item in conn.execute("SELECT * FROM workflow_triggers WHERE project_id=? ORDER BY created_at", (project_id,))]
        row["automatic_grants"] = json.loads(row.pop("automatic_grants_json"))
        return row

    def complete_workflow_trigger(self, trigger_id: str, *, result_ref: str) -> dict[str, Any]:
        with self.transaction() as conn:
            trigger = self._row(conn.execute("SELECT * FROM workflow_triggers WHERE trigger_id=?", (trigger_id,)).fetchone(), trigger_id)
            if trigger["state"] != "pending":
                raise LoomConflict("workflow trigger is already terminal")
            now = stamp()
            conn.execute("UPDATE workflow_triggers SET state='completed',result_ref=?,completed_at=? WHERE trigger_id=?", (result_ref, now, trigger_id))
            self._event(conn, project_id=trigger["project_id"], event_type="workflow.trigger-completed", subject_type="workflow-trigger", subject_id=trigger_id, payload={"result_ref": result_ref})
        return self.get_workflow_instance(trigger["project_id"])

    def get_attempt(self, attempt_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = self._row(conn.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone(), attempt_id)
        row["runtime_receipt"] = json.loads(row.pop("runtime_receipt_json")) if row.get("runtime_receipt_json") else None
        return row

    def graph_neighborhood(self, invocation_id: str, depth: int = 2) -> dict[str, Any]:
        if depth < 0 or depth > 5:
            raise ValueError("depth must be between 0 and 5")
        seen = {invocation_id}
        frontier = {invocation_id}
        edges: dict[str, dict[str, Any]] = {}
        with self._connect() as conn:
            for _ in range(depth):
                if not frontier:
                    break
                placeholders = ",".join("?" for _ in frontier)
                found = [dict(row) for row in conn.execute(f"SELECT * FROM edges WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})", (*frontier, *frontier))]
                next_frontier: set[str] = set()
                for edge in found:
                    edges[edge["edge_id"]] = edge
                    for identifier in (edge["source_id"], edge["target_id"]):
                        if identifier not in seen:
                            seen.add(identifier)
                            next_frontier.add(identifier)
                frontier = next_frontier
            nodes = [dict(row) for row in conn.execute(f"SELECT invocation_id,state,role_session_id,output_block_id,created_at FROM invocations WHERE invocation_id IN ({','.join('?' for _ in seen)})", tuple(seen))] if seen else []
        return {"root": invocation_id, "nodes": nodes, "edges": list(edges.values())}
