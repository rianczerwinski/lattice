"""No-model SDK initialization through Lattice's owned, raw-journaled process."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, PermissionResultDeny, Transport

from lattice.canonical import strict_json_loads
from lattice.execution.adapters import subscription_environment
from lattice.execution.journal import write_all, write_new_json
from lattice.execution.process import run_bounded
import lattice.execution.process as process_module


class ProbeTransport(Transport):
    """Diagnostic bridge: SDK owns protocol; the existing kernel owns the CLI."""

    def __init__(self, root: Path, command: list[str]):
        self.root = root
        self.command = command
        self.inputs = queue.Queue()
        self.received = asyncio.Queue()
        self.cancel = threading.Event()
        self.task = None
        self.failure = None
        self.result = None
        self.input_closed = False

    async def connect(self):
        if self.task is not None:
            raise RuntimeError("probe transport already connected")
        loop = asyncio.get_running_loop()

        def observe(channel, chunk):
            if channel == "stdout":
                loop.call_soon_threadsafe(self.received.put_nowait, chunk)

        def run():
            try:
                self.result = run_bounded(self.command, timeout=30, input_messages=self.inputs, cwd=self.root, env=subscription_environment(), stdout_journal=self.root / "stdout.log", stderr_journal=self.root / "stderr.log", max_output_bytes=1048576, max_input_bytes=1048576, cancel_event=self.cancel, on_chunk=observe)
                if self.result.returncode:
                    raise subprocess.CalledProcessError(self.result.returncode, self.command)
            except BaseException as error:
                self.failure = error
            finally:
                loop.call_soon_threadsafe(self.received.put_nowait, None)

        self.task = asyncio.create_task(asyncio.to_thread(run))

    async def write(self, data):
        if not self.is_ready():
            raise RuntimeError("probe transport is not accepting input")
        raw = data.encode("utf-8")
        descriptor = os.open(self.root / "stdin.log", os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            write_all(descriptor, raw)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.inputs.put_nowait(raw)

    async def read_messages(self):
        pending = bytearray()
        while True:
            chunk = await self.received.get()
            if chunk is None:
                if self.failure is not None:
                    raise self.failure
                if pending:
                    raise ValueError("native control stream ended inside a JSON line")
                return
            pending.extend(chunk)
            while b"\n" in pending:
                raw, _, remainder = pending.partition(b"\n")
                pending = bytearray(remainder)
                if not raw.strip():
                    continue
                value = strict_json_loads(raw.decode("utf-8", errors="strict"))
                if not isinstance(value, dict):
                    raise ValueError("native control message must be an object")
                yield value

    async def close(self):
        if self.task is None:
            return
        try:
            await self.end_input()
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=2)
            except TimeoutError:
                self.cancel.set()
                await asyncio.wait_for(asyncio.shield(self.task), timeout=8)
        finally:
            # Raw asyncio cancellation can interrupt SDK cleanup; the process
            # thread still observes this signal independently of the coroutine.
            self.cancel.set()

    def is_ready(self):
        return self.task is not None and not self.task.done() and not self.input_closed

    async def end_input(self):
        if not self.input_closed:
            self.input_closed = True
            self.inputs.put_nowait(None)


async def main(root: Path):
    if root.exists():
        raise FileExistsError("a native observation home cannot be reused")
    root.mkdir()
    binary = Path("/opt/homebrew/bin/claude")
    command = [str(binary), "--print", "--model", "claude-opus-5", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--permission-prompt-tool", "stdio", "--permission-mode", "default", "--safe-mode", "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands", "--tools", "", "--allowedTools", "", "--no-chrome", "--no-session-persistence"]
    write_new_json(root / "start.log", {
        "started_at": time.strftime("%Y%m%dT%H%M%S", time.gmtime()), "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "process_sha256": hashlib.sha256(Path(process_module.__file__).read_bytes()).hexdigest(), "sdk_version": importlib.metadata.version("claude-agent-sdk"),
        "binary": str(binary.resolve()), "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(), "command": command,
        "timeout_seconds": 30, "max_input_bytes": 1048576, "max_output_bytes": 1048576, "model_prompts": 0, "retention": "keep",
        "claim": "SDK/native initialization compatibility only; not authentication or an approval decision",
    })
    requested = []

    async def deny_unexpected(tool_name, tool_input, context):
        requested.append({"tool_name": tool_name, "tool_use_id": context.tool_use_id})
        return PermissionResultDeny(message="No tool action is authorized by this initialization probe", interrupt=True)

    transport = ProbeTransport(root, command)
    client = ClaudeSDKClient(options=ClaudeAgentOptions(can_use_tool=deny_unexpected), transport=transport)
    started = time.monotonic()
    try:
        await client.connect()
        info = await client.get_server_info()
        if not isinstance(info, dict):
            raise ValueError("SDK initialization returned no native information")
        terminal = {"state": "initialized", "permission_mode": info.get("current_permission_mode"), "session_state": info.get("session_state")}
    except BaseException as error:
        terminal = {"state": "failed", "error_type": type(error).__name__, "error": str(error)}
    finally:
        try:
            await client.disconnect()
        except BaseException as error:
            terminal["cleanup_error"] = {"type": type(error).__name__, "error": str(error)}
        terminal.update(elapsed_seconds=time.monotonic() - started, requested_tools=requested, model_prompts=0, process_returncode=None if transport.result is None else transport.result.returncode, process_error=None if transport.failure is None else f"{type(transport.failure).__name__}: {transport.failure}")
        write_new_json(root / "terminal.log", terminal)
    print(json.dumps(terminal))
    if terminal["state"] != "initialized" or terminal.get("cleanup_error") or terminal["process_error"] or requested:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]).resolve()))
