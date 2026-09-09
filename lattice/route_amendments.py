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
    current_scope_sets: list[list[str]] = []
    scope_state_valid = True
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
            current_scope_sets = item.get("previous_scope_sets", current_scope_sets)
        else:
            current_route = item["effective_route"]
            scope_state_valid = "effective_scope_sets" in item
            current_scope_sets = item.get("effective_scope_sets", [])
            pending = None
    route_state = "awaiting-operator" if pending else "active" if scope_state_valid else "scope-migration-required"
    return {"role_session_id": session_id, "state": route_state, "current_route": current_route, "current_scope_sets": current_scope_sets, "pending": pending, "history": history}


def assert_route(conn: sqlite3.Connection, session_id: str, route: dict[str, Any], route_scope_refs: list[str] | tuple[str, ...] = ()) -> dict[str, Any] | None:
    from .store import LatticeConflict

    view = state(conn, session_id)
    if view["state"] == "scope-migration-required":
        raise LatticeConflict("legacy unscoped route amendment requires an explicit scoped replacement before dispatch")
    if view["pending"] is not None:
        raise LatticeConflict(f"role session awaits operator route decision: {view['pending']['amendment_id']}")
    approved = view["current_route"]
    exact_scope = list(route_scope_refs)
    scope_matches = exact_scope in view["current_scope_sets"]
    if approved is not None and scope_matches:
        if runner(approved) != runner(route):
            raise LatticeConflict("the active scoped route amendment requires its approved runner")
        return approved
    if approved is not None and view["current_scope_sets"] and runner(approved) == runner(route):
        raise LatticeConflict("the amended runner is outside its approved route scope")
    return None


def invocation_scope_refs(conn: sqlite3.Connection, invocation_id: str) -> list[str]:
    from .canonical import canonical_bytes, sha256_bytes
    from .store import LatticeConflict

    row = conn.execute("SELECT manifest_json,manifest_sha256 FROM invocations WHERE invocation_id=?", (invocation_id,)).fetchone()
    if row is None:
        raise LatticeConflict("route scope invocation does not exist")
    manifest = json.loads(row["manifest_json"])
    if manifest.get("manifest_sha256") != row["manifest_sha256"] or sha256_bytes(canonical_bytes({key: value for key, value in manifest.items() if key != "manifest_sha256"})) != row["manifest_sha256"]:
        raise LatticeConflict("route scope invocation manifest bytes are corrupt")
    scope_refs = manifest.get("route_scope_refs", [])
    if not isinstance(scope_refs, list) or len(scope_refs) > 16 or scope_refs != sorted(set(scope_refs)) or any(not isinstance(item, str) or not item or len(item) > 500 or any(ord(char) < 32 or ord(char) == 127 for char in item) for item in scope_refs):
        raise LatticeConflict("route scope invocation manifest is malformed")
    return scope_refs


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
        if invocation_scope_refs(conn, value.source_invocation_id) not in value.route_scope_sets:
            raise LatticeConflict("route amendment scope must include the exact failed invocation scope")
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
            "previous_scope_sets": view["current_scope_sets"],
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
        effective_scope_sets = pending["request"]["route_scope_sets"] if value.decision == "approve" else pending.get("previous_scope_sets", [])
        store._event(conn, project_id=session["project_id"], event_type="role-route.decided", subject_type="role-session", subject_id=session_id, payload={
            "amendment_id": amendment_id, "request_event_id": pending["event_id"], **value.model_dump(mode="json"),
            "authority": "operator", "effective_route": effective, "state": "active",
            "effective_scope_sets": effective_scope_sets,
        })
        return state(conn, session_id)
