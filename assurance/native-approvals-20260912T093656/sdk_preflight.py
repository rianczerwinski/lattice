"""No-model SDK initialization through Lattice's owned, raw-journaled process."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys
import threading
import time

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, PermissionResultDeny

from lattice.execution.adapters import subscription_environment
from lattice.execution.journal import write_new_json
from lattice.execution.sdk_transport import GuardedSDKTransport
import lattice.execution.sdk_transport as transport_module
import lattice.execution.process as process_module



async def main(root: Path):
    if root.exists():
        raise FileExistsError("a native observation home cannot be reused")
    root.mkdir()
    binary = Path("/opt/homebrew/bin/claude")
    command = [str(binary), "--print", "--model", "claude-opus-5", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--permission-prompt-tool", "stdio", "--permission-mode", "default", "--safe-mode", "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands", "--tools", "", "--allowedTools", "", "--no-chrome", "--no-session-persistence"]
    write_new_json(root / "start.log", {
        "started_at": time.strftime("%Y%m%dT%H%M%S", time.gmtime()), "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "process_sha256": hashlib.sha256(Path(process_module.__file__).read_bytes()).hexdigest(), "sdk_version": importlib.metadata.version("claude-agent-sdk"),
        "sdk_transport_sha256": hashlib.sha256(Path(transport_module.__file__).read_bytes()).hexdigest(),
        "lock_sha256": hashlib.sha256((Path(process_module.__file__).parents[2] / "uv.lock").read_bytes()).hexdigest(),
        "binary": str(binary.resolve()), "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(), "command": command,
        "timeout_seconds": 30, "max_input_bytes": 1048576, "max_output_bytes": 1048576, "model_prompts": 0, "retention": "keep",
        "claim": "SDK/native initialization compatibility only; not authentication or an approval decision",
    })
    requested = []

    async def deny_unexpected(tool_name, tool_input, context):
        requested.append({"tool_name": tool_name, "tool_use_id": context.tool_use_id})
        return PermissionResultDeny(message="No tool action is authorized by this initialization probe", interrupt=True)

    transport = GuardedSDKTransport(command, cwd=root, env=subscription_environment(), journal_root=root, timeout=30, cancel_event=threading.Event(), max_input_bytes=1048576, max_output_bytes=1048576)
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
