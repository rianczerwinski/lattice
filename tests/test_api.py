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
def test_oracle_artifact_inspector_returns_exact_content_bytes(loom_env) -> None:
    store = loom_env["store"]
    draft = store.create_draft(DraftCreate(project_id=loom_env["project"]["project_id"], role_session_id=loom_env["session"]["role_session_id"], prompt="artifact", route=loom_env["route"]))
    invocation = loom_env["loom"].seal(draft["draft_id"])
    attempt = store.create_attempt(invocation["invocation_id"], "artifact-api-attempt", "test-boot", 9_999_999_999)
    store.mark_attempt_running(attempt["attempt_id"])
    result = store.finish_successful_attempt(attempt["attempt_id"], response="done", artifacts=[{"name": "proof.txt", "media_type": "text/plain", "content": "EXACT_ARTIFACT_CONTENT"}], journal_path="fixture:journal", runtime_receipt={"fixture": True})
    artifact_id = result["artifacts"][0]["artifact_id"]
    app = create_app(loom_env["config"])
    with TestClient(app) as client:
        inspected = client.get(f"/api/v1/artifacts/{artifact_id}")

    assert inspected.status_code == 200
    assert inspected.json()["content"] == "EXACT_ARTIFACT_CONTENT"


@pytest.mark.oracle
def test_closure_transition_automatically_seals_blinded_challenger_without_candidate_context(loom_env) -> None:
    store = loom_env["store"]
    created = store.create_workflow_project_atomic(title="closure workflow", purpose="", workflow_pack="quaternary", initial_state="countersearch", role_specs=[{"role_id": "verifier", "name": "Blinded closure challenger", "provider": "openai", "harness": "codex", "allowed_models": [], "authority": "challenge-only", "instructions": "Recompute independently."}], blackboard_content=b"{}", blackboard_source_ref="fixture:state", slice_external_ref=None)
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
    trigger = store.get_workflow_instance(project_id)["triggers"][0]
    assert trigger["state"] == "planned"
    attempt = store.create_attempt(challenger["invocation_id"], "challenger-attempt-1", "test-boot", 9_999_999_999)
    store.mark_attempt_running(attempt["attempt_id"])
    store.finish_successful_attempt(attempt["attempt_id"], response="independent result", artifacts=[], journal_path="fixture:journal", runtime_receipt={"fixture": True})
    assert store.get_workflow_instance(project_id)["triggers"][0]["state"] == "completed"


@pytest.mark.regression
def test_trigger_planning_failure_is_visible_retriable_and_does_not_lie_about_committed_state(monkeypatch, loom_env) -> None:
    store = loom_env["store"]
    created = store.create_workflow_project_atomic(title="retry workflow", purpose="", workflow_pack="quaternary", initial_state="countersearch", role_specs=[{"role_id": "verifier", "name": "Blinded closure challenger", "provider": "openai", "harness": "codex", "allowed_models": [], "authority": "challenge-only", "instructions": "Recompute independently."}], blackboard_content=b"{}", blackboard_source_ref="fixture:state", slice_external_ref=None)
    project_id = created["project"]["project_id"]
    evidence = store.create_context_block(ContextBlockCreate(project_id=project_id, semantic_type="search-receipts", content="independent"))
    candidate = store.create_context_block(ContextBlockCreate(project_id=project_id, semantic_type="closure-candidate", content="excluded"))
    app = create_app(loom_env["config"])
    original = app.state.store.create_draft
    calls = 0

    def fail_once(value):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("planted post-commit planning failure")
        return original(value)

    monkeypatch.setattr(app.state.store, "create_draft", fail_once)
    with TestClient(app) as client:
        csrf = client.get("/api/session").json()["csrf"]
        headers = {"x-loom-csrf": csrf, "origin": "http://testserver"}
        transition = client.post("/api/v1/workflow-packs/quaternary/transitions", headers=headers, json={"project_id": project_id, "expected_revision": 1, "from_state": "countersearch", "to_state": "closure-candidate", "evidence_block_ids": [evidence["block_id"]], "payload": {"closure_candidate_block_id": candidate["block_id"]}})
        body = transition.json()
        retried = client.post(f"/api/v1/workflow-triggers/{body['trigger']['trigger_id']}/retry", headers=headers, json={})

    assert transition.status_code == 200
    assert body["workflow"]["state"] == "closure-candidate"
    assert body["trigger"]["state"] == "pending"
    assert "planted post-commit planning failure" in body["trigger_error"]
    assert retried.status_code == 200, retried.text
    assert retried.json()["trigger"]["state"] == "planned"


@pytest.mark.oracle
def test_workflow_project_read_model_exposes_authoritative_blackboard_roles_and_triggers(loom_env) -> None:
    store = loom_env["store"]
    created = store.create_workflow_project_atomic(title="visible workflow", purpose="", workflow_pack="quaternary", initial_state="semantic-intent", role_specs=[{"name": "Native partner", "authority": "proposal-only", "instructions": "Interpret German."}], blackboard_content=b'{"language":"de","state":"calibrating"}', blackboard_source_ref="fixture:quaternary", slice_external_ref=None)

    project = store.get_project(created["project"]["project_id"])

    assert project["workflow"]["state"] == "semantic-intent"
    assert project["workflow"]["blackboard"]["content"] == '{"language":"de","state":"calibrating"}'
    assert [role["name"] for role in project["roles"]] == ["Native partner"]
    assert project["workflow"]["triggers"] == []
