"""Planted CAS identities, independent of either recovery enumerator."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from lattice.diagnostics import _referenced_blobs


def plant(blobs: Path, raw: bytes) -> str:
    digest = hashlib.sha256(raw).hexdigest()
    path = blobs / digest[:2] / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return digest


def encoded(value) -> bytes:
    return json.dumps(value, sort_keys=True).encode()


def reference(digest, prefix="lattice"):
    return f"{prefix}:blob/{digest}"


def enumerate_refs(env, reader):
    store = env["store"]
    if reader == "store":
        return store.referenced_blob_digests()
    with store._connect() as connection:
        return _referenced_blobs(connection, store.blobs.root)


def plant_conversation(env):
    store = env["store"]
    digests = [plant(store.blobs.root, label) for label in (b"request dependency", b"recovery dependency", b"checkpoint dependency")]
    # Only the selected columns contain these refs: events cannot mask omissions.
    with store.transaction() as conn:
        conn.execute("INSERT INTO conversations VALUES('conversation_fixture',?,?,?,NULL,NULL,1,?,'portable','fixture')", (env["project"]["project_id"], "Fixture", "task:fixture", json.dumps(env["route"].model_dump(mode="json"))))
        request = encoded({"prompt": "Retain this dependency", "source_ref": reference(digests[0])})
        conn.execute("INSERT INTO conversation_rounds VALUES('round_fixture','conversation_fixture',1,?,?,1,'queued',NULL,NULL,'fixture')", (request.decode(), hashlib.sha256(request).hexdigest()))
        conn.execute("INSERT INTO conversation_executions VALUES('execution_fixture','round_fixture',1,NULL,?,'fixture')", (encoded({"sources": [{"source_ref": reference(digests[1], "loom")}]}).decode(),))
        content = encoded({"provider_receipt": {"journal": {"content_ref": reference(digests[2])}}})
        conn.execute("INSERT INTO conversation_checkpoints VALUES('checkpoint_fixture','round_fixture',?,?, 'fixture')", (content.decode(), hashlib.sha256(content).hexdigest()))
    return set(digests)


def plant_native_proof(env, *, proof_raw=None):
    store = env["store"]
    leaves = set()
    proof = {"canary": "0" * 32}
    for part in ("first", "continued"):
        proof[part] = {}
        for channel in ("request", "prompt", "stdout", "terminal", "checkpoint"):
            digest = plant(store.blobs.root, f"{part} {channel} raw bytes".encode())
            leaves.add(digest)
            proof[part][channel] = reference(digest)
    digest = plant(store.blobs.root, proof_raw if proof_raw is not None else encoded(proof))
    receipt = {"contract_version": "ConversationRecoveryQualification/1.0", "evidence_refs": {"interrupted": reference(digest)}}
    with store.transaction() as conn:
        profile = conn.execute("SELECT profile_id FROM provider_profiles LIMIT 1").fetchone()[0]
        conn.execute("INSERT INTO provider_qualifications VALUES('qualification_fixture',? ,?, ?,?,'passed','fixture')", (profile, "b" * 64, encoded(receipt).decode(), hashlib.sha256(encoded(receipt)).hexdigest()))
    return digest, leaves


@pytest.mark.oracle
@pytest.mark.parametrize("reader", ["store", "diagnostics"])
def test_conversation_dependencies_are_retained(lattice_env, reader):
    expected = plant_conversation(lattice_env)
    assert expected <= enumerate_refs(lattice_env, reader)


@pytest.mark.oracle
@pytest.mark.parametrize("reader", ["store", "diagnostics"])
def test_native_proof_and_all_consumed_journal_leaves_are_retained(lattice_env, reader):
    proof, leaves = plant_native_proof(lattice_env)
    assert {proof, *leaves} <= enumerate_refs(lattice_env, reader)


@pytest.mark.oracle
@pytest.mark.parametrize("reader", ["store", "diagnostics"])
@pytest.mark.parametrize("fault", ["missing", "hash", "json", "utf8"])
def test_referenced_structured_content_must_be_present_and_verified(lattice_env, reader, fault):
    raw = b"not JSON" if fault == "json" else b"\xff" if fault == "utf8" else None
    proof, _ = plant_native_proof(lattice_env, proof_raw=raw)
    path = lattice_env["store"].blobs.root / proof[:2] / proof
    if fault == "missing":
        # Move fixture bytes aside; no workspace material is deleted.
        path.rename(path.with_suffix(".missing"))
    elif fault == "hash":
        path.write_bytes(b'{"first": {"request": "lattice:blob/' + b"a" * 64 + b'"}}')
    with pytest.raises(OSError, match="missing blob|match identity|not valid JSON"):
        enumerate_refs(lattice_env, reader)


@pytest.mark.oracle
def test_off_path_rebuild_copies_independently_planted_dependencies(lattice_env, tmp_path):
    store = lattice_env["store"]
    expected = plant_conversation(lattice_env)
    proof, leaves = plant_native_proof(lattice_env)
    expected |= {proof, *leaves}
    destination = tmp_path / "rebuilt" / "lattice.sqlite3"
    store.rebuild_database(destination)
    for digest in expected:
        copied = destination.parent / "blobs" / digest[:2] / digest
        assert hashlib.sha256(copied.read_bytes()).hexdigest() == digest


@pytest.mark.oracle
@pytest.mark.parametrize("row_factory", [None, sqlite3.Row])
def test_sealed_legacy_database_needs_no_store_or_migration(tmp_path, row_factory):
    from lattice.recovery_references import referenced_blob_digests

    database = tmp_path / "legacy.sqlite3"
    blobs = tmp_path / "blobs"
    raw = plant(blobs, b"legacy raw")
    result = plant(blobs, encoded({"receipt": {"source_ref": reference(raw, "loom")}}))
    with sqlite3.connect(database) as conn:
        conn.executescript("CREATE TABLE context_blocks(content_sha256 TEXT, transformation_json TEXT); CREATE TABLE workflow_commands(result_sha256 TEXT, receipt_json TEXT, arguments_json TEXT); CREATE TABLE workflow_triggers(result_ref TEXT);")
        conn.execute("INSERT INTO workflow_commands VALUES(?,NULL,NULL)", (result,))
        conn.execute("INSERT INTO workflow_triggers VALUES(?)", (reference(raw, "loom"),))
    with sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True) as conn:
        conn.row_factory = row_factory
        conn.execute("BEGIN")
        before = conn.total_changes
        assert referenced_blob_digests(conn, blobs) == {result, raw}
        assert _referenced_blobs(conn, blobs) == {result, raw}
        assert conn.in_transaction and conn.total_changes == before


@pytest.mark.oracle
@pytest.mark.parametrize("reader", ["shared", "diagnostics"])
def test_opaque_payloads_and_unconsumed_proof_fields_are_not_metadata(tmp_path, reader):
    from lattice.recovery_references import referenced_blob_digests

    blobs = tmp_path / "blobs"
    unrelated = plant(blobs, b"not a recovery dependency")
    leaf = plant(blobs, encoded({"user_text": reference(unrelated)}))
    proof = plant(blobs, encoded({"first": {"request": reference(leaf), "user_extension": reference(unrelated)}, "continued": {"stdout": reference(leaf)}, "notes": reference(unrelated)}))
    # An opaque context blob impersonates qualification metadata; its bytes still
    # remain payload and must not induce interpretation of another JSON object.
    opaque = plant(blobs, encoded({"contract_version": "ConversationRecoveryQualification/1.0", "evidence_refs": {"completed": reference(unrelated)}}))
    with sqlite3.connect(":memory:") as conn:
        conn.executescript("CREATE TABLE context_blocks(content_sha256 TEXT, transformation_json TEXT); CREATE TABLE provider_qualifications(receipt_json TEXT);")
        conn.execute("INSERT INTO context_blocks VALUES(?,NULL)", (opaque,))
        conn.execute("INSERT INTO provider_qualifications VALUES(?)", (encoded({"contract_version": "ConversationRecoveryQualification/1.0", "evidence_refs": {"completed": reference(proof)}}).decode(),))
        scan = referenced_blob_digests if reader == "shared" else _referenced_blobs
        assert scan(conn, blobs) == {proof, leaf, opaque}


@pytest.mark.oracle
@pytest.mark.parametrize("evidence", ["unknown-boundary", "nested-value", "other-contract"])
def test_only_consumer_recognized_qualification_slots_are_decoded(tmp_path, evidence):
    from lattice.recovery_references import referenced_blob_digests

    blobs = tmp_path / "blobs"
    raw = plant(blobs, b"opaque evidence, not a native proof")
    receipt = {"contract_version": "ConversationRecoveryQualification/1.0", "evidence_refs": {"completed": reference(raw)}}
    if evidence == "unknown-boundary":
        receipt["evidence_refs"] = {"notes": reference(raw)}
    elif evidence == "nested-value":
        receipt["evidence_refs"] = {"completed": {"user_text": reference(raw)}}
    else:
        receipt["contract_version"] = "OtherQualification/1.0"
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE provider_qualifications(receipt_json TEXT)")
        conn.execute("INSERT INTO provider_qualifications VALUES(?)", (encoded(receipt).decode(),))
        assert referenced_blob_digests(conn, blobs) == {raw}


@pytest.mark.oracle
@pytest.mark.parametrize("table,column", [("workflow_commands", "result_sha256"), ("conversation_imports", "result_sha256"), ("workflow_transitions", "payload_sha256")])
@pytest.mark.parametrize("fault", [None, "missing", "hash", "json", "symlink"])
def test_existing_structured_roots_keep_legacy_dependencies_and_reject_bad_bytes(tmp_path, table, column, fault):
    from lattice.recovery_references import referenced_blob_digests

    blobs = tmp_path / "blobs"
    leaf = plant(blobs, b"legacy receipt leaf")
    raw = b"not JSON" if fault == "json" else encoded({"receipt": {"source_ref": reference(leaf, "loom")}})
    digest = plant(blobs, raw)
    path = blobs / digest[:2] / digest
    if fault in ("missing", "symlink"):
        retained = path.with_suffix(".fixture")
        path.rename(retained)
        if fault == "symlink":
            path.symlink_to(retained)
    elif fault == "hash":
        path.write_bytes(b"{}")
    with sqlite3.connect(":memory:") as conn:
        # Other columns are empty; their schemas do not generate expected refs.
        columns = f"{column} TEXT" + (",receipt_json TEXT,arguments_json TEXT" if table == "workflow_commands" else "")
        conn.execute(f"CREATE TABLE {table}({columns})")
        conn.execute(f"INSERT INTO {table}({column}) VALUES(?)", (digest,))
        if fault is None:
            assert referenced_blob_digests(conn, blobs) == {digest, leaf}
        else:
            with pytest.raises(OSError, match="missing blob|match identity|not valid JSON"):
                referenced_blob_digests(conn, blobs)


@pytest.mark.oracle
@pytest.mark.parametrize("consumer", ["rebuild", "diagnostics"])
@pytest.mark.parametrize("fault", ["missing", "hash"])
def test_consumers_reject_missing_or_changed_nested_leaf(lattice_env, tmp_path, consumer, fault):
    from lattice.diagnostics import assess_data_root

    store = lattice_env["store"]
    _, leaves = plant_native_proof(lattice_env)
    digest = sorted(leaves)[0]
    path = store.blobs.root / digest[:2] / digest
    if fault == "missing":
        path.rename(path.with_suffix(".missing"))
    else:
        path.write_bytes(b"changed journal evidence")
    destination = tmp_path / "rejected-rebuild" / "lattice.sqlite3"
    with pytest.raises(OSError, match="missing blob|match identity"):
        if consumer == "rebuild":
            store.rebuild_database(destination)
        else:
            assess_data_root(store.config.data_root)
    assert not destination.parent.exists()


@pytest.mark.oracle
def test_legacy_run_artifact_locator_retains_only_present_artifacts(tmp_path):
    from lattice.recovery_references import referenced_blob_digests

    digests = [hashlib.sha256(label).hexdigest() for label in (b"present", b"removed", b"record", b"regeneration")]
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE run_artifacts(state TEXT, storage_locator TEXT, record_json TEXT, regeneration_json TEXT)")
        conn.execute("INSERT INTO run_artifacts VALUES('present',?,?,?)", (reference(digests[0], "loom"), encoded({"source_ref": reference(digests[2])}).decode(), encoded({"source_ref": reference(digests[3])}).decode()))
        conn.execute("INSERT INTO run_artifacts VALUES('removed',?,'{}',NULL)", (reference(digests[1]),))
        assert referenced_blob_digests(conn, tmp_path / "blobs") == {digests[0], digests[2], digests[3]}
