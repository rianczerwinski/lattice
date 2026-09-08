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
