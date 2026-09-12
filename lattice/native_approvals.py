"""One-action operator decisions in Lattice's existing append-only event ledger.

The native SDK callback may request or consume a decision, never create one.
The authenticated operator boundary owns ``decide``. A released decision means
the adapter claimed it once, not that the provider received or executed it.
Same-user credential/file isolation belongs to the qualified execution route;
this module does not claim to distinguish a human from a stolen credential.
"""

from __future__ import annotations

import json
import time
from time import monotonic
from typing import TYPE_CHECKING, Any

from .canonical import canonical_bytes, opaque_id, sha256_bytes
from .contracts import NativeApprovalDecisionV1, NativeToolActionV1

if TYPE_CHECKING:
    import sqlite3
    from .store import LatticeStore


# Match the runtime control-file boundary, without accepting unbounded native
# descriptions, permission suggestions, or tool arguments into operator views.
MAX_REQUEST_BYTES = 1024 * 1024
EVENT_TYPES = (
    "native-approval.requested",
    "native-approval.decided",
    "native-approval.released",
    "native-approval.withdrawn",
    "native-approval.closed",
)


def _attempt(conn: sqlite3.Connection, attempt_id: str) -> dict[str, Any]:
    from .store import LatticeNotFound

    row = conn.execute(
        """SELECT a.attempt_id,a.invocation_id,a.owner_boot_id,a.lease_id,a.state,
                  i.project_id,i.manifest_sha256,i.route_json,
                  l.state AS lease_state,l.expires_at,l.owner_boot_id AS lease_owner
           FROM attempts a JOIN invocations i USING(invocation_id)
           LEFT JOIN leases l ON l.lease_id=a.lease_id AND l.attempt_id=a.attempt_id
           WHERE a.attempt_id=?""", (attempt_id,),
    ).fetchone()
    if row is None:
        raise LatticeNotFound(attempt_id)
    return dict(row)


def _inactive_reason(attempt: dict[str, Any], deadline: float | None = None) -> str | None:
    if attempt["state"] != "running":
        return f"attempt is {attempt['state']}"
    if attempt["lease_state"] != "active":
        return "attempt lease is not active"
    if attempt["lease_owner"] != attempt["owner_boot_id"]:
        return "attempt and lease owners disagree"
    if deadline is not None:
        return "approval deadline expired" if monotonic() >= deadline else None
    if attempt["expires_at"] <= time.time():
        return "attempt lease expired"
    return None


def _active(attempt: dict[str, Any], owner_boot_id: str, deadline: float | None = None) -> None:
    from .store import LatticeConflict

    reason = _inactive_reason(attempt, deadline)
    if reason or attempt["owner_boot_id"] != owner_boot_id:
        raise LatticeConflict(reason or "approval attempt belongs to another supervisor")


def state(conn: sqlite3.Connection, attempt_id: str) -> dict[str, Any]:
    """Project from one caller-owned SQLite snapshot; never renew a stale grant."""
    attempt = _attempt(conn, attempt_id)
    requests: dict[str, dict[str, Any]] = {}
    closed_reason = None
    for row in conn.execute(
        """SELECT event_id,event_type,payload_json,occurred_at FROM events
           WHERE subject_type='attempt' AND subject_id=?
             AND event_type IN (?,?,?,?,?) ORDER BY seq""", (attempt_id, *EVENT_TYPES),
    ):
        payload = json.loads(row["payload_json"])
        event = {**payload, "event_id": row["event_id"], "occurred_at": row["occurred_at"]}
        kind = row["event_type"]
        if kind == "native-approval.closed":
            closed_reason = payload["reason"]
            continue
        request_id = payload["request_id"]
        if kind == "native-approval.requested":
            requests[request_id] = {**event, "state": "awaiting-operator", "decision": None, "release": None, "withdrawal": None}
        elif kind == "native-approval.decided":
            requests[request_id].update(decision=event, state="decided")
        elif kind == "native-approval.released":
            requests[request_id].update(release=event, state="released-to-adapter")
        elif kind == "native-approval.withdrawn":
            requests[request_id].update(withdrawal=event, state="withdrawn")
    for request in requests.values():
        inactive = closed_reason or _inactive_reason(attempt, request["binding"]["deadline_monotonic"])
        if inactive and request["state"] in {"awaiting-operator", "decided"}:
            request.update(state="expired", inactive_reason=inactive)
    return {
        "attempt_id": attempt_id, "invocation_id": attempt["invocation_id"],
        "requests": list(requests.values()), "closed_reason": closed_reason,
        "pending_count": sum(item["state"] == "awaiting-operator" for item in requests.values()),
    }


def inspect(store: LatticeStore, attempt_id: str) -> dict[str, Any]:
    with store._connect() as conn:
        conn.execute("BEGIN")
        try:
            return state(conn, attempt_id)
        finally:
            conn.rollback()


def _find(view: dict[str, Any], request_id: str) -> dict[str, Any]:
    from .store import LatticeNotFound

    for request in view["requests"]:
        if request["request_id"] == request_id:
            return request
    raise LatticeNotFound(request_id)


def request(store: LatticeStore, attempt_id: str, *, owner_boot_id: str, action: NativeToolActionV1) -> dict[str, Any]:
    from .store import LatticeConflict

    material = action.model_dump(mode="json")
    encoded = canonical_bytes(material)
    if len(encoded) > MAX_REQUEST_BYTES:
        raise LatticeConflict(f"native approval request exceeds {MAX_REQUEST_BYTES} bytes")
    with store.transaction() as conn:
        attempt = _attempt(conn, attempt_id)
        _active(attempt, owner_boot_id)
        route = json.loads(attempt["route_json"])
        if route["provider"] != "anthropic" or route["harness"] != "claude" or route["approval_policy"] != "on-request":
            raise LatticeConflict("invocation has no native interactive approval authority")
        if action.tool_name not in route["tools"]:
            raise LatticeConflict("native tool is outside the sealed invocation tool surface")
        binding = {
            "attempt_id": attempt_id, "invocation_id": attempt["invocation_id"],
            "owner_boot_id": owner_boot_id, "lease_id": attempt["lease_id"],
            "manifest_sha256": attempt["manifest_sha256"], "action": material,
        }
        view = state(conn, attempt_id)
        if view["closed_reason"] is not None:
            raise LatticeConflict("native approval channel is closed for this attempt")
        for prior in view["requests"]:
            if prior["binding"]["action"]["tool_use_id"] == action.tool_use_id:
                if any(prior["binding"][key] != value for key, value in binding.items()):
                    raise LatticeConflict("native tool-use identity was reused for a different action")
                return prior
        # Like asyncio deadlines, this duration uses the non-adjustable clock.
        # It belongs to one supervisor boot: restart/orphan recovery invalidates
        # it, and duplicate callbacks never renew it. Capping by lease_seconds
        # prevents a wall-clock correction from inflating initial authority.
        binding["deadline_monotonic"] = monotonic() + min(store.config.lease_seconds, max(0.0, attempt["expires_at"] - time.time()))
        digest = sha256_bytes(canonical_bytes(binding))
        request_id = opaque_id("native-approval")
        store._event(conn, project_id=attempt["project_id"], event_type="native-approval.requested", subject_type="attempt", subject_id=attempt_id, payload={
            "request_id": request_id, "invocation_id": attempt["invocation_id"],
            "request_sha256": digest, "binding": binding,
        })
        return _find(state(conn, attempt_id), request_id)


def _bound(request: dict[str, Any], attempt: dict[str, Any], request_sha256: str) -> None:
    from .store import LatticeConflict

    binding = request["binding"]
    if request["request_sha256"] != request_sha256 or sha256_bytes(canonical_bytes(binding)) != request_sha256:
        raise LatticeConflict("approval does not match the exact requested action")
    if any(binding[key] != attempt[key] for key in ("attempt_id", "invocation_id", "owner_boot_id", "lease_id", "manifest_sha256")):
        raise LatticeConflict("approval binding is stale")


def decide(store: LatticeStore, attempt_id: str, request_id: str, value: NativeApprovalDecisionV1, *, owner_boot_id: str) -> dict[str, Any]:
    """Authenticated operator entry point; cannot edit inputs or persist rules."""
    from .store import LatticeConflict

    with store.transaction() as conn:
        attempt = _attempt(conn, attempt_id)
        item = _find(state(conn, attempt_id), request_id)
        _bound(item, attempt, value.request_sha256)
        decision = value.model_dump(mode="json")
        if item["decision"] is not None:
            if all(item["decision"][key] == val for key, val in decision.items()):
                return item  # Lost HTTP response: observation only, not a new grant.
            raise LatticeConflict("an operator decision is immutable")
        _active(attempt, owner_boot_id, item["binding"]["deadline_monotonic"])
        if item["state"] != "awaiting-operator":
            raise LatticeConflict("native approval request is no longer pending")
        store._event(conn, project_id=attempt["project_id"], event_type="native-approval.decided", subject_type="attempt", subject_id=attempt_id, payload={
            "request_id": request_id, "invocation_id": attempt["invocation_id"],
            **decision, "authority": "operator", "request_event_id": item["event_id"],
        })
        return _find(state(conn, attempt_id), request_id)


def take_decision(store: LatticeStore, attempt_id: str, request_id: str, *, owner_boot_id: str, request_sha256: str) -> dict[str, Any] | None:
    """Claim one decided callback response; no replay after a crash or duplicate."""
    from .store import LatticeConflict

    with store.transaction() as conn:
        attempt = _attempt(conn, attempt_id)
        item = _find(state(conn, attempt_id), request_id)
        _active(attempt, owner_boot_id, item["binding"]["deadline_monotonic"])
        _bound(item, attempt, request_sha256)
        if item["state"] == "awaiting-operator":
            return None
        if item["state"] != "decided":
            raise LatticeConflict("native approval is not available for delivery")
        store._event(conn, project_id=attempt["project_id"], event_type="native-approval.released", subject_type="attempt", subject_id=attempt_id, payload={
            "request_id": request_id, "invocation_id": attempt["invocation_id"],
            "request_sha256": request_sha256, "decision_event_id": item["decision"]["event_id"],
            "decision": item["decision"]["decision"], "delivery_claim": "released-to-adapter",
        })
        return _find(state(conn, attempt_id), request_id)


def withdraw(store: LatticeStore, attempt_id: str, request_id: str, *, owner_boot_id: str, reason: str) -> None:
    """A native cancelled callback invalidates only its action, not its siblings."""
    from .store import LatticeConflict

    if not reason or len(reason) > 4000:
        raise ValueError("withdrawal requires a bounded reason")
    with store.transaction() as conn:
        attempt = _attempt(conn, attempt_id)
        if attempt["owner_boot_id"] != owner_boot_id:
            raise LatticeConflict("approval attempt belongs to another supervisor")
        item = _find(state(conn, attempt_id), request_id)
        if item["state"] not in {"awaiting-operator", "decided"}:
            return
        store._event(conn, project_id=attempt["project_id"], event_type="native-approval.withdrawn", subject_type="attempt", subject_id=attempt_id, payload={
            "request_id": request_id, "invocation_id": attempt["invocation_id"], "reason": reason,
        })


def close_in_transaction(store: LatticeStore, conn: sqlite3.Connection, attempt_id: str, *, reason: str) -> None:
    """Close before cancellation or in the same transaction as attempt terminality."""
    view = state(conn, attempt_id)
    if view["closed_reason"] is not None or not view["requests"]:
        return
    attempt = _attempt(conn, attempt_id)
    store._event(conn, project_id=attempt["project_id"], event_type="native-approval.closed", subject_type="attempt", subject_id=attempt_id, payload={
        "invocation_id": attempt["invocation_id"], "reason": reason,
    })


def close(store: LatticeStore, attempt_id: str, *, owner_boot_id: str, reason: str) -> None:
    from .store import LatticeConflict

    with store.transaction() as conn:
        attempt = _attempt(conn, attempt_id)
        if attempt["owner_boot_id"] != owner_boot_id:
            raise LatticeConflict("approval attempt belongs to another supervisor")
        # Persist closure even before the first callback, so cancellation cannot
        # race a late request into a still-running attempt.
        view = state(conn, attempt_id)
        if view["closed_reason"] is None:
            store._event(conn, project_id=attempt["project_id"], event_type="native-approval.closed", subject_type="attempt", subject_id=attempt_id, payload={
                "invocation_id": attempt["invocation_id"], "reason": reason,
            })
