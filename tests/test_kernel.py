"""Truth-typed assurance for Loom's storage and context kernel."""

from __future__ import annotations

import json
import sqlite3

import pytest

from loom import Loom, LoomStore
from loom.canonical import canonical_bytes
from loom.contracts import ContextBlockCreate, ContextSelection, DraftCreate
from loom.manifest_reader import ManifestVerificationError, verify_and_reconstruct

from conftest import completed_invocation


@pytest.mark.oracle
def test_oracle_manifest_compiles_the_hand_authored_context_bytes(loom_env) -> None:
    store = loom_env["store"]
    evidence = store.create_context_block(ContextBlockCreate(project_id=loom_env["project"]["project_id"], semantic_type="evidence", content="Exact source fact.", authority="source", evidence_level="verified"))
    draft = store.create_draft(DraftCreate(project_id=loom_env["project"]["project_id"], role_session_id=loom_env["session"]["role_session_id"], prompt="What follows?", route=loom_env["route"], selections=[ContextSelection(block_id=evidence["block_id"], plane="knowledge", label="source-fact")]))
    invocation = loom_env["loom"].seal(draft["draft_id"])
    manifest = invocation["manifest"]
    blocks = {item["label"]: store.get_block(item["block_id"]) for item in manifest["segments"]}
    expected = b"".join(
        (
            f"<<<LOOM {item['plane']} {item['label']} {item['block_id']} {item['sha256']}>>>\n"
            + blocks[item["label"]]["content"]
            + "\n<<<END>>>\n"
        ).encode()
        for item in manifest["segments"]
    )

    assert verify_and_reconstruct(store, manifest) == expected
    assert b"Exact source fact." in expected
    assert expected.endswith(b"What follows?\n<<<END>>>\n")


@pytest.mark.oracle
def test_negative_control_removed_context_cannot_pass_manifest_oracle(loom_env) -> None:
    draft = loom_env["store"].create_draft(DraftCreate(project_id=loom_env["project"]["project_id"], role_session_id=loom_env["session"]["role_session_id"], prompt="truth", route=loom_env["route"]))
    invocation = loom_env["loom"].seal(draft["draft_id"])
    mutated = json.loads(json.dumps(invocation["manifest"]))
    mutated["segments"] = mutated["segments"][:-1]
    mutated["manifest_sha256"] = __import__("loom.canonical", fromlist=["sha256_json"]).sha256_json({key: value for key, value in mutated.items() if key != "manifest_sha256"})

    with pytest.raises(ManifestVerificationError, match="does not reconstruct"):
        verify_and_reconstruct(loom_env["store"], mutated)


@pytest.mark.property
def test_property_two_parent_join_survives_restart_with_exact_ancestry(loom_env) -> None:
    left = completed_invocation(loom_env, "P1", "left answer")
    right = completed_invocation(loom_env, "P2", "right answer")
    draft = loom_env["loom"].join([left["invocation_id"], right["invocation_id"]], role_session_id=loom_env["session"]["role_session_id"], prompt="Compare them", route=loom_env["route"])
    joined = loom_env["loom"].seal(draft["draft_id"])

    restarted = LoomStore(loom_env["config"])
    recovered = Loom(restarted).inspect(joined["invocation_id"])

    assert {item["invocation_id"] for item in recovered["manifest"]["ancestry"]} == {left["invocation_id"], right["invocation_id"]}
    compiled = verify_and_reconstruct(restarted, recovered["manifest"])
    assert b"left answer" in compiled and b"right answer" in compiled


@pytest.mark.contract
def test_contract_event_ledger_rejects_update_and_delete(loom_env) -> None:
    database = loom_env["config"].database
    with sqlite3.connect(database) as conn:
        event_id = conn.execute("SELECT event_id FROM events LIMIT 1").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE events SET event_type='forged' WHERE event_id=?", (event_id,))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM events WHERE event_id=?", (event_id,))


@pytest.mark.contract
def test_contract_canonical_json_rejects_non_finite_values() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        canonical_bytes({"not_json": float("nan")})


@pytest.mark.contract
def test_contract_content_address_conflict_is_detected(loom_env) -> None:
    blobs = loom_env["store"].blobs
    digest = blobs.put(b"canonical")
    blobs.path_for(digest).write_bytes(b"corrupt")
    with pytest.raises(OSError, match="content-address conflict"):
        blobs.put(b"canonical")


@pytest.mark.regression
def test_default_response_schema_is_strict_for_nested_artifacts(loom_env) -> None:
    value = DraftCreate(project_id=loom_env["project"]["project_id"], role_session_id=loom_env["session"]["role_session_id"], prompt="strict schema", route=loom_env["route"])
    item = value.response_schema["properties"]["artifacts"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == {"name", "media_type", "content"}
