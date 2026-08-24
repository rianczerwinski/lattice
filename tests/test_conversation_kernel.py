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
    assert invocation["manifest"]["contract_version"] == "LatticeContextManifest/6.0"
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
    mutated["preflight_sha256"] = sha256_json({"contract_version": "LatticeContextPreflight/4.0", **{key: mutated[key] for key in preflight_keys}})
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


@pytest.mark.property
def test_actor_columns_cannot_represent_an_invalid_pair(lattice_env) -> None:
    draft = lattice_env["store"].create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], actor_binding=DirectActorBinding(), prompt="bound", route=lattice_env["route"]))
    with pytest.raises(sqlite3.IntegrityError):
        with lattice_env["store"].transaction() as conn:
            conn.execute("UPDATE drafts SET role_session_id=? WHERE draft_id=?", (lattice_env["session"]["role_session_id"], draft["draft_id"]))
