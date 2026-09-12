"""Official Claude SDK I/O over Lattice's guarded, raw-journaled process.

The SDK owns control messages and permission responses. This transport owns
UTF-8 JSON-line framing only; it neither grants tools nor claims native receipt
from bytes accepted into stdin. Callers must qualify the command and route.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
import os
from pathlib import Path
import queue
import subprocess
import threading
from typing import Any

from claude_agent_sdk import Transport

from ..canonical import strict_json_loads
from .journal import write_all, write_new_file
from .process import MAX_MODEL_OUTPUT_BYTES, MAX_PROCESS_INPUT_BYTES, ProcessInputLimit, run_bounded


class GuardedSDKTransport(Transport):
    """One connection, one owned process; reconnect requires a new journal."""

    def __init__(
        self, command: list[str], *, cwd: Path, env: Mapping[str, str],
        journal_root: Path, timeout: float, cancel_event: threading.Event,
        on_chunk: Callable[[str, bytes], None] | None = None,
        max_input_bytes: int = MAX_PROCESS_INPUT_BYTES,
        max_output_bytes: int = MAX_MODEL_OUTPUT_BYTES,
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.env = env
        self.root = journal_root
        self.timeout = timeout
        self.cancel = cancel_event
        self.on_chunk = on_chunk
        self.max_input_bytes = max_input_bytes
        self.max_output_bytes = max_output_bytes
        # Limit entry overhead as well as bytes. Reserve one slot for EOF so
        # shutdown never waits on a provider that has stopped consuming input.
        self._inputs: queue.Queue[bytes | None] = queue.Queue(maxsize=64)
        self._write_lock = asyncio.Lock()
        self._received: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._input_closed = False
        self._accepted_bytes = 0
        self.result: subprocess.CompletedProcess[str] | None = None
        self.failure: BaseException | None = None

    async def connect(self) -> None:
        if self._task is not None:
            raise RuntimeError("native transport cannot reconnect or reuse a journal")
        write_new_file(self.root / "stdin.accepted.log", b"")
        loop = asyncio.get_running_loop()

        def observe(channel: str, chunk: bytes) -> None:
            if self.on_chunk is not None:
                self.on_chunk(channel, chunk)
            if channel == "stdout":
                loop.call_soon_threadsafe(self._received.put_nowait, chunk)

        def run() -> None:
            try:
                self.result = run_bounded(
                    self.command, timeout=self.timeout, input_messages=self._inputs,
                    cwd=self.cwd, env=self.env, stdout_journal=self.root / "stdout.log",
                    stderr_journal=self.root / "stderr.log", cancel_event=self.cancel,
                    on_chunk=observe, max_input_bytes=self.max_input_bytes,
                    max_output_bytes=self.max_output_bytes,
                )
                if self.result.returncode:
                    raise subprocess.CalledProcessError(self.result.returncode, self.command)
            except BaseException as error:
                self.failure = error
            finally:
                loop.call_soon_threadsafe(self._received.put_nowait, None)

        self._task = asyncio.create_task(asyncio.to_thread(run))

    async def write(self, data: str) -> None:
        if not self.is_ready():
            raise RuntimeError("native transport is not accepting input")
        raw = data.encode("utf-8", errors="strict")
        if not raw:
            return
        async with self._write_lock:
            while self._inputs.qsize() >= self._inputs.maxsize - 1 and self.is_ready():
                # Only this locked coroutine adds payloads; the process thread
                # only removes them. Recheck liveness after yielding for space.
                await asyncio.sleep(0.01)
            if not self.is_ready():
                raise RuntimeError("native transport is not accepting input")
            if self._accepted_bytes + len(raw) > self.max_input_bytes:
                self.cancel.set()
                raise ProcessInputLimit("SDK input exceeds the admitted cumulative byte limit")
            # No await between durable acceptance and enqueue: journal order
            # equals FIFO order, including callbacks waiting under backpressure.
            try:
                descriptor = os.open(self.root / "stdin.accepted.log", os.O_APPEND | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    write_all(descriptor, raw)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except BaseException:
                self.cancel.set()
                raise
            self._accepted_bytes += len(raw)
            self._inputs.put_nowait(raw)

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        pending = bytearray()
        try:
            while True:
                chunk = await self._received.get()
                if chunk is None:
                    if self.failure is not None:
                        raise self.failure
                    if pending:
                        raise ValueError("native stream ended inside a JSON line")
                    return
                pending.extend(chunk)
                while b"\n" in pending:
                    raw, _, pending = pending.partition(b"\n")
                    if not raw.strip():
                        continue
                    value = strict_json_loads(raw.decode("utf-8", errors="strict"))
                    if not isinstance(value, dict):
                        raise ValueError("native stream message must be an object")
                    yield value
        except Exception:
            # The SDK cancels its reader before closing stdin. Reader-task
            # cancellation/GeneratorExit are not operator cancellation; actual
            # framing or process failures still stop the owned process.
            self.cancel.set()
            raise

    async def close(self) -> None:
        if self._task is None:
            return
        try:
            await self.end_input()
            try:
                # Give a well-behaved native peer time to finish after EOF; the
                # existing process runner owns group teardown and its bounds.
                await asyncio.wait_for(asyncio.shield(self._task), timeout=2)
            except TimeoutError:
                self.cancel.set()
                await asyncio.wait_for(asyncio.shield(self._task), timeout=8)
        finally:
            # SDK's AnyIO shield cannot prevent raw asyncio cancellation.
            # The process thread observes this signal even if close is cut off.
            self.cancel.set()

    def is_ready(self) -> bool:
        return self._task is not None and not self._task.done() and not self._input_closed and not self.cancel.is_set()

    async def end_input(self) -> None:
        if not self._input_closed:
            self._input_closed = True
            self._inputs.put_nowait(None)
