"""Production runner plumbing with real SDK/pipes and planted operator authority.

The test-only admission seam does not enable production policies or establish
native tool enforcement. Native CLI behavior remains a separate qualification.
"""

import asyncio
import json
from pathlib import Path
import sys
import threading

import pytest

from lattice import native_approvals as ledger
from lattice.execution import adapters
from lattice.execution.native_approval import NativeApprovalCallback
from test_claude_research import SCHEMA, profile
from test_native_approvals import approval_env, decide, start
from test_native_approval_sdk import pending


TERMINAL = {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 1, "duration_api_ms": 1, "num_turns": 1, "session_id": "planted-session", "structured_output": {"answer": "native terminal"}}


def raw(*messages):
    return "".join(json.dumps(message) + "\n" for message in messages)


@pytest.mark.contract
def test_terminal_parser_accepts_only_direct_contracted_payload():
    source = {"type": "user", "message": {"content": json.dumps({"answer": "source, not answer"})}}
    assert adapters.parse_claude_sdk_response(raw(source, TERMINAL), SCHEMA) == {"answer": "native terminal"}
    result_text = {key: value for key, value in TERMINAL.items() if key != "structured_output"}
    result_text["result"] = json.dumps({"answer": "text channel"})
    assert adapters.parse_claude_sdk_response(raw(result_text), SCHEMA) == {"answer": "text channel"}


@pytest.mark.regression
@pytest.mark.parametrize("form", ["unicode-lines", "idle-tail"])
def test_valid_native_framing_survives_unicode_and_post_turn_status(form):
    if form == "unicode-lines":
        answer = "first\u2028second\u2029last"
        stream = json.dumps({**TERMINAL, "structured_output": {"answer": answer}}, ensure_ascii=False) + "\n"
    else:
        answer = "native terminal"
        stream = raw(TERMINAL, {"type": "system", "subtype": "session_state_changed", "session_state": "idle"})
    assert adapters.parse_claude_sdk_response(stream, SCHEMA) == {"answer": answer}


@pytest.mark.regression
@pytest.mark.parametrize("form", ["none", "duplicate", "tail", "array", "nested", "no-payload", "interrupted", "injected", "duplicate-key", "nonfinite"])
def test_invalid_terminal_cannot_publish_domain_output(form):
    source = {"type": "user", "message": {"content": json.dumps({"answer": "source"})}}
    stream = {
        "none": raw(source), "duplicate": raw(TERMINAL, TERMINAL),
        "tail": raw(TERMINAL, source), "array": raw([], TERMINAL),
        "nested": raw({**TERMINAL, "structured_output": {"result": {"answer": "nested"}}}),
        "no-payload": raw(source, {key: value for key, value in TERMINAL.items() if key != "structured_output"}),
        "interrupted": raw({**TERMINAL, "terminal_reason": "aborted_tools"}),
        "injected": raw({**TERMINAL, "origin": {"kind": "task-notification"}}),
        "duplicate-key": raw(TERMINAL).replace('"is_error": false', '"is_error": true, "is_error": false'),
        "nonfinite": raw(TERMINAL).replace('"duration_ms": 1', '"duration_ms": NaN'),
    }[form]
    with pytest.raises(adapters.SubscriptionInvocationFailed) as failure:
        adapters.parse_claude_sdk_response(stream, SCHEMA)
    assert failure.value.kind == "malformed_output"


@pytest.mark.contract
@pytest.mark.parametrize("message,kind", [("Authentication required", "authentication"), ("Request refused under safety policy", "policy_refusal"), ("Connection refused", "outage")])
def test_native_error_cannot_be_hidden_by_valid_domain_json(message, kind):
    with pytest.raises(adapters.SubscriptionInvocationFailed) as failure:
        adapters.parse_claude_sdk_response(raw({**TERMINAL, "is_error": True, "errors": [message]}), SCHEMA)
    assert failure.value.kind == kind


@pytest.mark.contract
def test_command_uses_native_prompt_channel_without_widening_tools(tmp_path):
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps(SCHEMA))
    policy = adapters.validate_execution_policy(profile(), execution_profile="workspace-agent", tools=["Bash"], workspace=tmp_path, sandbox="provider-permissions", approval_policy="on-request", network_policy="provider_managed")
    command, prompt, observed = adapters.build_command(profile(), binary="/planted/claude", isolated=tmp_path, schema_path=schema, output_path=tmp_path / "out", effort="high", prompt="exact α", effective_policy=policy)
    for option, value in [("--tools", "Bash"), ("--allowedTools", ""), ("--permission-mode", "default"), ("--permission-prompt-tool", "stdio"), ("--input-format", "stream-json"), ("--output-format", "stream-json"), ("--setting-sources", "")]:
        assert command[command.index(option) + 1] == value
    assert command.count("--verbose") == 1
    assert "--safe-mode" in command and "--no-chrome" in command
    assert prompt == "exact α" and observed == {}
    with pytest.raises(adapters.SubscriptionRouteUnavailable, match="Claude workspace"):
        adapters.validate_execution_policy(profile(), approval_policy="on-request")


PEER = '''
import json, sys, time
from pathlib import Path
case, flag, terminal = json.loads(sys.argv[1])
def emit(value):
    print(json.dumps(value), flush=True)
initialize = json.loads(sys.stdin.readline())
emit({"type":"control_response","response":{"subtype":"success","request_id":initialize["request_id"],"response":{"current_permission_mode":"default"}}})
user = json.loads(sys.stdin.readline())
assert user["type"] == "user" and user["message"]["content"] == "exact α"
if case == "timeout":
    time.sleep(30)
elif case == "plain":
    emit(terminal)
else:
    emit({"type":"control_request","request_id":"ask-1","request":{"subtype":"can_use_tool","tool_name":"Read","tool_use_id":"tool_1","input":{"file_path":"/planted/source.txt"}}})
    if case == "pending-result":
        while not Path(flag).exists():
            time.sleep(.01)
        emit(terminal)
    else:
        line = sys.stdin.readline()
        if line:
            response = json.loads(line)
            decision = response["response"]["response"]
            assert "updatedPermissions" not in decision
            if decision["behavior"] == "allow":
                assert decision["updatedInput"] == {"file_path":"/planted/source.txt"}
            terminal["structured_output"] = {"answer":decision["behavior"]}
            emit(terminal)
for line in sys.stdin:
    pass
if case == "nonzero":
    sys.exit(7)
'''


def prepare(env, monkeypatch, tmp_path, case="ordinary"):
    attempt = start(env)
    cancel = threading.Event()
    callback = NativeApprovalCallback(env["store"], attempt, owner_boot_id="test-boot", cancel_event=cancel)
    original_policy = adapters.validate_execution_policy

    def test_admission(adapter, **policy):
        return {**original_policy(adapter, **{**policy, "approval_policy": "never"}), "approval_policy": policy["approval_policy"]}

    original_command = adapters.build_command
    flag = tmp_path / "release-peer"

    def planted_command(adapter, **kwargs):
        original_command(adapter, **kwargs)  # Still compile all declared flags.
        return [sys.executable, "-u", "-c", PEER, json.dumps([case, str(flag), TERMINAL])], kwargs["prompt"], {}

    monkeypatch.setattr(adapters, "validate_execution_policy", test_admission)
    monkeypatch.setattr(adapters, "build_command", planted_command)
    monkeypatch.setattr(adapters.shutil, "which", lambda _: sys.executable)
    monkeypatch.setattr(adapters, "_cli_version", lambda _: "planted-python-peer")
    kwargs = dict(prompt="exact α", schema=SCHEMA, runs_dir=tmp_path / "runs", subject="sdk-boundary", timeout=5, execution_profile="workspace-agent", tools=["Read"], workspace=tmp_path, sandbox="provider-permissions", approval_policy="on-request", native_approval_callback=callback, cancel_event=cancel)
    return attempt, callback, flag, kwargs


@pytest.mark.asyncio
@pytest.mark.contract
@pytest.mark.parametrize("choice", ["allow", "deny"])
async def test_adapter_waits_for_exact_operator_choice_and_journals_native_result(approval_env, monkeypatch, tmp_path, choice):
    attempt, approval, _, kwargs = prepare(approval_env, monkeypatch, tmp_path)
    task = asyncio.create_task(asyncio.to_thread(adapters.invoke_subscription, profile(), **kwargs))
    try:
        item = await pending(approval_env, attempt)
        assert not task.done() and item["decision"] is None
        decide(approval_env, attempt, item, choice)
        payload, journal = await asyncio.wait_for(task, 8)
        assert payload == {"answer": choice}
        assert json.loads((journal / "terminal.json").read_text())["status"] == "success"
        frames = [json.loads(line) for line in (journal / "stdin.accepted.log").read_text().splitlines()]
        assert [frame["type"] for frame in frames] == ["control_request", "user", "control_response"]
        assert frames[-1]["response"]["response"]["behavior"] == choice
        assert ledger.inspect(approval_env["store"], attempt)["requests"][0]["state"] == "released-to-adapter"
    finally:
        if not task.done():
            approval.cancel.set()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.regression
async def test_final_result_cannot_hide_a_withdrawal_failure(approval_env, monkeypatch, tmp_path):
    attempt, approval, flag, kwargs = prepare(approval_env, monkeypatch, tmp_path, "pending-result")
    task = asyncio.create_task(asyncio.to_thread(adapters.invoke_subscription, profile(), **kwargs))
    try:
        await pending(approval_env, attempt)
        def broken_withdraw(*args, **kwargs):
            raise OSError("planted withdrawal failure after native result")
        monkeypatch.setattr(ledger, "withdraw", broken_withdraw)
        flag.touch()
        with pytest.raises(adapters.SubscriptionInvocationFailed) as failure:
            await asyncio.wait_for(task, 8)
        assert failure.value.kind == "harness_failure"
        journal = failure.value.journal
        assert '"type": "result"' in (journal / "stdout.log").read_text()
        assert "planted withdrawal failure" in (journal / "sdk-failure.json").read_text()
        assert json.loads((journal / "terminal.json").read_text())["status"] == "failed"
        assert approval.errors
    finally:
        if not task.done():
            approval.cancel.set()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.regression
@pytest.mark.parametrize("mode", ["cancel", "timeout", "nonzero"])
async def test_process_failure_keeps_its_cause(approval_env, monkeypatch, tmp_path, mode):
    attempt, approval, _, kwargs = prepare(approval_env, monkeypatch, tmp_path, mode)
    if mode == "timeout":
        kwargs["timeout"] = .5
    task = asyncio.create_task(asyncio.to_thread(adapters.invoke_subscription, profile(), **kwargs))
    try:
        if mode != "timeout":
            item = await pending(approval_env, attempt)
            if mode == "cancel":
                approval.cancel.set()
            else:
                decide(approval_env, attempt, item)
        with pytest.raises(adapters.SubscriptionInvocationFailed) as failure:
            await asyncio.wait_for(task, 8)
        expected = {"cancel": "operator_interruption", "timeout": "timeout", "nonzero": "outage"}[mode]
        assert failure.value.kind == expected
        terminal = json.loads((failure.value.journal / "terminal.json").read_text())
        assert terminal["failure_kind"] == expected
    finally:
        if not task.done():
            approval.cancel.set()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.regression
@pytest.mark.parametrize("fault", ["journal", "input-limit"])
async def test_permission_response_write_failure_is_not_operator_cancellation(approval_env, monkeypatch, tmp_path, fault):
    from lattice.execution import sdk_transport
    attempt, approval, _, kwargs = prepare(approval_env, monkeypatch, tmp_path)
    task = asyncio.create_task(asyncio.to_thread(adapters.invoke_subscription, profile(), **kwargs))
    try:
        item = await pending(approval_env, attempt)
        if fault == "journal":
            def broken_write(*args, **kwargs):
                raise OSError("planted permission response journal failure")
            monkeypatch.setattr(sdk_transport, "write_all", broken_write)
        else:
            original_write = sdk_transport.GuardedSDKTransport.write
            async def cap_before_response(wire, data):
                wire.max_input_bytes = wire._accepted_bytes
                return await original_write(wire, data)
            monkeypatch.setattr(sdk_transport.GuardedSDKTransport, "write", cap_before_response)
        decide(approval_env, attempt, item)
        with pytest.raises(adapters.SubscriptionInvocationFailed) as failure:
            await asyncio.wait_for(task, 8)
        assert failure.value.kind == "harness_failure"
        detail = (failure.value.journal / "sdk-failure.json").read_text()
        assert ("planted permission response journal failure" if fault == "journal" else "ProcessInputLimit") in detail
    finally:
        if not task.done():
            approval.cancel.set()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.regression
@pytest.mark.parametrize("process_outcome", ["cancelled", "clean-exit"])
async def test_cancellation_at_response_admission_stays_operator_interruption(approval_env, monkeypatch, tmp_path, process_outcome):
    from lattice.execution import sdk_transport
    from lattice.execution.process import ProcessCancelled
    import subprocess
    attempt, approval, _, kwargs = prepare(approval_env, monkeypatch, tmp_path)
    if process_outcome == "clean-exit":
        bounded = sdk_transport.run_bounded
        def completed_at_cancel_boundary(command, **options):
            try:
                return bounded(command, **options)
            except ProcessCancelled as error:
                # Plant the competing terminal outcome: a process can finish
                # before its next cancellation poll, including with valid JSON.
                # SDK pipes and callback admission remain real; this outcome is
                # controlled, not claimed as an observed native scheduling race.
                return subprocess.CompletedProcess(command, 0, error.stdout + raw(TERMINAL), error.stderr)
        monkeypatch.setattr(sdk_transport, "run_bounded", completed_at_cancel_boundary)
    task = asyncio.create_task(asyncio.to_thread(adapters.invoke_subscription, profile(), **kwargs))
    try:
        item = await pending(approval_env, attempt)
        original = sdk_transport.GuardedSDKTransport.write
        async def cancel_before_response(wire, data):
            wire.cancel.set()
            return await original(wire, data)
        monkeypatch.setattr(sdk_transport.GuardedSDKTransport, "write", cancel_before_response)
        decide(approval_env, attempt, item)
        with pytest.raises(adapters.SubscriptionInvocationFailed) as failure:
            await asyncio.wait_for(task, 8)
        assert failure.value.kind == "operator_interruption"
        frames = [json.loads(line) for line in (failure.value.journal / "stdin.accepted.log").read_text().splitlines()]
        assert [frame["type"] for frame in frames] == ["control_request", "user"]
    finally:
        if not task.done():
            approval.cancel.set()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.contract
@pytest.mark.parametrize("fault", ["missing-callback", "wrong-cancel", "noninteractive"])
def test_callback_ownership_is_required_before_creating_a_run(approval_env, monkeypatch, tmp_path, fault):
    _, _, _, kwargs = prepare(approval_env, monkeypatch, tmp_path)
    if fault == "missing-callback":
        kwargs["native_approval_callback"] = None
    elif fault == "wrong-cancel":
        kwargs["cancel_event"] = threading.Event()
    else:
        kwargs["approval_policy"] = "never"
    with pytest.raises(adapters.SubscriptionRouteUnavailable):
        adapters.invoke_subscription(profile(), **kwargs)
    assert not kwargs["runs_dir"].exists()


@pytest.mark.asyncio
@pytest.mark.contract
async def test_supervisor_passes_its_exact_attempt_owner_on_worker_thread(approval_env, monkeypatch, tmp_path):
    from lattice.contracts import DraftCreate, ExecuteRequest
    from lattice.supervisor import ExecutionSupervisor
    from test_supervisor import fake_journal
    env = approval_env
    draft = env["store"].create_draft(DraftCreate(project_id=env["project"]["project_id"], actor_binding={"kind": "direct"}, prompt="planted read", route=env["route"]))
    invocation = env["lattice"].seal(draft["draft_id"])
    supervisor = ExecutionSupervisor(env["store"])
    main_thread = threading.get_ident()
    observed = {}

    def planted_invoke(adapter, **kwargs):
        callback = kwargs["native_approval_callback"]
        assert threading.get_ident() != main_thread
        assert callback.store is env["store"]
        assert callback.cancel is kwargs["cancel_event"]
        assert callback.owner == supervisor.boot_id
        assert kwargs["subject"] == invocation["invocation_id"]
        observed["attempt_id"] = callback.attempt_id
        return {"response": "planted completion", "artifacts": []}, fake_journal(tmp_path)

    monkeypatch.setattr("lattice.supervisor.invoke_subscription", planted_invoke)
    attempt = await supervisor.execute(invocation["invocation_id"], ExecuteRequest(idempotency_key="planted-owned-approval"))
    await asyncio.gather(*list(supervisor._tasks.values()))
    assert observed == {"attempt_id": attempt["attempt_id"]}
    assert env["store"].get_attempt(attempt["attempt_id"])["state"] == "succeeded"
