"""Operator-boundary and API-state contracts."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from loom.server import create_app


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
