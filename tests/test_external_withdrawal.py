"""Never-executed disposition tested against independently derived ledger identities."""

import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from lattice import Lattice, LatticeStore
from lattice.canonical import canonical_bytes, sha256_json
from lattice.contracts import ExecutionSubmissionV1
from lattice.store import LatticeConflict


AUTHORITY = {"kind": "operator", "boundary": "authenticated-local-operator"}


def submission(env, key="withdraw-me"):
    return ExecutionSubmissionV1(external_project_key="withdrawal.fixture", operation_key=key,
        idempotency_key="withdrawal:" + key, actor="fixture", prompt="Exact request", route=env["route"])


def identity(value):
    request = value.model_dump(mode="json")
    digest = hashlib.sha256(canonical_bytes(request)).hexdigest()
    full = hashlib.sha256(canonical_bytes({"external_project_key": value.external_project_key,
        "operation_key": value.operation_key, "request_sha256": digest})).hexdigest()
    batch = "batch_" + full[:32]
    draft = "draft_" + hashlib.sha256(canonical_bytes({"batch_id": batch, "spec_id": "direct"})).hexdigest()[:32]
    invocation = "invocation_" + hashlib.sha256((draft + ":1").encode()).hexdigest()[:32]
    return digest, full, batch, draft, invocation


def reserve(env, value):
    return env["store"].reserve_external_submission(value.model_dump(mode="json"), identity(value)[0])


def withdraw(store, value, **changes):
    args = dict(expected_request_sha256=identity(value)[0], reason="Operator closes unexecuted request", authority=AUTHORITY)
    args.update(changes)
    return store.withdraw_external_submission(value.external_project_key, value.operation_key, **args)


def test_withdrawal_is_explicit_and_does_not_manufacture_execution(lattice_env, monkeypatch):
    value = submission(lattice_env)
    def refused(*args, **kwargs):
        raise LatticeConflict("planted preview refusal")
    monkeypatch.setattr(lattice_env["lattice"], "preview_invocation_batch", refused)
    with pytest.raises(LatticeConflict, match="preview refusal"):
        lattice_env["lattice"].seal_execution_submission(value)
    before = lattice_env["store"].get_external_submission(value.external_project_key, value.operation_key)
    assert before["state"] == "reserved"
    result = withdraw(lattice_env["store"], value)
    assert result["state"] == "withdrawn"
    assert result["invocation_id"] is None
    assert result["receipt"]["request_sha256"] == identity(value)[0]
    assert withdraw(lattice_env["store"], value) == result
    after = lattice_env["store"].get_external_submission(value.external_project_key, value.operation_key)
    assert after["request"] == before["request"]
    with lattice_env["store"]._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM invocations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
    with pytest.raises(LatticeConflict, match="successor"):
        lattice_env["lattice"].seal_execution_submission(value)


@pytest.mark.parametrize("change", ["sha", "reason", "authority", "idempotency"])
def test_withdrawal_exact_retries_and_successor_keys(lattice_env, change):
    value = submission(lattice_env)
    reserve(lattice_env, value)
    if change == "sha":
        with pytest.raises(LatticeConflict, match="SHA"):
            withdraw(lattice_env["store"], value, expected_request_sha256="0" * 64)
        return
    receipt = withdraw(lattice_env["store"], value)
    if change == "reason":
        with pytest.raises(LatticeConflict, match="retry differs"):
            withdraw(lattice_env["store"], value, reason="Another reason")
    elif change == "authority":
        with pytest.raises(LatticeConflict, match="authority"):
            withdraw(lattice_env["store"], value, authority={"kind": "pretend"})
    else:
        successor = value.model_copy(update={"operation_key": "successor"})
        with pytest.raises(LatticeConflict, match="idempotency key"):
            reserve(lattice_env, successor)
        successor = successor.model_copy(update={"idempotency_key": "successor:new-key"})
        assert lattice_env["lattice"].seal_execution_submission(successor)["state"] == "sealed"
    assert withdraw(lattice_env["store"], value) == receipt


def _paused_seal(env, monkeypatch):
    reached, release = threading.Event(), threading.Event()
    original = env["store"].insert_invocation_batch
    def paused(**kwargs):
        reached.set()
        assert release.wait(10), "test did not release sealer"
        return original(**kwargs)
    monkeypatch.setattr(env["store"], "insert_invocation_batch", paused)
    return reached, release


@pytest.mark.parametrize("winner", ["withdrawal", "seal"])
def test_cross_connection_commit_orders(lattice_env, monkeypatch, winner):
    value = submission(lattice_env)
    other = LatticeStore(lattice_env["config"])
    reached, release = _paused_seal(lattice_env, monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(lattice_env["lattice"].seal_execution_submission, value)
        try:
            assert reached.wait(10)
            if winner == "withdrawal":
                withdraw(other, value)
                release.set()
                with pytest.raises(LatticeConflict, match="withdrawn"):
                    pending.result(timeout=10)
            else:
                release.set()
                operation = pending.result(timeout=10)
                assert operation["state"] == "sealed"
                with pytest.raises(LatticeConflict, match="admitted"):
                    withdraw(other, value)
        finally:
            release.set()
    restarted = LatticeStore(lattice_env["config"])
    with restarted._connect() as conn:
        batches = conn.execute("SELECT COUNT(*) FROM invocation_batches").fetchone()[0]
        attempts = conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
    assert batches == (1 if winner == "seal" else 0)
    assert attempts == 0
    assert restarted.get_external_submission(value.external_project_key, value.operation_key)["state"] == ("sealed" if winner == "seal" else "withdrawn")


@pytest.mark.parametrize("admitted_attempt", [False, True])
def test_historical_hidden_batch_is_recovery_obligation(lattice_env, monkeypatch, admitted_attempt):
    value = submission(lattice_env)
    store = lattice_env["store"]
    original = store._bind_external_in_transaction
    monkeypatch.setattr(store, "_bind_external_in_transaction", lambda *args, **kwargs: None)
    lattice_env["lattice"].seal_execution_submission(value)
    digest, full, batch, draft, invocation = identity(value)
    assert store.get_external_submission(value.external_project_key, value.operation_key)["invocation_id"] is None
    if admitted_attempt:
        attempt = store.create_attempt(invocation, "planted-hidden-attempt", "fixture-boot", 9999999999)
        store.finish_attempt(attempt["attempt_id"], state=__import__('lattice.contracts', fromlist=['AttemptState']).AttemptState.failed, failure_kind="timeout", error_text="planted")
    with pytest.raises(LatticeConflict, match="admitted"):
        withdraw(store, value)
    monkeypatch.setattr(store, "_bind_external_in_transaction", original)
    recovered = store.bind_external_submission(value.external_project_key, value.operation_key, invocation)
    assert recovered["state"] == ("terminal" if admitted_attempt else "sealed")


@pytest.mark.parametrize("entrypoint", ["admit_existing_invocation_batch", "insert_invocation_batch"])
def test_external_batch_without_members_refuses_before_binding(lattice_env, monkeypatch, entrypoint):
    value = submission(lattice_env)
    store = lattice_env["store"]
    operation = reserve(lattice_env, value)
    digest, full, batch, _, _ = identity(value)
    request_sha = "a" * 64
    receipt = {"batch_id": batch, "generated_object": {"generated_at": operation["created_at"]}}
    # Plant the corrupt existing row directly: supported sealing creates members
    # atomically, so ordinary producer calls cannot express this missing data.
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO invocation_batches(batch_id,project_id,idempotency_key,request_sha256,preview_sha256,receipt_json,receipt_sha256,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (batch, operation["project_id"], "external:" + full, request_sha, "b" * 64,
             canonical_bytes(receipt).decode(), sha256_json(receipt), operation["created_at"]),
        )

    def state():
        with store._connect() as conn:
            return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                    for table in ("events", "external_operations", "attempts", "invocations", "invocation_batches", "invocation_batch_members")}

    before = state()
    assert before["invocation_batches"] and not before["invocation_batch_members"]

    def forbidden_binding(*args, **kwargs):
        pytest.fail("missing batch membership reached external binding")

    monkeypatch.setattr(store, "_bind_external_in_transaction", forbidden_binding)
    kwargs = dict(idempotency_key="external:" + full, create_request_sha256=request_sha,
                  external_binding={"external_project_key": value.external_project_key,
                                    "operation_key": value.operation_key, "expected_request_sha256": digest})
    if entrypoint == "admit_existing_invocation_batch":
        kwargs["batch_id"] = batch
    else:
        kwargs.update(receipt=receipt, prepared=[])
    try:
        with pytest.raises(LatticeConflict, match="batch has no members"):
            getattr(store, entrypoint)(**kwargs)
    finally:
        assert state() == before
        assert store.get_external_submission(value.external_project_key, value.operation_key) == operation


def test_withdrawal_fences_generic_batches_and_late_bind_without_false_events(lattice_env):
    from lattice.contracts import InvocationBatchCreateV1, InvocationBatchPreviewV1, InvocationSpecV1, ContextAssemblyV1, DirectActorBinding
    value = submission(lattice_env)
    row = reserve(lattice_env, value)
    _, full, batch, draft, invocation = identity(value)
    spec = InvocationSpecV1(spec_id="direct", assembly=ContextAssemblyV1(project_id=row["project_id"], actor_binding=DirectActorBinding(), prompt=value.prompt), route=value.route)
    preview = lattice_env["lattice"].preview_invocation_batch(InvocationBatchPreviewV1(batch_id=batch, specifications=[spec]))
    create = InvocationBatchCreateV1(batch_id=batch, specifications=[spec], idempotency_key="external:" + full, expected_preview_sha256=preview["preview_sha256"], confirmed_reason_sha256s=[x["reason_sha256"] for x in preview["confirmation_reasons"]])
    withdraw(lattice_env["store"], value)
    for candidate in (create, create.model_copy(update={"idempotency_key": "other:identity"}), create.model_copy(update={"batch_id": "batch_" + "a" * 32})):
        # A different batch ID may fail preview first, which is also refusal.
        with pytest.raises(LatticeConflict):
            lattice_env["lattice"].create_invocation_batch(candidate)
    with pytest.raises(LatticeConflict, match="withdrawn"):
        lattice_env["store"].bind_external_submission(value.external_project_key, value.operation_key, invocation)
    with lattice_env["store"]._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE event_type='execution-submission.sealed'").fetchone()[0] == 0


@pytest.mark.parametrize("boundary", ["withdrawal", "binding"])
def test_transaction_failure_never_leaves_half_admission(lattice_env, monkeypatch, boundary):
    value = submission(lattice_env)
    reserve(lattice_env, value)
    store = lattice_env["store"]
    original = store._event
    event_type = "execution-submission.withdrawal-fence" if boundary == "withdrawal" else "execution-submission.sealed"
    def fail(conn, **kwargs):
        if kwargs["event_type"] == event_type:
            raise OSError("planted precommit crash")
        return original(conn, **kwargs)
    monkeypatch.setattr(store, "_event", fail)
    with pytest.raises(OSError, match="precommit crash"):
        if boundary == "withdrawal":
            withdraw(store, value)
        else:
            lattice_env["lattice"].seal_execution_submission(value)
    restarted = LatticeStore(lattice_env["config"])
    assert restarted.get_external_submission(value.external_project_key, value.operation_key)["state"] == "reserved"
    with restarted._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM invocation_batches").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM invocations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM events WHERE event_type IN ('execution-submission.withdrawn','execution-submission.withdrawal-fence')").fetchone()[0] == 0
    if boundary == "withdrawal":
        withdraw(restarted, value)
        assert withdraw(LatticeStore(lattice_env["config"]), value)["state"] == "withdrawn"
    else:
        assert Lattice(restarted).seal_execution_submission(value)["invocation_id"] == identity(value)[4]


def test_real_authenticated_api_and_no_result_or_provider_dispatch(lattice_env, monkeypatch):
    from fastapi.testclient import TestClient
    from lattice.server import create_app
    from lattice.security import ensure_cli_token
    value = submission(lattice_env)
    reserve(lattice_env, value)
    async def forbidden(*args, **kwargs):
        raise AssertionError("provider dispatch forbidden")
    monkeypatch.setattr("lattice.supervisor.ExecutionSupervisor.execute", forbidden)
    request = dict(external_project_key=value.external_project_key, operation_key=value.operation_key, expected_request_sha256=identity(value)[0], reason="Operator closes unexecuted request")
    app = create_app(lattice_env["config"])
    with TestClient(app) as client:
        assert client.post("/api/v1/external-operations/withdraw", json=request).status_code == 403
        headers = {"Authorization": "Bearer " + ensure_cli_token(lattice_env["config"].operator_token)}
        response = client.post("/api/v1/external-operations/withdraw", json=request, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["receipt"]["authority"] == AUTHORITY
        assert client.post("/api/v1/external-operations/withdraw", json=request, headers=headers).json() == response.json()
        assert client.post("/api/v1/executions", json=value.model_dump(mode="json"), headers=headers).status_code == 409
        assert client.get("/api/v1/executions", params={"external_project_key": value.external_project_key, "operation_key": value.operation_key}, headers=headers).status_code == 409


def _verifier():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("withdrawal_independent_verifier", Path(__file__).parents[1] / "ops/verify_execution_consolidation.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_independent_verifier_counts_withdrawal_but_rejects_plain_reservation(lattice_env):
    verifier = _verifier()
    value = submission(lattice_env)
    reserve(lattice_env, value)
    with pytest.raises(verifier.ConsolidationVerificationError, match="not bound"):
        verifier.verify_database(lattice_env["config"].data_root)
    withdraw(lattice_env["store"], value)
    checked = verifier.verify_database(lattice_env["config"].data_root)
    assert checked["withdrawn_external_operations"] == 1
    assert checked["executed_external_operations"] == 0
    assert checked["invocations"] == 0


@pytest.mark.parametrize("mutation", ["receipt", "fence", "no-fence", "late-seal", "invocation-evidence", "batch-evidence", "order"])
def test_independent_verifier_rejects_rebound_lifecycle_lies(lattice_env, mutation):
    verifier = _verifier()
    value = submission(lattice_env)
    reserve(lattice_env, value)
    receipt = withdraw(lattice_env["store"], value)["receipt"]
    store = lattice_env["store"]
    with store.transaction() as conn:
        for name, in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='events'").fetchall():
            conn.execute('DROP TRIGGER "' + name + '"')
        if mutation in {"receipt", "fence"}:
            changed = dict(receipt, batch_identity="f" * 64)
            changed["receipt_sha256"] = sha256_json({key: item for key, item in changed.items() if key != "receipt_sha256"})
            encoded = canonical_bytes(changed).decode()
            event_type = "execution-submission.withdrawn" if mutation == "receipt" else "execution-submission.withdrawal-fence"
            conn.execute("UPDATE events SET payload_json=?,payload_sha256=? WHERE event_type=?", (encoded, hashlib.sha256(encoded.encode()).hexdigest(), event_type))
        elif mutation == "no-fence":
            # Change type instead of removing evidence; all payload hashes stay valid.
            conn.execute("UPDATE events SET event_type='planted.not-a-fence' WHERE event_type='execution-submission.withdrawal-fence'")
        elif mutation == "late-seal":
            store._event(conn, project_id=receipt["project_id"], event_type="execution-submission.sealed", subject_type="external-operation", subject_id=value.external_project_key + ":" + value.operation_key, payload={"invocation_id": receipt["invocation_id"]})
        elif mutation in {"invocation-evidence", "batch-evidence"}:
            store._event(conn, project_id=receipt["project_id"], event_type="invocation.sealed" if mutation == "invocation-evidence" else "invocation-batch.sealed", subject_type="invocation" if mutation == "invocation-evidence" else "invocation-batch", subject_id=receipt["invocation_id"] if mutation == "invocation-evidence" else receipt["batch_id"], payload={"planted": True})
        else:
            conn.execute("UPDATE events SET seq=seq+10000 WHERE event_type='execution-submission.reserved'")
    with pytest.raises(verifier.ConsolidationVerificationError, match="withdrawn operation"):
        verifier.verify_database(lattice_env["config"].data_root)


def test_v16_migration_preserves_rows_history_constraints_and_old_runtime_barrier(tmp_path, monkeypatch):
    import lattice.store as module
    from lattice.config import LatticeConfig
    from lattice.contracts import ProviderRoute, AttemptState
    from lattice.snapshot import pre_migration_snapshot_if_needed
    config = LatticeConfig(data_root=tmp_path / "old-schema", axisvi_root=None)
    actual_migration = module.LatticeStore._migrate_external_withdrawal
    with monkeypatch.context() as legacy:
        legacy.setattr(module, "SCHEMA_VERSION", 16)
        legacy.setattr(module, "DDL", module.DDL.replace("'terminal','withdrawn'", "'terminal'"))
        legacy.setattr(module.LatticeStore, "_migrate_external_withdrawal", lambda self, conn: None)
        old = LatticeStore(config)
        env = {"store": old, "lattice": Lattice(old), "route": ProviderRoute(provider="openai", harness="codex", model="gpt-5.6-sol")}
        pending = submission(env, "pending")
        reserve(env, pending)
        completed = submission(env, "executed")
        operation = env["lattice"].seal_execution_submission(completed)
        attempt = old.create_attempt(operation["invocation_id"], "migration-failure", "test", 9999999999)
        old.finish_attempt(attempt["attempt_id"], state=AttemptState.failed, failure_kind="timeout", error_text="fixture")
    with old._connect() as conn:
        rows = [tuple(row) for row in conn.execute("SELECT * FROM external_operations ORDER BY operation_key")]
        history = [tuple(row) for row in conn.execute("SELECT * FROM events ORDER BY seq")]
        ledger = [tuple(row) for row in conn.execute("SELECT * FROM ledger_changes ORDER BY seq")]
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 16
    # Fail inside the DDL transaction and prove rollback, before the real migration.
    original_check = module.LatticeStore._verify_current_views_against_change_ledger
    def interrupt(conn):
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='external_operations' AND sql LIKE '%withdrawn%'").fetchone():
            raise OSError("planted migration interruption")
        return original_check(conn)
    with monkeypatch.context() as fault:
        fault.setattr(module, "pre_migration_snapshot_if_needed", lambda *args, **kwargs: None)
        fault.setattr(module.LatticeStore, "_verify_current_views_against_change_ledger", staticmethod(interrupt))
        with pytest.raises(OSError, match="migration interruption"):
            LatticeStore(config)
    with old._connect() as conn:
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 16
        assert "withdrawn" not in conn.execute("SELECT sql FROM sqlite_master WHERE name='external_operations'").fetchone()[0]
    # Pre-migration snapshot copying is separately owned; this test exercises the migration itself.
    monkeypatch.setattr(module, "pre_migration_snapshot_if_needed", lambda *args, **kwargs: None)
    migrated = LatticeStore(config)
    restarted = LatticeStore(config)
    with restarted._connect() as conn:
        assert [tuple(row) for row in conn.execute("SELECT * FROM external_operations ORDER BY operation_key")] == rows
        assert [tuple(row) for row in conn.execute("SELECT * FROM events ORDER BY seq")] == history
        assert [tuple(row) for row in conn.execute("SELECT * FROM ledger_changes ORDER BY seq")] == ledger
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert len(list(conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'ledger_external_operations_%'"))) == 3
    with pytest.raises(OSError, match="newer than runtime"):
        pre_migration_snapshot_if_needed(config.data_root, target_schema_version=16)
    withdraw(migrated, pending)
    with migrated.transaction() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE external_operations SET state='invented' WHERE operation_key='pending'")


@pytest.mark.parametrize("operation", ["withdrawal", "seal"])
@pytest.mark.parametrize("boundary", ["before", "after"])
def test_process_death_commit_boundary(lattice_env, operation, boundary):
    import subprocess
    import sys
    value = submission(lattice_env)
    reserve(lattice_env, value)
    script = '''
import json, os, sys
from lattice import Lattice, LatticeStore, LatticeConfig
from lattice.contracts import ExecutionSubmissionV1
settings = json.load(sys.stdin)
store = LatticeStore(LatticeConfig(data_root=__import__('pathlib').Path(settings['root']), axisvi_root=None))
value = ExecutionSubmissionV1.model_validate(settings['request'])
original = store._event
def interrupted(conn, **kwargs):
    result = original(conn, **kwargs)
    if kwargs['event_type'] == settings['event']:
        os._exit(73)
    return result
if settings['boundary'] == 'before':
    store._event = interrupted
if settings['operation'] == 'withdrawal':
    store.withdraw_external_submission(value.external_project_key, value.operation_key, expected_request_sha256=settings['sha'], reason='Operator closes unexecuted request', authority=settings['authority'])
else:
    Lattice(store).seal_execution_submission(value)
os._exit(73)
'''
    settings = dict(root=str(lattice_env["config"].data_root), request=value.model_dump(mode="json"), operation=operation, boundary=boundary, sha=identity(value)[0], authority=AUTHORITY, event="execution-submission.withdrawal-fence" if operation == "withdrawal" else "execution-submission.sealed")
    result = subprocess.run([sys.executable, "-c", script], input=json.dumps(settings), text=True, capture_output=True, timeout=20)
    assert result.returncode == 73, result.stderr
    restarted = LatticeStore(lattice_env["config"])
    row = restarted.get_external_submission(value.external_project_key, value.operation_key)
    assert row["state"] == ("reserved" if boundary == "before" else "withdrawn" if operation == "withdrawal" else "sealed")
    with restarted._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM invocation_batches").fetchone()[0] == (1 if boundary == "after" and operation == "seal" else 0)
    if boundary == "after" and operation == "withdrawal":
        with pytest.raises(LatticeConflict, match="successor"):
            Lattice(restarted).seal_execution_submission(value)


def test_client_and_cli_use_distinct_authenticated_control_contract(monkeypatch, capsys):
    from lattice.client import LatticeClient
    from lattice.cli import main
    from lattice.contracts import ExternalOperationWithdrawalRequestV1
    calls = []
    def request(self, method, path, payload):
        calls.append((method, path, payload))
        return {"contract_version": "ExternalOperationWithdrawal/1.0", "state": "withdrawn", "invocation_id": None, "receipt": {"fixture": True}}
    monkeypatch.setattr(LatticeClient, "_request", request)
    monkeypatch.setattr(LatticeClient, "from_environment", classmethod(lambda cls: cls("http://unused", "fixture", "unused")))
    value = ExternalOperationWithdrawalRequestV1(external_project_key="fixture", operation_key="never-executed", expected_request_sha256="1" * 64, reason="Close reservation")
    assert LatticeClient.from_environment().withdraw_external_operation(value).invocation_id is None
    with pytest.raises(SystemExit) as exit:
        main(["withdraw-operation", "--external-project-key", "fixture", "--operation-key", "never-executed", "--expected-request-sha256", "1" * 64, "--reason", "Close reservation"])
    assert exit.value.code == 0
    assert json.loads(capsys.readouterr().out)["state"] == "withdrawn"
    assert calls == [("POST", "/api/v1/external-operations/withdraw", value.model_dump(mode="json"))] * 2


def test_independent_verifier_refuses_hidden_batch_rows_with_valid_event_hashes(lattice_env, monkeypatch):
    verifier = _verifier()
    value = submission(lattice_env)
    store = lattice_env["store"]
    # Construct a real historically unbound batch, then plant a false producer
    # absence decision. Hide subject-level clues so the base-row oracle matters.
    monkeypatch.setattr(store, "_bind_external_in_transaction", lambda *args, **kwargs: None)
    lattice_env["lattice"].seal_execution_submission(value)
    monkeypatch.setattr(store, "_require_unadmitted", lambda *args: None)
    withdraw(store, value)
    with store.transaction() as conn:
        for name, in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='events'").fetchall():
            conn.execute('DROP TRIGGER "' + name + '"')
        conn.execute("UPDATE events SET subject_id='planted-hidden-subject' WHERE event_type IN ('draft.created','invocation.sealed','invocation-batch.sealed')")
        assert all(hashlib.sha256(row["payload_json"].encode()).hexdigest() == row["payload_sha256"] for row in conn.execute("SELECT * FROM events"))
    with pytest.raises(verifier.ConsolidationVerificationError, match="admitted batch or execution evidence"):
        verifier.verify_database(lattice_env["config"].data_root)
