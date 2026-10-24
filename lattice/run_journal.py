"""Incremental, bounded-memory journals for local durable-run attempts."""

from __future__ import annotations

import fcntl
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .canonical import canonical_bytes, sha256_bytes
from .durability import sync_directory, sync_file


DURABLE_RUNS_DIRECTORY = "durable-runs"
DURABLE_RUNS_LOCK = "durable-runs.lock"


def acquire_journal_writer(data_root: Path) -> int:
    """Hold a shared generation lock until one production journal is complete."""

    data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(data_root / DURABLE_RUNS_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def journal_capture_guard(data_root: Path, *, blocking: bool = True) -> Iterator[None]:
    """Exclude journal writers while snapshot or rebuild captures their bytes."""

    data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(data_root / DURABLE_RUNS_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as error:
            raise RuntimeError("snapshot or journal writer is active; retry maintenance after it completes") from error
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


class DurableRunJournal:
    def __init__(self, root: Path, launch_receipt: dict[str, Any], *, coordination_fd: int | None = None) -> None:
        self.root = root
        self._coordination_fd = coordination_fd
        self.root.mkdir(parents=True, exist_ok=False, mode=0o700)
        os.chmod(self.root, 0o700)
        self.events_path = root / "events.jsonl"
        self.stdout_path = root / "stdout.log"
        self.stderr_path = root / "stderr.log"
        self._events = self.events_path.open("xb", buffering=0)
        self._stdout = self.stdout_path.open("xb", buffering=0)
        self._stderr = self.stderr_path.open("xb", buffering=0)
        for path in (self.events_path, self.stdout_path, self.stderr_path):
            os.chmod(path, 0o600)
        self._stdout_lock = threading.Lock()
        self._stderr_lock = threading.Lock()
        self._closed = False
        self._write_once("launch.json", launch_receipt)
        sync_directory(root)

    def _write_once(self, name: str, value: dict[str, Any]) -> None:
        path = self.root / name
        raw = canonical_bytes(value)
        with path.open("xb", buffering=0) as handle:
            os.chmod(path, 0o600)
            handle.write(raw)
            sync_file(handle.fileno())

    def append_event(self, raw: bytes) -> None:
        self._events.write(raw + b"\n")
        sync_file(self._events.fileno())

    def write_unit_plan(self, *, run_id: str, attempt_id: str, remaining_unit_keys: list[str]) -> tuple[Path, str]:
        value = {
            "attempt_id": attempt_id,
            "contract_version": "RunUnitPlan/1.0",
            "remaining_unit_keys": remaining_unit_keys,
            "run_id": run_id,
        }
        self._write_once("unit-plan.json", value)
        raw = canonical_bytes(value)
        return self.root / "unit-plan.json", sha256_bytes(raw)

    def write_stdout(self, chunk: bytes) -> None:
        with self._stdout_lock:
            self._stdout.write(chunk)

    def write_stderr(self, chunk: bytes) -> None:
        with self._stderr_lock:
            self._stderr.write(chunk)

    def sync_streams(self) -> None:
        with self._stdout_lock:
            sync_file(self._stdout.fileno())
        with self._stderr_lock:
            sync_file(self._stderr.fileno())

    @property
    def stdout_bytes(self) -> int:
        with self._stdout_lock:
            return self._stdout.tell()

    @property
    def stderr_bytes(self) -> int:
        with self._stderr_lock:
            return self._stderr.tell()

    def finish(self, receipt: dict[str, Any]) -> None:
        self.sync_streams()
        sync_file(self._events.fileno())
        self._write_once("terminal.json", receipt)
        sync_directory(self.root)

    def close(self) -> None:
        if self._closed:
            return
        self._events.close()
        self._stdout.close()
        self._stderr.close()
        if self._coordination_fd is not None:
            fcntl.flock(self._coordination_fd, fcntl.LOCK_UN)
            os.close(self._coordination_fd)
            self._coordination_fd = None
        self._closed = True
