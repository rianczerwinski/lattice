"""Concurrency and terminalization assurance for model execution."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lattice.contracts import (
    AttemptState,
    ContextAssemblyV1,
    DirectActorBinding,
    DraftCreate,
    ExecuteRequest,
    InvocationBatchCreateV1,
    InvocationBatchPreviewV1,
    InvocationSpecV1,
    ProviderRoute,
)
from lattice.canonical import opaque_id
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
    attempt_events = [event for event in lattice_env["store"].events_after(lattice_env["project"]["project_id"], first["event_cursor"]) if event["subject_id"] == first["attempt_id"]]
    assert any(event["event_type"] == "attempt.chunk" for event in attempt_events)
    assert all(event["payload"]["invocation_id"] == invocation["invocation_id"] for event in attempt_events)
    assert len(result["attempts"]) == 1
    assert result["state"] == "succeeded"
    assert result["output_block"]["content"] == "finished"


@pytest.mark.asyncio
@pytest.mark.oracle
async def test_supervisor_delivers_the_manifest_bound_claude_system_instructions(monkeypatch, lattice_env, tmp_path) -> None:
    route = ProviderRoute(provider="anthropic", model="sonnet", harness="claude", effort="medium")
    draft = lattice_env["store"].create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], role_session_id=lattice_env["session"]["role_session_id"], prompt="Read routed context", route=route))
    invocation = lattice_env["lattice"].seal(draft["draft_id"])
    observed = {}

    def fake_invoke(adapter, **kwargs):
        observed.update(kwargs)
        return {"response": "accepted", "artifacts": []}, fake_journal(tmp_path)

    monkeypatch.setattr("lattice.supervisor.invoke_subscription", fake_invoke)
    supervisor = ExecutionSupervisor(lattice_env["store"])
    await supervisor.start()
    await supervisor.execute(invocation["invocation_id"], ExecuteRequest(idempotency_key="claude-system-channel-0001"))
    await asyncio.gather(*list(supervisor._tasks.values()))

    declared = invocation["manifest"]["provider_boundary"]["system_instructions"]
    assert declared["channel"] == "official-system-prompt-file"
    assert observed["system_prompt"] == declared["content"]
    assert observed["prompt"].startswith("LATTICE-CONVERSATION/10\n")


@pytest.mark.asyncio
@pytest.mark.regression
async def test_restart_dispatches_only_batch_members_that_never_received_an_attempt(monkeypatch, lattice_env, tmp_path) -> None:
    assembly = ContextAssemblyV1(
        project_id=lattice_env["project"]["project_id"],
        actor_binding=DirectActorBinding(),
        prompt="fan out durably",
    )
    request = InvocationBatchPreviewV1(
        batch_id=opaque_id("batch"),
        specifications=[
            InvocationSpecV1(spec_id="codex", assembly=assembly, route=lattice_env["route"]),
            InvocationSpecV1(
                spec_id="claude",
                assembly=assembly,
                route=ProviderRoute(provider="anthropic", model="claude-sonnet-4-6", harness="claude"),
            ),
        ],
    )
    preview = lattice_env["lattice"].preview_invocation_batch(request)
    batch = lattice_env["lattice"].create_invocation_batch(InvocationBatchCreateV1(
        **request.model_dump(mode="json"),
        idempotency_key="batch-restart-create-0001",
        expected_preview_sha256=preview["preview_sha256"],
        confirmed_reason_sha256s=[item["reason_sha256"] for item in preview["confirmation_reasons"]],
    ))
    first, second = batch["members"]
    first_attempt = lattice_env["store"].create_attempt(
        first["invocation_id"],
        f"batch:{request.batch_id}:{first['spec_id']}",
        "dead-boot",
        2_000_000_000,
    )

    monkeypatch.setattr(
        "lattice.supervisor.invoke_subscription",
        lambda *args, **kwargs: ({"response": "recovered sibling", "artifacts": []}, fake_journal(tmp_path)),
    )
    supervisor = ExecutionSupervisor(lattice_env["store"])
    reconciled = await supervisor.start()
    await asyncio.gather(*list(supervisor._tasks.values()))

    first_after = lattice_env["store"].get_invocation(first["invocation_id"])
    second_after = lattice_env["store"].get_invocation(second["invocation_id"])
    assert reconciled == 2
    assert len(first_after["attempts"]) == 1
    assert first_after["attempts"][0]["attempt_id"] == first_attempt["attempt_id"]
    assert first_after["attempts"][0]["state"] == "orphaned"
    assert len(second_after["attempts"]) == 1
    assert second_after["attempts"][0]["idempotency_key"] == f"batch:{request.batch_id}:{second['spec_id']}"
    assert second_after["state"] == "succeeded"


@pytest.mark.asyncio
@pytest.mark.regression
async def test_negative_control_one_poisoned_recovery_member_cannot_starve_later_batches(monkeypatch, lattice_env) -> None:
    project_id = lattice_env["project"]["project_id"]
    supervisor = ExecutionSupervisor(lattice_env["store"])
    members = [
        {"batch_id": "batch_poisoned", "project_id": project_id, "spec_id": "first", "invocation_id": "invocation_poisoned"},
        {"batch_id": "batch_healthy", "project_id": project_id, "spec_id": "second", "invocation_id": "invocation_healthy"},
    ]
    attempted: list[str] = []

    async def fake_execute(invocation_id, _request):
        attempted.append(invocation_id)
        if invocation_id == "invocation_poisoned":
            raise OSError("planted dispatch poison")
        return {"attempt_id": "attempt_healthy"}

    monkeypatch.setattr(lattice_env["store"], "list_undispatched_batch_members", lambda: members)
    monkeypatch.setattr(supervisor, "execute", fake_execute)

    reconciled = await supervisor.start()

    assert reconciled == 1
    assert attempted == ["invocation_poisoned", "invocation_healthy"]
    assert supervisor.startup_recovery_failures == [{"batch_id": "batch_poisoned", "spec_id": "first", "invocation_id": "invocation_poisoned", "error": "OSError: planted dispatch poison"}]
    assert any(event["event_type"] == "invocation-batch.recovery-failed" for event in lattice_env["store"].events_after(project_id, 0))


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
    terminal_event = next(event for event in lattice_env["store"].events_after(lattice_env["project"]["project_id"], 0) if event["event_type"] == "attempt.failed" and event["subject_id"] == attempt["attempt_id"])
    assert terminal_event["payload"]["invocation_id"] == invocation["invocation_id"]


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
    provider_boundary = preview["members"][0]["preflight"]["provider_boundary"]
    assert execution["native_session_id"] == session_id
    assert execution["native_parent_invocation_id"] == parent["invocation_id"]
    assert preview["members"][0]["preflight"]["capabilities"]["workspace"] == str(workspace.resolve())
    assert provider_boundary["opaque_contexts"] == [{
        "kind": "provider-native-session-history",
        "disposition": "provider_opaque",
        "provider": "openai",
        "harness": "codex",
        "native_session_id": session_id,
        "native_parent_invocation_id": parent["invocation_id"],
        "continuation_mode": continuation_mode,
        "statement": "The official provider harness may add prior native-session messages and compaction state that Lattice cannot inspect byte-for-byte.",
    }]
    assert any("native session history and compaction state" in limit for limit in provider_boundary["proof_limits"])

    sealed = lattice_env["lattice"].create_invocation_batch(InvocationBatchCreateV1(
        batch_id=preview["batch_id"],
        specifications=[InvocationSpecV1(spec_id="codex", assembly=assembly, route=continuation_route)],
        idempotency_key=f"native-opaque-{continuation_mode}",
        expected_preview_sha256=preview["preview_sha256"],
        confirmed_reason_sha256s=[item["reason_sha256"] for item in preview["confirmation_reasons"]],
    ))
    child = lattice_env["lattice"].inspect(sealed["invocation_ids"][0])
    assert child["manifest_verification"]["valid"] is True
    assert child["manifest"]["provider_boundary"]["opaque_contexts"] == provider_boundary["opaque_contexts"]


@pytest.mark.concurrency
@pytest.mark.asyncio
async def test_native_session_lease_serializes_resume_and_recovers_after_restart(monkeypatch, lattice_env, tmp_path) -> None:
    workspace = tmp_path / "lease-workspace"
    workspace.mkdir()
    parent_route = ProviderRoute(provider="openai", model="gpt-5.6-sol", harness="codex", execution_profile="workspace-agent", tools=["shell"], workspace=str(workspace), sandbox="read-only")
    parent_draft = lattice_env["store"].create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], actor_binding=DirectActorBinding(), prompt="start one native session", route=parent_route))
    parent = lattice_env["lattice"].seal(parent_draft["draft_id"])
    session_id = "33333333-3333-4333-8333-333333333333"
    monkeypatch.setattr("lattice.supervisor.invoke_subscription", lambda *args, **kwargs: ({"response": "session started", "artifacts": []}, fake_journal(tmp_path, native_session_id=session_id)))
    supervisor = ExecutionSupervisor(lattice_env["store"])
    await supervisor.start()
    await supervisor.execute(parent["invocation_id"], ExecuteRequest(idempotency_key="native-lease-parent"))
    await asyncio.gather(*list(supervisor._tasks.values()))

    route = ProviderRoute(
        provider="openai",
        model="gpt-5.6-sol",
        harness="codex",
        execution_profile="native-continuation",
        session_policy="visible_native_continuation",
        continuation_mode="resume",
        native_parent_invocation_id=parent["invocation_id"],
        tools=["shell"],
        workspace=str(workspace),
        sandbox="read-only",
    )
    children = []
    for prompt in ("first continuation", "second continuation"):
        draft = lattice_env["store"].create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], actor_binding=DirectActorBinding(), prompt=prompt, route=route, parent_invocation_ids=[parent["invocation_id"]]))
        children.append(lattice_env["lattice"].seal(draft["draft_id"]))

    first_attempt = lattice_env["store"].create_attempt(children[0]["invocation_id"], "native-session-first", "old-boot", 9_999_999_999)
    with pytest.raises(Exception, match="provider-native session is already executing"):
        lattice_env["store"].create_attempt(children[1]["invocation_id"], "native-session-second", "new-boot", 9_999_999_999)

    assert lattice_env["store"].reconcile_orphans("new-boot") == 1
    assert lattice_env["store"].get_attempt(first_attempt["attempt_id"])["state"] == AttemptState.orphaned.value
    second_attempt = lattice_env["store"].create_attempt(children[1]["invocation_id"], "native-session-second", "new-boot", 9_999_999_999)
    assert second_attempt["state"] == AttemptState.leased.value
    with lattice_env["store"]._connect() as conn:
        leases = [dict(row) for row in conn.execute("SELECT state,invocation_id FROM native_session_leases WHERE provider='openai' AND native_session_id=? ORDER BY acquired_at", (session_id,))]
    assert [item["state"] for item in leases] == ["released", "active"]


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
