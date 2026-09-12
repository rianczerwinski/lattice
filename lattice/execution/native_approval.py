"""Official SDK callback backed by exact-action operator decisions.

Run this callback on the provider worker's asyncio loop, not the HTTP loop.
Short synchronous ledger transactions cannot be abandoned halfway through by
SDK cancellation. Native callbacks request/consume authority; never create it.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

from .. import native_approvals as ledger
from ..contracts import NativeToolActionV1
from ..store import LatticeConflict

if TYPE_CHECKING:
    from ..store import LatticeStore


class NativeApprovalCallback:
    """One owned attempt; the adapter must call raise_if_failed at termination."""

    def __init__(self, store: LatticeStore, attempt_id: str, *, owner_boot_id: str, cancel_event: threading.Event) -> None:
        self.store = store
        self.attempt_id = attempt_id
        self.owner = owner_boot_id
        self.cancel = cancel_event
        self.errors: list[Exception] = []

    def _fail(self, error: Exception) -> None:
        # SDK may encode a callback exception as a control response and continue
        # its model loop. Preserve the failure independently and stop the peer.
        self.errors.append(error)
        self.cancel.set()

    def raise_if_failed(self) -> None:
        if self.errors:
            raise ExceptionGroup("native approval callback failed", self.errors)

    def _check_cancelled(self) -> None:
        if self.cancel.is_set():
            raise asyncio.CancelledError("owning invocation cancelled")

    async def __call__(self, tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext) -> PermissionResultAllow | PermissionResultDeny:
        item = None
        released = False
        reason = "native callback ended before delivery"
        try:
            self._check_cancelled()
            action = NativeToolActionV1(
                tool_use_id=context.tool_use_id,
                tool_name=tool_name,
                tool_input=tool_input,
                context={
                    **{key: getattr(context, key) for key in ("agent_id", "blocked_path", "decision_reason", "title", "display_name", "description")},
                    "suggestions": [suggestion.to_dict() for suggestion in context.suggestions],
                },
            )
            item = ledger.request(self.store, self.attempt_id, owner_boot_id=self.owner, action=action)
            while True:
                self._check_cancelled()
                view = ledger.inspect(self.store, self.attempt_id)
                self._check_cancelled()
                current = next(request for request in view["requests"] if request["request_id"] == item["request_id"])
                if current["state"] == "decided":
                    decision = ledger.take_decision(self.store, self.attempt_id, item["request_id"], owner_boot_id=self.owner, request_sha256=item["request_sha256"])
                    if decision is None:
                        raise LatticeConflict("decided native approval disappeared before release")
                    released = True
                    # Another thread can cancel during synchronous ledger I/O.
                    # A committed release remains consumed, but known cancelled
                    # authority must not return an SDK response. The transport
                    # independently checks the same event before admission.
                    self._check_cancelled()
                    if decision["decision"]["decision"] == "allow":
                        # Read the persisted exact action, never the caller's
                        # mutable dictionary or a native permission suggestion.
                        return PermissionResultAllow(updated_input=decision["binding"]["action"]["tool_input"])
                    return PermissionResultDeny(message=decision["decision"]["note"] or "Operator denied this exact action")
                if current["state"] != "awaiting-operator":
                    raise LatticeConflict(f"native approval is {current['state']}")
                # Read snapshots while waiting; acquire the write transaction
                # only for the actual one-use release. The HTTP loop is separate.
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            reason = "native callback cancelled"
            raise
        except Exception as error:
            reason = "native callback failed"
            self._fail(error)
            raise
        finally:
            if item is not None and not released:
                try:
                    ledger.withdraw(self.store, self.attempt_id, item["request_id"], owner_boot_id=self.owner, reason=reason)
                except Exception as error:
                    self._fail(error)
                    raise
