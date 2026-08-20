"""Truth-typed assurance for Loom's storage and context kernel."""

from __future__ import annotations

import json
import sqlite3

import pytest

from loom import Loom, LoomStore
from loom.canonical import canonical_bytes
from loom.contracts import BlockDisposition, ContextBlockCreate, ContextSelection, DraftCreate, RoleCreate
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


@pytest.mark.regression
def test_regression_trailheads_are_branch_tips_not_roots(loom_env) -> None:
    root = completed_invocation(loom_env, "root", "root answer")
    child_draft = loom_env["loom"].fork(root["invocation_id"], role_session_id=loom_env["session"]["role_session_id"], prompt="child", route=loom_env["route"])
    child = loom_env["loom"].seal(child_draft["draft_id"])

    trailhead_ids = {item["invocation_id"] for item in loom_env["store"].get_project(loom_env["project"]["project_id"])["trailheads"]}

    assert child["invocation_id"] in trailhead_ids
    assert root["invocation_id"] not in trailhead_ids


@pytest.mark.contract
def test_contract_failed_duplicate_role_does_not_orphan_instruction_block(loom_env) -> None:
    store = loom_env["store"]
    with store._connect() as conn:
        before = conn.execute("SELECT COUNT(*) FROM context_blocks WHERE project_id=?", (loom_env["project"]["project_id"],)).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        store.create_role(loom_env["project"]["project_id"], RoleCreate(name="operator", instructions="must roll back"))
    with store._connect() as conn:
        after = conn.execute("SELECT COUNT(*) FROM context_blocks WHERE project_id=?", (loom_env["project"]["project_id"],)).fetchone()[0]
    assert after == before


@pytest.mark.contract
def test_contract_summary_requires_exact_resolving_transformation_lineage(loom_env) -> None:
    store = loom_env["store"]
    source = store.create_context_block(ContextBlockCreate(project_id=loom_env["project"]["project_id"], semantic_type="evidence", content="long evidence"))
    false_summary = store.create_context_block(ContextBlockCreate(project_id=loom_env["project"]["project_id"], semantic_type="summary", content="short"))
    draft = store.create_draft(DraftCreate(project_id=loom_env["project"]["project_id"], role_session_id=loom_env["session"]["role_session_id"], prompt="use summary", route=loom_env["route"], selections=[ContextSelection(block_id=false_summary["block_id"], disposition=BlockDisposition.summarized, plane="knowledge", label="summary", derived_from_block_ids=[source["block_id"]])]))
    with pytest.raises(Exception, match="summary transformation"):
        loom_env["loom"].seal(draft["draft_id"])

    true_summary = store.create_context_block(ContextBlockCreate(project_id=loom_env["project"]["project_id"], semantic_type="summary", content="short", transformation={"kind": "summary", "source_block_ids": [source["block_id"]]}))
    valid = store.create_draft(DraftCreate(project_id=loom_env["project"]["project_id"], role_session_id=loom_env["session"]["role_session_id"], prompt="use summary", route=loom_env["route"], selections=[ContextSelection(block_id=true_summary["block_id"], disposition=BlockDisposition.summarized, plane="knowledge", label="summary", derived_from_block_ids=[source["block_id"]])]))
    assert loom_env["loom"].seal(valid["draft_id"])["state"] == "sealed"


@pytest.mark.oracle
def test_oracle_append_only_change_ledger_rebuilds_current_views_and_context(loom_env, tmp_path) -> None:
    root = completed_invocation(loom_env, "rebuild prompt", "rebuild answer")
    child = loom_env["loom"].fork(root["invocation_id"], role_session_id=loom_env["session"]["role_session_id"], prompt="continue after rebuild", route=loom_env["route"])
    sealed = loom_env["loom"].seal(child["draft_id"])
    expected_digest = loom_env["store"].state_digest()
    destination = tmp_path / "rebuilt" / "loom.sqlite3"

    rebuilt_digest = loom_env["store"].rebuild_database(destination)
    rebuilt_store = LoomStore(loom_env["config"].__class__(data_root=destination.parent, port=9010))
    inspected = Loom(rebuilt_store).inspect(sealed["invocation_id"])

    assert rebuilt_digest == expected_digest == rebuilt_store.state_digest()
    assert "continue after rebuild" in inspected["compiled_prompt"]
    with sqlite3.connect(destination) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM ledger_changes WHERE seq=(SELECT MAX(seq) FROM ledger_changes)")


@pytest.mark.oracle
def test_negative_control_missing_change_cannot_reproduce_state_digest(loom_env, tmp_path) -> None:
    completed_invocation(loom_env, "must be journaled", "terminal value")
    expected = loom_env["store"].state_digest()
    with sqlite3.connect(loom_env["config"].database) as conn:
        conn.execute("DROP TRIGGER ledger_changes_no_delete")
        conn.execute("DELETE FROM ledger_changes WHERE seq=(SELECT MAX(seq) FROM ledger_changes)")
    destination = tmp_path / "truncated" / "loom.sqlite3"

    rebuilt = loom_env["store"].rebuild_database(destination)

    assert rebuilt != expected
