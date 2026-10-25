"""Real-process startup/backup exclusion without a live service or provider."""
import fcntl
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys

import pytest

from lattice import LatticeConfig, LatticeStore
from lattice.run_journal import DURABLE_RUNS_LOCK, journal_capture_guard, journal_startup_guard


def line(stream):
    assert select.select([stream], [], [], 10)[0], "child made no observable progress"
    value = stream.readline()
    assert value, "child exited before the expected boundary"
    return value


@pytest.mark.concurrency
@pytest.mark.parametrize("boundary", ["store", "lifespan"])
def test_startup_waits_outside_sqlite_until_capture_releases(tmp_path, boundary):
    root = tmp_path / "data"
    config = LatticeConfig(data_root=root, axisvi_root=None)
    LatticeStore(config)
    script = '''
import asyncio, sys
from pathlib import Path
from lattice import LatticeConfig, LatticeStore
from lattice.server import create_app
from lattice.run_journal import journal_capture_guard
root = Path(sys.argv[1]); boundary = sys.argv[2]
config = LatticeConfig(data_root=root, axisvi_root=None)
if boundary == 'lifespan':
    app = create_app(config)
original = LatticeStore._open_connection
def impatient(self):
    connection = original(self)
    connection.execute('PRAGMA busy_timeout=1')
    return connection
LatticeStore._open_connection = impatient
print('ready', flush=True)
sys.stdin.readline()
if boundary == 'store':
    LatticeStore(config)
else:
    async def boot():
        async with app.router.lifespan_context(app):
            with journal_capture_guard(root, blocking=False):
                pass
            print('serving', flush=True)
    asyncio.run(boot())
with journal_capture_guard(root, blocking=False):
    pass
print('complete', flush=True)
'''
    child = subprocess.Popen([sys.executable, "-c", script, str(root), boundary], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert line(child.stdout).strip() == "ready"
        with journal_capture_guard(root), sqlite3.connect(config.database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            child.stdin.write("start\n")
            child.stdin.flush()
            assert "startup waiting for snapshot/journal capture" in line(child.stderr)
            assert child.poll() is None
            connection.rollback()
        output, error = child.communicate(timeout=15)
        assert child.returncode == 0, error
        assert "complete" in output
        if boundary == "lifespan":
            assert "serving" in output
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
        for stream in (child.stdin, child.stdout, child.stderr):
            stream.close()


@pytest.mark.concurrency
def test_startup_excludes_capture_but_allows_journal_writers_and_releases_on_failure(tmp_path):
    descriptor = os.open(tmp_path / DURABLE_RUNS_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with pytest.raises(ValueError, match="startup failed"):
            with journal_startup_guard(tmp_path):
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                with pytest.raises(BlockingIOError):
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                raise ValueError("startup failed")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)


@pytest.mark.asyncio
async def test_lifespan_wait_is_cancellable_while_capture_stays_owned(tmp_path):
    import asyncio
    from lattice.run_journal import async_journal_startup_guard
    entered = asyncio.Event()
    async def startup():
        async with async_journal_startup_guard(tmp_path):
            entered.set()
    with journal_capture_guard(tmp_path):
        task = asyncio.create_task(startup())
        await asyncio.sleep(0.15)
        assert not entered.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
    assert not entered.is_set()
    async with async_journal_startup_guard(tmp_path):
        with pytest.raises(RuntimeError, match="writer is active"):
            with journal_capture_guard(tmp_path, blocking=False):
                pass
    with journal_capture_guard(tmp_path, blocking=False):
        pass


@pytest.mark.regression
def test_pre_migration_snapshot_precedes_shared_startup_lock(tmp_path):
    root = tmp_path / "legacy"
    config = LatticeConfig(data_root=root, axisvi_root=None)
    LatticeStore(config)
    with sqlite3.connect(config.database) as connection:
        connection.execute("DROP TRIGGER schema_migrations_no_delete")
        connection.execute("DELETE FROM schema_migrations WHERE version=16")
    script = "from pathlib import Path; import sys; from lattice import LatticeConfig,LatticeStore; LatticeStore(LatticeConfig(data_root=Path(sys.argv[1]),axisvi_root=None))"
    result = subprocess.run([sys.executable, "-c", script, str(root)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    from lattice.snapshot import verify_snapshot
    snapshots = [p for p in config.backups.iterdir() if p.is_dir() and not p.name.startswith('.')]
    assert len(snapshots) == 1
    assert verify_snapshot(snapshots[0])["schema_version"] == 15
    with journal_capture_guard(root, blocking=False):
        pass
