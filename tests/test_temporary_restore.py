"""Migration rehearsal copies preserve exact state without publishing recovery data."""

import pytest

from lattice import snapshot as snapshots
from lattice.diagnostics import assess_data_root
from conftest import completed_invocation


@pytest.mark.contract
@pytest.mark.parametrize("clone", [False, True])
def test_copy_sync_policy_preserves_default_durability(tmp_path, monkeypatch, clone):
    source = tmp_path / "source"
    source.write_bytes(b"same bytes in either copy mode")
    calls = []

    def copy_clone(input_path, output_path):
        if clone:
            output_path.write_bytes(input_path.read_bytes())
        return clone

    monkeypatch.setattr(snapshots, "_try_clone_regular", copy_clone)
    monkeypatch.setattr(snapshots, "_sync_file", lambda fd: calls.append(fd))
    snapshots._copy_regular(source, tmp_path / "durable")
    assert len(calls) == 1
    calls.clear()
    snapshots._copy_regular(source, tmp_path / "temporary", durable=False)
    assert calls == []
    assert (tmp_path / "durable").read_bytes() == (tmp_path / "temporary").read_bytes() == source.read_bytes()


def retained_snapshot(lattice_env, tmp_path):
    completed_invocation(lattice_env, "restore canary", "retained answer")
    root = lattice_env["config"].data_root
    state = assess_data_root(root)
    path = snapshots.create_snapshot(
        root, backup_root=tmp_path / "backups", reason="temporary-copy-test",
        source_revision=state["state_digest"], schema_version=state["schema_version"],
        state_digest=state["state_digest"],
    )
    return path, state


@pytest.mark.oracle
def test_temporary_restore_is_exact_isolated_and_scoped(lattice_env, tmp_path, monkeypatch):
    saved, state = retained_snapshot(lattice_env, tmp_path)
    copy = snapshots._copy_regular
    calls = []

    def observe(source, destination, *, durable=True):
        calls.append(durable)
        copy(source, destination, durable=durable)

    monkeypatch.setattr(snapshots, "_copy_regular", observe)
    with snapshots.temporary_restored_snapshot(saved) as root:
        assert assess_data_root(root)["state_digest"] == state["state_digest"]
        assert calls and not any(calls)
        source = next((saved / "blobs").rglob("*.json"), None)
        if source is None:
            source = next(p for p in (saved / "blobs").rglob("*") if p.is_file())
        original = source.read_bytes()
        target = root / source.relative_to(saved)
        assert target.stat().st_ino != source.stat().st_ino
        target.write_bytes(b"changed only in rehearsal")
        assert source.read_bytes() == original
    assert not root.exists()
    snapshots.verify_snapshot(saved)


@pytest.mark.oracle
def test_normal_restore_still_requires_durable_copies(lattice_env, tmp_path, monkeypatch):
    saved, state = retained_snapshot(lattice_env, tmp_path)
    copy = snapshots._copy_regular
    calls = []

    def observe(source, destination, *, durable=True):
        calls.append(durable)
        copy(source, destination, durable=durable)

    monkeypatch.setattr(snapshots, "_copy_regular", observe)
    destination = tmp_path / "permanent-restore"
    snapshots.restore_snapshot(saved, destination)
    assert calls and all(calls)
    assert assess_data_root(destination)["state_digest"] == state["state_digest"]


@pytest.mark.regression
def test_temporary_restore_rejects_corruption_even_without_sync(lattice_env, tmp_path, monkeypatch):
    saved, _state = retained_snapshot(lattice_env, tmp_path)
    copy = snapshots._copy_regular
    planted = []

    def corrupt(source, destination, *, durable=True):
        copy(source, destination, durable=durable)
        if "blobs" in source.parts and not planted:
            destination.write_bytes(b"planted wrong bytes after copy")
            planted.append(destination)

    monkeypatch.setattr(snapshots, "_copy_regular", corrupt)
    with pytest.raises(OSError, match="reproduce the snapshot inventory"):
        with snapshots.temporary_restored_snapshot(saved):
            pytest.fail("corrupt restore must not be yielded")
    assert planted
    snapshots.verify_snapshot(saved)


@pytest.mark.regression
def test_temporary_restore_rejects_corrupt_source(lattice_env, tmp_path):
    saved, _state = retained_snapshot(lattice_env, tmp_path)
    source = next(p for p in (saved / "blobs").rglob("*") if p.is_file())
    source.write_bytes(b"planted corrupt source")
    with pytest.raises(OSError, match="disagrees with its inventory"):
        with snapshots.temporary_restored_snapshot(saved):
            pytest.fail("corrupt source must not be yielded")
