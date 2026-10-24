"""Bounded snapshot costs, retained units, and fresh-generation resume oracles."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest
from lattice import snapshot


@pytest.mark.contract
@pytest.mark.parametrize("result,expected", [(0, True), (1, False), (5, "error")])
def test_native_volume_flush_requests_hardware_and_wait_flags(monkeypatch, tmp_path, result, expected):
    from lattice import durability
    calls = []
    class Operation:
        def __call__(self, path, flags):
            calls.append((path, flags))
            return result
    operation = Operation()
    monkeypatch.setattr(durability, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(durability.ctypes, "CDLL", lambda _library: SimpleNamespace(sync_volume_np=operation))
    if expected == "error":
        with pytest.raises(OSError):
            durability.sync_volume(tmp_path)
    else:
        assert durability.sync_volume(tmp_path) is expected
    assert calls == [(os.fsencode(tmp_path), 3)]


def source_tree(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    with sqlite3.connect(root / "lattice.sqlite3") as connection:
        connection.execute("CREATE TABLE example(value TEXT)")
        connection.execute("INSERT INTO example VALUES('original')")
    (root / "blobs").mkdir()
    for name in ("a", "b", "c"):
        (root / "blobs" / name).write_text(name * 100)
    return root


def capture(root, **kwargs):
    return snapshot.create_snapshot(root, reason="test", source_revision="test-source",
                                    schema_version=None, state_digest=None, **kwargs)


@pytest.mark.oracle
def test_process_death_retains_completed_copy_and_resume_uses_new_generation(tmp_path):
    root = source_tree(tmp_path)
    script = '''
import os, sys
from pathlib import Path
from lattice import snapshot
native = snapshot._CaptureProgress.copy
def stop(self, source, target):
    native(self, source, target)
    self.checkpoint()
    os._exit(73)
snapshot._CaptureProgress.copy = stop
snapshot.create_snapshot(Path(sys.argv[1]), reason="killed", source_revision="old", schema_version=None, state_digest=None)
'''
    result = subprocess.run([sys.executable, "-c", script, str(root)])
    assert result.returncode == 73
    staging = next(p for p in (root / "backups").iterdir() if p.is_dir())
    retained = staging / "blobs" / "a"
    inode = retained.stat().st_ino
    assert retained.read_text() == "a" * 100
    # An unfinished copy and a later legitimate source change must not become
    # false reuse. The fresh database and file set must describe the same run.
    (staging / "blobs" / "b").write_text("partial")
    (root / "blobs" / "b").write_text("new generation")
    with sqlite3.connect(root / "lattice.sqlite3") as connection:
        connection.execute("UPDATE example SET value='new'")
    saved = capture(root)
    assert (saved / "blobs" / "a").stat().st_ino == inode
    assert (saved / "blobs" / "b").read_text() == "new generation"
    assert (saved / "blobs" / "c").read_text() == "c" * 100
    manifest = snapshot.verify_snapshot(saved)
    with snapshot.materialized_snapshot_database(saved, manifest) as database:
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT value FROM example").fetchone() == ("new",)
    progress = json.loads(staging.with_name(staging.name + ".progress.json").read_text())
    assert progress["phase"] == "complete"
    assert progress["reused_files"] == 1


@pytest.mark.contract
def test_copy_checkpoints_flush_files_before_one_drive_barrier(tmp_path, monkeypatch):
    root = source_tree(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    events = []
    real_fsync = os.fsync
    monkeypatch.setattr(snapshot, "_sync_volume", lambda _path: False)
    monkeypatch.setattr(snapshot.os, "fsync", lambda fd: (events.append("file"), real_fsync(fd))[-1])
    monkeypatch.setattr(snapshot, "_sync_file", lambda fd: events.append("barrier"))
    progress = snapshot._CaptureProgress(staging, root)
    events.clear()
    for source in sorted((root / "blobs").iterdir()):
        progress.copy(source, staging / source.name)
    assert events == ["file"] * 3
    progress.checkpoint()
    assert events[-1] == "file"  # atomic status rename's parent-directory sync
    assert events.count("barrier") == 1
    assert events.index("barrier") >= 3


@pytest.mark.contract
def test_volume_checkpoint_waits_before_publishing_progress(tmp_path, monkeypatch):
    root = source_tree(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    events = []
    monkeypatch.setattr(snapshot, "_sync_volume", lambda _path: events.append("volume") or True)
    monkeypatch.setattr(snapshot, "_sync_file", lambda _fd: events.append("status"))
    progress = snapshot._CaptureProgress(staging, root)
    events.clear()
    for source in sorted((root / "blobs").iterdir()):
        progress.copy(source, staging / source.name)
    assert events == []
    progress.checkpoint()
    assert events == ["volume", "status"]
    monkeypatch.setattr(snapshot, "_sync_volume", lambda _path: False)
    with pytest.raises(OSError, match="became unavailable"):
        progress.checkpoint()


@pytest.mark.regression
def test_interruption_is_retained_and_corruption_repaired(tmp_path, monkeypatch):
    root = source_tree(tmp_path)
    native = snapshot._copy_regular
    def broken(source, target, **kwargs):
        native(source, target, **kwargs)
        if source.name == "b":
            target.write_bytes(b"torn copy")
            raise OSError("planted disk interruption")
    monkeypatch.setattr(snapshot, "_copy_regular", broken)
    with pytest.raises(OSError, match="planted"):
        capture(root)
    staging = next(p for p in (root / "backups").iterdir() if p.is_dir())
    inode = (staging / "blobs" / "a").stat().st_ino
    monkeypatch.setattr(snapshot, "_copy_regular", native)
    saved = capture(root)
    assert (saved / "blobs" / "a").stat().st_ino == inode
    assert (saved / "blobs" / "b").read_bytes() == (root / "blobs" / "b").read_bytes()
    snapshot.verify_snapshot(saved)


@pytest.mark.regression
def test_resume_refuses_foreign_and_redirected_staging(tmp_path):
    root = source_tree(tmp_path)
    foreign = tmp_path / ".lattice-snapshot-foreign"
    foreign.mkdir()
    with pytest.raises(OSError, match="inside the backup root"):
        capture(root, resume=foreign)
    redirected = root / "backups" / ".lattice-snapshot-link"
    redirected.symlink_to(foreign)
    with pytest.raises(OSError, match="symlink"):
        capture(root, resume=redirected)


@pytest.mark.concurrency
def test_maintenance_refuses_busy_journal_without_waiting_or_stopping_it(tmp_path):
    import threading
    from lattice.run_journal import acquire_journal_writer
    root = source_tree(tmp_path)
    descriptor = acquire_journal_writer(root)
    ended = threading.Event()
    errors = []
    def attempt():
        try:
            capture(root)
        except RuntimeError as error:
            errors.append(str(error))
        finally:
            ended.set()
    worker = threading.Thread(target=attempt)
    worker.start()
    try:
        assert ended.wait(2), "maintenance waited indefinitely behind a journal writer"
        assert len(errors) == 1 and "writer is active" in errors[0]
    finally:
        os.close(descriptor)
        worker.join(timeout=5)


@pytest.mark.regression
def test_sealed_but_unpublished_snapshot_is_not_the_latest_backup(tmp_path):
    from lattice.diagnostics import latest_backup
    staged = tmp_path / ".lattice-snapshot-interrupted"
    staged.mkdir()
    (staged / "snapshot.json").write_text("{}")
    assert latest_backup(tmp_path) is None


@pytest.mark.regression
def test_torn_unpublished_manifest_does_not_trap_completed_copies(tmp_path, monkeypatch):
    root = source_tree(tmp_path)
    verify = snapshot.verify_snapshot
    def interrupt(staging):
        (staging / "snapshot.json").write_bytes(b'{"torn":')
        raise OSError("interrupted manifest publication")
    monkeypatch.setattr(snapshot, "verify_snapshot", interrupt)
    with pytest.raises(OSError, match="interrupted"):
        capture(root)
    staging = next(p for p in (root / "backups").iterdir() if p.is_dir())
    inode = (staging / "blobs" / "a").stat().st_ino
    monkeypatch.setattr(snapshot, "verify_snapshot", verify)
    saved = capture(root)
    assert (saved / "blobs" / "a").stat().st_ino == inode
    verify(saved)


@pytest.mark.oracle
def test_rollback_reuse_accepts_unchanged_state_and_rejects_new_rows(lattice_env, tmp_path, monkeypatch):
    from ops import install
    from lattice.contracts import ProjectCreate
    from lattice.diagnostics import assess_data_root
    root = lattice_env["config"].data_root
    state = assess_data_root(root)
    saved = snapshot.create_snapshot(root, reason="rollback", source_revision=state["state_digest"],
                                     state_digest=state["state_digest"], schema_version=state["schema_version"])
    monkeypatch.setattr(install, "DEFAULT_DATA_ROOT", root)
    assert install.reuse_rollback_snapshot(saved) == saved
    lattice_env["store"].create_project(ProjectCreate(title="later", purpose="stale snapshot falsifier"))
    with pytest.raises(RuntimeError, match="stale"):
        install.reuse_rollback_snapshot(saved)
