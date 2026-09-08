"""Runner substitution requires a distinct, evidenced operator decision."""

import pytest
from fastapi.testclient import TestClient

from lattice import LatticeStore
from lattice import route_amendments as routes
from lattice.contracts import AttemptState, BlockStatus, ContextBlockCreate, DraftCreate, ProviderRoute, RoleSessionCreate, RouteAmendmentDecision, RouteAmendmentRequest
from lattice.server import create_app
from lattice.security import ensure_cli_token
from lattice.store import LatticeConflict


def _seal(env, route=None):
    draft = env["store"].create_draft(DraftCreate(project_id=env["project"]["project_id"], role_session_id=env["session"]["role_session_id"], prompt="interpret source-backed patterns", route=route or env["route"]))
    return env["lattice"].seal(draft["draft_id"])


def _request(env):
    invocation = _seal(env)
    store = env["store"]
    attempt = store.create_attempt(invocation["invocation_id"], "source-failure", "test", 9_999_999_999)
    store.finish_attempt(attempt["attempt_id"], state=AttemptState.failed, failure_kind="timeout", error_text="planted transport failure")
    failure = store.create_context_block(ContextBlockCreate(project_id=env["project"]["project_id"], semantic_type="runner-failure", content="planted failure receipt", authority="test", evidence_level="observed"))
    qualification = store.create_context_block(ContextBlockCreate(project_id=env["project"]["project_id"], semantic_type="task-qualification", content="planted independent qualification for this lane and role", authority="test", evidence_level="passed"))
    request = RouteAmendmentRequest(source_invocation_id=invocation["invocation_id"], proposed_route=ProviderRoute(provider="anthropic", harness="claude", model="claude-opus-5"), reason="The current runner timed out; request reserve for the qualified task.", failure_evidence_block_id=failure["block_id"], qualification_evidence_block_id=qualification["block_id"])
    return invocation, request


@pytest.mark.contract
def test_pause_survives_restart_and_blocks_presealed_dispatch(lattice_env):
    env = lattice_env
    store = env["store"]
    session = env["session"]["role_session_id"]
    source, request = _request(env)
    presealed = _seal(env)
    pending = routes.request(store, session, request)
    assert pending["state"] == "awaiting-operator"
    assert pending["current_route"]["model"] == "gpt-5.6-sol"
    assert routes.request(store, session, request) == pending
    reopened = LatticeStore(env["config"])
    assert routes.inspect(reopened, session) == pending
    for invocation in (source, presealed):
        with pytest.raises(LatticeConflict, match="awaits operator"):
            reopened.create_attempt(invocation["invocation_id"], invocation["invocation_id"], "test", 9_999_999_999)
    with pytest.raises(LatticeConflict, match="awaits operator"):
        _seal(env, request.proposed_route)
    assert len(reopened.get_invocation(source["invocation_id"])["attempts"]) == 1
    assert not reopened.get_invocation(presealed["invocation_id"])["attempts"]
    independent = dict(env)
    independent["session"] = store.create_role_session(RoleSessionCreate(project_id=env["project"]["project_id"], role_id=env["role"]["role_id"], label="independent work"))
    unaffected = _seal(independent)
    assert store.create_attempt(unaffected["invocation_id"], "independent", "test", 9_999_999_999)["state"] == "leased"


@pytest.mark.contract
def test_approval_records_exact_change_without_rewriting_or_auto_dispatch(lattice_env):
    env = lattice_env
    store = env["store"]
    session = env["session"]["role_session_id"]
    source, request = _request(env)
    old = store.get_invocation(source["invocation_id"])
    pending = routes.request(store, session, request)["pending"]
    decision = RouteAmendmentDecision(decision="approve", note="Operator reviewed failure and role/lane qualification.")
    view = routes.decide(store, session, pending["amendment_id"], decision)
    assert view["state"] == "active"
    assert view["current_route"]["model"] == "claude-opus-5"
    assert len(view["history"]) == 2
    assert view["history"][0]["source_manifest_sha256"] == source["manifest_sha256"]
    assert view["history"][1]["request_event_id"] == pending["event_id"]
    assert routes.decide(store, session, pending["amendment_id"], decision) == view
    assert store.get_invocation(source["invocation_id"]) == old
    reserve = _seal(env, request.proposed_route)
    assert store.create_attempt(reserve["invocation_id"], "approved-reserve", "test", 9_999_999_999)["state"] == "leased"
    with pytest.raises(LatticeConflict, match="recorded route amendment"):
        store.create_attempt(source["invocation_id"], "stale-primary", "test", 9_999_999_999)


@pytest.mark.contract
def test_stale_evidence_cannot_approve_but_operator_can_resume_primary(lattice_env):
    env = lattice_env
    store = env["store"]
    session = env["session"]["role_session_id"]
    source, request = _request(env)
    pending = routes.request(store, session, request)["pending"]
    store.set_block_status(request.qualification_evidence_block_id, BlockStatus.superseded)
    with pytest.raises(LatticeConflict, match="evidence must be active"):
        routes.decide(store, session, pending["amendment_id"], RouteAmendmentDecision(decision="approve", note="stale approval"))
    assert routes.inspect(store, session)["state"] == "awaiting-operator"
    view = routes.decide(store, session, pending["amendment_id"], RouteAmendmentDecision(decision="resume-current", note="Operator repaired the primary; do not use reserve."))
    assert view["current_route"]["model"] == "gpt-5.6-sol"
    assert store.create_attempt(source["invocation_id"], "repaired-primary", "test", 9_999_999_999)["state"] == "leased"


@pytest.mark.contract
def test_active_attempt_prevents_racing_route_change(lattice_env):
    env = lattice_env
    _, request = _request(env)
    presealed = _seal(env)
    env["store"].create_attempt(presealed["invocation_id"], "in-flight", "test", 9_999_999_999)
    with pytest.raises(LatticeConflict, match="finish or cancel"):
        routes.request(env["store"], env["session"]["role_session_id"], request)


@pytest.mark.contract
def test_route_approval_is_separate_authenticated_api_action(lattice_env):
    env = lattice_env
    _, request = _request(env)
    url = f"/api/v1/role-sessions/{env['session']['role_session_id']}/route-amendments"
    with TestClient(create_app(env["config"])) as client:
        assert client.post(url, json=request.model_dump(mode="json")).status_code == 403
        headers = {"Authorization": f"Bearer {ensure_cli_token(env['config'].operator_token)}"}
        response = client.post(url, headers=headers, json=request.model_dump(mode="json"))
        assert response.status_code == 200, response.text
        pending = response.json()["pending"]
        decision_url = f"{url}/{pending['amendment_id']}/decision"
        assert client.post(decision_url, json={"decision": "approve", "note": "untrusted"}).status_code == 403
        assert client.get(url, headers=headers).json()["state"] == "awaiting-operator"
        response = client.post(decision_url, headers=headers, json={"decision": "approve", "note": "Operator explicitly approved this runner."})
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "active"


@pytest.mark.contract
def test_workflow_default_can_only_be_overridden_by_approved_amendment(lattice_env):
    env = dict(lattice_env)
    created = env["store"].create_workflow_project_atomic(title="native lane", purpose="", workflow_pack="quaternary", initial_state="native-interpretation", role_specs=[{"role_id": "native-partner", "name": "Native partner", "provider": "openai", "harness": "codex", "allowed_models": ["gpt-5.6-sol"], "authority": "interpret-only", "instructions": "Interpret source patterns."}], blackboard_content=b"{}", blackboard_source_ref="fixture:state", slice_external_ref=None)
    env["project"] = created["project"]
    env["session"] = created["project"]["sessions"][0]
    _, request = _request(env)
    with pytest.raises(LatticeConflict, match="bound to openai"):
        _seal(env, request.proposed_route)
    pending = routes.request(env["store"], env["session"]["role_session_id"], request)["pending"]
    routes.decide(env["store"], env["session"]["role_session_id"], pending["amendment_id"], RouteAmendmentDecision(decision="approve", note="Operator reviewed this exception."))
    assert _seal(env, request.proposed_route)["route"]["model"] == "claude-opus-5"
    assert env["store"].get_role(env["session"]["role_id"])["route_policy"]["provider"] == "openai"


@pytest.mark.contract
def test_bypass_mutant_is_rejected_by_dispatch_regression(lattice_env, monkeypatch):
    # Isolated in-memory production mutant; the ordinary test above supplies the
    # independent expected behavior. Fixture data stays in pytest's temp root.
    monkeypatch.setattr(routes, "assert_route", lambda *_args: None)
    with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
        test_pause_survives_restart_and_blocks_presealed_dispatch(lattice_env)


@pytest.mark.contract
def test_request_cannot_use_missing_or_shared_evidence(lattice_env):
    env = lattice_env
    _, request = _request(env)
    session = env["session"]["role_session_id"]
    for block_id, message in (("missing", "evidence must be active"), (request.failure_evidence_block_id, "separate evidence blocks")):
        invalid = request.model_copy(update={"qualification_evidence_block_id": block_id})
        with pytest.raises(LatticeConflict, match=message):
            routes.request(env["store"], session, invalid)
    assert routes.inspect(env["store"], session)["history"] == []


@pytest.mark.contract
def test_cli_cannot_combine_request_with_approval(monkeypatch):
    from lattice.cli import command_route_amendment, parser
    from lattice.client import LatticeClient

    # A malformed combined action must fail before reading a request or calling
    # the service; no real credential or external state participates.
    monkeypatch.setattr(LatticeClient, "from_environment", lambda: object())
    args = parser().parse_args(["route-amendment", "session", "--request", "unused.json", "--decision", "approve"])
    with pytest.raises(ValueError, match="cannot also approve"):
        command_route_amendment(args)
