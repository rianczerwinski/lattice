#!/usr/bin/env python3
"""Install a verified, rollback-capable Lattice runtime and launchd service."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
AXISVI_ROOT = SOURCE_ROOT.parents[2]
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
VERSION_PATTERN = re.compile(rb'^__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"$', re.MULTILINE)


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
    match = re.fullmatch(r"([0-9]+\.[0-9]+\.[0-9]+)-[0-9a-f]{12}", target.name)
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
    status_command = ["git", "status", "--porcelain=v1"]
    if pathspec is not None:
        status_command.extend(["--", pathspec])
    status = run(status_command, cwd=root, capture=True).stdout.decode().strip()
    if status:
        raise RuntimeError(f"refusing to install a non-immutable dirty source tree at {root}:\n{status}")
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
    directories: list[str] = []
    entrypoints = [required_binary("outport")]
    for name in ("claude", "codex", "agy", "grok"):
        candidate = shutil.which(name)
        if candidate is not None:
            entrypoints.append(Path(candidate).absolute())
    for binary in entrypoints:
        parent = str(binary.parent)
        if parent not in directories:
            directories.append(parent)
    for directory in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        if directory not in directories:
            directories.append(directory)
    return os.pathsep.join(directories)


def verify_source() -> None:
    run([str(source_python()), "-m", "pytest", "-q"])
    run(["npm", "run", "check"], cwd=SOURCE_ROOT / "web")
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
        sync_tree(staged_root)
        os.replace(staged_root, DEFAULT_DATA_ROOT)
        sync_directory(DEFAULT_DATA_ROOT.parent)
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


def _validate_provider_attestations(value: object) -> list[dict]:
    from lattice.runtime_attestation import validate_provider_attestations

    try:
        return validate_provider_attestations(value)
    except ValueError as error:
        raise RuntimeError(str(error)) from error


def verify_provider_attestation_freshness(value: object) -> None:
    from lattice.runtime_attestation import verify_provider_attestation_freshness as verify

    try:
        verify(value)
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
    _validate_provider_attestations(manifest.get("provider_attestations"))
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
    verify_provider_attestation_freshness(manifest["provider_attestations"])


def install_runtime(lattice_revision: str) -> Path:
    runtime_id = f"{release_version()}-{lattice_revision[:12]}"
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
        manifest = build_runtime_manifest(target=target, runtime_id=runtime_id, lattice_revision=lattice_revision, packages=frozen, module_locations=locations, source_trees=source_trees, provider_attestations=provider_runtime_attestations(probe_versions=True))
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


def publish_runtime(target: Path) -> None:
    prior = capture_publication_state()
    try:
        install_links(target)
        install_launchd(target)
        verify_live_service(target.name, installed_runtime_version(target))
    except BaseException as publication_error:
        try:
            restore_publication_state(prior)
            if prior.current_target is not None and prior.service_loaded:
                verify_live_service(prior.current_target.name, installed_runtime_version(prior.current_target))
        except BaseException as rollback_error:
            raise BaseExceptionGroup("Lattice publication and automatic rollback both failed", [publication_error, rollback_error])
        raise publication_error


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


def verify_live_service(expected_runtime_id: str, expected_version: str, timeout_seconds: int = 90) -> None:
    import_source_modules()
    from lattice.outport import resolve_lattice_service

    service = resolve_lattice_service(AXISVI_ROOT)
    deadline = time.monotonic() + timeout_seconds
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{service.port}/api/health", timeout=2) as response:
                payload = json.loads(response.read())
            if payload.get("status") == "ok" and payload.get("port") == service.port and Path(payload.get("data_root", "")).resolve() == DEFAULT_DATA_ROOT.resolve() and payload.get("runtime_id") == expected_runtime_id and payload.get("version") == expected_version and not payload.get("startup_recovery_failures") and not payload.get("workflow_pack_failures"):
                return
            last_error = RuntimeError(f"health identity mismatch: {payload}")
        except Exception as error:
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
    migrate_legacy_data(args.legacy_data_root.expanduser().resolve())
    backup_before_upgrade()
    target = install_runtime(lattice_revision)
    publish_runtime(target)
    print(f"Installed Lattice runtime {target.name}")
    print(f"CLI: {SHIM}")
    print(f"State: {DEFAULT_DATA_ROOT}")


if __name__ == "__main__":
    reexec_in_source_environment()
    main()
