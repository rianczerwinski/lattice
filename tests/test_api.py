"""Operator-boundary and API-state contracts."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from loom.server import create_app
from loom.contracts import DraftCreate


@pytest.mark.contract
def test_contract_mutations_require_same_origin_operator_session(loom_env) -> None:
    app = create_app(loom_env["config"])
    with TestClient(app) as client:
        denied = client.post("/api/v1/projects", json={"title": "Denied"})
        session = client.get("/api/session")
        csrf = session.json()["csrf"]
        evil = client.post("/api/v1/projects", headers={"x-loom-csrf": csrf, "origin": "http://localhost:31337"}, json={"title": "Evil"})
        allowed = client.post("/api/v1/projects", headers={"x-loom-csrf": csrf, "origin": "http://testserver"}, json={"title": "Allowed"})

    assert denied.status_code == 403
    assert evil.status_code == 403
    assert allowed.status_code == 200
    assert "access-control-allow-origin" not in allowed.headers


@pytest.mark.smoke
def test_smoke_health_and_cockpit_have_distinct_responses(loom_env) -> None:
    app = create_app(loom_env["config"])
    with TestClient(app) as client:
        health = client.get("/api/health")
        cockpit = client.get("/")

    assert health.json()["status"] == "ok"
    assert cockpit.status_code == 200
    assert '<div id="app"></div>' in cockpit.text


@pytest.mark.oracle
def test_oracle_invocation_inspection_exposes_independently_reconstructed_prompt(loom_env) -> None:
    draft = loom_env["store"].create_draft(DraftCreate(project_id=loom_env["project"]["project_id"], role_session_id=loom_env["session"]["role_session_id"], prompt="EXACT_VISIBLE_PROMPT", route=loom_env["route"]))
    invocation = loom_env["loom"].seal(draft["draft_id"])
    app = create_app(loom_env["config"])
    with TestClient(app) as client:
        inspected = client.get(f"/api/v1/invocations/{invocation['invocation_id']}").json()

    assert "EXACT_VISIBLE_PROMPT" in inspected["compiled_prompt"]
    assert inspected["manifest_verification"]["compiled_prompt_sha256"] == inspected["manifest"]["compilation"]["compiled_prompt_sha256"]
