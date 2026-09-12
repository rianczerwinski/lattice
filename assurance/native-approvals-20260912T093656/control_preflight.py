"""One local initialization exchange; no model prompt or tool approval."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path

from lattice.execution.adapters import subscription_environment
from lattice.execution.journal import write_new_json
from lattice.execution.process import run_bounded


def main() -> None:
    root = Path(__file__).resolve().parent
    binary = Path("/opt/homebrew/bin/claude")
    request = {"type": "control_request", "request_id": "lattice-init-20260912T093656", "request": {"subtype": "initialize", "hooks": None}}
    command = [str(binary), "--print", "--model", "claude-opus-5", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--permission-prompt-tool", "stdio", "--permission-mode", "default", "--safe-mode", "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands", "--tools", "", "--allowedTools", "", "--no-chrome", "--no-session-persistence"]
    write_new_json(root / "start.log", {
        "recorded_at_utc": time.strftime("%Y%m%dT%H%M%S", time.gmtime()),
        "source_revision": "bbd9849be4b6074ad274da7ffb7a85a1978bcf4d",
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "binary": str(binary.resolve()),
        "binary_sha256": hashlib.file_digest(binary.open("rb"), "sha256").hexdigest(),
        "command": command,
        "request": request,
        "timeout_seconds": 30,
        "max_output_bytes": 1048576,
        "model_prompts": 0,
        "retention": "keep",
    })
    started = time.monotonic()
    try:
        result = run_bounded(command, timeout=30, input_text=json.dumps(request) + "\n", cwd=root, env=subscription_environment(), stdout_journal=root / "stdout.log", stderr_journal=root / "stderr.log", max_output_bytes=1048576)
        terminal = {"state": "process-exited", "returncode": result.returncode}
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as error:
        terminal = {"state": "process-failed", "error_type": type(error).__name__, "error": str(error)}
    terminal.update(elapsed_seconds=time.monotonic() - started, model_prompts=0)
    write_new_json(root / "terminal.log", terminal)
    print(json.dumps(terminal))


if __name__ == "__main__":
    main()
