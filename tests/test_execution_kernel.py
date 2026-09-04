"""Truth-typed oracles for Lattice's provider-neutral execution kernel."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from lattice.execution import adapters
from lattice.execution import (
    DEFAULT_ADAPTERS,
    ProcessCancelled,
    ProcessOutputLimit,
    SubscriptionInvocationFailed,
    SubscriptionRouteUnavailable,
    bind_request,
    build_command,
    classify_provider_failure,
    continuation_prompt,
    create_run_journal,
    finish_run_journal,
    invoke_subscription,
    load_checkpoint,
    record_native_session,
    run_bounded,
    parse_structured_response,
    subscription_environment,
    installed_adapter_status,
    validate_execution_policy,
)
from lattice.__about__ import __version__ as LATTICE_VERSION


def _journal(tmp_path: Path, driver: str = "claude", **kwargs):
    return create_run_journal(
        driver,
        subject=kwargs.pop("subject", "alpha"),
        model=kwargs.pop("model", "opus"),
        effort=kwargs.pop("effort", "high"),
        timeout=kwargs.pop("timeout", None),
        runs_dir=tmp_path / "runs",
        **kwargs,
    )


@pytest.mark.contract
def test_contract_binds_prompt_and_request_bytes(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    bind_request(journal, prompt="original task", response_format="markdown", resume_strategy="claude")
    checkpoint = load_checkpoint(journal.root)

    assert checkpoint["state"] == "running"
    assert checkpoint["generated_object"]["role"] == "canonical"
    assert checkpoint["generated_object"]["producer"] == {"name": "lattice", "version": LATTICE_VERSION}
    assert (journal.root / checkpoint["request"]).name == "request.json"


@pytest.mark.regression
def test_negative_control_changed_prompt_breaks_binding(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    bind_request(journal, prompt="original task", response_format="markdown", resume_strategy="claude")
    journal.prompt.write_text("mutated task", encoding="utf-8")

    with pytest.raises(ValueError, match="prompt binding failed"):
        load_checkpoint(journal.root)


@pytest.mark.oracle
def test_oracle_binds_separate_system_instruction_bytes(tmp_path: Path) -> None:
    journal = _journal(tmp_path, subject="system-channel")
    bind_request(
        journal,
        prompt="user message",
        system_prompt="trusted harness protocol",
        response_format="json",
        resume_strategy="claude",
    )
    request = json.loads(journal.request.read_text(encoding="utf-8"))

    assert journal.system_prompt.read_text(encoding="utf-8") == "trusted harness protocol"
    assert request["system_prompt"] == "system-prompt.txt"
    assert len(request["system_prompt_sha256"]) == 64
    assert len(load_checkpoint(journal.root)["generated_object"]["sources"]) == 2

    journal.system_prompt.write_text("forged protocol", encoding="utf-8")
    with pytest.raises(ValueError, match="system prompt binding failed"):
        load_checkpoint(journal.root)


@pytest.mark.property
def test_property_stream_events_advance_exact_counts_and_lineage(tmp_path: Path) -> None:
    journal = _journal(tmp_path, timeout=10)
    first = bind_request(journal, prompt="task", response_format="markdown", resume_strategy="claude")
    session_id = "11111111-1111-4111-8111-111111111111"
    script = (
        "import sys; "
        "sys.stdout.write('first\\n'); sys.stdout.flush(); "
        f"sys.stderr.write('session id: {session_id}\\n'); sys.stderr.flush()"
    )

    result = run_bounded(
        [sys.executable, "-c", script],
        timeout=10,
        stdout_journal=journal.stdout,
        stderr_journal=journal.stderr,
    )
    checkpoint = load_checkpoint(journal.root)

    assert result.returncode == 0
    assert checkpoint["observed"]["stdout_bytes"] == len(b"first\n")
    assert checkpoint["observed"]["stderr_bytes"] == len(f"session id: {session_id}\n".encode())
    assert checkpoint["native_session"]["id"] == session_id
    assert checkpoint["generation"] == 2
    assert checkpoint["observed"]["event_count"] == 2
    assert checkpoint["generated_object"]["artifact_id"] != first["generated_object"]["artifact_id"]


@pytest.mark.smoke
def test_smoke_completed_run_marks_succeeded(tmp_path: Path) -> None:
    journal = _journal(tmp_path, driver="grok", model="grok", timeout=10)
    bind_request(journal, prompt="task", response_format="markdown", resume_strategy="grok")
    record_native_session(journal, "22222222-2222-4222-8222-222222222222")
    run_bounded(
        [sys.executable, "-c", "print('complete report')"],
        timeout=10,
        stdout_journal=journal.stdout,
        stderr_journal=journal.stderr,
    )
    finish_run_journal(journal, "success")

    assert load_checkpoint(journal.root)["state"] == "succeeded"


@pytest.mark.regression
def test_adversarial_timeout_keeps_partial_work_for_portable_resume(tmp_path: Path) -> None:
    journal = _journal(tmp_path, driver="grok", model="grok", timeout=3)
    bind_request(journal, prompt="research alpha", response_format="markdown", resume_strategy="grok")
    seen = threading.Event()

    def on_chunk(_channel: str, chunk: bytes) -> None:
        if b"visited source A" in chunk:
            seen.set()

    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded(
            [sys.executable, "-c", "import time; print('visited source A', flush=True); time.sleep(30)"],
            timeout=3,
            stdout_journal=journal.stdout,
            stderr_journal=journal.stderr,
            on_chunk=on_chunk,
        )
    assert seen.wait(0.1)
    finish_run_journal(journal, "timeout")
    prompt = continuation_prompt(journal.root, native=False)
    continuation = json.loads(journal.continuation.read_text(encoding="utf-8"))

    assert load_checkpoint(journal.root)["state"] == "resumable"
    assert "visited source A" in prompt
    assert "visited source A" in continuation["observable_work"]


@pytest.mark.contract
def test_contract_programmatic_cancel_preserves_streamed_output(tmp_path: Path) -> None:
    cancel = threading.Event()
    chunks: list[tuple[str, bytes]] = []

    def request_cancel(channel: str, chunk: bytes) -> None:
        chunks.append((channel, chunk))
        if b"durable-before-cancel" in chunk:
            cancel.set()

    journal = _journal(tmp_path, driver="codex", model="sol", timeout=10)
    bind_request(journal, prompt="task", response_format="markdown", resume_strategy="codex")
    script = "import time; print('durable-before-cancel', flush=True); time.sleep(5)"

    with pytest.raises(ProcessCancelled) as raised:
        run_bounded(
            [sys.executable, "-c", script],
            timeout=10,
            stdout_journal=journal.stdout,
            stderr_journal=journal.stderr,
            cancel_event=cancel,
            on_chunk=request_cancel,
        )

    assert "durable-before-cancel" in raised.value.stdout
    assert b"durable-before-cancel" in journal.stdout.read_bytes()
    assert chunks


@pytest.mark.regression
def test_negative_control_runaway_provider_output_is_bounded_but_journaled(tmp_path: Path) -> None:
    callbacks: list[bytes] = []
    journal = _journal(tmp_path, driver="codex", model="sol", timeout=10)
    bind_request(journal, prompt="task", response_format="json", resume_strategy="codex")

    with pytest.raises(ProcessOutputLimit) as raised:
        run_bounded(
            [sys.executable, "-c", "import os; os.write(1, b'x' * 1048576)"],
            timeout=10,
            stdout_journal=journal.stdout,
            stderr_journal=journal.stderr,
            on_chunk=lambda _channel, chunk: callbacks.append(chunk),
            max_output_bytes=4096,
        )

    assert len(raised.value.stdout.encode("utf-8")) <= 4096
    assert journal.stdout.stat().st_size == 4096
    assert sum(len(chunk) for chunk in callbacks) == 4096


@pytest.mark.regression
def test_negative_control_terminal_receipt_write_failure_is_never_reported_as_success(tmp_path: Path) -> None:
    journal = _journal(tmp_path, driver="codex", model="sol", timeout=10)
    bind_request(journal, prompt="task", response_format="json", resume_strategy="codex")
    with patch("lattice.execution.journal.write_new_json", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            finish_run_journal(journal, "success")
    assert not journal.terminal.exists()


@pytest.mark.contract
def test_contract_adapter_definitions_use_subscription_auth_only() -> None:
    assert {adapter.provider_code for adapter in DEFAULT_ADAPTERS} == {"anthropic", "openai", "google", "moonshot", "xai"}
    assert all(adapter.definition()["auth_surface"] == "consumer_subscription_cli" for adapter in DEFAULT_ADAPTERS)
    anthropic = next(adapter for adapter in DEFAULT_ADAPTERS if adapter.provider_code == "anthropic")
    assert "claude-fable-5" in anthropic.supported_models


@pytest.mark.regression
def test_regression_fast_route_discovery_does_not_probe_cli_versions() -> None:
    with patch("lattice.execution.adapters.shutil.which", return_value="/fixture/cli"), patch(
        "lattice.execution.adapters._cli_version", side_effect=AssertionError("slow version probe ran")
    ):
        statuses = installed_adapter_status(probe_versions=False)

    assert all(item["installed"] for item in statuses)
    assert all(item["version"] is None for item in statuses)
    assert all(item["operational"] is None for item in statuses)
    assert next(item for item in statuses if item["provider_code"] == "google")["qualified"] is True
    assert next(item for item in statuses if item["provider_code"] == "moonshot")["qualified"] is False


@pytest.mark.regression
def test_negative_control_operational_probe_does_not_call_a_hung_cli_qualified() -> None:
    codex = next(item for item in DEFAULT_ADAPTERS if item.provider_code == "openai")
    with patch("lattice.execution.adapters.shutil.which", return_value="/fixture/codex"), patch(
        "lattice.execution.adapters._cli_version", return_value="unreported: timeout"
    ):
        status = installed_adapter_status((codex,), probe_versions=True)[0]

    assert status["qualified"] is True
    assert status["operational"] is False


@pytest.mark.contract
def test_contract_operational_probes_run_independently() -> None:
    selected = tuple(item for item in DEFAULT_ADAPTERS if item.provider_code in {"anthropic", "openai"})
    with patch("lattice.execution.adapters.shutil.which", side_effect=lambda name: f"/fixture/{name}"), patch(
        "lattice.execution.adapters._cli_version", side_effect=lambda binary: f"version:{Path(binary).name}"
    ):
        statuses = installed_adapter_status(selected, probe_versions=True)

    assert [item["version"] for item in statuses] == ["version:claude", "version:codex"]
    assert all(item["operational"] for item in statuses)


@pytest.mark.contract
def test_contract_unenforced_execution_claims_are_rejected_before_dispatch() -> None:
    codex = next(item for item in DEFAULT_ADAPTERS if item.provider_code == "openai")
    baseline = validate_execution_policy(codex)
    assert baseline["tools"] == []
    for mutation in (
        {"session_policy": "visible_native_continuation"},
        {"tools": ["shell"]},
        {"permissions": ["workspace-write"]},
        {"workspace": "/tmp/project"},
        {"network_policy": "provider_managed"},
    ):
        with pytest.raises(SubscriptionRouteUnavailable):
            validate_execution_policy(codex, **mutation)


@pytest.mark.contract
def test_contract_codex_workspace_profile_binds_sandbox_workspace_and_session_persistence(tmp_path: Path) -> None:
    codex = next(item for item in DEFAULT_ADAPTERS if item.provider_code == "openai")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    schema_path = isolated / "schema.json"
    schema_path.write_text('{"type":"object"}', encoding="utf-8")
    policy = validate_execution_policy(
        codex,
        execution_profile="workspace-agent",
        tools=["shell"],
        workspace=workspace,
        sandbox="workspace-write",
        network_policy="disabled",
    )
    command, input_text, _ = build_command(
        codex,
        binary="/codex",
        isolated=isolated,
        schema_path=schema_path,
        output_path=isolated / "out.json",
        effort="high",
        prompt="do work",
        effective_policy=policy,
    )

    assert policy["execution_profile"] == "workspace-agent"
    assert "shell-tools-disabled" not in policy["enforcement"]
    assert "sandbox-workspace-write" in policy["enforcement"]
    assert command[command.index("--cd") + 1] == str(workspace.resolve())
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert "--ask-for-approval" not in command
    assert 'approval_policy="never"' in command
    assert "project_doc_max_bytes=0" in command
    assert "hooks={}" in command
    assert "mcp_servers={}" in command
    assert "plugins={}" in command
    assert "apps={}" in command
    assert "memories.enabled=false" not in command
    assert "agents.enabled=false" not in command
    for feature in adapters.CODEX_DISABLED_FEATURES:
        positions = [index for index, value in enumerate(command[:-1]) if value == "--disable" and command[index + 1] == feature]
        assert len(positions) == 1
    assert "--json" in command
    assert "--ephemeral" not in command
    assert "shell_tool" not in command
    assert input_text == "do work"


@pytest.mark.oracle
def test_oracle_codex_control_surface_probe_rejects_removed_isolation_control() -> None:
    features = "\n".join(f"{item} stable false" for item in adapters.CODEX_DISABLED_FEATURES)
    help_text = " ".join(adapters.CODEX_REQUIRED_EXEC_OPTIONS)
    with patch(
        "lattice.execution.adapters.subprocess.run",
        side_effect=[
            SimpleNamespace(returncode=0, stdout=features, stderr=""),
            SimpleNamespace(returncode=0, stdout=help_text, stderr=""),
        ],
    ):
        qualified, detail = adapters._codex_capability_probe("/fixture/codex")
    assert qualified, detail

    planted = features.replace("memories stable false\n", "")
    with patch(
        "lattice.execution.adapters.subprocess.run",
        side_effect=[
            SimpleNamespace(returncode=0, stdout=planted, stderr=""),
            SimpleNamespace(returncode=0, stdout=help_text, stderr=""),
        ],
    ):
        qualified, detail = adapters._codex_capability_probe("/fixture/codex")
    assert qualified is False
    assert "memories" in detail


@pytest.mark.oracle
def test_oracle_claude_control_surface_probe_rejects_removed_isolation_control() -> None:
    help_text = " ".join(
        option.removesuffix("-file") + "[-file]" if option.endswith("-file") else option
        for option in adapters.CLAUDE_REQUIRED_OPTIONS
    )
    with patch(
        "lattice.execution.adapters.subprocess.run",
        return_value=SimpleNamespace(returncode=0, stdout=help_text, stderr=""),
    ):
        qualified, detail = adapters._claude_capability_probe("/fixture/claude")
    assert qualified, detail

    planted = help_text.replace("--safe-mode", "")
    with patch(
        "lattice.execution.adapters.subprocess.run",
        return_value=SimpleNamespace(returncode=0, stdout=planted, stderr=""),
    ):
        qualified, detail = adapters._claude_capability_probe("/fixture/claude")
    assert qualified is False
    assert "--safe-mode" in detail


@pytest.mark.oracle
def test_oracle_workspace_journal_records_effective_not_baseline_isolation(tmp_path: Path) -> None:
    codex = next(item for item in DEFAULT_ADAPTERS if item.provider_code == "openai")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def fake_run(_command, **kwargs):
        kwargs["observed_files"]["structured-output"].write_text('{"response":"done","artifacts":[]}', encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch("lattice.execution.adapters.shutil.which", return_value="/fixture/codex"), patch(
        "lattice.execution.adapters._cli_version", return_value="codex-cli fixture"
    ), patch("lattice.execution.adapters.run_bounded", side_effect=fake_run):
        _payload, journal = invoke_subscription(
            codex,
            prompt="work here",
            schema={"type": "object"},
            runs_dir=tmp_path / "runs",
            subject="workspace-isolation",
            execution_profile="workspace-agent",
            tools=["shell"],
            workspace=workspace,
            sandbox="workspace-write",
        )

    request = json.loads((journal / "request.json").read_text(encoding="utf-8"))
    isolation = request["metadata"]["isolation"]
    assert isolation["working_directory"] == "explicit-workspace"
    assert isolation["working_directory_path"] == str(workspace.resolve())
    assert isolation["guarantees"] == request["metadata"]["effective_policy"]["enforcement"]
    assert "sandbox-workspace-write" in isolation["guarantees"]
    assert "temporary-working-directory" not in isolation["guarantees"]


@pytest.mark.contract
def test_contract_claude_workspace_profile_binds_tools_and_permissions(tmp_path: Path) -> None:
    claude = next(item for item in DEFAULT_ADAPTERS if item.provider_code == "anthropic")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    schema_path = isolated / "schema.json"
    schema_path.write_text('{"type":"object"}', encoding="utf-8")
    policy = validate_execution_policy(
        claude,
        execution_profile="workspace-agent",
        tools=["Read", "Edit"],
        permissions=["Read", "Edit"],
        workspace=workspace,
        sandbox="provider-permissions",
    )
    command, _, _ = build_command(
        claude,
        binary="/claude",
        isolated=isolated,
        schema_path=schema_path,
        output_path=isolated / "out.json",
        effort="high",
        prompt="do work",
        effective_policy=policy,
    )

    assert command[command.index("--tools") + 1] == "Read,Edit"
    assert command[command.index("--allowedTools") + 1] == "Read,Edit"
    assert "--no-session-persistence" not in command


@pytest.mark.contract
def test_contract_claude_system_instruction_uses_a_file_not_process_arguments(tmp_path: Path) -> None:
    claude = next(item for item in DEFAULT_ADAPTERS if item.provider_code == "anthropic")
    schema_path = tmp_path / "schema.json"
    schema_path.write_text('{"type":"object"}', encoding="utf-8")
    command, input_text, _ = build_command(
        claude,
        binary="/claude",
        isolated=tmp_path,
        schema_path=schema_path,
        output_path=tmp_path / "out.json",
        effort="high",
        prompt="routed conversation",
        system_prompt="trusted harness protocol",
    )

    system_prompt_path = Path(command[command.index("--system-prompt-file") + 1])
    assert system_prompt_path.read_text(encoding="utf-8") == "trusted harness protocol"
    assert input_text == "routed conversation"
    assert "trusted harness protocol" not in command


@pytest.mark.regression
def test_negative_control_unqualified_system_channel_is_rejected(tmp_path: Path) -> None:
    codex = next(item for item in DEFAULT_ADAPTERS if item.provider_code == "openai")
    schema_path = tmp_path / "schema.json"
    schema_path.write_text('{"type":"object"}', encoding="utf-8")
    with pytest.raises(SubscriptionRouteUnavailable, match="no qualified separate"):
        build_command(
            codex,
            binary="/codex",
            isolated=tmp_path,
            schema_path=schema_path,
            output_path=tmp_path / "out.json",
            effort="high",
            prompt="routed conversation",
            system_prompt="smuggled system protocol",
        )


@pytest.mark.regression
def test_negative_control_workspace_authority_cannot_be_smuggled_through_free_form_fields(tmp_path: Path) -> None:
    codex = next(item for item in DEFAULT_ADAPTERS if item.provider_code == "openai")
    claude = next(item for item in DEFAULT_ADAPTERS if item.provider_code == "anthropic")
    with pytest.raises(SubscriptionRouteUnavailable, match="must declare"):
        validate_execution_policy(codex, execution_profile="workspace-agent", tools=[], workspace=tmp_path)
    with pytest.raises(SubscriptionRouteUnavailable, match="only its sandboxed shell"):
        validate_execution_policy(codex, execution_profile="workspace-agent", tools=["undeclared-mcp"], workspace=tmp_path)
    with pytest.raises(SubscriptionRouteUnavailable, match="admitted grammar"):
        validate_execution_policy(
            claude,
            execution_profile="workspace-agent",
            tools=["Bash"],
            permissions=["Bash(git status),Write"],
            workspace=tmp_path,
            sandbox="provider-permissions",
            network_policy="provider_managed",
        )


@pytest.mark.contract
def test_contract_native_resume_and_fork_require_an_observed_session(tmp_path: Path) -> None:
    codex = next(item for item in DEFAULT_ADAPTERS if item.provider_code == "openai")
    session_id = "11111111-1111-4111-8111-111111111111"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    schema_path = isolated / "schema.json"
    schema_path.write_text('{"type":"object"}', encoding="utf-8")
    with pytest.raises(SubscriptionRouteUnavailable, match="observed provider session"):
        validate_execution_policy(
            codex,
            execution_profile="native-continuation",
            session_policy="visible_native_continuation",
            continuation_mode="resume",
            workspace=workspace,
        )
    for mode in ("resume", "fork"):
        policy = validate_execution_policy(
            codex,
            execution_profile="native-continuation",
            session_policy="visible_native_continuation",
            continuation_mode=mode,
            native_session_id=session_id,
            tools=["shell"],
            workspace=workspace,
            sandbox="read-only",
        )
        command, _, _ = build_command(
            codex,
            binary="/codex",
            isolated=isolated,
            schema_path=schema_path,
            output_path=isolated / "out.json",
            effort="high",
            prompt="continue",
            effective_policy=policy,
        )
        exec_index = command.index("exec")
        assert command[exec_index + 1:exec_index + 3] == [mode, session_id]
        assert "--ephemeral" not in command


@pytest.mark.contract
def test_contract_google_and_xai_refuse_generic_command_construction(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    schema_path.write_text('{"type":"object"}', encoding="utf-8")
    google = next(item for item in DEFAULT_ADAPTERS if item.provider_code == "google")
    grok = next(item for item in DEFAULT_ADAPTERS if item.provider_code == "xai")
    with pytest.raises(SubscriptionRouteUnavailable, match="isolated subscription-home"):
        build_command(google, binary="/agy", isolated=tmp_path, schema_path=schema_path, output_path=tmp_path / "out.json", effort="high", prompt="return JSON")
    with pytest.raises(SubscriptionRouteUnavailable, match="isolated subscription-OIDC"):
        build_command(grok, binary="/grok", isolated=tmp_path, schema_path=schema_path, output_path=tmp_path / "out.json", effort="high", prompt="return JSON")


@pytest.mark.property
def test_property_developer_credentials_never_enter_child_environment() -> None:
    filtered = subscription_environment(
        {
            "HOME": "/consumer-login-home",
            "ANTHROPIC_API_KEY": "developer-secret-a",
            "OPENAI_API_KEY": "developer-secret-b",
            "GOOGLE_ACCESS_TOKEN": "developer-secret-c",
            "PATH": "/bin",
        }
    )
    assert filtered["HOME"] == "/consumer-login-home"
    assert filtered["PATH"] == "/bin"
    assert not any("secret" in value for value in filtered.values())


@pytest.mark.regression
def test_negative_control_cloud_backends_and_proxy_routes_never_enter_child_environment() -> None:
    filtered = subscription_environment(
        {
            "HOME": "/consumer-login-home",
            "PATH": "/bin",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_ACCESS_KEY_ID": "developer-account",
            "AWS_SECRET_ACCESS_KEY": "developer-secret",
            "CLAUDE_CODE_USE_VERTEX": "1",
            "GOOGLE_APPLICATION_CREDENTIALS": "/developer-service-account.json",
            "ANTHROPIC_BASE_URL": "https://developer-proxy.example",
            "HTTPS_PROXY": "https://interceptor.example",
        }
    )
    assert filtered == {"HOME": "/consumer-login-home", "PATH": "/bin"}


@pytest.mark.contract
def test_contract_commands_disable_tools_where_supported(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    schema_path.write_text('{"type":"object"}', encoding="utf-8")
    commands = {}
    for adapter in DEFAULT_ADAPTERS:
        if adapter.provider_code in {"moonshot", "google", "xai"}:
            continue
        command, _, _ = build_command(
            adapter,
            binary=f"/{adapter.cli}",
            isolated=tmp_path,
            schema_path=schema_path,
            output_path=tmp_path / "out.json",
            effort="high",
            prompt="return JSON",
        )
        commands[adapter.provider_code] = command

    assert "--safe-mode" in commands["anthropic"]
    assert "--tools" in commands["anthropic"]
    assert "--ignore-user-config" in commands["openai"]
    assert "shell_tool" in commands["openai"]


@pytest.mark.contract
def test_contract_provider_prompts_are_never_process_arguments(tmp_path: Path) -> None:
    prompt = "PRIVATE_PROMPT_MARKER"
    schema_path = tmp_path / "schema.json"
    schema_path.write_text('{"type":"object"}', encoding="utf-8")
    for adapter in DEFAULT_ADAPTERS:
        if adapter.provider_code in {"moonshot", "google", "xai"}:
            continue
        command, input_text, _ = build_command(
            adapter,
            binary=f"/{adapter.cli}",
            isolated=tmp_path,
            schema_path=schema_path,
            output_path=tmp_path / "out.json",
            effort="high",
            prompt=prompt,
        )
        assert prompt not in command
        assert input_text == prompt


@pytest.mark.regression
def test_negative_control_unqualified_kimi_route_cannot_execute(tmp_path: Path) -> None:
    adapter = next(item for item in DEFAULT_ADAPTERS if item.provider_code == "moonshot")
    schema_path = tmp_path / "schema.json"
    schema_path.write_text('{"type":"object"}', encoding="utf-8")
    with pytest.raises(SubscriptionRouteUnavailable):
        build_command(
            adapter,
            binary="/kimi",
            isolated=tmp_path,
            schema_path=schema_path,
            output_path=tmp_path / "out.json",
            effort="high",
            prompt="return JSON",
        )


@pytest.mark.regression
def test_regression_rate_limit_is_operational_not_semantic_failure() -> None:
    assert classify_provider_failure(1, "", "Rate limit exceeded; retry later") == "rate_limit"


@pytest.mark.regression
def test_regression_exhausted_consumer_balance_is_not_a_provider_outage() -> None:
    stderr = 'API error (status 402 Payment Required): Grok Build usage balance exhausted'

    assert classify_provider_failure(1, "", stderr) == "subscription_exhausted"


@pytest.mark.regression
def test_regression_local_policy_failure_is_not_a_provider_refusal() -> None:
    assert classify_provider_failure(1, "", "Invalid policy rule: mcpName is required; Operation not permitted") == "harness_failure"


@pytest.mark.regression
def test_regression_invalid_schema_with_authority_prompt_is_not_authentication() -> None:
    stderr = """Act without inventing authority evidence.
ERROR: {"type":"invalid_request_error","code":"invalid_json_schema","message":"Invalid schema for response_format"}
"""
    assert classify_provider_failure(1, "", stderr) == "harness_failure"


@pytest.mark.contract
def test_contract_explicit_login_failure_is_authentication() -> None:
    assert classify_provider_failure(1, "", "Authentication required; please log in") == "authentication"


@pytest.mark.regression
def test_negative_control_non_json_response_is_rejected(tmp_path: Path) -> None:
    adapter = next(item for item in DEFAULT_ADAPTERS if item.provider_code == "xai")
    with pytest.raises(SubscriptionInvocationFailed) as raised:
        parse_structured_response(adapter, "not json", tmp_path / "missing.json")
    assert raised.value.kind == "malformed_output"


@pytest.mark.regression
def test_negative_control_structured_output_file_cannot_bypass_the_output_limit(tmp_path: Path) -> None:
    adapter = next(item for item in DEFAULT_ADAPTERS if item.provider_code == "openai")
    output = tmp_path / "structured-output.json"
    output.write_text('{"response":"' + ("x" * 100) + '","artifacts":[]}', encoding="utf-8")
    with pytest.raises(SubscriptionInvocationFailed) as raised:
        parse_structured_response(adapter, "", output, max_output_bytes=32)
    assert raised.value.kind == "output_limit"


@pytest.mark.regression
def test_negative_control_stdout_and_output_file_share_one_output_budget(tmp_path: Path) -> None:
    adapter = next(item for item in DEFAULT_ADAPTERS if item.provider_code == "openai")
    output = tmp_path / "structured-output.json"
    output.write_text('{"response":"file","artifacts":[]}', encoding="utf-8")
    with pytest.raises(SubscriptionInvocationFailed) as raised:
        parse_structured_response(adapter, '{"response":"stdout","artifacts":[]}', output, max_output_bytes=60)
    assert raised.value.kind == "output_limit"


@pytest.mark.regression
def test_negative_control_provider_input_is_bounded_before_dispatch(tmp_path: Path) -> None:
    adapter = next(item for item in DEFAULT_ADAPTERS if item.provider_code == "openai")
    with patch("lattice.execution.adapters.MAX_MODEL_INPUT_BYTES", 8):
        with pytest.raises(ValueError, match="model input exceeds"):
            invoke_subscription(adapter, prompt="nine bytes", schema={"type": "object"}, runs_dir=tmp_path, subject="bounded-input")


@pytest.mark.regression
def test_negative_control_structured_output_file_must_be_utf8(tmp_path: Path) -> None:
    adapter = next(item for item in DEFAULT_ADAPTERS if item.provider_code == "openai")
    output = tmp_path / "structured-output.json"
    output.write_bytes(b"\xff\xfe")
    with pytest.raises(SubscriptionInvocationFailed) as raised:
        parse_structured_response(adapter, "", output)
    assert raised.value.kind == "malformed_output"


@pytest.mark.contract
def test_contract_invocation_failure_can_carry_its_permanent_journal() -> None:
    journal = Path("/tmp/model-run-journal")
    failure = SubscriptionInvocationFailed("harness_failure", "process could not start", journal=journal)
    assert failure.journal == journal


@pytest.mark.contract
def test_contract_structured_response_must_satisfy_declared_schema(tmp_path: Path) -> None:
    adapter = next(item for item in DEFAULT_ADAPTERS if item.provider_code == "xai")
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["proposals", "unresolved"],
        "properties": {
            "proposals": {"type": "array", "items": {"type": "object"}},
            "unresolved": {"type": "array", "items": {"type": "string"}},
        },
    }
    with pytest.raises(SubscriptionInvocationFailed) as raised:
        parse_structured_response(adapter, '{"proposals":[],"unexpected":true}', tmp_path / "missing.json", schema)
    assert raised.value.kind == "malformed_output"


@pytest.mark.oracle
def test_oracle_draft_2020_schema_keywords_are_enforced(tmp_path: Path) -> None:
    adapter = next(item for item in DEFAULT_ADAPTERS if item.provider_code == "xai")
    schema = {"type": "object", "required": ["items"], "properties": {"items": {"type": "array", "maxItems": 1}}}
    with pytest.raises(SubscriptionInvocationFailed) as raised:
        parse_structured_response(adapter, '{"items":[1,2]}', tmp_path / "missing.json", schema)
    assert raised.value.kind == "malformed_output"


@pytest.mark.regression
def test_regression_json_looking_response_text_cannot_displace_the_valid_envelope(tmp_path: Path) -> None:
    adapter = next(item for item in DEFAULT_ADAPTERS if item.provider_code == "xai")
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["response", "artifacts"],
        "properties": {"response": {"type": "string"}, "artifacts": {"type": "array", "maxItems": 0}},
    }
    parsed = parse_structured_response(adapter, '{"response":"{\\"fabricated\\":true}","artifacts":[]}', tmp_path / "missing.json", schema)
    assert parsed == {"response": '{"fabricated":true}', "artifacts": []}


@pytest.mark.regression
def test_negative_control_duplicate_keys_and_nonfinite_numbers_are_rejected(tmp_path: Path) -> None:
    adapter = next(item for item in DEFAULT_ADAPTERS if item.provider_code == "xai")
    schema = {"type": "object"}
    output = tmp_path / "missing.json"
    for raw in ('{"value":1,"value":2}', '{"value":NaN}'):
        with pytest.raises(SubscriptionInvocationFailed) as raised:
            parse_structured_response(adapter, raw, output, schema)
        assert raised.value.kind == "malformed_output"


@pytest.mark.regression
def test_negative_control_response_schema_cannot_retrieve_external_authority(tmp_path: Path) -> None:
    adapter = next(item for item in DEFAULT_ADAPTERS if item.provider_code == "xai")
    with pytest.raises(ValueError, match="external references"):
        parse_structured_response(adapter, "{}", tmp_path / "missing.json", {"$ref": "https://example.com/schema.json"})
