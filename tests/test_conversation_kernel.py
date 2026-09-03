"""Direct-conversation actor and migration assurance."""

from __future__ import annotations

import json
import sqlite3

import pytest

from lattice import LatticeStore
from lattice.canonical import sha256_json
from lattice.config import LatticeConfig
from lattice.contracts import DirectActorBinding, DraftCreate
from lattice.manifest_reader import ManifestVerificationError, verify_and_reconstruct


def _downgrade_actor_schema(database) -> None:
    """Create a synthetic v7 table shape without using the migration under test."""

    with sqlite3.connect(database, isolation_level=None) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN EXCLUSIVE")
        try:
            for table in ("drafts", "invocations"):
                for operation in ("insert", "update", "delete"):
                    conn.execute(f'DROP TRIGGER IF EXISTS "ledger_{table}_{operation}"')
            conn.execute("DROP TRIGGER schema_migrations_no_delete")
            conn.execute("DELETE FROM schema_migrations WHERE version=8")
            conn.execute(
                """CREATE TABLE drafts_v7(
                    draft_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id),
                    role_session_id TEXT NOT NULL REFERENCES role_sessions(role_session_id),
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
                    updated_at TEXT NOT NULL
                ) STRICT"""
            )
            conn.execute(
                """CREATE TABLE invocations_v7(
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
                ) STRICT"""
            )
            conn.execute(
                """INSERT INTO drafts_v7 SELECT draft_id,project_id,role_session_id,prompt_block_id,route_json,selections_json,parents_json,relation,context_mode,response_schema_json,revision,state,invocation_id,created_at,updated_at FROM drafts"""
            )
            conn.execute(
                """INSERT INTO invocations_v7 SELECT invocation_id,project_id,role_session_id,draft_id,manifest_artifact_id,manifest_sha256,manifest_json,compiled_prompt_sha256,route_json,response_schema_json,state,output_block_id,terminal_kind,created_at,completed_at FROM invocations"""
            )
            conn.execute("DROP TABLE invocations")
            conn.execute("DROP TABLE drafts")
            conn.execute("ALTER TABLE drafts_v7 RENAME TO drafts")
            conn.execute("ALTER TABLE invocations_v7 RENAME TO invocations")
            conn.execute("CREATE INDEX invocations_project_created ON invocations(project_id,created_at)")
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()


def _downgrade_context_issuer_schema(database) -> None:
    """Create a synthetic v9 context-block table without issuer claims."""

    with sqlite3.connect(database, isolation_level=None) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN EXCLUSIVE")
        try:
            for operation in ("insert", "update", "delete"):
                conn.execute(f'DROP TRIGGER IF EXISTS "ledger_context_blocks_{operation}"')
            conn.execute("DROP TRIGGER schema_migrations_no_delete")
            conn.execute("DELETE FROM schema_migrations WHERE version=10")
            conn.execute(
                """CREATE TABLE context_blocks_v9(
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
                ) STRICT"""
            )
            conn.execute(
                """INSERT INTO context_blocks_v9
                   SELECT block_id,project_id,semantic_type,content_sha256,byte_count,initial_status,
                          producing_invocation_id,source_ref,source_sha256,authority,evidence_level,
                          sensitivity,transformation_json,created_at
                   FROM context_blocks"""
            )
            conn.execute("DROP TABLE context_blocks")
            conn.execute("ALTER TABLE context_blocks_v9 RENAME TO context_blocks")
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()


@pytest.mark.contract
def test_direct_actor_binding_rejects_implicit_or_contradictory_roles(lattice_env) -> None:
    base = {"project_id": lattice_env["project"]["project_id"], "prompt": "direct", "route": lattice_env["route"]}

    with pytest.raises(ValueError, match="explicit direct or role-session"):
        DraftCreate(**base)
    with pytest.raises(ValueError, match="cannot carry a role session"):
        DraftCreate(**base, actor_binding=DirectActorBinding(), role_session_id=lattice_env["session"]["role_session_id"])


@pytest.mark.oracle
def test_direct_manifest_contains_only_explicit_world_and_reconstructs(lattice_env) -> None:
    store = lattice_env["store"]
    draft = store.create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], actor_binding=DirectActorBinding(), prompt="No invisible role.", route=lattice_env["route"]))
    invocation = lattice_env["lattice"].seal(draft["draft_id"])

    assert invocation["actor_binding"] == {"kind": "direct"}
    assert invocation["manifest"]["contract_version"] == "LatticeContextManifest/10.0"
    assert invocation["manifest"]["actor_binding"] == {"kind": "direct"}
    assert invocation["manifest"]["role_session_id"] is None
    assert [item["label"] for item in invocation["manifest"]["segments"]] == ["user-prompt"]
    assert b"No invisible role." in verify_and_reconstruct(store, invocation["manifest"])


@pytest.mark.oracle
def test_negative_control_direct_manifest_cannot_acquire_a_hidden_role(lattice_env) -> None:
    draft = lattice_env["store"].create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], actor_binding=DirectActorBinding(), prompt="Remain direct.", route=lattice_env["route"]))
    invocation = lattice_env["lattice"].seal(draft["draft_id"])
    mutated = json.loads(json.dumps(invocation["manifest"]))
    mutated["actor_binding"] = {"kind": "role-session", "role_session_id": lattice_env["session"]["role_session_id"]}
    mutated["role_session_id"] = lattice_env["session"]["role_session_id"]
    preflight_keys = {"invocation_id", "project_id", "actor_binding", "role_session_id", "draft_id", "draft_revision", "context_mode", "ancestry", "segments", "omitted", "source_bindings", "capabilities", "execution", "response_contract", "compilation", "provider_boundary"}
    mutated["preflight_sha256"] = sha256_json({"contract_version": "LatticeContextPreflight/8.0", **{key: mutated[key] for key in preflight_keys}})
    mutated["manifest_sha256"] = sha256_json({key: value for key, value in mutated.items() if key != "manifest_sha256"})

    with pytest.raises(ManifestVerificationError, match="actor binding"):
        verify_and_reconstruct(lattice_env["store"], mutated)


@pytest.mark.regression
def test_v7_actor_migration_preserves_every_manifest_byte(lattice_env) -> None:
    store = lattice_env["store"]
    draft = store.create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], role_session_id=lattice_env["session"]["role_session_id"], prompt="legacy bytes", route=lattice_env["route"]))
    invocation = lattice_env["lattice"].seal(draft["draft_id"])
    with sqlite3.connect(store.config.database) as conn:
        before = conn.execute("SELECT manifest_json FROM invocations WHERE invocation_id=?", (invocation["invocation_id"],)).fetchone()[0]
    _downgrade_actor_schema(store.config.database)

    migrated = LatticeStore(LatticeConfig(data_root=store.config.data_root, port=store.config.port, axisvi_root=None))
    with sqlite3.connect(store.config.database) as conn:
        after = conn.execute("SELECT manifest_json FROM invocations WHERE invocation_id=?", (invocation["invocation_id"],)).fetchone()[0]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=8").fetchone()[0] == 1

    assert after == before
    assert migrated.get_invocation(invocation["invocation_id"])["actor_binding"] == {"kind": "role-session", "role_session_id": lattice_env["session"]["role_session_id"]}


@pytest.mark.regression
def test_v9_issuer_migration_preserves_manifest_bytes_and_marks_legacy_authority_unknown(lattice_env) -> None:
    store = lattice_env["store"]
    draft = store.create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], actor_binding=DirectActorBinding(), prompt="legacy issuer bytes", route=lattice_env["route"]))
    invocation = lattice_env["lattice"].seal(draft["draft_id"])
    prompt_block_id = invocation["manifest"]["segments"][-1]["block_id"]
    with sqlite3.connect(store.config.database) as conn:
        before = conn.execute("SELECT manifest_json FROM invocations WHERE invocation_id=?", (invocation["invocation_id"],)).fetchone()[0]
    _downgrade_context_issuer_schema(store.config.database)

    migrated = LatticeStore(LatticeConfig(data_root=store.config.data_root, port=store.config.port, axisvi_root=None))
    with sqlite3.connect(store.config.database) as conn:
        after = conn.execute("SELECT manifest_json FROM invocations WHERE invocation_id=?", (invocation["invocation_id"],)).fetchone()[0]
        migration_count = conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=10").fetchone()[0]
        migration_versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
        latest_image = json.loads(conn.execute("SELECT row_json FROM ledger_changes WHERE table_name='context_blocks' AND json_extract(primary_key_json,'$.block_id')=? ORDER BY seq DESC LIMIT 1", (prompt_block_id,)).fetchone()[0])

    assert after == before
    assert migration_count == 1
    assert migration_versions == list(range(1, 16))
    assert migrated.get_block(prompt_block_id, include_content=False)["issuer_kind"] == "legacy-unknown"
    assert latest_image["issuer_kind"] == "legacy-unknown"


@pytest.mark.property
def test_actor_columns_cannot_represent_an_invalid_pair(lattice_env) -> None:
    draft = lattice_env["store"].create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], actor_binding=DirectActorBinding(), prompt="bound", route=lattice_env["route"]))
    with pytest.raises(sqlite3.IntegrityError):
        with lattice_env["store"].transaction() as conn:
            conn.execute("UPDATE drafts SET role_session_id=? WHERE draft_id=?", (lattice_env["session"]["role_session_id"], draft["draft_id"]))
