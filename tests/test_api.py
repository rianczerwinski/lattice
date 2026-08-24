"""Operator-boundary and API-state contracts."""

from __future__ import annotations

import json
from pathlib import Path
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from lattice.config import LatticeConfig
from lattice.server import create_app
from lattice.contracts import AttemptState, BlockStatus, ContextBlockCreate, ContextSelection, DraftCreate, ProviderRoute, TagRequest
from lattice.operations import Lattice
from lattice.security import OperatorSessions, ensure_cli_token
from lattice.store import LatticeConflict
from lattice.workflows import WorkflowRegistry

from conftest import TEST_WORKFLOW_PACK_ROOTS, completed_invocation


def _terminal_workflow_output(lattice_env, created, response: str, *, workflow_role_key: str | None = None):
    project = created["project"]
    if workflow_role_key is None:
        session = project["sessions"][0]
    else:
        role_id = next(role["role_id"] for role in project["roles"] if role["workflow_role_key"] == workflow_role_key)
        session = next(item for item in project["sessions"] if item["role_id"] == role_id)
    draft = lattice_env["store"].create_draft(DraftCreate(project_id=project["project_id"], role_session_id=session["role_session_id"], prompt="independent evidence", route=lattice_env["route"]))
    invocation = lattice_env["lattice"].seal(draft["draft_id"])
    attempt = lattice_env["store"].create_attempt(invocation["invocation_id"], f"workflow-evidence-{response}", "test-boot", 9_999_999_999)
    lattice_env["store"].mark_attempt_running(attempt["attempt_id"])
    completed = lattice_env["store"].finish_successful_attempt(attempt["attempt_id"], response=response, artifacts=[], journal_path="fixture:journal", runtime_receipt={"fixture": True})
    return completed["output_block"]


@pytest.mark.contract
def test_contract_mutations_require_same_origin_operator_session(lattice_env) -> None:
    app = create_app(lattice_env["config"])
    with TestClient(app) as client:
        denied = client.post("/api/v1/projects", json={"title": "Denied"})
        session = client.get("/api/session")
        csrf = session.json()["csrf"]
        evil = client.post("/api/v1/projects", headers={"x-lattice-csrf": csrf, "origin": "http://localhost:31337"}, json={"title": "Evil"})
        allowed = client.post("/api/v1/projects", headers={"x-lattice-csrf": csrf, "origin": "http://testserver"}, json={"title": "Allowed"})

    assert denied.status_code == 403
    assert evil.status_code == 403
    assert allowed.status_code == 200
    assert "access-control-allow-origin" not in allowed.headers


@pytest.mark.regression
def test_negative_control_provider_process_probe_requires_operator_authority(monkeypatch, lattice_env) -> None:
    calls: list[bool] = []

    def fake_status(*, probe_versions: bool):
        calls.append(probe_versions)
        return []

    monkeypatch.setattr("axis_model_runner.installed_adapter_status", fake_status)
    app = create_app(lattice_env["config"])
    with TestClient(app) as client:
        denied_get = client.get("/api/v1/routes?probe=true")
        denied_post = client.post("/api/v1/routes/probe", json={})
        session = client.get("/api/session")
        allowed_post = client.post("/api/v1/routes/probe", headers={"x-lattice-csrf": session.json()["csrf"], "origin": "http://testserver"}, json={})

    assert denied_get.status_code == 405
    assert denied_post.status_code == 403
    assert allowed_post.status_code == 200
    assert calls == [True]


@pytest.mark.contract
def test_manual_project_endpoint_cannot_bypass_atomic_workflow_installation(lattice_env) -> None:
    app = create_app(lattice_env["config"])
    with TestClient(app) as client:
        session = client.get("/api/session")
        response = client.post("/api/v1/projects", headers={"x-lattice-csrf": session.json()["csrf"], "origin": "http://testserver"}, json={"title": "False workflow", "workflow_pack": "quaternary"})

    assert response.status_code == 422
    assert all(project["title"] != "False workflow" for project in lattice_env["store"].list_projects())


@pytest.mark.regression
def test_operator_session_bootstrap_rejects_cross_site_requests_and_sessions_expire(lattice_env) -> None:
    now = [10.0]
    sessions = OperatorSessions(ttl_seconds=5, max_sessions=2, clock=lambda: now[0])
    first, csrf = sessions.issue()
    sessions.issue()
    third, third_csrf = sessions.issue()
    assert sessions.valid(first, csrf) is False
    assert sessions.valid(third, third_csrf) is True
    now[0] = 16.0
    assert sessions.valid(third, third_csrf) is False

    app = create_app(lattice_env["config"])
    with TestClient(app) as client:
        cross_site = client.get("/api/session", headers={"sec-fetch-site": "cross-site"})
        same_site = client.get("/api/session", headers={"sec-fetch-site": "same-origin"})

    assert cross_site.status_code == 403
    assert same_site.status_code == 200
    assert "Max-Age=86400" in same_site.headers["set-cookie"]
    assert "frame-ancestors 'self' http://127.0.0.1:9000 http://localhost:9000" in same_site.headers["content-security-policy"]


@pytest.mark.regression
def test_operator_token_rejects_a_symlink_even_when_its_target_has_safe_permissions(tmp_path: Path) -> None:
    target = tmp_path / "planted-secret"
    target.write_text("x" * 48, encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "operator.token"
    link.symlink_to(target)

    with pytest.raises(PermissionError, match="non-symlink"):
        ensure_cli_token(link)


@pytest.mark.concurrency
def test_negative_control_second_service_cannot_orphan_live_first_service_work(lattice_env) -> None:
    first_app = create_app(lattice_env["config"])
    with TestClient(first_app):
        draft = first_app.state.store.create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], role_session_id=lattice_env["session"]["role_session_id"], prompt="owned by first process", route=lattice_env["route"]))
        invocation = first_app.state.lattice.seal(draft["draft_id"])
        attempt = first_app.state.store.create_attempt(invocation["invocation_id"], "first-process-live", first_app.state.supervisor.boot_id, 9_999_999_999)

        with pytest.raises(RuntimeError, match="another Lattice process owns data root"):
            create_app(lattice_env["config"])

        assert first_app.state.store.get_attempt(attempt["attempt_id"])["state"] == "leased"
        first_app.state.store.finish_attempt(attempt["attempt_id"], state=AttemptState.interrupted, error_text="test cleanup")


@pytest.mark.contract
def test_negative_control_malformed_development_origin_fails_closed(monkeypatch, lattice_env) -> None:
    monkeypatch.setenv("LATTICE_DEV_ORIGIN", "http://127.0.0.1:5173/path")
    with pytest.raises(ValueError, match="LATTICE_DEV_ORIGIN is invalid"):
        with TestClient(create_app(lattice_env["config"])):
            pass


@pytest.mark.smoke
def test_smoke_health_and_cockpit_have_distinct_responses(lattice_env) -> None:
    app = create_app(lattice_env["config"])
    with TestClient(app) as client:
        health = client.get("/api/health")
        cockpit = client.get("/")

    assert health.json()["status"] == "ok"
    assert cockpit.status_code == 200
    assert '<div id="app"></div>' in cockpit.text


@pytest.mark.regression
def test_invalid_optional_workflow_pack_does_not_take_down_manual_conversation(tmp_path) -> None:
    broken_root = tmp_path / "broken-workflows"
    broken_pack = broken_root / "platform-foundry"
    broken_pack.mkdir(parents=True)
    broken_pack.joinpath("pack.json").write_text('{"pack_id":"platform-foundry","version":1}', encoding="utf-8")
    config = LatticeConfig(data_root=tmp_path / "isolated-data", port=9010, axisvi_root=None, workflow_pack_roots_extra=(broken_root,))
    app = create_app(config)
    with TestClient(app, raise_server_exceptions=False) as client:
        catalogue = client.get("/api/v1/workflow-packs")
        session = client.get("/api/session")
        headers = {"x-lattice-csrf": session.json()["csrf"], "origin": "http://testserver"}
        created = client.post("/api/v1/projects", headers=headers, json={"title": "Conversation survives", "purpose": "No workflow required"})
        rejected = client.post("/api/v1/workflow-packs/platform-foundry/projects", headers=headers, json={"title": "Must fail", "selector": {"platform_id": "pinterest-com"}})
        health = client.get("/api/health")

    assert catalogue.status_code == 200
    assert catalogue.json()["failures"][0]["pack_id"] == "platform-foundry"
    assert created.status_code == 200
    assert created.json()["roles"] == []
    assert rejected.status_code == 422
    assert "workflow pack is unavailable" in rejected.json()["detail"]
    assert health.json()["status"] == "ok"
    assert health.json()["workflow_pack_failures"][0]["pack_id"] == "platform-foundry"


@pytest.mark.regression
def test_unknown_api_route_is_a_json_404_not_the_cockpit(lattice_env) -> None:
    app = create_app(lattice_env["config"])
    with TestClient(app) as client:
        response = client.get("/api/v1/typo-that-must-fail")

    assert response.status_code == 404
    assert response.json() == {"detail": "API route not found"}
    assert '<div id="app"></div>' not in response.text


@pytest.mark.contract
def test_context_module_catalogue_and_failures_are_explicit(lattice_env) -> None:
    app = create_app(lattice_env["config"])
    with TestClient(app) as client:
        catalogue = client.get("/api/v1/context-modules")
        session = client.get("/api/session")
        headers = {"x-lattice-csrf": session.json()["csrf"], "origin": "http://testserver"}
        failed = client.post("/api/v1/context-modules/resolve", headers=headers, json={
            "project_id": lattice_env["project"]["project_id"],
            "definition": {"module_id": "bad.file", "version": 1, "mode": "manual", "source_kind": "file", "capabilities": [], "configuration": {"path": "/unreadable"}},
        })
        governed = client.post("/api/v1/context-modules/resolve", headers=headers, json={
            "project_id": lattice_env["project"]["project_id"],
            "definition": {"module_id": "governed.required", "version": 1, "mode": "workflow_required", "source_kind": "file", "capabilities": ["read-local-file"], "configuration": {"path": "/unreadable"}},
        })

    assert catalogue.status_code == 200
    assert catalogue.json()["modes"] == ["manual", "project_default", "workflow_required"]
    assert {item["source_kind"] for item in catalogue.json()["sources"]} == {"file", "surface", "octopus", "http_readonly"}
    assert failed.status_code == 422
    with lattice_env["store"]._connect() as connection:
        event = connection.execute("SELECT payload_json FROM events WHERE event_type='context-module.failed' ORDER BY seq DESC LIMIT 1").fetchone()
    receipt = json.loads(event[0])["receipt"]
    assert receipt["failure_kind"] == "resolution-failed"
    assert receipt["sensitivity"] == "private"
    assert receipt["authority"] == "bad.file"
    assert receipt["receipt_sha256"]
    assert governed.status_code == 422
    assert "governed workflow-pack mode" in governed.json()["detail"]


@pytest.mark.oracle
def test_oracle_invocation_inspection_exposes_independently_reconstructed_prompt(lattice_env) -> None:
    draft = lattice_env["store"].create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], role_session_id=lattice_env["session"]["role_session_id"], prompt="EXACT_VISIBLE_PROMPT", route=lattice_env["route"]))
    invocation = lattice_env["lattice"].seal(draft["draft_id"])
    app = create_app(lattice_env["config"])
    with TestClient(app) as client:
        inspected = client.get(f"/api/v1/invocations/{invocation['invocation_id']}").json()

    assert "EXACT_VISIBLE_PROMPT" in inspected["compiled_prompt"]
    assert inspected["manifest_verification"]["compiled_prompt_sha256"] == inspected["manifest"]["compilation"]["compiled_prompt_sha256"]


@pytest.mark.oracle
def test_seal_is_bound_to_the_exact_context_preview(lattice_env) -> None:
    draft = lattice_env["store"].create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], role_session_id=lattice_env["session"]["role_session_id"], prompt="preview exactly", route=lattice_env["route"]))
    app = create_app(lattice_env["config"])
    with TestClient(app) as client:
        csrf = client.get("/api/session").json()["csrf"]
        headers = {"x-lattice-csrf": csrf, "origin": "http://testserver"}
        first = client.get(f"/api/v1/drafts/{draft['draft_id']}/preview").json()
        repeated = client.get(f"/api/v1/drafts/{draft['draft_id']}/preview").json()
        assert first["preflight"]["preflight_sha256"] == repeated["preflight"]["preflight_sha256"]
        assert "generated_object" not in first["preflight"]
        assert "manifest_sha256" not in first["preflight"]
        first_prompt_sha = first["preflight"]["compilation"]["compiled_prompt_sha256"]
        assert not lattice_env["store"].blobs.path_for(first_prompt_sha).exists()

        lattice_env["store"].set_block_status(lattice_env["role"]["instructions_block_id"], BlockStatus.disputed)
        rejected = client.post(f"/api/v1/drafts/{draft['draft_id']}/seal", headers=headers, json={"expected_revision": first["revision"], "expected_preflight_sha256": first["preflight"]["preflight_sha256"]})
        assert not lattice_env["store"].blobs.path_for(first_prompt_sha).exists()
        refreshed = client.get(f"/api/v1/drafts/{draft['draft_id']}/preview").json()
        accepted = client.post(f"/api/v1/drafts/{draft['draft_id']}/seal", headers=headers, json={"expected_revision": refreshed["revision"], "expected_preflight_sha256": refreshed["preflight"]["preflight_sha256"]})

    assert rejected.status_code == 409
    assert "context changed" in rejected.text
    assert accepted.status_code == 200, accepted.text
    assert lattice_env["store"].blobs.path_for(refreshed["preflight"]["compilation"]["compiled_prompt_sha256"]).is_file()


@pytest.mark.oracle
def test_context_preview_exposes_exact_delta_from_each_selected_parent(lattice_env) -> None:
    parent = completed_invocation(lattice_env, "parent prompt", "parent output")
    draft = lattice_env["lattice"].fork(parent["invocation_id"], role_session_id=lattice_env["session"]["role_session_id"], prompt="child prompt", route=lattice_env["route"])
    app = create_app(lattice_env["config"])
    with TestClient(app) as client:
        preview = client.get(f"/api/v1/drafts/{draft['draft_id']}/preview").json()

    assert len(preview["parent_deltas"]) == 1
    delta = preview["parent_deltas"][0]
    assert delta["invocation_id"] == parent["invocation_id"]
    assert any(item["plane"] == "current_prompt" for item in delta["added"])
    assert any(item["plane"] == "current_prompt" for item in delta["removed"])


@pytest.mark.oracle
def test_context_delta_discriminates_transformed_omitted_and_provider_opaque() -> None:
    supplied = {"block_id": "block_source", "plane": "knowledge", "label": "source", "disposition": "selected", "sha256": "a" * 64, "transformation": None, "derived_from_block_ids": []}
    transformed = {"block_id": "block_excerpt", "plane": "knowledge", "label": "excerpt", "disposition": "selected", "sha256": "b" * 64, "transformation": {"kind": "utf8-byte-range-v1"}, "derived_from_block_ids": ["block_source"]}
    omitted = {"block_id": "block_muted", "plane": "knowledge", "label": "muted", "disposition": "omitted", "sha256": "c" * 64, "transformation": None, "derived_from_block_ids": []}
    opaque = {"block_id": "block_opaque", "plane": "state", "label": "unknown", "disposition": "provider_opaque", "sha256": "d" * 64, "transformation": None, "derived_from_block_ids": []}

    delta = Lattice.context_delta({"segments": [supplied], "omitted": []}, {"segments": [supplied, transformed], "omitted": [omitted, opaque]})

    assert [item["block_id"] for item in delta["transformed"]] == ["block_excerpt"]
    assert [item["block_id"] for item in delta["omitted"]] == ["block_muted"]
    assert [item["block_id"] for item in delta["provider_opaque"]] == ["block_opaque"]
    assert delta["unchanged_count"] == 1


@pytest.mark.regression
def test_negative_control_context_delta_never_relables_opaque_context_as_ordinary_omission() -> None:
    opaque = {"block_id": "block_opaque", "plane": "state", "label": "unknown", "disposition": "provider_opaque", "sha256": "d" * 64, "transformation": None, "derived_from_block_ids": []}
    delta = Lattice.context_delta({"segments": [], "omitted": []}, {"segments": [], "omitted": [opaque]})

    assert delta["omitted"] == []
    assert delta["provider_opaque"][0]["block_id"] == "block_opaque"


@pytest.mark.oracle
def test_oracle_artifact_inspector_returns_exact_content_bytes(lattice_env) -> None:
    store = lattice_env["store"]
    draft = store.create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], role_session_id=lattice_env["session"]["role_session_id"], prompt="artifact", route=lattice_env["route"]))
    invocation = lattice_env["lattice"].seal(draft["draft_id"])
    attempt = store.create_attempt(invocation["invocation_id"], "artifact-api-attempt", "test-boot", 9_999_999_999)
    store.mark_attempt_running(attempt["attempt_id"])
    result = store.finish_successful_attempt(attempt["attempt_id"], response="done", artifacts=[{"name": "proof.txt", "media_type": "text/plain", "content": "EXACT_ARTIFACT_CONTENT"}], journal_path="fixture:journal", runtime_receipt={"fixture": True})
    artifact_id = result["artifacts"][0]["artifact_id"]
    app = create_app(lattice_env["config"])
    with TestClient(app) as client:
        session = client.get("/api/session")
        mutation_headers = {"x-lattice-csrf": session.json()["csrf"], "origin": "http://testserver"}
        inspected = client.get(f"/api/v1/artifacts/{artifact_id}")
        grafted = client.post(f"/api/v1/artifacts/{artifact_id}/context-blocks", headers=mutation_headers, json={})
        exact_block = client.get(f"/api/v1/context-blocks/{grafted.json()['block_id']}")

    assert inspected.status_code == 200
    assert inspected.json()["content"] == "EXACT_ARTIFACT_CONTENT"
    assert grafted.status_code == 200
    assert exact_block.json()["content"] == "EXACT_ARTIFACT_CONTENT"
    assert exact_block.json()["source_ref"] == f"lattice:artifact/{artifact_id}"
    assert exact_block.json()["source_sha256"] == inspected.json()["content_sha256"]
    assert json.loads(exact_block.json()["transformation_json"])["kind"] == "exact-artifact-graft-v1"
    draft = store.create_draft(DraftCreate(
        project_id=lattice_env["project"]["project_id"],
        role_session_id=lattice_env["session"]["role_session_id"],
        prompt="Use this exact artifact.",
        route=lattice_env["route"],
        selections=[ContextSelection(block_id=grafted.json()["block_id"], plane="knowledge", label="exact artifact")],
    ))
    sealed = lattice_env["lattice"].seal(draft["draft_id"])
    assert {"ref": f"lattice:artifact/{artifact_id}", "revision": None, "sha256": inspected.json()["content_sha256"]} in sealed["manifest"]["generated_object"]["sources"]


@pytest.mark.regression
def test_negative_control_artifact_graft_rejects_bytes_that_drift_from_identity(lattice_env) -> None:
    store = lattice_env["store"]
    draft = store.create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], role_session_id=lattice_env["session"]["role_session_id"], prompt="artifact", route=lattice_env["route"]))
    invocation = lattice_env["lattice"].seal(draft["draft_id"])
    attempt = store.create_attempt(invocation["invocation_id"], "artifact-drift-attempt", "test-boot", 9_999_999_999)
    store.mark_attempt_running(attempt["attempt_id"])
    result = store.finish_successful_attempt(attempt["attempt_id"], response="done", artifacts=[{"name": "proof.txt", "media_type": "text/plain", "content": "BOUND_BYTES"}], journal_path="fixture:journal", runtime_receipt={"fixture": True})
    artifact_id = result["artifacts"][0]["artifact_id"]
    artifact = store.get_artifact(artifact_id, include_content=False)
    store.blobs.path_for(artifact["content_sha256"]).write_text("DRIFTED", encoding="utf-8")

    with pytest.raises(OSError, match="do not match identity"):
        store.create_artifact_context_block(artifact_id)


@pytest.mark.regression
def test_negative_control_artifact_graft_rejects_semantic_source_drift(lattice_env) -> None:
    store = lattice_env["store"]
    draft = store.create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], role_session_id=lattice_env["session"]["role_session_id"], prompt="artifact", route=lattice_env["route"]))
    invocation = lattice_env["lattice"].seal(draft["draft_id"])
    attempt = store.create_attempt(invocation["invocation_id"], "artifact-semantic-drift-attempt", "test-boot", 9_999_999_999)
    store.mark_attempt_running(attempt["attempt_id"])
    result = store.finish_successful_attempt(attempt["attempt_id"], response="done", artifacts=[{"name": "proof.txt", "media_type": "text/plain", "content": "BOUND_BYTES"}], journal_path="fixture:journal", runtime_receipt={"fixture": True})
    artifact_id = result["artifacts"][0]["artifact_id"]
    block = store.create_artifact_context_block(artifact_id)
    artifact = store.get_artifact(artifact_id, include_content=False)
    artifact["generated"]["name"] = "substituted.txt"
    with store.transaction() as conn:
        conn.execute("UPDATE artifacts SET generated_json=? WHERE artifact_id=?", (json.dumps(artifact["generated"], sort_keys=True, separators=(",", ":")), artifact_id))
    consuming = store.create_draft(DraftCreate(
        project_id=lattice_env["project"]["project_id"],
        role_session_id=lattice_env["session"]["role_session_id"],
        prompt="Use it.",
        route=lattice_env["route"],
        selections=[ContextSelection(block_id=block["block_id"], plane="knowledge", label="exact artifact")],
    ))

    with pytest.raises(LatticeConflict, match="exact artifact bytes do not reconstruct"):
        lattice_env["lattice"].seal(consuming["draft_id"])


@pytest.mark.oracle
def test_closure_transition_automatically_seals_dispatches_and_blinds_challenger(monkeypatch, lattice_env) -> None:
    store = lattice_env["store"]
    pack_sha = WorkflowRegistry(TEST_WORKFLOW_PACK_ROOTS).load("quaternary")["canonical_sha256"]
    created = store.create_workflow_project_atomic(title="closure workflow", purpose="", workflow_pack="quaternary", initial_state="countersearch", role_specs=[{"role_id": "sol", "name": "Orchestrator", "provider": "openai", "harness": "codex", "allowed_models": [], "authority": "propose-only", "instructions": "Propose bounded closure."}, {"role_id": "verifier", "name": "Blinded closure challenger", "provider": "openai", "harness": "codex", "allowed_models": [], "authority": "challenge-only", "instructions": "Recompute independently."}], blackboard_content=b"{}", blackboard_source_ref="fixture:state", slice_external_ref=None, pack_canonical_sha256=pack_sha)
    project_id = created["project"]["project_id"]
    evidence = _terminal_workflow_output(lattice_env, created, "INDEPENDENT_BASE_EVIDENCE", workflow_role_key="sol")
    candidate = store.create_context_block(ContextBlockCreate(project_id=project_id, semantic_type="closure-candidate", content="SECRET_PROPOSER_CONCLUSION"))
    app = create_app(lattice_env["config"])
    dispatched: list[str] = []

    async def fake_execute(invocation_id, _request):
        dispatched.append(invocation_id)
        return {"attempt_id": "attempt_fixture", "state": "leased"}

    monkeypatch.setattr(app.state.supervisor, "execute", fake_execute)
    with TestClient(app) as client:
        session = client.get("/api/session")
        headers = {"x-lattice-csrf": session.json()["csrf"], "origin": "http://testserver"}
        pack = client.get("/api/v1/workflow-packs/quaternary").json()
        granted = client.put(f"/api/v1/workflow-packs/quaternary/projects/{project_id}/automatic-grants/closure-proposed", headers=headers, json={"expected_revision": 1, "enabled": True, "pack_canonical_sha256": pack["canonical_sha256"]})
        response = client.post("/api/v1/workflow-packs/quaternary/transitions", headers=headers, json={"project_id": project_id, "expected_revision": 2, "from_state": "countersearch", "to_state": "closure-candidate", "evidence_block_ids": [evidence["block_id"], created["blackboard"]["block_id"]], "payload": {"closure_candidate_block_id": candidate["block_id"]}})

    assert granted.status_code == 200, granted.text
    assert response.status_code == 200, response.text
    challenger = response.json()["challenger_invocation"]
    assert response.json()["challenger_attempt"]["state"] == "leased"
    assert dispatched == [challenger["invocation_id"]]
    exact_context = store.blobs.get(challenger["compiled_prompt_sha256"])
    assert b"INDEPENDENT_BASE_EVIDENCE" in exact_context
    assert b"SECRET_PROPOSER_CONCLUSION" not in exact_context
    trigger = store.get_workflow_instance(project_id)["triggers"][0]
    assert trigger["state"] == "planned"
    assert trigger["pack_canonical_sha256"] == pack["canonical_sha256"]
    assert trigger["action_sha256"]
    attempt = store.create_attempt(challenger["invocation_id"], "challenger-attempt-1", "test-boot", 9_999_999_999)
    store.mark_attempt_running(attempt["attempt_id"])
    store.finish_successful_attempt(attempt["attempt_id"], response="independent result", artifacts=[], journal_path="fixture:journal", runtime_receipt={"fixture": True})
    assert store.get_workflow_instance(project_id)["triggers"][0]["state"] == "completed"


@pytest.mark.regression
def test_trigger_planning_failure_is_visible_retriable_and_does_not_lie_about_committed_state(monkeypatch, lattice_env) -> None:
    store = lattice_env["store"]
    loaded = WorkflowRegistry(TEST_WORKFLOW_PACK_ROOTS).load("quaternary")
    created = store.create_workflow_project_atomic(title="retry workflow", purpose="", workflow_pack="quaternary", initial_state="countersearch", role_specs=[{"role_id": "sol", "name": "Orchestrator", "provider": "openai", "harness": "codex", "allowed_models": [], "authority": "propose-only", "instructions": "Propose bounded closure."}, {"role_id": "verifier", "name": "Blinded closure challenger", "provider": "openai", "harness": "codex", "allowed_models": [], "authority": "challenge-only", "instructions": "Recompute independently."}], blackboard_content=b"{}", blackboard_source_ref="fixture:state", slice_external_ref=None, pack_canonical_sha256=loaded["canonical_sha256"])
    project_id = created["project"]["project_id"]
    evidence = _terminal_workflow_output(lattice_env, created, "independent", workflow_role_key="sol")
    candidate = store.create_context_block(ContextBlockCreate(project_id=project_id, semantic_type="closure-candidate", content="excluded"))
    app = create_app(lattice_env["config"])
    async def fake_execute(_invocation_id, _request):
        return {"attempt_id": "attempt_fixture", "state": "leased"}

    monkeypatch.setattr(app.state.supervisor, "execute", fake_execute)
    trigger_definition = next(item for item in loaded["definition"]["triggers"] if item["trigger"] == "closure-proposed")
    from lattice.canonical import canonical_bytes, sha256_bytes
    store.set_workflow_automatic_grant(project_id, expected_revision=1, trigger_name="closure-proposed", grant={"trigger": "closure-proposed", "pack_canonical_sha256": loaded["canonical_sha256"], "action_sha256": sha256_bytes(canonical_bytes(trigger_definition["action"]))})
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
        headers = {"x-lattice-csrf": csrf, "origin": "http://testserver"}
        transition = client.post("/api/v1/workflow-packs/quaternary/transitions", headers=headers, json={"project_id": project_id, "expected_revision": 2, "from_state": "countersearch", "to_state": "closure-candidate", "evidence_block_ids": [evidence["block_id"], created["blackboard"]["block_id"]], "payload": {"closure_candidate_block_id": candidate["block_id"]}})
        body = transition.json()
        retried = client.post(f"/api/v1/workflow-triggers/{body['trigger']['trigger_id']}/retry", headers=headers, json={})

    assert transition.status_code == 200
    assert body["workflow"]["state"] == "closure-candidate"
    assert body["trigger"]["state"] == "pending"
    assert "planted post-commit planning failure" in body["trigger_error"]
    assert retried.status_code == 200, retried.text
    assert retried.json()["trigger"]["state"] == "planned"
    assert retried.json()["challenger_attempt"]["state"] == "leased"


@pytest.mark.regression
def test_trigger_dispatch_failure_is_retriable_but_never_under_a_changed_pack(monkeypatch, lattice_env) -> None:
    store = lattice_env["store"]
    loaded = WorkflowRegistry(TEST_WORKFLOW_PACK_ROOTS).load("quaternary")
    created = store.create_workflow_project_atomic(title="dispatch retry", purpose="", workflow_pack="quaternary", initial_state="countersearch", role_specs=[{"role_id": "sol", "name": "Orchestrator", "provider": "openai", "harness": "codex", "allowed_models": [], "authority": "propose-only", "instructions": "Propose bounded closure."}, {"role_id": "verifier", "name": "Blinded closure challenger", "provider": "openai", "harness": "codex", "allowed_models": [], "authority": "challenge-only", "instructions": "Recompute independently."}], blackboard_content=b"{}", blackboard_source_ref="fixture:state", slice_external_ref=None, pack_canonical_sha256=loaded["canonical_sha256"])
    project_id = created["project"]["project_id"]
    evidence = _terminal_workflow_output(lattice_env, created, "independent", workflow_role_key="sol")
    candidate = store.create_context_block(ContextBlockCreate(project_id=project_id, semantic_type="closure-candidate", content="excluded"))
    app = create_app(lattice_env["config"])
    calls: list[str] = []

    async def dispatch(invocation_id, _request):
        calls.append(invocation_id)
        if len(calls) == 1:
            raise OSError("planted dispatch outage")
        return {"attempt_id": "attempt_fixture", "state": "leased"}

    monkeypatch.setattr(app.state.supervisor, "execute", dispatch)
    trigger_definition = next(item for item in loaded["definition"]["triggers"] if item["trigger"] == "closure-proposed")
    from lattice.canonical import canonical_bytes, sha256_bytes
    store.set_workflow_automatic_grant(project_id, expected_revision=1, trigger_name="closure-proposed", grant={"trigger": "closure-proposed", "pack_canonical_sha256": loaded["canonical_sha256"], "action_sha256": sha256_bytes(canonical_bytes(trigger_definition["action"]))})

    with TestClient(app) as client:
        csrf = client.get("/api/session").json()["csrf"]
        headers = {"x-lattice-csrf": csrf, "origin": "http://testserver"}
        transition = client.post("/api/v1/workflow-packs/quaternary/transitions", headers=headers, json={"project_id": project_id, "expected_revision": 2, "from_state": "countersearch", "to_state": "closure-candidate", "evidence_block_ids": [evidence["block_id"], created["blackboard"]["block_id"]], "payload": {"closure_candidate_block_id": candidate["block_id"]}})
        trigger_id = transition.json()["trigger"]["trigger_id"]
        real_load = app.state.workflows.load
        monkeypatch.setattr(app.state.workflows, "load", lambda pack_id: {**real_load(pack_id), "canonical_sha256": "0" * 64})
        stale = client.post(f"/api/v1/workflow-triggers/{trigger_id}/retry", headers=headers, json={})
        calls_after_stale = len(calls)
        monkeypatch.setattr(app.state.workflows, "load", real_load)
        retried = client.post(f"/api/v1/workflow-triggers/{trigger_id}/retry", headers=headers, json={})

    assert transition.status_code == 200, transition.text
    assert transition.json()["trigger"]["state"] == "execution_failed"
    assert "planted dispatch outage" in transition.json()["trigger_error"]
    assert stale.status_code == 409
    assert "pack changed" in stale.text
    assert calls_after_stale == 1
    assert retried.status_code == 200, retried.text
    assert retried.json()["trigger"]["state"] == "execution_failed"
    assert retried.json()["challenger_attempt"]["state"] == "leased"
    assert len(calls) == 2


@pytest.mark.oracle
def test_negative_control_automatic_trigger_requires_exact_project_grant_and_pack_generation(monkeypatch, lattice_env) -> None:
    store = lattice_env["store"]
    pack_sha = WorkflowRegistry(TEST_WORKFLOW_PACK_ROOTS).load("quaternary")["canonical_sha256"]
    created = store.create_workflow_project_atomic(title="manual activation", purpose="", workflow_pack="quaternary", initial_state="countersearch", role_specs=[{"role_id": "sol", "name": "Orchestrator", "provider": "openai", "harness": "codex", "allowed_models": [], "authority": "propose-only", "instructions": "Propose."}, {"role_id": "verifier", "name": "Verifier", "provider": "openai", "harness": "codex", "allowed_models": [], "authority": "challenge-only", "instructions": "Challenge."}], blackboard_content=b"{}", blackboard_source_ref="fixture:state", slice_external_ref=None, pack_canonical_sha256=pack_sha)
    project_id = created["project"]["project_id"]
    evidence = _terminal_workflow_output(lattice_env, created, "independent", workflow_role_key="sol")
    wrong_role_evidence = _terminal_workflow_output(lattice_env, created, "wrong role", workflow_role_key="verifier")
    unsupported_evidence = store.create_context_block(ContextBlockCreate(project_id=project_id, semantic_type="search-receipts", content="operator assertion without a terminal invocation"))
    candidate = store.create_context_block(ContextBlockCreate(project_id=project_id, semantic_type="closure-candidate", content="excluded"))
    app = create_app(lattice_env["config"])
    with TestClient(app) as client:
        csrf = client.get("/api/session").json()["csrf"]
        headers = {"x-lattice-csrf": csrf, "origin": "http://testserver"}
        empty_evidence = client.post("/api/v1/workflow-packs/quaternary/transitions", headers=headers, json={"project_id": project_id, "expected_revision": 1, "from_state": "countersearch", "to_state": "closure-candidate", "evidence_block_ids": [], "payload": {"closure_candidate_block_id": candidate["block_id"]}})
        unsupported = client.post("/api/v1/workflow-packs/quaternary/transitions", headers=headers, json={"project_id": project_id, "expected_revision": 1, "from_state": "countersearch", "to_state": "closure-candidate", "evidence_block_ids": [unsupported_evidence["block_id"], created["blackboard"]["block_id"]], "payload": {"closure_candidate_block_id": candidate["block_id"]}})
        wrong_role = client.post("/api/v1/workflow-packs/quaternary/transitions", headers=headers, json={"project_id": project_id, "expected_revision": 1, "from_state": "countersearch", "to_state": "closure-candidate", "evidence_block_ids": [wrong_role_evidence["block_id"], created["blackboard"]["block_id"]], "payload": {"closure_candidate_block_id": candidate["block_id"]}})
        response = client.post("/api/v1/workflow-packs/quaternary/transitions", headers=headers, json={"project_id": project_id, "expected_revision": 1, "from_state": "countersearch", "to_state": "closure-candidate", "evidence_block_ids": [evidence["block_id"], created["blackboard"]["block_id"]], "payload": {"closure_candidate_block_id": candidate["block_id"]}})
        trigger_id = response.json()["trigger"]["trigger_id"]
        real_load = app.state.workflows.load

        def changed_pack(pack_id):
            loaded = real_load(pack_id)
            return {**loaded, "canonical_sha256": "0" * 64}

        monkeypatch.setattr(app.state.workflows, "load", changed_pack)
        stale_retry = client.post(f"/api/v1/workflow-triggers/{trigger_id}/retry", headers=headers, json={})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["activation_required"] is True
    assert body["trigger"]["state"] == "pending"
    assert "challenger_invocation" not in body
    assert empty_evidence.status_code == 422
    assert unsupported.status_code == 409
    assert "exact terminal invocation output" in unsupported.text
    assert wrong_role.status_code == 409
    assert "role-bound terminal output" in wrong_role.text
    assert stale_retry.status_code == 409
    assert "pack changed" in stale_retry.text


@pytest.mark.oracle
def test_negative_control_stale_workflow_instance_cannot_execute_under_new_pack(lattice_env) -> None:
    store = lattice_env["store"]
    created = store.create_workflow_project_atomic(title="stale workflow", purpose="", workflow_pack="quaternary", initial_state="countersearch", role_specs=[{"role_id": "verifier", "name": "Verifier", "provider": "openai", "harness": "codex", "allowed_models": [], "authority": "challenge-only", "instructions": "Challenge."}], blackboard_content=b"{}", blackboard_source_ref="fixture:state", slice_external_ref=None, pack_canonical_sha256="0" * 64)
    project_id = created["project"]["project_id"]
    evidence = store.create_context_block(ContextBlockCreate(project_id=project_id, semantic_type="search-receipts", content="independent"))
    candidate = store.create_context_block(ContextBlockCreate(project_id=project_id, semantic_type="closure-candidate", content="excluded"))
    app = create_app(lattice_env["config"])
    with TestClient(app) as client:
        csrf = client.get("/api/session").json()["csrf"]
        response = client.post("/api/v1/workflow-packs/quaternary/transitions", headers={"x-lattice-csrf": csrf, "origin": "http://testserver"}, json={"project_id": project_id, "expected_revision": 1, "from_state": "countersearch", "to_state": "closure-candidate", "evidence_block_ids": [evidence["block_id"]], "payload": {"closure_candidate_block_id": candidate["block_id"]}})

    assert response.status_code == 409
    assert "pack generation changed" in response.text
    assert store.get_workflow_instance(project_id)["state"] == "countersearch"


@pytest.mark.oracle
def test_workflow_file_command_review_binds_exact_bytes_and_idempotent_retry_needs_no_source_file(lattice_env) -> None:
    store = lattice_env["store"]
    pack_sha = WorkflowRegistry(TEST_WORKFLOW_PACK_ROOTS).load("quaternary")["canonical_sha256"]
    created = store.create_workflow_project_atomic(title="bound file command", purpose="", workflow_pack="quaternary", initial_state="semantic-intent", role_specs=[{"name": "Operator", "authority": "write", "instructions": "Operate."}], blackboard_content=b"{}", blackboard_source_ref="fixture:state", slice_external_ref=None, pack_canonical_sha256=pack_sha)
    project_id = created["project"]["project_id"]
    quaternary_root = lattice_env["config"].data_root / "quaternary"
    (quaternary_root / "tools").mkdir(parents=True)
    (quaternary_root / "quaternary.sqlite3").touch()
    (quaternary_root / "tools" / "gatherctl.py").write_text("# fixture", encoding="utf-8")
    source = lattice_env["config"].data_root / "profile.json"
    source.write_text('{"exact":"one"}', encoding="utf-8")
    config = replace(lattice_env["config"], axisvi_root=None, quaternary_root_override=quaternary_root)
    app = create_app(config)

    class RecordingAdapter:
        def __init__(self):
            self.calls = []
            self.state_revision = "a" * 64

        def inspect(self, _selector):
            return {"fixture": True, "state_sha256": self.state_revision}

        def command(self, operation, arguments):
            self.calls.append((operation, arguments))
            return {"receipt": {"fixture": True}, "output": {"ok": True}, "stdout": "{}", "stderr": ""}

    adapter = RecordingAdapter()
    app.state.workflows.adapters["quaternary"] = adapter
    store.refresh_workflow_blackboard(project_id, content=b'{"fixture":true,"state_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}', source_ref="fixture:bound-state")
    arguments = {"preparation_id": "qep_fixture", "input": str(source)}
    with TestClient(app) as client:
        csrf = client.get("/api/session").json()["csrf"]
        headers = {"x-lattice-csrf": csrf, "origin": "http://testserver"}
        review = client.post("/api/v1/workflow-packs/quaternary/commands/preflight", headers=headers, json={"project_id": project_id, "expected_revision": 1, "operation": "freeze-language-profile", "arguments": arguments})
        source.write_text('{"exact":"changed"}', encoding="utf-8")
        changed = client.post("/api/v1/workflow-packs/quaternary/commands", headers=headers, json={"project_id": project_id, "expected_revision": 1, "operation": "freeze-language-profile", "arguments": arguments, "idempotency_key": "bound-file-command-1", "review_sha256": review.json()["review_sha256"]})
        source.write_text('{"exact":"one"}', encoding="utf-8")
        accepted = client.post("/api/v1/workflow-packs/quaternary/commands", headers=headers, json={"project_id": project_id, "expected_revision": 1, "operation": "freeze-language-profile", "arguments": arguments, "idempotency_key": "bound-file-command-1", "review_sha256": review.json()["review_sha256"]})
        source.unlink()
        retried = client.post("/api/v1/workflow-packs/quaternary/commands", headers=headers, json={"project_id": project_id, "expected_revision": 1, "operation": "freeze-language-profile", "arguments": arguments, "idempotency_key": "bound-file-command-1", "review_sha256": review.json()["review_sha256"]})

    assert review.status_code == 200, review.text
    assert review.json()["file_receipts"][0]["content_sha256"]
    assert changed.status_code == 409
    assert adapter.calls == [("freeze-language-profile", {"preparation_id": "qep_fixture", "input": f"lattice:blob/{review.json()['file_receipts'][0]['content_sha256']}"})]
    assert accepted.status_code == 200, accepted.text
    assert retried.status_code == 200, retried.text
    assert retried.json()["command"]["command_id"] == accepted.json()["command"]["command_id"]


@pytest.mark.regression
def test_workflow_write_review_rejects_external_state_drift_before_dispatch(lattice_env) -> None:
    store = lattice_env["store"]
    pack_sha = WorkflowRegistry(TEST_WORKFLOW_PACK_ROOTS).load("quaternary")["canonical_sha256"]
    initial_revision = "1" * 64
    created = store.create_workflow_project_atomic(title="state-bound command", purpose="", workflow_pack="quaternary", initial_state="semantic-intent", role_specs=[{"name": "Operator", "authority": "write", "instructions": "Operate."}], blackboard_content=(f'{{"state_sha256":"{initial_revision}"}}').encode(), blackboard_source_ref="fixture:initial-state", slice_external_ref=None, selector={"language": "de"}, pack_canonical_sha256=pack_sha)
    project_id = created["project"]["project_id"]
    quaternary_root = lattice_env["config"].data_root / "drift-quaternary"
    (quaternary_root / "tools").mkdir(parents=True)
    (quaternary_root / "quaternary.sqlite3").touch()
    (quaternary_root / "tools" / "gatherctl.py").write_text("# fixture", encoding="utf-8")
    app = create_app(replace(lattice_env["config"], axisvi_root=None, quaternary_root_override=quaternary_root))

    class DriftingAdapter:
        revision = initial_revision
        calls: list[tuple[str, dict]] = []

        def inspect(self, _selector):
            return {"state_sha256": self.revision}

        def command(self, operation, arguments):
            self.calls.append((operation, arguments))
            return {"receipt": {}, "output": {}, "stdout": "{}", "stderr": ""}

    adapter = DriftingAdapter()
    app.state.workflows.adapters["quaternary"] = adapter
    with TestClient(app) as client:
        csrf = client.get("/api/session").json()["csrf"]
        headers = {"x-lattice-csrf": csrf, "origin": "http://testserver"}
        review = client.post("/api/v1/workflow-packs/quaternary/commands/preflight", headers=headers, json={"project_id": project_id, "expected_revision": 1, "operation": "prepare-language", "arguments": {"language": "de"}})
        adapter.revision = "2" * 64
        rejected = client.post("/api/v1/workflow-packs/quaternary/commands", headers=headers, json={"project_id": project_id, "expected_revision": 1, "operation": "prepare-language", "arguments": {"language": "de"}, "idempotency_key": "state-drift-command", "review_sha256": review.json()["review_sha256"]})

    assert review.status_code == 200, review.text
    assert rejected.status_code == 409
    assert "state changed" in rejected.text
    assert adapter.calls == []


@pytest.mark.oracle
def test_workflow_project_read_model_exposes_authoritative_blackboard_roles_and_triggers(lattice_env) -> None:
    store = lattice_env["store"]
    created = store.create_workflow_project_atomic(title="visible workflow", purpose="", workflow_pack="quaternary", initial_state="semantic-intent", role_specs=[{"name": "Native partner", "authority": "proposal-only", "instructions": "Interpret German."}], blackboard_content=b'{"language":"de","state":"calibrating"}', blackboard_source_ref="fixture:quaternary", slice_external_ref=None)

    project_id = created["project"]["project_id"]
    project = store.get_project(project_id)

    assert project["workflow"]["state"] == "semantic-intent"
    assert "content" not in project["workflow"]["blackboard"]
    assert project["workflow"]["blackboard"]["byte_count"] == len(b'{"language":"de","state":"calibrating"}')
    assert [role["name"] for role in project["roles"]] == ["Native partner"]
    assert project["workflow"]["triggers"] == []

    app = create_app(lattice_env["config"])
    with TestClient(app) as client:
        exact = client.get(f"/api/v1/context-blocks/{project['workflow']['blackboard']['block_id']}")
    assert exact.json()["content"] == '{"language":"de","state":"calibrating"}'


@pytest.mark.oracle
def test_oracle_workflow_blackboard_refresh_replaces_current_view_and_preserves_history(lattice_env) -> None:
    store = lattice_env["store"]
    created = store.create_workflow_project_atomic(title="refreshable", purpose="", workflow_pack="quaternary", initial_state="semantic-intent", role_specs=[{"name": "Operator", "authority": "read", "instructions": "Inspect."}], blackboard_content=b'{"generation":1}', blackboard_source_ref="fixture:g1", slice_external_ref="slate:refreshable", selector={"language": "de"})
    project_id = created["project"]["project_id"]

    refreshed = store.refresh_workflow_blackboard(project_id, content=b'{"generation":2}', source_ref="fixture:g2")
    project = store.get_project(project_id)

    assert "content" not in refreshed
    assert store.get_block(refreshed["block_id"])["content"] == '{"generation":2}'
    assert project["workflow"]["blackboard"]["block_id"] == refreshed["block_id"]
    assert project["workflow"]["selector"] == {"language": "de"}
    assert store.get_block(created["blackboard"]["block_id"])["status"] == "superseded"
    live_slice = store.get_slice(created["slice"]["slice_id"])
    assert {item["response"] for item in live_slice["items"]} == {'{"generation":1}', '{"generation":2}'}


@pytest.mark.oracle
def test_axial_projection_is_bounded_source_bound_and_contains_enduring_concerns(lattice_env) -> None:
    invocation = completed_invocation(lattice_env, "private prompt", "private response")
    lattice_env["lattice"].tag(invocation["invocation_id"], TagRequest(concern_ref="person:ada", label="Ada"))
    lattice_env["lattice"].tag(invocation["invocation_id"], TagRequest(concern_ref="slate:canon-quaternary-german", label="Canon > Quaternary > German"))
    app = create_app(lattice_env["config"])

    with TestClient(app) as client:
        project = client.get(f"/api/v1/projects/{lattice_env['project']['project_id']}").json()
        response = client.get(f"/api/v1/projects/{lattice_env['project']['project_id']}/axial-projection")

    projection = response.json()
    assert response.status_code == 200
    assert {item["kind"] for item in projection["concerns"]} == {"person", "slate"}
    assert all(set(item) == {"external_ref", "label", "kind"} for item in projection["concerns"])
    assert len(projection["projection_sha256"]) == 64
    assert "private prompt" not in response.text and "private response" not in response.text
    assert all("manifest_json" not in item and "compiled_prompt_sha256" not in item for item in project["trailheads"])


@pytest.mark.regression
def test_negative_control_axial_projection_is_bounded_at_its_authoritative_source(lattice_env, monkeypatch) -> None:
    invocation = completed_invocation(lattice_env, "bounded projection", "bounded result")
    lattice_env["lattice"].tag(invocation["invocation_id"], TagRequest(concern_ref="person:ada", label="Ada"))
    lattice_env["lattice"].tag(invocation["invocation_id"], TagRequest(concern_ref="person:grace", label="Grace"))
    monkeypatch.setattr("lattice.store.MAX_AXIAL_SOURCE_CLAIMS", 1)

    with pytest.raises(LatticeConflict, match="claim limit"):
        lattice_env["store"].project_enduring_graph(lattice_env["project"]["project_id"])


@pytest.mark.contract
def test_project_history_is_windowed_and_reusable_paths_remain_discoverable(lattice_env) -> None:
    invocations = [completed_invocation(lattice_env, f"prompt {index}", f"response {index}") for index in range(3)]
    template = lattice_env["lattice"].extract(lattice_env["project"]["project_id"], [invocations[0]["invocation_id"]], "Reusable path")
    app = create_app(lattice_env["config"])

    with TestClient(app) as client:
        project = client.get(f"/api/v1/projects/{lattice_env['project']['project_id']}").json()
        recent = client.get(f"/api/v1/projects/{lattice_env['project']['project_id']}/invocations", params={"limit": 2}).json()
        cursor = recent["next_before"]
        older = client.get(f"/api/v1/projects/{lattice_env['project']['project_id']}/invocations", params={"limit": 2, "before_created_at": cursor["created_at"], "before_invocation_id": cursor["invocation_id"]}).json()
        malformed = client.get(f"/api/v1/projects/{lattice_env['project']['project_id']}/invocations", params={"limit": 2, "before_created_at": cursor["created_at"]})
        recent_trailheads = client.get(f"/api/v1/projects/{lattice_env['project']['project_id']}/trailheads", params={"limit": 2}).json()
        trailhead_cursor = recent_trailheads["next_before"]
        older_trailheads = client.get(f"/api/v1/projects/{lattice_env['project']['project_id']}/trailheads", params={"limit": 2, "before_created_at": trailhead_cursor["created_at"], "before_invocation_id": trailhead_cursor["invocation_id"]}).json()
        malformed_trailhead_cursor = client.get(f"/api/v1/projects/{lattice_env['project']['project_id']}/trailheads", params={"limit": 2, "before_invocation_id": trailhead_cursor["invocation_id"]})

    assert len(recent["items"]) == 2
    assert recent["has_older"] is True
    assert len(older["items"]) == 1
    assert older["has_older"] is False
    assert {item["invocation_id"] for item in [*older["items"], *recent["items"]]} == {item["invocation_id"] for item in invocations}
    assert malformed.status_code == 409
    assert recent_trailheads["has_older"] is True
    assert older_trailheads["has_older"] is False
    assert {item["invocation_id"] for item in [*recent_trailheads["items"], *older_trailheads["items"]]} == {item["invocation_id"] for item in invocations}
    assert malformed_trailhead_cursor.status_code == 409
    assert project["invocation_page"] == {"has_older": False, "next_before": None}
    assert project["workflow_templates"] == [{
        "artifact_id": template["generated_object"]["artifact_id"],
        "artifact_kind": "workflow-template",
        "content_sha256": lattice_env["store"].get_artifact(template["generated_object"]["artifact_id"], include_content=False)["content_sha256"],
        "created_at": project["workflow_templates"][0]["created_at"],
    }]
    assert "generated_json" not in project["workflow_templates"][0] and "content" not in project["workflow_templates"][0]


@pytest.mark.contract
def test_blank_project_batch_api_fans_out_without_role_bootstrap(lattice_env, monkeypatch) -> None:
    async def fake_execute(_self, invocation_id, request):
        return {"attempt_id": f"attempt_{invocation_id[-8:]}", "invocation_id": invocation_id, "idempotency_key": request.idempotency_key, "event_cursor": 1}

    monkeypatch.setattr("lattice.server.ExecutionSupervisor.execute", fake_execute)
    app = create_app(lattice_env["config"])
    with TestClient(app) as client:
        csrf = client.get("/api/session").json()["csrf"]
        headers = {"x-lattice-csrf": csrf, "origin": "http://testserver"}
        project_response = client.post("/api/v1/projects", headers=headers, json={"title": "Blank conversation"})
        project = project_response.json()
        assembly = {"project_id": project["project_id"], "actor_binding": {"kind": "direct"}, "prompt": "Ask both", "parent_invocation_ids": []}
        request = {
            "batch_id": "batch_0123456789abcdef0123456789abcdef",
            "specifications": [
                {"spec_id": "codex", "assembly": assembly, "route": lattice_env["route"].model_dump(mode="json")},
                {"spec_id": "claude", "assembly": assembly, "route": ProviderRoute(provider="anthropic", model="claude-sonnet-4-6", harness="claude").model_dump(mode="json")},
            ],
        }
        preview = client.post("/api/v1/invocation-batches/preview", headers=headers, json=request)
        created = client.post("/api/v1/invocation-batches", headers=headers, json={**request, "idempotency_key": "browser-fanout-0001", "expected_preview_sha256": preview.json()["preview_sha256"], "confirmed_reason_sha256s": []})

    assert project_response.status_code == 200
    assert project["roles"] == [] and project["sessions"] == []
    assert preview.status_code == 200
    assert created.status_code == 202, created.text
    assert len(created.json()["invocation_ids"]) == 2
    assert {item["preflight"]["execution"]["provider"] for item in preview.json()["members"]} == {"openai", "anthropic"}
