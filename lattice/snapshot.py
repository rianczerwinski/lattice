"""SQLite-consistent, inventory-bound Lattice snapshots and off-path restores."""

from __future__ import annotations

import ctypes
import errno
import gzip
import hashlib
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .canonical import canonical_bytes, generated_object, sha256_bytes, stamp, strict_json_loads, validate_generated_object_envelope
from .durability import sync_directory as _sync_directory
from .durability import sync_file as _sync_file
from .durability import sync_tree_directories as _sync_tree_directories
from .run_journal import DURABLE_RUNS_DIRECTORY, journal_capture_guard


SNAPSHOT_MANIFEST = "snapshot.json"
SNAPSHOT_INVENTORY = "inventory.jsonl"
SNAPSHOT_DATABASE = "lattice.sqlite3"
COMPRESSED_SNAPSHOT_DATABASE = "lattice.sqlite3.gz"
SNAPSHOT_CONTENT_ROOTS = ("acceptance", "blobs", "model-runs", DURABLE_RUNS_DIRECTORY)
SNAPSHOT_CONTENT_FILES = (SNAPSHOT_DATABASE, "operator-token")


def _sha256_file(path: Path) -> tuple[str, int]:
    before = path.stat()
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            byte_count += len(chunk)
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or byte_count != after.st_size:
        raise OSError(f"snapshot source changed while hashing: {path}")
    return digest.hexdigest(), byte_count


def _try_clone_regular(source: Path, destination: Path) -> bool:
    """Use APFS copy-on-write when available, with ordinary copy as fallback."""

    if sys.platform != "darwin":
        return False
    clonefile = ctypes.CDLL(None, use_errno=True).clonefile
    clonefile.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32)
    clonefile.restype = ctypes.c_int
    if clonefile(os.fsencode(source), os.fsencode(destination), 0) == 0:
        return True
    error = ctypes.get_errno()
    if error in {errno.EACCES, errno.EPERM, errno.EXDEV, errno.EINVAL, errno.ENOTSUP, errno.ENOSYS}:
        return False
    raise OSError(error, os.strerror(error), destination)


def _copy_regular(source: Path, destination: Path) -> None:
    if source.is_symlink():
        raise OSError(f"snapshot source is a symlink: {source}")
    source = source.resolve(strict=True)
    if not source.is_file():
        raise OSError(f"snapshot source is not a regular non-symlink file: {source}")
    before = source.stat()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    cloned = _try_clone_regular(source, destination)
    if not cloned:
        with source.open("rb") as input_handle, destination.open("xb") as output_handle:
            shutil.copyfileobj(input_handle, output_handle, length=1024 * 1024)
            output_handle.flush()
            _sync_file(output_handle.fileno())
    os.chmod(destination, 0o600)
    if cloned:
        with destination.open("rb") as output_handle:
            _sync_file(output_handle.fileno())
    after = source.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise OSError(f"snapshot source changed while copying: {source}")
    source_sha, source_size = _sha256_file(source)
    destination_sha, destination_size = _sha256_file(destination)
    if (source_sha, source_size) != (destination_sha, destination_size):
        raise OSError(f"snapshot copy changed bytes: {source}")


def copy_exact_tree(source: Path, destination: Path) -> None:
    if source.is_symlink():
        raise OSError(f"snapshot source directory is a symlink: {source}")
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    for child in sorted(source.rglob("*")):
        if child.is_symlink():
            raise OSError(f"snapshot source contains a symlink: {child}")
        relative = child.relative_to(source)
        target = destination / relative
        if child.is_dir():
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
        elif child.is_file():
            _copy_regular(child, target)
        else:
            raise OSError(f"snapshot source contains an unsupported filesystem object: {child}")


def _snapshot_files(root: Path) -> list[Path]:
    metadata = {SNAPSHOT_MANIFEST, SNAPSHOT_INVENTORY}
    return sorted(path for path in root.rglob("*") if path.is_file() and path.relative_to(root).as_posix() not in metadata)


def _inventory(root: Path) -> list[dict[str, Any]]:
    items = []
    for path in _snapshot_files(root):
        if path.is_symlink():
            raise OSError(f"snapshot contains a symlink: {path}")
        digest, byte_count = _sha256_file(path)
        items.append({"path": path.relative_to(root).as_posix(), "sha256": digest, "byte_count": byte_count, "mode": stat.S_IMODE(path.stat().st_mode)})
    return items


def _write_inventory(root: Path, items: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep large file inventories canonical without one oversized JSON tree."""

    path = root / SNAPSHOT_INVENTORY
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("xb") as handle:
        os.chmod(path, 0o600)
        for item in items:
            row = canonical_bytes(item) + b"\n"
            handle.write(row)
            digest.update(row)
            byte_count += len(row)
        handle.flush()
        _sync_file(handle.fileno())
    return {"path": SNAPSHOT_INVENTORY, "encoding": "canonical-jsonl-v1", "sha256": digest.hexdigest(), "byte_count": byte_count, "item_count": len(items)}


def _read_inventory(snapshot_root: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    value = manifest.get("inventory")
    if manifest.get("contract_version") != "LatticeBackup/4.0":
        if not isinstance(value, list) or not value:
            raise OSError("Lattice snapshot inventory is missing")
        return value
    required = {"path", "encoding", "sha256", "byte_count", "item_count"}
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("path") != SNAPSHOT_INVENTORY
        or value.get("encoding") != "canonical-jsonl-v1"
        or not isinstance(value.get("sha256"), str)
        or len(value["sha256"]) != 64
        or not isinstance(value.get("byte_count"), int)
        or value["byte_count"] <= 0
        or not isinstance(value.get("item_count"), int)
        or value["item_count"] <= 0
    ):
        raise OSError("Lattice snapshot inventory descriptor is malformed")
    path = snapshot_root / SNAPSHOT_INVENTORY
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise OSError("Lattice snapshot inventory stream is missing") from error
    if snapshot_root not in resolved.parents or path.is_symlink() or not path.is_file():
        raise OSError("Lattice snapshot inventory stream escaped its root")
    digest, byte_count = _sha256_file(path)
    if digest != value["sha256"] or byte_count != value["byte_count"]:
        raise OSError("Lattice snapshot inventory stream disagrees with its descriptor")
    items = []
    with path.open("rb") as handle:
        for raw in handle:
            if not raw.endswith(b"\n"):
                raise OSError("Lattice snapshot inventory stream has an unterminated row")
            try:
                item = strict_json_loads(raw[:-1])
            except ValueError as error:
                raise OSError("Lattice snapshot inventory stream contains invalid canonical JSON") from error
            if canonical_bytes(item) + b"\n" != raw:
                raise OSError("Lattice snapshot inventory stream contains a noncanonical row")
            items.append(item)
    if len(items) != value["item_count"]:
        raise OSError("Lattice snapshot inventory stream item count differs")
    return items


def _active_work(connection: sqlite3.Connection) -> dict[str, int]:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    checks = {
        "attempts": ("attempts", "SELECT COUNT(*) FROM attempts WHERE state IN ('leased','running')"),
        "runs": ("runs", "SELECT COUNT(*) FROM runs WHERE state IN ('running','waiting','stopping')"),
        "commands": ("workflow_commands", "SELECT COUNT(*) FROM workflow_commands WHERE state='running'"),
        "imports": ("conversation_imports", "SELECT COUNT(*) FROM conversation_imports WHERE state='running'"),
        "leases": ("leases", "SELECT COUNT(*) FROM leases WHERE state='active'"),
    }
    return {name: int(connection.execute(query).fetchone()[0]) if table in tables else 0 for name, (table, query) in checks.items()}


@contextmanager
def _snapshot_guard(data_root: Path, source: Path, *, require_idle: bool) -> Iterator[dict[str, int]]:
    """Hold SQLite's write reservation across every byte copied into a snapshot."""

    with journal_capture_guard(data_root):
        guard = sqlite3.connect(source, timeout=30, isolation_level=None)
        guard.execute("PRAGMA busy_timeout=30000")
        guard.execute("BEGIN IMMEDIATE")
        try:
            active = _active_work(guard)
            if require_idle and any(active.values()):
                raise RuntimeError(f"cannot snapshot while durable work is active: {active}")
            yield active
        finally:
            guard.rollback()
            guard.close()


def _backup_database(source: Path, destination: Path) -> None:
    """Back up through a reader separate from the write-reserving guard."""

    reader = sqlite3.connect(source, timeout=30)
    target = sqlite3.connect(destination)
    try:
        reader.backup(target)
        target.commit()
    finally:
        target.close()
        reader.close()
    os.chmod(destination, 0o600)
    check = sqlite3.connect(f"file:{destination}?mode=ro", uri=True)
    try:
        result = check.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise OSError(f"snapshot SQLite integrity check failed: {result}")
    finally:
        check.close()
    with destination.open("rb") as handle:
        _sync_file(handle.fileno())


def _compress_database(source: Path, destination: Path) -> dict[str, Any]:
    """Compress a consistent SQLite copy while retaining its logical identity."""

    digest = hashlib.sha256()
    byte_count = 0
    with source.open("rb") as input_handle, destination.open("xb") as raw_output:
        os.chmod(destination, 0o600)
        with gzip.GzipFile(filename="", mode="wb", compresslevel=1, fileobj=raw_output, mtime=0) as output:
            while chunk := input_handle.read(1024 * 1024):
                digest.update(chunk)
                byte_count += len(chunk)
                output.write(chunk)
        raw_output.flush()
        _sync_file(raw_output.fileno())
    return {
        "path": COMPRESSED_SNAPSHOT_DATABASE,
        "encoding": "gzip",
        "sha256": digest.hexdigest(),
        "byte_count": byte_count,
    }


def _decompress_database(source: Path, destination: Path, descriptor: dict[str, Any]) -> None:
    expected_sha = descriptor.get("sha256")
    expected_bytes = descriptor.get("byte_count")
    if not isinstance(expected_sha, str) or len(expected_sha) != 64 or not isinstance(expected_bytes, int) or expected_bytes <= 0:
        raise OSError("compressed snapshot database identity is malformed")
    digest = hashlib.sha256()
    byte_count = 0
    with gzip.open(source, "rb") as input_handle, destination.open("xb") as output_handle:
        os.chmod(destination, 0o600)
        while chunk := input_handle.read(1024 * 1024):
            byte_count += len(chunk)
            if byte_count > expected_bytes:
                raise OSError("compressed snapshot database exceeds its declared size")
            digest.update(chunk)
            output_handle.write(chunk)
        output_handle.flush()
        _sync_file(output_handle.fileno())
    if byte_count != expected_bytes or digest.hexdigest() != expected_sha:
        raise OSError("compressed snapshot database disagrees with its logical identity")


@contextmanager
def materialized_snapshot_database(snapshot_root: Path, manifest: dict[str, Any]) -> Iterator[Path]:
    """Yield a readable SQLite file for legacy and compressed snapshots."""

    if manifest.get("contract_version") not in {"LatticeBackup/3.0", "LatticeBackup/4.0"}:
        yield snapshot_root / SNAPSHOT_DATABASE
        return
    descriptor = manifest.get("database")
    if not isinstance(descriptor, dict) or set(descriptor) != {"path", "encoding", "sha256", "byte_count"} or descriptor.get("path") != COMPRESSED_SNAPSHOT_DATABASE or descriptor.get("encoding") != "gzip":
        raise OSError("compressed snapshot database descriptor is malformed")
    with tempfile.TemporaryDirectory(prefix="lattice-snapshot-database-") as temporary_root:
        database = Path(temporary_root) / SNAPSHOT_DATABASE
        _decompress_database(snapshot_root / COMPRESSED_SNAPSHOT_DATABASE, database, descriptor)
        yield database


def create_snapshot(
    data_root: Path,
    *,
    backup_root: Path | None = None,
    reason: str,
    source_revision: str,
    schema_version: int | None,
    state_digest: str | None,
    require_idle: bool = True,
) -> Path:
    """Publish one complete snapshot only after its database and file inventory verify."""

    data_root = data_root.expanduser().resolve(strict=True)
    database = data_root / "lattice.sqlite3"
    if not database.is_file() or database.is_symlink():
        raise FileNotFoundError(database)
    backup_candidate = (backup_root or data_root / "backups").expanduser()
    if backup_candidate.is_symlink():
        raise OSError(f"backup root is a symlink: {backup_candidate}")
    backup_root = backup_candidate.resolve()
    protected_roots = tuple((data_root / name).resolve() for name in SNAPSHOT_CONTENT_ROOTS)
    if any(backup_root == protected or backup_root.is_relative_to(protected) for protected in protected_roots):
        raise OSError("backup root cannot live inside snapshot content")
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    artifact_id = f"artifact:{uuid.uuid4()}"
    created_at = stamp()
    final = backup_root / f"{created_at}-{artifact_id.removeprefix('artifact:')[:12]}"
    if final.exists():
        raise FileExistsError(final)
    staging = Path(tempfile.mkdtemp(prefix=".lattice-snapshot-", dir=backup_root))
    os.chmod(staging, 0o700)
    try:
        with _snapshot_guard(data_root, database, require_idle=require_idle) as active_work:
            _backup_database(database, staging / SNAPSHOT_DATABASE)
            for name in SNAPSHOT_CONTENT_FILES[1:]:
                source = data_root / name
                if source.exists():
                    _copy_regular(source, staging / name)
            for name in SNAPSHOT_CONTENT_ROOTS:
                source = data_root / name
                if source.exists():
                    if not source.is_dir():
                        raise OSError(f"snapshot content root is not a directory: {source}")
                    copy_exact_tree(source, staging / name)
            if state_digest is not None:
                # The staged bytes, not a caller's earlier observation of the live
                # store, are the authority for the published snapshot identity.
                from .diagnostics import assess_data_root

                captured = assess_data_root(staging)
                if source_revision == state_digest:
                    source_revision = captured["state_digest"]
                schema_version = captured["schema_version"]
                state_digest = captured["state_digest"]
            database_descriptor = _compress_database(
                staging / SNAPSHOT_DATABASE,
                staging / COMPRESSED_SNAPSHOT_DATABASE,
            )
            (staging / SNAPSHOT_DATABASE).unlink()
        inventory = _inventory(staging)
        inventory_descriptor = _write_inventory(staging, inventory)
        manifest = {
            "generated_object": generated_object(
                kind="lattice-backup",
                schema_name="lattice-backup",
                schema_version=4,
                subject="lattice:canonical-runtime-state",
                sources=[{"ref": f"lattice:data-root:{data_root}", "revision": source_revision, "sha256": None}],
                role="snapshot",
                artifact_id=artifact_id,
                generated_at=created_at,
            ),
            "contract_version": "LatticeBackup/4.0",
            "reason": reason,
            "source_data_root": str(data_root),
            "schema_version": schema_version,
            "state_digest": state_digest,
            "active_work": active_work,
            "database": database_descriptor,
            "inventory": inventory_descriptor,
        }
        raw = canonical_bytes(manifest)
        manifest_path = staging / SNAPSHOT_MANIFEST
        with manifest_path.open("xb") as handle:
            os.chmod(manifest_path, 0o600)
            handle.write(raw)
            handle.flush()
            _sync_file(handle.fileno())
        verify_snapshot(staging)
        _sync_tree_directories(staging)
        os.replace(staging, final)
        _sync_directory(backup_root)
        return final
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def verify_snapshot(snapshot_root: Path) -> dict[str, Any]:
    snapshot_root = snapshot_root.expanduser().resolve(strict=True)
    manifest_path = snapshot_root / SNAPSHOT_MANIFEST
    raw = manifest_path.read_bytes()
    manifest = strict_json_loads(raw)
    contract_version = manifest.get("contract_version") if isinstance(manifest, dict) else None
    if not isinstance(manifest, dict) or canonical_bytes(manifest) != raw or contract_version not in {"LatticeBackup/1.0", "LatticeBackup/2.0", "LatticeBackup/3.0", "LatticeBackup/4.0"}:
        raise OSError("Lattice snapshot manifest is not exact canonical supported LatticeBackup JSON")
    envelope = manifest.get("generated_object")
    if contract_version in {"LatticeBackup/2.0", "LatticeBackup/3.0", "LatticeBackup/4.0"}:
        try:
            validate_generated_object_envelope(envelope)
        except ValueError as error:
            raise OSError("Lattice snapshot generated-object envelope is invalid") from error
        expected_schema = {"name": "lattice-backup", "version": {"LatticeBackup/2.0": 2, "LatticeBackup/3.0": 3, "LatticeBackup/4.0": 4}[contract_version]}
    else:
        required_envelope = {"contract_version", "role", "kind", "schema", "artifact_id", "generated_at", "producer", "subject", "sources"}
        if not isinstance(envelope, dict) or set(envelope) != required_envelope:
            raise OSError("legacy Lattice snapshot generated-object envelope is invalid")
        expected_schema = {"name": "lattice-backup", "version": 1}
    producer = envelope.get("producer")
    valid_producer = isinstance(producer, dict) and producer.get("name") == "lattice" and (contract_version in {"LatticeBackup/2.0", "LatticeBackup/3.0", "LatticeBackup/4.0"} or producer == {"name": "lattice", "version": "0.1.0"})
    if envelope.get("role") != "snapshot" or envelope.get("kind") != "lattice-backup" or envelope.get("schema") != expected_schema or not valid_producer or envelope.get("subject") != "lattice:canonical-runtime-state":
        raise OSError("Lattice snapshot generated-object envelope is invalid")
    sources = envelope.get("sources")
    expected_source_keys = {"ref", "revision", "sha256"} if contract_version in {"LatticeBackup/2.0", "LatticeBackup/3.0", "LatticeBackup/4.0"} else {"ref", "revision"}
    if not isinstance(sources, list) or len(sources) != 1 or not isinstance(sources[0], dict) or set(sources[0]) != expected_source_keys or sources[0].get("sha256") is not None or not isinstance(sources[0].get("revision"), str) or not sources[0]["revision"]:
        raise OSError("Lattice snapshot generated-object source is malformed")
    inventory = _read_inventory(snapshot_root, manifest)
    declared: dict[str, dict[str, Any]] = {}
    for item in inventory:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "byte_count", "mode"} or not isinstance(item["path"], str) or item["path"] in declared:
            raise OSError("Lattice snapshot inventory is malformed")
        path = snapshot_root / item["path"]
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError as error:
            raise OSError(f"Lattice snapshot inventory file is missing: {item['path']}") from error
        if snapshot_root not in resolved.parents or path.is_symlink() or not path.is_file():
            raise OSError(f"Lattice snapshot inventory escaped its root: {item['path']}")
        digest, byte_count = _sha256_file(path)
        if digest != item["sha256"] or byte_count != item["byte_count"] or stat.S_IMODE(path.stat().st_mode) != item["mode"]:
            raise OSError(f"Lattice snapshot file disagrees with its inventory: {item['path']}")
        declared[item["path"]] = item
    actual = {path.relative_to(snapshot_root).as_posix() for path in _snapshot_files(snapshot_root)}
    if actual != set(declared):
        raise OSError(f"Lattice snapshot carries undeclared or missing files: declared={sorted(declared)}, actual={sorted(actual)}")
    expected_database_path = COMPRESSED_SNAPSHOT_DATABASE if contract_version in {"LatticeBackup/3.0", "LatticeBackup/4.0"} else SNAPSHOT_DATABASE
    if expected_database_path not in declared:
        raise OSError("snapshot database payload is missing")
    with materialized_snapshot_database(snapshot_root, manifest) as database:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise OSError(f"Lattice snapshot SQLite integrity check failed: {result}")
        finally:
            connection.close()
    return {**manifest, "inventory": inventory}


def restore_snapshot(snapshot_root: Path, destination: Path) -> dict[str, Any]:
    snapshot_root = snapshot_root.expanduser().resolve(strict=True)
    destination = destination.expanduser().resolve()
    if destination == snapshot_root or destination.is_relative_to(snapshot_root):
        raise OSError("restore destination cannot live inside its immutable snapshot")
    manifest = verify_snapshot(snapshot_root)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.restore-", dir=destination.parent))
    os.chmod(staging, 0o700)
    try:
        compressed = manifest.get("contract_version") in {"LatticeBackup/3.0", "LatticeBackup/4.0"}
        for item in manifest["inventory"]:
            if compressed and item["path"] == COMPRESSED_SNAPSHOT_DATABASE:
                continue
            _copy_regular(snapshot_root / item["path"], staging / item["path"])
        if compressed:
            _decompress_database(
                snapshot_root / COMPRESSED_SNAPSHOT_DATABASE,
                staging / SNAPSHOT_DATABASE,
                manifest["database"],
            )
            expected_files = {
                item["path"]: item
                for item in manifest["inventory"]
                if item["path"] != COMPRESSED_SNAPSHOT_DATABASE
            }
            actual_files = {
                item["path"]: item
                for item in _inventory(staging)
                if item["path"] != SNAPSHOT_DATABASE
            }
            if actual_files != expected_files:
                raise OSError("restored Lattice files do not reproduce the snapshot inventory")
        elif _inventory(staging) != manifest["inventory"]:
            raise OSError("restored Lattice files do not reproduce the snapshot inventory")
        _sync_tree_directories(staging)
        os.replace(staging, destination)
        _sync_directory(destination.parent)
        return manifest
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def pre_migration_snapshot_if_needed(data_root: Path, *, target_schema_version: int) -> Path | None:
    """Freeze legacy bytes before Store executes any schema-changing statement."""

    database = data_root / "lattice.sqlite3"
    if not database.exists():
        return None
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        current = int(connection.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]) if "schema_migrations" in tables else 0
    finally:
        connection.close()
    if current >= target_schema_version:
        return None
    return create_snapshot(
        data_root,
        reason=f"pre-migration-schema-{current}-to-{target_schema_version}",
        source_revision=f"schema:{current}",
        schema_version=current,
        state_digest=None,
        require_idle=True,
    )
