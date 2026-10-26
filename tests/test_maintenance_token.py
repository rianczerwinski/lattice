"""Planted SQLite/control mutations exercise bounded maintenance identity.

Oracle authority: independently committed SQL mutations and retained snapshot
bytes. No provider, launchd, live data, or installer execution is involved.
"""
from __future__ import annotations

import json
import select
import shutil
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from lattice import LatticeConfig, LatticeStore
from lattice.contracts import ProjectCreate
from ops import install as deployment


@pytest.fixture
def baseline(tmp_path, monkeypatch):
    config = LatticeConfig(data_root=tmp_path / "state", axisvi_root=None)
    store = LatticeStore(config)
    project = store.create_project(ProjectCreate(title="baseline"))
    monkeypatch.setattr(deployment, "DEFAULT_DATA_ROOT", config.data_root)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    with closing(sqlite3.connect(snapshot / "lattice.sqlite3")) as destination, destination, closing(sqlite3.connect(config.database)) as source, source:
        source.backup(destination)
    digest, version = deployment.live_database_generation()
    (snapshot / "snapshot.json").write_text(json.dumps({"state_digest": digest, "schema_version": version}))
    target = tmp_path / "candidate"
    target.mkdir()
    (target / "runtime.json").write_text('{"runtime_id":"candidate"}')
    return config, store, project, snapshot, target


@contextmanager
def qualified(baseline):
    config, _store, _project, snapshot, target = baseline
    with deployment.qualified_cutover_identity(target, snapshot) as identity:
        deployment.verify_rollback_snapshot_current(snapshot)
        identity.bind_snapshot(snapshot)
        identity.assert_unchanged()
        identity.qualified = True
        yield identity


def change(store, sql, parameters=(), *, replay=False):
    with store.transaction() as connection:
        if replay:
            connection.create_function("lattice_replaying", 0, lambda: 1)
        connection.execute(sql, parameters)


@pytest.mark.oracle
def test_unchanged_and_checkpoint_are_valid(baseline):
    config, _store, _project, _snapshot, _target = baseline
    with qualified(baseline) as identity:
        assert not identity.connection.in_transaction
        with closing(sqlite3.connect(config.database)) as writer, writer:
            writer.execute("PRAGMA wal_checkpoint(PASSIVE)")
        identity.verify()
        assert not identity.connection.in_transaction


@pytest.mark.oracle
@pytest.mark.parametrize("mutation", ["event", "update", "delete", "lower-migration", "drop-trigger", "unknown-table", "replace-trigger"])
def test_committed_mutation_invalidates_cutover(baseline, mutation):
    _config, store, project, _snapshot, _target = baseline
    with qualified(baseline) as identity:
        if mutation == "event":
            with store.transaction() as connection:
                store._event(connection, project_id=None, event_type="token-control", subject_type="test", subject_id="x", payload={})
        else:
            sql = {
                "update": "UPDATE projects SET title='changed' WHERE project_id=?",
                "delete": "DELETE FROM projects WHERE project_id=?",
                "lower-migration": "INSERT INTO schema_migrations VALUES(-1,'control')",
                "drop-trigger": "DROP TRIGGER ledger_projects_update",
                "unknown-table": "CREATE TABLE unclassified(id INTEGER PRIMARY KEY)",
                "replace-trigger": "CREATE TRIGGER control_extra AFTER INSERT ON projects BEGIN SELECT 1; END",
            }[mutation]
            change(store, sql, (project["project_id"],) if mutation in ("update", "delete") else ())
        with pytest.raises(deployment.RollbackSnapshotStale):
            identity.verify()


@pytest.mark.oracle
def test_replay_bypass_is_detected_without_chain_movement(baseline):
    config, store, project, _snapshot, _target = baseline
    with qualified(baseline) as identity:
        before = deployment.append_stream_heads(config.database)
        change(store, "UPDATE projects SET title='bypass' WHERE project_id=?", (project["project_id"],), replay=True)
        assert deployment.append_stream_heads(config.database) == before, "the planted bypass must evade the durable chain token"
        with pytest.raises(deployment.RollbackSnapshotStale):
            identity.verify()


@pytest.mark.oracle
def test_connection_loss_and_unqualified_identity_refuse(baseline):
    config, *_rest = baseline
    identity = deployment.CutoverIdentity(config.database)
    try:
        with pytest.raises(deployment.RollbackSnapshotStale, match="independently verified"):
            identity.verify()
        identity.qualified = True
        identity.connection.close()
        with pytest.raises(deployment.RollbackSnapshotStale, match="invalidated"):
            identity.verify()
    finally:
        identity.close()


@pytest.mark.oracle
def test_identical_database_replacement_refuses(baseline, tmp_path):
    config, _store, _project, snapshot, _target = baseline
    with qualified(baseline) as identity:
        replacement = tmp_path / "replacement.sqlite3"
        shutil.copyfile(snapshot / "lattice.sqlite3", replacement)
        replacement.replace(config.database)
        with pytest.raises(deployment.RollbackSnapshotStale):
            identity.verify()


@pytest.mark.oracle
@pytest.mark.parametrize("control", ["snapshot", "runtime"])
def test_bound_control_bytes_refuse(baseline, control):
    _config, _store, _project, snapshot, target = baseline
    with qualified(baseline) as identity:
        path = snapshot / "snapshot.json" if control == "snapshot" else target / "runtime.json"
        path.write_text(path.read_text() + " ")
        with pytest.raises(deployment.RollbackSnapshotStale, match="publication input"):
            identity.verify()


@pytest.mark.oracle
@pytest.mark.parametrize("mutation", ["trigger", "table", "migration"])
def test_preexisting_coverage_or_snapshot_mismatch_refuses(baseline, mutation):
    config, store, _project, snapshot, target = baseline
    sql = {"trigger": "DROP TRIGGER ledger_projects_delete", "table": "CREATE TABLE unwired(id INTEGER PRIMARY KEY)", "migration": "INSERT INTO schema_migrations VALUES(-2,'before-sentinel')"}[mutation]
    change(store, sql)
    with pytest.raises(deployment.RollbackSnapshotStale):
        with deployment.qualified_cutover_identity(target, snapshot) as identity:
            identity.bind_snapshot(snapshot)


@pytest.mark.oracle
def test_full_scan_is_online_and_final_checks_hold_writer_exclusion(baseline, monkeypatch):
    config, store, _project, snapshot, target = baseline
    prior = deployment.PublicationState(None, None, None, None, None, False, False)
    monkeypatch.setattr(deployment, "capture_publication_state", lambda: prior)
    monkeypatch.setattr(deployment, "reuse_rollback_snapshot", lambda _snapshot: snapshot)
    monkeypatch.setattr(deployment, "installed_runtime_version", lambda _target: "0.0.0")
    stopped = []
    guarded = []
    real_compare = deployment.verify_rollback_snapshot_current
    def compare(path):
        assert not guarded, "growing-history equality must finish before admission lock"
        real_compare(path)
    monkeypatch.setattr(deployment, "verify_rollback_snapshot_current", compare)
    real_guard = deployment.upgrade_quiescence
    @contextmanager
    def guard():
        with real_guard():
            guarded.append(True)
            yield
            guarded.clear()
    monkeypatch.setattr(deployment, "upgrade_quiescence", guard)
    def stop(label):
        assert guarded
        with closing(sqlite3.connect(config.database, timeout=0)) as writer, writer:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                writer.execute("BEGIN IMMEDIATE")
        stopped.append(label)
    monkeypatch.setattr(deployment, "unload_job", stop)
    monkeypatch.setattr(deployment, "publish_runtime", lambda *_a, **_k: None)
    deployment.perform_upgrade(target, rollback_snapshot=snapshot, verify_candidate=lambda *_a: None, before_cutover=lambda: None)
    assert stopped == [deployment.SERVICE_LABEL, deployment.BACKUP_LABEL]


@pytest.mark.oracle
@pytest.mark.parametrize("drift", ["publication", "database", "runtime"])
def test_final_drift_refuses_before_stop(baseline, monkeypatch, drift):
    _config, store, project, snapshot, target = baseline
    prior = deployment.PublicationState(None, None, None, None, None, False, False)
    current = [prior]
    monkeypatch.setattr(deployment, "capture_publication_state", lambda: current[0])
    monkeypatch.setattr(deployment, "reuse_rollback_snapshot", lambda _path: snapshot)
    stops = []
    monkeypatch.setattr(deployment, "unload_job", stops.append)
    def mutate():
        if drift == "publication":
            current[0] = deployment.PublicationState(None, None, deployment.FileState(b"changed", 0o700), None, None, False, False)
        elif drift == "runtime":
            (target / "runtime.json").write_text('{}')
        else:
            change(store, "UPDATE projects SET title='late' WHERE project_id=?", (project["project_id"],))
    with pytest.raises(deployment.RollbackSnapshotStale):
        deployment.perform_upgrade(target, rollback_snapshot=snapshot, verify_candidate=lambda *_a: None, before_cutover=mutate)
    assert stops == []
    assert not deployment.maintenance_record_path().exists()


@pytest.mark.oracle
def test_candidate_boot_and_completed_admission_have_different_heads(baseline):
    config, store, _project, _snapshot, _target = baseline
    before = deployment.append_stream_heads(config.database)
    LatticeStore(config)
    assert deployment.append_stream_heads(config.database) == before
    with store.transaction() as connection:
        store._event(connection, project_id=None, event_type="candidate-completed", subject_type="test", subject_id="finished", payload={})
    assert deployment.append_stream_heads(config.database) != before


@pytest.mark.oracle
def test_token_queries_do_not_scan_history(baseline):
    config, *_rest = baseline
    connection = sqlite3.connect(config.database)
    statements = []
    connection.set_trace_callback(statements.append)
    try:
        deployment.database_change_token(connection)
    finally:
        connection.close()
    history = [sql for sql in statements if any(f'FROM "{table}"' in sql for table in ("events", "ledger_changes", "append_integrity"))]
    assert len(history) == 3 and all("ORDER BY seq DESC LIMIT 1" in sql for sql in history)
    assert not any("COUNT(" in sql.upper() for sql in statements)


@pytest.mark.oracle
def test_snapshot_cookie_and_physical_layout_are_not_source_identity(baseline):
    _config, _store, _project, snapshot, _target = baseline
    with closing(sqlite3.connect(snapshot / "lattice.sqlite3")) as connection, connection:
        connection.execute("PRAGMA schema_version=123456")
        connection.execute("VACUUM")
    with qualified(baseline) as identity:
        identity.verify()


@pytest.mark.oracle
@pytest.mark.parametrize("kind", ["event", "insert", "update", "delete"])
def test_chain_token_itself_covers_event_and_tracked_changes(baseline, kind):
    config, store, project, _snapshot, _target = baseline
    def token():
        connection = sqlite3.connect(config.database)
        try:
            return deployment.database_change_token(connection)
        finally:
            connection.close()
    before = token()
    if kind == "event":
        with store.transaction() as connection:
            store._event(connection, project_id=None, event_type="event-only", subject_type="test", subject_id="x", payload={})
    elif kind == "insert":
        store.create_project(ProjectCreate(title="new tracked row"))
    else:
        sql = "UPDATE projects SET title='changed' WHERE project_id=?" if kind == "update" else "DELETE FROM projects WHERE project_id=?"
        change(store, sql, (project["project_id"],))
    after = token()
    assert after["heads"]["append_integrity"] != before["heads"]["append_integrity"]
    if kind == "event":
        assert after["heads"]["ledger_changes"] == before["heads"]["ledger_changes"]
        assert after["heads"]["events"] != before["heads"]["events"]


@pytest.mark.oracle
def test_noop_tracking_trigger_is_not_qualified(baseline):
    config, store, _project, _snapshot, _target = baseline
    with store.transaction() as connection:
        connection.execute("DROP TRIGGER ledger_projects_update")
        connection.execute("CREATE TRIGGER ledger_projects_update AFTER UPDATE ON projects BEGIN SELECT 1; END")
    with pytest.raises(deployment.RollbackSnapshotStale, match="trigger coverage"):
        deployment.CutoverIdentity(config.database)


@pytest.mark.oracle
def test_busy_guard_with_real_online_identity_never_stops_service(baseline, monkeypatch):
    _config, _store, _project, snapshot, target = baseline
    prior = deployment.PublicationState(None, None, None, None, None, False, False)
    monkeypatch.setattr(deployment, "capture_publication_state", lambda: prior)
    monkeypatch.setattr(deployment, "reuse_rollback_snapshot", lambda _path: snapshot)
    stopped = []
    monkeypatch.setattr(deployment, "unload_job", stopped.append)
    @contextmanager
    def busy():
        raise RuntimeError("busy: planted active admission guard")
        yield
    monkeypatch.setattr(deployment, "upgrade_quiescence", busy)
    with pytest.raises(RuntimeError, match="busy"):
        deployment.perform_upgrade(target, rollback_snapshot=snapshot, verify_candidate=lambda *_a: None, before_cutover=lambda: None)
    assert stopped == [] and not deployment.maintenance_record_path().exists()


@pytest.mark.oracle
def test_preexisting_replay_divergence_needs_semantic_binding(baseline, monkeypatch):
    _config, store, project, snapshot, target = baseline
    change(store, "UPDATE projects SET title='preexisting bypass' WHERE project_id=?", (project["project_id"],), replay=True)
    prior = deployment.PublicationState(None, None, None, None, None, False, False)
    monkeypatch.setattr(deployment, "capture_publication_state", lambda: prior)
    monkeypatch.setattr(deployment, "reuse_rollback_snapshot", lambda _path: snapshot)
    stopped = []
    monkeypatch.setattr(deployment, "unload_job", stopped.append)
    with pytest.raises(deployment.RollbackSnapshotStale, match="stale at cutover"):
        deployment.perform_upgrade(target, rollback_snapshot=snapshot, verify_candidate=lambda *_a: None, before_cutover=lambda: None)
    assert stopped == []


@pytest.mark.oracle
def test_current_compressed_snapshot_binds_through_materialization(baseline):
    from lattice.diagnostics import assess_backup
    from lattice.snapshot import create_snapshot
    config, _store, _project, _legacy_snapshot, target = baseline
    digest, schema = deployment.live_database_generation()
    snapshot = create_snapshot(config.data_root, reason="bounded-token-control", source_revision=digest, schema_version=schema, state_digest=digest)
    assert assess_backup(snapshot)["state_digest"] == digest
    with deployment.qualified_cutover_identity(target, snapshot) as identity:
        deployment.verify_rollback_snapshot_current(snapshot)
        identity.bind_snapshot(snapshot)
        identity.assert_unchanged()
        identity.qualified = True
        identity.verify()


# Copied from the supported pre-rename fixture in test_kernel.py, rather than
# generated by the comparison implementation under test.
HISTORICAL_EVENT_TRIGGER = """CREATE TRIGGER events_append_integrity AFTER INSERT ON events WHEN loom_replaying()=0 BEGIN
    INSERT INTO append_integrity(stream,source_seq,prev_sha256,entry_sha256,created_at)
    VALUES('event',NEW.seq,COALESCE((SELECT entry_sha256 FROM append_integrity ORDER BY seq DESC LIMIT 1),'0000000000000000000000000000000000000000000000000000000000000000'),loom_integrity_hash('event',COALESCE((SELECT entry_sha256 FROM append_integrity ORDER BY seq DESC LIMIT 1),'0000000000000000000000000000000000000000000000000000000000000000'),NEW.seq,NEW.event_id,NEW.project_id,NEW.event_type,NEW.subject_type,NEW.subject_id,NEW.payload_json,NEW.payload_sha256,NEW.occurred_at),loom_stamp());
END;"""


def legacy_manifest(baseline, extra_files=0):
    """Independently authored Backup/1 fixture, admitted by the real verifier."""
    from lattice.canonical import canonical_bytes
    from lattice.snapshot import _inventory
    config, _store, _project, snapshot, _target = baseline
    # Preserve the real backup API's WAL header; readers must isolate side effects.
    assert (snapshot / "lattice.sqlite3").read_bytes()[18:20] == b"\x02\x02"
    if config.blobs.exists():
        shutil.copytree(config.blobs, snapshot / "blobs", dirs_exist_ok=True)
    journals = snapshot / "model-runs" / "historical"
    journals.mkdir(parents=True, exist_ok=True)
    (journals / "stdout.log").write_bytes(b"")
    (journals / "stderr.log").write_bytes(b"")
    (journals / "progress.jsonl").write_bytes(b"")
    (journals / "start.json").write_text(json.dumps({"event": "start", "invocation_id": "historical"}))
    for index in range(extra_files):
        (journals / f"{index}.log").write_bytes(b"retained journal")
    digest, version = deployment.live_database_generation()
    manifest = {
        "contract_version": "LatticeBackup/1.0", "state_digest": digest, "schema_version": version,
        "generated_object": {
            "contract_version": "GeneratedObject/1.0", "role": "snapshot", "kind": "lattice-backup",
            "schema": {"name": "lattice-backup", "version": 1}, "artifact_id": "legacy-control",
            "generated_at": "20261026T000000", "producer": {"name": "lattice", "version": "0.1.0"},
            "subject": "lattice:canonical-runtime-state", "sources": [{"ref": "lattice:state", "revision": digest}],
        },
        "inventory": _inventory(snapshot),
    }
    (snapshot / "snapshot.json").write_bytes(canonical_bytes(manifest))


def mocked_publication(monkeypatch):
    prior = deployment.PublicationState(None, None, None, None, None, False, False)
    monkeypatch.setattr(deployment, "capture_publication_state", lambda: prior)
    monkeypatch.setattr(deployment, "installed_runtime_version", lambda _target: "0.0.0")
    calls = []
    monkeypatch.setattr(deployment, "unload_job", calls.append)
    monkeypatch.setattr(deployment, "publish_runtime", lambda *_args, **_kwargs: calls.append("publish"))
    return calls


@pytest.mark.oracle
@pytest.mark.parametrize("mutation", ["valid", "noop", "hash-argument"])
def test_historical_trigger_through_verified_upgrade(baseline, monkeypatch, mutation):
    config, store, _project, snapshot, target = baseline
    with store.transaction() as connection:
        connection.execute("DROP TRIGGER events_append_integrity")
        sql = HISTORICAL_EVENT_TRIGGER
        if mutation == "noop":
            sql = "CREATE TRIGGER events_append_integrity AFTER INSERT ON events BEGIN SELECT 1; END"
        elif mutation == "hash-argument":
            sql = sql.replace("NEW.payload_sha256,NEW.occurred_at", "NEW.payload_json,NEW.occurred_at")
        connection.executescript(sql)
    if mutation == "valid":
        store.record_event(project_id=None, event_type="historical-control", subject_type="test", subject_id="historical", payload={})
    # Fresh recovery bytes retain the exact historical schema.
    with closing(sqlite3.connect(snapshot / "lattice.sqlite3")) as destination, destination, closing(sqlite3.connect(config.database)) as source, source:
        source.backup(destination)
    legacy_manifest(baseline)
    calls = mocked_publication(monkeypatch)
    from lattice.diagnostics import assess_backup
    def verify(_target, path):
        assert calls == []
        assess_backup(path)
    if mutation == "valid":
        deployment.perform_upgrade(target, rollback_snapshot=snapshot, verify_candidate=verify, before_cutover=lambda: None)
        assert calls == [deployment.SERVICE_LABEL, deployment.BACKUP_LABEL, "publish"]
    else:
        with pytest.raises(deployment.RollbackSnapshotStale, match="trigger"):
            deployment.perform_upgrade(target, rollback_snapshot=snapshot, verify_candidate=verify, before_cutover=lambda: None)
        assert calls == []


@pytest.mark.oracle
@pytest.mark.skipif(not hasattr(select, "kqueue"), reason="native macOS manifest byte bound")
@pytest.mark.parametrize("inventory_size", [0, 1000])
def test_legacy_manifest_reads_finish_online(baseline, monkeypatch, inventory_size):
    _config, _store, _project, snapshot, target = baseline
    legacy_manifest(baseline, inventory_size)
    calls = mocked_publication(monkeypatch)
    inside = []
    real_guard = deployment.upgrade_quiescence
    real_open = Path.open
    @contextmanager
    def guard():
        with real_guard():
            inside.append(True)
            try:
                yield
            finally:
                inside.clear()
    def observed_open(path, *args, **kwargs):
        if path == snapshot / "snapshot.json":
            assert not inside, "legacy inventory bytes were read under final exclusion"
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(deployment, "upgrade_quiescence", guard)
    monkeypatch.setattr(Path, "open", observed_open)
    from lattice.diagnostics import assess_backup
    deployment.perform_upgrade(target, rollback_snapshot=snapshot, verify_candidate=lambda _t, s: assess_backup(s), before_cutover=lambda: None)
    assert calls == [deployment.SERVICE_LABEL, deployment.BACKUP_LABEL, "publish"]


@pytest.mark.oracle
def test_replay_during_online_comparison_refuses_before_stop(baseline, monkeypatch):
    config, store, project, snapshot, target = baseline
    legacy_manifest(baseline)
    calls = mocked_publication(monkeypatch)
    real_compare = deployment.verify_rollback_snapshot_current
    real_generation = deployment.live_database_generation
    def interleaved(path):
        def generation():
            result = real_generation()
            before = deployment.append_stream_heads(config.database)
            change(store, "UPDATE projects SET title='interleaved bypass' WHERE project_id=?", (project["project_id"],), replay=True)
            assert deployment.append_stream_heads(config.database) == before
            return result
        with monkeypatch.context() as scope:
            scope.setattr(deployment, "live_database_generation", generation)
            real_compare(path)
    monkeypatch.setattr(deployment, "verify_rollback_snapshot_current", interleaved)
    with pytest.raises(deployment.RollbackSnapshotStale, match="changed during online"):
        deployment.perform_upgrade(target, rollback_snapshot=snapshot, verify_candidate=lambda *_a: None, before_cutover=lambda: None)
    assert calls == [] and not deployment.maintenance_record_path().exists()


@pytest.mark.oracle
@pytest.mark.parametrize("mutation", ["valid-sibling", "altered-literal", "altered-hash"])
def test_historical_sibling_chain_and_literal_preservation(baseline, mutation):
    config, store, _project, _snapshot, _target = baseline
    sibling = """CREATE TRIGGER ledger_changes_append_integrity AFTER INSERT ON ledger_changes WHEN loom_replaying()=0 BEGIN
    INSERT INTO append_integrity(stream,source_seq,prev_sha256,entry_sha256,created_at)
    VALUES('ledger-change',NEW.seq,COALESCE((SELECT entry_sha256 FROM append_integrity ORDER BY seq DESC LIMIT 1),'0000000000000000000000000000000000000000000000000000000000000000'),loom_integrity_hash('ledger-change',COALESCE((SELECT entry_sha256 FROM append_integrity ORDER BY seq DESC LIMIT 1),'0000000000000000000000000000000000000000000000000000000000000000'),NEW.seq,NEW.change_id,NEW.table_name,NEW.operation,NEW.primary_key_json,NEW.row_json,NEW.occurred_at),loom_stamp()); END"""
    with store.transaction() as connection:
        connection.execute("DROP TRIGGER ledger_changes_append_integrity")
        if mutation == "altered-hash":
            sibling = sibling.replace("NEW.row_json,NEW.occurred_at", "NEW.primary_key_json,NEW.occurred_at")
        connection.execute(sibling)
        if mutation == "altered-literal":
            connection.execute("DROP TRIGGER events_no_update")
            connection.execute("CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'Lattice  events are append-only'); END")
    connection = sqlite3.connect(config.database)
    try:
        if mutation == "valid-sibling":
            deployment.database_change_token(connection)
        else:
            with pytest.raises(deployment.RollbackSnapshotStale, match="trigger"):
                deployment.database_change_token(connection)
    finally:
        connection.close()


@pytest.mark.oracle
@pytest.mark.parametrize("mutation", ["write-restore", "replace", "truncate", "watch-loss", "directory-rename"])
def test_manifest_kernel_watch_invalidates_without_metadata_identity(tmp_path, mutation):
    import os
    import select
    if not hasattr(select, "kqueue"):
        pytest.skip("native macOS vnode mutation boundary")
    parent = tmp_path / "snapshot"
    parent.mkdir()
    path = parent / "snapshot.json"
    original = b'{"legacy":"baseline"}'
    path.write_bytes(original)
    info = path.stat()
    control = deployment.WatchedControl(path)
    try:
        if mutation == "write-restore":
            path.write_bytes(b"x" * len(original))
            path.write_bytes(original)
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
            assert path.read_bytes() == original and path.stat().st_mtime_ns == info.st_mtime_ns
        elif mutation == "replace":
            replacement = tmp_path / "replacement"
            replacement.write_bytes(original)
            replacement.replace(path)
        elif mutation == "truncate":
            with path.open("r+b") as handle:
                handle.truncate(1)
        elif mutation == "directory-rename":
            parent.rename(tmp_path / "old-snapshot")
            parent.mkdir()
            path.write_bytes(original)
        else:
            control.close()
        with pytest.raises(deployment.RollbackSnapshotStale, match="publication input"):
            control.verify()
    finally:
        control.close()


@pytest.mark.oracle
@pytest.mark.parametrize("mutation", ["write", "replace", "write-restore"])
def test_manifest_mutation_between_qualification_and_exclusion_refuses(baseline, monkeypatch, mutation):
    if mutation != "write" and not hasattr(select, "kqueue"):
        pytest.skip("native macOS mutation-history detection")
    _config, _store, _project, snapshot, target = baseline
    legacy_manifest(baseline)
    calls = mocked_publication(monkeypatch)
    def mutate():
        path = snapshot / "snapshot.json"
        original = path.read_bytes()
        if mutation == "replace":
            replacement = snapshot / "replacement.json"
            replacement.write_bytes(original)
            replacement.replace(path)
        else:
            path.write_bytes(original + b" ")
            if mutation == "write-restore":
                path.write_bytes(original)
    with pytest.raises(deployment.RollbackSnapshotStale, match="publication input"):
        deployment.perform_upgrade(target, rollback_snapshot=snapshot, verify_candidate=lambda *_a: None, before_cutover=mutate)
    assert calls == [] and not deployment.maintenance_record_path().exists()


@pytest.mark.oracle
@pytest.mark.parametrize("mutation", ["unchanged", "write", "replace-changed", "write-restore", "replace-same"])
def test_manifest_hash_fallback_content_contract(tmp_path, monkeypatch, mutation):
    monkeypatch.delattr(select, "kqueue", raising=False)
    path = tmp_path / "snapshot.json"
    original = b'{"baseline":true}'
    path.write_bytes(original)
    control = deployment.WatchedControl(path)
    try:
        assert control.queue is None
        reads = []
        real_hash = deployment.exact_sha256
        def observed_hash(value):
            reads.append(value)
            return real_hash(value)
        monkeypatch.setattr(deployment, "exact_sha256", observed_hash)
        if mutation.startswith("replace"):
            replacement = tmp_path / "replacement"
            replacement.write_bytes(original if mutation == "replace-same" else b"changed")
            replacement.replace(path)
        elif mutation != "unchanged":
            path.write_bytes(b"changed")
            if mutation == "write-restore":
                path.write_bytes(original)
        if mutation in {"write", "replace-changed"}:
            with pytest.raises(deployment.RollbackSnapshotStale, match="publication input"):
                control.verify()
        else:
            control.verify()
        assert reads == [path]  # Fallback rereads content, with no history/byte bound.
    finally:
        control.close()


@pytest.mark.oracle
def test_manifest_watch_accepts_sibling_activity_without_rehash(tmp_path, monkeypatch):
    import select
    if not hasattr(select, "kqueue"):
        pytest.skip("native macOS vnode mutation boundary")
    path = tmp_path / "snapshot.json"
    original = b'{"baseline":true}'
    path.write_bytes(original)
    control = deployment.WatchedControl(path)
    try:
        def no_rehash(_path):
            raise AssertionError("final mutation check must not reread manifest bytes")
        monkeypatch.setattr(deployment, "exact_sha256", no_rehash)
        control.verify()
        sibling = tmp_path / "lattice.sqlite3-wal"
        sibling.write_bytes(b"sidecar")
        control.verify()
        sibling.write_bytes(b"changed sidecar")
        sibling.rename(tmp_path / "renamed-sidecar")
        control.verify()
        assert path.read_bytes() == original
    finally:
        control.close()
