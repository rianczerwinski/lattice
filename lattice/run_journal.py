"""Incremental, bounded-memory journals for local durable-run attempts."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import threading
import time
from contextlib import asynccontextmanager, contextmanager
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


def _try_startup_lock(descriptor: int, data_root: Path, warning_at: float) -> tuple[bool, float]:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return True, warning_at
    except BlockingIOError:
        now = time.monotonic()
        if now >= warning_at:
            logging.getLogger(__name__).warning("Lattice startup waiting for snapshot/journal capture at %s", data_root)
            # Periodic diagnostics explain a long capture without imposing the
            # database timeout that caused repeated startup crashes.
            warning_at = now + 30
        return False, warning_at


@asynccontextmanager
async def async_journal_startup_guard(data_root: Path):
    """Cancellable startup wait; never leave a thread acquiring a leaked lock."""
    data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(data_root / DURABLE_RUNS_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        warning_at = 0.0
        while True:
            acquired, warning_at = _try_startup_lock(descriptor, data_root, warning_at)
            if acquired:
                break
            await asyncio.sleep(0.1)
        yield
    finally:
        os.close(descriptor)


@contextmanager
def journal_startup_guard(data_root: Path) -> Iterator[None]:
    """Wait outside SQLite while a backup owns its writer reservation.

    Take this only after pre-migration snapshots, which need LOCK_EX themselves.
    Shared ownership permits orphan reconciliation and journal writers while
    excluding a new snapshot until startup's database writes have finished.
    """
    data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(data_root / DURABLE_RUNS_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        warning_at = 0.0
        while True:
            acquired, warning_at = _try_startup_lock(descriptor, data_root, warning_at)
            if acquired:
                break
            time.sleep(0.1)
        yield
    finally:
        os.close(descriptor)


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
        sync_directory(self.root.parent)
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
        sync_directory(self.root)

    def append_event(self, raw: bytes, *, framed: bool = False, sync: bool = True) -> int:
        offset = self._events.tell()
        self._events.write(raw if framed else raw + b"\n")
        if sync:
            sync_file(self._events.fileno())
        return offset

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

    def reconcile_terminal(self, receipt: dict[str, Any]) -> None:
        """Retry only a committed terminal projection after its writer closed.

        An existing different/partial receipt needs explicit diagnosis; it is
        never overwritten. SQLite remains the semantic completion authority.
        """
        for path in (self.events_path, self.stdout_path, self.stderr_path):
            with path.open("r+b", buffering=0) as handle:
                sync_file(handle.fileno())
        terminal = self.root / "terminal.json"
        if terminal.exists():
            if terminal.read_bytes() != canonical_bytes(receipt):
                raise OSError("terminal projection differs from committed receipt")
            with terminal.open("r+b", buffering=0) as handle:
                sync_file(handle.fileno())
            sync_directory(self.root)
        else:
            self._write_once("terminal.json", receipt)
