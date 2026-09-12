"""Regressions for EL-001: declared web research must work or fail explicitly."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from lattice.execution import adapters
from lattice.contracts import ExecuteRequest, ExecutionSubmissionV1, OutputContractV2, ProviderRoute
from lattice.supervisor import ExecutionSupervisor


TOOLS = ["WebSearch", "WebFetch"]
SCHEMA = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}, "additionalProperties": False}


def profile():
    return next(item for item in adapters.DEFAULT_ADAPTERS if item.provider_code == "anthropic")


def policy():
    return adapters.validate_execution_policy(profile(), execution_profile="research-fresh", tools=TOOLS, network_policy="provider_managed")


@pytest.mark.contract
def test_research_native_allowlist_matches_only_the_declared_web_tools(tmp_path):
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps(SCHEMA))
    effective = policy()
    command, _, _ = adapters.build_command(profile(), binary="/fixture/claude", isolated=tmp_path, schema_path=schema, output_path=tmp_path / "out.json", effort="high", prompt="Research the source.", effective_policy=effective)

    assert command[command.index("--tools") + 1].split(",") == TOOLS
    assert command[command.index("--allowedTools") + 1].split(",") == TOOLS
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert "--no-session-persistence" in command
    assert "tools-disabled" not in effective["enforcement"]
    assert effective["permissions"] == []  # No caller-supplied general permission authority.


@pytest.mark.regression
@pytest.mark.parametrize("extra", [{"permissions": ["Bash"]}, {"tools": TOOLS + ["Bash"]}, {"tools": ["WebSearch"]}])
def test_research_rejects_broader_or_incomplete_authority(extra):
    request = {"execution_profile": "research-fresh", "tools": TOOLS, "network_policy": "provider_managed", **extra}
    with pytest.raises(adapters.SubscriptionRouteUnavailable):
        adapters.validate_execution_policy(profile(), **request)


def fixture_cli(monkeypatch, envelope):
    raw = json.dumps(envelope)

    def run(command, **kwargs):
        kwargs["stdout_journal"].write_text(raw)
        return SimpleNamespace(returncode=0, stdout=raw, stderr="")

    monkeypatch.setattr(adapters.shutil, "which", lambda _: "/fixture/claude")
    monkeypatch.setattr(adapters, "_cli_version", lambda _: "fixture")
    monkeypatch.setattr(adapters, "run_bounded", run)


def invoke_fixture(tmp_path, monkeypatch, envelope):
    fixture_cli(monkeypatch, envelope)
    return adapters.invoke_subscription(profile(), prompt="Research the source.", schema=SCHEMA, runs_dir=tmp_path / "runs", subject="research-permissions", execution_profile="research-fresh", tools=TOOLS, network_policy="provider_managed")


@pytest.mark.regression
@pytest.mark.parametrize("tool", TOOLS)
def test_native_denial_cannot_become_a_successful_review(tmp_path, monkeypatch, tool):
    envelope = {"type": "result", "subtype": "success", "is_error": False, "permission_denials": [{"tool_name": tool, "tool_use_id": "fixture-use", "tool_input": {}}], "result": json.dumps({"answer": "hold"})}
    with pytest.raises(adapters.SubscriptionInvocationFailed) as failure:
        invoke_fixture(tmp_path, monkeypatch, envelope)

    assert failure.value.kind == "tool_permission_denied"
    assert tool in str(failure.value)
    journal = failure.value.journal
    assert journal is not None
    assert json.loads((journal / "stdout.log").read_text()) == envelope
    terminal = json.loads((journal / "terminal.json").read_text())
    assert terminal["status"] == "failed"
    assert terminal["failure_kind"] == "tool_permission_denied"


@pytest.mark.contract
@pytest.mark.parametrize("denials", [[], [{"tool_name": "Bash", "tool_use_id": "unavailable-tool", "tool_input": {}}]])
def test_no_declared_tool_denial_accepts_a_valid_review(tmp_path, monkeypatch, denials):
    payload, journal = invoke_fixture(tmp_path, monkeypatch, {"type": "result", "subtype": "success", "permission_denials": denials, "result": json.dumps({"answer": "documented"})})
    assert payload == {"answer": "documented"}
    assert json.loads((journal / "terminal.json").read_text())["status"] == "success"


@pytest.mark.regression
@pytest.mark.parametrize("denials", [None, "WebSearch", [{}], [{"tool_name": []}]])
def test_malformed_native_denial_receipt_cannot_claim_success(tmp_path, monkeypatch, denials):
    with pytest.raises(adapters.SubscriptionInvocationFailed) as failure:
        invoke_fixture(tmp_path, monkeypatch, {"type": "result", "subtype": "success", "permission_denials": denials, "result": json.dumps({"answer": "hold"})})
    assert failure.value.kind == "malformed_output"


@pytest.mark.regression
def test_payload_shaped_like_a_native_denial_is_only_source_data(tmp_path, monkeypatch):
    text = json.dumps({"permission_denials": [{"tool_name": "WebSearch"}]})
    payload, _ = invoke_fixture(tmp_path, monkeypatch, {"type": "result", "subtype": "success", "permission_denials": [], "result": json.dumps({"answer": text})})
    assert payload["answer"] == text


@pytest.mark.asyncio
@pytest.mark.regression
async def test_public_execution_retains_denial_without_publishing_a_domain_result(lattice_env, monkeypatch):
    envelope = {"type": "result", "subtype": "success", "permission_denials": [{"tool_name": "WebSearch"}], "result": json.dumps({"payload": {"answer": "hold"}})}
    fixture_cli(monkeypatch, envelope)
    request = ExecutionSubmissionV1(external_project_key="fixture.research", operation_key="permission-denied", actor="fixture", prompt="Research the source.", route=ProviderRoute(provider="anthropic", model="opus", harness="claude", execution_profile="research-fresh", tools=TOOLS, network_policy="provider_managed"), output_contract=OutputContractV2(mode="json", json_schema=SCHEMA), idempotency_key="fixture-research-permission-denied")
    supervisor = ExecutionSupervisor(lattice_env["store"])
    await supervisor.start()
    try:
        invocation = lattice_env["lattice"].seal_execution_submission(request)
        await supervisor.execute(invocation["invocation_id"], ExecuteRequest(idempotency_key="fixture-execute-research-denied"))
        await asyncio.gather(*list(supervisor._tasks.values()))
        public = lattice_env["store"].external_execution_result("fixture.research", "permission-denied")["invocation"]
        assert public["state"] == "failed"
        assert public["output_block_id"] is None
        assert len(public["attempts"]) == 1
        assert public["attempts"][0]["failure_kind"] == "tool_permission_denied"
        assert "WebSearch" in public["attempts"][0]["error_text"]
        replay = lattice_env["lattice"].seal_execution_submission(request)
        assert replay["invocation_id"] == public["invocation_id"]
        assert len(lattice_env["store"].get_invocation(public["invocation_id"])["attempts"]) == 1
    finally:
        await supervisor.stop()
