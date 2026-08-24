#!/usr/bin/env python3
"""Install a verified, rollback-capable Lattice runtime and launchd service."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]
AXISVI_ROOT = SOURCE_ROOT.parents[2]
MODEL_RUNNER_ROOT = AXISVI_ROOT / "axis" / "tools" / "model-runner"
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


def run(arguments: list[str], *, cwd: Path = SOURCE_ROOT, env: dict[str, str] | None = None, capture: bool = False) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(arguments, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE if capture else None, check=False)
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


def git_revision(root: Path, *, pathspec: str | None = None) -> str:
    status_command = ["git", "status", "--porcelain=v1"]
    if pathspec is not None:
        status_command.extend(["--", pathspec])
    status = run(status_command, cwd=root, capture=True).stdout.decode().strip()
    if status:
        raise RuntimeError(f"refusing to install a non-immutable dirty source tree at {root}:\n{status}")
    return run(["git", "rev-parse", "HEAD"], cwd=root, capture=True).stdout.decode().strip()


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
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def replace_symlink(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = link.parent / f".{link.name}.{uuid.uuid4().hex}.pending"
    os.symlink(target, temporary)
    os.replace(temporary, link)


def source_python() -> Path:
    candidate = SOURCE_ROOT / ".venv" / "bin" / "python"
    if not candidate.is_file():
        raise RuntimeError(f"source verification environment is missing: {candidate}")
    return candidate


def required_binary(name: str) -> Path:
    candidate = shutil.which(name)
    if candidate is None:
        raise RuntimeError(f"required deployment executable is unavailable: {name}")
    return Path(candidate).resolve(strict=True)


def runtime_path() -> str:
    directories: list[str] = []
    for binary in (required_binary("outport"), required_binary("claude"), required_binary("codex")):
        parent = str(binary.parent)
        if parent not in directories:
            directories.append(parent)
    for directory in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        if directory not in directories:
            directories.append(directory)
    return os.pathsep.join(directories)


def verify_source() -> None:
    run([str(source_python()), "-m", "pytest", "-q"])
    run(["npm", "run", "build"], cwd=SOURCE_ROOT / "web")
    run(["npm", "test", "--", "--run"], cwd=SOURCE_ROOT / "web")
    if run(["git", "status", "--porcelain=v1"], capture=True).stdout.decode().strip():
        raise RuntimeError("verification changed the source tree; commit the exact built artifact before installation")


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


def verify_migration_baseline(root: Path, baseline: dict) -> None:
    connection = sqlite3.connect(f"file:{(root / 'lattice.sqlite3').resolve(strict=True)}?mode=ro", uri=True)
    try:
        for table, contract in baseline["tables"].items():
            selected = ",".join(f'"{name}"' for name in contract["columns"])
            order = ",".join(f'"{name}"' for name in contract["primary"])
            actual = [tuple(row) for row in connection.execute(f'SELECT {selected} FROM "{table}" ORDER BY {order}')]
            if actual != contract["rows"]:
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
        os.replace(staged_root, DEFAULT_DATA_ROOT)
    finally:
        shutil.rmtree(temporary_backup)
        shutil.rmtree(migration_wrapper)


def backup_before_upgrade() -> None:
    current_cli = CURRENT_LINK / ".venv" / "bin" / "lattice"
    if current_cli.is_file():
        run([str(current_cli), "backup", "--reason", "pre-runtime-upgrade"])
        return
    import_source_modules()
    from lattice.diagnostics import assess_backup, assess_data_root
    from lattice.snapshot import create_snapshot

    if not (DEFAULT_DATA_ROOT / "lattice.sqlite3").is_file():
        return
    report = assess_data_root(DEFAULT_DATA_ROOT)
    snapshot = create_snapshot(DEFAULT_DATA_ROOT, reason="pre-first-installed-runtime", source_revision=report["state_digest"], schema_version=report["schema_version"], state_digest=report["state_digest"])
    assess_backup(snapshot)


def probe_runtime(target: Path) -> dict[str, str]:
    result = run([
        str(target / ".venv" / "bin" / "python"),
        "-c",
        "import json,lattice,axis_model_runner; print(json.dumps({'lattice':lattice.__file__,'model_runner':axis_model_runner.__file__}))",
    ], cwd=target, capture=True)
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or set(value) != {"lattice", "model_runner"} or any(not isinstance(location, str) for location in value.values()):
        raise RuntimeError(f"installed runtime probe returned invalid module locations: {value}")
    return value


def install_runtime(lattice_revision: str, runner_revision: str) -> Path:
    version = "0.1.0"
    runtime_id = f"{version}-{lattice_revision[:12]}-{runner_revision[:12]}"
    target = VERSIONS_ROOT / runtime_id
    marker = target / ".pending"
    if target.exists():
        if marker.exists() or not (target / "runtime.json").is_file() or not (target / ".venv" / "bin" / "lattice").is_file():
            raise RuntimeError(f"runtime target is incomplete: {target}")
        return target
    VERSIONS_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.mkdir(mode=0o700)
    marker.write_text("installation is not published\n", encoding="utf-8")
    os.chmod(marker, 0o600)
    try:
        run(["uv", "venv", str(target / ".venv"), "--python", str(source_python())])
        environment = dict(os.environ)
        environment["VIRTUAL_ENV"] = str(target / ".venv")
        run(["uv", "sync", "--project", str(SOURCE_ROOT), "--active", "--frozen", "--no-dev", "--no-editable"], env=environment)
        locations = probe_runtime(target)
        if any(str(SOURCE_ROOT) in location or str(MODEL_RUNNER_ROOT) in location for location in locations.values()):
            raise RuntimeError(f"installed runtime still imports a development checkout: {locations}")
        run([str(target / ".venv" / "bin" / "lattice"), "--help"])
        frozen = run(["uv", "pip", "freeze", "--python", str(target / ".venv" / "bin" / "python")], capture=True).stdout.decode().splitlines()
        manifest = {
            "generated_object": {
                "contract_version": 1,
                "role": "canonical",
                "kind": "lattice-installed-runtime",
                "schema": {"name": "lattice-installed-runtime", "version": 1},
                "artifact_id": f"artifact:{uuid.uuid4()}",
                "generated_at": time.strftime("%Y%m%dT%H%M%S"),
                "producer": {"name": "lattice-installer", "version": "1.0.0"},
                "subject": f"lattice:installed-runtime:{runtime_id}",
                "sources": [
                    {"ref": f"git:{SOURCE_ROOT}", "revision": lattice_revision},
                    {"ref": f"git:{MODEL_RUNNER_ROOT}", "revision": runner_revision},
                    {"ref": f"file:{SOURCE_ROOT / 'uv.lock'}", "sha256": exact_sha256(SOURCE_ROOT / "uv.lock")},
                ],
            },
            "contract_version": "LatticeInstalledRuntime/1.0",
            "runtime_id": runtime_id,
            "python": str((target / ".venv" / "bin" / "python").resolve()),
            "packages": frozen,
            "module_locations": locations,
        }
        import_source_modules()
        from lattice.canonical import canonical_bytes
        write_atomic(target / "runtime.json", canonical_bytes(manifest), mode=0o600)
        marker.unlink()
        return target
    except BaseException:
        shutil.rmtree(target)
        raise


def install_links(target: Path) -> Path | None:
    old = CURRENT_LINK.resolve() if CURRENT_LINK.is_symlink() and CURRENT_LINK.exists() else None
    if old is not None and old != target:
        replace_symlink(PREVIOUS_LINK, old)
    replace_symlink(CURRENT_LINK, target)
    SHIM.parent.mkdir(parents=True, exist_ok=True)
    if SHIM.exists() and not SHIM.is_symlink() and not managed_shim(SHIM.read_bytes()):
        raise RuntimeError(f"refusing to replace non-symlink CLI path: {SHIM}")
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
    return content in {legacy, shim_payload()}


def plist_payload(label: str, *, backup: bool) -> dict:
    executable = str(CURRENT_LINK / ".venv" / "bin" / "lattice")
    payload = {
        "Label": label,
        "ProgramArguments": [executable, "backup", "--reason", "scheduled-daily"] if backup else [executable, "serve"],
        "WorkingDirectory": str(AXISVI_ROOT),
        "EnvironmentVariables": {"AXISVI_ROOT": str(AXISVI_ROOT), "LATTICE_DATA_DIR": str(DEFAULT_DATA_ROOT), "OUTPORT_BIN": str(required_binary("outport")), "PATH": runtime_path()},
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "StandardOutPath": str(LOG_ROOT / ("backup.stdout.log" if backup else "service.stdout.log")),
        "StandardErrorPath": str(LOG_ROOT / ("backup.stderr.log" if backup else "service.stderr.log")),
    }
    if backup:
        payload["StartCalendarInterval"] = {"Hour": 3, "Minute": 0}
    else:
        payload["RunAtLoad"] = True
        payload["KeepAlive"] = True
    return payload


def reload_job(label: str, path: Path) -> None:
    domain = f"gui/{os.getuid()}"
    print_result = subprocess.run(["launchctl", "print", f"{domain}/{label}"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if print_result.returncode == 0:
        run(["launchctl", "bootout", f"{domain}/{label}"])
    run(["launchctl", "bootstrap", domain, str(path)])


def install_launchd() -> None:
    if sys.platform != "darwin":
        raise RuntimeError("the current supervised Lattice deployment supports macOS launchd")
    LOG_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    service_path = LAUNCH_AGENTS / f"{SERVICE_LABEL}.plist"
    backup_path = LAUNCH_AGENTS / f"{BACKUP_LABEL}.plist"
    write_atomic(service_path, plistlib.dumps(plist_payload(SERVICE_LABEL, backup=False), sort_keys=True), mode=0o644)
    write_atomic(backup_path, plistlib.dumps(plist_payload(BACKUP_LABEL, backup=True), sort_keys=True), mode=0o644)
    reload_job(BACKUP_LABEL, backup_path)
    reload_job(SERVICE_LABEL, service_path)


def verify_live_service(timeout_seconds: int = 30) -> None:
    import_source_modules()
    from lattice.outport import resolve_lattice_service

    service = resolve_lattice_service(AXISVI_ROOT)
    deadline = time.monotonic() + timeout_seconds
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{service.port}/api/health", timeout=2) as response:
                payload = json.loads(response.read())
            if payload.get("status") == "ok" and payload.get("port") == service.port and Path(payload.get("data_root", "")).resolve() == DEFAULT_DATA_ROOT.resolve():
                return
            last_error = RuntimeError(f"health identity mismatch: {payload}")
        except BaseException as error:
            last_error = error
        time.sleep(0.5)
    raise RuntimeError(f"installed Lattice did not become healthy: {last_error}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-data-root", type=Path, default=DEFAULT_LEGACY_ROOT)
    parser.add_argument("--skip-tests", action="store_true", help="reserved for recovery; normal upgrades must run assurance")
    args = parser.parse_args()
    if not args.skip_tests:
        verify_source()
    lattice_revision = git_revision(SOURCE_ROOT)
    runner_revision = git_revision(AXISVI_ROOT, pathspec="axis/tools/model-runner")
    migrate_legacy_data(args.legacy_data_root.expanduser().resolve())
    backup_before_upgrade()
    target = install_runtime(lattice_revision, runner_revision)
    old = install_links(target)
    try:
        install_launchd()
        verify_live_service()
    except BaseException:
        if old is not None:
            replace_symlink(CURRENT_LINK, old)
            install_launchd()
        raise
    print(f"Installed Lattice runtime {target.name}")
    print(f"CLI: {SHIM}")
    print(f"State: {DEFAULT_DATA_ROOT}")


if __name__ == "__main__":
    main()
