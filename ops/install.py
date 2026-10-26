#!/usr/bin/env python3
"""Install a verified, rollback-capable Lattice runtime and launchd service."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import plistlib
import re
import select
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass
from contextlib import ExitStack, closing, contextmanager, nullcontext
from pathlib import Path
from typing import Callable

SOURCE_ROOT = Path(__file__).resolve().parents[1]
AXISVI_ROOT = Path(os.environ.get("AXISVI_ROOT", str(SOURCE_ROOT.parents[2]))).expanduser().resolve()
DEFAULT_DATA_ROOT = Path.home() / "Library" / "Application Support" / "Lattice"
DEFAULT_LEGACY_ROOT = AXISVI_ROOT / "axis" / "psyche" / "recollection" / "lattice" / "data"
RUNTIME_ROOT = DEFAULT_DATA_ROOT / "runtime"
VERSIONS_ROOT = RUNTIME_ROOT / "versions"
CURRENT_LINK = RUNTIME_ROOT / "current"
PREVIOUS_LINK = RUNTIME_ROOT / "previous"
SHIM = Path.home() / ".local" / "bin" / "lattice"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
LOG_ROOT = Path.home() / "Library" / "Logs" / "Lattice"
SERVICE_LABEL = "com.axisvi.lattice"
BACKUP_LABEL = "com.axisvi.lattice.backup"
LAUNCHD_TRANSITION_TIMEOUT_SECONDS = 30
# Maintenance ownership and durable recovery state live in the data root. The
# flock on MAINTENANCE_LOCK is the only authority; pids inside are informational.
MAINTENANCE_LOCK = "maintenance.lock"
MAINTENANCE_WRITER_LOCK = "maintenance-writer.lock"
MAINTENANCE_RECORD = "maintenance.json"
MAINTENANCE_RECORD_CONTRACT = "LatticeMaintenanceRecord/1.0"
MAINTENANCE_WRITER_WAIT_SECONDS = 600
# Admission (`upgrade_quiescence`) tolerates lock contention from a booting
# service for a bounded time; active durable work is refused immediately.
QUIESCENCE_WAIT_SECONDS = 60
QUIESCENCE_POLL_SECONDS = 0.25
QUIESCENCE_REPORT_SECONDS = 10
GRACEFUL_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
VERSION_PATTERN = re.compile(rb'^__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"$', re.MULTILINE)
RELEASE_INPUTS = (
    "lattice", "workflow_packs", "web", "ops", "tests",
    "pyproject.toml", "uv.lock", ".gitignore", ".gitleaksignore",
)


@dataclass(frozen=True)
class FileState:
    content: bytes
    mode: int


@dataclass(frozen=True)
class PublicationState:
    current_target: Path | None
    previous_target: Path | None
    shim: FileState | None
    service_plist: FileState | None
    backup_plist: FileState | None
    service_loaded: bool
    backup_loaded: bool


def run(arguments: list[str], *, cwd: Path = SOURCE_ROOT, env: dict[str, str] | None = None, capture: bool = False, pass_fds: tuple[int, ...] = ()) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(arguments, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE if capture else None, check=False, pass_fds=pass_fds)
    if result.returncode != 0:
        detail = ""
        if capture:
            detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(arguments)}{': ' + detail if detail else ''}")
    return result


def exact_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def release_version() -> str:
    """Read the same static package version consumed by Hatch without importing source code."""

    raw = (SOURCE_ROOT / "lattice" / "__about__.py").read_bytes()
    match = VERSION_PATTERN.search(raw)
    if match is None:
        raise RuntimeError("Lattice package version is missing or not a stable semantic version")
    return match.group(1).decode("ascii")


def installed_runtime_version(target: Path) -> str:
    """Read the exact Lattice package version attested by an installed runtime."""

    raw = target.joinpath("runtime.json").read_bytes()
    manifest = json.loads(raw)
    if canonical_runtime_manifest(manifest, target / ".venv" / "bin" / "python") != raw or manifest.get("contract_version") != "LatticeInstalledRuntime/4.0" or manifest.get("runtime_id") != target.name:
        raise RuntimeError(f"installed runtime manifest is not exact for {target}")
    match = re.fullmatch(r"([0-9]+\.[0-9]+\.[0-9]+)-[0-9a-f]{12}(?:-[0-9a-f]{12})?", target.name)
    if match is None:
        raise RuntimeError(f"installed runtime identity has no stable package version: {target}")
    expected = match.group(1)
    observed = run([str(target / ".venv" / "bin" / "python"), "-c", "import lattice; print(lattice.__version__)"], cwd=target, capture=True).stdout.decode("utf-8", errors="strict").strip()
    if observed != expected:
        raise RuntimeError(f"installed runtime package version disagrees with its identity: expected {expected}, observed {observed}")
    return expected


def source_tree_inventories() -> dict[str, list[dict[str, str | int]]]:
    from lattice.runtime_attestation import mapped_inventory

    return {
        "lattice": mapped_inventory(((SOURCE_ROOT / "lattice", ""), (SOURCE_ROOT / "web" / "dist", "web_dist"), (SOURCE_ROOT / "workflow_packs", "workflow_packs"))),
    }


def installed_tree_inventories(locations: dict[str, str]) -> dict[str, list[dict[str, str | int]]]:
    from lattice.runtime_attestation import tree_inventory

    return {
        "lattice": tree_inventory(Path(locations["lattice"]).resolve(strict=True).parent),
    }


def runtime_sync_arguments() -> list[str]:
    """Force local packages to rebuild even when their public version is unchanged."""

    return ["uv", "sync", "--project", str(SOURCE_ROOT), "--active", "--frozen", "--no-dev", "--no-editable", "--reinstall-package", "lattice"]


def git_revision(root: Path, *, pathspec: str | None = None) -> str:
    # Installation consumes these paths, not assurance journals, Surface
    # history, or unrelated documentation in the same checkout.
    status_command = ["git", "status", "--porcelain=v1", "--untracked-files=all", "--", *(RELEASE_INPUTS if pathspec is None else (pathspec,))]
    status = run(status_command, cwd=root, capture=True).stdout.decode().strip()
    if status:
        raise RuntimeError(f"refusing to install dirty release inputs at {root}:\n{status}")
    return run(["git", "rev-parse", "HEAD"], cwd=root, capture=True).stdout.decode().strip()


def sync_file(descriptor: int) -> None:
    os.fsync(descriptor)
    if sys.platform == "darwin":
        fcntl.fcntl(descriptor, fcntl.F_FULLFSYNC)


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sync_tree(root: Path) -> None:
    for path in sorted((item for item in root.rglob("*") if item.is_file() and not item.is_symlink()), key=str):
        with path.open("rb") as handle:
            sync_file(handle.fileno())
    directories = [item for item in root.rglob("*") if item.is_dir() and not item.is_symlink()]
    for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        sync_directory(path)
    sync_directory(root)


def write_atomic(path: Path, content: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".pending", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError(f"write made no progress: {path}")
            view = view[written:]
        sync_file(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def canonical_runtime_manifest(value: dict, runtime_python: Path) -> bytes:
    """Canonicalize with the candidate's pinned RFC 8785 implementation."""

    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    result = subprocess.run(
        [str(runtime_python), "-c", "import json,rfc8785,sys; sys.stdout.buffer.write(rfc8785.dumps(json.load(sys.stdin)))"],
        input=encoded,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"candidate runtime could not canonicalize its manifest: {detail or f'exit {result.returncode}'}")
    return result.stdout


def replace_symlink(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = link.parent / f".{link.name}.{uuid.uuid4().hex}.pending"
    os.symlink(target, temporary)
    os.replace(temporary, link)
    sync_directory(link.parent)


def source_python() -> Path:
    candidate = SOURCE_ROOT / ".venv" / "bin" / "python"
    if not candidate.is_file():
        raise RuntimeError(f"source verification environment is missing: {candidate}")
    return candidate


def reexec_in_source_environment() -> None:
    """Enter the verified source environment before importing application code."""

    target = source_python().resolve()
    source_environment = SOURCE_ROOT / ".venv"
    if Path(sys.prefix).resolve() == source_environment.resolve():
        return
    os.execv(str(target), [str(target), str(Path(__file__).resolve()), *sys.argv[1:]])


def required_binary(name: str) -> Path:
    candidate = shutil.which(name)
    if candidate is None:
        raise RuntimeError(f"required deployment executable is unavailable: {name}")
    entrypoint = Path(candidate).absolute()
    resolved = entrypoint.resolve(strict=True)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise RuntimeError(f"required deployment executable is not runnable: {entrypoint}")
    return entrypoint


def runtime_path() -> str:
    entrypoints: dict[str, Path] = {"outport": required_binary("outport")}
    for name in ("claude", "codex", "agy", "grok"):
        candidate = shutil.which(name)
        if candidate is not None:
            entrypoints[name] = Path(candidate).absolute()

    directories: list[str] = []
    for binary in entrypoints.values():
        parent = str(binary.parent)
        if parent not in directories:
            directories.append(parent)
    for directory in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        if directory not in directories:
            directories.append(directory)

    # PATH is itself part of the provider attestation.  When two directories
    # contain the same command (for example ~/.grok/bin/grok and a stale
    # ~/.local/bin/grok), every attested directory must precede directories
    # that would shadow its selected entrypoint.  A stable topological order
    # preserves the discovery order where no constraint exists.
    precedence: dict[str, set[str]] = {directory: set() for directory in directories}
    for name, selected in entrypoints.items():
        selected_directory = str(selected.parent)
        for directory in directories:
            candidate = Path(directory) / name
            if directory != selected_directory and candidate.is_file() and os.access(candidate, os.X_OK):
                precedence[selected_directory].add(directory)
    ordered: list[str] = []
    remaining = set(directories)
    while remaining:
        ready = next((directory for directory in directories if directory in remaining and not any(directory in targets for source, targets in precedence.items() if source in remaining)), None)
        if ready is None:
            raise RuntimeError("provider entrypoints impose incompatible PATH precedence")
        ordered.append(ready)
        remaining.remove(ready)

    value = os.pathsep.join(ordered)
    for name, selected in entrypoints.items():
        resolved = shutil.which(name, path=value)
        if resolved is None or Path(resolved).absolute() != selected:
            raise RuntimeError(f"launchd PATH resolves a different {name} entrypoint: expected {selected}, observed {resolved}")
    return value


def verify_source() -> str:
    revision = git_revision(SOURCE_ROOT)
    run([str(source_python()), "-m", "pytest", "-q"])
    run(["npm", "run", "check"], cwd=SOURCE_ROOT / "web")
    run(["npm", "run", "build"], cwd=SOURCE_ROOT / "web")
    run(["npm", "test", "--", "--run"], cwd=SOURCE_ROOT / "web")
    if git_revision(SOURCE_ROOT) != revision:
        raise RuntimeError("source revision changed during verification; restart from one immutable release input set")
    return revision


def import_source_modules() -> None:
    if str(SOURCE_ROOT) not in sys.path:
        sys.path.insert(0, str(SOURCE_ROOT))


def _filesystem_invariants(root: Path) -> list[tuple[str, str, int]]:
    values: list[tuple[str, str, int]] = []
    for name in ("operator-token", "blobs", "model-runs"):
        source = root / name
        if not source.exists():
            continue
        candidates = [source] if source.is_file() else sorted(path for path in source.rglob("*") if path.is_file())
        for path in candidates:
            if path.is_symlink():
                raise OSError(f"migration source contains a symlink: {path}")
            values.append((path.relative_to(root).as_posix(), exact_sha256(path), path.stat().st_size))
    return values


def capture_migration_baseline(root: Path) -> dict:
    """Capture source-schema facts that a migration is permitted only to extend."""

    connection = sqlite3.connect(f"file:{(root / 'lattice.sqlite3').resolve(strict=True)}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tracked = sorted(str(row[0]) for row in connection.execute("SELECT tbl_name FROM sqlite_master WHERE type='trigger' AND name LIKE 'ledger_%_insert'"))
        tables = {}
        for table in tracked:
            info = [dict(row) for row in connection.execute(f'PRAGMA table_info("{table}")')]
            columns = [str(row["name"]) for row in info]
            primary = [str(row["name"]) for row in sorted(info, key=lambda item: item["pk"]) if row["pk"]]
            if not columns or not primary:
                raise OSError(f"migration source has no durable table contract for {table}")
            selected = ",".join(f'"{name}"' for name in columns)
            order = ",".join(f'"{name}"' for name in primary)
            tables[table] = {"columns": columns, "primary": primary, "rows": [tuple(row) for row in connection.execute(f'SELECT {selected} FROM "{table}" ORDER BY {order}')]}
        append_prefixes = {}
        for table, order in (("events", "seq"), ("ledger_changes", "seq"), ("append_integrity", "seq"), ("schema_migrations", "version")):
            columns = [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')]
            selected = ",".join(f'"{name}"' for name in columns)
            append_prefixes[table] = {"columns": columns, "order": order, "rows": [tuple(row) for row in connection.execute(f'SELECT {selected} FROM "{table}" ORDER BY "{order}"')]}
    finally:
        connection.close()
    return {"tables": tables, "append_prefixes": append_prefixes, "files": _filesystem_invariants(root)}


def verify_migration_baseline(root: Path, baseline: dict, *, candidate_provider_profiles: dict | None = None) -> None:
    connection = sqlite3.connect(f"file:{(root / 'lattice.sqlite3').resolve(strict=True)}?mode=ro", uri=True)
    try:
        for table, contract in baseline["tables"].items():
            selected = ",".join(f'"{name}"' for name in contract["columns"])
            order = ",".join(f'"{name}"' for name in contract["primary"])
            actual = [tuple(row) for row in connection.execute(f'SELECT {selected} FROM "{table}" ORDER BY {order}')]
            if actual != contract["rows"]:
                # Startup registers the candidate's immutable model catalog. This
                # is the sole permitted addition to a source-schema table: old
                # profiles stay byte-exact, and new definitions must come from
                # the installed candidate, not from the migrated database.
                originals = set(contract["rows"])
                preserved = [row for row in actual if row in originals]
                additions = [dict(zip(contract["columns"], row, strict=True)) for row in actual if row not in originals]
                permitted = table == "provider_profiles" and candidate_provider_profiles is not None and preserved == contract["rows"]
                for row in additions if permitted else ():
                    created_at = row.pop("created_at", None)
                    if not isinstance(created_at, str) or re.fullmatch(r"[0-9]{8}T[0-9]{6}", created_at) is None or row != candidate_provider_profiles.get(row.get("profile_id")):
                        permitted = False
                        break
                if not permitted:
                    raise OSError(f"Lattice migration changed source-schema rows in {table}")
        for table, contract in baseline["append_prefixes"].items():
            selected = ",".join(f'"{name}"' for name in contract["columns"])
            actual = [tuple(row) for row in connection.execute(f'SELECT {selected} FROM "{table}" ORDER BY "{contract["order"]}" LIMIT ?', (len(contract["rows"]),))]
            if actual != contract["rows"]:
                raise OSError(f"Lattice migration rewrote append-only history in {table}")
    finally:
        connection.close()
    if _filesystem_invariants(root) != baseline["files"]:
        raise OSError("Lattice migration changed immutable blob, journal, or operator-token bytes")


def migrate_legacy_data(legacy_root: Path) -> None:
    if DEFAULT_DATA_ROOT.exists():
        if not (DEFAULT_DATA_ROOT / "lattice.sqlite3").exists():
            raise RuntimeError(f"canonical Lattice root exists without a database: {DEFAULT_DATA_ROOT}")
        return
    if not (legacy_root / "lattice.sqlite3").is_file():
        DEFAULT_DATA_ROOT.mkdir(parents=True, exist_ok=False, mode=0o700)
        return
    import_source_modules()
    from lattice.diagnostics import assess_data_root
    from lattice.snapshot import create_snapshot, restore_snapshot
    from lattice.config import LatticeConfig
    from lattice.store import LatticeStore, SCHEMA_VERSION

    source_report = assess_data_root(legacy_root)
    baseline = capture_migration_baseline(legacy_root)
    temporary_backup = Path(tempfile.mkdtemp(prefix=".lattice-legacy-snapshot-", dir=legacy_root.parent))
    DEFAULT_DATA_ROOT.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    migration_wrapper = Path(tempfile.mkdtemp(prefix=".lattice-migration-", dir=DEFAULT_DATA_ROOT.parent))
    staged_root = migration_wrapper / "data"
    try:
        snapshot = create_snapshot(
            legacy_root,
            backup_root=temporary_backup,
            reason="legacy-repository-state-cutover",
            source_revision=source_report["state_digest"],
            schema_version=source_report["schema_version"],
            state_digest=source_report["state_digest"],
        )
        restore_snapshot(snapshot, staged_root)
        restored_report = assess_data_root(staged_root)
        if restored_report["state_digest"] != source_report["state_digest"]:
            raise OSError("legacy Lattice copy changed logical state before migration")
        LatticeStore(LatticeConfig(data_root=staged_root, axisvi_root=AXISVI_ROOT))
        target_report = assess_data_root(staged_root)
        if target_report["schema_version"] != SCHEMA_VERSION:
            raise OSError(f"staged Lattice migration ended at schema {target_report['schema_version']}, expected {SCHEMA_VERSION}")
        verify_migration_baseline(staged_root, baseline)
        sync_tree(staged_root)
        os.replace(staged_root, DEFAULT_DATA_ROOT)
        sync_directory(DEFAULT_DATA_ROOT.parent)
    finally:
        shutil.rmtree(temporary_backup)
        shutil.rmtree(migration_wrapper)


@contextmanager
def upgrade_quiescence():
    """Refuse active work and block new dispatch until the old writers stop.

    Durable work (leased/running attempts, runs, commands, imports, leases)
    refuses immediately: the installer never stops or restarts it. Lock
    contention is different. Every service boot holds the shared startup lock
    on `durable-runs.lock`, and a KeepAlive crash loop re-takes it every few
    seconds, so one non-blocking attempt fails for no durable reason exactly
    when recovery is needed most. That case is polled for a bounded time and
    the original refusal is raised once the budget is spent. Signals keep
    their existing semantics: cancellable phases raise at once, rollback
    sections honour the recorded signal after this bounded wait.
    """
    database = DEFAULT_DATA_ROOT / "lattice.sqlite3"
    if not database.exists():
        yield
        return
    import_source_modules()
    from lattice.run_journal import DURABLE_RUNS_LOCK
    from lattice.snapshot import _snapshot_guard

    lock = DEFAULT_DATA_ROOT / DURABLE_RUNS_LOCK
    deadline = time.monotonic() + QUIESCENCE_WAIT_SECONDS
    report_at = time.monotonic() + QUIESCENCE_REPORT_SECONDS
    with ExitStack() as stack:
        while True:
            try:
                stack.enter_context(_snapshot_guard(DEFAULT_DATA_ROOT, database, require_idle=True))
                break
            except RuntimeError as error:
                if not isinstance(error.__cause__, BlockingIOError):
                    raise  # active durable work, or any refusal that is not lock contention
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"{error} (waited {QUIESCENCE_WAIT_SECONDS:g}s for {lock})") from error
                if time.monotonic() >= report_at:
                    print(f"Waiting for a service startup, snapshot or journal writer to release {lock}", flush=True)
                    report_at = time.monotonic() + QUIESCENCE_REPORT_SECONDS
                time.sleep(QUIESCENCE_POLL_SECONDS)
        yield


class MaintenanceOwned(RuntimeError):
    """Another installer, recovery, or driver currently owns maintenance."""


class MaintenanceInterrupted(BaseException):
    """A graceful stop signal arrived; the existing rollback paths handle it."""


class MaintenanceRecoveryRefused(RuntimeError):
    """Interrupted state exists but automatic restoration would discard admitted work."""


class RollbackSnapshotStale(RuntimeError):
    """The live database moved after the rollback snapshot was captured."""


@contextmanager
def maintenance_lock():
    """Single maintenance owner per data root.

    `flock(LOCK_EX|LOCK_NB)` on a file that is never unlinked. The kernel drops
    the lock when the holder dies, so SIGKILL cannot leave it stuck and pid reuse
    cannot forge ownership; the pid written inside is a diagnostic only.
    """
    DEFAULT_DATA_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = DEFAULT_DATA_ROOT / MAINTENANCE_LOCK
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            holder = os.read(descriptor, 256).decode("utf-8", errors="replace").strip()
            raise MaintenanceOwned(f"maintenance already owned ({holder or 'holder unknown'}); the flock on {path} is the authority, wait for or stop that process") from error
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"pid={os.getpid()} started={time.strftime('%Y%m%dT%H%M%S')} argv={shlex.join(sys.argv)}\n".encode())
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


class _Cancellation:
    armed = False
    cancellable = False
    received: list[int] = []


_cancellation = _Cancellation()


def _graceful_signal(signum: int, _frame) -> None:
    _cancellation.received.append(signum)
    if _cancellation.cancellable:
        # Raise once; rollback code below runs with cancellable=False so a second
        # `runjob stop` or Ctrl-C cannot interrupt restoration mid-operation.
        _cancellation.cancellable = False
        raise MaintenanceInterrupted(f"maintenance interrupted by signal {signal.Signals(signum).name}")
    print(f"signal {signal.Signals(signum).name} received during rollback; finishing restoration first", file=sys.stderr, flush=True)


@contextmanager
def cancellation_scope():
    """Turn SIGTERM/SIGINT/SIGHUP into one MaintenanceInterrupted in the main thread.

    Python delivers handlers between bytecodes of the main thread, so the
    exception can surface inside any operation. `subprocess.run` kills and reaps
    its direct child on that exception; grandchildren are not covered. SIGKILL
    never invokes handlers; the durable maintenance record covers that case.
    """
    if threading.current_thread() is not threading.main_thread() or _cancellation.armed:
        yield
        return
    previous = {signum: signal.signal(signum, _graceful_signal) for signum in GRACEFUL_SIGNALS}
    _cancellation.received = []
    _cancellation.armed = True
    _cancellation.cancellable = True
    try:
        yield
    finally:
        _cancellation.cancellable = False
        _cancellation.armed = False
        for signum, handler in previous.items():
            signal.signal(signum, handler)


@contextmanager
def rollback_section():
    """While actually restoring, graceful signals are recorded, not raised.

    The prior cancellable state is restored on every exit, so a later phase in
    the same scope (for example a new upgrade after automatic recovery) remains
    cancellable. A signal received during restoration is honoured as soon as the
    restoration completes instead of being swallowed.
    """
    previous = _cancellation.cancellable
    seen = len(_cancellation.received)
    _cancellation.cancellable = False
    try:
        yield
    except BaseException:
        _cancellation.cancellable = previous
        raise
    _cancellation.cancellable = previous
    if previous and len(_cancellation.received) > seen:
        names = ",".join(signal.Signals(number).name for number in _cancellation.received[seen:])
        raise MaintenanceInterrupted(f"maintenance interrupted by signal {names} received during restoration; restoration completed")


def live_database_generation() -> tuple[str, int]:
    """Current (state_digest, schema_version) from one pinned read; no blob hashing."""
    import_source_modules()
    from lattice.diagnostics import _begin_read_snapshot, _connect_read_only, _state_digest, _tracked_tables

    connection = _connect_read_only(DEFAULT_DATA_ROOT / "lattice.sqlite3")
    try:
        _begin_read_snapshot(connection)
        schema_version = int(connection.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0])
        digest = _state_digest(connection, _tracked_tables(connection))
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    return digest, schema_version


def verify_rollback_snapshot_current(snapshot: Path | None) -> None:
    """Online semantic binding for current and legacy rollback snapshots.

    This growing-history comparison runs before admission exclusion. The
    retained CutoverIdentity detects changes through the final bounded check.
    Snapshot integrity and required blob bytes are independently verified by
    the candidate or reuse path; a token alone cannot establish them.
    """
    if snapshot is None:
        return
    manifest = json.loads((snapshot / "snapshot.json").read_bytes())
    saved = (manifest["state_digest"], int(manifest["schema_version"]))
    current = live_database_generation()
    if current != saved:
        raise RollbackSnapshotStale(f"rollback snapshot {snapshot.name} is stale at cutover: saved {saved} live {current}; the service was not stopped, re-run to capture the current generation")



def database_change_token(connection: sqlite3.Connection, *, schema_cookie: bool = True) -> dict:
    """Schema-sized coverage checks and indexed heads; never an integrity proof.

    Actual table columns generate the expected tracking triggers, including old
    schemas. Fixed stream/immutability triggers come from the shipped DDL. The
    full semantic verifier remains the independent baseline authority.
    """
    from lattice.store import CHANGE_TABLES, DDL, LatticeStore

    if connection.in_transaction:
        raise RuntimeError("change token requires its own short read transaction")
    connection.execute("BEGIN")
    try:
        schema = [tuple(row) for row in connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]
        tables = {row[1] for row in schema if row[0] == "table"}
        streams = {"events", "ledger_changes", "append_integrity", "schema_migrations"}
        if not streams <= tables or tables - set(CHANGE_TABLES) - streams - {"sqlite_sequence"}:
            raise RollbackSnapshotStale("database has missing streams or unclassified tables")
        # No live-store initialization: build only schema in a private memory DB.
        reference = sqlite3.connect(":memory:")
        reference.row_factory = sqlite3.Row
        try:
            for kind, name, _table, sql in schema:
                if kind == "table" and name in CHANGE_TABLES:
                    reference.execute(sql)
            reference.executescript(DDL)
            LatticeStore._install_change_triggers(reference)
            expected = {row[0]: row[1] for row in reference.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name IN (" + ",".join("?" for _ in tables) + ")", tuple(tables))}
        finally:
            reference.close()
        actual = {row[1]: row[3] for row in schema if row[0] == "trigger"}

        def normalized(sql: str) -> tuple[str, ...]:
            # Compare only the shipped trigger templates' lexical grammar. This
            # is not SQL semantic equivalence: comments and unknown punctuation
            # refuse; quoted contents remain exact (including whitespace).
            # The only identifier aliases are the three registered by _open_connection.
            grammar = r'''\s+|'(?:[^']|'')*'|"(?:[^"]|"")*"|[A-Za-z_][A-Za-z_0-9]*|[0-9]+|<>|[(),.;=]'''
            aliases = {"loom_replaying": "lattice_replaying", "loom_integrity_hash": "lattice_integrity_hash", "loom_stamp": "lattice_stamp"}
            tokens = []
            offset = 0
            for match in re.finditer(grammar, sql):
                if match.start() != offset:
                    raise RollbackSnapshotStale("database trigger contains unsupported SQL spelling")
                offset = match.end()
                token = match.group()
                if not token.isspace():
                    tokens.append(aliases.get(token, token))
            if offset != len(sql):
                raise RollbackSnapshotStale("database trigger contains unsupported SQL spelling")
            return tuple(tokens)

        if actual.keys() != expected.keys() or any(normalized(actual[name]) != normalized(sql) for name, sql in expected.items()):
            raise RollbackSnapshotStale("database trigger coverage or definitions changed")
        heads = {}
        for table, columns in (("events", "seq"), ("ledger_changes", "seq"), ("append_integrity", "seq,entry_sha256")):
            row = connection.execute(f'SELECT {columns} FROM "{table}" ORDER BY seq DESC LIMIT 1').fetchone()
            heads[table] = None if row is None else list(row)
        token = {
            "heads": heads,
            "migrations": [list(row) for row in connection.execute("SELECT * FROM schema_migrations ORDER BY version")],
            "schema_sha256": hashlib.sha256(json.dumps(schema, separators=(",", ":")).encode()).hexdigest(),
        }
        if schema_cookie:
            token["schema_cookie"] = connection.execute("PRAGMA schema_version").fetchone()[0]
        return token
    finally:
        connection.rollback()


class WatchedControl:
    """Online content binding plus a retained macOS kernel mutation detector.

    kqueue(2), EVFILT_VNODE: NOTE_WRITE observes writes, not timestamps. Watch
    before hashing and never clear events: mutate-and-restore also invalidates.
    The file watch covers manifest replacement; the containing directory watch
    covers its own rename/removal, not unrelated sibling entries.
    This covers normal local filesystem syscalls, not raw-device corruption,
    writable mmap stores or transient replacement of higher path ancestors.
    Other platforms retain exact hashing; only the macOS path has the byte bound.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.queue = None
        self.descriptors: list[int] = []
        self.identities = []
        self.invalid = False
        self.closed = False
        try:
            if hasattr(select, "kqueue"):
                self.queue = select.kqueue()
                flags = (select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND | select.KQ_NOTE_ATTRIB |
                         select.KQ_NOTE_LINK | select.KQ_NOTE_RENAME | select.KQ_NOTE_DELETE | select.KQ_NOTE_REVOKE)
                directory_flags = select.KQ_NOTE_ATTRIB | select.KQ_NOTE_RENAME | select.KQ_NOTE_DELETE | select.KQ_NOTE_REVOKE
                for watched, watched_flags in ((path, flags), (path.parent, directory_flags)):
                    descriptor = os.open(watched, os.O_RDONLY)
                    self.descriptors.append(descriptor)
                    info = os.fstat(descriptor)
                    self.identities.append((info.st_dev, info.st_ino))
                    self.queue.control([select.kevent(descriptor, filter=select.KQ_FILTER_VNODE,
                                       flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE, fflags=watched_flags)], 0, 0)
            self.digest = exact_sha256(path)
            self.verify()
        except BaseException:
            self.close()
            raise

    def verify(self) -> None:
        if self.closed:
            raise RollbackSnapshotStale(f"qualified publication input watch lost: {self.path}")
        try:
            if self.queue is None:
                changed = exact_sha256(self.path) != self.digest
            else:
                changed = bool(self.queue.control(None, 1, 0))
                for path, identity in zip((self.path, self.path.parent), self.identities, strict=True):
                    info = path.stat()
                    changed |= (info.st_dev, info.st_ino) != identity
            self.invalid |= changed
            if self.invalid:
                raise RollbackSnapshotStale(f"qualified publication input changed: {self.path}")
        except (OSError, ValueError) as error:
            raise RollbackSnapshotStale(f"qualified publication input watch invalidated: {self.path}") from error

    def close(self) -> None:
        self.closed = True
        if self.queue is not None:
            self.queue.close()
            self.queue = None
        for descriptor in self.descriptors:
            os.close(descriptor)
        self.descriptors.clear()


class CutoverIdentity:
    """Process-local sentinel, bound by an online full comparison to recovery bytes.

    data_version is meaningful only on this same read-only connection. No long
    transaction is held; a lost connection invalidates qualification. File
    replacement is checked separately because a connection follows its inode.
    """

    def __init__(self, database: Path) -> None:
        self.database = database
        self.connection: sqlite3.Connection | None = None
        self.identity = None
        self.token = None
        self.version = None
        self.controls: list[tuple[Path, str]] = []
        self.watched_controls: list[WatchedControl] = []
        self.qualified = False
        try:
            self.identity = self.file_identity()
            self.connection = sqlite3.connect(database.resolve(strict=True).as_uri() + "?mode=ro", uri=True)
            self.version = self.data_version()
            self.token = database_change_token(self.connection)
            self.assert_unchanged()
        except BaseException:
            self.close()
            raise

    def file_identity(self) -> tuple[int, int]:
        info = self.database.stat()
        return info.st_dev, info.st_ino

    def data_version(self) -> int:
        if self.connection is None or self.connection.in_transaction:
            raise RollbackSnapshotStale("cutover sentinel lost or holds a read transaction; requalify")
        return self.connection.execute("PRAGMA data_version").fetchone()[0]

    def assert_unchanged(self) -> None:
        try:
            if self.file_identity() != self.identity or self.data_version() != self.version:
                raise RollbackSnapshotStale("database changed during online qualification; service was not stopped")
            if database_change_token(self.connection) != self.token or self.data_version() != self.version:
                raise RollbackSnapshotStale("database token changed; service was not stopped")
        except (OSError, sqlite3.Error) as error:
            raise RollbackSnapshotStale("cutover sentinel invalidated; requalify before stopping") from error

    def bind_file(self, path: Path, *, watch: bool = False) -> None:
        if watch:
            self.watched_controls.append(WatchedControl(path))
        else:
            self.controls.append((path, exact_sha256(path)))

    def bind_snapshot(self, snapshot: Path) -> None:
        from lattice.snapshot import materialized_snapshot_database

        manifest = json.loads((snapshot / "snapshot.json").read_bytes())
        with materialized_snapshot_database(snapshot, manifest) as database:
            connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                saved = database_change_token(connection, schema_cookie=False)
            finally:
                connection.close()
        current = {key: value for key, value in self.token.items() if key != "schema_cookie"}
        if saved != current:
            raise RollbackSnapshotStale("rollback snapshot schema, migration history or heads differ from live baseline")

    def verify(self) -> None:
        if not self.qualified:
            raise RollbackSnapshotStale("cutover identity has no independently verified baseline")
        self.assert_unchanged()
        for control in self.watched_controls:
            control.verify()
        for path, digest in self.controls:
            if exact_sha256(path) != digest:
                raise RollbackSnapshotStale(f"qualified publication input changed: {path}")

    def close(self) -> None:
        for control in self.watched_controls:
            control.close()
        self.watched_controls.clear()
        if self.connection is not None:
            self.connection.close()
            self.connection = None


@contextmanager
def qualified_cutover_identity(target: Path, snapshot: Path | None):
    """Bracket expensive candidate and semantic checks while service stays online."""
    database = DEFAULT_DATA_ROOT / "lattice.sqlite3"
    if snapshot is None:
        if database.exists():
            raise RollbackSnapshotStale("database exists without a rollback snapshot")
        yield None
        return
    import_source_modules()
    identity = CutoverIdentity(database)
    try:
        identity.bind_file(snapshot / "snapshot.json", watch=True)
        identity.bind_file(target / "runtime.json")
        yield identity
    finally:
        identity.close()

def backup_before_upgrade() -> Path | None:
    def source_backup(reason: str) -> Path | None:
        import_source_modules()
        from lattice.diagnostics import assess_backup, assess_data_root
        from lattice.snapshot import create_snapshot

        if not (DEFAULT_DATA_ROOT / "lattice.sqlite3").is_file():
            return None
        report = assess_data_root(DEFAULT_DATA_ROOT)
        snapshot = create_snapshot(DEFAULT_DATA_ROOT, reason=reason, source_revision=report["state_digest"], schema_version=report["schema_version"], state_digest=report["state_digest"])
        assess_backup(snapshot)
        return snapshot

    current_cli = CURRENT_LINK / ".venv" / "bin" / "lattice"
    if current_cli.is_file():
        try:
            result = run([str(current_cli), "backup", "--reason", "pre-runtime-upgrade", "--json"], capture=True)
        except RuntimeError as error:
            # Backup/3 embeds every retained file in one JSON tree. An older
            # writer may hit its structural-node limit before this upgrade.
            detail = str(error)
            if "canonical JSON exceeds" not in detail or "structural nodes" not in detail:
                raise
            return source_backup("pre-runtime-upgrade")
        response = json.loads(result.stdout)
        if not isinstance(response, dict) or not isinstance(response.get("snapshot"), str):
            raise RuntimeError("installed backup command did not return a snapshot path")
        snapshot = Path(response["snapshot"]).resolve(strict=True)
        if snapshot.parent != (DEFAULT_DATA_ROOT / "backups").resolve():
            raise RuntimeError("installed backup command returned a snapshot outside the canonical backup root")
        return snapshot
    return source_backup("pre-first-installed-runtime")


def reuse_rollback_snapshot(snapshot: Path) -> Path:
    """Reuse recovery bytes only when their logical database is still current.

    Rollback restores the database; immutable blobs and journals remain in the
    canonical store. Later append-only files do not require copying the archive
    again. This is local rollback evidence, not a new disaster-recovery backup.
    """
    import_source_modules()
    from lattice.diagnostics import assess_backup, assess_data_root

    snapshot = snapshot.expanduser().resolve(strict=True)
    saved = assess_backup(snapshot)
    current = assess_data_root(DEFAULT_DATA_ROOT)
    if any(current["active_work"].values()):
        raise RuntimeError("cannot reuse rollback snapshot while durable work is active")
    if (saved["state_digest"], saved["schema_version"]) != (current["state_digest"], current["schema_version"]):
        raise RuntimeError("rollback snapshot is stale; capture the current database generation")
    print(f"Reusing verified current rollback snapshot: {snapshot}", flush=True)
    return snapshot


def verify_candidate_backup(target: Path, snapshot: Path | None) -> None:
    """Reject a runtime that cannot verify the backup needed for its rollback."""

    if snapshot is None:
        return
    run([
        str(target / ".venv" / "bin" / "python"),
        "-c",
        "import sys; from pathlib import Path; from lattice.diagnostics import assess_backup; assess_backup(Path(sys.argv[1]))",
        str(snapshot),
    ], cwd=target, capture=True)


def verify_candidate_workflow_packs(target: Path) -> None:
    """Validate shipped definitions before cutover, independently of external packs."""
    run([
        str(target / ".venv" / "bin" / "python"), "-c",
        "from lattice.config import WORKFLOW_PACK_ROOT; from lattice.workflows import WorkflowRegistry; "
        "failures=WorkflowRegistry(WORKFLOW_PACK_ROOT).catalogue()['failures']; "
        "\nif failures: raise RuntimeError(f'invalid bundled workflow packs: {failures}')",
    ], cwd=target, capture=True)


def candidate_provider_profile_catalog(target: Path) -> dict:
    """Read admitted definitions from the frozen candidate without opening state."""

    result = run([
        str(target / ".venv" / "bin" / "python"), "-c",
        "import json; from lattice.execution import DEFAULT_ADAPTERS; from lattice.canonical import canonical_bytes,sha256_bytes; "
        "profiles=[base.select_model(model) for base in DEFAULT_ADAPTERS for model in base.supported_models or (base.model_code,)]; "
        "print(json.dumps({p.profile_id:dict(profile_id=p.profile_id,provider_code=p.provider_code,harness=p.harness,model_code=p.model_code,definition_json=canonical_bytes(p.definition()).decode(),definition_sha256=sha256_bytes(canonical_bytes(p.definition()))) for p in profiles}))",
    ], cwd=target, capture=True)
    catalog = json.loads(result.stdout)
    if not isinstance(catalog, dict) or not catalog:
        raise RuntimeError("candidate runtime returned no provider profile catalog")
    return catalog


def qualify_candidate_migration(target: Path, snapshot: Path | None) -> None:
    """Exercise the candidate's migration on an independent saved-state copy."""

    if snapshot is None:
        return
    from lattice.diagnostics import assess_data_root
    from lattice.snapshot import temporary_restored_snapshot
    from lattice.canonical import canonical_bytes, generated_object

    print("Restoring temporary migration rehearsal copy", flush=True)
    with temporary_restored_snapshot(snapshot) as root:
        print("Checking restored state integrity", flush=True)
        captured = assess_data_root(root)
        print("Capturing original rows and immutable-file identities", flush=True)
        before = capture_migration_baseline(root)
        catalog = candidate_provider_profile_catalog(target)
        command = [
            str(target / ".venv" / "bin" / "python"), "-c",
            "import sys; from pathlib import Path; from lattice import LatticeStore,LatticeConfig; LatticeStore(LatticeConfig(data_root=Path(sys.argv[1]),axisvi_root=None))",
            str(root),
        ]
        print("Running candidate migration on the temporary copy", flush=True)
        run(command, cwd=target, capture=True)
        print("Checking preserved rows, history and immutable files", flush=True)
        verify_migration_baseline(root, before, candidate_provider_profiles=catalog)
        print("Checking migrated state integrity", flush=True)
        report = assess_data_root(root)
        if report["schema_version"] != report["expected_schema_version"]:
            raise RuntimeError("candidate migration did not reach the supported schema")
        receipt = {
            "generated_object": generated_object(kind="lattice-candidate-migration", schema_name="lattice-candidate-migration", subject=f"lattice:upgrade:{target.name}", role="snapshot", sources=[
                {"ref": f"file:{target / 'runtime.json'}", "revision": None, "sha256": exact_sha256(target / "runtime.json")},
                {"ref": f"file:{snapshot / 'snapshot.json'}", "revision": None, "sha256": exact_sha256(snapshot / "snapshot.json")},
                {"ref": f"file:{Path(__file__).resolve()}", "revision": None, "sha256": exact_sha256(Path(__file__).resolve())},
            ], producer_name="lattice-installer", producer_version="4.1.0"),
            "contract_version": "LatticeCandidateMigration/1.0",
            "command": command, "runtime_id": target.name,
            "before": {"state_digest": captured["state_digest"], "schema_version": captured["schema_version"]},
            "after": {"state_digest": report["state_digest"], "schema_version": report["schema_version"]},
            "preserved_tables": {name: len(value["rows"]) for name, value in before["tables"].items()},
            "candidate_provider_profile_catalog": catalog,
            "preserved_append_prefixes": {name: len(value["rows"]) for name, value in before["append_prefixes"].items()},
            "preserved_file_count": len(before["files"]),
            "copy_retention": "disposable: reproduce by restoring the referenced snapshot",
        }
        path = DEFAULT_DATA_ROOT / "acceptance" / f"candidate-migration-{receipt['generated_object']['generated_at']}-{uuid.uuid4().hex}.json"
        write_atomic(path, canonical_bytes(receipt), mode=0o600)
        print(f"Candidate copied-store migration qualified at schema {report['schema_version']}: {path}", flush=True)


def restore_upgrade_database(snapshot: Path) -> None:
    """Restore the quiesced database before an older runtime may restart.

    Failed-candidate database/WAL bytes remain separately inspectable. Extra
    immutable blobs and journals stay retained; none are removed by rollback.
    """

    from lattice.diagnostics import assess_data_root
    from lattice.snapshot import materialized_snapshot_database, verify_snapshot

    manifest = verify_snapshot(snapshot)
    with materialized_snapshot_database(snapshot, manifest) as source:
        descriptor, name = tempfile.mkstemp(prefix=".lattice-rollback-", dir=DEFAULT_DATA_ROOT)
        os.close(descriptor)
        staged = Path(name)
        try:
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as saved, closing(sqlite3.connect(staged)) as destination:
                schema_query = "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
                migrations_query = "SELECT * FROM schema_migrations ORDER BY version"
                saved_schema = saved.execute(schema_query).fetchall()
                saved_migrations = saved.execute(migrations_query).fetchall()
                # Materialization retains declared WAL; only SQLite's backup
                # API copies the committed database rather than just main pages.
                saved.backup(destination)
                if destination.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                    raise RuntimeError("rollback staging could not produce a standalone database")
            with closing(sqlite3.connect(staged.as_uri() + "?mode=ro", uri=True)) as check:
                if check.execute(schema_query).fetchall() != saved_schema or check.execute(migrations_query).fetchall() != saved_migrations:
                    raise RuntimeError("rollback staging changed snapshot schema or migration history; active database retained")
            # Validate the staged image against the snapshot before moving any
            # active bytes. State digests alone do not bind schema/migrations.
            prepared = assess_data_root(DEFAULT_DATA_ROOT, database_path=staged)
            if (prepared["state_digest"], prepared["schema_version"]) != (manifest["state_digest"], int(manifest["schema_version"])):
                raise RuntimeError("rollback staging did not reproduce pre-upgrade state; active database retained")
            staged.chmod(0o600)
            with staged.open("rb") as handle:
                sync_file(handle.fileno())
            archive = DEFAULT_DATA_ROOT / "acceptance" / "failed-upgrades" / f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex}"
            archive.mkdir(parents=True, mode=0o700)
            index = archive.parent / "README.md"
            if not index.exists():
                write_atomic(index, b"# Failed upgrades\n\nEach directory retains a stopped candidate database and sidecars before rollback. These private records preserve candidate work and diagnose failed publication; the active database lives at the data root.\n", mode=0o600)
            write_atomic(archive / "README.md", f"# Failed upgrade state\n\nDatabase and SQLite sidecars retained before restoring `{snapshot}`. These bytes preserve candidate work for inspection; they are not the active database.\n".encode(), mode=0o600)
            for name in ("lattice.sqlite3", "lattice.sqlite3-wal", "lattice.sqlite3-shm"):
                path = DEFAULT_DATA_ROOT / name
                if path.is_symlink():
                    raise RuntimeError(f"rollback database path is a symlink: {path}")
                if path.exists():
                    os.replace(path, archive / name)
            sync_tree(archive)
            os.replace(staged, DEFAULT_DATA_ROOT / "lattice.sqlite3")
            sync_directory(DEFAULT_DATA_ROOT)
        finally:
            staged.unlink(missing_ok=True)
    restored = assess_data_root(DEFAULT_DATA_ROOT)
    if restored["state_digest"] != manifest["state_digest"]:
        raise RuntimeError("rollback database did not reproduce pre-upgrade state; prior runtime remains stopped")


def probe_runtime(target: Path) -> dict[str, str]:
    result = run([
        str(target / ".venv" / "bin" / "python"),
        "-c",
        "import json,lattice; print(json.dumps({'lattice':lattice.__file__}))",
    ], cwd=target, capture=True)
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or set(value) != {"lattice"} or any(not isinstance(location, str) for location in value.values()):
        raise RuntimeError(f"installed runtime probe returned invalid module locations: {value}")
    return value


def provider_runtime_attestations(*, probe_versions: bool) -> list[dict]:
    """Bind immutable profiles and the exact official CLI entrypoints observed at qualification."""

    from lattice.execution import installed_adapter_status

    return sorted(installed_adapter_status(probe_versions=probe_versions), key=lambda item: item["provider_code"])


def runtime_identity(lattice_revision: str, provider_attestations: list[dict]) -> str:
    """Key immutable installations by source and the qualified external CLI state."""

    from lattice.canonical import canonical_bytes

    _validate_provider_attestations(provider_attestations)
    provider_digest = hashlib.sha256(canonical_bytes(provider_attestations)).hexdigest()[:12]
    return f"{release_version()}-{lattice_revision[:12]}-{provider_digest}"


def _validate_provider_attestations(value: object) -> list[dict]:
    from lattice.runtime_attestation import validate_provider_attestations

    try:
        return validate_provider_attestations(value)
    except ValueError as error:
        raise RuntimeError(str(error)) from error


def _generated_object_source(ref: str, *, revision: str | None, sha256: str | None) -> dict[str, str | None]:
    if not ref or (revision is None and sha256 is None):
        raise ValueError("runtime sources require a ref and immutable revision or digest")
    return {"ref": ref, "revision": revision, "sha256": sha256}


def _valid_artifact_id(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("artifact:"):
        return False
    try:
        return str(uuid.UUID(value.removeprefix("artifact:"))) == value.removeprefix("artifact:")
    except ValueError:
        return False


def _validate_runtime_manifest(manifest: object, *, target: Path, runtime_id: str, lattice_revision: str) -> dict:
    if not isinstance(manifest, dict) or set(manifest) != {"generated_object", "contract_version", "runtime_id", "python", "packages", "module_locations", "source_trees", "provider_attestations"}:
        raise RuntimeError("runtime manifest body is malformed")
    envelope = manifest.get("generated_object")
    required = {"contract_version", "role", "kind", "schema", "artifact_id", "generated_at", "producer", "subject", "sources"}
    if not isinstance(envelope, dict) or set(envelope) != required:
        raise RuntimeError("runtime generated-object envelope is malformed")
    if envelope.get("contract_version") != 1 or envelope.get("role") != "canonical" or envelope.get("kind") != "lattice-installed-runtime" or envelope.get("schema") != {"name": "lattice-installed-runtime", "version": 4} or envelope.get("producer") != {"name": "lattice-installer", "version": "4.0.0"}:
        raise RuntimeError("runtime generated-object contract identity is invalid")
    if not _valid_artifact_id(envelope.get("artifact_id")) or envelope.get("subject") != f"lattice:installed-runtime:{runtime_id}":
        raise RuntimeError("runtime generated-object durable identity is invalid")
    try:
        time.strptime(str(envelope.get("generated_at")), "%Y%m%dT%H%M%S")
    except ValueError as error:
        raise RuntimeError("runtime generated-object timestamp is invalid") from error
    expected_sources = [
        _generated_object_source(f"git:{SOURCE_ROOT}", revision=lattice_revision, sha256=None),
        _generated_object_source(f"file:{SOURCE_ROOT / 'uv.lock'}", revision=None, sha256=exact_sha256(SOURCE_ROOT / "uv.lock")),
    ]
    if envelope.get("sources") != expected_sources:
        raise RuntimeError("runtime target provenance disagrees with its version identity or lockfile")
    expected_python = str((target / ".venv" / "bin" / "python").resolve(strict=True))
    if manifest.get("contract_version") != "LatticeInstalledRuntime/4.0" or manifest.get("runtime_id") != runtime_id or manifest.get("python") != expected_python:
        raise RuntimeError("runtime manifest identity or Python executable is invalid")
    packages = manifest.get("packages")
    locations = manifest.get("module_locations")
    if not isinstance(packages, list) or any(not isinstance(item, str) or not item for item in packages) or not isinstance(locations, dict) or set(locations) != {"lattice"} or any(not isinstance(item, str) or not item for item in locations.values()):
        raise RuntimeError("runtime manifest package or module inventory is malformed")
    try:
        from lattice.runtime_attestation import validate_tree_set

        validate_tree_set(manifest.get("source_trees"))
    except ValueError as error:
        raise RuntimeError("runtime manifest source-tree attestation is malformed") from error
    attestations = _validate_provider_attestations(manifest.get("provider_attestations"))
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]{12}-[0-9a-f]{12}", runtime_id):
        if runtime_id != runtime_identity(lattice_revision, attestations):
            raise RuntimeError("runtime identity disagrees with its provider attestations")
    return manifest


def build_runtime_manifest(*, target: Path, runtime_id: str, lattice_revision: str, packages: list[str], module_locations: dict[str, str], source_trees: dict[str, list[dict[str, str | int]]], provider_attestations: list[dict]) -> dict:
    return {
        "generated_object": {
            "contract_version": 1,
            "role": "canonical",
            "kind": "lattice-installed-runtime",
            "schema": {"name": "lattice-installed-runtime", "version": 4},
            "artifact_id": f"artifact:{uuid.uuid4()}",
            "generated_at": time.strftime("%Y%m%dT%H%M%S"),
            "producer": {"name": "lattice-installer", "version": "4.0.0"},
            "subject": f"lattice:installed-runtime:{runtime_id}",
            "sources": [
                _generated_object_source(f"git:{SOURCE_ROOT}", revision=lattice_revision, sha256=None),
                _generated_object_source(f"file:{SOURCE_ROOT / 'uv.lock'}", revision=None, sha256=exact_sha256(SOURCE_ROOT / "uv.lock")),
            ],
        },
        "contract_version": "LatticeInstalledRuntime/4.0",
        "runtime_id": runtime_id,
        "python": str((target / ".venv" / "bin" / "python").resolve(strict=True)),
        "packages": packages,
        "module_locations": module_locations,
        "source_trees": source_trees,
        "provider_attestations": provider_attestations,
    }


def verify_runtime_target(target: Path, *, runtime_id: str, lattice_revision: str) -> None:
    marker = target / ".pending"
    manifest_path = target / "runtime.json"
    executable = target / ".venv" / "bin" / "lattice"
    if marker.exists() or not manifest_path.is_file() or not executable.is_file():
        raise RuntimeError(f"runtime target is incomplete: {target}")
    try:
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"runtime manifest is unreadable: {manifest_path}") from error
    manifest = _validate_runtime_manifest(manifest, target=target, runtime_id=runtime_id, lattice_revision=lattice_revision)
    if canonical_runtime_manifest(manifest, target / ".venv" / "bin" / "python") != raw:
        raise RuntimeError(f"runtime manifest is not exact RFC 8785 canonical JSON: {manifest_path}")
    locations = probe_runtime(target)
    if locations != manifest["module_locations"]:
        raise RuntimeError(f"runtime module locations changed after installation: recorded={manifest['module_locations']}, actual={locations}")
    if any(str(SOURCE_ROOT) in location for location in locations.values()):
        raise RuntimeError(f"installed runtime still imports a development checkout: {locations}")
    expected_trees = source_tree_inventories()
    if manifest["source_trees"] != expected_trees:
        raise RuntimeError("runtime manifest source-tree attestation disagrees with the immutable source revision")
    actual_trees = installed_tree_inventories(locations)
    if actual_trees != manifest["source_trees"]:
        raise RuntimeError("installed runtime application bytes disagree with the attested source trees")


def install_runtime(lattice_revision: str) -> Path:
    attestations = provider_runtime_attestations(probe_versions=True)
    runtime_id = runtime_identity(lattice_revision, attestations)
    target = VERSIONS_ROOT / runtime_id
    marker = target / ".pending"
    if target.exists():
        verify_runtime_target(target, runtime_id=runtime_id, lattice_revision=lattice_revision)
        return target
    VERSIONS_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.mkdir(mode=0o700)
    write_atomic(marker, b"installation is not published\n", mode=0o600)
    try:
        run(["uv", "venv", str(target / ".venv"), "--python", str(source_python())])
        environment = dict(os.environ)
        environment["VIRTUAL_ENV"] = str(target / ".venv")
        run(runtime_sync_arguments(), env=environment)
        locations = probe_runtime(target)
        if any(str(SOURCE_ROOT) in location for location in locations.values()):
            raise RuntimeError(f"installed runtime still imports a development checkout: {locations}")
        source_trees = source_tree_inventories()
        if installed_tree_inventories(locations) != source_trees:
            raise RuntimeError("freshly installed application bytes disagree with the immutable source trees")
        run([str(target / ".venv" / "bin" / "lattice"), "--help"])
        frozen = run(["uv", "pip", "freeze", "--python", str(target / ".venv" / "bin" / "python")], capture=True).stdout.decode().splitlines()
        manifest = build_runtime_manifest(target=target, runtime_id=runtime_id, lattice_revision=lattice_revision, packages=frozen, module_locations=locations, source_trees=source_trees, provider_attestations=attestations)
        write_atomic(target / "runtime.json", canonical_runtime_manifest(manifest, target / ".venv" / "bin" / "python"), mode=0o600)
        sync_tree(target)
        marker.unlink()
        sync_directory(target)
        sync_directory(VERSIONS_ROOT)
        verify_runtime_target(target, runtime_id=runtime_id, lattice_revision=lattice_revision)
        return target
    except BaseException:
        shutil.rmtree(target)
        sync_directory(VERSIONS_ROOT)
        raise


def install_links(target: Path) -> Path | None:
    for link in (CURRENT_LINK, PREVIOUS_LINK):
        if (link.exists() or link.is_symlink()) and not link.is_symlink():
            raise RuntimeError(f"refusing to replace non-symlink runtime selector: {link}")
    if SHIM.exists() and (SHIM.is_symlink() or not SHIM.is_file() or not managed_shim(SHIM.read_bytes())):
        raise RuntimeError(f"refusing to replace unmanaged CLI path: {SHIM}")
    old = CURRENT_LINK.resolve() if CURRENT_LINK.is_symlink() and CURRENT_LINK.exists() else None
    if old is not None and old != target:
        replace_symlink(PREVIOUS_LINK, old)
    replace_symlink(CURRENT_LINK, target)
    SHIM.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(SHIM, shim_payload(), mode=0o755)
    return old


def shim_payload() -> bytes:
    """Bind the personal AXISVI deployment while keeping the active runtime swappable."""

    executable = CURRENT_LINK / ".venv" / "bin" / "lattice"
    script = f"#!/bin/sh\n# managed-by: lattice-installer-v1\nexport AXISVI_ROOT={shlex.quote(str(AXISVI_ROOT))}\nexport OUTPORT_BIN={shlex.quote(str(required_binary('outport')))}\nexec {shlex.quote(str(executable))} \"$@\"\n"
    return script.encode("utf-8")


def managed_shim(content: bytes) -> bool:
    executable = CURRENT_LINK / ".venv" / "bin" / "lattice"
    legacy = f"#!/bin/sh\nexport AXISVI_ROOT={shlex.quote(str(AXISVI_ROOT))}\nexec {shlex.quote(str(executable))} \"$@\"\n".encode("utf-8")
    if content == legacy or content == shim_payload():
        return True
    try:
        lines = content.decode("utf-8").splitlines()
        outport_assignment = shlex.split(lines[3])
    except (UnicodeDecodeError, ValueError, IndexError):
        return False
    if lines[:3] != ["#!/bin/sh", "# managed-by: lattice-installer-v1", f"export AXISVI_ROOT={shlex.quote(str(AXISVI_ROOT))}"] or lines[4:] != [f"exec {shlex.quote(str(executable))} \"$@\""]:
        return False
    if len(outport_assignment) != 2 or outport_assignment[0] != "export" or not outport_assignment[1].startswith("OUTPORT_BIN="):
        return False
    candidate = Path(outport_assignment[1].removeprefix("OUTPORT_BIN="))
    try:
        return candidate.is_absolute() and candidate.resolve(strict=True) == required_binary("outport").resolve(strict=True)
    except FileNotFoundError:
        return False


def _symlink_target(path: Path) -> Path | None:
    if not path.exists() and not path.is_symlink():
        return None
    if not path.is_symlink():
        raise RuntimeError(f"deployment selector is not a symlink: {path}")
    try:
        return path.resolve(strict=True)
    except FileNotFoundError as error:
        raise RuntimeError(f"deployment selector is dangling: {path}") from error


def _file_state(path: Path) -> FileState | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"deployment-owned path is not a regular file: {path}")
    return FileState(content=path.read_bytes(), mode=path.stat().st_mode & 0o777)


def _job_loaded(label: str) -> bool:
    return subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0


def capture_publication_state() -> PublicationState:
    service_path = LAUNCH_AGENTS / f"{SERVICE_LABEL}.plist"
    backup_path = LAUNCH_AGENTS / f"{BACKUP_LABEL}.plist"
    return PublicationState(
        current_target=_symlink_target(CURRENT_LINK),
        previous_target=_symlink_target(PREVIOUS_LINK),
        shim=_file_state(SHIM),
        service_plist=_file_state(service_path),
        backup_plist=_file_state(backup_path),
        service_loaded=_job_loaded(SERVICE_LABEL),
        backup_loaded=_job_loaded(BACKUP_LABEL),
    )


def _restore_symlink(path: Path, target: Path | None) -> None:
    if target is not None:
        replace_symlink(path, target)
        return
    if path.is_symlink():
        path.unlink()
        sync_directory(path.parent)
    elif path.exists():
        raise RuntimeError(f"refusing to remove non-symlink deployment selector: {path}")


def _restore_file(path: Path, state: FileState | None) -> None:
    if state is not None:
        write_atomic(path, state.content, mode=state.mode)
        return
    if path.is_symlink() or path.exists() and not path.is_file():
        raise RuntimeError(f"refusing to remove unsupported deployment object: {path}")
    path.unlink(missing_ok=True)
    sync_directory(path.parent)


def unload_job(label: str) -> None:
    target = f"gui/{os.getuid()}/{label}"
    if not _job_loaded(label):
        return
    run(["launchctl", "bootout", target])
    deadline = time.monotonic() + LAUNCHD_TRANSITION_TIMEOUT_SECONDS
    while _job_loaded(label):
        if time.monotonic() >= deadline:
            raise RuntimeError(f"launchd did not remove {target} within {LAUNCHD_TRANSITION_TIMEOUT_SECONDS} seconds")
        time.sleep(0.1)


def restore_publication_state(state: PublicationState) -> None:
    """Restore every published selector and supervised job to its exact prior state."""

    service_path = LAUNCH_AGENTS / f"{SERVICE_LABEL}.plist"
    backup_path = LAUNCH_AGENTS / f"{BACKUP_LABEL}.plist"
    failures: list[BaseException] = []

    def attempt(operation) -> None:
        try:
            operation()
        except BaseException as error:
            failures.append(error)

    attempt(lambda: _restore_symlink(CURRENT_LINK, state.current_target))
    attempt(lambda: _restore_symlink(PREVIOUS_LINK, state.previous_target))
    attempt(lambda: _restore_file(SHIM, state.shim))
    attempt(lambda: _restore_file(service_path, state.service_plist))
    attempt(lambda: _restore_file(backup_path, state.backup_plist))
    if state.backup_loaded and state.backup_plist is None:
        failures.append(RuntimeError("prior backup job was loaded without a recoverable plist"))
    else:
        attempt(lambda: reload_job(BACKUP_LABEL, backup_path) if state.backup_loaded else unload_job(BACKUP_LABEL))
    if state.service_loaded and state.service_plist is None:
        failures.append(RuntimeError("prior service job was loaded without a recoverable plist"))
    else:
        attempt(lambda: reload_job(SERVICE_LABEL, service_path) if state.service_loaded else unload_job(SERVICE_LABEL))
    if failures:
        raise BaseExceptionGroup("Lattice publication rollback was incomplete", failures)


def _encode_file_state(state: FileState | None) -> dict | None:
    if state is None:
        return None
    return {"content_b64": base64.b64encode(state.content).decode("ascii"), "mode": state.mode}


def _decode_file_state(value: dict | None) -> FileState | None:
    if value is None:
        return None
    return FileState(content=base64.b64decode(value["content_b64"], validate=True), mode=int(value["mode"]))


def encode_publication_state(state: PublicationState) -> dict:
    return {
        "current_target": None if state.current_target is None else str(state.current_target),
        "previous_target": None if state.previous_target is None else str(state.previous_target),
        "shim": _encode_file_state(state.shim),
        "service_plist": _encode_file_state(state.service_plist),
        "backup_plist": _encode_file_state(state.backup_plist),
        "service_loaded": bool(state.service_loaded),
        "backup_loaded": bool(state.backup_loaded),
    }


def decode_publication_state(value: dict) -> PublicationState:
    return PublicationState(
        current_target=None if value["current_target"] is None else Path(value["current_target"]),
        previous_target=None if value["previous_target"] is None else Path(value["previous_target"]),
        shim=_decode_file_state(value["shim"]),
        service_plist=_decode_file_state(value["service_plist"]),
        backup_plist=_decode_file_state(value["backup_plist"]),
        service_loaded=bool(value["service_loaded"]),
        backup_loaded=bool(value["backup_loaded"]),
    )


def maintenance_record_path() -> Path:
    return DEFAULT_DATA_ROOT / MAINTENANCE_RECORD


def read_maintenance_record() -> dict | None:
    path = maintenance_record_path()
    if not path.exists():
        return None
    record = json.loads(path.read_bytes())
    if not isinstance(record, dict) or record.get("contract_version") != MAINTENANCE_RECORD_CONTRACT:
        raise RuntimeError(f"unrecognized maintenance record at {path}; inspect before continuing")
    if record.get("phase") not in {"stopped", "migrating", "launching", "restoring", "restored"}:
        raise RuntimeError(f"unrecognized maintenance phase at {path}; inspect before continuing")
    return record


def write_maintenance_record(*, phase: str, prior: PublicationState, snapshot: Path | None, target: Path, target_version: str, extra: dict | None = None) -> dict:
    """Operational state for recovery; fsync+rename so it survives SIGKILL/power loss.

    Phases: stopped (writers stopped, database untouched), migrating (candidate
    may have changed the database), launching (candidate jobs bootstrapped and
    may admit new work). Written before the action each phase names.
    """
    record = {
        "contract_version": MAINTENANCE_RECORD_CONTRACT,
        "pid": os.getpid(),
        "argv": sys.argv,
        "started_at": time.strftime("%Y%m%dT%H%M%S"),
        "phase": phase,
        "snapshot": None if snapshot is None else str(snapshot),
        "target": str(target),
        "target_version": target_version,
        "prior": encode_publication_state(prior),
    }
    record.update(extra or {})
    write_atomic(maintenance_record_path(), json.dumps(record, sort_keys=True, indent=1).encode(), mode=0o600)
    return record


def clear_maintenance_record() -> None:
    path = maintenance_record_path()
    path.unlink(missing_ok=True)
    sync_directory(path.parent)


def append_stream_heads(database: Path) -> dict[str, list]:
    """(count, max seq) of the append-only streams: admitted-work evidence.

    Kept for legacy and current recovery records: a cutover sentinel is process
    local and cannot strengthen a fresh recovery operator's durable evidence.
    """
    heads: dict[str, list] = {}
    if not database.is_file():
        return heads
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30)
    try:
        present = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("events", "ledger_changes", "append_integrity", "attempts", "runs", "workflow_commands", "conversation_imports"):
            if table in present:
                count = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                seq = connection.execute(f'SELECT MAX(seq) FROM "{table}"').fetchone()[0] if table in ("events", "ledger_changes", "append_integrity") else None
                heads[table] = [int(count), seq]
    finally:
        connection.close()
    return heads


@contextmanager
def candidate_writer_exclusion():
    """Hold LOCK_EX on the writer lock for the whole restore.

    The migration child inherits a LOCK_SH description taken by the installer
    before spawning (`migrate_database_for_publication`), so an orphan left by
    SIGKILL still holds it from its first instruction until exit. Exclusive
    acquisition therefore proves no migration writer exists, and keeping it held
    through the restore keeps one from appearing.
    """
    path = DEFAULT_DATA_ROOT / MAINTENANCE_WRITER_LOCK
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + MAINTENANCE_WRITER_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"a candidate migration process still holds {path}; stop it before recovery")
                time.sleep(0.2)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def protected_rollback(*, prior: PublicationState, snapshot: Path | None, target: Path, started: bool, baseline_heads: dict | None, record: bool, exclude_writers: bool) -> None:
    """Refuse admitted work under the same admission locks used for cutover.

    Recovery owns the writer exclusion through database restoration. The
    restored phase is durable before restarting prior jobs, so an interrupted
    restart never causes a later operator to overwrite work served by the prior
    runtime. Internal non-recorded calls retain the legacy test seam.
    """
    database = DEFAULT_DATA_ROOT / "lattice.sqlite3"
    with rollback_section():
        with candidate_writer_exclusion() if exclude_writers else nullcontext():
            with upgrade_quiescence() if record else nullcontext():
                if snapshot is not None and started:
                    heads = append_stream_heads(database)
                    if heads != baseline_heads:
                        message = (
                            f"candidate {target.name} admitted work after it started; "
                            "candidate preserved, database not restored; inspect the retained maintenance record"
                        )
                        if record:
                            retained = read_maintenance_record()
                            if retained is not None:
                                retained.update(refusal=message, refused_at=time.strftime("%Y%m%dT%H%M%S"))
                                write_atomic(maintenance_record_path(), json.dumps(retained, sort_keys=True).encode(), mode=0o600)
                        raise MaintenanceRecoveryRefused(message)
                if record or snapshot is not None:
                    unload_job(SERVICE_LABEL)
                    unload_job(BACKUP_LABEL)
            if snapshot is not None:
                if record:
                    write_maintenance_record(phase="restoring", prior=prior, snapshot=snapshot, target=target, target_version=installed_runtime_version(target))
                restore_upgrade_database(snapshot)
            if record:
                write_maintenance_record(phase="restored", prior=prior, snapshot=snapshot, target=target, target_version=installed_runtime_version(target))
        restore_publication_state(prior)
        if prior.current_target is not None and prior.service_loaded:
            verify_live_service(prior.current_target.name, installed_runtime_version(prior.current_target))
        if record:
            clear_maintenance_record()


def recover_maintenance() -> bool:
    """Consume an interrupted maintenance record. Caller holds `maintenance_lock`.

    Returns True when a record was found and resolved. A candidate that answers
    exact health is retained. Otherwise `protected_rollback` applies the same
    admission guard, writer exclusion, and restore as in-process failure.
    """
    record = read_maintenance_record()
    if record is None:
        return False
    phase = record["phase"]
    prior = decode_publication_state(record["prior"])
    snapshot = None if record["snapshot"] is None else Path(record["snapshot"])
    target = Path(record["target"])
    print(f"Recovering interrupted maintenance: phase {phase}, pid {record['pid']}, started {record['started_at']}", flush=True)
    if phase == "launching":
        try:
            verify_live_service(target.name, record["target_version"], timeout_seconds=60)
        except RuntimeError:
            pass
        else:
            print("Interrupted publication is already healthy; keeping the candidate", flush=True)
            clear_maintenance_record()
            return True
    if phase in ("stopped", "restored") and prior.service_loaded and prior.current_target is not None and capture_publication_state() == prior:
        # Loaded flags and bytes alone do not prove a usable daemon; ask it.
        try:
            verify_live_service(prior.current_target.name, installed_runtime_version(prior.current_target), timeout_seconds=60)
        except RuntimeError:
            pass
        else:
            clear_maintenance_record()
            print("Prior publication intact and healthy; nothing to restore", flush=True)
            return True
    protected_rollback(prior=prior, snapshot=snapshot if phase not in ("stopped", "restored") else None, target=target, started=phase == "launching", baseline_heads=record.get("post_migration_heads"), record=True, exclude_writers=phase not in ("stopped", "restored"))
    print("Interrupted maintenance recovered; prior publication restored", flush=True)
    return True


def migrate_database_for_publication(target: Path, snapshot: Path | None) -> None:
    """Finish fallible migration before starting the service-readiness clock.

    Store migration may first take a full crash-consistent snapshot. That work
    has its own retained process/log boundary and cannot fit a small HTTP boot
    deadline. Publication's rollback owns any database changes from this point.
    The installer takes LOCK_SH on the writer lock before spawning and passes the
    same descriptor to the child, so the child holds it from exec until exit even
    if the installer dies first; recovery proves absence with LOCK_EX.
    """
    if snapshot is None:
        return
    print(f"Migrating canonical state with {target.name} before service startup", flush=True)
    descriptor = os.open(DEFAULT_DATA_ROOT / MAINTENANCE_WRITER_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        run([
            str(target / '.venv' / 'bin' / 'python'), '-c',
            'import sys; from pathlib import Path; from lattice import LatticeStore,LatticeConfig; LatticeStore(LatticeConfig(data_root=Path(sys.argv[1]),axisvi_root=Path(sys.argv[2])))',
            str(DEFAULT_DATA_ROOT), str(AXISVI_ROOT),
        ], cwd=target, pass_fds=(descriptor,))
    finally:
        os.close(descriptor)
    print('Canonical migration completed; starting service readiness', flush=True)


def publish_runtime(target: Path, *, snapshot: Path | None = None, prior: PublicationState | None = None, record: bool = False) -> None:
    prior = prior or capture_publication_state()
    started = False
    baseline_heads: dict | None = None
    try:
        version = installed_runtime_version(target)
        if record:
            write_maintenance_record(phase="migrating", prior=prior, snapshot=snapshot, target=target, target_version=version)
        migrate_database_for_publication(target, snapshot)
        if snapshot is not None:
            baseline_heads = append_stream_heads(DEFAULT_DATA_ROOT / "lattice.sqlite3")
        if record:
            write_maintenance_record(phase="launching", prior=prior, snapshot=snapshot, target=target, target_version=version, extra={"post_migration_heads": baseline_heads})
        install_links(target)
        started = True
        install_launchd(target)
        verify_live_service(target.name, version)
    except BaseException as publication_error:
        if record:
            raise
        try:
            protected_rollback(prior=prior, snapshot=snapshot, target=target, started=started, baseline_heads=baseline_heads, record=False, exclude_writers=False)
        except BaseException as rollback_error:
            raise BaseExceptionGroup("Lattice publication and automatic rollback both failed", [publication_error, rollback_error])
        raise publication_error
    if record:
        clear_maintenance_record()


def plist_payload(label: str, *, backup: bool, runtime_id: str | None = None) -> dict:
    executable = str(CURRENT_LINK / ".venv" / "bin" / "lattice")
    selected_runtime = runtime_id or (CURRENT_LINK.resolve().name if CURRENT_LINK.is_symlink() and CURRENT_LINK.exists() else "unpublished")
    payload = {
        "Label": label,
        "ProgramArguments": [executable, "backup", "--reason", "scheduled-daily"] if backup else [executable, "serve"],
        "WorkingDirectory": str(AXISVI_ROOT),
        "EnvironmentVariables": {"AXISVI_ROOT": str(AXISVI_ROOT), "LATTICE_DATA_DIR": str(DEFAULT_DATA_ROOT), "LATTICE_RUNTIME_ID": selected_runtime, "OUTPORT_BIN": str(required_binary("outport")), "PATH": runtime_path()},
        # launchd's Background class throttles work that was not directly
        # requested by the user. The API daemon is the synchronous response
        # path for the cockpit, while the scheduled snapshot is true
        # background work.
        "ProcessType": "Background" if backup else "Interactive",
        "ThrottleInterval": 10,
        "StandardOutPath": str(LOG_ROOT / ("backup.stdout.log" if backup else "service.stdout.log")),
        "StandardErrorPath": str(LOG_ROOT / ("backup.stderr.log" if backup else "service.stderr.log")),
    }
    if backup:
        payload["ProgramArguments"] = [str(CURRENT_LINK / ".venv" / "bin" / "python"), str(AXISVI_ROOT / "axis" / "tools" / "storage" / "lattice_backup.py"), "--data-root", str(DEFAULT_DATA_ROOT)]
        payload["StartCalendarInterval"] = {"Hour": 3, "Minute": 0}
    else:
        payload["RunAtLoad"] = True
        payload["KeepAlive"] = True
    return payload


def reload_job(label: str, path: Path) -> None:
    domain = f"gui/{os.getuid()}"
    service_target = f"{domain}/{label}"
    print_result = subprocess.run(["launchctl", "print", service_target], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if print_result.returncode == 0:
        run(["launchctl", "bootout", service_target])
        deadline = time.monotonic() + LAUNCHD_TRANSITION_TIMEOUT_SECONDS
        while subprocess.run(["launchctl", "print", service_target], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"launchd did not remove {service_target} within {LAUNCHD_TRANSITION_TIMEOUT_SECONDS} seconds")
            time.sleep(0.1)
    deadline = time.monotonic() + LAUNCHD_TRANSITION_TIMEOUT_SECONDS
    while True:
        result = subprocess.run(["launchctl", "bootstrap", domain, str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode == 0:
            return
        if result.returncode != 5 or time.monotonic() >= deadline:
            detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"launchctl could not bootstrap {label}: {detail or f'exit {result.returncode}'}")
        time.sleep(0.1)


def install_launchd(target: Path) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("the current supervised Lattice deployment supports macOS launchd")
    LOG_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    service_path = LAUNCH_AGENTS / f"{SERVICE_LABEL}.plist"
    backup_path = LAUNCH_AGENTS / f"{BACKUP_LABEL}.plist"
    write_atomic(service_path, plistlib.dumps(plist_payload(SERVICE_LABEL, backup=False, runtime_id=target.name), sort_keys=True), mode=0o644)
    write_atomic(backup_path, plistlib.dumps(plist_payload(BACKUP_LABEL, backup=True, runtime_id=target.name), sort_keys=True), mode=0o644)
    reload_job(BACKUP_LABEL, backup_path)
    reload_job(SERVICE_LABEL, service_path)


def verify_live_service(expected_runtime_id: str, expected_version: str, timeout_seconds: int = 300) -> None:
    import_source_modules()
    from lattice.outport import resolve_lattice_service

    service = resolve_lattice_service(AXISVI_ROOT)
    deadline = time.monotonic() + timeout_seconds
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{service.port}/api/health", timeout=2) as response:
                payload = json.loads(response.read())
            # Overall health can be degraded by external workflow definitions
            # that neither this upgrade nor rollback can repair. Use the core
            # readiness contract, including older payloads without attribution.
            core_ready = payload.get("conversation_ready", payload.get("status") == "ok") is True and payload.get("durable_run_ready", True) is True
            if payload.get("status") in ("ok", "degraded") and core_ready and payload.get("port") == service.port and Path(payload.get("data_root", "")).resolve() == DEFAULT_DATA_ROOT.resolve() and payload.get("runtime_id") == expected_runtime_id and payload.get("version") == expected_version and not payload.get("startup_recovery_failures"):
                return
            last_error = RuntimeError(f"health identity mismatch: {payload}")
        except Exception as error:
            last_error = error
        time.sleep(0.5)
    raise RuntimeError(f"installed Lattice did not become healthy: {last_error}")


def perform_upgrade(target: Path, *, rollback_snapshot: Path | None, verify_candidate: Callable[[Path, Path | None], None], before_cutover: Callable[[], None]) -> None:
    """Qualify online, then compare bounded identity under admission exclusion.

    Full snapshot checks, semantic equality and candidate rehearsal run while
    service remains available. The same read-only sentinel spans those checks
    through the final writer reservation, idle check and publication-control
    comparison. Migration and existing candidate-write recovery remain separate.
    """
    prior = capture_publication_state()
    print("Capturing pre-upgrade rollback snapshot while the service runs", flush=True)
    snapshot = reuse_rollback_snapshot(rollback_snapshot) if rollback_snapshot else backup_before_upgrade()
    candidate_manifest_sha256 = exact_sha256(target / "runtime.json")
    with qualified_cutover_identity(target, snapshot) as identity:
        print("Verifying candidate compatibility with the rollback snapshot", flush=True)
        verify_candidate(target, snapshot)
        # Legacy snapshots have no source token: bind through the full logical
        # comparison here, before taking admission locks or stopping service.
        verify_rollback_snapshot_current(snapshot)
        if identity is not None:
            identity.bind_snapshot(snapshot)
            identity.assert_unchanged()
            identity.qualified = True
            identity.verify()
        before_cutover()
        try:
            with upgrade_quiescence():
                print("Comparing bounded cutover identity", flush=True)
                if identity is not None:
                    identity.verify()
                elif (DEFAULT_DATA_ROOT / "lattice.sqlite3").exists():
                    raise RollbackSnapshotStale("database appeared during first-install qualification")
                if exact_sha256(target / "runtime.json") != candidate_manifest_sha256:
                    raise RollbackSnapshotStale("candidate runtime manifest changed; service was not stopped")
                if capture_publication_state() != prior:
                    raise RollbackSnapshotStale("publication controls changed; service was not stopped")
                write_maintenance_record(phase="stopped", prior=prior, snapshot=snapshot, target=target, target_version=installed_runtime_version(target))
                print("Stopping service for cutover", flush=True)
                unload_job(SERVICE_LABEL)
                unload_job(BACKUP_LABEL)
            # The cutover sentinel must end before expected migration/startup DDL.
            if identity is not None:
                identity.close()
            publish_runtime(target, snapshot=snapshot, prior=prior, record=True)
        except BaseException as upgrade_error:
            if identity is not None:
                identity.close()
            # The record, not a flag set after writing, is authoritative.
            if read_maintenance_record() is not None:
                try:
                    with rollback_section():
                        recover_maintenance()
                except BaseException as rollback_error:
                    raise BaseExceptionGroup("Lattice upgrade and recovery both failed", [upgrade_error, rollback_error])
            raise


def default_candidate_verification(target: Path, snapshot: Path | None) -> None:
    verify_candidate_workflow_packs(target)
    verify_candidate_backup(target, snapshot)
    qualify_candidate_migration(target, snapshot)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-data-root", type=Path, default=DEFAULT_LEGACY_ROOT)
    parser.add_argument("--skip-tests", action="store_true", help="reserved for recovery; normal upgrades must run assurance")
    parser.add_argument("--rollback-snapshot", type=Path, help="reuse a verified snapshot only if its database state still matches the stopped service")
    parser.add_argument("--recover", action="store_true", help="only resolve an interrupted maintenance record; do not build or publish")
    args = parser.parse_args()
    with maintenance_lock(), cancellation_scope():
        # Interrupted state is consumed explicitly before any new attempt captures
        # a "prior" publication; a retry never records an outage as the baseline.
        recovered = recover_maintenance()
        if args.recover:
            print("Recovered interrupted maintenance" if recovered else "No interrupted maintenance recorded", flush=True)
            return
        lattice_revision = git_revision(SOURCE_ROOT) if args.skip_tests else verify_source()
        migrate_legacy_data(args.legacy_data_root.expanduser().resolve())
        target = install_runtime(lattice_revision)

        def revision_unchanged() -> None:
            if git_revision(SOURCE_ROOT) != lattice_revision:
                raise RuntimeError("source revision changed before runtime publication")

        perform_upgrade(target, rollback_snapshot=args.rollback_snapshot, verify_candidate=default_candidate_verification, before_cutover=revision_unchanged)
    print(f"Installed Lattice runtime {target.name}")
    print(f"CLI: {SHIM}")
    print(f"State: {DEFAULT_DATA_ROOT}")


if __name__ == "__main__":
    reexec_in_source_environment()
    main()
