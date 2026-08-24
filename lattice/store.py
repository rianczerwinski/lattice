"""Append-only Lattice ledger and its transactionally derived current views."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from axis_model_runner import DEFAULT_ADAPTERS, SubscriptionRouteUnavailable, validate_execution_policy

from .blob_store import BlobStore
from .canonical import canonical_bytes, opaque_id, sha256_bytes, stamp
from .config import LatticeConfig
from .snapshot import pre_migration_snapshot_if_needed
from .contracts import (
    AttemptState,
    BlockDisposition,
    BlockStatus,
    ContextBlockCreate,
    DerivedTextBlockCreate,
    DraftCreate,
    DraftPatch,
    DraftState,
    EdgeRelation,
    EpisodeCreate,
    EpisodeMemberCreate,
    InvocationState,
    model_runner_policy,
    ProjectCreate,
    RoleCreate,
    RolePinCreate,
    RoleSessionCreate,
    SliceCreate,
    SliceQuery,
    TagRequest,
)


MAX_AXIAL_CONCERNS = 10_000
MAX_AXIAL_SOURCE_CLAIMS = 100_000
MAX_AXIAL_PROJECTION_BYTES = 16 * 1024 * 1024
SCHEMA_VERSION = 9
CURRENT_BLOB_PREFIX = "lattice:blob/"
LEGACY_BLOB_PREFIX = "loom:blob/"


DDL = """
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_migrations(
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
) STRICT;

CREATE TRIGGER IF NOT EXISTS schema_migrations_no_update BEFORE UPDATE ON schema_migrations
BEGIN SELECT RAISE(ABORT, 'Lattice schema migration history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS schema_migrations_no_delete BEFORE DELETE ON schema_migrations
BEGIN SELECT RAISE(ABORT, 'Lattice schema migration history is append-only'); END;

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
BEGIN SELECT RAISE(ABORT, 'Lattice events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'Lattice events are append-only'); END;

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
BEGIN SELECT RAISE(ABORT, 'Lattice change ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_changes_no_delete BEFORE DELETE ON ledger_changes
BEGIN SELECT RAISE(ABORT, 'Lattice change ledger is append-only'); END;

CREATE TABLE IF NOT EXISTS append_integrity(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    stream TEXT NOT NULL CHECK(stream IN ('event','ledger-change')),
    source_seq INTEGER NOT NULL,
    prev_sha256 TEXT NOT NULL CHECK(length(prev_sha256)=64),
    entry_sha256 TEXT NOT NULL CHECK(length(entry_sha256)=64),
    created_at TEXT NOT NULL,
    UNIQUE(stream, source_seq)
) STRICT;

CREATE TRIGGER IF NOT EXISTS append_integrity_no_update BEFORE UPDATE ON append_integrity
BEGIN SELECT RAISE(ABORT, 'Lattice integrity chain is append-only'); END;
CREATE TRIGGER IF NOT EXISTS append_integrity_no_delete BEFORE DELETE ON append_integrity
BEGIN SELECT RAISE(ABORT, 'Lattice integrity chain is append-only'); END;

CREATE TRIGGER IF NOT EXISTS events_append_integrity AFTER INSERT ON events WHEN lattice_replaying()=0
BEGIN
    INSERT INTO append_integrity(stream,source_seq,prev_sha256,entry_sha256,created_at)
    VALUES(
        'event',
        NEW.seq,
        COALESCE((SELECT entry_sha256 FROM append_integrity ORDER BY seq DESC LIMIT 1),'0000000000000000000000000000000000000000000000000000000000000000'),
        lattice_integrity_hash('event',COALESCE((SELECT entry_sha256 FROM append_integrity ORDER BY seq DESC LIMIT 1),'0000000000000000000000000000000000000000000000000000000000000000'),NEW.seq,NEW.event_id,NEW.project_id,NEW.event_type,NEW.subject_type,NEW.subject_id,NEW.payload_json,NEW.payload_sha256,NEW.occurred_at),
        lattice_stamp()
    );
END;

CREATE TRIGGER IF NOT EXISTS ledger_changes_append_integrity AFTER INSERT ON ledger_changes WHEN lattice_replaying()=0
BEGIN
    INSERT INTO append_integrity(stream,source_seq,prev_sha256,entry_sha256,created_at)
    VALUES(
        'ledger-change',
        NEW.seq,
        COALESCE((SELECT entry_sha256 FROM append_integrity ORDER BY seq DESC LIMIT 1),'0000000000000000000000000000000000000000000000000000000000000000'),
        lattice_integrity_hash('ledger-change',COALESCE((SELECT entry_sha256 FROM append_integrity ORDER BY seq DESC LIMIT 1),'0000000000000000000000000000000000000000000000000000000000000000'),NEW.seq,NEW.change_id,NEW.table_name,NEW.operation,NEW.primary_key_json,NEW.row_json,NEW.occurred_at),
        lattice_stamp()
    );
END;

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
    workflow_role_key TEXT,
    route_policy_json TEXT NOT NULL DEFAULT '{"kind":"operator-selectable"}' CHECK(json_valid(route_policy_json)),
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
    actor_kind TEXT NOT NULL CHECK(actor_kind IN ('direct','role-session')),
    role_session_id TEXT REFERENCES role_sessions(role_session_id),
    prompt_block_id TEXT NOT NULL REFERENCES context_blocks(block_id),
    route_json TEXT NOT NULL CHECK(json_valid(route_json)),
    selections_json TEXT NOT NULL CHECK(json_valid(selections_json)),
    parents_json TEXT NOT NULL CHECK(json_valid(parents_json)),
    relation TEXT NOT NULL,
    context_mode TEXT NOT NULL DEFAULT 'standard',
    response_schema_json TEXT NOT NULL CHECK(json_valid(response_schema_json)),
    revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL,
    invocation_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK((actor_kind='direct' AND role_session_id IS NULL) OR (actor_kind='role-session' AND role_session_id IS NOT NULL))
) STRICT;

CREATE TABLE IF NOT EXISTS invocations(
    invocation_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    actor_kind TEXT NOT NULL CHECK(actor_kind IN ('direct','role-session')),
    role_session_id TEXT REFERENCES role_sessions(role_session_id),
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
    completed_at TEXT,
    CHECK((actor_kind='direct' AND role_session_id IS NULL) OR (actor_kind='role-session' AND role_session_id IS NOT NULL))
) STRICT;

CREATE TABLE IF NOT EXISTS invocation_batches(
    batch_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    idempotency_key TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
    preview_sha256 TEXT NOT NULL CHECK(length(preview_sha256)=64),
    receipt_json TEXT NOT NULL CHECK(json_valid(receipt_json)),
    receipt_sha256 TEXT NOT NULL CHECK(length(receipt_sha256)=64),
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS invocation_batch_members(
    batch_id TEXT NOT NULL REFERENCES invocation_batches(batch_id),
    ordinal INTEGER NOT NULL CHECK(ordinal>=0),
    spec_id TEXT NOT NULL,
    assembly_sha256 TEXT NOT NULL CHECK(length(assembly_sha256)=64),
    invocation_id TEXT NOT NULL UNIQUE REFERENCES invocations(invocation_id),
    PRIMARY KEY(batch_id,ordinal),
    UNIQUE(batch_id,spec_id)
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
    scope TEXT NOT NULL DEFAULT 'project' CHECK(scope IN ('project','global')),
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
    expected_workflow_revision INTEGER,
    pack_canonical_sha256 TEXT,
    review_sha256 TEXT,
    review_json TEXT CHECK(review_json IS NULL OR json_valid(review_json)),
    state TEXT NOT NULL,
    owner_boot_id TEXT NOT NULL,
    result_sha256 TEXT,
    receipt_json TEXT CHECK(receipt_json IS NULL OR json_valid(receipt_json)),
    error_text TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS conversation_imports(
    import_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    conversation_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    transcript_sha256 TEXT NOT NULL CHECK(length(transcript_sha256)=64),
    refs_sha256 TEXT NOT NULL CHECK(length(refs_sha256)=64),
    state TEXT NOT NULL,
    owner_boot_id TEXT NOT NULL,
    result_sha256 TEXT,
    role_session_id TEXT REFERENCES role_sessions(role_session_id),
    proof_limit_block_id TEXT REFERENCES context_blocks(block_id),
    error_text TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(project_id,conversation_id,provider,transcript_sha256,refs_sha256)
) STRICT;

CREATE TABLE IF NOT EXISTS conversation_import_turns(
    import_id TEXT NOT NULL REFERENCES conversation_imports(import_id),
    ordinal INTEGER NOT NULL CHECK(ordinal>=0),
    prompt_sha256 TEXT NOT NULL CHECK(length(prompt_sha256)=64),
    response_sha256 TEXT NOT NULL CHECK(length(response_sha256)=64),
    response_state TEXT NOT NULL DEFAULT 'present' CHECK(response_state IN ('present','absent')),
    draft_id TEXT REFERENCES drafts(draft_id),
    invocation_id TEXT REFERENCES invocations(invocation_id),
    output_block_id TEXT REFERENCES context_blocks(block_id),
    state TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(import_id, ordinal)
) STRICT;

CREATE UNIQUE INDEX IF NOT EXISTS one_running_workflow_command
ON workflow_commands(project_id) WHERE state='running';

CREATE TABLE IF NOT EXISTS workflow_instances(
    project_id TEXT PRIMARY KEY REFERENCES projects(project_id),
    pack_id TEXT NOT NULL,
    state TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision>0),
    automatic_grants_json TEXT NOT NULL CHECK(json_valid(automatic_grants_json)),
    selector_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(selector_json)),
    pack_canonical_sha256 TEXT,
    pack_definition_sha256 TEXT,
    current_blackboard_block_id TEXT REFERENCES context_blocks(block_id),
    state_entry_seq INTEGER NOT NULL DEFAULT 0 CHECK(state_entry_seq>=0),
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
    pack_canonical_sha256 TEXT,
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
    pack_canonical_sha256 TEXT,
    action_sha256 TEXT,
    action_json TEXT CHECK(action_json IS NULL OR json_valid(action_json)),
    result_ref TEXT,
    error_text TEXT,
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

CREATE TABLE IF NOT EXISTS project_context_defaults(
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    block_id TEXT NOT NULL REFERENCES context_blocks(block_id),
    module_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('project_default','workflow_required')),
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    PRIMARY KEY(project_id,block_id),
    UNIQUE(project_id,module_id)
) STRICT;

CREATE INDEX IF NOT EXISTS events_project_seq ON events(project_id, seq);
CREATE INDEX IF NOT EXISTS events_subject_seq ON events(subject_type, subject_id, event_type, seq);
CREATE INDEX IF NOT EXISTS invocations_project_created ON invocations(project_id, created_at);
CREATE INDEX IF NOT EXISTS edges_source ON edges(source_type, source_id);
CREATE INDEX IF NOT EXISTS edges_target ON edges(target_type, target_id);
CREATE INDEX IF NOT EXISTS ledger_changes_current ON ledger_changes(table_name, primary_key_json, seq DESC);
"""


CHANGE_TABLES = (
    "projects", "context_blocks", "block_status", "roles", "role_sessions", "drafts", "invocations", "edges",
    "invocation_batches", "invocation_batch_members", "attempts", "leases", "slices", "artifacts", "workflow_commands", "workflow_instances", "workflow_transitions",
    "workflow_triggers", "role_context_defaults", "project_context_defaults", "conversation_imports", "conversation_import_turns",
)


class LatticeConflict(RuntimeError):
    pass


class LatticeNotFound(KeyError):
    pass


class LatticeStore:
    SLICE_DEFAULT_LIMIT = 200
    SLICE_MAX_LIMIT = 500
    SLICE_INLINE_RESPONSE_BYTES = 8 * 1024 * 1024
    NEIGHBORHOOD_DEFAULT_MAX_NODES = 500
    NEIGHBORHOOD_DEFAULT_MAX_EDGES = 1000
    NEIGHBORHOOD_MAX_NODES = 5000
    NEIGHBORHOOD_MAX_EDGES = 10000

    def __init__(self, config: LatticeConfig) -> None:
        self.config = config
        self.config.data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.config.data_root, 0o700)
        pre_migration_snapshot_if_needed(self.config.data_root, target_schema_version=SCHEMA_VERSION)
        self.blobs = BlobStore(config.blobs)
        self._write_lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript(DDL)
            attempt_columns = {row[1] for row in conn.execute("PRAGMA table_info(attempts)")}
            role_columns = {row[1] for row in conn.execute("PRAGMA table_info(roles)")}
            trigger_columns = {row[1] for row in conn.execute("PRAGMA table_info(workflow_triggers)")}
            draft_columns = {row[1] for row in conn.execute("PRAGMA table_info(drafts)")}
            invocation_columns = {row[1] for row in conn.execute("PRAGMA table_info(invocations)")}
            slice_columns = {row[1] for row in conn.execute("PRAGMA table_info(slices)")}
            workflow_columns = {row[1] for row in conn.execute("PRAGMA table_info(workflow_instances)")}
            transition_columns = {row[1] for row in conn.execute("PRAGMA table_info(workflow_transitions)")}
            command_columns = {row[1] for row in conn.execute("PRAGMA table_info(workflow_commands)")}
            import_columns = {row[1] for row in conn.execute("PRAGMA table_info(conversation_imports)")}
            import_turn_columns = {row[1] for row in conn.execute("PRAGMA table_info(conversation_import_turns)")}
            required_columns = {
                "attempts": ({"runtime_receipt_json"}, attempt_columns),
                "roles": ({"workflow_role_key", "route_policy_json"}, role_columns),
                "workflow_triggers": ({"error_text", "pack_canonical_sha256", "action_sha256", "action_json"}, trigger_columns),
                "drafts": ({"context_mode", "actor_kind"}, draft_columns),
                "invocations": ({"actor_kind"}, invocation_columns),
                "slices": ({"scope"}, slice_columns),
                "workflow_instances": ({"selector_json", "pack_canonical_sha256", "pack_definition_sha256", "current_blackboard_block_id", "state_entry_seq"}, workflow_columns),
                "workflow_transitions": ({"pack_canonical_sha256"}, transition_columns),
                "workflow_commands": ({"expected_workflow_revision", "pack_canonical_sha256", "review_sha256", "review_json"}, command_columns),
                "conversation_imports": ({"role_session_id", "proof_limit_block_id"}, import_columns),
                "conversation_import_turns": ({"response_state"}, import_turn_columns),
            }
            for table, (required, present) in required_columns.items():
                if not required.issubset(present):
                    for operation in ("insert", "update", "delete"):
                        conn.execute(f'DROP TRIGGER IF EXISTS "ledger_{table}_{operation}"')
            if "runtime_receipt_json" not in attempt_columns:
                conn.execute("ALTER TABLE attempts ADD COLUMN runtime_receipt_json TEXT CHECK(runtime_receipt_json IS NULL OR json_valid(runtime_receipt_json))")
            if "workflow_role_key" not in role_columns:
                conn.execute("ALTER TABLE roles ADD COLUMN workflow_role_key TEXT")
            if "route_policy_json" not in role_columns:
                conn.execute("ALTER TABLE roles ADD COLUMN route_policy_json TEXT NOT NULL DEFAULT '{\"kind\":\"operator-selectable\"}' CHECK(json_valid(route_policy_json))")
            if "error_text" not in trigger_columns:
                conn.execute("ALTER TABLE workflow_triggers ADD COLUMN error_text TEXT")
            if "pack_canonical_sha256" not in trigger_columns:
                conn.execute("ALTER TABLE workflow_triggers ADD COLUMN pack_canonical_sha256 TEXT")
            if "action_sha256" not in trigger_columns:
                conn.execute("ALTER TABLE workflow_triggers ADD COLUMN action_sha256 TEXT")
            if "action_json" not in trigger_columns:
                conn.execute("ALTER TABLE workflow_triggers ADD COLUMN action_json TEXT CHECK(action_json IS NULL OR json_valid(action_json))")
            if "context_mode" not in draft_columns:
                conn.execute("ALTER TABLE drafts ADD COLUMN context_mode TEXT NOT NULL DEFAULT 'standard'")
            if "scope" not in slice_columns:
                conn.execute("ALTER TABLE slices ADD COLUMN scope TEXT NOT NULL DEFAULT 'project'")
            if "selector_json" not in workflow_columns:
                conn.execute("ALTER TABLE workflow_instances ADD COLUMN selector_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(selector_json))")
            if "pack_canonical_sha256" not in workflow_columns:
                conn.execute("ALTER TABLE workflow_instances ADD COLUMN pack_canonical_sha256 TEXT")
            if "pack_definition_sha256" not in workflow_columns:
                conn.execute("ALTER TABLE workflow_instances ADD COLUMN pack_definition_sha256 TEXT")
            if "current_blackboard_block_id" not in workflow_columns:
                conn.execute("ALTER TABLE workflow_instances ADD COLUMN current_blackboard_block_id TEXT REFERENCES context_blocks(block_id)")
            if "state_entry_seq" not in workflow_columns:
                conn.execute("ALTER TABLE workflow_instances ADD COLUMN state_entry_seq INTEGER NOT NULL DEFAULT 0 CHECK(state_entry_seq>=0)")
            if "pack_canonical_sha256" not in transition_columns:
                conn.execute("ALTER TABLE workflow_transitions ADD COLUMN pack_canonical_sha256 TEXT")
            if "expected_workflow_revision" not in command_columns:
                conn.execute("ALTER TABLE workflow_commands ADD COLUMN expected_workflow_revision INTEGER")
            if "pack_canonical_sha256" not in command_columns:
                conn.execute("ALTER TABLE workflow_commands ADD COLUMN pack_canonical_sha256 TEXT")
            if "review_sha256" not in command_columns:
                conn.execute("ALTER TABLE workflow_commands ADD COLUMN review_sha256 TEXT")
            if "review_json" not in command_columns:
                conn.execute("ALTER TABLE workflow_commands ADD COLUMN review_json TEXT CHECK(review_json IS NULL OR json_valid(review_json))")
            if "role_session_id" not in import_columns:
                conn.execute("ALTER TABLE conversation_imports ADD COLUMN role_session_id TEXT REFERENCES role_sessions(role_session_id)")
            if "proof_limit_block_id" not in import_columns:
                conn.execute("ALTER TABLE conversation_imports ADD COLUMN proof_limit_block_id TEXT REFERENCES context_blocks(block_id)")
            if "response_state" not in import_turn_columns:
                conn.execute("ALTER TABLE conversation_import_turns ADD COLUMN response_state TEXT NOT NULL DEFAULT 'present' CHECK(response_state IN ('present','absent'))")
            if "actor_kind" not in draft_columns or "actor_kind" not in invocation_columns:
                self._migrate_actor_bindings(conn)
            self._install_change_triggers(conn)
            conn.execute("UPDATE slices SET scope='global' WHERE external_ref IS NOT NULL AND scope<>'global'")
            if conn.execute("SELECT 1 FROM schema_migrations WHERE version=4").fetchone() is None:
                for edge in conn.execute("SELECT edge_id,target_id,target_revision FROM edges WHERE target_type='external-concern'"):
                    expected_revision = sha256_bytes(str(edge["target_id"]).encode("utf-8"))
                    if edge["target_revision"] != expected_revision:
                        conn.execute("UPDATE edges SET target_revision=? WHERE edge_id=?", (expected_revision, edge["edge_id"]))
                conn.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(4,?)", (stamp(),))
            if conn.execute("SELECT 1 FROM schema_migrations WHERE version=5").fetchone() is None:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    self._migrate_role_contracts(conn)
                    conn.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(5,?)", (stamp(),))
                except BaseException:
                    conn.rollback()
                    raise
                else:
                    conn.commit()
            conn.execute("""UPDATE workflow_instances SET current_blackboard_block_id=(
                                SELECT b.block_id FROM context_blocks b JOIN block_status s USING(block_id)
                                JOIN events e ON e.subject_type='context-block' AND e.subject_id=b.block_id AND e.event_type='context-block.created'
                                WHERE b.project_id=workflow_instances.project_id AND b.semantic_type='workflow-blackboard-snapshot' AND s.status='active'
                                ORDER BY e.seq DESC LIMIT 1
                            ) WHERE current_blackboard_block_id IS NULL""")
            conn.execute("""UPDATE workflow_instances SET state_entry_seq=COALESCE(
                                (SELECT MAX(e.seq) FROM events e WHERE e.project_id=workflow_instances.project_id AND e.event_type='workflow.transitioned' AND json_extract(e.payload_json,'$.to')=workflow_instances.state),
                                (SELECT MAX(e.seq) FROM events e WHERE e.project_id=workflow_instances.project_id AND e.event_type IN ('workflow.instance-created','workflow.instance-reconciled')),
                                0
                            ) WHERE state_entry_seq=0""")
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(1,?)",
                (stamp(),),
            )
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(2,?)", (stamp(),))
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(3,?)", (stamp(),))
            self._backfill_change_ledger(conn)
            if conn.execute("SELECT 1 FROM schema_migrations WHERE version=6").fetchone() is None:
                self._backfill_integrity_chain(conn)
                conn.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(6,?)", (stamp(),))
            if conn.execute("SELECT 1 FROM schema_migrations WHERE version=7").fetchone() is None:
                self._baseline_current_views(conn)
                conn.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(7,?)", (stamp(),))
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(8,?)", (stamp(),))
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(?,?)", (SCHEMA_VERSION, stamp()))
            self._verify_integrity_chain(conn)
            self._verify_current_views_against_change_ledger(conn)

    def _migrate_actor_bindings(self, conn: sqlite3.Connection) -> None:
        """Atomically add direct actors without rewriting any sealed manifest bytes."""

        draft_columns = {row[1] for row in conn.execute("PRAGMA table_info(drafts)")}
        invocation_columns = {row[1] for row in conn.execute("PRAGMA table_info(invocations)")}
        if ("actor_kind" in draft_columns) != ("actor_kind" in invocation_columns):
            raise OSError("Lattice actor-binding migration is partially applied")
        if "actor_kind" in draft_columns:
            return
        if conn.execute("SELECT 1 FROM schema_migrations WHERE version=8").fetchone() is not None:
            raise OSError("Lattice schema claims actor bindings without actor-bound tables")

        draft_count = conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0]
        invocation_count = conn.execute("SELECT COUNT(*) FROM invocations").fetchone()[0]
        manifest_bytes = [(row["invocation_id"], row["manifest_json"]) for row in conn.execute("SELECT invocation_id,manifest_json FROM invocations ORDER BY invocation_id")]
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN EXCLUSIVE")
        try:
            for table in ("drafts", "invocations"):
                for operation in ("insert", "update", "delete"):
                    conn.execute(f'DROP TRIGGER IF EXISTS "ledger_{table}_{operation}"')
            conn.execute(
                """CREATE TABLE drafts_v8(
                    draft_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id),
                    actor_kind TEXT NOT NULL CHECK(actor_kind IN ('direct','role-session')),
                    role_session_id TEXT REFERENCES role_sessions(role_session_id),
                    prompt_block_id TEXT NOT NULL REFERENCES context_blocks(block_id),
                    route_json TEXT NOT NULL CHECK(json_valid(route_json)),
                    selections_json TEXT NOT NULL CHECK(json_valid(selections_json)),
                    parents_json TEXT NOT NULL CHECK(json_valid(parents_json)),
                    relation TEXT NOT NULL,
                    context_mode TEXT NOT NULL DEFAULT 'standard',
                    response_schema_json TEXT NOT NULL CHECK(json_valid(response_schema_json)),
                    revision INTEGER NOT NULL DEFAULT 1,
                    state TEXT NOT NULL,
                    invocation_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK((actor_kind='direct' AND role_session_id IS NULL) OR (actor_kind='role-session' AND role_session_id IS NOT NULL))
                ) STRICT"""
            )
            conn.execute(
                """CREATE TABLE invocations_v8(
                    invocation_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id),
                    actor_kind TEXT NOT NULL CHECK(actor_kind IN ('direct','role-session')),
                    role_session_id TEXT REFERENCES role_sessions(role_session_id),
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
                    completed_at TEXT,
                    CHECK((actor_kind='direct' AND role_session_id IS NULL) OR (actor_kind='role-session' AND role_session_id IS NOT NULL))
                ) STRICT"""
            )
            conn.execute(
                """INSERT INTO drafts_v8(
                    draft_id,project_id,actor_kind,role_session_id,prompt_block_id,route_json,selections_json,parents_json,relation,context_mode,response_schema_json,revision,state,invocation_id,created_at,updated_at
                ) SELECT draft_id,project_id,'role-session',role_session_id,prompt_block_id,route_json,selections_json,parents_json,relation,context_mode,response_schema_json,revision,state,invocation_id,created_at,updated_at FROM drafts"""
            )
            conn.execute(
                """INSERT INTO invocations_v8(
                    invocation_id,project_id,actor_kind,role_session_id,draft_id,manifest_artifact_id,manifest_sha256,manifest_json,compiled_prompt_sha256,route_json,response_schema_json,state,output_block_id,terminal_kind,created_at,completed_at
                ) SELECT invocation_id,project_id,'role-session',role_session_id,draft_id,manifest_artifact_id,manifest_sha256,manifest_json,compiled_prompt_sha256,route_json,response_schema_json,state,output_block_id,terminal_kind,created_at,completed_at FROM invocations"""
            )
            conn.execute("DROP TABLE invocations")
            conn.execute("DROP TABLE drafts")
            conn.execute("ALTER TABLE drafts_v8 RENAME TO drafts")
            conn.execute("ALTER TABLE invocations_v8 RENAME TO invocations")
            conn.execute("CREATE INDEX invocations_project_created ON invocations(project_id,created_at)")
            self._install_change_triggers(conn)
            for table in ("drafts", "invocations"):
                _columns, primary = self._table_contract(conn, table)
                for row in conn.execute(f'SELECT * FROM "{table}"'):
                    value = dict(row)
                    conn.execute(
                        "INSERT INTO ledger_changes(change_id,table_name,operation,primary_key_json,row_json,occurred_at) VALUES(?,?,'baseline',?,?,?)",
                        (opaque_id("change"), table, canonical_bytes({key: value[key] for key in primary}).decode(), canonical_bytes(value).decode(), stamp()),
                    )
            if conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] != draft_count or conn.execute("SELECT COUNT(*) FROM invocations").fetchone()[0] != invocation_count:
                raise OSError("Lattice actor-binding migration changed row counts")
            migrated_manifests = [(row["invocation_id"], row["manifest_json"]) for row in conn.execute("SELECT invocation_id,manifest_json FROM invocations ORDER BY invocation_id")]
            if migrated_manifests != manifest_bytes:
                raise OSError("Lattice actor-binding migration changed sealed manifest bytes")
            foreign_key_errors = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
            if foreign_key_errors:
                raise OSError(f"Lattice actor-binding migration broke foreign keys: {foreign_key_errors[:5]}")
            self._verify_integrity_chain(conn)
            self._verify_current_views_against_change_ledger(conn)
            conn.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(8,?)", (stamp(),))
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

    def _open_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.config.database, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        if sys.platform == "darwin":
            conn.execute("PRAGMA fullfsync=ON")
            conn.execute("PRAGMA checkpoint_fullfsync=ON")
            if conn.execute("PRAGMA fullfsync").fetchone()[0] != 1 or conn.execute("PRAGMA checkpoint_fullfsync").fetchone()[0] != 1:
                conn.close()
                raise OSError("SQLite could not enable macOS full-drive synchronization")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.create_function("lattice_stamp", 0, stamp)
        conn.create_function("lattice_replaying", 0, lambda: 0)
        conn.create_function("lattice_integrity_hash", -1, self._integrity_hash, deterministic=True)
        # Pre-rename databases carry these function names inside immutable SQLite
        # trigger definitions. Registering aliases keeps that historical state
        # executable without rewriting its evidence-bearing bytes.
        conn.create_function("loom_stamp", 0, stamp)
        conn.create_function("loom_replaying", 0, lambda: 0)
        conn.create_function("loom_integrity_hash", -1, self._integrity_hash, deterministic=True)
        return conn

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Own exactly one SQLite connection and close it at the lexical boundary."""

        conn = self._open_connection()
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _integrity_hash(*values: Any) -> str:
        return sha256_bytes(canonical_bytes(list(values)))

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
                conn.execute(f'DROP TRIGGER IF EXISTS "ledger_{table}_{operation}"')
                conn.execute(f'''CREATE TRIGGER "ledger_{table}_{operation}" AFTER {operation.upper()} ON "{table}" WHEN lattice_replaying()=0 BEGIN
                    INSERT INTO ledger_changes(change_id,table_name,operation,primary_key_json,row_json,occurred_at)
                    VALUES(lower(hex(randomblob(16))),'{table}','{operation}',json_object({pk_args}),{row_expr},lattice_stamp());
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

    @classmethod
    def _baseline_current_views(cls, conn: sqlite3.Connection) -> None:
        """Append one complete-schema row image for every extant derived object."""

        for table in CHANGE_TABLES:
            _columns, primary = cls._table_contract(conn, table)
            for row in conn.execute(f'SELECT * FROM "{table}"'):
                value = dict(row)
                conn.execute(
                    "INSERT INTO ledger_changes(change_id,table_name,operation,primary_key_json,row_json,occurred_at) VALUES(?,?,'baseline',?,?,?)",
                    (opaque_id("change"), table, canonical_bytes({key: value[key] for key in primary}).decode(), canonical_bytes(value).decode(), stamp()),
                )

    @classmethod
    def _backfill_integrity_chain(cls, conn: sqlite3.Connection) -> None:
        """Bind pre-chain history once; subsequent inserts are chained by SQLite triggers."""
        for stream, table, columns in (
            ("event", "events", ("seq", "event_id", "project_id", "event_type", "subject_type", "subject_id", "payload_json", "payload_sha256", "occurred_at")),
            ("ledger-change", "ledger_changes", ("seq", "change_id", "table_name", "operation", "primary_key_json", "row_json", "occurred_at")),
        ):
            selected = ",".join(columns)
            for row in conn.execute(f"SELECT {selected} FROM {table} WHERE seq NOT IN (SELECT source_seq FROM append_integrity WHERE stream=?) ORDER BY seq", (stream,)):
                previous = conn.execute("SELECT entry_sha256 FROM append_integrity ORDER BY seq DESC LIMIT 1").fetchone()
                prev_sha256 = previous[0] if previous else "0" * 64
                values = tuple(row[name] for name in columns)
                conn.execute(
                    "INSERT INTO append_integrity(stream,source_seq,prev_sha256,entry_sha256,created_at) VALUES(?,?,?,?,?)",
                    (stream, row["seq"], prev_sha256, cls._integrity_hash(stream, prev_sha256, *values), stamp()),
                )

    @classmethod
    def _verify_integrity_chain(cls, conn: sqlite3.Connection) -> None:
        missing = [tuple(row) for row in conn.execute(
            """SELECT 'event',e.seq FROM events e LEFT JOIN append_integrity a ON a.stream='event' AND a.source_seq=e.seq WHERE a.seq IS NULL
               UNION ALL
               SELECT 'ledger-change',l.seq FROM ledger_changes l LEFT JOIN append_integrity a ON a.stream='ledger-change' AND a.source_seq=l.seq WHERE a.seq IS NULL
               ORDER BY 1,2 LIMIT 5"""
        )]
        dangling = [tuple(row) for row in conn.execute(
            """SELECT a.stream,a.source_seq FROM append_integrity a LEFT JOIN events e ON a.stream='event' AND e.seq=a.source_seq
               LEFT JOIN ledger_changes l ON a.stream='ledger-change' AND l.seq=a.source_seq
               WHERE (a.stream='event' AND e.seq IS NULL) OR (a.stream='ledger-change' AND l.seq IS NULL)
               ORDER BY a.stream,a.source_seq LIMIT 5"""
        )]
        if missing or dangling:
            raise OSError(f"Lattice append history failed source coverage: missing={missing}, dangling={dangling}")
        previous = "0" * 64
        chain_rows = conn.execute(
            """SELECT a.*,
                      e.seq AS event_seq,e.event_id,e.project_id AS event_project_id,e.event_type,e.subject_type,e.subject_id,e.payload_json,e.payload_sha256,e.occurred_at AS event_occurred_at,
                      l.seq AS change_seq,l.change_id,l.table_name,l.operation,l.primary_key_json,l.row_json,l.occurred_at AS change_occurred_at
               FROM append_integrity a
               LEFT JOIN events e ON a.stream='event' AND e.seq=a.source_seq
               LEFT JOIN ledger_changes l ON a.stream='ledger-change' AND l.seq=a.source_seq
               ORDER BY a.seq"""
        )
        for source_row in chain_rows:
            chain = dict(source_row)
            if chain["prev_sha256"] != previous:
                raise OSError(f"Lattice append history chain is broken at integrity row {chain['seq']}")
            if chain["stream"] == "event":
                source = tuple(chain[key] for key in ("event_seq", "event_id", "event_project_id", "event_type", "subject_type", "subject_id", "payload_json", "payload_sha256", "event_occurred_at"))
            else:
                source = tuple(chain[key] for key in ("change_seq", "change_id", "table_name", "operation", "primary_key_json", "row_json", "change_occurred_at"))
            expected_hash = cls._integrity_hash(chain["stream"], previous, *source)
            if chain["entry_sha256"] != expected_hash:
                raise OSError(f"Lattice append history content hash is invalid at integrity row {chain['seq']}")
            previous = chain["entry_sha256"]

    @classmethod
    def _verify_current_views_against_change_ledger(cls, conn: sqlite3.Connection, tables: tuple[str, ...] = CHANGE_TABLES) -> None:
        contracts = {table: cls._table_contract(conn, table) for table in tables}
        unsupported = conn.execute("SELECT DISTINCT table_name FROM ledger_changes WHERE table_name NOT IN ({})".format(",".join("?" for _ in tables)), tables).fetchone()
        if unsupported is not None:
            raise OSError(f"Lattice change ledger names unsupported table: {unsupported[0]}")
        for table in tables:
            _columns, primary_columns = contracts[table]
            expected_count = 0
            changes = conn.execute(
                """SELECT l.operation,l.primary_key_json,l.row_json FROM ledger_changes l
                   JOIN (SELECT primary_key_json,MAX(seq) AS seq FROM ledger_changes WHERE table_name=? GROUP BY primary_key_json) latest
                     ON latest.seq=l.seq
                   WHERE l.table_name=?""",
                (table, table),
            )
            for change in changes:
                primary = json.loads(change["primary_key_json"])
                if not isinstance(primary, dict) or len(primary) != len(primary_columns) or set(primary) != set(primary_columns):
                    raise OSError(f"Lattice change ledger has an invalid primary identity for {table}")
                predicate = " AND ".join(f'"{key}"=?' for key in primary_columns)
                parameters = tuple(primary[key] for key in primary_columns)
                actual = conn.execute(f'SELECT * FROM "{table}" WHERE {predicate}', parameters).fetchone()
                if change["operation"] == "delete":
                    if change["row_json"] is not None:
                        raise OSError(f"Lattice delete ledger row unexpectedly carries state for {table}")
                    if actual is not None:
                        raise OSError(f"Lattice current view diverges from its append-only change ledger: {table}")
                    continue
                if change["row_json"] is None:
                    raise OSError(f"Lattice change ledger row lacks state for {table}")
                row = json.loads(change["row_json"])
                if not isinstance(row, dict) or any(row.get(key) != primary[key] for key in primary_columns):
                    raise OSError(f"Lattice change ledger row disagrees with its primary identity for {table}")
                expected_count += 1
                if actual is None or dict(actual) != row:
                    raise OSError(f"Lattice current view diverges from its append-only change ledger: {table}")
            actual_count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            if expected_count != actual_count:
                raise OSError(f"Lattice current view diverges from its append-only change ledger: {table}")

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
            raise LatticeNotFound(label)
        return dict(row)

    @staticmethod
    def _decode_role(role: dict[str, Any]) -> dict[str, Any]:
        role["incentives"] = json.loads(role.pop("incentives_json"))
        role["authority_limits"] = json.loads(role.pop("authority_limits_json"))
        role["route_policy"] = json.loads(role.pop("route_policy_json"))
        return role

    @staticmethod
    def _role_contract_bytes(*, name: str, instructions: str, incentives: list[str], authority_limits: list[str]) -> bytes:
        return canonical_bytes({"kind": "lattice-role-contract-v1", "name": name, "instructions": instructions, "incentives": incentives, "authority_limits": authority_limits})

    def _migrate_role_contracts(self, conn: sqlite3.Connection) -> None:
        """Move legacy instruction-only roles to complete future-facing role contracts without rewriting old manifests."""
        rows = conn.execute("""SELECT r.*,b.content_sha256,b.byte_count,s.revision AS status_revision
                               FROM roles r JOIN context_blocks b ON b.block_id=r.instructions_block_id
                               JOIN block_status s ON s.block_id=b.block_id""").fetchall()
        for row in rows:
            old_bytes = self.blobs.get(row["content_sha256"])
            try:
                decoded = old_bytes.decode("utf-8", errors="strict")
            except UnicodeDecodeError as error:
                raise LatticeConflict(f"legacy role instruction is not exact UTF-8: {row['role_id']}") from error
            incentives = json.loads(row["incentives_json"])
            authority_limits = json.loads(row["authority_limits_json"])
            try:
                existing_contract = json.loads(decoded)
            except json.JSONDecodeError:
                existing_contract = None
            if isinstance(existing_contract, dict) and existing_contract.get("kind") == "lattice-role-contract-v1" and isinstance(existing_contract.get("instructions"), str):
                instructions = existing_contract["instructions"]
            else:
                instructions = decoded
            expected = self._role_contract_bytes(name=row["name"], instructions=instructions, incentives=incentives, authority_limits=authority_limits)
            if old_bytes == expected:
                continue
            block_id = opaque_id("block")
            digest = self.blobs.put(expected)
            now = stamp()
            transformation = {"kind": "role-contract-upgrade-v1", "source_block_ids": [row["instructions_block_id"]]}
            conn.execute("""INSERT INTO context_blocks(block_id,project_id,semantic_type,content_sha256,byte_count,initial_status,authority,evidence_level,sensitivity,transformation_json,created_at)
                          VALUES(?,?,?,?,?,'active','lattice-migration','governing-instruction','private',?,?)""", (block_id, row["project_id"], "role-instruction", digest, len(expected), canonical_bytes(transformation).decode(), now))
            event_id = self._event(conn, project_id=row["project_id"], event_type="context-block.created", subject_type="context-block", subject_id=block_id, payload={"sha256": digest, "semantic_type": "role-instruction", "status": "active", "migration": "role-contract-v1"})
            conn.execute("INSERT INTO block_status(block_id,status,revision,event_id) VALUES(?,'active',1,?)", (block_id, event_id))
            self._insert_edge(conn, project_id=row["project_id"], relation=EdgeRelation.derived_from, source_type="context-block", source_id=block_id, source_revision=digest, target_type="context-block", target_id=row["instructions_block_id"], target_revision=row["content_sha256"], metadata={"migration": "role-contract-v1"})
            old_revision = int(row["status_revision"]) + 1
            status_event = self._event(conn, project_id=row["project_id"], event_type="context-block.status", subject_type="context-block", subject_id=row["instructions_block_id"], payload={"status": "superseded", "revision": old_revision, "superseded_by": block_id})
            conn.execute("UPDATE block_status SET status='superseded',revision=?,event_id=? WHERE block_id=?", (old_revision, status_event, row["instructions_block_id"]))
            conn.execute("UPDATE roles SET instructions_block_id=? WHERE role_id=?", (block_id, row["role_id"]))
            self._event(conn, project_id=row["project_id"], event_type="role.contract-upgraded", subject_type="role", subject_id=row["role_id"], payload={"previous_instructions_block_id": row["instructions_block_id"], "instructions_block_id": block_id})

    @staticmethod
    def _workflow_route_policy(spec: dict[str, Any]) -> dict[str, Any]:
        if "provider" not in spec:
            return {"kind": "operator-selectable"}
        if spec["provider"] in {"local", "operator"}:
            return {"kind": "non-model", "provider": spec["provider"]}
        return {"kind": "model", "provider": spec["provider"], "harness": spec["harness"], "allowed_models": spec.get("allowed_models", [])}

    def _assert_actor_route_authorized(
        self,
        actor_kind: str,
        role_session_id: str | None,
        route: dict[str, Any],
        parent_invocation_ids: list[str] | tuple[str, ...] = (),
    ) -> dict[str, Any]:
        if actor_kind == "direct":
            if role_session_id is not None:
                raise LatticeConflict("direct actor bindings cannot carry a role session")
        elif actor_kind == "role-session":
            if role_session_id is None:
                raise LatticeConflict("role-session actor bindings require a role session")
            session = self.get_role_session(role_session_id)
            role = self.get_role(session["role_id"])
            policy = role["route_policy"]
            if policy["kind"] == "non-model":
                raise LatticeConflict(f"{role['name']} is not a model-execution role")
            if policy["kind"] == "model":
                if route["provider"] != policy["provider"] or route["harness"] != policy["harness"]:
                    raise LatticeConflict(f"{role['name']} is bound to {policy['provider']} through {policy['harness']}")
                if policy["allowed_models"] and route["model"] not in policy["allowed_models"]:
                    raise LatticeConflict(f"{role['name']} does not authorize model {route['model']}")
        else:
            raise LatticeConflict(f"unsupported actor binding: {actor_kind}")
        adapter = next((item for item in DEFAULT_ADAPTERS if item.provider_code == route["provider"] and item.cli == route["harness"]), None)
        if adapter is None:
            raise LatticeConflict("provider and official CLI route are not qualified together")
        normalized = dict(route)
        execution_profile = normalized.get("execution_profile", "native-continuation" if normalized.get("session_policy") == "visible_native_continuation" else "compiled-fresh")
        native_parent = None
        if execution_profile == "native-continuation":
            native_parent_id = normalized.get("native_parent_invocation_id")
            if native_parent_id not in parent_invocation_ids:
                raise LatticeConflict("native continuation parent must be explicitly selected as invocation ancestry")
            native_parent = self.get_invocation(native_parent_id)
            if native_parent["state"] != InvocationState.succeeded.value:
                raise LatticeConflict("native continuation parent must have a successful terminal result")
            successful_attempts = [attempt for attempt in native_parent["attempts"] if attempt["state"] == AttemptState.succeeded.value and attempt.get("runtime_receipt")]
            receipt = successful_attempts[-1]["runtime_receipt"] if successful_attempts else None
            native_session = receipt.get("native_session") if isinstance(receipt, dict) else None
            observed_session_id = native_session.get("id") if isinstance(native_session, dict) else None
            if not isinstance(observed_session_id, str):
                raise LatticeConflict("native continuation parent has no observed provider session identity")
            supplied_session_id = normalized.get("native_session_id")
            if supplied_session_id is not None and supplied_session_id != observed_session_id:
                raise LatticeConflict("caller-supplied native session identity disagrees with the parent receipt")
            normalized["native_session_id"] = observed_session_id
        try:
            effective_policy = validate_execution_policy(adapter, **model_runner_policy(normalized))
        except SubscriptionRouteUnavailable as error:
            raise LatticeConflict(str(error)) from error
        for key in ("execution_profile", "session_policy", "continuation_mode", "native_session_id", "tools", "permissions", "workspace", "sandbox", "approval_policy", "network_policy"):
            normalized[key] = effective_policy[key]
        if native_parent is not None:
            if any(normalized[key] != native_parent["route"].get(key) for key in ("provider", "model", "harness")):
                raise LatticeConflict("native continuation cannot cross provider, model, or harness boundaries")
            if native_parent["route"].get("execution_profile", "compiled-fresh") not in {"workspace-agent", "native-continuation"}:
                raise LatticeConflict("native continuation parent did not create persistent provider state")
            authority_keys = ("tools", "permissions", "workspace", "sandbox", "approval_policy", "network_policy")
            parent_capabilities = native_parent["manifest"].get("capabilities", {})
            changed = [key for key in authority_keys if effective_policy.get(key) != parent_capabilities.get(key)]
            if changed:
                raise LatticeConflict(f"native continuation cannot cross authority boundaries: {', '.join(changed)}")
        return normalized

    def validate_invocation_intent(self, assembly: Any, route: Any) -> dict[str, Any]:
        """Validate a stateless preflight without creating any ledger objects."""

        project = self.get_project(assembly.project_id)
        actor = assembly.actor_binding.model_dump(mode="json")
        role_session_id = actor.get("role_session_id")
        normalized_route = self._assert_actor_route_authorized(actor["kind"], role_session_id, route.model_dump(mode="json"), assembly.parent_invocation_ids)
        if project["workflow_pack"] is not None and actor["kind"] == "direct":
            # Direct conversation may coexist with a workflow project, but it
            # cannot impersonate a named workflow actor or satisfy role gates.
            pass
        for parent_id in assembly.parent_invocation_ids:
            parent = self.get_invocation(parent_id)
            if parent["project_id"] != assembly.project_id:
                raise LatticeConflict("parent invocation does not belong to project")
            if not parent.get("output_block_id"):
                raise LatticeConflict(f"parent {parent_id} has no terminal output to consume")
        for selection in assembly.selections:
            if assembly.context_mode == "explicit" and selection.plane == "current_prompt":
                continue
            block = self.get_block(selection.block_id, include_content=False)
            if block["project_id"] != assembly.project_id:
                raise LatticeConflict("context block belongs to another project")
        return normalized_route

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
            project["roles"] = [self._decode_role(dict(row)) for row in conn.execute("SELECT * FROM roles WHERE project_id=? ORDER BY created_at", (project_id,))]
            project["sessions"] = [dict(row) for row in conn.execute("SELECT * FROM role_sessions WHERE project_id=? ORDER BY created_at", (project_id,))]
            project["context_defaults"] = [dict(row) for row in conn.execute("SELECT * FROM project_context_defaults WHERE project_id=? AND active=1 ORDER BY module_id,block_id", (project_id,))]
            workflow = conn.execute("SELECT * FROM workflow_instances WHERE project_id=?", (project_id,)).fetchone()
            project["workflow"] = dict(workflow) if workflow else None
            if project["workflow"]:
                project["workflow"]["automatic_grants"] = json.loads(project["workflow"].pop("automatic_grants_json"))
                project["workflow"]["selector"] = json.loads(project["workflow"].pop("selector_json"))
                project["workflow"]["triggers"] = [dict(row) for row in conn.execute("SELECT * FROM workflow_triggers WHERE project_id=? ORDER BY created_at", (project_id,))]
                for trigger in project["workflow"]["triggers"]:
                    trigger["action"] = json.loads(trigger.pop("action_json")) if trigger.get("action_json") else None
                blackboard_id = project["workflow"].get("current_blackboard_block_id")
            project["trailheads"] = [dict(row) for row in conn.execute(
                """SELECT i.invocation_id,i.project_id,i.actor_kind,i.role_session_id,i.state,i.manifest_sha256,i.output_block_id,i.terminal_kind,i.created_at,i.completed_at FROM invocations i
                   WHERE i.project_id=? AND NOT EXISTS(
                     SELECT 1 FROM edges e WHERE e.source_type='invocation'
                     AND e.target_type='invocation' AND e.target_id=i.invocation_id)
                   ORDER BY i.created_at DESC,i.invocation_id DESC LIMIT 3""", (project_id,))]
            workflow_templates = [dict(row) for row in conn.execute(
                """SELECT artifact_id,artifact_kind,content_sha256,created_at FROM artifacts
                   WHERE project_id=? AND artifact_kind='workflow-template'
                   ORDER BY created_at DESC,artifact_id DESC LIMIT 101""",
                (project_id,),
            )]
            project["workflow_templates"] = workflow_templates[:100]
            project["workflow_templates_partial"] = len(workflow_templates) > 100
        if project["workflow"] and blackboard_id:
            # Workflow snapshots can be many megabytes. Carry exact identity and
            # size in the project view; retrieve private bytes only on inspection.
            project["workflow"]["blackboard"] = self.get_block(blackboard_id, include_content=False)
        return project

    def project_enduring_graph(self, project_id: str) -> dict[str, Any]:
        """Return the bounded enduring identities Axial may mirror from Lattice."""

        with self._connect() as conn:
            project = self._row(conn.execute("SELECT project_id,title,purpose,workflow_pack,created_at FROM projects WHERE project_id=?", (project_id,)).fetchone(), project_id)
            rows = [dict(row) for row in conn.execute(
                """SELECT e.edge_id,e.source_type,e.source_id,e.source_revision,e.target_id,e.target_revision,e.metadata_json,s.name AS slice_name
                   FROM edges e LEFT JOIN slices s ON s.external_ref=e.target_id
                   WHERE e.project_id=? AND e.relation='concerns' AND e.target_type='external-concern'
                   ORDER BY e.target_id,e.edge_id LIMIT ?""",
                (project_id, MAX_AXIAL_SOURCE_CLAIMS + 1),
            )]
        if len(rows) > MAX_AXIAL_SOURCE_CLAIMS:
            raise LatticeConflict(f"Axial projection exceeds its {MAX_AXIAL_SOURCE_CLAIMS}-claim limit")
        grouped: dict[str, dict[str, Any]] = {}
        claims: list[dict[str, str]] = []
        for row in rows:
            metadata = json.loads(row["metadata_json"])
            ref = row["target_id"]
            prefix = ref.split(":", 1)[0] if ":" in ref else "concern"
            kind = prefix if prefix in {"project", "topic", "person", "slate"} else "concern"
            fallback = ref.split(":", 1)[-1].replace("-", " ")
            value = grouped.setdefault(ref, {"external_ref": ref, "label": row["slice_name"] or metadata.get("label") or fallback, "kind": kind})
            if len(grouped) > MAX_AXIAL_CONCERNS:
                raise LatticeConflict(f"Axial projection exceeds its {MAX_AXIAL_CONCERNS}-concern limit")
            if row["slice_name"]:
                value["label"] = row["slice_name"]
            claims.append({key: str(row[key]) for key in ("edge_id", "source_type", "source_id", "source_revision", "target_id", "target_revision")})
        material = {"contract_version": "LatticeAxialProjection/1.0", "project": project, "concerns": [grouped[key] for key in sorted(grouped)], "source_claims": claims}
        encoded = canonical_bytes(material)
        if len(encoded) > MAX_AXIAL_PROJECTION_BYTES:
            raise LatticeConflict(f"Axial projection exceeds its {MAX_AXIAL_PROJECTION_BYTES}-byte limit")
        return {**material, "projection_sha256": sha256_bytes(encoded)}

    def list_projects(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM projects ORDER BY created_at DESC")]

    def reconcile_workflow_instances(self, initial_states: dict[str, str]) -> int:
        """Bind pre-runtime workflow projects without guessing domain state from their blackboards."""
        repaired = 0
        with self.transaction() as conn:
            missing = conn.execute("""SELECT p.project_id,p.workflow_pack FROM projects p
                                      LEFT JOIN workflow_instances w USING(project_id)
                                      WHERE p.workflow_pack IS NOT NULL AND w.project_id IS NULL""").fetchall()
            for row in missing:
                initial_state = initial_states.get(row["workflow_pack"])
                if initial_state is None:
                    continue
                now = stamp()
                conn.execute("INSERT INTO workflow_instances(project_id,pack_id,state,revision,automatic_grants_json,updated_at) VALUES(?,?,?,1,'[]',?)", (row["project_id"], row["workflow_pack"], initial_state, now))
                entry_event = self._event(conn, project_id=row["project_id"], event_type="workflow.instance-reconciled", subject_type="workflow-instance", subject_id=row["project_id"], payload={"pack_id": row["workflow_pack"], "state": initial_state, "revision": 1, "reason": "legacy-project-runtime-binding"})
                entry_seq = conn.execute("SELECT seq FROM events WHERE event_id=?", (entry_event,)).fetchone()[0]
                conn.execute("UPDATE workflow_instances SET state_entry_seq=? WHERE project_id=?", (entry_seq, row["project_id"]))
                repaired += 1
        return repaired

    def reconcile_workflow_role_policies(self, pack_contracts: dict[str, dict[str, Any]]) -> int:
        """Bind only roles whose workflow instance already proves the exact pack generation."""
        repaired = 0
        with self.transaction() as conn:
            rows = conn.execute("""SELECT r.*,p.workflow_pack,w.pack_canonical_sha256
                                   FROM roles r JOIN projects p USING(project_id)
                                   JOIN workflow_instances w USING(project_id)
                                   WHERE p.workflow_pack IS NOT NULL AND r.workflow_role_key IS NULL""").fetchall()
            for row in rows:
                contract = pack_contracts.get(row["workflow_pack"])
                matches = [item for item in (contract or {}).get("roles", []) if item["name"] == row["name"]]
                generation_proven = contract is not None and row["pack_canonical_sha256"] == contract["canonical_sha256"]
                if generation_proven and len(matches) == 1:
                    role_spec = matches[0]
                    workflow_role_key = role_spec["role_id"]
                    route_policy = self._workflow_route_policy(role_spec)
                    event_type = "role.workflow-policy-reconciled"
                    reason = "exact-pack-generation-and-unique-role-name"
                else:
                    workflow_role_key = None
                    route_policy = {"kind": "non-model", "provider": "legacy-unbound"}
                    event_type = "role.workflow-policy-unbound"
                    reason = "legacy-role-lacks-exact-pack-generation" if not generation_proven else "legacy-role-name-is-not-unique"
                if json.loads(row["route_policy_json"]) == route_policy and row["workflow_role_key"] == workflow_role_key:
                    continue
                conn.execute("UPDATE roles SET workflow_role_key=?,route_policy_json=? WHERE role_id=?", (workflow_role_key, canonical_bytes(route_policy).decode(), row["role_id"]))
                self._event(conn, project_id=row["project_id"], event_type=event_type, subject_type="role", subject_id=row["role_id"], payload={"workflow_role_key": workflow_role_key, "route_policy": route_policy, "pack_id": row["workflow_pack"], "reason": reason})
                repaired += 1
        return repaired

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
        selector: dict[str, Any] | None = None,
        context_modules: list[dict[str, Any]] | None = None,
        pack_canonical_sha256: str | None = None,
        pack_definition_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Install a workflow project and its first authoritative snapshot as one ledger transition."""
        project_id = opaque_id("project")
        now = stamp()
        blackboard_id = opaque_id("block")
        blackboard_sha = self.blobs.put(blackboard_content)
        prepared_roles = []
        for spec in role_specs:
            instructions = self._role_contract_bytes(name=spec["name"], instructions=spec["instructions"], incentives=[], authority_limits=[spec["authority"]])
            prepared_roles.append((spec, opaque_id("role"), opaque_id("session"), opaque_id("block"), self.blobs.put(instructions), instructions))
        prepared_modules = []
        for module in context_modules or []:
            content = module["content"]
            prepared_modules.append((module, opaque_id("block"), self.blobs.put(content)))
        slice_id = opaque_id("slice") if slice_external_ref else None
        with self.transaction() as conn:
            conn.execute("INSERT INTO projects(project_id,title,purpose,workflow_pack,created_at) VALUES(?,?,?,?,?)", (project_id, title, purpose, workflow_pack, now))
            self._event(conn, project_id=project_id, event_type="project.created", subject_type="project", subject_id=project_id, payload={"title": title, "purpose": purpose, "workflow_pack": workflow_pack})
            if pack_definition_sha256 is not None and pack_definition_sha256 != pack_canonical_sha256:
                raise LatticeConflict("workflow pack definition bytes do not match its canonical identity")
            conn.execute("INSERT INTO workflow_instances(project_id,pack_id,state,revision,automatic_grants_json,selector_json,pack_canonical_sha256,pack_definition_sha256,updated_at) VALUES(?,?,?,1,'[]',?,?,?,?)", (project_id, workflow_pack, initial_state, canonical_bytes(selector or {}).decode(), pack_canonical_sha256, pack_definition_sha256, now))
            entry_event = self._event(conn, project_id=project_id, event_type="workflow.instance-created", subject_type="workflow-instance", subject_id=project_id, payload={"pack_id": workflow_pack, "pack_canonical_sha256": pack_canonical_sha256, "pack_definition_ref": f"lattice:blob/{pack_definition_sha256}" if pack_definition_sha256 else None, "state": initial_state, "revision": 1, "automatic_grants": []})
            entry_seq = conn.execute("SELECT seq FROM events WHERE event_id=?", (entry_event,)).fetchone()[0]
            conn.execute("UPDATE workflow_instances SET state_entry_seq=? WHERE project_id=?", (entry_seq, project_id))
            for spec, role_id, session_id, instruction_id, instruction_sha, instructions in prepared_roles:
                conn.execute("INSERT INTO context_blocks(block_id,project_id,semantic_type,content_sha256,byte_count,initial_status,authority,evidence_level,sensitivity,created_at) VALUES(?,?,?,?,?,'active','workflow-pack','governing-instruction','private',?)", (instruction_id, project_id, "role-instruction", instruction_sha, len(instructions), now))
                instruction_event = self._event(conn, project_id=project_id, event_type="context-block.created", subject_type="context-block", subject_id=instruction_id, payload={"sha256": instruction_sha, "semantic_type": "role-instruction", "status": "active"})
                conn.execute("INSERT INTO block_status(block_id,status,revision,event_id) VALUES(?,'active',1,?)", (instruction_id, instruction_event))
                route_policy = self._workflow_route_policy(spec)
                conn.execute("INSERT INTO roles(role_id,project_id,name,instructions_block_id,incentives_json,authority_limits_json,workflow_role_key,route_policy_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (role_id, project_id, spec["name"], instruction_id, "[]", canonical_bytes([spec["authority"]]).decode(), spec.get("role_id"), canonical_bytes(route_policy).decode(), now))
                self._event(conn, project_id=project_id, event_type="role.created", subject_type="role", subject_id=role_id, payload={"name": spec["name"], "instructions_block_id": instruction_id, "workflow_role_key": spec.get("role_id"), "route_policy": route_policy})
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
                if definition["mode"] in {"project_default", "workflow_required"}:
                    conn.execute("INSERT INTO project_context_defaults(project_id,block_id,module_id,mode,active) VALUES(?,?,?,?,1)", (project_id, block_id, definition["module_id"], definition["mode"]))
                if definition["mode"] in {"role_default", "workflow_required"}:
                    for _spec, role_id, *_rest in prepared_roles:
                        conn.execute("INSERT INTO role_context_defaults(role_id,block_id,module_id,mode,active) VALUES(?,?,?,?,1)", (role_id, block_id, definition["module_id"], definition["mode"]))
            conn.execute("""INSERT INTO context_blocks(block_id,project_id,semantic_type,content_sha256,byte_count,initial_status,source_ref,source_sha256,authority,evidence_level,sensitivity,created_at)
                         VALUES(?,?,?,?,?,'active',?,?,?,'authoritative-read','private',?)""", (blackboard_id, project_id, "workflow-blackboard-snapshot", blackboard_sha, len(blackboard_content), blackboard_source_ref, blackboard_sha, workflow_pack, now))
            blackboard_event = self._event(conn, project_id=project_id, event_type="context-block.created", subject_type="context-block", subject_id=blackboard_id, payload={"sha256": blackboard_sha, "semantic_type": "workflow-blackboard-snapshot", "status": "active"})
            conn.execute("INSERT INTO block_status(block_id,status,revision,event_id) VALUES(?,'active',1,?)", (blackboard_id, blackboard_event))
            conn.execute("UPDATE workflow_instances SET current_blackboard_block_id=? WHERE project_id=?", (blackboard_id, project_id))
            if slice_external_ref and slice_id:
                self._insert_edge(conn, project_id=project_id, relation=EdgeRelation.concerns, source_type="context-block", source_id=blackboard_id, source_revision=blackboard_sha, target_type="external-concern", target_id=slice_external_ref, target_revision=sha256_bytes(slice_external_ref.encode("utf-8")), metadata={"workflow_pack": workflow_pack})
                query = {"concern_ref": slice_external_ref, "object_types": ["invocation", "context-block"]}
                conn.execute("INSERT INTO slices(slice_id,project_id,scope,name,query_json,external_ref,created_at,updated_at) VALUES(?,?,'global',?,?,?,?,?)", (slice_id, project_id, title, canonical_bytes(query).decode(), slice_external_ref, now, now))
                self._event(conn, project_id=project_id, event_type="slice.created", subject_type="slice", subject_id=slice_id, payload={"project_id": project_id, "name": title, "query": query, "external_ref": slice_external_ref})
        return {"project": self.get_project(project_id), "blackboard": self.get_block(blackboard_id, include_content=False), "slice": self.get_slice(slice_id) if slice_id else None}

    def list_role_context_defaults(self, role_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM role_context_defaults WHERE role_id=? AND active=1 ORDER BY module_id,block_id", (role_id,))]

    def list_project_context_defaults(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM project_context_defaults WHERE project_id=? AND active=1 ORDER BY module_id,block_id", (project_id,))]

    def set_project_context_default(self, project_id: str, block_id: str, *, module_id: str, mode: str) -> dict[str, Any]:
        if mode not in {"project_default", "workflow_required"}:
            raise LatticeConflict("only project-default or workflow-required modules can be registered on a project")
        with self.transaction() as conn:
            project = self._row(conn.execute("SELECT workflow_pack FROM projects WHERE project_id=?", (project_id,)).fetchone(), project_id)
            block = self._row(conn.execute("SELECT project_id FROM context_blocks WHERE block_id=?", (block_id,)).fetchone(), block_id)
            if block["project_id"] != project_id:
                raise LatticeConflict("context default block belongs to another project")
            if mode == "workflow_required" and project["workflow_pack"] is None:
                raise LatticeConflict("workflow-required context needs a governed workflow project")
            existing = conn.execute("SELECT block_id,mode,active FROM project_context_defaults WHERE project_id=? AND module_id=?", (project_id, module_id)).fetchone()
            if existing is None:
                conn.execute("INSERT INTO project_context_defaults(project_id,block_id,module_id,mode,active) VALUES(?,?,?,?,1)", (project_id, block_id, module_id, mode))
            elif existing["block_id"] != block_id or existing["mode"] != mode or not existing["active"]:
                conn.execute("UPDATE project_context_defaults SET block_id=?,mode=?,active=1 WHERE project_id=? AND module_id=?", (block_id, mode, project_id, module_id))
            self._event(conn, project_id=project_id, event_type="project-context.registered", subject_type="context-block", subject_id=block_id, payload={"module_id": module_id, "mode": mode, "previous_block_id": existing["block_id"] if existing else None})
        return next(item for item in self.list_project_context_defaults(project_id) if item["module_id"] == module_id)

    def remove_project_context_default(self, project_id: str, module_id: str) -> None:
        with self.transaction() as conn:
            row = self._row(conn.execute("SELECT block_id,mode,active FROM project_context_defaults WHERE project_id=? AND module_id=?", (project_id, module_id)).fetchone(), module_id)
            if row["mode"] != "project_default":
                raise LatticeConflict("workflow-required context cannot be removed")
            if not row["active"]:
                return
            conn.execute("UPDATE project_context_defaults SET active=0 WHERE project_id=? AND module_id=?", (project_id, module_id))
            self._event(conn, project_id=project_id, event_type="project-context.removed", subject_type="context-block", subject_id=row["block_id"], payload={"module_id": module_id})

    def remove_role_context_default(self, role_id: str, block_id: str) -> None:
        with self.transaction() as conn:
            row = self._row(conn.execute("SELECT r.project_id,d.mode,d.active FROM role_context_defaults d JOIN roles r USING(role_id) WHERE d.role_id=? AND d.block_id=?", (role_id, block_id)).fetchone(), block_id)
            if row["mode"] != "role_default":
                raise LatticeConflict("workflow-required context cannot be removed")
            if not row["active"]:
                return
            conn.execute("UPDATE role_context_defaults SET active=0 WHERE role_id=? AND block_id=?", (role_id, block_id))
            self._event(conn, project_id=row["project_id"], event_type="role-context.removed", subject_type="context-block", subject_id=block_id, payload={"role_id": role_id})

    def _insert_context_block(self, conn: sqlite3.Connection, value: ContextBlockCreate, *, block_id: str, content: bytes, digest: str, now: str) -> None:
        if value.producing_invocation_id is not None:
            producer = self._row(conn.execute("SELECT project_id FROM invocations WHERE invocation_id=?", (value.producing_invocation_id,)).fetchone(), value.producing_invocation_id)
            if producer["project_id"] != value.project_id:
                raise LatticeConflict("context block producer belongs to another project")
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

    def create_context_block(self, value: ContextBlockCreate) -> dict[str, Any]:
        block_id = opaque_id("block")
        content = value.content.encode("utf-8")
        digest = self.blobs.put(content)
        now = stamp()
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM projects WHERE project_id=?", (value.project_id,)).fetchone() is None:
                raise LatticeNotFound(value.project_id)
            self._insert_context_block(conn, value, block_id=block_id, content=content, digest=digest, now=now)
        return self.get_block(block_id, include_content=False)

    def create_derived_text_block(self, value: DerivedTextBlockCreate) -> dict[str, Any]:
        """Create a byte-exact UTF-8 excerpt without mutating its source block."""

        source = self.get_block(value.source_block_id)
        if source["project_id"] != value.project_id:
            raise LatticeConflict("excerpt source belongs to another project")
        source_bytes = source["content"].encode("utf-8")
        if value.end_byte > len(source_bytes):
            raise LatticeConflict("excerpt byte range exceeds its source")
        excerpt = source_bytes[value.start_byte:value.end_byte]
        try:
            excerpt_text = excerpt.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise LatticeConflict("excerpt byte range splits a UTF-8 code point") from error
        excerpt_sha = self.blobs.put(excerpt)
        transformation = {
            "kind": "exact-utf8-byte-range-v1",
            "source_block_id": value.source_block_id,
            "source_sha256": source["content_sha256"],
            "start_byte": value.start_byte,
            "end_byte": value.end_byte,
            "excerpt_sha256": excerpt_sha,
        }
        block_value = ContextBlockCreate(
            project_id=value.project_id,
            semantic_type=value.semantic_type,
            content=excerpt_text,
            source_ref=f"lattice:block/{value.source_block_id}",
            source_sha256=source["content_sha256"],
            authority="operator-derived",
            evidence_level="exact-excerpt",
            sensitivity=value.sensitivity or source["sensitivity"],
            transformation=transformation,
        )
        block_id = opaque_id("block")
        now = stamp()
        with self.transaction() as conn:
            self._insert_context_block(conn, block_value, block_id=block_id, content=excerpt, digest=excerpt_sha, now=now)
            self._insert_edge(
                conn,
                project_id=value.project_id,
                relation=EdgeRelation.derived_from,
                source_type="context-block",
                source_id=block_id,
                source_revision=excerpt_sha,
                target_type="context-block",
                target_id=value.source_block_id,
                target_revision=source["content_sha256"],
                metadata={"transformation": transformation},
            )
        return self.get_block(block_id, include_content=False)

    def get_block(self, block_id: str, *, include_content: bool = True) -> dict[str, Any]:
        with self._connect() as conn:
            row = self._row(conn.execute(
                """SELECT b.*,s.status,s.revision AS status_revision FROM context_blocks b
                   JOIN block_status s USING(block_id) WHERE block_id=?""", (block_id,)).fetchone(), block_id)
        if include_content:
            row["content"] = self.blobs.get(row["content_sha256"]).decode("utf-8")
        return row

    def get_block_status_revision(self, block_id: str, revision: int) -> str:
        if revision < 1:
            raise LatticeNotFound(f"{block_id}@{revision}")
        with self._connect() as conn:
            rows = conn.execute("""SELECT event_type,payload_json FROM events
                                   WHERE subject_type='context-block' AND subject_id=?
                                   AND event_type IN ('context-block.created','context-block.status')
                                   ORDER BY seq""", (block_id,)).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            event_revision = 1 if row["event_type"] == "context-block.created" else payload.get("revision")
            if event_revision == revision:
                status = payload.get("status")
                if not isinstance(status, str):
                    break
                return status
        raise LatticeNotFound(f"{block_id}@{revision}")

    def set_block_status(self, block_id: str, status: BlockStatus) -> dict[str, Any]:
        with self.transaction() as conn:
            row = self._row(conn.execute("SELECT b.project_id,s.revision FROM context_blocks b JOIN block_status s USING(block_id) WHERE block_id=?", (block_id,)).fetchone(), block_id)
            revision = int(row["revision"]) + 1
            event_id = self._event(conn, project_id=row["project_id"], event_type="context-block.status", subject_type="context-block", subject_id=block_id, payload={"status": status.value, "revision": revision})
            conn.execute("UPDATE block_status SET status=?,revision=?,event_id=? WHERE block_id=?", (status.value, revision, event_id, block_id))
        return self.get_block(block_id, include_content=False)

    def _insert_operator_role(
        self,
        conn: sqlite3.Connection,
        *,
        project_id: str,
        value: RoleCreate,
        now: str,
        derived_from_invocation: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        instructions = self._role_contract_bytes(name=value.name, instructions=value.instructions, incentives=value.incentives, authority_limits=value.authority_limits)
        instruction_sha = self.blobs.put(instructions)
        instruction_id = opaque_id("block")
        role_id = opaque_id("role")
        conn.execute(
            """INSERT INTO context_blocks(block_id,project_id,semantic_type,content_sha256,byte_count,initial_status,
               authority,evidence_level,sensitivity,created_at) VALUES(?,?,?,?,?,'active','workflow-or-operator','governing-instruction','private',?)""",
            (instruction_id, project_id, "role-instruction", instruction_sha, len(instructions), now),
        )
        instruction_event = self._event(conn, project_id=project_id, event_type="context-block.created", subject_type="context-block", subject_id=instruction_id, payload={"sha256": instruction_sha, "semantic_type": "role-instruction", "status": "active"})
        conn.execute("INSERT INTO block_status(block_id,status,revision,event_id) VALUES(?,'active',1,?)", (instruction_id, instruction_event))
        conn.execute(
            "INSERT INTO roles(role_id,project_id,name,instructions_block_id,incentives_json,authority_limits_json,workflow_role_key,route_policy_json,created_at) VALUES(?,?,?,?,?,?,NULL,?,?)",
            (role_id, project_id, value.name, instruction_id, canonical_bytes(value.incentives).decode(), canonical_bytes(value.authority_limits).decode(), canonical_bytes({"kind": "operator-selectable"}).decode(), now),
        )
        payload: dict[str, Any] = {"name": value.name, "instructions_block_id": instruction_id}
        if derived_from_invocation is not None:
            payload["derived_from_invocation_id"] = derived_from_invocation["invocation_id"]
            self._insert_edge(
                conn,
                project_id=project_id,
                relation=EdgeRelation.derived_from,
                source_type="context-block",
                source_id=instruction_id,
                source_revision=instruction_sha,
                target_type="invocation",
                target_id=derived_from_invocation["invocation_id"],
                target_revision=derived_from_invocation["manifest_sha256"],
                metadata={"kind": "role-demonstration"},
            )
        self._event(conn, project_id=project_id, event_type="role.created", subject_type="role", subject_id=role_id, payload=payload)
        return role_id, instruction_id

    def create_role(self, project_id: str, value: RoleCreate) -> dict[str, Any]:
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone() is None:
                raise LatticeNotFound(project_id)
            role_id, _instruction_id = self._insert_operator_role(conn, project_id=project_id, value=value, now=stamp())
        return self.get_role(role_id)

    def pin_invocation_as_role(self, invocation_id: str, value: RolePinCreate) -> dict[str, Any]:
        source = self.get_invocation(invocation_id)
        if source["state"] not in {"succeeded", "imported"}:
            raise LatticeConflict("only a completed invocation can demonstrate a role")
        role_value = RoleCreate(name=value.name, instructions=value.instructions, incentives=value.incentives, authority_limits=value.authority_limits)
        role_id = ""
        session_id = opaque_id("session")
        with self.transaction() as conn:
            role_id, _instruction_id = self._insert_operator_role(conn, project_id=source["project_id"], value=role_value, now=stamp(), derived_from_invocation=source)
            conn.execute("INSERT INTO role_sessions(role_session_id,project_id,role_id,label,created_at) VALUES(?,?,?,?,?)", (session_id, source["project_id"], role_id, value.session_label, stamp()))
            self._event(conn, project_id=source["project_id"], event_type="role-session.created", subject_type="role-session", subject_id=session_id, payload={"project_id": source["project_id"], "role_id": role_id, "label": value.session_label, "derived_from_invocation_id": invocation_id})
        return {"role": self.get_role(role_id), "session": self.get_role_session(session_id), "source_invocation_id": invocation_id}

    def get_role(self, role_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            return self._decode_role(self._row(conn.execute("SELECT * FROM roles WHERE role_id=?", (role_id,)).fetchone(), role_id))

    def find_role_by_name(self, project_id: str, name: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM roles WHERE project_id=? AND name=?", (project_id, name)).fetchone()
        return self._decode_role(dict(row)) if row else None

    def create_role_session(self, value: RoleSessionCreate) -> dict[str, Any]:
        session_id = opaque_id("session")
        with self.transaction() as conn:
            role = self._row(conn.execute("SELECT project_id FROM roles WHERE role_id=?", (value.role_id,)).fetchone(), value.role_id)
            if role["project_id"] != value.project_id:
                raise LatticeConflict("role does not belong to project")
            conn.execute("INSERT INTO role_sessions(role_session_id,project_id,role_id,label,created_at) VALUES(?,?,?,?,?)", (session_id, value.project_id, value.role_id, value.label, stamp()))
            self._event(conn, project_id=value.project_id, event_type="role-session.created", subject_type="role-session", subject_id=session_id, payload=value.model_dump(mode="json"))
        return self.get_role_session(session_id)

    def get_role_session(self, session_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            return self._row(conn.execute("SELECT * FROM role_sessions WHERE role_session_id=?", (session_id,)).fetchone(), session_id)

    def create_draft(self, value: DraftCreate, *, prompt_block_id: str | None = None, import_turn: tuple[str, int] | None = None, retarget_current_prompt_selections: bool = False, source_artifact: tuple[str, str, str] | None = None) -> dict[str, Any]:
        actor_binding = value.actor_binding.model_dump(mode="json")
        actor_kind = actor_binding["kind"]
        role_session_id = actor_binding.get("role_session_id")
        if role_session_id is not None:
            session = self.get_role_session(role_session_id)
            if session["project_id"] != value.project_id:
                raise LatticeConflict("role session does not belong to project")
        for parent_id in value.parent_invocation_ids:
            parent = self.get_invocation(parent_id)
            if parent["project_id"] != value.project_id:
                raise LatticeConflict("parent invocation does not belong to project")
        route = self._assert_actor_route_authorized(actor_kind, role_session_id, value.route.model_dump(mode="json"), value.parent_invocation_ids)
        prompt_value = None
        prompt_content = value.prompt.encode("utf-8")
        if prompt_block_id is None:
            prompt_value = ContextBlockCreate(project_id=value.project_id, semantic_type="user-prompt", content=value.prompt, evidence_level="operator-intent")
            prompt_block_id = opaque_id("block")
            prompt_digest = self.blobs.put(prompt_content)
        else:
            prompt_block = self.get_block(prompt_block_id)
            if prompt_block["project_id"] != value.project_id or prompt_block["content"] != value.prompt:
                raise LatticeConflict("reused prompt block does not match the draft project and bytes")
        selections = [item.model_dump(mode="json") for item in value.selections]
        if retarget_current_prompt_selections:
            selections = [{**item, "block_id": prompt_block_id} if item["plane"] == "current_prompt" else item for item in selections]
        draft_id = opaque_id("draft")
        now = stamp()
        with self.transaction() as conn:
            if prompt_value is not None:
                self._insert_context_block(conn, prompt_value, block_id=prompt_block_id, content=prompt_content, digest=prompt_digest, now=now)
            conn.execute(
                """INSERT INTO drafts(draft_id,project_id,actor_kind,role_session_id,prompt_block_id,route_json,selections_json,parents_json,relation,context_mode,response_schema_json,revision,state,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)""",
                (draft_id, value.project_id, actor_kind, role_session_id, prompt_block_id,
                 canonical_bytes(route).decode(), canonical_bytes(selections).decode(),
                 canonical_bytes(value.parent_invocation_ids).decode(), value.relation.value, value.context_mode, canonical_bytes(value.response_schema).decode(),
                 DraftState.editable.value, now, now),
            )
            self._event(conn, project_id=value.project_id, event_type="draft.created", subject_type="draft", subject_id=draft_id, payload={"prompt_block_id": prompt_block_id, "parents": value.parent_invocation_ids})
            if source_artifact is not None:
                artifact_id, artifact_sha256, step_id = source_artifact
                artifact = self._row(conn.execute("SELECT project_id,content_sha256 FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone(), artifact_id)
                if artifact["content_sha256"] != artifact_sha256:
                    raise LatticeConflict("workflow template changed during draft materialization")
                self._insert_edge(conn, project_id=value.project_id, relation=EdgeRelation.derived_from, source_type="draft", source_id=draft_id, source_revision="1", target_type="artifact", target_id=artifact_id, target_revision=artifact_sha256, metadata={"template_step_id": step_id, "template_source_project_id": artifact["project_id"]})
            if import_turn is not None:
                import_id, ordinal = import_turn
                changed = conn.execute("UPDATE conversation_import_turns SET draft_id=?,state='draft_created',updated_at=? WHERE import_id=? AND ordinal=? AND state='pending' AND draft_id IS NULL", (draft_id, now, import_id, ordinal)).rowcount
                if changed != 1:
                    raise LatticeConflict("conversation import turn changed before draft creation")
        return self.get_draft(draft_id)

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = self._row(conn.execute("SELECT * FROM drafts WHERE draft_id=?", (draft_id,)).fetchone(), draft_id)
        for key in ("route_json", "selections_json", "parents_json", "response_schema_json"):
            row[key.removesuffix("_json")] = json.loads(row.pop(key))
        row["actor_binding"] = {"kind": row["actor_kind"], **({"role_session_id": row["role_session_id"]} if row["role_session_id"] is not None else {})}
        return row

    def patch_draft(self, draft_id: str, patch: DraftPatch) -> dict[str, Any]:
        current = self.get_draft(draft_id)
        if current["state"] != DraftState.editable.value:
            raise LatticeConflict("sealed or abandoned drafts are immutable")
        if current["revision"] != patch.expected_revision:
            raise LatticeConflict("draft revision changed")
        prompt_block_id = current["prompt_block_id"]
        prompt_value = None
        if patch.prompt is not None:
            prompt_value = ContextBlockCreate(project_id=current["project_id"], semantic_type="user-prompt", content=patch.prompt, evidence_level="operator-intent")
            prompt_content = patch.prompt.encode("utf-8")
            prompt_digest = self.blobs.put(prompt_content)
            prompt_block_id = opaque_id("block")
        route = patch.route.model_dump(mode="json") if patch.route else current["route"]
        selections = [item.model_dump(mode="json") for item in patch.selections] if patch.selections is not None else current["selections"]
        parents = patch.parent_invocation_ids if patch.parent_invocation_ids is not None else current["parents"]
        for parent_id in parents:
            parent = self.get_invocation(parent_id)
            if parent["project_id"] != current["project_id"]:
                raise LatticeConflict("parent invocation does not belong to project")
        route = self._assert_actor_route_authorized(current["actor_kind"], current["role_session_id"], route, parents)
        if patch.prompt is not None:
            selections = [{**item, "block_id": prompt_block_id} if item["plane"] == "current_prompt" else item for item in selections]
        if current["context_mode"] == "explicit":
            prompt_selections = [item for item in selections if item["plane"] == "current_prompt"]
            if len(prompt_selections) != 1 or prompt_selections[0]["disposition"] != BlockDisposition.selected.value:
                raise LatticeConflict("explicit context requires exactly one directly selected current-prompt block")
        revision = current["revision"] + 1
        with self.transaction() as conn:
            if prompt_value is not None:
                self._insert_context_block(conn, prompt_value, block_id=prompt_block_id, content=prompt_content, digest=prompt_digest, now=stamp())
            changed = conn.execute(
                """UPDATE drafts SET prompt_block_id=?,route_json=?,selections_json=?,parents_json=?,revision=?,updated_at=?
                   WHERE draft_id=? AND state='editable' AND revision=?""",
                (prompt_block_id, canonical_bytes(route).decode(), canonical_bytes(selections).decode(), canonical_bytes(parents).decode(), revision, stamp(), draft_id, patch.expected_revision),
            ).rowcount
            if changed != 1:
                raise LatticeConflict("draft changed during update")
            self._event(conn, project_id=current["project_id"], event_type="draft.revised", subject_type="draft", subject_id=draft_id, payload={"revision": revision})
        return self.get_draft(draft_id)

    def insert_invocation(self, draft: dict[str, Any], manifest: dict[str, Any], manifest_sha: str, compiled_prompt_sha: str, *, trigger_id: str | None = None, import_turn: tuple[str, int] | None = None) -> dict[str, Any]:
        self._assert_actor_route_authorized(draft["actor_kind"], draft["role_session_id"], draft["route"], draft["parents"])
        invocation_id = manifest["invocation_id"]
        now = stamp()
        with self.transaction() as conn:
            current = self._row(conn.execute("SELECT state,revision FROM drafts WHERE draft_id=?", (draft["draft_id"],)).fetchone(), draft["draft_id"])
            if current["state"] != DraftState.editable.value or current["revision"] != draft["revision"]:
                raise LatticeConflict("draft changed before sealing")
            conn.execute(
                """INSERT INTO invocations(invocation_id,project_id,actor_kind,role_session_id,draft_id,manifest_artifact_id,manifest_sha256,manifest_json,compiled_prompt_sha256,route_json,response_schema_json,state,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (invocation_id, draft["project_id"], draft["actor_kind"], draft["role_session_id"], draft["draft_id"], manifest["generated_object"]["artifact_id"], manifest_sha,
                 canonical_bytes(manifest).decode(), compiled_prompt_sha, canonical_bytes(draft["route"]).decode(), canonical_bytes(draft["response_schema"]).decode(), InvocationState.sealed.value, now),
            )
            conn.execute("UPDATE drafts SET state='sealed',invocation_id=?,updated_at=? WHERE draft_id=?", (invocation_id, now, draft["draft_id"]))
            self._event(conn, project_id=draft["project_id"], event_type="invocation.sealed", subject_type="invocation", subject_id=invocation_id, payload={"manifest_artifact_id": manifest["generated_object"]["artifact_id"], "manifest_sha256": manifest_sha})
            for parent_id in draft["parents"]:
                self._insert_edge(conn, project_id=draft["project_id"], relation=EdgeRelation(draft["relation"]), source_type="invocation", source_id=invocation_id, source_revision=manifest_sha, target_type="invocation", target_id=parent_id, target_revision=self._invocation_revision(conn, parent_id), metadata={})
            if trigger_id is not None:
                trigger = self._row(conn.execute("SELECT * FROM workflow_triggers WHERE trigger_id=?", (trigger_id,)).fetchone(), trigger_id)
                if trigger["state"] != "pending":
                    raise LatticeConflict("workflow trigger is not pending")
                if trigger["project_id"] != draft["project_id"]:
                    raise LatticeConflict("workflow trigger belongs to another project")
                self._assert_trigger_invocation_role(conn, trigger, draft["role_session_id"])
                conn.execute("UPDATE workflow_triggers SET state='planned',result_ref=?,error_text=NULL WHERE trigger_id=?", (invocation_id, trigger_id))
                self._insert_edge(conn, project_id=draft["project_id"], relation=EdgeRelation.triggered_by, source_type="invocation", source_id=invocation_id, source_revision=manifest_sha, target_type="workflow-trigger", target_id=trigger_id, target_revision=trigger["source_transition_id"], metadata={"blinded": True})
                self._event(conn, project_id=draft["project_id"], event_type="workflow.trigger-planned", subject_type="workflow-trigger", subject_id=trigger_id, payload={"result_ref": invocation_id})
            if import_turn is not None:
                import_id, ordinal = import_turn
                changed = conn.execute("UPDATE conversation_import_turns SET invocation_id=?,state='invocation_sealed',updated_at=? WHERE import_id=? AND ordinal=? AND state='draft_created' AND draft_id=? AND invocation_id IS NULL", (invocation_id, now, import_id, ordinal, draft["draft_id"])).rowcount
                if changed != 1:
                    raise LatticeConflict("conversation import turn changed before invocation sealing")
        return self.get_invocation(invocation_id)

    def get_invocation_batch_by_idempotency(self, idempotency_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM invocation_batches WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        return self.get_invocation_batch(row["batch_id"]) if row else None

    def get_invocation_batch(self, batch_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = self._row(conn.execute("SELECT * FROM invocation_batches WHERE batch_id=?", (batch_id,)).fetchone(), batch_id)
            members = [dict(item) for item in conn.execute(
                """SELECT m.ordinal,m.spec_id,m.assembly_sha256,m.invocation_id,i.state,i.terminal_kind,i.output_block_id,i.manifest_sha256
                   FROM invocation_batch_members m JOIN invocations i USING(invocation_id)
                   WHERE m.batch_id=? ORDER BY m.ordinal""",
                (batch_id,),
            )]
            for member in members:
                member["attempts"] = [dict(item) for item in conn.execute(
                    "SELECT attempt_id,attempt_number,state,failure_kind,error_text,started_at,completed_at FROM attempts WHERE invocation_id=? ORDER BY attempt_number",
                    (member["invocation_id"],),
                )]
        receipt = json.loads(row.pop("receipt_json"))
        if sha256_bytes(canonical_bytes({key: value for key, value in receipt.items() if key != "receipt_sha256"})) != row["receipt_sha256"] or receipt.get("receipt_sha256") != row["receipt_sha256"]:
            raise LatticeConflict("stored invocation batch receipt disagrees with its immutable identity")
        return {**receipt, "members": members}

    def list_undispatched_batch_members(self) -> list[dict[str, str]]:
        """Return atomically sealed batch members that never crossed the dispatch boundary."""

        with self._connect() as conn:
            return [dict(row) for row in conn.execute(
                """SELECT b.batch_id,b.project_id,m.spec_id,m.invocation_id
                   FROM invocation_batches b
                   JOIN invocation_batch_members m USING(batch_id)
                   JOIN invocations i USING(invocation_id)
                   WHERE i.state='sealed'
                     AND NOT EXISTS (SELECT 1 FROM attempts a WHERE a.invocation_id=m.invocation_id)
                   ORDER BY b.created_at,b.batch_id,m.ordinal"""
            )]

    def insert_invocation_batch(
        self,
        *,
        idempotency_key: str,
        create_request_sha256: str,
        receipt: dict[str, Any],
        prepared: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Seal every batch member and its shared receipt in one transaction."""

        batch_id = receipt["batch_id"]
        now = receipt["generated_object"]["generated_at"]
        with self.transaction() as conn:
            existing = conn.execute("SELECT batch_id,request_sha256 FROM invocation_batches WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if existing is not None:
                if existing["request_sha256"] != create_request_sha256:
                    raise LatticeConflict("batch idempotency key was already used for different bytes")
                existing_id = existing["batch_id"]
            else:
                if conn.execute("SELECT 1 FROM invocation_batches WHERE batch_id=?", (batch_id,)).fetchone() is not None:
                    raise LatticeConflict("batch identity was already used")
                prompt_blocks: dict[str, dict[str, Any]] = {}
                for item in prepared:
                    prompt_blocks[item["prompt_block"]["block_id"]] = item["prompt_block"]
                for block in prompt_blocks.values():
                    stored = conn.execute("SELECT project_id,content_sha256,semantic_type FROM context_blocks WHERE block_id=?", (block["block_id"],)).fetchone()
                    if stored is None:
                        prompt_value = ContextBlockCreate(project_id=block["project_id"], semantic_type="user-prompt", content=block["content"], evidence_level="operator-intent")
                        self._insert_context_block(conn, prompt_value, block_id=block["block_id"], content=block["content"].encode("utf-8"), digest=block["content_sha256"], now=now)
                    elif dict(stored) != {"project_id": block["project_id"], "content_sha256": block["content_sha256"], "semantic_type": "user-prompt"}:
                        raise LatticeConflict("content-bound prompt identity conflicts with existing bytes")

                for item in prepared:
                    draft = item["draft"]
                    manifest = item["manifest"]
                    for segment in manifest["segments"] + manifest["omitted"]:
                        current = conn.execute(
                            """SELECT b.project_id,b.content_sha256,b.semantic_type,s.status,s.revision
                               FROM context_blocks b JOIN block_status s USING(block_id) WHERE b.block_id=?""",
                            (segment["block_id"],),
                        ).fetchone()
                        if current is None or current["project_id"] != draft["project_id"] or current["content_sha256"] != segment["sha256"] or current["semantic_type"] != segment["semantic_type"] or current["status"] != segment["status"] or current["revision"] != segment["status_revision"]:
                            raise LatticeConflict("batch context changed before atomic sealing")
                    for parent in manifest["ancestry"]:
                        current_parent = conn.execute("SELECT project_id,manifest_sha256,output_block_id FROM invocations WHERE invocation_id=?", (parent["invocation_id"],)).fetchone()
                        if current_parent is None or current_parent["project_id"] != draft["project_id"] or current_parent["manifest_sha256"] != parent["manifest_sha256"] or current_parent["output_block_id"] is None:
                            raise LatticeConflict("batch ancestry changed before atomic sealing")
                    conn.execute(
                        """INSERT INTO drafts(draft_id,project_id,actor_kind,role_session_id,prompt_block_id,route_json,selections_json,parents_json,relation,context_mode,response_schema_json,revision,state,invocation_id,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,1,'sealed',?,?,?)""",
                        (draft["draft_id"], draft["project_id"], draft["actor_kind"], draft["role_session_id"], draft["prompt_block_id"], canonical_bytes(draft["route"]).decode(), canonical_bytes(draft["selections"]).decode(), canonical_bytes(draft["parents"]).decode(), draft["relation"], draft["context_mode"], canonical_bytes(draft["response_schema"]).decode(), manifest["invocation_id"], now, now),
                    )
                    self._event(conn, project_id=draft["project_id"], event_type="draft.created", subject_type="draft", subject_id=draft["draft_id"], payload={"prompt_block_id": draft["prompt_block_id"], "parents": draft["parents"], "batch_id": batch_id})
                    conn.execute(
                        """INSERT INTO invocations(invocation_id,project_id,actor_kind,role_session_id,draft_id,manifest_artifact_id,manifest_sha256,manifest_json,compiled_prompt_sha256,route_json,response_schema_json,state,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (manifest["invocation_id"], draft["project_id"], draft["actor_kind"], draft["role_session_id"], draft["draft_id"], manifest["generated_object"]["artifact_id"], manifest["manifest_sha256"], canonical_bytes(manifest).decode(), item["compiled_prompt_sha256"], canonical_bytes(draft["route"]).decode(), canonical_bytes(draft["response_schema"]).decode(), InvocationState.sealed.value, now),
                    )
                    self._event(conn, project_id=draft["project_id"], event_type="invocation.sealed", subject_type="invocation", subject_id=manifest["invocation_id"], payload={"manifest_artifact_id": manifest["generated_object"]["artifact_id"], "manifest_sha256": manifest["manifest_sha256"], "batch_id": batch_id})
                    for parent_id in draft["parents"]:
                        self._insert_edge(conn, project_id=draft["project_id"], relation=EdgeRelation(draft["relation"]), source_type="invocation", source_id=manifest["invocation_id"], source_revision=manifest["manifest_sha256"], target_type="invocation", target_id=parent_id, target_revision=self._invocation_revision(conn, parent_id), metadata={"batch_id": batch_id})
                    for concern_ref in item.get("concern_refs", []):
                        concern_slice = conn.execute(
                            "SELECT project_id,scope FROM slices WHERE json_extract(query_json,'$.concern_ref')=? ORDER BY created_at LIMIT 1",
                            (concern_ref,),
                        ).fetchone()
                        if concern_slice is None or (concern_slice["scope"] != "global" and concern_slice["project_id"] != draft["project_id"]):
                            raise LatticeConflict("batch concern does not resolve to a visible saved slice")
                        self._insert_edge(conn, project_id=draft["project_id"], relation=EdgeRelation.concerns, source_type="invocation", source_id=manifest["invocation_id"], source_revision=manifest["manifest_sha256"], target_type="external-concern", target_id=concern_ref, target_revision=sha256_bytes(concern_ref.encode("utf-8")), metadata={"batch_id": batch_id})

                conn.execute(
                    "INSERT INTO invocation_batches(batch_id,project_id,idempotency_key,request_sha256,preview_sha256,receipt_json,receipt_sha256,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (batch_id, receipt["project_id"], idempotency_key, create_request_sha256, receipt["preview_sha256"], canonical_bytes(receipt).decode(), receipt["receipt_sha256"], now),
                )
                for ordinal, item in enumerate(prepared):
                    conn.execute(
                        "INSERT INTO invocation_batch_members(batch_id,ordinal,spec_id,assembly_sha256,invocation_id) VALUES(?,?,?,?,?)",
                        (batch_id, ordinal, item["spec_id"], item["assembly_sha256"], item["manifest"]["invocation_id"]),
                    )
                self._event(conn, project_id=receipt["project_id"], event_type="invocation-batch.sealed", subject_type="invocation-batch", subject_id=batch_id, payload={"receipt_sha256": receipt["receipt_sha256"], "invocation_ids": receipt["invocation_ids"]})
                existing_id = batch_id
        return self.get_invocation_batch(existing_id)

    @staticmethod
    def _invocation_revision(conn: sqlite3.Connection, invocation_id: str) -> str:
        row = conn.execute("SELECT manifest_sha256 FROM invocations WHERE invocation_id=?", (invocation_id,)).fetchone()
        if row is None:
            raise LatticeNotFound(invocation_id)
        return str(row[0])

    def _insert_edge(self, conn: sqlite3.Connection, *, project_id: str, relation: EdgeRelation, source_type: str, source_id: str, source_revision: str, target_type: str, target_id: str, target_revision: str, metadata: dict[str, Any]) -> str:
        if target_type == "external-concern" and target_revision != sha256_bytes(target_id.encode("utf-8")):
            raise LatticeConflict("external concern edge revision must bind its exact stable identity")
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
        row["actor_binding"] = {"kind": row["actor_kind"], **({"role_session_id": row["role_session_id"]} if row["role_session_id"] is not None else {})}
        if row["manifest"].get("invocation_id") != row["invocation_id"] or row["manifest"].get("manifest_sha256") != row["manifest_sha256"] or row["manifest"].get("generated_object", {}).get("artifact_id") != row["manifest_artifact_id"] or row["manifest"].get("compilation", {}).get("compiled_prompt_sha256") != row["compiled_prompt_sha256"] or row["manifest"].get("execution") != row["route"]:
            raise LatticeConflict("stored invocation columns disagree with its immutable manifest")
        if row["manifest"].get("contract_version") in {"LatticeContextManifest/4.0", "LatticeContextManifest/5.0", "LatticeContextManifest/6.0"} and row["manifest"].get("actor_binding") != row["actor_binding"]:
            raise LatticeConflict("stored invocation actor binding disagrees with its immutable manifest")
        if row["manifest"].get("contract_version") in {"LatticeContextManifest/2.0", "LatticeContextManifest/3.0", "LatticeContextManifest/4.0", "LatticeContextManifest/5.0", "LatticeContextManifest/6.0", "LoomContextManifest/2.0", "LoomContextManifest/3.0"} and row["manifest"].get("response_contract") != row["response_schema"]:
            raise LatticeConflict("stored invocation response contract disagrees with its immutable manifest")
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
                raise LatticeConflict("terminal attempts are immutable")
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
            completed_triggers = conn.execute("UPDATE workflow_triggers SET state='completed',error_text=NULL,completed_at=? WHERE result_ref=? AND state IN ('planned','execution_failed')", (now, attempt["invocation_id"])).rowcount
            self._event(conn, project_id=attempt["project_id"], event_type="attempt.succeeded", subject_type="attempt", subject_id=attempt_id, payload={"output_block_id": block_id, "artifact_ids": [item[1] for item in prepared_artifacts]})
            if completed_triggers:
                self._event(conn, project_id=attempt["project_id"], event_type="workflow.trigger-completed", subject_type="invocation", subject_id=attempt["invocation_id"], payload={"result_ref": attempt["invocation_id"], "trigger_count": completed_triggers})
        return self.get_invocation(attempt["invocation_id"])

    def list_invocations(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute("SELECT invocation_id,project_id,actor_kind,role_session_id,state,manifest_sha256,output_block_id,terminal_kind,created_at,completed_at FROM invocations WHERE project_id=? ORDER BY created_at,invocation_id", (project_id,))]

    def page_invocations(
        self,
        project_id: str,
        *,
        limit: int,
        before_created_at: str | None = None,
        before_invocation_id: str | None = None,
    ) -> dict[str, Any]:
        """Return a stable recent window without making the project read path unbounded."""

        if (before_created_at is None) != (before_invocation_id is None):
            raise LatticeConflict("invocation history cursor requires both creation time and invocation identity")
        predicates = ["project_id=?"]
        parameters: list[Any] = [project_id]
        if before_created_at is not None and before_invocation_id is not None:
            predicates.append("(created_at < ? OR (created_at = ? AND invocation_id < ?))")
            parameters.extend([before_created_at, before_created_at, before_invocation_id])
        parameters.append(limit + 1)
        with self._connect() as conn:
            rows = [dict(row) for row in conn.execute(
                f"""SELECT invocation_id,project_id,actor_kind,role_session_id,state,manifest_sha256,output_block_id,terminal_kind,created_at,completed_at
                    FROM invocations WHERE {' AND '.join(predicates)}
                    ORDER BY created_at DESC,invocation_id DESC LIMIT ?""",
                parameters,
            )]
        has_older = len(rows) > limit
        window = rows[:limit]
        oldest = window[-1] if window else None
        return {
            "items": list(reversed(window)),
            "has_older": has_older,
            "next_before": {"created_at": oldest["created_at"], "invocation_id": oldest["invocation_id"]} if has_older and oldest else None,
        }

    def page_trailheads(
        self,
        project_id: str,
        *,
        limit: int,
        before_created_at: str | None = None,
        before_invocation_id: str | None = None,
    ) -> dict[str, Any]:
        """Page every branch tip while the foreground recommends only three."""

        if (before_created_at is None) != (before_invocation_id is None):
            raise LatticeConflict("trailhead cursor requires both creation time and invocation identity")
        cursor = ""
        parameters: list[Any] = [project_id]
        if before_created_at is not None and before_invocation_id is not None:
            cursor = " AND (i.created_at < ? OR (i.created_at = ? AND i.invocation_id < ?))"
            parameters.extend([before_created_at, before_created_at, before_invocation_id])
        parameters.append(limit + 1)
        with self._connect() as conn:
            rows = [dict(row) for row in conn.execute(
                f"""SELECT i.invocation_id,i.project_id,i.actor_kind,i.role_session_id,i.state,i.manifest_sha256,i.output_block_id,i.terminal_kind,i.created_at,i.completed_at
                    FROM invocations i
                    WHERE i.project_id=?{cursor} AND NOT EXISTS(
                        SELECT 1 FROM edges e WHERE e.source_type='invocation'
                        AND e.target_type='invocation' AND e.target_id=i.invocation_id)
                    ORDER BY i.created_at DESC,i.invocation_id DESC LIMIT ?""",
                parameters,
            )]
        has_older = len(rows) > limit
        window = rows[:limit]
        oldest = window[-1] if window else None
        return {
            "items": window,
            "has_older": has_older,
            "next_before": {"created_at": oldest["created_at"], "invocation_id": oldest["invocation_id"]} if has_older and oldest else None,
        }

    def get_artifact(self, artifact_id: str, *, include_content: bool = True) -> dict[str, Any]:
        with self._connect() as conn:
            row = self._row(conn.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone(), artifact_id)
        row["generated"] = json.loads(row.pop("generated_json"))
        if include_content:
            row["content"] = self.blobs.get(row["content_sha256"]).decode("utf-8")
        return row

    def create_artifact_context_block(self, artifact_id: str) -> dict[str, Any]:
        """Expose exact UTF-8 artifact bytes as a reusable block without copying their identity."""

        artifact = self.get_artifact(artifact_id, include_content=False)
        content = self.blobs.get(artifact["content_sha256"])
        try:
            content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise LatticeConflict("only UTF-8 artifacts can enter text context") from error
        generated = artifact["generated"]
        transformation = {
            "kind": "exact-artifact-graft-v1",
            "source_artifact_id": artifact_id,
            "source_sha256": artifact["content_sha256"],
            "name": generated.get("name"),
            "media_type": generated.get("media_type"),
        }
        value = ContextBlockCreate(
            project_id=artifact["project_id"],
            semantic_type="exact-artifact-graft",
            content=content.decode("utf-8"),
            producing_invocation_id=generated.get("producing_invocation_id"),
            source_ref=f"lattice:artifact/{artifact_id}",
            source_sha256=artifact["content_sha256"],
            authority="operator-derived",
            evidence_level="exact-artifact",
            sensitivity="private",
            transformation=transformation,
        )
        block_id = opaque_id("block")
        now = stamp()
        with self.transaction() as conn:
            self._insert_context_block(conn, value, block_id=block_id, content=content, digest=artifact["content_sha256"], now=now)
            self._insert_edge(
                conn,
                project_id=artifact["project_id"],
                relation=EdgeRelation.derived_from,
                source_type="context-block",
                source_id=block_id,
                source_revision=artifact["content_sha256"],
                target_type="artifact",
                target_id=artifact_id,
                target_revision=artifact["content_sha256"],
                metadata={"transformation": transformation},
            )
        return self.get_block(block_id, include_content=False)

    def create_attempt(self, invocation_id: str, idempotency_key: str, boot_id: str, expires_at: int) -> dict[str, Any]:
        with self.transaction() as conn:
            prior = conn.execute("SELECT * FROM attempts WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if prior is not None:
                if prior["invocation_id"] != invocation_id:
                    raise LatticeConflict("idempotency key belongs to another invocation")
                return dict(prior)
            invocation = self._row(conn.execute("SELECT project_id,state FROM invocations WHERE invocation_id=?", (invocation_id,)).fetchone(), invocation_id)
            if invocation["state"] not in {InvocationState.sealed.value, InvocationState.failed.value, InvocationState.interrupted.value}:
                raise LatticeConflict(f"invocation cannot execute from {invocation['state']}")
            active = conn.execute("SELECT 1 FROM leases WHERE invocation_id=? AND state='active'", (invocation_id,)).fetchone()
            if active:
                raise LatticeConflict("invocation already has an active lease")
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
                raise LatticeConflict("attempt is not leased")
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
                raise LatticeConflict("terminal attempts are immutable")
            now = stamp()
            conn.execute("UPDATE attempts SET state=?,journal_path=COALESCE(?,journal_path),output_sha256=?,failure_kind=?,error_text=?,runtime_receipt_json=?,completed_at=? WHERE attempt_id=?", (state.value, journal_path, output_sha256, failure_kind, error_text, canonical_bytes(runtime_receipt).decode() if runtime_receipt else None, now, attempt_id))
            conn.execute("UPDATE leases SET state='released',released_at=? WHERE attempt_id=? AND state='active'", (now, attempt_id))
            invocation_state = InvocationState.succeeded if state == AttemptState.succeeded else InvocationState.interrupted if state in {AttemptState.interrupted, AttemptState.orphaned} else InvocationState.failed
            conn.execute("UPDATE invocations SET state=?,output_block_id=COALESCE(?,output_block_id),terminal_kind=?,completed_at=? WHERE invocation_id=?", (invocation_state.value, output_block_id, failure_kind or state.value, now, attempt["invocation_id"]))
            failed_triggers = conn.execute("UPDATE workflow_triggers SET state='execution_failed',error_text=?,completed_at=NULL WHERE result_ref=? AND state='planned'", (error_text or failure_kind or state.value, attempt["invocation_id"])).rowcount
            self._event(conn, project_id=attempt["project_id"], event_type=f"attempt.{state.value}", subject_type="attempt", subject_id=attempt_id, payload={"output_block_id": output_block_id, "failure_kind": failure_kind})
            if failed_triggers:
                self._event(conn, project_id=attempt["project_id"], event_type="workflow.trigger-execution-failed", subject_type="invocation", subject_id=attempt["invocation_id"], payload={"failure_kind": failure_kind, "error_text": error_text, "trigger_count": failed_triggers})
        return self.get_invocation(attempt["invocation_id"])

    def attach_imported_output(self, invocation_id: str, output_block_id: str) -> dict[str, Any]:
        block = self.get_block(output_block_id, include_content=False)
        with self.transaction() as conn:
            invocation = self._row(conn.execute("SELECT project_id,state,manifest_sha256 FROM invocations WHERE invocation_id=?", (invocation_id,)).fetchone(), invocation_id)
            if invocation["state"] != InvocationState.sealed.value:
                raise LatticeConflict("only newly sealed invocations can become imported history")
            if invocation["project_id"] != block["project_id"]:
                raise LatticeConflict("imported output belongs to another project")
            conn.execute("UPDATE invocations SET state='imported',output_block_id=?,terminal_kind='historical-import',completed_at=? WHERE invocation_id=?", (output_block_id, stamp(), invocation_id))
            self._insert_edge(conn, project_id=invocation["project_id"], relation=EdgeRelation.produced, source_type="invocation", source_id=invocation_id, source_revision=invocation["manifest_sha256"], target_type="context-block", target_id=output_block_id, target_revision=block["content_sha256"], metadata={"historical_import": True})
            self._event(conn, project_id=invocation["project_id"], event_type="invocation.imported", subject_type="invocation", subject_id=invocation_id, payload={"output_block_id": output_block_id})
        return self.get_invocation(invocation_id)

    def reconcile_orphans(self, boot_id: str) -> int:
        with self._connect() as conn:
            rows = [dict(row) for row in conn.execute("SELECT attempt_id FROM attempts WHERE state IN ('leased','running') AND COALESCE(owner_boot_id,'')<>?", (boot_id,))]
        for row in rows:
            self.finish_attempt(row["attempt_id"], state=AttemptState.orphaned, failure_kind="process_restart", error_text="Lattice restarted before the attempt reached a terminal receipt")
        return len(rows)

    def create_slice(self, value: SliceCreate) -> dict[str, Any]:
        slice_id = opaque_id("slice")
        now = stamp()
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM projects WHERE project_id=?", (value.project_id,)).fetchone() is None:
                raise LatticeNotFound(value.project_id)
            if value.external_ref is not None and conn.execute("SELECT 1 FROM slices WHERE external_ref=?", (value.external_ref,)).fetchone() is not None:
                raise LatticeConflict("external Lattice slice identity already exists")
            conn.execute("INSERT INTO slices(slice_id,project_id,scope,name,query_json,external_ref,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (slice_id, value.project_id, value.scope, value.name, canonical_bytes(value.query.model_dump(mode="json")).decode(), value.external_ref, now, now))
            self._event(conn, project_id=value.project_id, event_type="slice.created", subject_type="slice", subject_id=slice_id, payload=value.model_dump(mode="json"))
        return self.get_slice(slice_id)

    @staticmethod
    def _object_revision(conn: sqlite3.Connection, project_id: str, object_type: str, object_id: str) -> str:
        contracts = {
            "invocation": ("invocations", "invocation_id", "manifest_sha256"),
            "context-block": ("context_blocks", "block_id", "content_sha256"),
            "artifact": ("artifacts", "artifact_id", "content_sha256"),
        }
        table, identity, revision = contracts[object_type]
        row = conn.execute(f'SELECT project_id,"{revision}" AS revision FROM "{table}" WHERE "{identity}"=?', (object_id,)).fetchone()
        if row is None:
            raise LatticeNotFound(object_id)
        if row["project_id"] != project_id:
            raise LatticeConflict("episode member belongs to another project")
        return row["revision"]

    def create_episode(self, value: EpisodeCreate) -> dict[str, Any]:
        episode_id = opaque_id("episode")
        slice_id = opaque_id("slice")
        concern_ref = f"lattice:{episode_id}"
        now = stamp()
        query = {"concern_ref": concern_ref, "object_types": ["invocation", "context-block", "artifact"]}
        with self.transaction() as conn:
            anchor_revision = self._object_revision(conn, value.project_id, value.anchor_type, value.anchor_id)
            parent = None
            if value.parent_episode_id is not None:
                parent = self._row(conn.execute("SELECT slice_id,project_id,query_json FROM slices WHERE slice_id=?", (value.parent_episode_id,)).fetchone(), value.parent_episode_id)
                if parent["project_id"] != value.project_id or not str(json.loads(parent["query_json"]).get("concern_ref", "")).startswith("lattice:episode_"):
                    raise LatticeConflict("parent episode is not a project-local episode")
            conn.execute("INSERT INTO slices(slice_id,project_id,scope,name,query_json,external_ref,created_at,updated_at) VALUES(?,?,'project',?,?,NULL,?,?)", (slice_id, value.project_id, value.name, canonical_bytes(query).decode(), now, now))
            self._event(conn, project_id=value.project_id, event_type="episode.created", subject_type="slice", subject_id=slice_id, payload={"episode_id": episode_id, "concern_ref": concern_ref, **value.model_dump(mode="json")})
            self._insert_edge(conn, project_id=value.project_id, relation=EdgeRelation.concerns, source_type=value.anchor_type, source_id=value.anchor_id, source_revision=anchor_revision, target_type="external-concern", target_id=concern_ref, target_revision=sha256_bytes(concern_ref.encode("utf-8")), metadata={"episode_id": episode_id, "anchor": True})
            if parent is not None:
                self._insert_edge(conn, project_id=value.project_id, relation=EdgeRelation.derived_from, source_type="slice", source_id=slice_id, source_revision=sha256_bytes(canonical_bytes(query)), target_type="slice", target_id=parent["slice_id"], target_revision=sha256_bytes(parent["query_json"].encode("utf-8")), metadata={"kind": "child-episode"})
        return self.get_episode(slice_id)

    def add_episode_member(self, episode_id: str, value: EpisodeMemberCreate) -> dict[str, Any]:
        with self.transaction() as conn:
            episode = self._row(conn.execute("SELECT slice_id,project_id,query_json FROM slices WHERE slice_id=?", (episode_id,)).fetchone(), episode_id)
            concern_ref = json.loads(episode["query_json"]).get("concern_ref")
            if not isinstance(concern_ref, str) or not concern_ref.startswith("lattice:episode_"):
                raise LatticeConflict("slice is not an episode")
            revision = self._object_revision(conn, episode["project_id"], value.object_type, value.object_id)
            existing = conn.execute("SELECT edge_id FROM edges WHERE relation='concerns' AND source_type=? AND source_id=? AND source_revision=? AND target_type='external-concern' AND target_id=?", (value.object_type, value.object_id, revision, concern_ref)).fetchone()
            edge_id = existing["edge_id"] if existing else self._insert_edge(conn, project_id=episode["project_id"], relation=EdgeRelation.concerns, source_type=value.object_type, source_id=value.object_id, source_revision=revision, target_type="external-concern", target_id=concern_ref, target_revision=sha256_bytes(concern_ref.encode("utf-8")), metadata={"episode_id": episode_id})
        return {"edge_id": edge_id, "episode": self.get_episode(episode_id)}

    def get_episode(self, episode_id: str) -> dict[str, Any]:
        episode = self.get_slice(episode_id)
        concern_ref = episode["query"].get("concern_ref")
        if not isinstance(concern_ref, str) or not concern_ref.startswith("lattice:episode_"):
            raise LatticeConflict("slice is not an episode")
        with self._connect() as conn:
            parent = conn.execute("SELECT target_id FROM edges WHERE relation='derived_from' AND source_type='slice' AND source_id=? AND target_type='slice' ORDER BY created_at LIMIT 1", (episode_id,)).fetchone()
        return {**episode, "episode_id": concern_ref.removeprefix("lattice:"), "concern_ref": concern_ref, "parent_episode_id": parent["target_id"] if parent else None}

    def list_project_episodes(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = [row["slice_id"] for row in conn.execute("SELECT slice_id FROM slices WHERE project_id=? AND scope='project' AND json_extract(query_json,'$.concern_ref') GLOB 'lattice:episode_*' ORDER BY created_at", (project_id,))]
        return [self.get_episode(slice_id) for slice_id in rows]

    def list_invocation_episodes(self, invocation_id: str) -> list[dict[str, Any]]:
        invocation = self.get_invocation(invocation_id)
        with self._connect() as conn:
            rows = [row["slice_id"] for row in conn.execute(
                """SELECT DISTINCT s.slice_id FROM edges e JOIN slices s ON json_extract(s.query_json,'$.concern_ref')=e.target_id
                   WHERE e.relation='concerns' AND e.source_type='invocation' AND e.source_id=? AND s.project_id=? AND json_extract(s.query_json,'$.concern_ref') GLOB 'lattice:episode_*'
                   ORDER BY s.created_at""",
                (invocation_id, invocation["project_id"]),
            )]
        return [self.get_episode(slice_id) for slice_id in rows]

    def tag_invocation(self, invocation_id: str, value: TagRequest) -> dict[str, Any]:
        """Commit the concern edge and its globally addressable live slice as one fact."""
        target_revision = sha256_bytes(value.concern_ref.encode("utf-8"))
        with self.transaction() as conn:
            invocation = self._row(conn.execute("SELECT project_id,manifest_sha256 FROM invocations WHERE invocation_id=?", (invocation_id,)).fetchone(), invocation_id)
            slice_row = conn.execute("SELECT slice_id,name FROM slices WHERE external_ref=?", (value.concern_ref,)).fetchone()
            if slice_row is None:
                slice_id = opaque_id("slice")
                now = stamp()
                query = {"concern_ref": value.concern_ref}
                conn.execute("INSERT INTO slices(slice_id,project_id,scope,name,query_json,external_ref,created_at,updated_at) VALUES(?,?,'global',?,?,?,?,?)", (slice_id, invocation["project_id"], value.label, canonical_bytes(query).decode(), value.concern_ref, now, now))
                self._event(conn, project_id=invocation["project_id"], event_type="slice.created", subject_type="slice", subject_id=slice_id, payload={"project_id": invocation["project_id"], "scope": "global", "name": value.label, "query": query, "external_ref": value.concern_ref})
            else:
                slice_id = slice_row["slice_id"]
                if slice_row["name"] != value.label:
                    raise LatticeConflict("external concern label disagrees with its existing stable identity")
            existing = conn.execute(
                """SELECT edge_id FROM edges WHERE relation='concerns' AND source_type='invocation' AND source_id=? AND source_revision=?
                   AND target_type='external-concern' AND target_id=? AND target_revision=?""",
                (invocation_id, invocation["manifest_sha256"], value.concern_ref, target_revision),
            ).fetchone()
            edge_id = existing["edge_id"] if existing else self._insert_edge(conn, project_id=invocation["project_id"], relation=EdgeRelation.concerns, source_type="invocation", source_id=invocation_id, source_revision=invocation["manifest_sha256"], target_type="external-concern", target_id=value.concern_ref, target_revision=target_revision, metadata={"label": value.label})
        return {"edge_id": edge_id, "slice": self.get_slice(slice_id)}

    def tag_context_block(self, block_id: str, value: TagRequest) -> dict[str, Any]:
        """Tag a reusable block into the same live concern-slice system as invocations."""

        target_revision = sha256_bytes(value.concern_ref.encode("utf-8"))
        with self.transaction() as conn:
            block = self._row(conn.execute("SELECT project_id,content_sha256 FROM context_blocks WHERE block_id=?", (block_id,)).fetchone(), block_id)
            slice_row = conn.execute("SELECT slice_id,name FROM slices WHERE external_ref=?", (value.concern_ref,)).fetchone()
            if slice_row is None:
                slice_id = opaque_id("slice")
                now = stamp()
                query = {"concern_ref": value.concern_ref, "object_types": ["invocation", "context-block"]}
                conn.execute("INSERT INTO slices(slice_id,project_id,scope,name,query_json,external_ref,created_at,updated_at) VALUES(?,?,'global',?,?,?,?,?)", (slice_id, block["project_id"], value.label, canonical_bytes(query).decode(), value.concern_ref, now, now))
                self._event(conn, project_id=block["project_id"], event_type="slice.created", subject_type="slice", subject_id=slice_id, payload={"project_id": block["project_id"], "scope": "global", "name": value.label, "query": query, "external_ref": value.concern_ref})
            else:
                slice_id = slice_row["slice_id"]
                if slice_row["name"] != value.label:
                    raise LatticeConflict("external concern label disagrees with its existing stable identity")
                existing_query = conn.execute("SELECT query_json,revision FROM slices WHERE slice_id=?", (slice_id,)).fetchone()
                query = json.loads(existing_query["query_json"])
                object_types = list(query.get("object_types", ["invocation"]))
                if "context-block" not in object_types:
                    query["object_types"] = [*object_types, "context-block"]
                    conn.execute("UPDATE slices SET query_json=?,revision=?,updated_at=? WHERE slice_id=?", (canonical_bytes(query).decode(), existing_query["revision"] + 1, stamp(), slice_id))
                    self._event(conn, project_id=block["project_id"], event_type="slice.query-expanded", subject_type="slice", subject_id=slice_id, payload={"object_types": query["object_types"], "reason": "explicit-context-block-tag"})
            existing = conn.execute(
                """SELECT edge_id FROM edges WHERE relation='concerns' AND source_type='context-block' AND source_id=? AND source_revision=?
                   AND target_type='external-concern' AND target_id=? AND target_revision=?""",
                (block_id, block["content_sha256"], value.concern_ref, target_revision),
            ).fetchone()
            edge_id = existing["edge_id"] if existing else self._insert_edge(conn, project_id=block["project_id"], relation=EdgeRelation.concerns, source_type="context-block", source_id=block_id, source_revision=block["content_sha256"], target_type="external-concern", target_id=value.concern_ref, target_revision=target_revision, metadata={"label": value.label})
        return {"edge_id": edge_id, "slice": self.get_slice(slice_id)}

    @staticmethod
    def _slice_cursor(
        *,
        after_created_at: str | None,
        after_object_type: str | None,
        after_object_id: str | None,
    ) -> tuple[str, str, str] | None:
        values = (after_created_at, after_object_type, after_object_id)
        if all(value is None for value in values):
            return None
        if any(value is None for value in values):
            raise LatticeConflict("slice continuation requires created_at, object_type, and object_id together")
        if after_object_type not in {"artifact", "context-block", "invocation"}:
            raise LatticeConflict("slice continuation has an unsupported object type")
        return after_created_at, after_object_type, after_object_id

    def get_slice(
        self,
        slice_id: str,
        *,
        limit: int = SLICE_DEFAULT_LIMIT,
        after_created_at: str | None = None,
        after_object_type: str | None = None,
        after_object_id: str | None = None,
        snapshot_event_seq: int | None = None,
    ) -> dict[str, Any]:
        if not 1 <= limit <= self.SLICE_MAX_LIMIT:
            raise LatticeConflict(f"slice limit must be between 1 and {self.SLICE_MAX_LIMIT}")
        cursor = self._slice_cursor(
            after_created_at=after_created_at,
            after_object_type=after_object_type,
            after_object_id=after_object_id,
        )
        if cursor is not None and snapshot_event_seq is None:
            raise LatticeConflict("slice continuation requires its enumeration snapshot event sequence")
        if snapshot_event_seq is not None and snapshot_event_seq < 1:
            raise LatticeConflict("slice enumeration snapshot event sequence must be positive")
        with self._connect() as conn:
            row = self._row(conn.execute("SELECT * FROM slices WHERE slice_id=?", (slice_id,)).fetchone(), slice_id)
        row["query"] = json.loads(row.pop("query_json"))
        evaluation = self.evaluate_slice(row, limit=limit, cursor=cursor, snapshot_event_seq=snapshot_event_seq)
        row["items"] = evaluation["items"]
        row["coverage"] = evaluation["coverage"]
        return row

    def get_slice_by_external_ref(
        self,
        external_ref: str,
        *,
        limit: int = SLICE_DEFAULT_LIMIT,
        after_created_at: str | None = None,
        after_object_type: str | None = None,
        after_object_id: str | None = None,
        snapshot_event_seq: int | None = None,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT slice_id FROM slices WHERE external_ref=?", (external_ref,)).fetchone()
        return self.get_slice(
            row["slice_id"],
            limit=limit,
            after_created_at=after_created_at,
            after_object_type=after_object_type,
            after_object_id=after_object_id,
            snapshot_event_seq=snapshot_event_seq,
        ) if row else None

    def evaluate_slice(
        self,
        slice_row: dict[str, Any],
        *,
        limit: int,
        cursor: tuple[str, str, str] | None = None,
        snapshot_event_seq: int | None = None,
    ) -> dict[str, Any]:
        try:
            query = SliceQuery.model_validate(slice_row.get("query") or json.loads(slice_row["query_json"]))
        except ValueError as error:
            raise LatticeConflict("stored slice query is not executable by this Lattice generation") from error
        concern = query.concern_ref
        object_types = set(query.object_types)
        global_scope = slice_row.get("scope", "project") == "global"
        selections: list[str] = []
        params: list[Any] = []
        if "invocation" in object_types:
            sql = """SELECT 'invocation' AS object_type,i.invocation_id AS object_id,i.project_id,i.state,
                            i.role_session_id,i.output_block_id,NULL AS semantic_type,
                            b.content_sha256 AS response_sha256,b.byte_count AS response_byte_count,NULL AS media_type,i.created_at,
                            COALESCE((SELECT MIN(e.seq) FROM events e WHERE e.subject_type='invocation' AND e.subject_id=i.invocation_id AND e.event_type='invocation.sealed'),0) AS created_event_seq
                     FROM invocations i LEFT JOIN context_blocks b ON b.block_id=i.output_block_id"""
            conditions: list[str] = []
            if concern:
                conditions.append("EXISTS (SELECT 1 FROM edges e WHERE e.relation='concerns' AND e.source_type='invocation' AND e.source_id=i.invocation_id AND e.target_id=?)")
                params.append(concern)
            if not global_scope:
                conditions.append("i.project_id=?")
                params.append(slice_row["project_id"])
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            selections.append(sql)
        if "context-block" in object_types:
            sql = """SELECT 'context-block' AS object_type,b.block_id AS object_id,b.project_id,s.status AS state,
                            NULL AS role_session_id,NULL AS output_block_id,b.semantic_type,
                            b.content_sha256 AS response_sha256,b.byte_count AS response_byte_count,NULL AS media_type,b.created_at,
                            COALESCE((SELECT MIN(e.seq) FROM events e WHERE e.subject_type='context-block' AND e.subject_id=b.block_id AND e.event_type='context-block.created'),0) AS created_event_seq
                     FROM context_blocks b JOIN block_status s USING(block_id)"""
            conditions = []
            if concern:
                conditions.append("EXISTS (SELECT 1 FROM edges e WHERE e.relation='concerns' AND e.source_type='context-block' AND e.source_id=b.block_id AND e.target_id=?)")
                params.append(concern)
            if not global_scope:
                conditions.append("b.project_id=?")
                params.append(slice_row["project_id"])
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            selections.append(sql)
        if "artifact" in object_types:
            sql = """SELECT 'artifact' AS object_type,a.artifact_id AS object_id,a.project_id,'available' AS state,
                            NULL AS role_session_id,NULL AS output_block_id,a.artifact_kind AS semantic_type,
                            a.content_sha256 AS response_sha256,json_extract(a.generated_json,'$.byte_count') AS response_byte_count,
                            json_extract(a.generated_json,'$.media_type') AS media_type,a.created_at,
                            COALESCE((SELECT MIN(e.seq) FROM events e WHERE e.subject_type='artifact' AND e.subject_id=a.artifact_id AND e.event_type='artifact.created'),0) AS created_event_seq
                     FROM artifacts a"""
            conditions = []
            if concern:
                conditions.append("EXISTS (SELECT 1 FROM edges e WHERE e.relation='concerns' AND e.source_type='artifact' AND e.source_id=a.artifact_id AND e.target_id=?)")
                params.append(concern)
            if not global_scope:
                conditions.append("a.project_id=?")
                params.append(slice_row["project_id"])
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            selections.append(sql)

        combined = " UNION ALL ".join(selections)
        bounded_sql = f"SELECT * FROM ({combined}) WHERE created_event_seq<=?"
        cursor_params: list[Any] = []
        if cursor is not None:
            bounded_sql += " AND (created_at>? OR (created_at=? AND object_type>?) OR (created_at=? AND object_type=? AND object_id>?))"
            cursor_params.extend((cursor[0], cursor[0], cursor[1], cursor[0], cursor[1], cursor[2]))
        bounded_sql += " ORDER BY created_at,object_type,object_id LIMIT ?"
        with self._connect() as conn:
            if snapshot_event_seq is None:
                snapshot_event_seq = conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
            params.extend((snapshot_event_seq, *cursor_params, limit + 1))
            rows = [dict(row) for row in conn.execute(bounded_sql, params)]

        has_more = len(rows) > limit
        items = rows[:limit]
        inline_bytes = 0
        response_omissions = 0
        for item in items:
            if item["object_type"] == "invocation":
                item["invocation_id"] = item["object_id"]
            elif item["object_type"] == "context-block":
                item["block_id"] = item["object_id"]
                item["status"] = item["state"]
                item["content_sha256"] = item["response_sha256"]
            else:
                item["artifact_id"] = item["object_id"]
                item["artifact_kind"] = item["semantic_type"]
                item["content_sha256"] = item["response_sha256"]
            byte_count = item.get("response_byte_count")
            digest = item.get("response_sha256")
            if digest is None or byte_count is None:
                item["response_state"] = "not-produced"
                continue
            if item["object_type"] == "artifact" and not self._textual_media_type(item.get("media_type")):
                item["response_state"] = "binary-artifact"
                continue
            if byte_count > self.SLICE_INLINE_RESPONSE_BYTES - inline_bytes:
                item["response_state"] = "omitted-byte-budget"
                response_omissions += 1
                continue
            content = self.blobs.get(digest)
            if len(content) != byte_count:
                raise OSError(f"slice response byte count disagrees with blob identity {digest}")
            item["response"] = content.decode("utf-8")
            item["response_state"] = "included"
            inline_bytes += byte_count

        continuation = None
        if has_more and items:
            last = items[-1]
            continuation = {
                "after_created_at": last["created_at"],
                "after_object_type": last["object_type"],
                "after_object_id": last["object_id"],
                "snapshot_event_seq": snapshot_event_seq,
            }
        enumeration_state = "partial" if has_more else "complete"
        content_state = "partial" if response_omissions else "complete"
        return {
            "items": items,
            "coverage": {
                "state": "partial" if has_more or response_omissions else "complete",
                "enumeration": {
                    "state": enumeration_state,
                    "returned": len(items),
                    "has_more": has_more,
                    "continuation": continuation,
                    "snapshot_event_seq": snapshot_event_seq,
                },
                "content": {
                    "state": content_state,
                    "inline_byte_limit": self.SLICE_INLINE_RESPONSE_BYTES,
                    "inline_bytes": inline_bytes,
                    "omitted_responses": response_omissions,
                },
            },
        }

    @staticmethod
    def _textual_media_type(media_type: Any) -> bool:
        if not isinstance(media_type, str):
            return False
        base = media_type.split(";", 1)[0].strip().casefold()
        return base.startswith("text/") or base in {"application/json", "application/ld+json", "application/xml", "application/javascript"} or base.endswith("+json") or base.endswith("+xml")

    def events_after(self, project_id: str, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM events WHERE project_id=? AND seq>? ORDER BY seq LIMIT ?", (project_id, after, limit))]
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_json"))
        return rows

    def state_digest(self) -> str:
        digest = hashlib.sha256()

        def bind(value: Any) -> None:
            encoded = canonical_bytes(value)
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)

        bind({"kind": "LatticeStateDigest", "version": 2})
        with self._connect() as conn:
            self._verify_integrity_chain(conn)
            for table in CHANGE_TABLES:
                _columns, primary = self._table_contract(conn, table)
                order = ",".join(f'"{name}"' for name in primary)
                bind({"stream": "current-view", "table": table})
                for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY {order}'):
                    bind(dict(row))
            for name, query in (
                ("events", "SELECT * FROM events ORDER BY seq"),
                ("ledger-changes", "SELECT * FROM ledger_changes ORDER BY seq"),
                ("append-integrity", "SELECT * FROM append_integrity ORDER BY seq"),
            ):
                bind({"stream": name})
                for row in conn.execute(query):
                    bind(dict(row))
        return digest.hexdigest()

    @staticmethod
    def _collect_blob_refs(value: Any, refs: set[str]) -> None:
        if isinstance(value, str) and value.startswith((CURRENT_BLOB_PREFIX, LEGACY_BLOB_PREFIX)):
            prefix = CURRENT_BLOB_PREFIX if value.startswith(CURRENT_BLOB_PREFIX) else LEGACY_BLOB_PREFIX
            digest = value.removeprefix(prefix)
            if len(digest) == 64 and all(character in "0123456789abcdef" for character in digest):
                refs.add(digest)
        elif isinstance(value, dict):
            for child in value.values():
                LatticeStore._collect_blob_refs(child, refs)
        elif isinstance(value, list):
            for child in value:
                LatticeStore._collect_blob_refs(child, refs)

    def _required_blob_bytes(self, digest: str) -> bytes:
        try:
            return self.blobs.get(digest)
        except FileNotFoundError as error:
            raise OSError(f"missing blob required by Lattice state: {digest}") from error

    def referenced_blob_digests(self) -> set[str]:
        """Return every CAS object needed to reconstruct current and historical Lattice truth."""
        refs: set[str] = set()
        structured_blobs: set[str] = set()
        with self._connect() as conn:
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
                values = {str(row[0]) for row in conn.execute(f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL')}
                refs.update(values)
                if (table, column) in {("workflow_commands", "result_sha256"), ("conversation_imports", "result_sha256"), ("workflow_transitions", "payload_sha256")}:
                    structured_blobs.update(values)
            for table, column in (
                ("invocations", "manifest_json"),
                ("artifacts", "generated_json"),
                ("context_blocks", "transformation_json"),
                ("workflow_commands", "receipt_json"),
                ("workflow_commands", "arguments_json"),
                ("attempts", "runtime_receipt_json"),
                ("events", "payload_json"),
            ):
                for row in conn.execute(f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'):
                    self._collect_blob_refs(json.loads(row[0]), refs)
            for row in conn.execute("SELECT result_ref FROM workflow_triggers WHERE result_ref LIKE 'lattice:blob/%' OR result_ref LIKE 'loom:blob/%'"):
                self._collect_blob_refs(row[0], refs)
        for digest in sorted(structured_blobs):
            try:
                value = json.loads(self._required_blob_bytes(digest))
            except json.JSONDecodeError as error:
                raise OSError(f"structured Lattice blob is not valid JSON: {digest}") from error
            self._collect_blob_refs(value, refs)
        return refs

    def rebuild_database(self, destination: Path) -> str:
        """Reconstruct a complete Lattice data root off-path, then publish it atomically."""
        with self._write_lock, self._connect() as guard:
            guard.execute("BEGIN IMMEDIATE")
            active_work = {
                "attempts": guard.execute("SELECT COUNT(*) FROM attempts WHERE state IN ('leased','running')").fetchone()[0],
                "commands": guard.execute("SELECT COUNT(*) FROM workflow_commands WHERE state='running'").fetchone()[0],
                "imports": guard.execute("SELECT COUNT(*) FROM conversation_imports WHERE state='running'").fetchone()[0],
                "leases": guard.execute("SELECT COUNT(*) FROM leases WHERE state='active'").fetchone()[0],
            }
            if any(active_work.values()):
                raise LatticeConflict(f"cannot rebuild while durable work is active: {active_work}")
            self._verify_integrity_chain(guard)
            expected_state_digest = self.state_digest()
            return self._rebuild_database_locked(destination, expected_state_digest=expected_state_digest)

    def _rebuild_database_locked(self, destination: Path, *, expected_state_digest: str) -> str:
        """Rebuild while the caller holds both the process lock and SQLite writer lock."""
        destination = destination.resolve()
        if destination.name != "lattice.sqlite3":
            raise ValueError("rebuild destination must be named lattice.sqlite3")
        data_root = destination.parent
        if data_root.exists():
            raise FileExistsError(data_root)
        required_blobs = sorted(self.referenced_blob_digests())
        for digest in required_blobs:
            self.blobs.verify_file(digest)
        data_root.parent.mkdir(parents=True, exist_ok=True)
        staging_root = Path(tempfile.mkdtemp(prefix=f".{data_root.name}.rebuild-", dir=data_root.parent))
        staging_destination = staging_root / "lattice.sqlite3"
        target = sqlite3.connect(staging_destination, isolation_level=None)
        target.row_factory = sqlite3.Row
        target.create_function("lattice_stamp", 0, stamp)
        target.create_function("lattice_replaying", 0, lambda: 1)
        target.create_function("lattice_integrity_hash", -1, self._integrity_hash, deterministic=True)
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
                        if change["operation"] == "insert":
                            target.execute(f'INSERT INTO "{table}"({",".join(f"\"{name}\"" for name in columns)}) VALUES({",".join("?" for _ in columns)})', tuple(row[name] for name in columns))
                        elif change["operation"] == "baseline":
                            where = " AND ".join(f'"{name}"=?' for name in primary)
                            if target.execute(f'SELECT 1 FROM "{table}" WHERE {where}', tuple(primary.values())).fetchone() is None:
                                target.execute(f'INSERT INTO "{table}"({",".join(f"\"{name}\"" for name in columns)}) VALUES({",".join("?" for _ in columns)})', tuple(row[name] for name in columns))
                            else:
                                non_primary = [name for name in columns if name not in primary]
                                assignments = ",".join(f'"{name}"=?' for name in non_primary)
                                target.execute(f'UPDATE "{table}" SET {assignments} WHERE {where}', tuple(row[name] for name in non_primary) + tuple(primary.values()))
                        else:
                            non_primary = [name for name in columns if name not in primary]
                            assignments = ",".join(f'"{name}"=?' for name in non_primary)
                            where = " AND ".join(f'"{name}"=?' for name in primary)
                            target.execute(f'UPDATE "{table}" SET {assignments} WHERE {where}', tuple(row[name] for name in non_primary) + tuple(primary.values()))
                for row in source.execute("SELECT * FROM ledger_changes ORDER BY seq"):
                    value = dict(row)
                    columns = list(value)
                    target.execute(f"INSERT INTO ledger_changes({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", tuple(value[name] for name in columns))
                for row in source.execute("SELECT * FROM append_integrity ORDER BY seq"):
                    value = dict(row)
                    columns = list(value)
                    target.execute(f"INSERT INTO append_integrity({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", tuple(value[name] for name in columns))
                for row in source.execute("SELECT * FROM schema_migrations ORDER BY version"):
                    target.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(?,?)", tuple(row))
            target.commit()
            target.close()
            staging_config = LatticeConfig(data_root=staging_root, host=self.config.host, port=self.config.port, octopus_base_url=self.config.octopus_base_url, lease_seconds=self.config.lease_seconds)
            staging_blobs = BlobStore(staging_config.blobs)
            for digest in required_blobs:
                source, byte_count = self.blobs.verify_file(digest)
                copied_digest, copied_bytes = staging_blobs.put_file(source)
                if copied_digest != digest or copied_bytes != byte_count:
                    raise OSError(f"blob identity changed during rebuild: {digest}")
            rebuilt = LatticeStore(staging_config)
            rebuilt_digest = rebuilt.state_digest()
            if rebuilt_digest != expected_state_digest:
                raise OSError(f"rebuilt Lattice state does not match its source: expected {expected_state_digest}, got {rebuilt_digest}")
            os.replace(staging_root, data_root)
            parent_descriptor = os.open(data_root.parent, os.O_RDONLY)
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
            return rebuilt_digest
        except BaseException:
            try:
                target.rollback()
            except sqlite3.Error:
                pass
            target.close()
            if staging_root.exists():
                shutil.rmtree(staging_root)
            raise

    def record_event(self, *, project_id: str | None, event_type: str, subject_type: str, subject_id: str, payload: dict[str, Any]) -> str:
        with self.transaction() as conn:
            return self._event(conn, project_id=project_id, event_type=event_type, subject_type=subject_type, subject_id=subject_id, payload=payload)

    def latest_event_seq(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0])

    def ensure_conversation_import_setup(self, import_id: str, *, role_name: str, session_label: str, proof_limit: str, provenance: dict[str, Any]) -> dict[str, Any]:
        """Create the import session and its explicit context limit exactly once."""
        instructions = "Historical provider session. Lattice did not observe its original instruction stack; never infer missing context."
        instruction_bytes = instructions.encode("utf-8")
        instruction_sha = self.blobs.put(instruction_bytes)
        proof_bytes = proof_limit.encode("utf-8")
        proof_sha = self.blobs.put(proof_bytes)
        now = stamp()
        with self.transaction() as conn:
            import_row = self._row(conn.execute("SELECT * FROM conversation_imports WHERE import_id=?", (import_id,)).fetchone(), import_id)
            if import_row["state"] != "running":
                raise LatticeConflict("conversation import is not running")
            if import_row["role_session_id"] and import_row["proof_limit_block_id"]:
                return {"role_session_id": import_row["role_session_id"], "proof_limit_block_id": import_row["proof_limit_block_id"]}
            role = conn.execute("SELECT role_id FROM roles WHERE project_id=? AND name=?", (import_row["project_id"], role_name)).fetchone()
            if role is None:
                instruction_id = opaque_id("block")
                role_id = opaque_id("role")
                conn.execute("""INSERT INTO context_blocks(block_id,project_id,semantic_type,content_sha256,byte_count,initial_status,authority,evidence_level,sensitivity,created_at)
                              VALUES(?,?,?,?,?,'active','octopus-import','governing-instruction','private',?)""", (instruction_id, import_row["project_id"], "role-instruction", instruction_sha, len(instruction_bytes), now))
                instruction_event = self._event(conn, project_id=import_row["project_id"], event_type="context-block.created", subject_type="context-block", subject_id=instruction_id, payload={"sha256": instruction_sha, "semantic_type": "role-instruction", "status": "active"})
                conn.execute("INSERT INTO block_status(block_id,status,revision,event_id) VALUES(?,'active',1,?)", (instruction_id, instruction_event))
                conn.execute("INSERT INTO roles(role_id,project_id,name,instructions_block_id,incentives_json,authority_limits_json,workflow_role_key,route_policy_json,created_at) VALUES(?,?,?,?,?,'[\"read-only historical import\"]',NULL,'{\"kind\":\"operator-selectable\"}',?)", (role_id, import_row["project_id"], role_name, instruction_id, "[]", now))
                self._event(conn, project_id=import_row["project_id"], event_type="role.created", subject_type="role", subject_id=role_id, payload={"name": role_name, "instructions_block_id": instruction_id})
            else:
                role_id = role["role_id"]
            session_id = opaque_id("session")
            conn.execute("INSERT INTO role_sessions(role_session_id,project_id,role_id,label,created_at) VALUES(?,?,?,?,?)", (session_id, import_row["project_id"], role_id, session_label, now))
            self._event(conn, project_id=import_row["project_id"], event_type="role-session.created", subject_type="role-session", subject_id=session_id, payload={"project_id": import_row["project_id"], "role_id": role_id, "label": session_label})
            proof_id = opaque_id("block")
            conn.execute("""INSERT INTO context_blocks(block_id,project_id,semantic_type,content_sha256,byte_count,initial_status,source_ref,source_sha256,authority,evidence_level,sensitivity,transformation_json,created_at)
                          VALUES(?,?,?,?,?,'unknown',?,?,?,'explicit-proof-limit','private',?,?)""", (proof_id, import_row["project_id"], "provider-proof-limit", proof_sha, len(proof_bytes), f"lattice:blob/{proof_sha}", proof_sha, "octopus-projection", canonical_bytes(provenance).decode(), now))
            proof_event = self._event(conn, project_id=import_row["project_id"], event_type="context-block.created", subject_type="context-block", subject_id=proof_id, payload={"sha256": proof_sha, "semantic_type": "provider-proof-limit", "status": "unknown"})
            conn.execute("INSERT INTO block_status(block_id,status,revision,event_id) VALUES(?,'unknown',1,?)", (proof_id, proof_event))
            conn.execute("UPDATE conversation_imports SET role_session_id=?,proof_limit_block_id=? WHERE import_id=? AND role_session_id IS NULL AND proof_limit_block_id IS NULL", (session_id, proof_id, import_id))
            self._event(conn, project_id=import_row["project_id"], event_type="conversation-import.setup-created", subject_type="conversation-import", subject_id=import_id, payload={"role_session_id": session_id, "proof_limit_block_id": proof_id})
        return {"role_session_id": session_id, "proof_limit_block_id": proof_id}

    def claim_conversation_import_turn(self, import_id: str, *, ordinal: int, prompt_sha256: str, response_sha256: str, response_state: str = "present") -> dict[str, Any]:
        if response_state not in {"present", "absent"}:
            raise ValueError("conversation import response state must be present or absent")
        with self.transaction() as conn:
            import_row = self._row(conn.execute("SELECT project_id,state FROM conversation_imports WHERE import_id=?", (import_id,)).fetchone(), import_id)
            if import_row["state"] != "running":
                raise LatticeConflict("conversation import is not running")
            existing = conn.execute("SELECT * FROM conversation_import_turns WHERE import_id=? AND ordinal=?", (import_id, ordinal)).fetchone()
            if existing is not None:
                value = dict(existing)
                if value["prompt_sha256"] != prompt_sha256 or value["response_sha256"] != response_sha256 or value["response_state"] != response_state:
                    raise LatticeConflict("conversation import turn bytes changed during resume")
                return value
            now = stamp()
            conn.execute("INSERT INTO conversation_import_turns(import_id,ordinal,prompt_sha256,response_sha256,response_state,state,updated_at) VALUES(?,?,?,?,?,'pending',?)", (import_id, ordinal, prompt_sha256, response_sha256, response_state, now))
            self._event(conn, project_id=import_row["project_id"], event_type="conversation-import.turn-planned", subject_type="conversation-import", subject_id=import_id, payload={"ordinal": ordinal, "prompt_sha256": prompt_sha256, "response_sha256": response_sha256, "response_state": response_state})
            return dict(conn.execute("SELECT * FROM conversation_import_turns WHERE import_id=? AND ordinal=?", (import_id, ordinal)).fetchone())

    def get_conversation_import_turn(self, import_id: str, ordinal: int) -> dict[str, Any]:
        with self._connect() as conn:
            return self._row(conn.execute("SELECT * FROM conversation_import_turns WHERE import_id=? AND ordinal=?", (import_id, ordinal)).fetchone(), f"{import_id}:{ordinal}")

    def attach_imported_turn_output(self, import_id: str, *, ordinal: int, response: str, provenance: dict[str, Any], previous_invocation_id: str | None) -> dict[str, Any]:
        response_bytes = response.encode("utf-8")
        response_sha = self.blobs.put(response_bytes)
        block_id = opaque_id("block")
        now = stamp()
        with self.transaction() as conn:
            turn = self._row(conn.execute("SELECT * FROM conversation_import_turns WHERE import_id=? AND ordinal=?", (import_id, ordinal)).fetchone(), f"{import_id}:{ordinal}")
            if turn["response_state"] != "present":
                raise LatticeConflict("an unanswered imported prompt cannot acquire a historical response")
            invocation_id = turn["invocation_id"]
            if turn["state"] == "completed":
                pass
            elif turn["state"] != "invocation_sealed" or not invocation_id:
                raise LatticeConflict("conversation import turn has no sealed invocation")
            elif turn["response_sha256"] != response_sha:
                raise LatticeConflict("conversation import response changed before attachment")
            else:
                invocation = self._row(conn.execute("SELECT project_id,state,manifest_sha256 FROM invocations WHERE invocation_id=?", (invocation_id,)).fetchone(), invocation_id)
                if invocation["state"] != InvocationState.sealed.value:
                    raise LatticeConflict("only newly sealed invocations can become imported history")
                conn.execute("""INSERT INTO context_blocks(block_id,project_id,semantic_type,content_sha256,byte_count,initial_status,producing_invocation_id,source_ref,source_sha256,authority,evidence_level,sensitivity,transformation_json,created_at)
                          VALUES(?,?,?,?,?,'active',?,?,?,'octopus-projection','captured-provider-output','private',?,?)""", (block_id, invocation["project_id"], "imported-model-response", response_sha, len(response_bytes), turn["invocation_id"], f"lattice:blob/{response_sha}", response_sha, canonical_bytes(provenance).decode(), now))
                block_event = self._event(conn, project_id=invocation["project_id"], event_type="context-block.created", subject_type="context-block", subject_id=block_id, payload={"sha256": response_sha, "semantic_type": "imported-model-response", "status": "active"})
                conn.execute("INSERT INTO block_status(block_id,status,revision,event_id) VALUES(?,'active',1,?)", (block_id, block_event))
                conn.execute("UPDATE invocations SET state='imported',output_block_id=?,terminal_kind='historical-import',completed_at=? WHERE invocation_id=?", (block_id, now, invocation_id))
                self._insert_edge(conn, project_id=invocation["project_id"], relation=EdgeRelation.produced, source_type="invocation", source_id=invocation_id, source_revision=invocation["manifest_sha256"], target_type="context-block", target_id=block_id, target_revision=response_sha, metadata={"historical_import": True})
                if previous_invocation_id is not None:
                    previous = self._row(conn.execute("SELECT project_id,manifest_sha256,state FROM invocations WHERE invocation_id=?", (previous_invocation_id,)).fetchone(), previous_invocation_id)
                    if previous["project_id"] != invocation["project_id"] or previous["state"] != InvocationState.imported.value:
                        raise LatticeConflict("previous imported turn is not a completed project-local invocation")
                    self._insert_edge(conn, project_id=invocation["project_id"], relation=EdgeRelation.responds_to, source_type="invocation", source_id=invocation_id, source_revision=invocation["manifest_sha256"], target_type="invocation", target_id=previous_invocation_id, target_revision=previous["manifest_sha256"], metadata={"historical_visible_order": True, "context_inheritance_unproven": True})
                conn.execute("UPDATE conversation_import_turns SET output_block_id=?,state='completed',updated_at=? WHERE import_id=? AND ordinal=?", (block_id, now, import_id, ordinal))
                self._event(conn, project_id=invocation["project_id"], event_type="conversation-import.turn-completed", subject_type="conversation-import", subject_id=import_id, payload={"ordinal": ordinal, "invocation_id": invocation_id, "output_block_id": block_id})
        return self.get_invocation(invocation_id)

    def claim_conversation_import(self, *, project_id: str, conversation_id: str, provider: str, transcript_sha256: str, refs_sha256: str, boot_id: str) -> dict[str, Any]:
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone() is None:
                raise LatticeNotFound(project_id)
            prior = conn.execute("""SELECT * FROM conversation_imports
                                    WHERE project_id=? AND conversation_id=? AND provider=? AND transcript_sha256=? AND refs_sha256=?""", (project_id, conversation_id, provider, transcript_sha256, refs_sha256)).fetchone()
            if prior is not None:
                value = dict(prior)
                if value["state"] == "completed":
                    value["claimed"] = False
                    return value
                if value["state"] == "running":
                    raise LatticeConflict("exact conversation import is already running")
                conn.execute("UPDATE conversation_imports SET state='running',owner_boot_id=?,error_text=NULL,started_at=?,completed_at=NULL WHERE import_id=?", (boot_id, stamp(), value["import_id"]))
                self._event(conn, project_id=project_id, event_type="conversation-import.restarted", subject_type="conversation-import", subject_id=value["import_id"], payload={"conversation_id": conversation_id, "provider": provider})
                value = dict(conn.execute("SELECT * FROM conversation_imports WHERE import_id=?", (value["import_id"],)).fetchone())
                value["claimed"] = True
                return value
            import_id = opaque_id("conversation_import")
            now = stamp()
            conn.execute("""INSERT INTO conversation_imports(import_id,project_id,conversation_id,provider,transcript_sha256,refs_sha256,state,owner_boot_id,started_at)
                            VALUES(?,?,?,?,?,?,'running',?,?)""", (import_id, project_id, conversation_id, provider, transcript_sha256, refs_sha256, boot_id, now))
            self._event(conn, project_id=project_id, event_type="conversation-import.started", subject_type="conversation-import", subject_id=import_id, payload={"conversation_id": conversation_id, "provider": provider, "transcript_sha256": transcript_sha256, "refs_sha256": refs_sha256})
            value = dict(conn.execute("SELECT * FROM conversation_imports WHERE import_id=?", (import_id,)).fetchone())
            value["claimed"] = True
            return value

    def complete_conversation_import(self, import_id: str, *, result: dict[str, Any]) -> dict[str, Any]:
        raw = canonical_bytes(result)
        result_sha = self.blobs.put(raw)
        with self.transaction() as conn:
            row = self._row(conn.execute("SELECT * FROM conversation_imports WHERE import_id=?", (import_id,)).fetchone(), import_id)
            if row["state"] != "running":
                raise LatticeConflict("conversation import is not running")
            unfinished = [dict(turn) for turn in conn.execute("SELECT ordinal,response_state,state FROM conversation_import_turns WHERE import_id=? AND NOT ((response_state='present' AND state='completed') OR (response_state='absent' AND state='draft_created')) ORDER BY ordinal", (import_id,))]
            if unfinished:
                raise LatticeConflict(f"conversation import has unfinished turns: {[turn['ordinal'] for turn in unfinished]}")
            now = stamp()
            conn.execute("UPDATE conversation_imports SET state='completed',result_sha256=?,completed_at=? WHERE import_id=?", (result_sha, now, import_id))
            self._event(conn, project_id=row["project_id"], event_type="conversation-import.completed", subject_type="conversation-import", subject_id=import_id, payload={"result_sha256": result_sha})
        return self.get_conversation_import(import_id, include_result=True)

    def fail_conversation_import(self, import_id: str, *, error_text: str) -> dict[str, Any]:
        with self.transaction() as conn:
            row = self._row(conn.execute("SELECT * FROM conversation_imports WHERE import_id=?", (import_id,)).fetchone(), import_id)
            if row["state"] != "running":
                raise LatticeConflict("conversation import is not running")
            conn.execute("UPDATE conversation_imports SET state='failed',error_text=?,completed_at=? WHERE import_id=?", (error_text, stamp(), import_id))
            self._event(conn, project_id=row["project_id"], event_type="conversation-import.failed", subject_type="conversation-import", subject_id=import_id, payload={"error_text": error_text})
        return self.get_conversation_import(import_id)

    def get_conversation_import(self, import_id: str, *, include_result: bool = False) -> dict[str, Any]:
        with self._connect() as conn:
            row = self._row(conn.execute("SELECT * FROM conversation_imports WHERE import_id=?", (import_id,)).fetchone(), import_id)
            row["turns"] = [dict(item) for item in conn.execute("SELECT * FROM conversation_import_turns WHERE import_id=? ORDER BY ordinal", (import_id,))]
        if include_result and row.get("result_sha256"):
            row["result"] = json.loads(self.blobs.get(row["result_sha256"]))
        return row

    def reconcile_conversation_imports(self, boot_id: str) -> int:
        with self.transaction() as conn:
            rows = [dict(row) for row in conn.execute("SELECT import_id,project_id FROM conversation_imports WHERE state='running' AND owner_boot_id<>?", (boot_id,))]
            for row in rows:
                conn.execute("UPDATE conversation_imports SET state='interrupted',error_text='Lattice restarted before the import receipt became durable',completed_at=? WHERE import_id=?", (stamp(), row["import_id"]))
                self._event(conn, project_id=row["project_id"], event_type="conversation-import.interrupted", subject_type="conversation-import", subject_id=row["import_id"], payload={"reason": "process_restart"})
        return len(rows)

    def claim_workflow_command(self, *, project_id: str, pack_id: str, expected_workflow_revision: int, pack_canonical_sha256: str, operation: str, arguments: dict[str, Any], review: dict[str, Any], idempotency_key: str, boot_id: str) -> dict[str, Any]:
        arguments_json = canonical_bytes(arguments).decode()
        arguments_sha256 = sha256_bytes(arguments_json.encode())
        review_json = canonical_bytes(review).decode()
        review_sha256 = review.get("review_sha256")
        if not isinstance(review_sha256, str) or len(review_sha256) != 64:
            raise ValueError("workflow command review is not byte-bound")
        expected_review_sha256 = sha256_bytes(canonical_bytes({key: value for key, value in review.items() if key != "review_sha256"}))
        if review_sha256 != expected_review_sha256:
            raise ValueError("workflow command review hash is invalid")
        expected_review_claims = {"project_id": project_id, "workflow_revision": expected_workflow_revision, "pack_id": pack_id, "pack_canonical_sha256": pack_canonical_sha256, "operation": operation, "prepared_arguments_sha256": arguments_sha256}
        if any(review.get(key) != value for key, value in expected_review_claims.items()):
            raise ValueError("workflow command review does not bind the claimed command")
        with self.transaction() as conn:
            prior = conn.execute("SELECT * FROM workflow_commands WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if prior is not None:
                if prior["project_id"] != project_id or prior["pack_id"] != pack_id or prior["operation"] != operation or prior["arguments_json"] != arguments_json or prior["expected_workflow_revision"] != expected_workflow_revision or prior["pack_canonical_sha256"] != pack_canonical_sha256 or prior["review_sha256"] != review_sha256 or prior["review_json"] != review_json:
                    raise LatticeConflict("workflow idempotency key was reused for another command")
                return dict(prior)
            instance = self._row(conn.execute("SELECT p.workflow_pack,w.pack_id,w.revision,w.pack_canonical_sha256,w.current_blackboard_block_id FROM projects p JOIN workflow_instances w USING(project_id) WHERE p.project_id=?", (project_id,)).fetchone(), project_id)
            if instance["workflow_pack"] != pack_id or instance["pack_id"] != pack_id:
                raise LatticeConflict("project is not governed by this workflow pack")
            if instance["revision"] != expected_workflow_revision:
                raise LatticeConflict("workflow state changed after command review")
            if instance["pack_canonical_sha256"] != pack_canonical_sha256:
                raise LatticeConflict("workflow pack changed after command review")
            state_basis = review.get("state_basis")
            if state_basis is not None:
                if not isinstance(state_basis, dict) or set(state_basis) != {"blackboard_block_id", "blackboard_sha256", "adapter_state_sha256"}:
                    raise LatticeConflict("workflow command state basis is malformed")
                blackboard = conn.execute("SELECT content_sha256 FROM context_blocks WHERE block_id=? AND project_id=?", (state_basis["blackboard_block_id"], project_id)).fetchone()
                if instance["current_blackboard_block_id"] != state_basis["blackboard_block_id"] or blackboard is None or blackboard["content_sha256"] != state_basis["blackboard_sha256"]:
                    raise LatticeConflict("workflow authoritative state changed after command review")
            command_id = opaque_id("workflow_command")
            now = stamp()
            conn.execute("INSERT INTO workflow_commands(command_id,project_id,pack_id,operation,arguments_json,idempotency_key,expected_workflow_revision,pack_canonical_sha256,review_sha256,review_json,state,owner_boot_id,started_at) VALUES(?,?,?,?,?,?,?,?,?,?, 'running',?,?)", (command_id, project_id, pack_id, operation, arguments_json, idempotency_key, expected_workflow_revision, pack_canonical_sha256, review_sha256, review_json, boot_id, now))
            self._event(conn, project_id=project_id, event_type="workflow.command-started", subject_type="workflow-command", subject_id=command_id, payload={"pack_id": pack_id, "pack_canonical_sha256": pack_canonical_sha256, "workflow_revision": expected_workflow_revision, "operation": operation, "arguments_sha256": arguments_sha256, "argument_keys": sorted(arguments), "review_sha256": review_sha256, "idempotency_key": idempotency_key})
            return dict(conn.execute("SELECT * FROM workflow_commands WHERE command_id=?", (command_id,)).fetchone())

    def complete_workflow_command(self, command_id: str, *, result: dict[str, Any]) -> dict[str, Any]:
        result_bytes = canonical_bytes(result)
        result_sha = self.blobs.put(result_bytes)
        with self.transaction() as conn:
            command = self._row(conn.execute("SELECT * FROM workflow_commands WHERE command_id=?", (command_id,)).fetchone(), command_id)
            if command["state"] != "running":
                raise LatticeConflict("workflow command is already terminal")
            now = stamp()
            receipt = result.get("receipt", {})
            conn.execute("UPDATE workflow_commands SET state='completed',result_sha256=?,receipt_json=?,completed_at=? WHERE command_id=?", (result_sha, canonical_bytes(receipt).decode(), now, command_id))
            self._event(conn, project_id=command["project_id"], event_type="workflow.command-completed", subject_type="workflow-command", subject_id=command_id, payload={"operation": command["operation"], "result_sha256": result_sha, "receipt": receipt})
        return self.get_workflow_command(command_id, include_result=True)

    def fail_workflow_command(self, command_id: str, *, error_text: str, outcome_unknown: bool = False) -> dict[str, Any]:
        with self.transaction() as conn:
            command = self._row(conn.execute("SELECT * FROM workflow_commands WHERE command_id=?", (command_id,)).fetchone(), command_id)
            if command["state"] != "running":
                raise LatticeConflict("workflow command is already terminal")
            state = "outcome_unknown" if outcome_unknown else "failed"
            now = stamp()
            conn.execute("UPDATE workflow_commands SET state=?,error_text=?,completed_at=? WHERE command_id=?", (state, error_text, now, command_id))
            self._event(conn, project_id=command["project_id"], event_type=f"workflow.command-{state.replace('_', '-')}", subject_type="workflow-command", subject_id=command_id, payload={"operation": command["operation"], "error": error_text})
        return self.get_workflow_command(command_id)

    def get_workflow_command(self, command_id: str, *, include_result: bool = False) -> dict[str, Any]:
        with self._connect() as conn:
            row = self._row(conn.execute("SELECT * FROM workflow_commands WHERE command_id=?", (command_id,)).fetchone(), command_id)
        row["arguments"] = json.loads(row.pop("arguments_json"))
        row["review"] = json.loads(row.pop("review_json")) if row.get("review_json") else None
        row["receipt"] = json.loads(row.pop("receipt_json")) if row.get("receipt_json") else None
        if include_result and row.get("result_sha256"):
            row["result"] = json.loads(self.blobs.get(row["result_sha256"]))
        return row

    def get_workflow_command_by_idempotency(self, idempotency_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT command_id FROM workflow_commands WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        return self.get_workflow_command(row["command_id"]) if row else None

    def reconcile_workflow_commands(self, boot_id: str) -> int:
        with self._connect() as conn:
            command_ids = [row[0] for row in conn.execute("SELECT command_id FROM workflow_commands WHERE state='running' AND owner_boot_id<>?", (boot_id,))]
        for command_id in command_ids:
            self.fail_workflow_command(command_id, error_text="Lattice restarted after dispatch; authoritative external outcome must be inspected before retry", outcome_unknown=True)
        return len(command_ids)

    def advance_workflow(self, *, project_id: str, pack_id: str, expected_revision: int, from_state: str, to_state: str, evidence_block_ids: list[str], payload: dict[str, Any], trigger: dict[str, Any] | None, requires_completed_trigger: str | None = None, trigger_pack_sha256: str | None = None, minimum_evidence_blocks: int = 0, maximum_evidence_blocks: int | None = None, minimum_terminal_invocation_outputs: int = 0, terminal_outputs_after_current_state_entry: bool = False, terminal_output_requirements: list[dict[str, Any]] | None = None, evidence_requirements: list[dict[str, Any]] | None = None, expected_pack_canonical_sha256: str | None = None) -> dict[str, Any]:
        if len(evidence_block_ids) != len(set(evidence_block_ids)):
            raise LatticeConflict("workflow evidence block IDs must be unique")
        if len(evidence_block_ids) < minimum_evidence_blocks:
            raise LatticeConflict(f"workflow transition requires at least {minimum_evidence_blocks} evidence block(s)")
        if maximum_evidence_blocks is not None and len(evidence_block_ids) > maximum_evidence_blocks:
            raise LatticeConflict(f"workflow transition permits at most {maximum_evidence_blocks} evidence block(s)")
        payload_bytes = canonical_bytes(payload)
        trigger_exact_bytes = None
        trigger_completed = False
        if trigger and trigger["action"]["type"] == "freeze_payload_field":
            field = trigger["action"]["field"]
            exact_value = payload.get(field)
            if not isinstance(exact_value, str) or not exact_value:
                raise LatticeConflict(f"trigger {trigger['trigger']} requires exact string payload field {field}")
            trigger_exact_bytes = exact_value.encode("utf-8")
            trigger_completed = True
        with self.transaction() as conn:
            instance = self._row(conn.execute("SELECT * FROM workflow_instances WHERE project_id=?", (project_id,)).fetchone(), project_id)
            if instance["pack_id"] != pack_id or instance["state"] != from_state or instance["revision"] != expected_revision:
                raise LatticeConflict("workflow state changed before transition")
            if expected_pack_canonical_sha256 is not None and instance["pack_canonical_sha256"] != expected_pack_canonical_sha256:
                raise LatticeConflict("workflow pack changed before transition")
            if trigger and trigger_pack_sha256 != instance["pack_canonical_sha256"]:
                raise LatticeConflict("workflow trigger does not bind the transition pack generation")
            state_entry_seq = int(instance["state_entry_seq"])
            terminal_invocation_outputs = 0
            evidence: list[dict[str, Any]] = []
            for block_id in evidence_block_ids:
                block = self._row(conn.execute("""SELECT b.block_id,b.project_id,b.semantic_type,b.authority,b.evidence_level,b.producing_invocation_id,s.status,e.seq AS created_seq
                                                  FROM context_blocks b JOIN block_status s USING(block_id)
                                                  JOIN events e ON e.subject_type='context-block' AND e.subject_id=b.block_id AND e.event_type='context-block.created'
                                                  WHERE b.block_id=?""", (block_id,)).fetchone(), block_id)
                if block["project_id"] != project_id:
                    raise LatticeConflict("workflow evidence belongs to another project")
                block["invocation_state"] = None
                block["workflow_role_key"] = None
                block["is_terminal_output"] = False
                block["fresh_after_state_entry"] = int(block["created_seq"]) > state_entry_seq
                block["invocation_fresh_after_state_entry"] = False
                evidence.append(block)
                if block["producing_invocation_id"]:
                    invocation = conn.execute("""SELECT i.state,i.output_block_id,r.workflow_role_key,e.seq AS sealed_seq
                                                 FROM invocations i JOIN role_sessions s USING(role_session_id)
                                                 JOIN roles r USING(role_id)
                                                 JOIN events e ON e.subject_type='invocation' AND e.subject_id=i.invocation_id AND e.event_type='invocation.sealed'
                                                 WHERE i.invocation_id=?""", (block["producing_invocation_id"],)).fetchone()
                    invocation_fresh = invocation is not None and int(invocation["sealed_seq"]) > state_entry_seq and int(block["created_seq"]) > state_entry_seq
                    fresh_enough = not terminal_outputs_after_current_state_entry or invocation_fresh
                    if invocation is not None and invocation["output_block_id"] == block_id and invocation["state"] in {InvocationState.succeeded.value, InvocationState.imported.value} and fresh_enough:
                        terminal_invocation_outputs += 1
                    if invocation is not None:
                        block["invocation_state"] = invocation["state"]
                        block["workflow_role_key"] = invocation["workflow_role_key"]
                        block["is_terminal_output"] = invocation["output_block_id"] == block_id and invocation["state"] in {InvocationState.succeeded.value, InvocationState.imported.value}
                        block["invocation_fresh_after_state_entry"] = invocation_fresh
            if terminal_invocation_outputs < minimum_terminal_invocation_outputs:
                raise LatticeConflict(f"workflow transition requires {minimum_terminal_invocation_outputs} exact terminal invocation output(s)")
            for requirement in terminal_output_requirements or []:
                matching = [block for block in evidence if block["is_terminal_output"] and block["workflow_role_key"] == requirement["workflow_role_key"] and block["invocation_state"] in requirement["invocation_states"] and (not requirement.get("after_current_state_entry") or block["invocation_fresh_after_state_entry"])]
                if len(matching) < requirement["minimum_count"]:
                    raise LatticeConflict(f"workflow transition lacks role-bound terminal output requirement: {requirement}")
            for requirement in evidence_requirements or []:
                matching = [block for block in evidence if (requirement.get("semantic_type") is None or block["semantic_type"] == requirement["semantic_type"]) and (requirement.get("authority") is None or block["authority"] == requirement["authority"]) and (requirement.get("evidence_level") is None or block["evidence_level"] == requirement["evidence_level"]) and (requirement.get("status") is None or block["status"] == requirement["status"]) and (requirement.get("provenance") != "current_workflow_blackboard" or block["block_id"] == instance["current_blackboard_block_id"]) and (not requirement.get("after_current_state_entry") or int(block["created_seq"]) > state_entry_seq)]
                if len(matching) < requirement["minimum_count"]:
                    raise LatticeConflict(f"workflow transition lacks typed evidence requirement: {requirement}")
            if requires_completed_trigger:
                completed = conn.execute(
                    """SELECT t.result_ref,t.action_json,t.pack_canonical_sha256
                       FROM workflow_triggers t
                       JOIN workflow_transitions x ON x.transition_id=t.source_transition_id
                       JOIN events e ON e.subject_type='workflow-transition'
                         AND e.subject_id=x.transition_id AND e.event_type='workflow.transitioned'
                       WHERE t.project_id=? AND t.trigger_name=? AND t.state='completed'
                         AND x.to_state=? AND e.seq=? AND t.pack_canonical_sha256=?""",
                    (project_id, requires_completed_trigger, from_state, state_entry_seq, instance["pack_canonical_sha256"]),
                ).fetchone()
                if completed is None:
                    raise LatticeConflict(f"workflow cannot advance before the current state entry's trigger {requires_completed_trigger} completes")
                action = json.loads(completed["action_json"])
                if action.get("type") == "invoke_role":
                    invocation = conn.execute(
                        """SELECT i.state,r.workflow_role_key FROM invocations i
                           JOIN role_sessions s USING(role_session_id) JOIN roles r USING(role_id)
                           WHERE i.invocation_id=?""",
                        (completed["result_ref"],),
                    ).fetchone()
                    if invocation is None or invocation["state"] != InvocationState.succeeded.value or invocation["workflow_role_key"] != action["role_id"]:
                        raise LatticeConflict(f"trigger {requires_completed_trigger} has not produced a successful result from its declared role")
                elif action.get("type") == "freeze_payload_field":
                    if not isinstance(completed["result_ref"], str) or not completed["result_ref"].startswith("lattice:blob/"):
                        raise LatticeConflict(f"trigger {requires_completed_trigger} has no frozen-byte result")
                    self.blobs.get(completed["result_ref"].removeprefix("lattice:blob/"))
                else:
                    raise LatticeConflict(f"trigger {requires_completed_trigger} has an unsupported action")
            payload_sha = self.blobs.put(payload_bytes)
            trigger_result_ref = f"lattice:blob/{self.blobs.put(trigger_exact_bytes)}" if trigger_exact_bytes is not None else None
            revision = expected_revision + 1
            transition_id = opaque_id("workflow_transition")
            now = stamp()
            conn.execute("INSERT INTO workflow_transitions(transition_id,project_id,pack_id,from_state,to_state,revision,evidence_block_ids_json,payload_sha256,pack_canonical_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (transition_id, project_id, pack_id, from_state, to_state, revision, canonical_bytes(evidence_block_ids).decode(), payload_sha, instance["pack_canonical_sha256"], now))
            conn.execute("UPDATE workflow_instances SET state=?,revision=?,updated_at=? WHERE project_id=?", (to_state, revision, now, project_id))
            transition_event = self._event(conn, project_id=project_id, event_type="workflow.transitioned", subject_type="workflow-transition", subject_id=transition_id, payload={"pack_id": pack_id, "from": from_state, "to": to_state, "revision": revision, "evidence_block_ids": evidence_block_ids, "payload_sha256": payload_sha})
            transition_seq = conn.execute("SELECT seq FROM events WHERE event_id=?", (transition_event,)).fetchone()[0]
            conn.execute("UPDATE workflow_instances SET state_entry_seq=? WHERE project_id=?", (transition_seq, project_id))
            created_trigger_id = None
            if trigger:
                trigger_id = opaque_id("workflow_trigger")
                created_trigger_id = trigger_id
                trigger_state = "completed" if trigger_completed else "pending"
                result_ref = trigger_result_ref
                completed_at = now if trigger_state == "completed" else None
                action_raw = canonical_bytes(trigger["action"])
                action_sha256 = sha256_bytes(action_raw)
                conn.execute("INSERT INTO workflow_triggers(trigger_id,project_id,trigger_name,required_action,state,source_transition_id,pack_canonical_sha256,action_sha256,action_json,result_ref,error_text,created_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (trigger_id, project_id, trigger["trigger"], trigger["required_action"], trigger_state, transition_id, trigger_pack_sha256, action_sha256, action_raw.decode(), result_ref, None, now, completed_at))
                self._event(conn, project_id=project_id, event_type=f"workflow.trigger-{trigger_state}", subject_type="workflow-trigger", subject_id=trigger_id, payload={"trigger": trigger["trigger"], "required_action": trigger["required_action"], "pack_canonical_sha256": trigger_pack_sha256, "action_sha256": action_sha256, "result_ref": result_ref})
        trigger_row = self.get_workflow_trigger(created_trigger_id) if created_trigger_id else None
        return {"workflow": self.get_workflow_instance(project_id), "transition_id": transition_id, "trigger": trigger_row}

    def get_workflow_instance(self, project_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = self._row(conn.execute("SELECT * FROM workflow_instances WHERE project_id=?", (project_id,)).fetchone(), project_id)
            row["triggers"] = [dict(item) for item in conn.execute("SELECT * FROM workflow_triggers WHERE project_id=? ORDER BY created_at", (project_id,))]
        for trigger in row["triggers"]:
            trigger["action"] = json.loads(trigger.pop("action_json")) if trigger.get("action_json") else None
        row["automatic_grants"] = json.loads(row.pop("automatic_grants_json"))
        row["selector"] = json.loads(row.pop("selector_json"))
        return row

    def set_workflow_automatic_grant(self, project_id: str, *, expected_revision: int, trigger_name: str, grant: dict[str, Any] | None) -> dict[str, Any]:
        """CAS an exact pack-bound automation grant without silently surviving pack changes."""
        with self.transaction() as conn:
            instance = self._row(conn.execute("SELECT * FROM workflow_instances WHERE project_id=?", (project_id,)).fetchone(), project_id)
            if instance["revision"] != expected_revision:
                raise LatticeConflict("workflow state changed before automation consent")
            grants = [item for item in json.loads(instance["automatic_grants_json"]) if isinstance(item, dict) and item.get("trigger") != trigger_name]
            if grant is not None:
                grants.append(grant)
            revision = expected_revision + 1
            now = stamp()
            conn.execute("UPDATE workflow_instances SET automatic_grants_json=?,revision=?,updated_at=? WHERE project_id=?", (canonical_bytes(grants).decode(), revision, now, project_id))
            self._event(conn, project_id=project_id, event_type="workflow.automation-grant-changed", subject_type="workflow-instance", subject_id=project_id, payload={"trigger": trigger_name, "enabled": grant is not None, "grant": grant, "revision": revision})
        return self.get_workflow_instance(project_id)

    def refresh_workflow_blackboard(self, project_id: str, *, content: bytes, source_ref: str) -> dict[str, Any]:
        content_sha = self.blobs.put(content)
        block_id = opaque_id("block")
        now = stamp()
        with self.transaction() as conn:
            instance = self._row(conn.execute("SELECT pack_id FROM workflow_instances WHERE project_id=?", (project_id,)).fetchone(), project_id)
            previous = conn.execute("""SELECT b.block_id,s.revision FROM context_blocks b JOIN block_status s USING(block_id)
                                       JOIN events e ON e.subject_type='context-block' AND e.subject_id=b.block_id AND e.event_type='context-block.created'
                                       WHERE b.project_id=? AND b.semantic_type='workflow-blackboard-snapshot' AND s.status='active'
                                       ORDER BY e.seq DESC LIMIT 1""", (project_id,)).fetchone()
            conn.execute("""INSERT INTO context_blocks(block_id,project_id,semantic_type,content_sha256,byte_count,initial_status,source_ref,source_sha256,authority,evidence_level,sensitivity,created_at)
                         VALUES(?,?,?,?,?,'active',?,?,?,'authoritative-read','private',?)""", (block_id, project_id, "workflow-blackboard-snapshot", content_sha, len(content), source_ref, content_sha, instance["pack_id"], now))
            created_event = self._event(conn, project_id=project_id, event_type="context-block.created", subject_type="context-block", subject_id=block_id, payload={"sha256": content_sha, "semantic_type": "workflow-blackboard-snapshot", "status": "active"})
            conn.execute("INSERT INTO block_status(block_id,status,revision,event_id) VALUES(?,'active',1,?)", (block_id, created_event))
            if previous:
                revision = previous["revision"] + 1
                status_event = self._event(conn, project_id=project_id, event_type="context-block.status", subject_type="context-block", subject_id=previous["block_id"], payload={"status": "superseded", "revision": revision, "superseded_by": block_id})
                conn.execute("UPDATE block_status SET status='superseded',revision=?,event_id=? WHERE block_id=?", (revision, status_event, previous["block_id"]))
            slice_row = conn.execute("SELECT external_ref FROM slices WHERE project_id=? AND external_ref IS NOT NULL ORDER BY created_at LIMIT 1", (project_id,)).fetchone()
            if slice_row:
                external_ref = slice_row["external_ref"]
                self._insert_edge(conn, project_id=project_id, relation=EdgeRelation.concerns, source_type="context-block", source_id=block_id, source_revision=content_sha, target_type="external-concern", target_id=external_ref, target_revision=sha256_bytes(external_ref.encode("utf-8")), metadata={"workflow_pack": instance["pack_id"], "refresh": True})
            conn.execute("UPDATE workflow_instances SET current_blackboard_block_id=?,updated_at=? WHERE project_id=?", (block_id, now, project_id))
            self._event(conn, project_id=project_id, event_type="workflow.blackboard-refreshed", subject_type="workflow-instance", subject_id=project_id, payload={"block_id": block_id, "content_sha256": content_sha, "source_ref": source_ref})
        return self.get_block(block_id, include_content=False)

    def get_workflow_trigger(self, trigger_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = self._row(conn.execute(
                """SELECT t.*,x.pack_id,x.from_state,x.to_state,x.revision AS transition_revision,
                          x.evidence_block_ids_json,x.payload_sha256
                   FROM workflow_triggers t JOIN workflow_transitions x ON x.transition_id=t.source_transition_id
                   WHERE t.trigger_id=?""",
                (trigger_id,),
            ).fetchone(), trigger_id)
        row["evidence_block_ids"] = json.loads(row.pop("evidence_block_ids_json"))
        row["action"] = json.loads(row.pop("action_json")) if row.get("action_json") else None
        return row

    def plan_workflow_trigger(self, trigger_id: str, *, result_ref: str) -> dict[str, Any]:
        with self.transaction() as conn:
            trigger = self._row(conn.execute("SELECT * FROM workflow_triggers WHERE trigger_id=?", (trigger_id,)).fetchone(), trigger_id)
            if trigger["state"] != "pending":
                raise LatticeConflict("workflow trigger is not pending")
            invocation = self._row(conn.execute("SELECT project_id,manifest_sha256,role_session_id FROM invocations WHERE invocation_id=?", (result_ref,)).fetchone(), result_ref)
            if invocation["project_id"] != trigger["project_id"]:
                raise LatticeConflict("workflow trigger result belongs to another project")
            self._assert_trigger_invocation_role(conn, trigger, invocation["role_session_id"])
            conn.execute("UPDATE workflow_triggers SET state='planned',result_ref=?,error_text=NULL WHERE trigger_id=?", (result_ref, trigger_id))
            self._insert_edge(conn, project_id=trigger["project_id"], relation=EdgeRelation.triggered_by, source_type="invocation", source_id=result_ref, source_revision=invocation["manifest_sha256"], target_type="workflow-trigger", target_id=trigger_id, target_revision=trigger["source_transition_id"], metadata={"blinded": True})
            self._event(conn, project_id=trigger["project_id"], event_type="workflow.trigger-planned", subject_type="workflow-trigger", subject_id=trigger_id, payload={"result_ref": result_ref})
        return self.get_workflow_instance(trigger["project_id"])

    def fail_workflow_trigger_planning(self, trigger_id: str, *, error_text: str) -> dict[str, Any]:
        with self.transaction() as conn:
            trigger = self._row(conn.execute("SELECT * FROM workflow_triggers WHERE trigger_id=?", (trigger_id,)).fetchone(), trigger_id)
            if trigger["state"] != "pending":
                raise LatticeConflict("workflow trigger is not pending")
            conn.execute("UPDATE workflow_triggers SET error_text=? WHERE trigger_id=?", (error_text, trigger_id))
            self._event(conn, project_id=trigger["project_id"], event_type="workflow.trigger-planning-failed", subject_type="workflow-trigger", subject_id=trigger_id, payload={"error_text": error_text})
        return self.get_workflow_trigger(trigger_id)

    @staticmethod
    def _assert_trigger_invocation_role(conn: sqlite3.Connection, trigger: dict[str, Any], role_session_id: str) -> None:
        action = json.loads(trigger["action_json"]) if trigger.get("action_json") else None
        if not isinstance(action, dict) or action.get("type") != "invoke_role":
            raise LatticeConflict("workflow trigger does not admit an invocation result")
        role = conn.execute(
            """SELECT r.workflow_role_key FROM role_sessions s JOIN roles r USING(role_id)
               WHERE s.role_session_id=? AND s.project_id=?""",
            (role_session_id, trigger["project_id"]),
        ).fetchone()
        if role is None or role["workflow_role_key"] != action.get("role_id"):
            raise LatticeConflict(f"workflow trigger requires role {action.get('role_id')}")

    def record_workflow_trigger_dispatch_failure(self, trigger_id: str, *, error_text: str) -> dict[str, Any]:
        with self.transaction() as conn:
            trigger = self._row(conn.execute("SELECT * FROM workflow_triggers WHERE trigger_id=?", (trigger_id,)).fetchone(), trigger_id)
            if trigger["state"] not in {"planned", "execution_failed"}:
                raise LatticeConflict("workflow trigger has no dispatchable invocation")
            conn.execute("UPDATE workflow_triggers SET state='execution_failed',error_text=?,completed_at=NULL WHERE trigger_id=?", (error_text, trigger_id))
            self._event(conn, project_id=trigger["project_id"], event_type="workflow.trigger-dispatch-failed", subject_type="workflow-trigger", subject_id=trigger_id, payload={"error_text": error_text, "result_ref": trigger["result_ref"]})
        return self.get_workflow_trigger(trigger_id)

    def get_attempt(self, attempt_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = self._row(conn.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone(), attempt_id)
        row["runtime_receipt"] = json.loads(row.pop("runtime_receipt_json")) if row.get("runtime_receipt_json") else None
        return row

    def graph_neighborhood(
        self,
        invocation_id: str,
        depth: int = 2,
        *,
        max_nodes: int = NEIGHBORHOOD_DEFAULT_MAX_NODES,
        max_edges: int = NEIGHBORHOOD_DEFAULT_MAX_EDGES,
    ) -> dict[str, Any]:
        if depth < 0 or depth > 5:
            raise ValueError("depth must be between 0 and 5")
        if not 1 <= max_nodes <= self.NEIGHBORHOOD_MAX_NODES:
            raise ValueError(f"max_nodes must be between 1 and {self.NEIGHBORHOOD_MAX_NODES}")
        if not 1 <= max_edges <= self.NEIGHBORHOOD_MAX_EDGES:
            raise ValueError(f"max_edges must be between 1 and {self.NEIGHBORHOOD_MAX_EDGES}")
        self.get_invocation(invocation_id)
        seen: set[tuple[str, str]] = {("invocation", invocation_id)}
        frontier = set(seen)
        edges: dict[str, dict[str, Any]] = {}
        partial = False
        completed_depth = 0
        with self._connect() as conn:
            for level in range(depth):
                if not frontier:
                    completed_depth = depth
                    break
                next_frontier: set[tuple[str, str]] = set()
                for object_type, object_id in sorted(frontier):
                    rows = conn.execute(
                        """SELECT * FROM edges WHERE (source_type=? AND source_id=?) OR (target_type=? AND target_id=?)
                           ORDER BY edge_id""",
                        (object_type, object_id, object_type, object_id),
                    )
                    for row in rows:
                        edge = dict(row)
                        if edge["edge_id"] in edges:
                            continue
                        endpoints = {
                            (edge["source_type"], edge["source_id"]),
                            (edge["target_type"], edge["target_id"]),
                        }
                        additions = endpoints - seen
                        if len(edges) >= max_edges or len(seen) + len(additions) > max_nodes:
                            partial = True
                            break
                        edges[edge["edge_id"]] = edge
                        seen.update(additions)
                        next_frontier.update(additions)
                    if partial:
                        break
                if partial:
                    break
                frontier = next_frontier
                completed_depth = level + 1
            nodes: list[dict[str, Any]] = []
            for object_type, object_id in sorted(seen):
                if object_type == "invocation":
                    row = conn.execute("SELECT invocation_id AS object_id,state,actor_kind,role_session_id,output_block_id,created_at FROM invocations WHERE invocation_id=?", (object_id,)).fetchone()
                elif object_type == "context-block":
                    row = conn.execute("SELECT block_id AS object_id,semantic_type,content_sha256,created_at FROM context_blocks WHERE block_id=?", (object_id,)).fetchone()
                elif object_type == "artifact":
                    row = conn.execute("SELECT artifact_id AS object_id,artifact_kind,content_sha256,created_at FROM artifacts WHERE artifact_id=?", (object_id,)).fetchone()
                elif object_type == "workflow-trigger":
                    row = conn.execute("SELECT trigger_id AS object_id,trigger_name,state,required_action,created_at FROM workflow_triggers WHERE trigger_id=?", (object_id,)).fetchone()
                else:
                    row = None
                nodes.append({"object_type": object_type, "object_id": object_id, **(dict(row) if row else {"external": True})})
        return {
            "root": invocation_id,
            "nodes": nodes,
            "edges": list(edges.values()),
            "coverage": {
                "state": "partial" if partial else "complete",
                "requested_depth": depth,
                "completed_depth": completed_depth,
                "node_limit": max_nodes,
                "edge_limit": max_edges,
            },
        }
