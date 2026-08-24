"""Concurrency and terminalization assurance for model execution."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lattice.contracts import (
    ContextAssemblyV1,
    DirectActorBinding,
    DraftCreate,
    ExecuteRequest,
    InvocationBatchPreviewV1,
    InvocationSpecV1,
    ProviderRoute,
)
from lattice.supervisor import ExecutionSupervisor


def fake_journal(tmp_path: Path, *, native_session_id: str | None = None) -> Path:
    journal = tmp_path / "journal"
    journal.mkdir(exist_ok=True)
    (journal / "request.json").write_text('{"driver":"codex","model":"test","metadata":{"cli_version":"test-cli 1","binary":"/test/codex","isolation":{"working_directory":"fresh-temporary"}}}', encoding="utf-8")
    (journal / "checkpoint.json").write_text(__import__("json").dumps({"native_session": {"strategy": "codex", "id": native_session_id, "state_dir": None}, "observed": {"stdout_bytes": 0}}), encoding="utf-8")
    (journal / "terminal.json").write_text('{"status":"success"}', encoding="utf-8")
    return journal


@pytest.mark.asyncio
@pytest.mark.concurrency
async def test_concurrency_idempotent_execution_produces_one_attempt(monkeypatch, lattice_env, tmp_path) -> None:
    draft = lattice_env["store"].create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], role_session_id=lattice_env["session"]["role_session_id"], prompt="P1", route=lattice_env["route"]))
    invocation = lattice_env["lattice"].seal(draft["draft_id"])

    def fake_invoke(adapter, **kwargs):
        kwargs["on_chunk"]("stdout", b"working")
        return {"response": "finished", "artifacts": []}, fake_journal(tmp_path)

    monkeypatch.setattr("lattice.supervisor.invoke_subscription", fake_invoke)
    supervisor = ExecutionSupervisor(lattice_env["store"])
    await supervisor.start()
    first = await supervisor.execute(invocation["invocation_id"], ExecuteRequest(idempotency_key="same-request-0001"))
    second = await supervisor.execute(invocation["invocation_id"], ExecuteRequest(idempotency_key="same-request-0001"))
    await asyncio.gather(*list(supervisor._tasks.values()))
    result = lattice_env["store"].get_invocation(invocation["invocation_id"])

    assert first["attempt_id"] == second["attempt_id"]
    assert first["event_cursor"] > 0
    assert any(event["event_type"] == "attempt.chunk" and event["subject_id"] == first["attempt_id"] for event in lattice_env["store"].events_after(lattice_env["project"]["project_id"], first["event_cursor"]))
    assert len(result["attempts"]) == 1
    assert result["state"] == "succeeded"
    assert result["output_block"]["content"] == "finished"


@pytest.mark.asyncio
@pytest.mark.contract
async def test_contract_unexpected_execution_error_terminalizes_attempt(monkeypatch, lattice_env) -> None:
    draft = lattice_env["store"].create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], role_session_id=lattice_env["session"]["role_session_id"], prompt="P1", route=lattice_env["route"]))
    invocation = lattice_env["lattice"].seal(draft["draft_id"])

    def explode(*args, **kwargs):
        raise OSError("planted execution failure")

    monkeypatch.setattr("lattice.supervisor.invoke_subscription", explode)
    supervisor = ExecutionSupervisor(lattice_env["store"])
    attempt = await supervisor.execute(invocation["invocation_id"], ExecuteRequest(idempotency_key="failure-request-001"))
    await asyncio.gather(*list(supervisor._tasks.values()))
    terminal = lattice_env["store"].get_attempt(attempt["attempt_id"])

    assert terminal["state"] == "failed"
    assert terminal["failure_kind"] == "harness_failure"
    assert "planted execution failure" in terminal["error_text"]


@pytest.mark.oracle
@pytest.mark.asyncio
async def test_oracle_provider_artifacts_are_content_addressed_and_linked(monkeypatch, lattice_env, tmp_path) -> None:
    draft = lattice_env["store"].create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], role_session_id=lattice_env["session"]["role_session_id"], prompt="make artifact", route=lattice_env["route"]))
    invocation = lattice_env["lattice"].seal(draft["draft_id"])

    def fake_invoke(*args, **kwargs):
        return {"response": "made it", "artifacts": [{"name": "answer.txt", "media_type": "text/plain", "content": "exact artifact bytes"}]}, fake_journal(tmp_path)

    monkeypatch.setattr("lattice.supervisor.invoke_subscription", fake_invoke)
    supervisor = ExecutionSupervisor(lattice_env["store"])
    await supervisor.start()
    await supervisor.execute(invocation["invocation_id"], ExecuteRequest(idempotency_key="artifact-request-001"))
    await asyncio.gather(*list(supervisor._tasks.values()))
    result = lattice_env["store"].get_invocation(invocation["invocation_id"])

    assert len(result["artifacts"]) == 1
    artifact = result["artifacts"][0]
    assert lattice_env["store"].blobs.get(artifact["content_sha256"]) == b"exact artifact bytes"
    assert any(edge["target_id"] == artifact["artifact_id"] and edge["relation"] == "produced" for edge in result["edges"])
    assert result["attempts"][0]["runtime_receipt"]["cli_version"] == "test-cli 1"
    assert result["attempts"][0]["runtime_receipt"]["native_session"]["strategy"] == "codex"


@pytest.mark.oracle
@pytest.mark.asyncio
@pytest.mark.parametrize("continuation_mode", ["resume", "fork"])
async def test_oracle_native_continuation_uses_the_selected_parent_receipt_and_exact_authority(monkeypatch, lattice_env, tmp_path, continuation_mode) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent_route = ProviderRoute(
        provider="openai",
        model="gpt-5.6-sol",
        harness="codex",
        execution_profile="workspace-agent",
        tools=["shell"],
        workspace=str(workspace),
        sandbox="read-only",
    )
    parent_draft = lattice_env["store"].create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], actor_binding=DirectActorBinding(), prompt="start durable session", route=parent_route))
    parent = lattice_env["lattice"].seal(parent_draft["draft_id"])
    session_id = "11111111-1111-4111-8111-111111111111"

    def start_session(*args, **kwargs):
        return {"response": "persistent result", "artifacts": []}, fake_journal(tmp_path, native_session_id=session_id)

    monkeypatch.setattr("lattice.supervisor.invoke_subscription", start_session)
    supervisor = ExecutionSupervisor(lattice_env["store"])
    await supervisor.start()
    await supervisor.execute(parent["invocation_id"], ExecuteRequest(idempotency_key=f"workspace-parent-{continuation_mode}"))
    await asyncio.gather(*list(supervisor._tasks.values()))

    continuation_route = ProviderRoute(
        provider="openai",
        model="gpt-5.6-sol",
        harness="codex",
        execution_profile="native-continuation",
        session_policy="visible_native_continuation",
        continuation_mode=continuation_mode,
        native_parent_invocation_id=parent["invocation_id"],
        tools=["shell"],
        workspace=str(workspace),
        sandbox="read-only",
    )
    assembly = ContextAssemblyV1(
        project_id=lattice_env["project"]["project_id"],
        prompt="continue exactly",
        parent_invocation_ids=[parent["invocation_id"]],
    )
    preview = lattice_env["lattice"].preview_invocation_batch(InvocationBatchPreviewV1(
        batch_id="batch_" + (("1" if continuation_mode == "resume" else "2") * 32),
        specifications=[InvocationSpecV1(spec_id="codex", assembly=assembly, route=continuation_route)],
    ))

    execution = preview["members"][0]["preflight"]["execution"]
    assert execution["native_session_id"] == session_id
    assert execution["native_parent_invocation_id"] == parent["invocation_id"]
    assert preview["members"][0]["preflight"]["capabilities"]["workspace"] == str(workspace.resolve())


@pytest.mark.contract
@pytest.mark.asyncio
async def test_negative_control_native_continuation_cannot_cross_workspace_or_invent_session(monkeypatch, lattice_env, tmp_path) -> None:
    workspace = tmp_path / "workspace"
    other_workspace = tmp_path / "other"
    workspace.mkdir()
    other_workspace.mkdir()
    route = ProviderRoute(provider="openai", model="gpt-5.6-sol", harness="codex", execution_profile="workspace-agent", tools=["shell"], workspace=str(workspace))
    draft = lattice_env["store"].create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], actor_binding=DirectActorBinding(), prompt="start", route=route))
    parent = lattice_env["lattice"].seal(draft["draft_id"])

    monkeypatch.setattr("lattice.supervisor.invoke_subscription", lambda *args, **kwargs: ({"response": "done", "artifacts": []}, fake_journal(tmp_path, native_session_id="11111111-1111-4111-8111-111111111111")))
    supervisor = ExecutionSupervisor(lattice_env["store"])
    await supervisor.start()
    await supervisor.execute(parent["invocation_id"], ExecuteRequest(idempotency_key="native-negative-parent"))
    await asyncio.gather(*list(supervisor._tasks.values()))

    for mutation in (
        {"workspace": str(other_workspace)},
        {"native_session_id": "22222222-2222-4222-8222-222222222222"},
    ):
        child_route = ProviderRoute(
            provider="openai",
            model="gpt-5.6-sol",
            harness="codex",
            execution_profile="native-continuation",
            session_policy="visible_native_continuation",
            continuation_mode="resume",
            native_parent_invocation_id=parent["invocation_id"],
            tools=["shell"],
            workspace=mutation.get("workspace", str(workspace)),
            native_session_id=mutation.get("native_session_id"),
        )
        with pytest.raises(Exception, match="authority boundaries|disagrees with the parent receipt"):
            lattice_env["store"].create_draft(DraftCreate(
                project_id=lattice_env["project"]["project_id"],
                actor_binding=DirectActorBinding(),
                prompt="smuggle",
                route=child_route,
                parent_invocation_ids=[parent["invocation_id"]],
            ))
