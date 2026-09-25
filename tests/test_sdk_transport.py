"""Real local pipes and the pinned official SDK; no model/provider invocation."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, PermissionResultAllow, PermissionResultDeny
import pytest

from lattice.execution.process import ProcessCancelled, ProcessInputLimit, ProcessOutputLimit
from lattice.execution.sdk_transport import GuardedSDKTransport


def transport(tmp_path, code, **kwargs):
    root = tmp_path / "journal"
    root.mkdir()
    return GuardedSDKTransport([sys.executable, "-u", "-c", code], cwd=tmp_path, env={"PATH": os.environ["PATH"]}, journal_root=root, timeout=10, cancel_event=threading.Event(), **kwargs)


ECHO = "import sys; print('{\"ready\":true}',flush=True); [print(line,end='',flush=True) for line in sys.stdin]"


@pytest.mark.asyncio
@pytest.mark.contract
async def test_transport_roundtrip_preserves_unicode_fifo_and_accepted_bytes(tmp_path):
    native = transport(tmp_path, ECHO)
    await native.connect()
    messages = native.read_messages()
    try:
        assert await anext(messages) == {"ready": True}
        assert native.is_ready()  # Empty input queue has not closed stdin.
        frames = [json.dumps({"value": value}, ensure_ascii=False) + "\n" for value in ["αé", "line\ninside", "last"]]
        for frame in frames:
            await native.write(frame)
        assert [await anext(messages) for _ in frames] == [json.loads(frame) for frame in frames]
        await native.end_input()
        assert [message async for message in messages] == []
        assert (native.root / "stdin.accepted.log").read_bytes() == "".join(frames).encode()
        assert not native.is_ready()
        with pytest.raises(RuntimeError, match="not accepting"):
            await native.write("{}\n")
        with pytest.raises(RuntimeError, match="reconnect"):
            await native.connect()
    finally:
        await native.close()


@pytest.mark.asyncio
@pytest.mark.contract
@pytest.mark.parametrize("raw", [b"{\"x\":1}", b"[]\n", b"{\"x\":1,\"x\":2}\n", b'{"x":"\xff"}\n'])
async def test_invalid_native_framing_fails_visibly(tmp_path, raw):
    native = transport(tmp_path, f"import os; os.write(1,{raw!r})")
    await native.connect()
    try:
        with pytest.raises((ValueError, UnicodeDecodeError)):
            _ = [message async for message in native.read_messages()]
        assert native.cancel.is_set()
        assert (native.root / "stdout.log").read_bytes() == raw
    finally:
        await native.close()


@pytest.mark.asyncio
@pytest.mark.contract
async def test_input_cap_rejects_before_acceptance_and_stops_peer(tmp_path):
    first = '{"x":"é"}\n'
    native = transport(tmp_path, ECHO, max_input_bytes=len(first.encode()))
    await native.connect()
    messages = native.read_messages()
    try:
        assert await anext(messages) == {"ready": True}
        await native.write(first)
        assert await anext(messages) == {"x": "é"}
        with pytest.raises(ProcessInputLimit):
            await native.write(" ")
        assert (native.root / "stdin.accepted.log").read_text() == first
        assert native.cancel.is_set()
    finally:
        await native.close()
    assert native._task.done()


@pytest.mark.asyncio
@pytest.mark.contract
async def test_output_cap_and_provider_exit_preserve_failure(tmp_path):
    native = transport(tmp_path, "import os; os.write(1,b'x'*1024)", max_output_bytes=128)
    await native.connect()
    try:
        with pytest.raises(ProcessOutputLimit):
            _ = [message async for message in native.read_messages()]
    finally:
        await native.close()
    assert isinstance(native.failure, ProcessOutputLimit)
    assert (native.root / "stdout.log").stat().st_size <= 128


@pytest.mark.asyncio
@pytest.mark.contract
async def test_lost_input_journal_stops_peer_before_acceptance(tmp_path, monkeypatch):
    native = transport(tmp_path, ECHO)
    await native.connect()
    messages = native.read_messages()
    try:
        assert await anext(messages) == {"ready": True}
        original_open = os.open

        def denied(path, flags, *args, **kwargs):
            if Path(path) == native.root / "stdin.accepted.log":
                raise PermissionError("planted input journal denial")
            return original_open(path, flags, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(os, "open", denied)
            with pytest.raises(PermissionError, match="planted input journal denial"):
                await native.write("{}\n")
        assert native.cancel.is_set()
        assert native._accepted_bytes == 0
    finally:
        await native.close()


@pytest.mark.asyncio
@pytest.mark.contract
async def test_pre_cancelled_transport_never_starts_peer(tmp_path):
    marker = tmp_path / "spawned"
    native = transport(tmp_path, f"from pathlib import Path; Path({str(marker)!r}).touch()")
    native.cancel.set()
    await native.connect()
    try:
        with pytest.raises(ProcessCancelled):
            _ = [message async for message in native.read_messages()]
        assert not marker.exists()
    finally:
        await native.close()


@pytest.mark.asyncio
@pytest.mark.contract
async def test_sdk_reader_shutdown_does_not_cancel_graceful_process_exit(tmp_path):
    native = transport(tmp_path, ECHO)
    await native.connect()
    messages = native.read_messages()
    started = asyncio.Event()

    async def read_next():
        started.set()
        return await anext(messages)

    try:
        assert await anext(messages) == {"ready": True}
        reader = asyncio.create_task(read_next())
        await started.wait()
        # This is Query._close_impl's documented ordering: cancel the read task,
        # then close transport input. Reader cancellation is not an operator act.
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader
        assert not native.cancel.is_set()
    finally:
        await native.close()
    assert native.failure is None
    assert native.result.returncode == 0


@pytest.mark.asyncio
@pytest.mark.contract
async def test_empty_writes_do_not_consume_queue_entries(tmp_path, monkeypatch):
    native = transport(tmp_path, ECHO)
    await native.connect()
    messages = native.read_messages()
    try:
        assert await anext(messages) == {"ready": True}
        queued = []
        with monkeypatch.context() as patch:
            patch.setattr(native._inputs, "put_nowait", queued.append)
            for _ in range(100):
                await native.write("")
        assert queued == []
        assert native._accepted_bytes == 0
    finally:
        await native.close()


@pytest.mark.asyncio
@pytest.mark.contract
@pytest.mark.parametrize("release", ["space", "eof", "cancel"])
async def test_stalled_input_is_bounded_and_shutdown_does_not_wait_for_space(tmp_path, monkeypatch, release):
    # A controlled consumer preserves the queue/backpressure seam without
    # depending on OS pipe capacity or the native provider's read scheduling.
    stopped = threading.Event()

    def stalled(command, **kwargs):
        assert stopped.wait(5), "test did not release the planted consumer"
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("lattice.execution.sdk_transport.run_bounded", stalled)
    native = transport(tmp_path, "")
    await native.connect()
    pending = None
    try:
        capacity = native._inputs.maxsize
        assert capacity > 1, "payload entries need a finite bound plus an EOF slot"
        for _ in range(capacity - 1):
            await native.write("1\n")
        saturated = asyncio.Event()
        original_size = native._inputs.qsize

        def observed_size():
            size = original_size()
            if size >= capacity - 1:
                saturated.set()
            return size

        monkeypatch.setattr(native._inputs, "qsize", observed_size)
        pending = asyncio.create_task(native.write("2\n"))
        await asyncio.wait_for(saturated.wait(), 1)
        assert not pending.done()
        before = b"1\n" * (capacity - 1)
        assert (native.root / "stdin.accepted.log").read_bytes() == before
        if release == "space":
            assert native._inputs.get_nowait() == b"1\n"
            await asyncio.wait_for(pending, 1)
            assert original_size() == capacity - 1
            assert (native.root / "stdin.accepted.log").read_bytes() == before + b"2\n"
        else:
            if release == "eof":
                await asyncio.wait_for(native.end_input(), 1)
                assert original_size() == capacity
                assert [native._inputs.get_nowait() for _ in range(capacity)] == [b"1\n"] * (capacity - 1) + [None]
            else:
                native.cancel.set()
            error_type = ProcessCancelled if release == "cancel" else RuntimeError
            detail = "process cancelled" if release == "cancel" else "not accepting"
            with pytest.raises(error_type, match=detail):
                await asyncio.wait_for(pending, 1)
            assert (native.root / "stdin.accepted.log").read_bytes() == before
    finally:
        stopped.set()
        if pending is not None and not pending.done():
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        await native.close()


# A synthetic native peer tests what the official SDK emits over real pipes.
# It performs no tool action; native permission enforcement needs live evidence.
PEER = r'''
import json,sys
def receive(): return json.loads(sys.stdin.readline())
def send(value): print(json.dumps(value),flush=True)
initialize=receive()
assert initialize['request']['subtype']=='initialize'
send({'type':'control_response','response':{'subtype':'success','request_id':initialize['request_id'],'response':{'current_permission_mode':'default','session_state':'idle'}}})
send({'type':'control_request','request_id':'permission_1','request':{'subtype':'can_use_tool','tool_name':'Read','tool_use_id':'tool_1','input':{'file_path':'/planted/source.txt','offset':17},'permission_suggestions':[]}})
response=receive()
send({'type':'system','subtype':'status','status':'idle','permission_observation':response})
sys.stdin.read()
'''


@pytest.mark.asyncio
@pytest.mark.contract
@pytest.mark.parametrize("decision", ["allow", "deny"])
async def test_official_sdk_sends_exact_single_action_response_without_saved_rules(tmp_path, decision):
    native = transport(tmp_path, PEER)
    calls = []

    async def callback(name, inputs, context):
        calls.append((name, inputs, context.tool_use_id))
        if decision == "allow":
            return PermissionResultAllow(updated_input=inputs)
        return PermissionResultDeny(message="Planted operator denial")

    client = ClaudeSDKClient(options=ClaudeAgentOptions(can_use_tool=callback), transport=native)
    try:
        await client.connect()
        message = await anext(client.receive_messages())
        observed = message.data["permission_observation"]
        assert calls == [("Read", {"file_path": "/planted/source.txt", "offset": 17}, "tool_1")]
        expected = {"behavior": "allow", "updatedInput": calls[0][1]} if decision == "allow" else {"behavior": "deny", "message": "Planted operator denial"}
        assert observed == {"type": "control_response", "response": {"subtype": "success", "request_id": "permission_1", "response": expected}}
        assert "updatedPermissions" not in observed["response"]["response"]
    finally:
        await client.disconnect()
    assert native._task.done()
    assert native.result.returncode == 0
