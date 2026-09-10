"""Contract checks for the thin authenticated Lattice client."""

from __future__ import annotations

import pytest

from lattice.client import LatticeClient, LatticeClientError
from lattice.contracts import ContextAssemblyV1, ContextSelection, DirectActorBinding, InvocationBatchCreateV1, InvocationBatchPreviewV1, InvocationSpecV1, ProjectCreate, ProviderRoute


def _batch_preview() -> InvocationBatchPreviewV1:
    return InvocationBatchPreviewV1(
        batch_id="batch_0123456789abcdef0123456789abcdef",
        specifications=[
            InvocationSpecV1(
                spec_id="voice-modality",
                assembly=ContextAssemblyV1(
                    project_id="project_0123456789abcdef0123456789abcdef",
                    actor_binding=DirectActorBinding(),
                    context_mode="explicit",
                    prompt="Write the contracted artifact.",
                    selections=[ContextSelection(block_id="virtual:current-prompt", plane="current_prompt", label="current campaign prompt")],
                ),
                route=ProviderRoute(provider="anthropic", harness="claude", model="claude-fable-5", effort="xhigh"),
            )
        ],
    )


def test_contract_cancel_attempt_uses_the_attempt_resource(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    def request(_self, method, path, payload=None, **_kwargs):
        calls.append((method, path, payload))
        return {"attempt_id": "attempt/a", "state": "cancelling"}

    monkeypatch.setattr(LatticeClient, "_request", request)
    client = LatticeClient("http://127.0.0.1:9010", "token", "127.0.0.1:9010")

    result = client.cancel_attempt("attempt/a")

    assert result == {"attempt_id": "attempt/a", "state": "cancelling"}
    assert calls == [("POST", "/api/v1/attempts/attempt%2Fa/cancel", {})]


def test_contract_route_decision_is_a_separate_exact_resource(monkeypatch) -> None:
    calls = []

    def request(_self, method, path, payload=None, **_kwargs):
        calls.append((method, path, payload))
        return {"state": "active"}

    monkeypatch.setattr(LatticeClient, "_request", request)
    client = LatticeClient("http://127.0.0.1:9010", "token", "127.0.0.1:9010")
    client.route_amendments("session/a")
    client.decide_route_amendment("session/a", "amendment/b", decision="approve", note="Explicit operator approval")
    assert calls == [
        ("GET", "/api/v1/role-sessions/session%2Fa/route-amendments", None),
        ("POST", "/api/v1/role-sessions/session%2Fa/route-amendments/amendment%2Fb/decision", {"decision": "approve", "note": "Explicit operator approval"}),
    ]


def test_contract_project_creation_validates_and_sends_only_manual_fields(monkeypatch) -> None:
    calls = []

    def request(_self, method, path, payload=None, **_kwargs):
        calls.append((method, path, payload))
        return {"project_id": "project_0123456789abcdef0123456789abcdef", "title": payload["title"]}

    monkeypatch.setattr(LatticeClient, "_request", request)
    client = LatticeClient("http://127.0.0.1:9010", "token", "127.0.0.1:9010")

    created = client.create_project(ProjectCreate(title="Philosophical corpus", purpose="Exact corpus campaign"))

    assert created["project_id"].startswith("project_")
    assert calls == [("POST", "/api/v1/projects", {"title": "Philosophical corpus", "purpose": "Exact corpus campaign"})]
    with pytest.raises(ValueError, match="does not accept a workflow pack"):
        client.create_project(ProjectCreate(title="Wrong boundary", workflow_pack="quaternary"))


def test_contract_workflow_client_uses_pack_bound_resources(monkeypatch) -> None:
    calls = []
    project_id = "project_0123456789abcdef0123456789abcdef"

    def request(_self, method, path, payload=None, **_kwargs):
        calls.append((method, path, payload))
        if path.endswith("/projects"):
            return {"project": {"project_id": project_id}}
        if path.endswith("/refresh"):
            return {"workflow": {"project_id": project_id, "revision": 1}, "blackboard": {"block_id": "block_fixture"}}
        if path.endswith("/adapter-evidence"):
            return {"block_id": "block_fixture", "issuer_kind": "workflow-adapter"}
        if path.endswith("/transitions"):
            return {"workflow": {"project_id": project_id, "revision": 2}}
        if path.startswith("/api/v1/workflow-projects/"):
            return {"project_id": project_id, "revision": 1}
        return {"project_id": project_id, "workflow": {"revision": 1}}

    monkeypatch.setattr(LatticeClient, "_request", request)
    client = LatticeClient("http://127.0.0.1:9010", "token", "127.0.0.1:9010")
    created = client.create_workflow_project("quaternary", {"title": "German", "selector": {"language": "de"}})
    workflow = client.workflow_project(project_id)
    refreshed = client.refresh_workflow_project("quaternary", project_id)
    evidence = client.create_workflow_adapter_evidence("quaternary", project_id, {"expected_revision": 1, "evidence_kind": "prevalidated-query-arm", "subject": {"arm_id": "arm_1"}})
    transition = client.transition_workflow("quaternary", {"project_id": project_id, "expected_revision": 1, "from_state": "coverage", "to_state": "query-frozen", "evidence_block_ids": [evidence["block_id"]]})

    assert created["project"]["project_id"] == project_id
    assert workflow["revision"] == 1
    assert refreshed["blackboard"]["block_id"] == "block_fixture"
    assert transition["workflow"]["revision"] == 2
    assert [call[:2] for call in calls] == [
        ("POST", "/api/v1/workflow-packs/quaternary/projects"),
        ("GET", f"/api/v1/workflow-projects/{project_id}"),
        ("POST", f"/api/v1/workflow-packs/quaternary/projects/{project_id}/refresh"),
        ("POST", f"/api/v1/workflow-packs/quaternary/projects/{project_id}/adapter-evidence"),
        ("POST", "/api/v1/workflow-packs/quaternary/transitions"),
    ]


def test_contract_workflow_cli_reads_the_role_session_view_and_exposes_refresh(monkeypatch, capsys) -> None:
    from lattice.cli import parser

    calls: list[tuple[str, ...]] = []

    class FakeClient:
        def workflow_project(self, project_id):
            calls.append(("workflow-project", project_id))
            return {"project_id": project_id, "workflow": {"revision": 3}, "roles": [], "sessions": []}

        def refresh_workflow_project(self, pack_id, project_id):
            calls.append(("workflow-refresh", pack_id, project_id))
            return {"workflow": {"revision": 3}, "blackboard": {"block_id": "block_current"}}

        def project(self, _project_id):
            raise AssertionError("workflow-project must not collapse to the generic project view")

    monkeypatch.setattr(LatticeClient, "from_environment", lambda: FakeClient())
    read_args = parser().parse_args(["workflow-project", "project_fixture"])
    refresh_args = parser().parse_args(["workflow-refresh", "quaternary", "project_fixture"])

    assert read_args.handler(read_args) == 0
    assert refresh_args.handler(refresh_args) == 0

    output = capsys.readouterr().out
    assert '"roles": []' in output
    assert '"block_id": "block_current"' in output
    assert calls == [
        ("workflow-project", "project_fixture"),
        ("workflow-refresh", "quaternary", "project_fixture"),
    ]


def test_contract_cli_executes_an_existing_invocation_with_an_explicit_idempotency_key(monkeypatch, capsys) -> None:
    from lattice.cli import parser

    calls: list[tuple[str, str]] = []

    class FakeClient:
        def execute_invocation(self, invocation_id, idempotency_key):
            calls.append((invocation_id, idempotency_key))
            return {"attempt_id": "attempt_retry", "state": "leased"}

    monkeypatch.setattr(LatticeClient, "from_environment", lambda: FakeClient())
    args = parser().parse_args(
        ["execute-invocation", "invocation_fixture", "--idempotency-key", "canon-retry:fixture:2"]
    )

    assert args.handler(args) == 0
    assert calls == [("invocation_fixture", "canon-retry:fixture:2")]
    assert '"attempt_id": "attempt_retry"' in capsys.readouterr().out


def test_contract_invocation_batch_methods_validate_payloads_and_resources(monkeypatch) -> None:
    calls = []
    preview_request = _batch_preview()
    preview_sha = "a" * 64

    def request(_self, method, path, payload=None, **_kwargs):
        calls.append((method, path, payload))
        if path.endswith("/preview"):
            return {"batch_id": preview_request.batch_id, "preview_sha256": preview_sha, "members": []}
        if method == "POST":
            return {"batch_id": preview_request.batch_id, "members": [{"spec_id": "voice-modality"}]}
        return {"batch_id": preview_request.batch_id, "members": []}

    monkeypatch.setattr(LatticeClient, "_request", request)
    client = LatticeClient("http://127.0.0.1:9010", "token", "127.0.0.1:9010")

    preview = client.preview_invocation_batch(preview_request)
    create_request = InvocationBatchCreateV1(
        **preview_request.model_dump(mode="json"),
        idempotency_key="campaign-batch-0001",
        expected_preview_sha256=preview["preview_sha256"],
        confirmed_reason_sha256s=[],
    )
    created = client.create_invocation_batch(create_request)
    inspected = client.invocation_batch(preview_request.batch_id)

    assert created["members"][0]["spec_id"] == "voice-modality"
    assert inspected["batch_id"] == preview_request.batch_id
    assert [call[:2] for call in calls] == [
        ("POST", "/api/v1/invocation-batches/preview"),
        ("POST", "/api/v1/invocation-batches"),
        ("GET", f"/api/v1/invocation-batches/{preview_request.batch_id}"),
    ]


def test_contract_invocation_batch_methods_reject_mismatched_responses(monkeypatch) -> None:
    monkeypatch.setattr(LatticeClient, "_request", lambda *_args, **_kwargs: {"batch_id": "batch_wrong", "members": []})
    client = LatticeClient("http://127.0.0.1:9010", "token", "127.0.0.1:9010")
    with pytest.raises(LatticeClientError, match="preview response is malformed"):
        client.preview_invocation_batch(_batch_preview())
    with pytest.raises(ValueError, match="cannot be empty"):
        client.invocation_batch("")
