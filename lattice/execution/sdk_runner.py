"""One SDK response under the guarded process and exact-action ledger.

The production policy gate is separate. This runner never grants authority or
changes native permission rules; the pinned SDK owns the control protocol.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from pathlib import Path
import subprocess
import threading
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage

from .journal import write_new_json
from .process import ProcessCancelled
from .sdk_transport import GuardedSDKTransport

if TYPE_CHECKING:
    from .native_approval import NativeApprovalCallback


class NativeSDKFailure(RuntimeError):
    """SDK or callback failure retained independently of native domain output."""


def _error_record(error: BaseException) -> dict[str, Any]:
    result = {"type": type(error).__name__, "message": str(error)}
    if isinstance(error, BaseExceptionGroup):
        result["causes"] = [_error_record(child) for child in error.exceptions]
    return result


async def _run(transport: GuardedSDKTransport, prompt: str, approval: NativeApprovalCallback) -> subprocess.CompletedProcess[str]:
    pending: set[asyncio.Task[Any]] = set()
    handler_errors: list[BaseException] = []

    def handler_done(task: asyncio.Task[Any]) -> None:
        pending.discard(task)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                handler_errors.append(error)

    async def callback(tool_name, tool_input, context):
        # SDK close cancels control handlers but does not join each one. Track
        # handlers through the response write too, not just our callback body.
        task = asyncio.current_task()
        pending.add(task)
        task.add_done_callback(handler_done)
        return await approval(tool_name, tool_input, context)

    client = ClaudeSDKClient(options=ClaudeAgentOptions(can_use_tool=callback), transport=transport)
    exchange_error = None
    finalizer_errors = []
    try:
        async with asyncio.timeout(transport.timeout):
            await client.connect()
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, ResultMessage):
                    break
    except BaseException as error:
        exchange_error = error
        transport.cancel.set()
    finally:
        try:
            await client.disconnect()
        except BaseException as error:
            finalizer_errors.append(error)
        finally:
            # connect() can fail before the SDK owns a Query. Always close the
            # supplied transport too; its repeated close is idempotent.
            try:
                await transport.close()
            except BaseException as error:
                finalizer_errors.append(error)
            tasks = list(pending)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        try:
            approval.raise_if_failed()
        except Exception as error:
            finalizer_errors.append(error)

    if transport.boundary_failure is not None:
        finalizer_errors.append(transport.boundary_failure)
    finalizer_errors.extend(error for error in handler_errors if not isinstance(error, ProcessCancelled))
    errors = ([exchange_error] if exchange_error is not None else []) + finalizer_errors
    errors.extend(error for error in handler_errors if error not in errors)
    if transport.failure is not None and transport.failure not in errors:
        errors.append(transport.failure)
    if errors:
        write_new_json(transport.root / "sdk-failure.json", {"errors": [_error_record(error) for error in errors]})
    if finalizer_errors:
        raise NativeSDKFailure("SDK finalization failed; see sdk-failure.json") from BaseExceptionGroup("SDK finalization", finalizer_errors)
    if isinstance(exchange_error, TimeoutError):
        raise subprocess.TimeoutExpired(transport.command, transport.timeout) from exchange_error
    # The SDK propagates transport errors unchanged. Preserve their existing
    # timeout/cancel/output-limit types instead of reporting empty output.
    if transport.failure is not None and (exchange_error is None or exchange_error is transport.failure or isinstance(exchange_error, ProcessCancelled)):
        if isinstance(transport.failure, subprocess.CalledProcessError) and transport.result is not None:
            return transport.result
        raise transport.failure
    if isinstance(exchange_error, ProcessCancelled) or any(isinstance(error, ProcessCancelled) for error in handler_errors):
        # Cancellation was observed at input admission even if the process won
        # its next poll and exited zero. Never discard that recorded outcome.
        result = transport.result
        raise ProcessCancelled(transport.command, result.stdout if result else "", result.stderr if result else "")
    if exchange_error is not None:
        raise NativeSDKFailure(f"SDK exchange failed: {type(exchange_error).__name__}: {exchange_error}") from exchange_error
    if transport.result is None:
        raise NativeSDKFailure("SDK transport ended without a process result")
    return transport.result


def run_native_approval(
    command: list[str], *, prompt: str, cwd: Path, env: Mapping[str, str],
    journal_root: Path, timeout: float, cancel_event: threading.Event,
    approval: NativeApprovalCallback, on_chunk: Callable[[str, bytes], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run on the supervisor's worker thread, never its HTTP event loop."""
    if approval.cancel is not cancel_event:
        raise ValueError("native approval and process must share cancellation ownership")
    wire = GuardedSDKTransport(command, cwd=cwd, env=env, journal_root=journal_root, timeout=timeout, cancel_event=cancel_event, on_chunk=on_chunk)
    return asyncio.run(_run(wire, prompt, approval))
