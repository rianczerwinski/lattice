"""Negative controls for installer maintenance ownership, ordering, cancellation and recovery.

Every scenario drives the real `install.main` / `--recover` entry paths in a
separate OS process with fake service functions and a temporary data root. No
launchctl, no live data root. Proof limits are listed at the bottom.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from ops import install as deployment

OPS = Path(deployment.__file__).resolve().parent
SERVICE = deployment.SERVICE_LABEL
BACKUP = deployment.BACKUP_LABEL
CHILDREN = []


@pytest.fixture(autouse=True)
def reap_test_children():
    yield
    for _root, process in CHILDREN:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    CHILDREN.clear()

HARNESS = textwrap.dedent('''
    import json, os, signal, sys, time
    from contextlib import closing, contextmanager
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    import install
    root = Path(sys.argv[2]); plan = json.loads(sys.argv[3]); argv = sys.argv[4:]
    log = root / "calls.jsonl"
    def note(*event):
        with log.open("a") as handle:
            handle.write(json.dumps(list(event)) + "\\n")
    def pause(phase):
        if plan.get("sleep_in") == phase:
            note("sleeping", phase)
            time.sleep(plan.get("sleep_seconds", 60))
    install.DEFAULT_DATA_ROOT = root
    install.LAUNCH_AGENTS = root / "agents"
    install.CURRENT_LINK = root / "current"
    install.PREVIOUS_LINK = root / "previous"
    install.SHIM = root / "shim"
    state = root / "launchd.json"
    def loaded():
        return json.loads(state.read_text()) if state.exists() else {}
    def set_loaded(label, value):
        current = loaded(); current[label] = value; state.write_text(json.dumps(current))
    install._job_loaded = lambda label: bool(loaded().get(label))
    def unload(label):
        pause("unload"); note("unload", label); set_loaded(label, False)
    def reload(label, path):
        note("reload", label); set_loaded(label, True)
        if label == install.SERVICE_LABEL:
            pause("reload-service")
    install.unload_job = unload
    install.reload_job = reload
    install.import_source_modules = lambda: None
    install.verify_source = lambda: "rev"
    install.git_revision = lambda _root, **_k: "rev"
    install.migrate_legacy_data = lambda _root: None
    target = root / "versions" / "candidate"; target.mkdir(parents=True, exist_ok=True)
    (target / "runtime.json").write_text("synthetic-runtime")
    install.install_runtime = lambda _revision: target
    install.installed_runtime_version = lambda _target: "0.0.0"
    def backup():
        note("backup"); pause("backup")
        snapshot = root / "backups" / "snap"; snapshot.mkdir(parents=True, exist_ok=True)
        (snapshot / "snapshot.json").write_text(json.dumps({"state_digest": "d0", "schema_version": 1}))
        return snapshot
    install.backup_before_upgrade = backup
    install.verify_candidate_backup = lambda *_a: note("verify-backup")
    install.qualify_candidate_migration = lambda *_a: note("qualify")
    # Empty fake candidate has no .venv; bundled-pack validation is proven
    # by tests/test_workflow_readiness_boundary.py.
    install.verify_candidate_workflow_packs = lambda *_a: None
    @contextmanager
    def quiescence():
        note("quiescence-enter"); yield; note("quiescence-exit")
    install.upgrade_quiescence = quiescence
    install.live_database_generation = lambda: tuple(plan.get("live_generation", ["d0", 1]))
    class SyntheticIdentity:
        def assert_unchanged(self): pass
        def bind_snapshot(self, _snapshot): pass
        def verify(self): pass
        def close(self): pass
    @contextmanager
    def identity(_target, _snapshot):
        yield SyntheticIdentity()
    install.qualified_cutover_identity = identity
    if plan.get("orphan_migration"):
        # Real migrate_database_for_publication (inherited LOCK_SH descriptor); the
        # child is a shell that holds the descriptor before any Python runs.
        real_run = install.run
        def run_orphan(_arguments, **options):
            note("migrate")
            return real_run(["/bin/sh", "-c", "echo $$ > " + str(root / "orphan.pid") + "; exec sleep 60"], pass_fds=options.get("pass_fds", ()))
        install.run = run_orphan
    else:
        def migrate(_target, _snapshot):
            note("migrate"); pause("migrate")
        install.migrate_database_for_publication = migrate
    install.install_links = lambda _target: note("links")
    def admit():
        import sqlite3
        with closing(sqlite3.connect(root / "lattice.sqlite3")) as connection, connection:
            connection.execute("INSERT INTO events(body) VALUES('admitted after candidate start')")
    def launch(_target):
        note("launch"); set_loaded(install.SERVICE_LABEL, True); set_loaded(install.BACKUP_LABEL, True)
        if plan.get("admit_in_launch"):
            admit()
        pause("launch")
    install.install_launchd = launch
    def verify_live(runtime_id, version, timeout_seconds=300):
        first = "verify-live" not in [json.loads(line)[0] for line in log.read_text().splitlines()] if log.exists() else True
        note("verify-live", runtime_id)
        if plan.get("health") == "fail" and runtime_id == "candidate":
            raise RuntimeError("planted unhealthy candidate")
        if plan.get("health") == "fail-first" and first:
            raise RuntimeError("planted first health failure")
    install.verify_live_service = verify_live
    install.MAINTENANCE_WRITER_WAIT_SECONDS = plan.get("writer_wait", 600)
    real_exclusion = install.candidate_writer_exclusion
    @contextmanager
    def exclusion():
        with real_exclusion():
            note("writers-excluded"); yield
    install.candidate_writer_exclusion = exclusion
    def restore_database(snapshot):
        note("restore-database")
        if plan.get("slow_restore"):
            time.sleep(plan["slow_restore"])
        if plan.get("stale_rollback"):
            raise RuntimeError("rollback database did not reproduce pre-upgrade state")
    install.restore_upgrade_database = restore_database
    if plan.get("signal_before_publish"):
        def signal_before_publish(*args, **kwargs):
            os.kill(os.getpid(), signal.SIGTERM)
        install.publish_runtime = signal_before_publish
    sys.argv = ["install.py"] + argv
    install.main()
    note("done")
''')


def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "state"
    (root / "versions" / "old").mkdir(parents=True)
    (root / "current").symlink_to(root / "versions" / "old")
    agents = root / "agents"
    agents.mkdir()
    (agents / f"{SERVICE}.plist").write_bytes(b"service-plist")
    (agents / f"{BACKUP}.plist").write_bytes(b"backup-plist")
    (root / "launchd.json").write_text(json.dumps({SERVICE: True, BACKUP: True}))
    harness = tmp_path / "harness.py"
    harness.write_text(HARNESS)
    return root


def launch(root: Path, plan: dict | None = None, *argv: str) -> subprocess.Popen:
    process = subprocess.Popen([sys.executable, str(root.parent / "harness.py"), str(OPS), str(root), json.dumps(plan or {}), *argv],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    CHILDREN.append((root, process))
    return process


def calls(root: Path) -> list[list]:
    path = root / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def names(root: Path) -> list[str]:
    return [event[0] for event in calls(root)]


def wait_for(root: Path, event: list, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if event in calls(root):
            return
        process = next((p for directory, p in reversed(CHILDREN) if directory == root), None)
        if process is not None and process.poll() is not None:
            raise AssertionError(f"installer exited before {event}; calls={calls(root)}")
        time.sleep(0.05)
    raise AssertionError(f"{event} not observed; calls={calls(root)}")


def finish(process: subprocess.Popen, timeout: float = 60) -> tuple[int, str]:
    _out, err = process.communicate(timeout=timeout)
    return process.returncode, err


def loaded(root: Path) -> dict:
    return json.loads((root / "launchd.json").read_text())


def record(root: Path) -> dict | None:
    path = root / deployment.MAINTENANCE_RECORD
    return json.loads(path.read_text()) if path.exists() else None


def reset_log(root: Path) -> None:
    (root / "calls.jsonl").unlink(missing_ok=True)


@pytest.mark.oracle
def test_service_is_stopped_only_after_live_snapshot_and_candidate_verification(tmp_path):
    root = data_root(tmp_path)
    code, err = finish(launch(root))
    assert code == 0, err
    order = names(root)
    assert order.index("unload") > max(order.index("backup"), order.index("verify-backup"), order.index("qualify"))
    assert order[order.index("quiescence-enter"):order.index("quiescence-exit") + 1] == ["quiescence-enter", "unload", "unload", "quiescence-exit"]
    assert order[-1] == "done" and record(root) is None
    assert loaded(root) == {SERVICE: True, BACKUP: True}


@pytest.mark.oracle
def test_cutover_drift_refuses_with_service_still_running(tmp_path):
    # Negative control for the old ordering: a stale snapshot must never cost an outage.
    root = data_root(tmp_path)
    code, err = finish(launch(root, {"live_generation": ["d1", 1]}))
    assert code != 0 and "stale at cutover" in err
    assert "unload" not in names(root) and "reload" not in names(root) and "restore-database" not in names(root)
    assert loaded(root) == {SERVICE: True, BACKUP: True} and record(root) is None


@pytest.mark.concurrency
def test_second_installer_is_refused_while_first_owns_maintenance(tmp_path):
    root = data_root(tmp_path)
    first = launch(root, {"sleep_in": "backup"})
    try:
        wait_for(root, ["sleeping", "backup"])
        code, err = finish(launch(root))
        assert code != 0 and "maintenance already owned" in err
        assert names(root).count("backup") == 1
    finally:
        first.send_signal(signal.SIGTERM)
        code, err = finish(first)
    # Cancelled before the stop: nothing to restore, no record, service untouched.
    assert code != 0 and "interrupted by signal SIGTERM" in err
    assert "unload" not in names(root) and record(root) is None and loaded(root)[SERVICE] is True


@pytest.mark.concurrency
def test_lock_is_flock_authority_not_pid_liveness(tmp_path):
    root = data_root(tmp_path)
    # A dead pid written in the lock file must not block; a live flock must.
    (root / deployment.MAINTENANCE_LOCK).write_text("pid=999999999 stale\n")
    code, err = finish(launch(root))
    assert code == 0, err
    descriptor = os.open(root / deployment.MAINTENANCE_LOCK, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        reset_log(root)
        code, err = finish(launch(root))
    finally:
        os.close(descriptor)
    assert code != 0 and "maintenance already owned" in err and calls(root) == []


@pytest.mark.concurrency
def test_sigterm_during_publication_rolls_back_and_ignores_repeated_signals(tmp_path):
    root = data_root(tmp_path)
    process = launch(root, {"sleep_in": "migrate", "slow_restore": 2})
    wait_for(root, ["sleeping", "migrate"])
    assert record(root)["phase"] == "migrating" and loaded(root)[SERVICE] is False
    process.send_signal(signal.SIGTERM)
    wait_for(root, ["restore-database"])
    process.send_signal(signal.SIGTERM)  # arrives inside the rollback section
    code, err = finish(process)
    assert code != 0 and "interrupted by signal SIGTERM" in err and "finishing restoration first" in err
    order = names(root)
    assert order[order.index("restore-database"):] == ["restore-database", "reload", "reload", "verify-live"]
    assert loaded(root) == {SERVICE: True, BACKUP: True} and record(root) is None


@pytest.mark.concurrency
def test_sigkill_leaves_durable_record_and_fresh_recover_restores(tmp_path):
    root = data_root(tmp_path)
    process = launch(root, {"sleep_in": "migrate"})
    wait_for(root, ["sleeping", "migrate"])
    process.kill()
    process.wait()
    saved = record(root)
    assert saved is not None
    assert saved["phase"] == "migrating" and saved["prior"]["service_loaded"] is True and saved["snapshot"].endswith("snap")
    assert loaded(root)[SERVICE] is False
    reset_log(root)
    code, err = finish(launch(root, {}, "--recover"))
    assert code == 0, err
    order = names(root)
    assert order.index("writers-excluded") < order.index("restore-database") < order.index("reload")
    assert loaded(root) == {SERVICE: True, BACKUP: True} and record(root) is None
    # Negative control: without the durable record a fresh operator process has nothing to restore.
    reset_log(root)
    (root / "launchd.json").write_text(json.dumps({SERVICE: False, BACKUP: False}))
    code, _err = finish(launch(root, {}, "--recover"))
    assert code == 0 and "reload" not in names(root) and loaded(root)[SERVICE] is False


@pytest.mark.oracle
@pytest.mark.concurrency
def test_retry_consumes_record_before_capturing_prior_and_stays_cancellable(tmp_path):
    root = data_root(tmp_path)
    process = launch(root, {"sleep_in": "migrate"})
    wait_for(root, ["sleeping", "migrate"])
    process.kill()
    process.wait()
    reset_log(root)
    retry = launch(root, {"sleep_in": "migrate", "sleep_seconds": 2})
    wait_for(root, ["sleeping", "migrate"])
    order = names(root)
    assert order.index("reload") < order.index("backup"), "recovery must precede the new attempt's prior capture"
    # Negative control for swallowed cancellation after automatic recovery.
    retry.send_signal(signal.SIGTERM)
    code, err = finish(retry)
    assert code != 0 and "interrupted by signal SIGTERM" in err
    assert names(root)[-1] == "verify-live" and loaded(root) == {SERVICE: True, BACKUP: True} and record(root) is None


def events_table(root: Path) -> None:
    with closing(sqlite3.connect(root / "lattice.sqlite3")) as connection, connection:
        connection.execute("CREATE TABLE events(seq INTEGER PRIMARY KEY, body TEXT)")


@pytest.mark.oracle
def test_recovery_preserves_candidate_that_admitted_work_after_start(tmp_path):
    root = data_root(tmp_path)
    events_table(root)
    process = launch(root, {"sleep_in": "launch"})
    wait_for(root, ["sleeping", "launch"])
    process.kill()
    process.wait()
    assert record(root)["phase"] == "launching"
    with closing(sqlite3.connect(root / "lattice.sqlite3")) as connection, connection:
        connection.execute("INSERT INTO events(body) VALUES('admitted after candidate start')")
    reset_log(root)
    code, err = finish(launch(root, {"health": "fail"}, "--recover"))
    assert code != 0 and "admitted work after it started" in err
    assert "unload" not in names(root) and "restore-database" not in names(root)
    assert loaded(root) == {SERVICE: True, BACKUP: True}, "candidate must keep running"
    assert record(root)["phase"] == "launching" and "refusal" in record(root)


@pytest.mark.oracle
def test_in_process_rollback_applies_the_same_admission_guard(tmp_path):
    root = data_root(tmp_path)
    events_table(root)
    code, err = finish(launch(root, {"health": "fail", "admit_in_launch": True}))
    assert code != 0 and "admitted work after it started" in err
    assert "unload" not in names(root)[names(root).index("launch"):] and "restore-database" not in names(root)
    assert loaded(root) == {SERVICE: True, BACKUP: True} and "refusal" in record(root)
    # Control: without admitted work the same failure restores the prior runtime.
    root = data_root(tmp_path / "control")
    events_table(root)
    code, err = finish(launch(root, {"health": "fail"}))
    assert code != 0 and "restore-database" in names(root) and record(root) is None and loaded(root)[SERVICE] is True


@pytest.mark.concurrency
def test_orphaned_migration_child_blocks_restore_until_it_exits(tmp_path):
    root = data_root(tmp_path)
    process = launch(root, {"orphan_migration": True, "writer_wait": 2})
    deadline = time.monotonic() + 30
    while not (root / "orphan.pid").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    orphan = int((root / "orphan.pid").read_text())
    process.kill()
    process.wait()
    assert record(root)["phase"] == "migrating"
    try:
        reset_log(root)
        code, err = finish(launch(root, {"writer_wait": 0.3}, "--recover"))
        assert code != 0 and "still holds" in err
        assert "restore-database" not in names(root) and "reload" not in names(root)
    finally:
        os.kill(orphan, signal.SIGKILL)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(orphan, 0)
            time.sleep(0.05)
        except ProcessLookupError:
            break
    reset_log(root)
    code, err = finish(launch(root, {}, "--recover"))
    assert code == 0, err
    assert names(root).index("writers-excluded") < names(root).index("restore-database") < names(root).index("reload")


@pytest.mark.oracle
def test_stopped_phase_record_is_not_retired_on_loaded_flags_alone(tmp_path):
    root = data_root(tmp_path)
    process = launch(root, {"sleep_in": "unload"})
    wait_for(root, ["sleeping", "unload"])
    process.kill()
    process.wait()
    assert record(root)["phase"] == "stopped" and loaded(root)[SERVICE] is True
    reset_log(root)
    code, err = finish(launch(root, {"health": "fail-first"}, "--recover"))
    assert code == 0, err
    assert names(root).count("verify-live") == 2 and "reload" in names(root) and record(root) is None
    # Healthy prior: retired after a health answer, without a restart.
    root = data_root(tmp_path / "healthy")
    process = launch(root, {"sleep_in": "unload"})
    wait_for(root, ["sleeping", "unload"])
    process.kill()
    process.wait()
    reset_log(root)
    code, err = finish(launch(root, {}, "--recover"))
    assert code == 0, err
    assert names(root) == ["verify-live", "done"] and record(root) is None


@pytest.mark.oracle
def test_healthy_interrupted_publication_is_kept(tmp_path):
    root = data_root(tmp_path)
    process = launch(root, {"sleep_in": "launch"})
    wait_for(root, ["sleeping", "launch"])
    process.kill()
    process.wait()
    reset_log(root)
    code, err = finish(launch(root, {}, "--recover"))
    assert code == 0, err
    assert names(root) == ["verify-live", "done"] and record(root) is None


@pytest.mark.oracle
def test_stale_rollback_keeps_record_and_does_not_revive_prior_runtime(tmp_path):
    root = data_root(tmp_path)
    process = launch(root, {"sleep_in": "migrate"})
    wait_for(root, ["sleeping", "migrate"])
    process.kill()
    process.wait()
    reset_log(root)
    code, err = finish(launch(root, {"stale_rollback": True}, "--recover"))
    assert code != 0 and "did not reproduce pre-upgrade state" in err
    assert "reload" not in names(root) and record(root)["phase"] == "restoring"


@pytest.mark.oracle
def test_recover_flag_only_recovers(tmp_path):
    root = data_root(tmp_path)
    code, err = finish(launch(root, {}, "--recover"))
    assert code == 0, err
    assert names(root) == ["done"]


def test_readiness_upgrade_holds_the_same_lock_and_lifecycle(tmp_path, monkeypatch):
    sys.path.insert(0, str(OPS))
    import readiness_upgrade as upgrade

    root = tmp_path / "state"
    root.mkdir()
    old = tmp_path / upgrade.OLD_RUNTIME
    old.mkdir()
    current = tmp_path / "current"
    current.symlink_to(old)
    monkeypatch.setattr(upgrade.install, "DEFAULT_DATA_ROOT", root)
    monkeypatch.setattr(upgrade.install, "CURRENT_LINK", current)
    monkeypatch.setattr(upgrade, "prove", lambda *args: {})
    monkeypatch.setattr(upgrade.install, "capture_publication_state", lambda: object())
    monkeypatch.setattr(upgrade.install, "reuse_rollback_snapshot", lambda path: path)
    monkeypatch.setattr(upgrade.install, "installed_runtime_version", lambda _target: "0.3.1")
    monkeypatch.setattr(upgrade.install, "exact_sha256", lambda _path: "0" * 64)
    restored = []
    monkeypatch.setattr(upgrade.install, "restore_publication_state", restored.append)
    monkeypatch.setattr(upgrade.install, "verify_rollback_snapshot_current", lambda _snapshot: None)

    @contextmanager
    def synthetic_identity(_target, _snapshot):
        class Identity:
            def assert_unchanged(self): pass
            def bind_snapshot(self, _snapshot): pass
            def verify(self): pass
            def close(self): pass
        yield Identity()
    monkeypatch.setattr(upgrade.install, "qualified_cutover_identity", synthetic_identity)

    @contextmanager
    def busy():
        raise RuntimeError("busy")
        yield

    monkeypatch.setattr(upgrade.install, "upgrade_quiescence", busy)
    monkeypatch.setattr(sys, "argv", ["readiness_upgrade", "--rollback-snapshot", str(tmp_path)])
    with pytest.raises(RuntimeError, match="busy"):
        upgrade.main()
    assert restored == [] and (root / deployment.MAINTENANCE_LOCK).exists() and not (root / deployment.MAINTENANCE_RECORD).exists()
    descriptor = os.open(root / deployment.MAINTENANCE_LOCK, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(upgrade.install.MaintenanceOwned):
            upgrade.main()
    finally:
        os.close(descriptor)


# Proof limits:
# - launchd, the real snapshot guard, the migration child, bounded cutover identity and the live digest are
#   faked; the real functions are covered by tests/test_reliability.py.
# - SIGKILL never runs handlers; recovery after it is proven only through the
#   durable record and a fresh process.
# - A signal arriving between the last restore step and clearing the record is
#   recovered by the next installer run, not by this process.


@pytest.mark.oracle
def test_recovery_after_prior_restart_never_restores_database_again(tmp_path):
    root = data_root(tmp_path)
    events_table(root)
    process = launch(root, {"health": "fail", "sleep_in": "reload-service"})
    wait_for(root, ["sleeping", "reload-service"])
    assert record(root) is not None
    with closing(sqlite3.connect(root / "lattice.sqlite3")) as connection, connection:
        connection.execute("INSERT INTO events(body) VALUES('work served by restored runtime')")
    process.kill()
    process.wait()
    reset_log(root)
    code, err = finish(launch(root, {}, "--recover"))
    assert code == 0, err
    assert "restore-database" not in names(root) and "unload" not in names(root)
    with closing(sqlite3.connect(root / "lattice.sqlite3")) as connection, connection:
        assert connection.execute("SELECT body FROM events").fetchone()[0] == 'work served by restored runtime'
    assert record(root) is None


@pytest.mark.concurrency
@pytest.mark.oracle
def test_rollback_checks_real_admission_guard_before_stopping_writers(tmp_path, monkeypatch):
    root = data_root(tmp_path)
    database = root / 'lattice.sqlite3'
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute('CREATE TABLE attempts(state TEXT)')
        connection.execute("INSERT INTO attempts VALUES('running')")
    monkeypatch.setattr(deployment, 'DEFAULT_DATA_ROOT', root)
    calls = []
    def stop(label):
        with closing(sqlite3.connect(database, timeout=0)) as observer, observer:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                observer.execute('BEGIN IMMEDIATE')
        calls.append(label)
    monkeypatch.setattr(deployment, 'unload_job', stop)
    monkeypatch.setattr(deployment, 'restore_upgrade_database', lambda _p: calls.append('restore'))
    monkeypatch.setattr(deployment, 'restore_publication_state', lambda _p: None)
    monkeypatch.setattr(deployment, 'installed_runtime_version', lambda _p: '0.0.0')
    prior = deployment.PublicationState(None, None, None, None, None, False, False)
    arguments = dict(prior=prior, snapshot=root / 'backup', target=root / 'candidate', started=True,
                     baseline_heads=deployment.append_stream_heads(database), record=True, exclude_writers=True)
    with pytest.raises(RuntimeError, match='durable work is active'):
        deployment.protected_rollback(**arguments)
    assert calls == []
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE attempts SET state='completed'")
    deployment.protected_rollback(**arguments)
    assert calls == [SERVICE, BACKUP, 'restore']


@pytest.mark.concurrency
def test_signal_between_stop_and_publication_restores_service(tmp_path):
    root = data_root(tmp_path)
    code, err = finish(launch(root, {"signal_before_publish": True}))
    assert code != 0 and "SIGTERM" in err
    assert loaded(root) == {SERVICE: True, BACKUP: True}
    assert "reload" in names(root) and "restore-database" not in names(root)
    assert record(root) is None
