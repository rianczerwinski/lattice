"""Subprocess execution with live raw journaling and checkpoint projection."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Mapping

from .checkpoint import observe_chunk, observe_sidecar
from .journal import write_all


MAX_MODEL_OUTPUT_BYTES = 64 * 1024 * 1024
_PARENT_DEATH_WATCHDOG = """import os,signal,sys,time
parent=int(sys.argv[1]); group=int(sys.argv[2])
while os.getppid()==parent:
    time.sleep(0.1)
try:
    os.killpg(group, signal.SIGKILL)
except ProcessLookupError:
    pass
"""


class ProcessCancelled(RuntimeError):
    """The operator cancelled a bounded process after preserving its output."""

    def __init__(self, command: list[str], stdout: str, stderr: str) -> None:
        super().__init__(f"process cancelled: {command[0] if command else '<unknown>'}")
        self.command = command
        self.stdout = stdout
        self.stderr = stderr


class ProcessOutputLimit(RuntimeError):
    """The provider exceeded the admitted in-memory/output-observation budget."""

    def __init__(self, command: list[str], stdout: str, stderr: str, max_output_bytes: int) -> None:
        super().__init__(f"process exceeded its {max_output_bytes}-byte output limit: {command[0] if command else '<unknown>'}")
        self.command = command
        self.stdout = stdout
        self.stderr = stderr
        self.max_output_bytes = max_output_bytes


def terminate_process_group(proc: subprocess.Popen[object]) -> None:
    # The direct child may exit while descendants still own its stdout/stderr.
    # Address the session/process group regardless of the leader's state.
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (PermissionError, ProcessLookupError):
        if proc.poll() is None:
            proc.terminate()
    try:
        proc.wait(timeout=2)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except PermissionError:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass


def _join_drain_thread(thread: threading.Thread | None, proc: subprocess.Popen[object], *, timeout: float) -> bool:
    if thread is None:
        return False
    thread.join(timeout=max(timeout, 0.001))
    retained_pipe = thread.is_alive()
    if thread.is_alive():
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (PermissionError, ProcessLookupError):
            pass
        thread.join(timeout=2)
    if thread.is_alive():
        raise OSError("provider descendants retained an output pipe after process-group termination")
    return retained_pipe


def _journal_root(stdout_journal: Path | None, stderr_journal: Path | None) -> Path | None:
    paths = [path for path in (stdout_journal, stderr_journal) if path is not None]
    if not paths or any(path.parent != paths[0].parent for path in paths):
        return None
    return paths[0].parent


def run_bounded(
    command: list[str],
    *,
    timeout: float | None,
    input_text: str | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    stdout_journal: Path | None = None,
    stderr_journal: Path | None = None,
    observed_files: Mapping[str, Path] | None = None,
    cancel_event: threading.Event | None = None,
    on_chunk: Callable[[str, bytes], None] | None = None,
    max_output_bytes: int = MAX_MODEL_OUTPUT_BYTES,
) -> subprocess.CompletedProcess[str]:
    if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int) or max_output_bytes < 1:
        raise ValueError("max_output_bytes must be a positive integer")
    proc: subprocess.Popen[bytes] = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )
    try:
        watchdog = subprocess.Popen(
            [sys.executable, "-c", _PARENT_DEATH_WATCHDOG, str(os.getpid()), str(proc.pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except BaseException:
        terminate_process_group(proc)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
        raise
    root = _journal_root(stdout_journal, stderr_journal)
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    drain_errors: list[tuple[str, Path, BaseException]] = []
    output_limit_hit = threading.Event()
    output_lock = threading.Lock()
    observed_output_bytes = 0

    def admit_output(chunk: bytes, captured: list[bytes] | None = None) -> bytes:
        """Reserve the bounded byte budget before exposing a chunk anywhere."""
        nonlocal observed_output_bytes
        with output_lock:
            available = max(0, max_output_bytes - observed_output_bytes)
            admitted = chunk[:available]
            if captured is not None and admitted:
                captured.append(admitted)
            observed_output_bytes += len(chunk)
            if len(chunk) > available:
                output_limit_hit.set()
            return admitted

    def drain(channel: str, stream, chunks: list[bytes], journal_path: Path | None) -> None:
        descriptor: int | None = None
        try:
            if journal_path is not None:
                journal_path.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(journal_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            while True:
                chunk = os.read(stream.fileno(), 65536)
                if not chunk:
                    break
                admitted = admit_output(chunk, chunks)
                if not admitted:
                    break
                if descriptor is not None:
                    write_all(descriptor, admitted)
                    os.fsync(descriptor)
                if root is not None:
                    observe_chunk(root, channel, admitted)
                if on_chunk is not None:
                    on_chunk(channel, admitted)
                if len(admitted) != len(chunk):
                    break
        except BaseException as exc:  # noqa: BLE001 - durable transport boundary
            drain_errors.append((channel, journal_path or Path("<unset>"), exc))
        finally:
            if descriptor is not None:
                os.close(descriptor)
            stream.close()

    stdout_thread = threading.Thread(target=drain, args=("stdout", proc.stdout, stdout_chunks, stdout_journal), name="process-stdout-journal")
    stderr_thread = threading.Thread(target=drain, args=("stderr", proc.stderr, stderr_chunks, stderr_journal), name="process-stderr-journal")
    stdout_thread.start()
    stderr_thread.start()

    def write_input() -> None:
        if proc.stdin is None:
            return
        try:
            proc.stdin.write((input_text or "").encode("utf-8"))
            proc.stdin.flush()
        except BrokenPipeError:
            pass
        finally:
            proc.stdin.close()

    input_thread: threading.Thread | None = None
    if input_text is not None:
        input_thread = threading.Thread(target=write_input, name="process-stdin-writer")
        input_thread.start()

    offsets = {name: 0 for name in (observed_files or {})}

    def refresh_sidecars() -> None:
        if root is None:
            return
        for name, path in (observed_files or {}).items():
            try:
                with path.open("rb") as handle:
                    handle.seek(offsets[name])
                    while not output_limit_hit.is_set():
                        chunk = handle.read(65536)
                        if not chunk:
                            break
                        admitted = admit_output(chunk)
                        if admitted:
                            offsets[name] += len(admitted)
                            observe_sidecar(root, name, admitted)
                        if len(admitted) != len(chunk):
                            break
            except FileNotFoundError:
                continue

    deadline = None if timeout is None else time.monotonic() + max(timeout, 0.001)
    timed_out = False
    cancelled = False
    interrupted: KeyboardInterrupt | None = None
    try:
        while proc.poll() is None:
            refresh_sidecars()
            if drain_errors:
                terminate_process_group(proc)
                break
            if output_limit_hit.is_set():
                terminate_process_group(proc)
                break
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                terminate_process_group(proc)
                break
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                terminate_process_group(proc)
                break
            time.sleep(0.02)
    except KeyboardInterrupt as exc:
        interrupted = exc
        terminate_process_group(proc)
    finally:
        join_timeout = 2.0 if deadline is None else max(0.001, deadline - time.monotonic())
        retained_pipe = _join_drain_thread(input_thread, proc, timeout=join_timeout)
        retained_pipe = _join_drain_thread(stdout_thread, proc, timeout=join_timeout) or retained_pipe
        retained_pipe = _join_drain_thread(stderr_thread, proc, timeout=join_timeout) or retained_pipe
        refresh_sidecars()
        if watchdog.poll() is None:
            watchdog.terminate()
            try:
                watchdog.wait(timeout=2)
            except subprocess.TimeoutExpired:
                watchdog.kill()
                watchdog.wait(timeout=2)

    if retained_pipe and not timed_out and not cancelled and not output_limit_hit.is_set() and not drain_errors:
        raise OSError("provider child exited while an unowned descendant retained its output pipe")

    stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace")
    stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    if drain_errors:
        channel, path, cause = drain_errors[0]
        error = OSError(f"could not durably append model {channel} to {path}: {cause}")
        error.add_note(f"captured stdout bytes: {len(stdout.encode('utf-8'))}")
        error.add_note(f"captured stderr bytes: {len(stderr.encode('utf-8'))}")
        raise error from cause
    if timed_out:
        raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
    if output_limit_hit.is_set():
        raise ProcessOutputLimit(command, stdout, stderr, max_output_bytes)
    if cancelled:
        raise ProcessCancelled(command, stdout, stderr)
    if interrupted is not None:
        raise interrupted
    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
