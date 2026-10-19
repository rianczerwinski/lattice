"""Official JSONL input lifetime; no provider calls or authentication required."""

from __future__ import annotations

import hashlib
import json
import queue
import sys
import threading

import pytest

from lattice.execution import adapters
from lattice.execution.checkpoint import load_checkpoint
from lattice.execution.providers import claude


SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"], "additionalProperties": False}
PROMPT = 'Unchanged user text: café, \u2028, "quotes", and\nnewlines.'


@pytest.mark.contract
def test_wire_encoding_is_one_unchanged_user_message():
    encoded = claude.encode_user_message(PROMPT)
    assert encoded.count("\n") == 1
    assert json.loads(encoded) == {
        "type": "user", "message": {"role": "user", "content": PROMPT},
        "parent_tool_use_id": None, "session_id": "",
    }


@pytest.mark.regression
def test_only_complete_top_level_stdout_result_releases_eof():
    controller = claude.StreamingInput(claude.encode_user_message(PROMPT))
    assert json.loads(controller.messages.get_nowait())["message"]["content"] == PROMPT
    controller.observe("stderr", b'{"type":"result"}\n')
    nested = json.dumps({"type": "user", "message": {"content": '{"type":"result"}\n'}, "data": {"type": "result"}}, ensure_ascii=False).encode() + b"\n"
    for byte in nested:
        controller.observe("stdout", bytes([byte]))
    terminal = b'{"type":"result","result":"caf\xc3\xa9"}\n'
    for byte in terminal[:-1]:
        controller.observe("stdout", bytes([byte]))
    with pytest.raises(queue.Empty):
        controller.messages.get_nowait()
    controller.observe("stdout", terminal[-1:])
    assert controller.messages.get_nowait() is None
    controller.observe("stdout", terminal)
    with pytest.raises(queue.Empty):
        controller.messages.get_nowait()  # Duplicate terminals cannot send extra input.


@pytest.mark.regression
@pytest.mark.parametrize("frame", [b'not json\n', b'[]\n', b'{"type":"result","text":"\xff"}\n'])
def test_invalid_native_frames_close_input_without_claiming_a_result(frame):
    controller = claude.StreamingInput(claude.encode_user_message(PROMPT))
    controller.messages.get_nowait()
    controller.observe("stdout", frame)
    assert controller.protocol_error
    assert controller.messages.get_nowait() is None
    with pytest.raises(queue.Empty):
        controller.messages.get_nowait()


def invoke_process(tmp_path, monkeypatch, body, **options):
    """Exercise command compilation, the actual pipe runner, journals and parser.

    Only executable discovery is replaced; Python is a deliberately bounded
    protocol producer, not a claim about Claude's native implementation.
    """
    profile = next(item for item in adapters.DEFAULT_ADAPTERS if item.provider_code == "anthropic")
    compile_command = adapters.build_command

    def command(*args, **kwargs):
        native, input_text, files = compile_command(*args, **kwargs)
        assert native.count("--input-format") == 1
        assert native[native.index("--input-format") + 1] == "stream-json"
        assert "--permission-prompt-tool" not in native
        return [sys.executable, "-c", preamble + body], input_text, files

    monkeypatch.setattr(adapters.shutil, "which", lambda _: sys.executable)
    monkeypatch.setattr(adapters, "_cli_version", lambda _: "protocol-fixture")
    monkeypatch.setattr(adapters, "_binary_sha256", lambda _: "f" * 64)
    monkeypatch.setattr(adapters, "build_command", command)
    init = json.dumps({"type": "system", "subtype": "init", "model": profile.model_code})
    preamble = (
        "import json,os,sys,threading,time\n"
        "request=json.loads(sys.stdin.buffer.readline())\n"
        f"assert request['message']=={{'role':'user','content':{PROMPT!r}}}\n"
        f"print({init!r},flush=True)\n"
    )
    return adapters.invoke_subscription(
        profile, prompt=PROMPT, system_prompt="separate system instructions", schema=SCHEMA,
        runs_dir=tmp_path / "runs", subject="stream-input", timeout=5, **options,
    )


@pytest.mark.regression
def test_large_reply_is_fully_captured_before_stdin_eof(tmp_path, monkeypatch):
    # The producer terminates immediately on EOF, even with an asynchronous write
    # outstanding. Early-EOF transport loses its tail; result-gated EOF cannot.
    body = r"""
answer='x'*420000+'café\u2028tail'
source=json.dumps({'type':'user','message':{'content':answer}})+'\n'
terminal=json.dumps({'type':'result','subtype':'success','is_error':False,'result':json.dumps({'answer':answer})})+'\n'
def produce():
    sys.stdout.write(source+terminal)
    sys.stdout.flush()
threading.Thread(target=produce,daemon=True).start()
assert sys.stdin.buffer.read()==b''
os._exit(0)
"""
    observed = {"stdout": bytearray(), "stderr": bytearray()}
    payload, journal = invoke_process(tmp_path, monkeypatch, body, on_chunk=lambda channel, chunk: observed[channel].extend(chunk))
    assert payload == {"answer": "x" * 420000 + "café\u2028tail"}
    assert bytes(observed["stdout"]) == (journal / "stdout.log").read_bytes()
    assert bytes(observed["stderr"]) == b""
    request = json.loads((journal / "request.json").read_text())
    wire = (journal / "provider-prompt.txt").read_bytes()
    assert wire == claude.encode_user_message(PROMPT).encode()
    assert request["provider_prompt_sha256"] == hashlib.sha256(wire).hexdigest()
    assert (journal / "prompt.txt").read_text() == PROMPT
    assert (journal / "system-prompt.txt").read_text() == "separate system instructions"
    assert request["metadata"]["stdin_eof_policy"] == "after-complete-native-result-or-invalid-frame"
    assert load_checkpoint(journal)["state"] == "succeeded"


@pytest.mark.regression
@pytest.mark.parametrize("body,kind", [
    ("print('not json',flush=True)\nsys.stdin.buffer.read()", "malformed_output"),
    ("print(json.dumps({'type':'user','message':{'content':json.dumps({'answer':'source only'})}}),flush=True)", "malformed_output"),
    ("print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':json.dumps({'answer':'one'})})+'\\n'+json.dumps({'type':'result','subtype':'success','is_error':False,'result':json.dumps({'answer':'two'})}),flush=True)\nsys.stdin.buffer.read()", "malformed_output"),
    ("print(json.dumps({'type':'result','subtype':'error_during_execution','is_error':True,'result':'failed'}),flush=True)\nsys.stdin.buffer.read()", "malformed_output"),
    ("sys.stdout.buffer.write(b'{\"type\":\"user\",\"message\":{\"content\":\"\\xff\"}}\\n')\nsys.stdout.flush()\nprint(json.dumps({'type':'result','subtype':'success','is_error':False,'result':json.dumps({'answer':'must not pass'})}),flush=True)\nsys.stdin.buffer.read()", "malformed_output"),
    ("print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':json.dumps({'answer':'must not pass'})}),flush=True)\nsys.stdout.buffer.write(b'{\"type\":\"system\",\"subtype\":\"session_state_changed\",\"text\":\"\\xff\"}')\nsys.stdout.flush()", "malformed_output"),
])
def test_incomplete_or_invalid_terminal_remains_failure(tmp_path, monkeypatch, body, kind):
    with pytest.raises(adapters.SubscriptionInvocationFailed) as failure:
        invoke_process(tmp_path, monkeypatch, body)
    assert failure.value.kind == kind
    terminal = json.loads((failure.value.journal / "terminal.json").read_text())
    assert terminal["status"] == "failed"
    assert terminal["failure_kind"] == kind


@pytest.mark.regression
@pytest.mark.parametrize("mode,kind", [("cancel", "operator_interruption"), ("timeout", "timeout")])
def test_waiting_for_result_remains_interruptible(tmp_path, monkeypatch, mode, kind):
    cancel = threading.Event()
    kwargs = {"cancel_event": cancel}
    if mode == "cancel":
        kwargs["on_chunk"] = lambda channel, chunk: cancel.set() if channel == "stdout" else None
        body = "sys.stdin.buffer.read()"
    else:
        body = "time.sleep(10)"
    if mode == "timeout":
        # Shorten the declared deadline, not the controller's state machine.
        original = adapters.run_bounded
        monkeypatch.setattr(adapters, "run_bounded", lambda command, **kw: original(command, **{**kw, "timeout": .15}))
    with pytest.raises(adapters.SubscriptionInvocationFailed) as failure:
        invoke_process(tmp_path, monkeypatch, body, **kwargs)
    assert failure.value.kind == kind


@pytest.mark.regression
def test_output_budget_precedes_stream_observation(tmp_path, monkeypatch):
    original = adapters.run_bounded
    monkeypatch.setattr(adapters, "run_bounded", lambda command, **kw: original(command, **{**kw, "max_output_bytes": 256}))
    with pytest.raises(adapters.SubscriptionInvocationFailed) as failure:
        invoke_process(tmp_path, monkeypatch, "print(json.dumps({'type':'user','message':{'content':'x'*10000}}),flush=True)\nsys.stdin.buffer.read()")
    assert failure.value.kind == "output_limit"
    assert (failure.value.journal / "stdout.log").stat().st_size == 256


@pytest.mark.regression
def test_wire_encoding_overhead_is_admitted_before_process_launch(tmp_path, monkeypatch):
    raw_size = len(PROMPT.encode()) + len("separate system instructions".encode())
    monkeypatch.setattr(adapters, "MAX_MODEL_INPUT_BYTES", raw_size + 1)
    monkeypatch.setattr(adapters, "run_bounded", lambda *args, **kwargs: pytest.fail("oversized wire input launched a process"))
    with pytest.raises(ValueError, match="compiled provider input exceeds"):
        invoke_process(tmp_path, monkeypatch, "raise AssertionError('must not launch')")


@pytest.mark.regression
@pytest.mark.parametrize("message,kind", [("authentication required", "authentication"), ("rate limit reached", "rate_limit")])
def test_nonjson_native_failure_keeps_its_original_cause(tmp_path, monkeypatch, message, kind):
    with pytest.raises(adapters.SubscriptionInvocationFailed) as failure:
        invoke_process(tmp_path, monkeypatch, f"print({message!r},flush=True)\nsys.exit(1)")
    assert failure.value.kind == kind
