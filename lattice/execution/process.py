"""Subprocess execution with live raw journaling and checkpoint projection."""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Mapping

from .checkpoint import observe_chunk, observe_sidecar
from .journal import write_all


MAX_MODEL_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_PROCESS_INPUT_BYTES = 64 * 1024 * 1024
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


class ProcessInputLimit(RuntimeError):
    """A streaming sender exceeded the admitted input budget before delivery."""


def terminate_process_group(proc: subprocess.Popen[object]) -> None:
    # wait() observes only the leader. Always escalate the owned group, including
    # descendants that ignore TERM and redirect all three standard streams.
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (PermissionError, ProcessLookupError):
        if proc.poll() is None:
            proc.terminate()
    try:
        proc.wait(timeout=2)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except PermissionError:
            if proc.poll() is None:
                proc.kill()
            raise
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass


@contextmanager
def _owned_process(command: list[str], *, has_input: bool, cwd: Path | None, env: Mapping[str, str] | None) -> Iterator[tuple[subprocess.Popen[bytes], Callable[[], None]]]:
    """Keep process ownership outside every fallible observation/setup path."""
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if has_input else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )
    watchdog = None
    stopped = False

    def stop() -> None:
        nonlocal stopped
        if not stopped:
            terminate_process_group(proc)
            stopped = True

    try:
        watchdog = subprocess.Popen(
            [sys.executable, "-c", _PARENT_DEATH_WATCHDOG, str(os.getpid()), str(proc.pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        yield proc, stop
    finally:
        try:
            stop()
        finally:
            try:
                if watchdog is not None and watchdog.poll() is None:
                    watchdog.terminate()
                    try:
                        watchdog.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        watchdog.kill()
                        watchdog.wait(timeout=2)
            finally:
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None:
                        stream.close()


def _join_drain_thread(thread: threading.Thread | None, stop: Callable[[], None], *, timeout: float) -> bool:
    if thread is None:
        return False
    thread.join(timeout=max(timeout, 0.001))
    retained_pipe = thread.is_alive()
    if thread.is_alive():
        stop()
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
    input_messages: queue.Queue[bytes | None] | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    stdout_journal: Path | None = None,
    stderr_journal: Path | None = None,
    observed_files: Mapping[str, Path] | None = None,
    cancel_event: threading.Event | None = None,
    on_chunk: Callable[[str, bytes], None] | None = None,
    max_output_bytes: int = MAX_MODEL_OUTPUT_BYTES,
    max_input_bytes: int = MAX_PROCESS_INPUT_BYTES,
) -> subprocess.CompletedProcess[str]:
    """Run one owned process; an optional queue keeps its input channel open.

    Queue messages are bytes, in FIFO order; None closes input. Empty queues do
    not mean EOF. Child exit, cancellation and timeout stop the writer without
    requiring a sentinel. Queue delivery is not a native action acknowledgement.
    Protocol framing, permission decisions and their durable records belong to
    the caller. The transport never interprets or grants permission.
    """
    if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int) or max_output_bytes < 1:
        raise ValueError("max_output_bytes must be a positive integer")
    if isinstance(max_input_bytes, bool) or not isinstance(max_input_bytes, int) or max_input_bytes < 1:
        raise ValueError("max_input_bytes must be a positive integer")
    if input_text is not None and input_messages is not None:
        raise ValueError("input_text and input_messages are mutually exclusive")
    initial_input = None if input_text is None else input_text.encode("utf-8")
    if initial_input is not None and len(initial_input) > max_input_bytes:
        raise ProcessInputLimit(f"process input exceeds its {max_input_bytes}-byte input limit")
    if cancel_event is not None and cancel_event.is_set():
        raise ProcessCancelled(command, "", "")
    with _owned_process(command, has_input=initial_input is not None or input_messages is not None, cwd=cwd, env=env) as (proc, stop):
        root = _journal_root(stdout_journal, stderr_journal)
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        drain_errors: list[tuple[str, Path, BaseException]] = []
        input_errors: list[BaseException] = []
        input_stop = threading.Event()
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
                if input_messages is None:
                    proc.stdin.write(initial_input or b"")
                    proc.stdin.flush()
                    return
                input_bytes = 0
                while not input_stop.is_set() and proc.poll() is None:
                    try:
                        message = input_messages.get(timeout=0.02)
                    except queue.Empty:
                        continue
                    if message is None:
                        break
                    if not isinstance(message, bytes):
                        raise TypeError("process input messages must be bytes or None")
                    if input_stop.is_set() or (cancel_event is not None and cancel_event.is_set()):
                        break
                    input_bytes += len(message)
                    if input_bytes > max_input_bytes:
                        raise ProcessInputLimit(f"process input exceeds its {max_input_bytes}-byte input limit")
                    proc.stdin.write(message)
                    proc.stdin.flush()
            except BrokenPipeError:
                # The native process owns EOF; the caller must inspect its terminal
                # result rather than treating enqueued input as acknowledged work.
                pass
            except BaseException as exc:  # noqa: BLE001 - process input boundary
                input_errors.append(exc)
            finally:
                try:
                    proc.stdin.close()
                except BrokenPipeError:
                    pass  # Flush on close can observe the same native EOF.
                except OSError as exc:
                    input_errors.append(exc)

        input_thread: threading.Thread | None = None
        if input_text is not None or input_messages is not None:
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
        observation_failed = False
        try:
            while proc.poll() is None:
                refresh_sidecars()
                if drain_errors or input_errors:
                    stop()
                    break
                if output_limit_hit.is_set():
                    stop()
                    break
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    stop()
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    timed_out = True
                    stop()
                    break
                time.sleep(0.02)
        except BaseException:  # noqa: BLE001 - stop owned work before draining on any observation failure
            observation_failed = True
            stop()
            raise
        finally:
            input_stop.set()
            join_timeout = 2.0 if deadline is None else min(2.0, max(0.001, deadline - time.monotonic()))
            # An idle queue reader needs its bounded wake-up after input_stop, even
            # when the execution deadline has elapsed. It is not a retained pipe.
            retained_pipe = _join_drain_thread(input_thread, stop, timeout=2.0)
            retained_pipe = _join_drain_thread(stdout_thread, stop, timeout=join_timeout) or retained_pipe
            retained_pipe = _join_drain_thread(stderr_thread, stop, timeout=join_timeout) or retained_pipe
            if not observation_failed:
                refresh_sidecars()

        if retained_pipe and not timed_out and not cancelled and not output_limit_hit.is_set() and not drain_errors and not input_errors:
            raise OSError("provider child exited while an unowned descendant retained its output pipe")

        stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace")
        stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        if input_errors:
            raise input_errors[0]
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
        return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
