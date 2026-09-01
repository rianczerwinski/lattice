"""Exact local-process binding and launch for semantic durable runs."""

from __future__ import annotations

import ast
import hashlib
import fcntl
import os
import secrets
import select
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError, field_validator

from .canonical import canonical_bytes, sha256_bytes, strict_json_loads
from .contracts import DurableRunDeclarationV2, LocalFileClosureV1, RunProcessIdentityV2, StrictModel
from .durability import sync_directory, sync_file
from .run_sdk import (
    ATTEMPT_ID_ENV,
    CHECKPOINT_BYTES_ENV,
    CHECKPOINT_ID_ENV,
    CHECKPOINT_LOCATOR_ENV,
    CHECKPOINT_READ_PATH_ENV,
    CHECKPOINT_SHA256_ENV,
    CONTROL_FD_ENV,
    EVENT_FD_ENV,
    RUN_ID_ENV,
    UNIT_PLAN_PATH_ENV,
)
from .store import LatticeConflict


LOCAL_EXECUTOR_IDENTITY = "lattice-local-durable-run-v1"
EXECUTOR_ENTRY_FILES = ("__init__.py", "run_protocol.py", "run_sdk.py", "run_journal.py", "run_launcher.py", "run_executor.py", "run_supervisor.py")
TARGET_ENVIRONMENT_CONTRACT = "LocalRunTargetEnvironment/1.0"


def sanitized_launcher_environment() -> dict[str, str]:
    """Return the complete non-project environment admitted before the exec gate."""

    environment = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
    }
    if sys.platform == "darwin":
        environment["__CF_USER_TEXT_ENCODING"] = f"0x{os.getuid():X}:0x0:0x0"
    return environment


class LocalRunLaunchRefused(LatticeConflict):
    """A typed proof limit or immutable-binding failure prevents local launch."""

    def __init__(self, failure_kind: str, message: str) -> None:
        super().__init__(message)
        self.failure_kind = failure_kind


class LocalRunLaunchV1(StrictModel):
    """Legacy ephemeral request; retained for V1 read compatibility only."""

    contract_version: str = Field(default="LocalRunLaunch/1.0", pattern=r"^LocalRunLaunch/1\.0$")
    idempotency_key: str = Field(min_length=8, max_length=300, pattern=r"^[^\x00-\x1f\x7f]+$")
    command: list[str] = Field(min_length=1, max_length=1000)
    cwd: str = Field(min_length=1, max_length=4000, pattern=r"^[^\x00-\x1f\x7f]+$")
    environment: dict[str, str] = Field(default_factory=dict, max_length=1000)
    artifact_roots: list[str] = Field(min_length=1, max_length=100)
    code_revision: str = Field(min_length=1, max_length=500, pattern=r"^[^\x00-\x1f\x7f]+$")
    inputs_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("command")
    @classmethod
    def command_is_process_safe(cls, value: list[str]) -> list[str]:
        if any(not item or len(item) > 100_000 or "\x00" in item for item in value):
            raise ValueError("legacy local durable-run command arguments must be non-empty NUL-free strings")
        return value


def sha256_file(path: Path) -> tuple[str, int]:
    if path.is_symlink():
        raise OSError(f"bound executable or artifact is a symlink: {path}")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise OSError(f"bound executable or artifact is not a regular file: {resolved}")
    before = resolved.stat()
    digest = hashlib.sha256()
    byte_count = 0
    with resolved.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            byte_count += len(chunk)
    after = resolved.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or byte_count != after.st_size:
        raise OSError(f"bound file changed while hashing: {resolved}")
    return digest.hexdigest(), byte_count


def sha256_executable(path: Path) -> tuple[str, int]:
    """Hash executable bytes while preserving a symlinked launcher path.

    Python virtual environments use the invoked symlink path to select their
    environment. Resolving that path before launch silently escapes the venv,
    so executable identity binds the launcher path and the bytes of its current
    target separately.
    """

    resolved = path.resolve(strict=True)
    return sha256_file(resolved)


def _local_module_path(root: Path, parts: tuple[str, ...]) -> Path | None:
    file_candidate = root.joinpath(*parts).with_suffix(".py") if parts else None
    package_candidate = root.joinpath(*parts, "__init__.py")
    if file_candidate is not None and file_candidate.is_file():
        return file_candidate
    if package_candidate.is_file():
        return package_candidate
    return None


def executor_implementation_manifest(
    source_root: Path | None = None,
    entry_files: tuple[str, ...] = EXECUTOR_ENTRY_FILES,
) -> list[dict[str, str | int]]:
    """Bind the transitive local-Python closure of the durable-run executor."""

    root = (source_root or Path(__file__).resolve().parent).resolve(strict=True)
    pending = [Path(name) for name in entry_files]
    discovered: dict[str, dict[str, str | int]] = {}
    while pending:
        relative = pending.pop()
        if relative.is_absolute() or ".." in relative.parts:
            raise OSError(f"executor source path escaped its package root: {relative}")
        normalized = relative.as_posix()
        if normalized in discovered:
            continue
        source = root / relative
        if source.is_symlink() or not source.is_file():
            raise OSError(f"executor source is unavailable or not a regular file: {source}")
        first_digest, first_bytes = sha256_file(source)
        raw = source.read_bytes()
        second_digest, second_bytes = sha256_file(source)
        if first_digest != second_digest or first_bytes != second_bytes or hashlib.sha256(raw).hexdigest() != first_digest or len(raw) != first_bytes:
            raise OSError(f"executor source changed while resolving its import closure: {source}")
        discovered[normalized] = {"path": normalized, "sha256": first_digest, "byte_count": first_bytes}

        for parent in relative.parents:
            if parent == Path("."):
                continue
            initializer = parent / "__init__.py"
            if (root / initializer).is_file():
                pending.append(initializer)

        package_parts = () if relative.parent == Path(".") else relative.parent.parts
        tree = ast.parse(raw, filename=str(source))
        for node in ast.walk(tree):
            module_parts: tuple[str, ...] | None = None
            aliases: tuple[str, ...] = ()
            if isinstance(node, ast.ImportFrom):
                aliases = tuple(alias.name for alias in node.names if alias.name != "*")
                if node.level:
                    ascend = node.level - 1
                    if ascend > len(package_parts):
                        continue
                    base = package_parts[: len(package_parts) - ascend]
                    module_parts = (*base, *(node.module.split(".") if node.module else ()))
                elif node.module == "lattice" or (node.module and node.module.startswith("lattice.")):
                    module_parts = tuple(node.module.split(".")[1:])
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "lattice" or alias.name.startswith("lattice."):
                        candidate = _local_module_path(root, tuple(alias.name.split(".")[1:]))
                        if candidate is not None:
                            pending.append(candidate.relative_to(root))
                continue
            if module_parts is None:
                continue
            candidate = _local_module_path(root, module_parts)
            if candidate is not None:
                pending.append(candidate.relative_to(root))
                if candidate.name == "__init__.py":
                    for alias in aliases:
                        child = _local_module_path(root, (*module_parts, *alias.split(".")))
                        if child is not None:
                            pending.append(child.relative_to(root))
            elif not node.module:
                for alias in aliases:
                    child = _local_module_path(root, (*module_parts, *alias.split(".")))
                    if child is not None:
                        pending.append(child.relative_to(root))
    return [discovered[path] for path in sorted(discovered)]


def executor_implementation_sha256(
    source_root: Path | None = None,
    entry_files: tuple[str, ...] = EXECUTOR_ENTRY_FILES,
) -> str:
    return sha256_bytes(canonical_bytes(executor_implementation_manifest(source_root, entry_files)))


def verify_local_closure(closure: LocalFileClosureV1, *, label: str) -> None:
    for binding in closure.bindings:
        candidate = Path(binding.path)
        if candidate.is_symlink():
            raise LocalRunLaunchRefused(f"{label}_binding_drift", f"{label} closure contains a symlink: {binding.path}")
        try:
            resolved = candidate.resolve(strict=True)
            if str(resolved) != binding.path:
                raise OSError("path is not canonical")
            digest, byte_count = sha256_file(resolved)
        except OSError as error:
            raise LocalRunLaunchRefused(f"{label}_binding_drift", f"{label} closure is unavailable: {binding.path}: {error}") from error
        if digest != binding.content_sha256 or byte_count != binding.byte_count:
            raise LocalRunLaunchRefused(f"{label}_binding_drift", f"{label} closure bytes changed: {binding.path}")


@dataclass(frozen=True)
class BoundLocalRun:
    command: tuple[str, ...]
    cwd: Path
    environment: dict[str, str]
    artifact_roots: tuple[Path, ...]
    command_sha256: str
    environment_profile_sha256: str
    executable_sha256: str
    executor_sha256: str
    code_revision: str
    inputs_sha256: str
    code_closure: LocalFileClosureV1
    input_closure: LocalFileClosureV1

    def receipt(self) -> dict[str, Any]:
        return {
            "kind": "lattice-local-process-v1",
            "command": list(self.command),
            "command_sha256": self.command_sha256,
            "cwd": str(self.cwd),
            "environment_profile_sha256": self.environment_profile_sha256,
            "environment_keys": sorted(self.environment),
            "artifact_roots": [str(path) for path in self.artifact_roots],
            "executable_sha256": self.executable_sha256,
            "executor_identity": LOCAL_EXECUTOR_IDENTITY,
            "executor_sha256": self.executor_sha256,
            "code_revision": self.code_revision,
            "inputs_sha256": self.inputs_sha256,
        }


def bind_declared_local_run(run: dict[str, Any]) -> BoundLocalRun:
    raw_declaration = run["declaration"]
    if raw_declaration.get("contract_version") != "DurableRunDeclaration/2.0":
        raise LocalRunLaunchRefused(
            "declaration_not_relaunchable",
            "DurableRunDeclaration/1.0 has no durable launch profile and cannot be launched or resumed by the local executor",
        )
    try:
        declaration = DurableRunDeclarationV2.model_validate(raw_declaration)
    except ValidationError as error:
        raise LocalRunLaunchRefused("invalid_launch_profile", f"durable local launch profile is invalid: {error}") from error
    profile = declaration.local_launch_profile
    verify_local_closure(declaration.code_closure, label="code")
    verify_local_closure(declaration.input_closure, label="input")
    executable_candidate = Path(profile.command[0])
    try:
        if not executable_candidate.exists():
            raise OSError("executable does not exist")
        executable = executable_candidate.absolute()
        executable_sha256, _ = sha256_executable(executable)
    except OSError as error:
        raise LocalRunLaunchRefused("binding_drift", f"local durable-run executable is unavailable: {error}") from error
    cwd_candidate = Path(profile.cwd)
    if not cwd_candidate.is_absolute() or cwd_candidate.is_symlink():
        raise LocalRunLaunchRefused("binding_drift", "local durable-run cwd must be an absolute real directory")
    try:
        cwd = cwd_candidate.resolve(strict=True)
    except OSError as error:
        raise LocalRunLaunchRefused("binding_drift", f"local durable-run cwd is unavailable: {error}") from error
    if not cwd.is_dir() or str(cwd) != profile.cwd:
        raise LocalRunLaunchRefused("binding_drift", "local durable-run cwd changed or is not a canonical real directory")
    roots: list[Path] = []
    for raw in profile.artifact_roots:
        candidate = Path(raw)
        if not candidate.is_absolute() or candidate.is_symlink():
            raise LocalRunLaunchRefused("binding_drift", "local durable-run artifact roots must be absolute real directories")
        try:
            root = candidate.resolve(strict=True)
        except OSError as error:
            raise LocalRunLaunchRefused("binding_drift", f"local durable-run artifact root is unavailable: {error}") from error
        if not root.is_dir() or str(root) != raw:
            raise LocalRunLaunchRefused("binding_drift", "local durable-run artifact root changed or is not a canonical real directory")
        roots.append(root)
    environment = profile.literal_environment()
    reserved = sorted(
        set(environment)
        & {
            EVENT_FD_ENV,
            CONTROL_FD_ENV,
            RUN_ID_ENV,
            ATTEMPT_ID_ENV,
            UNIT_PLAN_PATH_ENV,
            CHECKPOINT_ID_ENV,
            CHECKPOINT_LOCATOR_ENV,
            CHECKPOINT_SHA256_ENV,
            CHECKPOINT_BYTES_ENV,
            CHECKPOINT_READ_PATH_ENV,
        }
    )
    if reserved:
        raise LocalRunLaunchRefused("reserved_environment", f"local durable-run profile cannot override SDK environment: {reserved}")
    normalized_command = (str(executable), *profile.command[1:])
    command_sha256 = sha256_bytes(canonical_bytes({"argv": list(normalized_command[1:])}))
    environment_material = {"cwd": str(cwd), "environment": environment, "artifact_roots": [str(path) for path in roots]}
    environment_profile_sha256 = sha256_bytes(canonical_bytes(environment_material))
    try:
        executor_sha256 = executor_implementation_sha256()
    except OSError as error:
        raise LocalRunLaunchRefused("executor_binding_drift", f"local durable-run executor closure is unavailable: {error}") from error
    exact = {
        "executable_ref": str(executable),
        "executable_sha256": executable_sha256,
        "configuration_sha256": command_sha256,
        "environment_profile_sha256": environment_profile_sha256,
        "executor_identity": LOCAL_EXECUTOR_IDENTITY,
        "executor_sha256": executor_sha256,
        "code_revision": declaration.code_revision,
        "inputs_sha256": declaration.inputs_sha256,
    }
    declaration_values = declaration.model_dump(mode="json")
    changed = [key for key, observed in exact.items() if declaration_values[key] != observed]
    if changed:
        raise LocalRunLaunchRefused("binding_drift", f"local durable-run launch disagrees with immutable binding: {', '.join(changed)}")
    return BoundLocalRun(
        command=normalized_command,
        cwd=cwd,
        environment=environment,
        artifact_roots=tuple(roots),
        command_sha256=command_sha256,
        environment_profile_sha256=environment_profile_sha256,
        executable_sha256=executable_sha256,
        executor_sha256=executor_sha256,
        code_revision=declaration.code_revision,
        inputs_sha256=declaration.inputs_sha256,
        code_closure=declaration.code_closure,
        input_closure=declaration.input_closure,
    )


@dataclass
class SpawnedLocalRun:
    process: subprocess.Popen[bytes]
    event_read_fd: int
    control_write_fd: int
    gate_write_fd: int
    process_identity: RunProcessIdentityV2


def _close_descriptor(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def spawn_local_run(
    bound: BoundLocalRun,
    *,
    run_id: str,
    attempt_id: str,
    attempt_root: Path,
    unit_plan_path: Path,
    checkpoint: dict[str, Any] | None,
    checkpoint_read_path: Path | None,
) -> SpawnedLocalRun:
    try:
        observed_executor_sha256 = executor_implementation_sha256()
    except OSError as error:
        raise LocalRunLaunchRefused("executor_binding_drift", f"local durable-run executor closure became unavailable: {error}") from error
    if observed_executor_sha256 != bound.executor_sha256:
        raise LocalRunLaunchRefused("executor_binding_drift", "local durable-run executor closure changed after binding")
    verify_local_closure(bound.code_closure, label="code")
    verify_local_closure(bound.input_closure, label="input")
    try:
        observed_executable_sha256, _ = sha256_executable(Path(bound.command[0]))
    except OSError as error:
        raise LocalRunLaunchRefused("binding_drift", f"local durable-run executable became unavailable: {error}") from error
    if observed_executable_sha256 != bound.executable_sha256:
        raise LocalRunLaunchRefused("binding_drift", "local durable-run executable changed after binding")
    event_read_fd, event_write_fd = os.pipe()
    control_read_fd, control_write_fd = os.pipe()
    gate_read_fd, gate_write_fd = os.pipe()
    ready_read_fd, ready_write_fd = os.pipe()
    os.set_inheritable(event_write_fd, True)
    os.set_inheritable(control_read_fd, True)
    os.set_inheritable(gate_read_fd, True)
    os.set_inheritable(ready_write_fd, True)
    child_environment = dict(bound.environment)
    child_environment.update(
        {
            EVENT_FD_ENV: str(event_write_fd),
            CONTROL_FD_ENV: str(control_read_fd),
            RUN_ID_ENV: run_id,
            ATTEMPT_ID_ENV: attempt_id,
            UNIT_PLAN_PATH_ENV: str(unit_plan_path),
        }
    )
    if checkpoint is not None:
        if checkpoint_read_path is None or not checkpoint_read_path.is_absolute():
            raise LocalRunLaunchRefused("checkpoint_binding_drift", "resume checkpoint lacks a verified local read path")
        child_environment.update(
            {
                CHECKPOINT_ID_ENV: checkpoint["artifact_id"],
                CHECKPOINT_LOCATOR_ENV: checkpoint["storage_locator"],
                CHECKPOINT_SHA256_ENV: checkpoint["content_sha256"],
                CHECKPOINT_BYTES_ENV: str(checkpoint["byte_count"]),
                CHECKPOINT_READ_PATH_ENV: str(checkpoint_read_path),
            }
        )
    elif checkpoint_read_path is not None:
        raise LocalRunLaunchRefused("checkpoint_binding_drift", "checkpoint read path was supplied without checkpoint identity")
    target_environment_path = attempt_root / "target-environment.json"
    target_environment_raw = canonical_bytes(
        {
            "contract_version": TARGET_ENVIRONMENT_CONTRACT,
            "environment": child_environment,
        }
    )
    target_environment_sha256 = sha256_bytes(target_environment_raw)
    with target_environment_path.open("xb", buffering=0) as handle:
        os.chmod(target_environment_path, 0o600)
        handle.write(target_environment_raw)
        sync_file(handle.fileno())
    sync_directory(attempt_root)
    launcher_environment = sanitized_launcher_environment()
    launcher_environment_sha256 = sha256_bytes(canonical_bytes(launcher_environment))
    nonce = secrets.token_hex(32)
    lock_path = attempt_root / "process.lock"
    record_path = attempt_root / "process.json"
    launcher = Path(__file__).resolve().with_name("run_launcher.py")
    launcher_command = [
        sys.executable,
        "-I",
        "-S",
        str(launcher),
        "--gate-fd",
        str(gate_read_fd),
        "--ready-fd",
        str(ready_write_fd),
        "--run-id",
        run_id,
        "--attempt-id",
        attempt_id,
        "--nonce",
        nonce,
        "--lock-path",
        str(lock_path),
        "--record-path",
        str(record_path),
        "--target-environment-path",
        str(target_environment_path),
        "--target-environment-sha256",
        target_environment_sha256,
        "--launcher-environment-sha256",
        launcher_environment_sha256,
        "--worker-fd",
        str(event_write_fd),
        "--worker-fd",
        str(control_read_fd),
        "--",
        *bound.command,
    ]
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            launcher_command,
            cwd=bound.cwd,
            env=launcher_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
            pass_fds=(event_write_fd, control_read_fd, gate_read_fd, ready_write_fd),
        )
    except BaseException:
        for descriptor in (event_read_fd, control_write_fd, gate_write_fd, ready_read_fd):
            _close_descriptor(descriptor)
        raise
    finally:
        for descriptor in (event_write_fd, control_read_fd, gate_read_fd, ready_write_fd):
            _close_descriptor(descriptor)
    assert process is not None
    try:
        readable, _, _ = select.select([ready_read_fd], [], [], 5.0)
        if not readable:
            raise OSError("durable-run launcher did not persist its process identity before the startup deadline")
        with os.fdopen(ready_read_fd, "rb", buffering=0) as ready:
            raw = ready.readline(1024 * 1024 + 2)
        ready_read_fd = -1
        if not raw.endswith(b"\n") or len(raw) - 1 > 1024 * 1024:
            raise OSError("durable-run launcher emitted an invalid process identity frame")
        value = strict_json_loads(raw[:-1])
        if canonical_bytes(value) != raw[:-1]:
            raise OSError("durable-run launcher process identity is not canonical JSON")
        identity = RunProcessIdentityV2.model_validate(value)
        expected = {
            "run_id": run_id,
            "attempt_id": attempt_id,
            "pid": process.pid,
            "process_group_id": process.pid,
            "nonce": nonce,
            "lock_path": str(lock_path),
            "record_path": str(record_path),
            "target_environment_path": str(target_environment_path),
            "target_environment_sha256": target_environment_sha256,
            "launcher_environment_sha256": launcher_environment_sha256,
        }
        changed = [key for key, expected_value in expected.items() if getattr(identity, key) != expected_value]
        if changed or record_path.read_bytes() != raw[:-1]:
            raise OSError(f"durable-run launcher process identity disagrees with its parent: {changed}")
    except BaseException:
        _close_descriptor(ready_read_fd)
        _close_descriptor(gate_write_fd)
        _close_descriptor(event_read_fd)
        _close_descriptor(control_write_fd)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        raise
    return SpawnedLocalRun(process=process, event_read_fd=event_read_fd, control_write_fd=control_write_fd, gate_write_fd=gate_write_fd, process_identity=identity)


def release_local_run(spawned: SpawnedLocalRun) -> None:
    try:
        if os.write(spawned.gate_write_fd, b"G") != 1:
            raise OSError("durable-run exec gate accepted no release byte")
    finally:
        _close_descriptor(spawned.gate_write_fd)


def load_process_identity(record_path: Path, *, run_id: str, attempt_id: str, attempt_root: Path) -> RunProcessIdentityV2:
    if record_path.is_symlink():
        raise OSError("durable-run process record is a symlink")
    raw = record_path.read_bytes()
    if not raw or len(raw) > 1024 * 1024:
        raise OSError("durable-run process record is empty or oversized")
    value = strict_json_loads(raw)
    if canonical_bytes(value) != raw:
        raise OSError("durable-run process record is not canonical JSON")
    identity = RunProcessIdentityV2.model_validate(value)
    expected_root = attempt_root.resolve(strict=True)
    if identity.run_id != run_id or identity.attempt_id != attempt_id:
        raise OSError("durable-run process record belongs to another attempt")
    if (
        Path(identity.record_path) != expected_root / "process.json"
        or Path(identity.lock_path) != expected_root / "process.lock"
        or Path(identity.target_environment_path) != expected_root / "target-environment.json"
    ):
        raise OSError("durable-run process record escaped its attempt journal")
    return identity


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def terminate_and_prove_process_dead(identity: RunProcessIdentityV2) -> bool:
    """Kill only a group whose nonce guardian still holds the identity lock."""

    lock_path = Path(identity.lock_path)
    if lock_path.is_symlink():
        raise OSError("durable-run process lock is a symlink")
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise OSError("durable-run process lock is not a regular file")
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.read(descriptor, 256) != (identity.nonce + "\n").encode("ascii"):
            raise OSError("durable-run process lock disagrees with its identity nonce")
        lock_acquired = False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_acquired = True
        except BlockingIOError:
            pass
        if lock_acquired:
            # A freely acquired nonce lock means the recorded guardian no
            # longer owns this numeric process-group identity.  A live group
            # can therefore be PID/PGID reuse and must never inherit kill
            # authority from the stale record.
            return False
        try:
            os.killpg(identity.process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            group_exists = _process_group_exists(identity.process_group_id)
            if not lock_acquired:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    lock_acquired = True
                except BlockingIOError:
                    pass
            if lock_acquired:
                return not group_exists
            time.sleep(0.02)
        group_exists = _process_group_exists(identity.process_group_id)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return not group_exists
        except BlockingIOError:
            pass
        try:
            os.killpg(identity.process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            group_exists = _process_group_exists(identity.process_group_id)
            if not lock_acquired:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    lock_acquired = True
                except BlockingIOError:
                    pass
            if lock_acquired:
                return not group_exists
            time.sleep(0.02)
        raise OSError("durable-run process group remained live or retained its guardian lock after SIGKILL")
    finally:
        os.close(descriptor)


def verify_worker_artifact(storage_locator: str, expected_sha256: str, expected_bytes: int, roots: tuple[Path, ...]) -> Path | None:
    if storage_locator.startswith("lattice:blob/"):
        return None
    if not storage_locator.startswith("file:"):
        raise LatticeConflict("local durable-run worker artifacts require a file or Lattice CAS locator")
    raw = storage_locator.removeprefix("file:")
    candidate = Path(raw)
    if not candidate.is_absolute() or candidate.is_symlink():
        raise LatticeConflict("local durable-run worker artifact must be an absolute real file")
    resolved = candidate.resolve(strict=True)
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        raise LatticeConflict("local durable-run worker artifact escaped its declared artifact roots")
    digest, byte_count = sha256_file(resolved)
    if digest != expected_sha256 or byte_count != expected_bytes:
        raise LatticeConflict("local durable-run worker artifact changed or disagrees with its event")
    return resolved
