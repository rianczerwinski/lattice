"""Operator-gated runner changes, recorded in the existing append-only ledger.

Domain callers supply failure and task-qualification evidence; Lattice preserves
the exact evidence, pauses dispatch, and requires a separate operator decision.
It cannot infer linguistic competence or human intent from an API credential.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .canonical import opaque_id
from .contracts import RouteAmendmentDecision, RouteAmendmentRequest

if TYPE_CHECKING:
    import sqlite3
    from .store import LatticeStore


def runner(route: dict[str, Any]) -> tuple[str, str, str]:
    return route["provider"], route["harness"], route["model"]


def state(conn: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    history = []
    current_route = None
    pending = None
    for row in conn.execute(
        """SELECT event_id,event_type,payload_json,occurred_at FROM events
           WHERE subject_type='role-session' AND subject_id=?
             AND event_type IN ('role-route.requested','role-route.decided') ORDER BY seq""",
        (session_id,),
    ):
        item = {**json.loads(row["payload_json"]), "event_id": row["event_id"], "occurred_at": row["occurred_at"]}
        history.append(item)
        if row["event_type"] == "role-route.requested":
            pending = item
            current_route = item["previous_route"]
        else:
            current_route = item["effective_route"]
            pending = None
    return {"role_session_id": session_id, "state": "awaiting-operator" if pending else "active", "current_route": current_route, "pending": pending, "history": history}


def assert_route(conn: sqlite3.Connection, session_id: str, route: dict[str, Any]) -> dict[str, Any] | None:
    from .store import LatticeConflict

    view = state(conn, session_id)
    if view["pending"] is not None:
        raise LatticeConflict(f"role session awaits operator route decision: {view['pending']['amendment_id']}")
    approved = view["current_route"]
    if approved is not None and runner(approved) != runner(route):
        raise LatticeConflict("runner change requires a recorded route amendment")
    return approved


def inspect(store: LatticeStore, session_id: str) -> dict[str, Any]:
    store.get_role_session(session_id)
    with store._connect() as conn:
        return state(conn, session_id)


def _evidence(conn: sqlite3.Connection, block_id: str, project_id: str) -> dict[str, Any]:
    from .store import LatticeConflict

    row = conn.execute(
        """SELECT b.block_id,b.project_id,b.content_sha256,s.status,s.revision
           FROM context_blocks b JOIN block_status s USING(block_id) WHERE b.block_id=?""", (block_id,),
    ).fetchone()
    if row is None or row["project_id"] != project_id or row["status"] != "active":
        raise LatticeConflict("route amendment evidence must be active and belong to the session project")
    return dict(row)


def _idle(conn: sqlite3.Connection, session_id: str) -> None:
    from .store import LatticeConflict

    if conn.execute(
        """SELECT 1 FROM leases l JOIN invocations i USING(invocation_id)
           WHERE i.role_session_id=? AND l.state='active' LIMIT 1""", (session_id,),
    ).fetchone():
        raise LatticeConflict("finish or cancel the active role invocation before changing its runner")


def request(store: LatticeStore, session_id: str, value: RouteAmendmentRequest) -> dict[str, Any]:
    from .store import LatticeConflict

    session = store.get_role_session(session_id)
    proposed = value.proposed_route.model_dump(mode="json")
    if proposed["route_kind"] != "execution" or proposed["execution_profile"] == "native-continuation":
        raise LatticeConflict("a runner switch requires a fresh executable route with portable context")
    store._assert_actor_route_authorized("direct", None, proposed)
    with store.transaction() as conn:
        source = conn.execute("SELECT * FROM invocations WHERE invocation_id=?", (value.source_invocation_id,)).fetchone()
        if source is None or source["role_session_id"] != session_id:
            raise LatticeConflict("route amendment source must belong to this role session")
        if source["state"] not in {"succeeded", "failed", "interrupted"}:
            raise LatticeConflict("route amendment requires a terminal source invocation")
        _idle(conn, session_id)
        view = state(conn, session_id)
        if view["pending"]:
            if view["pending"]["request"] == value.model_dump(mode="json"):
                return view
            raise LatticeConflict("role session already awaits an operator route decision")
        previous = view["current_route"] or json.loads(source["route_json"])
        if runner(previous) != runner(json.loads(source["route_json"])):
            raise LatticeConflict("failure invocation does not name the current runner")
        if runner(previous) == runner(proposed):
            raise LatticeConflict("a route amendment must change the runner")
        evidence = [_evidence(conn, block_id, session["project_id"]) for block_id in (value.failure_evidence_block_id, value.qualification_evidence_block_id)]
        if evidence[0]["block_id"] == evidence[1]["block_id"]:
            raise LatticeConflict("failure and task qualification require separate evidence blocks")
        store._event(conn, project_id=session["project_id"], event_type="role-route.requested", subject_type="role-session", subject_id=session_id, payload={
            "amendment_id": opaque_id("route-amendment"), "request": value.model_dump(mode="json"),
            "source_manifest_sha256": source["manifest_sha256"], "previous_route": previous, "proposed_route": proposed,
            "evidence": evidence, "activation_policy": "operator-approval", "state": "awaiting-operator",
        })
        return state(conn, session_id)


def decide(store: LatticeStore, session_id: str, amendment_id: str, value: RouteAmendmentDecision) -> dict[str, Any]:
    from .store import LatticeConflict

    session = store.get_role_session(session_id)
    with store.transaction() as conn:
        view = state(conn, session_id)
        pending = view["pending"]
        if pending is None or pending["amendment_id"] != amendment_id:
            # A repeated delivery is a read, never a second activation.
            prior = next((item for item in reversed(view["history"]) if item.get("amendment_id") == amendment_id and "decision" in item), None)
            if prior is not None and prior["decision"] == value.decision and prior["note"] == value.note:
                return view
            raise LatticeConflict("route decision must name the current pending amendment")
        _idle(conn, session_id)
        if value.decision == "approve":
            for evidence in pending["evidence"]:
                if _evidence(conn, evidence["block_id"], session["project_id"]) != evidence:
                    raise LatticeConflict("route amendment evidence changed; approval refused")
        effective = pending["proposed_route"] if value.decision == "approve" else pending["previous_route"]
        store._event(conn, project_id=session["project_id"], event_type="role-route.decided", subject_type="role-session", subject_id=session_id, payload={
            "amendment_id": amendment_id, "request_event_id": pending["event_id"], **value.model_dump(mode="json"),
            "authority": "operator", "effective_route": effective, "state": "active",
        })
        return state(conn, session_id)
