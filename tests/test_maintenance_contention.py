"""Maintenance waits through transient startup contention while preserving active work."""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

from lattice.contracts import DraftCreate
from lattice.run_journal import journal_startup_guard
from ops import install as deployment


class StartupHolder:
    """Hold the shared startup lock from another thread, as a booting service does."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.held = threading.Event()
        self.release = threading.Event()
        self.released_at: float | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        with journal_startup_guard(self.root):
            self.held.set()
            self.release.wait()
            self.released_at = time.monotonic()

    def release_after(self, seconds: float) -> None:
        threading.Timer(seconds, self.release.set).start()

    def __enter__(self) -> "StartupHolder":
        self.thread.start()
        assert self.held.wait(5), "holder thread did not take the startup lock"
        return self

    def __exit__(self, *_exc) -> None:
        self.release.set()
        self.thread.join(5)


def fast_quiescence(monkeypatch, root: Path, *, wait: float, report: float = 100.0) -> None:
    monkeypatch.setattr(deployment, "DEFAULT_DATA_ROOT", root)
    monkeypatch.setattr(deployment, "QUIESCENCE_WAIT_SECONDS", wait)
    monkeypatch.setattr(deployment, "QUIESCENCE_POLL_SECONDS", 0.05)
    monkeypatch.setattr(deployment, "QUIESCENCE_REPORT_SECONDS", report)


@pytest.mark.concurrency
@pytest.mark.regression
def test_quiescence_waits_for_startup_lock_release_instead_of_failing(lattice_env, monkeypatch, capsys):
    root = lattice_env["config"].data_root
    fast_quiescence(monkeypatch, root, wait=10, report=0.2)
    with StartupHolder(root) as holder:
        holder.release_after(1.0)
        started = time.monotonic()
        with deployment.upgrade_quiescence():
            entered = time.monotonic()
            assert holder.released_at is not None and entered >= holder.released_at
            assert entered - started >= 0.9
            # Both admission locks are held once entered, exactly as before.
            with closing(sqlite3.connect(root / "lattice.sqlite3", timeout=0)) as connection, connection:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    connection.execute("BEGIN IMMEDIATE")
    assert "Waiting for a service startup, snapshot or journal writer" in capsys.readouterr().out
    with deployment.upgrade_quiescence():
        pass


@pytest.mark.concurrency
@pytest.mark.regression
def test_quiescence_wait_is_bounded_and_keeps_the_original_refusal(lattice_env, monkeypatch):
    root = lattice_env["config"].data_root
    fast_quiescence(monkeypatch, root, wait=0.4)
    with StartupHolder(root):
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="writer is active") as info:
            with deployment.upgrade_quiescence():
                pass
        elapsed = time.monotonic() - started
        assert 0.4 <= elapsed < 5, elapsed
        assert isinstance(info.value.__cause__, RuntimeError) and "waited" in str(info.value)
        # Zero budget reproduces the previous single-attempt behaviour.
        monkeypatch.setattr(deployment, "QUIESCENCE_WAIT_SECONDS", 0)
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="writer is active"):
            with deployment.upgrade_quiescence():
                pass
        assert time.monotonic() - started < 1
    with deployment.upgrade_quiescence():
        pass


@pytest.mark.oracle
@pytest.mark.regression
def test_active_durable_work_still_refuses_without_waiting(lattice_env, monkeypatch):
    store = lattice_env["store"]
    root = lattice_env["config"].data_root
    draft = store.create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], role_session_id=lattice_env["session"]["role_session_id"], prompt="Active work must not be stopped.", route=lattice_env["route"]))
    invocation = lattice_env["lattice"].seal(draft["draft_id"])
    attempt = store.create_attempt(invocation["invocation_id"], "active-work-control", "other-operator", 9_999_999_999)
    store.mark_attempt_running(attempt["attempt_id"])
    fast_quiescence(monkeypatch, root, wait=30)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="durable work is active"):
        with deployment.upgrade_quiescence():
            pass
    assert time.monotonic() - started < 5, "active work is refused at once, never waited on"
    # Contention and active work together: the contention is waited out, then the work refuses.
    with StartupHolder(root) as holder:
        holder.release_after(0.5)
        with pytest.raises(RuntimeError, match="durable work is active"):
            with deployment.upgrade_quiescence():
                pass
        assert holder.released_at is not None
    with closing(sqlite3.connect(root / "lattice.sqlite3")) as connection, connection:
        assert connection.execute("SELECT state FROM attempts WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone()[0] == "running"


@pytest.mark.concurrency
@pytest.mark.oracle
def test_recovery_rollback_waits_out_a_booting_candidate_then_restores(tmp_path, monkeypatch):
    """The real `--recover` path: a crash-looping candidate's startup lock no longer fails recovery."""
    root = tmp_path / "state"
    root.mkdir()
    database = root / "lattice.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE attempts(state TEXT)")
    fast_quiescence(monkeypatch, root, wait=10)
    calls: list[tuple[str, float]] = []
    monkeypatch.setattr(deployment, "unload_job", lambda label: calls.append((label, time.monotonic())))
    monkeypatch.setattr(deployment, "restore_upgrade_database", lambda _snapshot: calls.append(("restore", time.monotonic())))
    monkeypatch.setattr(deployment, "restore_publication_state", lambda _prior: None)
    monkeypatch.setattr(deployment, "installed_runtime_version", lambda _target: "0.0.0")
    prior = deployment.PublicationState(None, None, None, None, None, False, False)
    arguments = dict(prior=prior, snapshot=root / "backup", target=root / "candidate", started=True,
                     baseline_heads=deployment.append_stream_heads(database), record=True, exclude_writers=True)
    with StartupHolder(root) as holder:
        holder.release_after(1.0)
        deployment.protected_rollback(**arguments)
        assert holder.released_at is not None
    assert [name for name, _at in calls] == [deployment.SERVICE_LABEL, deployment.BACKUP_LABEL, "restore"]
    assert calls[0][1] >= holder.released_at, "writers are stopped only after the booting process released the lock"
    assert not (root / deployment.MAINTENANCE_RECORD).exists()



@pytest.mark.regression
@pytest.mark.oracle
def test_real_candidate_boot_does_not_prevent_rollback(tmp_path, monkeypatch):
    from lattice import LatticeConfig, LatticeStore
    from lattice.contracts import ProjectCreate

    config = LatticeConfig(data_root=tmp_path / "state", axisvi_root=None)
    store = LatticeStore(config)
    store.create_project(ProjectCreate(title="Legacy workflow", workflow_pack="quaternary"))
    store.reconcile_workflow_instances({"quaternary": "initial"})
    LatticeStore(config)  # migration child startup
    baseline = deployment.append_stream_heads(config.database)
    LatticeStore(config)  # candidate service startup
    fast_quiescence(monkeypatch, config.data_root, wait=1)
    restored = []
    monkeypatch.setattr(deployment, "unload_job", lambda _label: None)
    monkeypatch.setattr(deployment, "restore_upgrade_database", lambda _snapshot: restored.append(True))
    monkeypatch.setattr(deployment, "restore_publication_state", lambda _prior: None)
    monkeypatch.setattr(deployment, "installed_runtime_version", lambda _target: "0.0.0")
    prior = deployment.PublicationState(None, None, None, None, None, False, False)
    deployment.protected_rollback(prior=prior, snapshot=tmp_path / "backup", target=tmp_path / "candidate", started=True, baseline_heads=baseline, record=True, exclude_writers=True)
    assert restored == [True]
