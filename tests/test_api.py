"""Operator-boundary and API-state contracts."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from loom.server import create_app
from loom.contracts import ContextBlockCreate, DraftCreate


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


@pytest.mark.oracle
def test_closure_transition_automatically_seals_blinded_challenger_without_candidate_context(loom_env) -> None:
    store = loom_env["store"]
    created = store.create_workflow_project_atomic(title="closure workflow", purpose="", workflow_pack="quaternary", initial_state="countersearch", role_specs=[{"name": "Blinded closure challenger", "authority": "challenge-only", "instructions": "Recompute independently."}], blackboard_content=b"{}", blackboard_source_ref="fixture:state", slice_external_ref=None)
    project_id = created["project"]["project_id"]
    evidence = store.create_context_block(ContextBlockCreate(project_id=project_id, semantic_type="search-receipts", content="INDEPENDENT_BASE_EVIDENCE"))
    candidate = store.create_context_block(ContextBlockCreate(project_id=project_id, semantic_type="closure-candidate", content="SECRET_PROPOSER_CONCLUSION"))
    app = create_app(loom_env["config"])
    with TestClient(app) as client:
        session = client.get("/api/session")
        headers = {"x-loom-csrf": session.json()["csrf"], "origin": "http://testserver"}
        response = client.post("/api/v1/workflow-packs/quaternary/transitions", headers=headers, json={"project_id": project_id, "expected_revision": 1, "from_state": "countersearch", "to_state": "closure-candidate", "evidence_block_ids": [evidence["block_id"]], "payload": {"closure_candidate_block_id": candidate["block_id"]}})

    assert response.status_code == 200, response.text
    challenger = response.json()["challenger_invocation"]
    exact_context = store.blobs.get(challenger["compiled_prompt_sha256"])
    assert b"INDEPENDENT_BASE_EVIDENCE" in exact_context
    assert b"SECRET_PROPOSER_CONCLUSION" not in exact_context


@pytest.mark.oracle
def test_workflow_project_read_model_exposes_authoritative_blackboard_roles_and_triggers(loom_env) -> None:
    store = loom_env["store"]
    created = store.create_workflow_project_atomic(title="visible workflow", purpose="", workflow_pack="quaternary", initial_state="semantic-intent", role_specs=[{"name": "Native partner", "authority": "proposal-only", "instructions": "Interpret German."}], blackboard_content=b'{"language":"de","state":"calibrating"}', blackboard_source_ref="fixture:quaternary", slice_external_ref=None)

    project = store.get_project(created["project"]["project_id"])

    assert project["workflow"]["state"] == "semantic-intent"
    assert project["workflow"]["blackboard"]["content"] == '{"language":"de","state":"calibrating"}'
    assert [role["name"] for role in project["roles"]] == ["Native partner"]
    assert project["workflow"]["triggers"] == []
