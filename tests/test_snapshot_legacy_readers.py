"""Published legacy SQLite bytes and membership are an independent reader oracle."""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import uuid

import pytest

from lattice import LatticeConfig, LatticeStore, snapshot
from lattice.contracts import ProjectCreate
from lattice.diagnostics import assess_backup, assess_data_root
from ops import install, verify_execution_consolidation as verifier


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def census(root):
    result = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if path.is_dir():
            result[name] = {"kind": "directory"}
        else:
            raw = path.read_bytes()
            result[name] = {"kind": "file", "sha256": hashlib.sha256(raw).hexdigest(),
                            "byte_count": len(raw), "mode": path.stat().st_mode & 0o7777}
    return result


def seal(root, version, report):
    rows = [{"path": name, **{key: item[key] for key in ("sha256", "byte_count", "mode")}}
            for name, item in census(root).items() if item["kind"] == "file"]
    manifest = {
        "contract_version": f"LatticeBackup/{version}.0", "state_digest": report["state_digest"],
        "schema_version": report["schema_version"], "active_work": report["active_work"],
        "generated_object": {
            "contract_version": 1, "role": "snapshot", "kind": "lattice-backup",
            "schema": {"name": "lattice-backup", "version": version},
            "artifact_id": "artifact:12345678-1234-1234-1234-123456789abc",
            "generated_at": "20261026T040000", "producer": {"name": "lattice", "version": "0.1.0"},
            "subject": "lattice:canonical-runtime-state",
            "sources": [{"ref": "synthetic:legacy-reader", "revision": report["state_digest"], "sha256": None}],
        }, "inventory": rows,
    }
    if version == 1:
        del manifest["generated_object"]["sources"][0]["sha256"]
    (root / "snapshot.json").write_bytes(encoded(manifest))
    return manifest


@pytest.fixture(params=[1, 2])
def legacy(request, tmp_path, monkeypatch):
    config = LatticeConfig(data_root=tmp_path / "live-fixture", axisvi_root=None)
    store = LatticeStore(config)
    store.create_project(ProjectCreate(title="legacy WAL image"))
    report = assess_data_root(config.data_root)
    root = tmp_path / "published"
    root.mkdir()
    with closing(sqlite3.connect(config.database)) as source, closing(sqlite3.connect(root / "lattice.sqlite3")) as target:
        source.backup(target)
    assert (root / "lattice.sqlite3").read_bytes()[18:20] == b"\x02\x02"
    assert not list(root.glob("*-wal")) and not list(root.glob("*-shm"))
    if config.blobs.exists():
        shutil.copytree(config.blobs, root / "blobs")
    manifest = seal(root, request.param, report)
    before = census(root)
    observations = {"before": before}
    def unchanged(label):
        observations[label] = census(root)
        assert observations[label] == before, "reader changed published snapshot bytes or membership"
    monkeypatch.setattr(install, "DEFAULT_DATA_ROOT", config.data_root)
    yield config, root, manifest, report, unchanged
    observations["after"] = census(root)
    destination = os.environ.get("LATTICE_LEGACY_CENSUS_DIR")
    if destination:
        with (Path(destination) / f"{request.node.name}-{uuid.uuid4().hex}.json").open("x") as handle:
            json.dump({"test": request.node.nodeid, "root": str(root), "observations": observations}, handle, sort_keys=True)


@pytest.mark.oracle
def test_repeated_verify_assess_and_independent_reader_preserve_source(legacy):
    _config, root, manifest, report, unchanged = legacy
    for iteration in range(2):
        snapshot.verify_snapshot(root)
        unchanged(f"verify-{iteration}")
        assert assess_backup(root)["state_digest"] == report["state_digest"]
        unchanged(f"assess-{iteration}")
        with verifier._snapshot_database(root, manifest) as database:
            assert verifier._database_state_digest(database) == report["state_digest"]
        unchanged(f"independent-{iteration}")
        if manifest["contract_version"] == "LatticeBackup/2.0":
            verifier._verify_snapshot(root)
            unchanged(f"independent-verification-{iteration}")


@pytest.mark.oracle
def test_actual_upgrade_and_candidate_subprocess_preserve_source(legacy, tmp_path, monkeypatch):
    _config, root, _manifest, _report, unchanged = legacy
    target = tmp_path / "candidate"
    target.mkdir()
    # Preserve the interpreter's qualified environment, not just its executable:
    # a bare executable symlink loses pyvenv.cfg and its installed dependencies.
    (target / ".venv").symlink_to(sys.prefix, target_is_directory=True)
    (target / "runtime.json").write_text('{"runtime_id":"synthetic-candidate"}')
    prior = install.PublicationState(None, None, None, None, None, False, False)
    monkeypatch.setattr(install, "capture_publication_state", lambda: prior)
    monkeypatch.setattr(install, "installed_runtime_version", lambda _target: "0.0.0")
    effects = []
    monkeypatch.setattr(install, "unload_job", effects.append)
    monkeypatch.setattr(install, "publish_runtime", lambda *_a, **_k: effects.append("publish"))
    def candidate(candidate_root, saved):
        assert effects == []
        for iteration in range(2):
            install.verify_candidate_backup(candidate_root, saved)
            unchanged(f"candidate-subprocess-{iteration}")
    install.perform_upgrade(target, rollback_snapshot=root, verify_candidate=candidate, before_cutover=lambda: unchanged("before-cutover"))
    assert effects == [install.SERVICE_LABEL, install.BACKUP_LABEL, "publish"]
    unchanged("after-upgrade")
    snapshot.verify_snapshot(root)
    unchanged("next-verification")


@pytest.mark.oracle
@pytest.mark.parametrize("reader", ["shared", "independent"])
def test_direct_path_reader_mutant_is_rejected(legacy, monkeypatch, reader):
    _config, root, manifest, _report, unchanged = legacy
    @contextmanager
    def direct(path, _manifest):
        yield path / "lattice.sqlite3"
    if reader == "shared":
        monkeypatch.setattr(snapshot, "materialized_snapshot_database", direct)
        snapshot.verify_snapshot(root)
    else:
        monkeypatch.setattr(verifier, "_snapshot_database", direct)
        with verifier._snapshot_database(root, manifest) as database:
            verifier._database_state_digest(database)
    with pytest.raises(AssertionError, match="reader changed"):
        unchanged("mutant-after-read")
    with pytest.raises(OSError, match="undeclared or missing"):
        snapshot.verify_snapshot(root)


@pytest.mark.oracle
@pytest.mark.parametrize("reader", ["shared", "independent"])
def test_declared_wal_is_replayed_only_in_private_materialization(tmp_path, reader):
    source = tmp_path / "unpublished"
    source.mkdir(parents=True)
    database = source / "lattice.sqlite3"
    root = tmp_path / "published"
    root.mkdir()
    with closing(sqlite3.connect(database)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE facts(value TEXT)")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute("INSERT INTO facts VALUES ('committed WAL row')")
        writer.commit()
        for name in ("lattice.sqlite3", "lattice.sqlite3-wal", "lattice.sqlite3-shm"):
            shutil.copyfile(source / name, root / name)
    report = {"state_digest": "fixture", "schema_version": 1, "active_work": {}}
    manifest = seal(root, 2, report)
    before = census(root)
    boundary = snapshot.materialized_snapshot_database if reader == "shared" else verifier._snapshot_database
    for _ in range(2):
        with boundary(root, manifest) as materialized:
            assert materialized.parent != root
            with closing(sqlite3.connect(materialized.as_uri() + "?mode=ro", uri=True)) as connection:
                assert connection.execute("SELECT value FROM facts").fetchall() == [("committed WAL row",)]
        assert not materialized.parent.exists()
        assert census(root) == before
        snapshot.verify_snapshot(root)
        assert census(root) == before


@pytest.mark.oracle
@pytest.mark.parametrize("reader", ["shared", "independent"])
def test_read_failure_cleans_private_materialization(legacy, reader):
    _config, root, manifest, _report, unchanged = legacy
    boundary = snapshot.materialized_snapshot_database if reader == "shared" else verifier._snapshot_database
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        with boundary(root, manifest) as database:
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                connection.execute("SELECT * FROM nonexistent_reader_failure")
    assert not database.parent.exists()
    unchanged("read-failure")


@pytest.mark.oracle
@pytest.mark.parametrize("reader", ["shared", "independent"])
def test_copy_failure_cleans_private_materialization(legacy, monkeypatch, reader):
    _config, root, manifest, _report, unchanged = legacy
    module, name = (snapshot, "_copy_regular") if reader == "shared" else (verifier, "_copy_legacy_snapshot_file")
    original = getattr(module, name)
    destinations = []
    def fail_after_copy(source, destination, **kwargs):
        original(source, destination, **kwargs)
        destinations.append(destination)
        raise OSError("planted materialization read failure")
    monkeypatch.setattr(module, name, fail_after_copy)
    boundary = snapshot.materialized_snapshot_database if reader == "shared" else verifier._snapshot_database
    with pytest.raises(OSError, match="planted materialization"):
        with boundary(root, manifest):
            pytest.fail("failed copy must not expose a readable image")
    assert len(destinations) == 1 and not destinations[0].parent.exists()
    unchanged("copy-failure")


@pytest.mark.oracle
def test_ordinary_copy_fallback_preserves_bytes_and_wal_semantics(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot, "_try_clone_regular", lambda *_a: False)
    monkeypatch.setattr(verifier.sys, "platform", "no-native-clone")
    test_declared_wal_is_replayed_only_in_private_materialization(tmp_path / "shared", "shared")
    test_declared_wal_is_replayed_only_in_private_materialization(tmp_path / "independent", "independent")


@pytest.mark.oracle
@pytest.mark.parametrize("reader", ["shared", "independent"])
def test_corrupt_copy_transport_is_rejected(legacy, monkeypatch, reader):
    _config, root, manifest, _report, unchanged = legacy
    monkeypatch.setattr(snapshot, "_try_clone_regular", lambda *_a: False)
    monkeypatch.setattr(verifier.sys, "platform", "no-native-clone")
    original = shutil.copyfileobj
    destinations = []
    def corrupt(source, destination, **kwargs):
        original(source, destination, **kwargs)
        destinations.append(Path(destination.name))
        destination.seek(0)
        destination.write(b"corrupt SQLite header")
        destination.flush()
    monkeypatch.setattr(shutil, "copyfileobj", corrupt)
    boundary = snapshot.materialized_snapshot_database if reader == "shared" else verifier._snapshot_database
    with pytest.raises((OSError, verifier.ConsolidationVerificationError), match="copy changed bytes"):
        with boundary(root, manifest):
            pytest.fail("corrupt clone/copy must not be admitted")
    assert len(destinations) == 1 and not destinations[0].parent.exists()
    unchanged("corrupt-transport")


@pytest.fixture(params=[1, 2])
def rollback_wal(request, tmp_path, monkeypatch):
    config = LatticeConfig(data_root=tmp_path / "active", axisvi_root=None)
    store = LatticeStore(config)
    store.create_project(ProjectCreate(title="checkpointed base"))
    root = tmp_path / "published-wal"
    root.mkdir()
    main_only = tmp_path / "main-only.sqlite3"
    # Keep one connection open so Store's short-lived writers cannot checkpoint
    # their final committed WAL on last-close before the fixture is captured.
    with closing(sqlite3.connect(config.database)) as keeper:
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        assert keeper.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        accepted = store.create_project(ProjectCreate(title="accepted only in declared WAL"))
        event = keeper.execute("SELECT event_id,payload_json,payload_sha256 FROM events WHERE subject_id=?", (accepted["project_id"],)).fetchone()
        change = keeper.execute("SELECT change_id,row_json FROM ledger_changes WHERE table_name='projects' AND row_json LIKE ?", (f'%{accepted["project_id"]}%',)).fetchone()
        assert event is not None and change is not None
        report = assess_data_root(config.data_root)
        for name in ("lattice.sqlite3", "lattice.sqlite3-wal", "lattice.sqlite3-shm"):
            shutil.copyfile(config.data_root / name, root / name)
        shutil.copyfile(config.database, main_only)
    # Independently establish that the accepted row and its event are absent
    # from checkpointed pages, rather than merely trusting the WAL filename.
    with closing(sqlite3.connect(main_only)) as connection:
        assert connection.execute("SELECT count(*) FROM projects WHERE project_id=?", (accepted["project_id"],)).fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM events WHERE event_id=?", (event[0],)).fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM ledger_changes WHERE change_id=?", (change[0],)).fetchone()[0] == 0
    if config.blobs.exists():
        shutil.copytree(config.blobs, root / "blobs")
    manifest = seal(root, request.param, report)
    before = census(root)
    assert assess_backup(root)["state_digest"] == report["state_digest"]
    assert census(root) == before
    candidate = store.create_project(ProjectCreate(title="failed candidate, retained for diagnosis"))
    archive = config.data_root / "acceptance" / "failed-upgrades" / "previous-attempt"
    archive.mkdir(parents=True)
    (archive / "lattice.sqlite3").write_bytes(config.database.read_bytes())
    monkeypatch.setattr(install, "DEFAULT_DATA_ROOT", config.data_root)
    yield config, root, manifest, accepted, event, change, candidate
    after = census(root)
    destination = os.environ.get("LATTICE_ROLLBACK_EVIDENCE_DIR")
    if destination:
        with (Path(destination) / f"census-{request.node.name}-{uuid.uuid4().hex}.json").open("x") as handle:
            json.dump({"test": request.node.nodeid, "before": before, "after": after,
                       "accepted": accepted, "event": event, "change": change}, handle, sort_keys=True)
    assert after == before, "rollback changed published snapshot bytes or membership"


@pytest.mark.oracle
def test_rollback_restores_declared_wal_before_prior_restart(rollback_wal, tmp_path, monkeypatch):
    config, root, manifest, accepted, event, change, candidate = rollback_wal
    before = census(config.data_root / "acceptance")
    effects = []
    prior = install.PublicationState(None, None, None, None, None, False, False)
    monkeypatch.setattr(install, "unload_job", effects.append)
    def restarted(_prior):
        assert config.database.read_bytes()[18:20] == b"\x01\x01"
        assert not list(config.data_root.glob("lattice.sqlite3-*"))
        with closing(sqlite3.connect(config.database)) as connection:
            assert connection.execute("SELECT title FROM projects WHERE project_id=?", (accepted["project_id"],)).fetchone() == (accepted["title"],)
            assert connection.execute("SELECT event_id,payload_json,payload_sha256 FROM events WHERE event_id=?", (event[0],)).fetchone() == event
            assert connection.execute("SELECT change_id,row_json FROM ledger_changes WHERE change_id=?", (change[0],)).fetchone() == change
            assert connection.execute("SELECT count(*) FROM projects WHERE project_id=?", (candidate["project_id"],)).fetchone()[0] == 0
        assert assess_data_root(config.data_root)["state_digest"] == manifest["state_digest"]
        effects.append("restart")
    monkeypatch.setattr(install, "restore_publication_state", restarted)
    install.protected_rollback(prior=prior, snapshot=root, target=tmp_path / "candidate-runtime", started=True,
                               baseline_heads=install.append_stream_heads(config.database), record=False, exclude_writers=True)
    assert effects == [install.SERVICE_LABEL, install.BACKUP_LABEL, "restart"]
    after = census(config.data_root / "acceptance")
    assert all(after[name] == value for name, value in before.items())
    archives = list((config.data_root / "acceptance" / "failed-upgrades").glob("*/lattice.sqlite3"))
    assert len(archives) == 2
    for database in archives:
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("SELECT title FROM projects WHERE project_id=?", (candidate["project_id"],)).fetchone() == (candidate["title"],)


@pytest.mark.oracle
@pytest.mark.parametrize("fault", ["corrupt", "wrong-logical-state", "wrong-schema", "wrong-migrations"])
def test_rollback_rejects_bad_stage_before_replacing_active(rollback_wal, monkeypatch, fault):
    config, root, _manifest, _accepted, _event, _change, _candidate = rollback_wal
    before = census(config.data_root)
    original_connect = sqlite3.connect
    faults = []
    class FaultyBackup(sqlite3.Connection):
        def backup(self, target, **kwargs):
            super().backup(target, **kwargs)
            faults.append(fault)
            if fault == "corrupt":
                database = Path(target.execute("PRAGMA database_list").fetchone()[2])
                with database.open("r+b") as handle:
                    handle.write(b"invalid SQLite header")
            elif fault == "wrong-logical-state":
                with closing(original_connect(config.database)) as wrong:
                    wrong.backup(target)
            elif fault == "wrong-schema":
                target.execute("CREATE TABLE unwanted_stage_table(value TEXT)")
                target.commit()
            else:
                target.execute("INSERT INTO schema_migrations VALUES(-123,'planted')")
                target.commit()
    def connect(*args, **kwargs):
        kwargs["factory"] = FaultyBackup
        return original_connect(*args, **kwargs)
    monkeypatch.setattr(install.sqlite3, "connect", connect)
    with pytest.raises((RuntimeError, sqlite3.DatabaseError, OSError)):
        install.restore_upgrade_database(root)
    assert faults == [fault], "fault must reach the actual rollback backup boundary"
    assert census(config.data_root) == before, "invalid staged image displaced active or archived candidate bytes"
