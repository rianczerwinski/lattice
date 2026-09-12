"""Native callback/ledger boundary with planted authority; no model calls."""

import asyncio
import json
import threading

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, PermissionResultAllow, PermissionResultDeny, PermissionUpdate, ToolPermissionContext
import pytest

from lattice import native_approvals as ledger
from lattice.execution.native_approval import NativeApprovalCallback
from lattice.store import LatticeConflict
from test_native_approvals import approval_env, decide, start
from test_sdk_transport import PEER, transport


def callback(env, attempt):
    return NativeApprovalCallback(env["store"], attempt, owner_boot_id="test-boot", cancel_event=threading.Event())


async def pending(env, attempt, tool_id="tool_1"):
    async def inspect():
        while True:
            for item in ledger.inspect(env["store"], attempt)["requests"]:
                if item["binding"]["action"]["tool_use_id"] == tool_id:
                    return item
            await asyncio.sleep(0)
    return await asyncio.wait_for(inspect(), 2)


@pytest.mark.asyncio
@pytest.mark.contract
@pytest.mark.parametrize("choice", ["allow", "deny"])
async def test_callback_waits_for_operator_then_returns_exact_persisted_action(approval_env, choice):
    env = approval_env
    attempt = start(env)
    native = callback(env, attempt)
    inputs = {"file_path": "/planted/source.txt", "nested": {"offset": 17}}
    context = ToolPermissionContext(tool_use_id="tool_1", title="Read the planted source", suggestions=[PermissionUpdate(type="setMode", mode="bypassPermissions")])
    task = asyncio.create_task(native("Read", inputs, context))
    try:
        item = await pending(env, attempt)
        assert not task.done()
        assert item["decision"] is None
        assert item["binding"]["action"]["context"]["title"] == "Read the planted source"
        inputs["nested"]["offset"] = 99
        decide(env, attempt, item, choice)
        response = await asyncio.wait_for(task, 2)
        if choice == "allow":
            assert isinstance(response, PermissionResultAllow)
            assert response.updated_input == {"file_path": "/planted/source.txt", "nested": {"offset": 17}}
            assert response.updated_permissions is None
        else:
            assert isinstance(response, PermissionResultDeny)
            assert response.interrupt is False
            assert response.message == "Operator denied this exact action"
        assert ledger.inspect(env["store"], attempt)["requests"][0]["state"] == "released-to-adapter"
        native.raise_if_failed()
        assert not native.cancel.is_set()
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
@pytest.mark.contract
async def test_native_cancellation_withdraws_only_its_callback(approval_env):
    env = approval_env
    attempt = start(env)
    native = callback(env, attempt)
    first = asyncio.create_task(native("Read", {"file_path": "/planted/one"}, ToolPermissionContext(tool_use_id="tool_1")))
    second = asyncio.create_task(native("Read", {"file_path": "/planted/two"}, ToolPermissionContext(tool_use_id="tool_2")))
    try:
        await pending(env, attempt)
        sibling = await pending(env, attempt, "tool_2")
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        view = ledger.inspect(env["store"], attempt)
        assert [item["state"] for item in view["requests"]] == ["withdrawn", "awaiting-operator"]
        assert not native.cancel.is_set()
        decide(env, attempt, sibling)
        assert isinstance(await asyncio.wait_for(second, 2), PermissionResultAllow)
        native.raise_if_failed()
    finally:
        for task in (first, second):
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task


@pytest.mark.asyncio
@pytest.mark.contract
@pytest.mark.parametrize("at_entry", [False, True])
async def test_operator_cancel_never_releases_a_response(approval_env, at_entry):
    env = approval_env
    attempt = start(env)
    native = callback(env, attempt)
    if at_entry:
        native.cancel.set()
    task = asyncio.create_task(native("Read", {"file_path": "/planted/source"}, ToolPermissionContext(tool_use_id="tool_1")))
    if not at_entry:
        await pending(env, attempt)
        native.cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    items = ledger.inspect(env["store"], attempt)["requests"]
    if at_entry:
        assert items == []
    else:
        assert items[0]["state"] == "withdrawn"
    assert all(item["release"] is None for item in items)


@pytest.mark.asyncio
@pytest.mark.contract
@pytest.mark.parametrize("fault", ["expired", "closed", "duplicate"])
async def test_stale_or_released_authority_stops_the_native_channel(approval_env, monkeypatch, fault):
    env = approval_env
    attempt = start(env)
    native = callback(env, attempt)
    task = asyncio.create_task(native("Read", {"file_path": "/planted/source"}, ToolPermissionContext(tool_use_id="tool_1")))
    try:
        item = await pending(env, attempt)
        decide(env, attempt, item)
        if fault == "expired":
            monkeypatch.setattr(ledger, "monotonic", lambda: item["binding"]["deadline_monotonic"] + 1)
        elif fault == "closed":
            ledger.close(env["store"], attempt, owner_boot_id="test-boot", reason="planted disconnect")
        else:
            ledger.take_decision(env["store"], attempt, item["request_id"], owner_boot_id="test-boot", request_sha256=item["request_sha256"])
        with pytest.raises(LatticeConflict):
            await asyncio.wait_for(task, 2)
        assert native.cancel.is_set()
        with pytest.raises(ExceptionGroup, match="native approval callback failed"):
            native.raise_if_failed()
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
@pytest.mark.contract
@pytest.mark.parametrize("operation", ["request", "inspect", "take_decision", "withdraw"])
async def test_persistence_failure_cannot_be_hidden_by_sdk_continuation(approval_env, monkeypatch, operation):
    env = approval_env
    attempt = start(env)
    native = callback(env, attempt)

    def denied(*args, **kwargs):
        raise OSError(f"planted {operation} failure")

    if operation == "request":
        monkeypatch.setattr(ledger, operation, denied)
    task = asyncio.create_task(native("Read", {"file_path": "/planted/source"}, ToolPermissionContext(tool_use_id="tool_1")))
    try:
        if operation != "request":
            item = await pending(env, attempt)
            decide(env, attempt, item)
            monkeypatch.setattr(ledger, operation, denied)
            if operation == "withdraw":
                task.cancel()
        with pytest.raises(OSError, match=f"planted {operation} failure"):
            await asyncio.wait_for(task, 2)
        assert native.cancel.is_set()
        with pytest.raises(ExceptionGroup) as error:
            native.raise_if_failed()
        assert any(str(cause) == f"planted {operation} failure" for cause in error.value.exceptions)
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
@pytest.mark.contract
@pytest.mark.parametrize("choice", ["allow", "deny"])
async def test_official_sdk_delivers_only_the_recorded_callback_decision(approval_env, tmp_path, choice):
    env = approval_env
    attempt = start(env)
    approval = callback(env, attempt)
    wire = transport(tmp_path, PEER)
    wire.cancel = approval.cancel
    client = ClaudeSDKClient(options=ClaudeAgentOptions(can_use_tool=approval), transport=wire)
    try:
        await client.connect()
        item = await pending(env, attempt)
        assert item["decision"] is None
        decide(env, attempt, item, choice)
        message = await asyncio.wait_for(anext(client.receive_messages()), 2)
        decision = message.data["permission_observation"]["response"]["response"]
        expected = {"behavior": "allow", "updatedInput": {"file_path": "/planted/source.txt", "offset": 17}} if choice == "allow" else {"behavior": "deny", "message": "Operator denied this exact action"}
        assert decision == expected
        assert ledger.inspect(env["store"], attempt)["requests"][0]["release"]["decision"] == choice
        approval.raise_if_failed()
    finally:
        await client.disconnect()
    assert wire.failure is None
    assert wire.result.returncode == 0


@pytest.mark.asyncio
@pytest.mark.contract
@pytest.mark.parametrize("boundary", ["inspect", "take_decision"])
async def test_cancel_during_ledger_io_does_not_return_an_allow(approval_env, monkeypatch, boundary):
    env = approval_env
    attempt = start(env)
    native = callback(env, attempt)
    task = asyncio.create_task(native("Read", {"file_path": "/planted/source"}, ToolPermissionContext(tool_use_id="tool_1")))
    try:
        item = await pending(env, attempt)
        decide(env, attempt, item)
        original = getattr(ledger, boundary)

        def cancel_after(*args, **kwargs):
            result = original(*args, **kwargs)
            native.cancel.set()
            return result

        monkeypatch.setattr(ledger, boundary, cancel_after)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        state = ledger.inspect(env["store"], attempt)["requests"][0]
        if boundary == "inspect":
            assert state["state"] == "withdrawn"
            assert state["release"] is None
        else:
            assert state["state"] == "released-to-adapter"
            assert state["release"] is not None  # Already consumed, not replayable.
        native.raise_if_failed()
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


CANCEL_PEER = r'''
import json,sys
def receive(): return json.loads(sys.stdin.readline())
def send(value): print(json.dumps(value),flush=True)
initialize=receive()
send({'type':'control_response','response':{'subtype':'success','request_id':initialize['request_id'],'response':{}}})
send({'type':'control_request','request_id':'permission_1','request':{'subtype':'can_use_tool','tool_name':'Read','tool_use_id':'tool_1','input':{'file_path':'/planted/source.txt'},'permission_suggestions':[]}})
assert receive()['type']=='user'
send({'type':'control_cancel_request','request_id':'permission_1'})
send({'type':'system','subtype':'status','status':'idle'})
sys.stdin.read()
'''


@pytest.mark.asyncio
@pytest.mark.contract
async def test_official_sdk_native_cancel_withdraws_without_an_allow_response(approval_env, tmp_path):
    env = approval_env
    attempt = start(env)
    approval = callback(env, attempt)
    wire = transport(tmp_path, CANCEL_PEER)
    wire.cancel = approval.cancel
    client = ClaudeSDKClient(options=ClaudeAgentOptions(can_use_tool=approval), transport=wire)
    try:
        await client.connect()
        await pending(env, attempt)
        # A synthetic user frame releases the planted peer only after the real
        # ledger request exists; no sleep guesses when the callback is active.
        await client.query("release the planted cancellation")
        await asyncio.wait_for(anext(client.receive_messages()), 2)

        async def withdrawn():
            while ledger.inspect(env["store"], attempt)["requests"][0]["state"] != "withdrawn":
                await asyncio.sleep(0)

        await asyncio.wait_for(withdrawn(), 2)
        assert not approval.cancel.is_set()
        approval.raise_if_failed()
    finally:
        await client.disconnect()
    frames = [json.loads(line) for line in (wire.root / "stdin.accepted.log").read_text().splitlines()]
    assert [frame["type"] for frame in frames] == ["control_request", "user"]
    assert wire.failure is None
    assert wire.result.returncode == 0
