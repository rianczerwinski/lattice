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


@pytest.mark.concurrency
@pytest.mark.oracle
def test_blob_copy_releases_live_writers_but_keeps_captured_generation(lattice_env, monkeypatch):
    import fcntl
    from lattice.contracts import ProjectCreate
    from lattice.diagnostics import assess_data_root, assess_backup
    root = lattice_env["config"].data_root
    before = assess_data_root(root)
    native = snapshot._CaptureProgress.copy
    reached = []
    def copy(self, source, target, **kwargs):
        if kwargs.get("expected_sha") and not reached:
            descriptor = os.open(root / "durable-runs.lock", os.O_RDWR)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                lattice_env["store"].create_project(ProjectCreate(title="during blob copy", purpose="excluded from saved generation"))
                reached.append(True)
            finally:
                os.close(descriptor)
        return native(self, source, target, **kwargs)
    monkeypatch.setattr(snapshot._CaptureProgress, "copy", copy)
    saved = snapshot.create_snapshot(root, reason="concurrent-copy", source_revision=before["state_digest"],
                                     state_digest=before["state_digest"], schema_version=before["schema_version"])
    assert reached == [True]
    assert assess_backup(saved)["state_digest"] == before["state_digest"]
    assert assess_data_root(root)["state_digest"] != before["state_digest"]


@pytest.mark.regression
def test_blob_address_is_an_independent_copy_oracle(tmp_path, monkeypatch):
    import hashlib
    root = source_tree(tmp_path)
    content = b"content before lock release"
    digest = hashlib.sha256(content).hexdigest()
    blob = root / "blobs" / digest[:2] / digest
    blob.parent.mkdir()
    blob.write_bytes(content)
    native = snapshot._CaptureProgress.copy
    def mutate(self, source, target, **kwargs):
        if kwargs.get("expected_sha"):
            # Both source and destination will agree, so copy-equality alone
            # cannot detect violation of the immutable content address.
            source.write_bytes(b"wrong content with the original address")
        return native(self, source, target, **kwargs)
    monkeypatch.setattr(snapshot._CaptureProgress, "copy", mutate)
    with pytest.raises(OSError, match="content-addressed snapshot blob changed"):
        capture(root)


@pytest.mark.oracle
def test_backup_command_validates_captured_state_without_duplicate_live_scans(lattice_env, monkeypatch, capsys):
    from lattice import cli
    from lattice.diagnostics import assess_backup, assess_data_root
    root = lattice_env["config"].data_root
    expected = assess_data_root(root)["state_digest"]
    monkeypatch.setattr(cli.LatticeConfig, "from_environment", lambda: lattice_env["config"])
    monkeypatch.setattr(cli, "assess_data_root", lambda *_: pytest.fail("redundant live scan"))
    monkeypatch.setattr(cli, "assess_backup", lambda *_: pytest.fail("redundant post-publication scan"))
    assert cli.command_backup(SimpleNamespace(to=None, reason="bounded-command", resume=None, json=True)) == 0
    result = json.loads(capsys.readouterr().out)
    # The independent consumer still resolves and verifies the emitted artifact.
    actual = assess_backup(Path(result["snapshot"]))
    assert result["state_digest"] == actual["state_digest"] == expected
    assert result["artifact_id"] == actual["artifact_id"]


# ---- R5: one locked-source hash per mutable file; staged bytes proven after release ----

ORIGINAL_RUN = b"run-journal-generation-one\n"
ORIGINAL_TOKEN = b"operator-token-original\n"


def mutable_tree(tmp_path):
    """Source with every mutable root class plus one content-addressed blob."""
    import hashlib
    root = source_tree(tmp_path)
    (root / "durable-runs").mkdir()
    (root / "durable-runs" / "run.jsonl").write_bytes(ORIGINAL_RUN)
    (root / "model-runs").mkdir()
    (root / "model-runs" / "model.json").write_bytes(b'{"model":"one"}\n')
    (root / "acceptance").mkdir()
    (root / "acceptance" / "accepted.txt").write_bytes(b"accepted\n")
    (root / "operator-token").write_bytes(ORIGINAL_TOKEN)
    content = b"immutable content-addressed blob\n"
    digest = hashlib.sha256(content).hexdigest()
    (root / "blobs" / digest[:2]).mkdir()
    (root / "blobs" / digest[:2] / digest).write_bytes(content)
    return root


def same_size_mutation(original: bytes) -> bytes:
    replacement = original[:-2][::-1] + original[-2:]
    assert replacement != original and len(replacement) == len(original)
    return replacement


def force_copy_fallback(monkeypatch, enabled: bool) -> list:
    consulted = []
    if enabled:
        def fallback(source, _destination):
            consulted.append(source)
            return False  # behave like a non-APFS volume: ordinary copy path
        monkeypatch.setattr(snapshot, "_try_clone_regular", fallback)
    return consulted


@pytest.mark.oracle
@pytest.mark.parametrize("fallback", [False, True], ids=["clone", "copy-fallback"])
def test_live_mutation_after_lock_release_keeps_locked_bytes(tmp_path, monkeypatch, fallback):
    """Writers resume once the guard drops; the snapshot still proves the locked generation."""
    import fcntl
    from lattice.run_journal import DURABLE_RUNS_LOCK
    root = mutable_tree(tmp_path)
    consulted = force_copy_fallback(monkeypatch, fallback)
    native = snapshot._compress_database
    mutated = []
    def mutate_live_then_compress(*args, **kwargs):
        descriptor = os.open(root / DURABLE_RUNS_LOCK, os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)  # proves the guard is gone
            (root / "durable-runs" / "run.jsonl").write_bytes(same_size_mutation(ORIGINAL_RUN))
            (root / "operator-token").write_bytes(same_size_mutation(ORIGINAL_TOKEN))
            mutated.append(True)
        finally:
            os.close(descriptor)
        return native(*args, **kwargs)
    monkeypatch.setattr(snapshot, "_compress_database", mutate_live_then_compress)
    saved = capture(root)
    assert mutated == [True]
    assert (saved / "durable-runs" / "run.jsonl").read_bytes() == ORIGINAL_RUN
    assert (saved / "operator-token").read_bytes() == ORIGINAL_TOKEN
    assert (root / "durable-runs" / "run.jsonl").read_bytes() != ORIGINAL_RUN
    snapshot.verify_snapshot(saved)
    assert bool(consulted) is fallback


@pytest.mark.regression
@pytest.mark.parametrize("fallback", [False, True], ids=["clone", "copy-fallback"])
def test_staged_corruption_after_lock_release_is_rejected(tmp_path, monkeypatch, fallback):
    """A staged unit that drifts from the locked source digest must not publish."""
    root = mutable_tree(tmp_path)
    force_copy_fallback(monkeypatch, fallback)
    native = snapshot._inventory
    tampered = []
    def tamper_then_inventory(staging):
        target = staging / "durable-runs" / "run.jsonl"
        assert target.read_bytes() == ORIGINAL_RUN
        target.write_bytes(same_size_mutation(ORIGINAL_RUN))  # same size: size is not identity
        tampered.append(target)
        return native(staging)
    monkeypatch.setattr(snapshot, "_inventory", tamper_then_inventory)
    with pytest.raises(OSError, match="disagree with the locked source: durable-runs/run.jsonl"):
        capture(root)
    assert len(tampered) == 1
    published = [p for p in (root / "backups").iterdir() if p.is_dir() and not p.name.startswith(".lattice-snapshot-")]
    assert published == []


@pytest.mark.contract
def test_exactly_one_source_hash_per_mutable_file_under_guard(tmp_path, monkeypatch):
    """Fresh capture: each guarded source file is hashed once, staged files never, under the lock."""
    import collections
    import contextlib
    root = mutable_tree(tmp_path)
    held = []
    native_guard = snapshot._snapshot_guard
    @contextlib.contextmanager
    def observed_guard(*args, **kwargs):
        with native_guard(*args, **kwargs) as active:
            held.append(True)
            try:
                yield active
            finally:
                held.pop()
    monkeypatch.setattr(snapshot, "_snapshot_guard", observed_guard)
    under_lock = collections.Counter()
    overall = collections.Counter()
    native_hash = snapshot._sha256_file
    def counted(path):
        key = Path(path).resolve()
        overall[key] += 1
        if held:
            under_lock[key] += 1
        return native_hash(path)
    monkeypatch.setattr(snapshot, "_sha256_file", counted)
    saved = capture(root)
    root = root.resolve()
    guarded_sources = sorted(
        [root / "operator-token"]
        + [p for name in ("acceptance", "model-runs", "durable-runs") for p in (root / name).rglob("*") if p.is_file()]
        + [root / "blobs" / n for n in ("a", "b", "c")]
    )
    for source in guarded_sources:
        assert under_lock[source] == 1, source
    assert sum(under_lock.values()) == len(guarded_sources), under_lock
    assert not any(root / "backups" in path.parents for path in under_lock), "staged files hashed under lock"
    assert under_lock[root / "lattice.sqlite3"] == 0
    blob = next(p for p in (root / "blobs").rglob("*") if p.is_file() and len(p.name) == 64)
    assert overall[blob] == 1  # content-addressed unit: one source hash, outside the lock
    snapshot.verify_snapshot(saved)


@pytest.mark.regression
def test_forced_copy_fallback_retains_units_and_repairs_torn_copy(tmp_path, monkeypatch):
    """Without clonefile, interruption still retains complete units and repairs torn ones on resume."""
    root = mutable_tree(tmp_path)
    consulted = force_copy_fallback(monkeypatch, True)
    native = snapshot._copy_regular
    def broken(source, target, **kwargs):
        native(source, target, **kwargs)
        if source.name == "run.jsonl":
            target.write_bytes(b"torn")
            raise OSError("planted disk interruption")
    monkeypatch.setattr(snapshot, "_copy_regular", broken)
    with pytest.raises(OSError, match="planted"):
        capture(root)
    assert consulted, "fallback path was not exercised"
    staging = next(p for p in (root / "backups").iterdir() if p.is_dir())
    retained = staging / "acceptance" / "accepted.txt"
    inode = retained.stat().st_ino
    monkeypatch.setattr(snapshot, "_copy_regular", native)
    saved = capture(root)
    assert (saved / "acceptance" / "accepted.txt").stat().st_ino == inode
    assert (saved / "durable-runs" / "run.jsonl").read_bytes() == ORIGINAL_RUN
    snapshot.verify_snapshot(saved)
    progress = json.loads(staging.with_name(staging.name + ".progress.json").read_text())
    assert progress["phase"] == "complete"
    assert progress["reused_files"] >= 1


@pytest.mark.contract
def test_copy_regular_default_still_proves_bytes_for_restore_callers(tmp_path, monkeypatch):
    """Callers outside capture keep the full two-sided hash proof; only capture defers it."""
    source = tmp_path / "source.bin"
    source.write_bytes(b"restore me exactly")
    def lying_clone(_source, destination):
        destination.write_bytes(b"restore me exactlY")  # equal length, different bytes
        return True
    monkeypatch.setattr(snapshot, "_try_clone_regular", lying_clone)
    with pytest.raises(OSError, match="snapshot copy changed bytes"):
        snapshot._copy_regular(source, tmp_path / "restored.bin")
    # Capture's deferred mode places bytes without proof here; prove_staged owns that proof.
    staging = tmp_path / "staging"
    snapshot._copy_regular(source, staging / "staged.bin", durable=False, prove_bytes=False)
    progress = snapshot._CaptureProgress.__new__(snapshot._CaptureProgress)
    progress.source_hashes = {"staged.bin": snapshot._sha256_file(source)}
    with pytest.raises(OSError, match="disagree with the locked source: staged.bin"):
        progress.prove_staged(snapshot._inventory(staging))
    progress.source_hashes = {"staged.bin": snapshot._sha256_file(staging / "staged.bin")}
    progress.prove_staged(snapshot._inventory(staging))  # agreement passes


@pytest.mark.oracle
def test_corrupt_copy_is_compared_to_source_not_to_itself(tmp_path, monkeypatch):
    root = mutable_tree(tmp_path)
    native = snapshot._copy_regular
    def corrupt(source, target, **kwargs):
        native(source, target, **kwargs)
        if source.name == "run.jsonl":
            target.write_bytes(same_size_mutation(ORIGINAL_RUN))
    monkeypatch.setattr(snapshot, "_copy_regular", corrupt)
    with pytest.raises(OSError, match="disagree with the locked source: durable-runs/run.jsonl"):
        capture(root)


@pytest.mark.oracle
def test_missing_staged_file_is_rejected_before_publication(tmp_path, monkeypatch):
    root = mutable_tree(tmp_path)
    native = snapshot._inventory
    def missing(staging):
        (staging / "durable-runs" / "run.jsonl").unlink()
        return native(staging)
    monkeypatch.setattr(snapshot, "_inventory", missing)
    with pytest.raises(OSError, match="disagree with the locked source: missing file"):
        capture(root)
