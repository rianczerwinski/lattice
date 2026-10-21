"""Claude Code provider-specific execution boundary."""

from __future__ import annotations

import json
import queue


def is_synthetic_error(message: dict) -> bool:
    """The CLI's marked API-error placeholder is not an inference model.

    Preserve the error as evidence; unmarked placeholders and real model IDs
    still participate in exact-model enforcement.
    """
    native = message.get("message")
    return (
        message.get("type") == "assistant"
        and message.get("is_api_error_message") is True
        and isinstance(native, dict)
        and native.get("model") == "<synthetic>"
    )


def encode_user_message(prompt: str) -> str:
    """Frame one unchanged user message using the official streaming input."""
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": prompt},
        "parent_tool_use_id": None,
        "session_id": "",
    }, ensure_ascii=False, separators=(",", ":")) + "\n"


class StreamingInput:
    """Keep stdin open until the CLI has emitted its complete result frame.

    Closing immediately after a request can race asynchronous stdout flushing.
    This controller supplies no further turns or tool approvals. The ordinary
    terminal parser still decides success, including errors and duplicate results.
    The guarded runner admits the output byte budget before calling observe().
    An invalid frame closes input without killing the native error reporter;
    its nonzero exit can still distinguish authentication/capacity failures.
    """

    def __init__(self, initial_message: str) -> None:
        self.messages: queue.Queue[bytes | None] = queue.Queue(maxsize=2)
        self.messages.put_nowait(initial_message.encode("utf-8"))
        self._pending = bytearray()
        self._scan_from = 0
        self._closed = False
        self.protocol_error: str | None = None

    def _close(self) -> None:
        if not self._closed:
            self._closed = True
            self.messages.put_nowait(None)

    def finish(self) -> None:
        """Validate an unterminated final frame after the process has exited."""
        if self._pending:
            self.observe("stdout", b"\n")

    def observe(self, channel: str, chunk: bytes) -> None:
        if channel != "stdout":
            return
        self._pending.extend(chunk)
        while (end := self._pending.find(b"\n", self._scan_from)) >= 0:
            frame = bytes(self._pending[:end])
            del self._pending[:end + 1]
            self._scan_from = 0
            if not frame.strip():
                continue
            try:
                value = json.loads(frame.decode("utf-8", errors="strict"))
            except (UnicodeError, ValueError, RecursionError):
                self.protocol_error = "Claude emitted an invalid UTF-8 JSON-line frame"
                self._close()
                continue
            if not isinstance(value, dict):
                self.protocol_error = "Claude emitted a nonobject native frame"
                self._close()
                continue
            if value.get("type") == "result":
                self._close()
        # A large tool record can span many chunks. Scan each byte once instead
        # of repeatedly rescanning the growing unfinished frame.
        self._scan_from = len(self._pending)


CLAUDE_REQUIRED_OPTIONS = (
    "--print", "--model", "--effort", "--output-format", "--json-schema", "--safe-mode",
    "--setting-sources", "--strict-mcp-config", "--mcp-config", "--disable-slash-commands",
    "--tools", "--allowedTools", "--permission-mode", "--no-chrome", "--no-session-persistence",
    "--resume", "--fork-session", "--system-prompt-file", "--settings", "--include-partial-messages", "--input-format",
)
