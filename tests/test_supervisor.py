"""Concurrency and terminalization assurance for model execution."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from loom.contracts import DraftCreate, ExecuteRequest
from loom.supervisor import ExecutionSupervisor


def fake_journal(tmp_path: Path) -> Path:
    journal = tmp_path / "journal"
    journal.mkdir(exist_ok=True)
    (journal / "request.json").write_text('{"driver":"codex","model":"test","metadata":{"cli_version":"test-cli 1","binary":"/test/codex","isolation":{"working_directory":"fresh-temporary"}}}', encoding="utf-8")
    (journal / "checkpoint.json").write_text('{"native_session":{"strategy":"codex","id":null,"state_dir":null},"observed":{"stdout_bytes":0}}', encoding="utf-8")
    (journal / "terminal.json").write_text('{"status":"success"}', encoding="utf-8")
    return journal


@pytest.mark.asyncio
@pytest.mark.concurrency
async def test_concurrency_idempotent_execution_produces_one_attempt(monkeypatch, loom_env, tmp_path) -> None:
    draft = loom_env["store"].create_draft(DraftCreate(project_id=loom_env["project"]["project_id"], role_session_id=loom_env["session"]["role_session_id"], prompt="P1", route=loom_env["route"]))
    invocation = loom_env["loom"].seal(draft["draft_id"])

    def fake_invoke(adapter, **kwargs):
        kwargs["on_chunk"]("stdout", b"working")
        return {"response": "finished", "artifacts": []}, fake_journal(tmp_path)

    monkeypatch.setattr("loom.supervisor.invoke_subscription", fake_invoke)
    supervisor = ExecutionSupervisor(loom_env["store"])
    await supervisor.start()
    first = await supervisor.execute(invocation["invocation_id"], ExecuteRequest(idempotency_key="same-request-0001"))
    second = await supervisor.execute(invocation["invocation_id"], ExecuteRequest(idempotency_key="same-request-0001"))
    await asyncio.gather(*list(supervisor._tasks.values()))
    result = loom_env["store"].get_invocation(invocation["invocation_id"])

    assert first["attempt_id"] == second["attempt_id"]
    assert len(result["attempts"]) == 1
    assert result["state"] == "succeeded"
    assert result["output_block"]["content"] == "finished"


@pytest.mark.asyncio
@pytest.mark.contract
async def test_contract_unexpected_execution_error_terminalizes_attempt(monkeypatch, loom_env) -> None:
    draft = loom_env["store"].create_draft(DraftCreate(project_id=loom_env["project"]["project_id"], role_session_id=loom_env["session"]["role_session_id"], prompt="P1", route=loom_env["route"]))
    invocation = loom_env["loom"].seal(draft["draft_id"])

    def explode(*args, **kwargs):
        raise OSError("planted execution failure")

    monkeypatch.setattr("loom.supervisor.invoke_subscription", explode)
    supervisor = ExecutionSupervisor(loom_env["store"])
    attempt = await supervisor.execute(invocation["invocation_id"], ExecuteRequest(idempotency_key="failure-request-001"))
    await asyncio.gather(*list(supervisor._tasks.values()))
    terminal = loom_env["store"].get_attempt(attempt["attempt_id"])

    assert terminal["state"] == "failed"
    assert terminal["failure_kind"] == "harness_failure"
    assert "planted execution failure" in terminal["error_text"]


@pytest.mark.oracle
@pytest.mark.asyncio
async def test_oracle_provider_artifacts_are_content_addressed_and_linked(monkeypatch, loom_env, tmp_path) -> None:
    draft = loom_env["store"].create_draft(DraftCreate(project_id=loom_env["project"]["project_id"], role_session_id=loom_env["session"]["role_session_id"], prompt="make artifact", route=loom_env["route"]))
    invocation = loom_env["loom"].seal(draft["draft_id"])

    def fake_invoke(*args, **kwargs):
        return {"response": "made it", "artifacts": [{"name": "answer.txt", "media_type": "text/plain", "content": "exact artifact bytes"}]}, fake_journal(tmp_path)

    monkeypatch.setattr("loom.supervisor.invoke_subscription", fake_invoke)
    supervisor = ExecutionSupervisor(loom_env["store"])
    await supervisor.start()
    await supervisor.execute(invocation["invocation_id"], ExecuteRequest(idempotency_key="artifact-request-001"))
    await asyncio.gather(*list(supervisor._tasks.values()))
    result = loom_env["store"].get_invocation(invocation["invocation_id"])

    assert len(result["artifacts"]) == 1
    artifact = result["artifacts"][0]
    assert loom_env["store"].blobs.get(artifact["content_sha256"]) == b"exact artifact bytes"
    assert any(edge["target_id"] == artifact["artifact_id"] and edge["relation"] == "produced" for edge in result["edges"])
    assert result["attempts"][0]["runtime_receipt"]["cli_version"] == "test-cli 1"
    assert result["attempts"][0]["runtime_receipt"]["native_session"]["strategy"] == "codex"
