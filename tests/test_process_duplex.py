"""Real pipes exercise delayed replies without a provider or permission grant."""

import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from lattice.execution.process import ProcessCancelled, ProcessInputLimit, ProcessOutputLimit, run_bounded


@pytest.fixture
def stdin_writes(monkeypatch):
    """Observe the actual pipe-write boundary, before child scheduling can hide it."""
    real_popen = subprocess.Popen
    writes = []
    entered = threading.Event()
    returned = threading.Event()

    class ObservedInput:
        def __init__(self, stream):
            self.stream = stream

        def write(self, message):
            writes.append(message)
            entered.set()
            result = self.stream.write(message)
            returned.set()
            return result

        def __getattr__(self, name):
            return getattr(self.stream, name)

    def spawn(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        if proc.stdin is not None:
            proc.stdin = ObservedInput(proc.stdin)
        return proc

    monkeypatch.setattr(subprocess, "Popen", spawn)
    return writes, entered, returned


@pytest.mark.contract
def test_reply_after_observed_request_preserves_exact_bytes_and_eof(tmp_path: Path):
    messages = queue.Queue()
    messages.put(b"task\n")
    observed = bytearray()

    def receive(channel, chunk):
        if channel == "stdout":
            observed.extend(chunk)
            if observed == b"request\n":
                messages.put("réponse\n".encode())
                messages.put(None)

    child = "import sys; assert sys.stdin.buffer.readline()==b'task\\n'; print('request',flush=True); data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data); sys.stdout.flush()"
    result = run_bounded([sys.executable, "-c", child], timeout=5, input_messages=messages, on_chunk=receive, stdout_journal=tmp_path / "stdout.log", stderr_journal=tmp_path / "stderr.log")
    assert result.returncode == 0
    assert result.stdout == "request\nréponse\n"
    assert (tmp_path / "stdout.log").read_bytes() == result.stdout.encode()


@pytest.mark.contract
def test_child_exit_does_not_need_input_sentinel():
    result = run_bounded([sys.executable, "-c", "print('done')"], timeout=5, input_messages=queue.Queue())
    assert result.returncode == 0
    assert result.stdout == "done\n"


@pytest.mark.contract
def test_cancel_while_waiting_for_reply_preserves_request(tmp_path: Path):
    cancel = threading.Event()
    seen = bytearray()

    def receive(channel, chunk):
        if channel == "stdout":
            seen.extend(chunk)
            if b"request\n" in seen:
                cancel.set()

    with pytest.raises(ProcessCancelled) as failure:
        run_bounded([sys.executable, "-c", "import sys; print('request',flush=True); sys.stdin.read(); print('acted',flush=True)"], timeout=5, input_messages=queue.Queue(), cancel_event=cancel, on_chunk=receive, stdout_journal=tmp_path / "stdout.log", stderr_journal=tmp_path / "stderr.log")
    assert failure.value.stdout == "request\n"
    assert (tmp_path / "stdout.log").read_bytes() == b"request\n"


@pytest.mark.contract
def test_timeout_while_waiting_for_reply_closes_process():
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded([sys.executable, "-c", "import sys; sys.stdin.read()"], timeout=0.15, input_messages=queue.Queue())


@pytest.mark.contract
def test_streaming_input_budget_rejects_frame_before_delivery(tmp_path: Path, stdin_writes):
    messages = queue.Queue()
    messages.put(b"first\n")
    observed = bytearray()

    def receive(channel, chunk):
        if channel == "stdout":
            observed.extend(chunk)
            if b"first\n" == observed:
                messages.put(b"too much\n")

    child = "import sys; first=sys.stdin.buffer.readline(); sys.stdout.buffer.write(first); sys.stdout.flush(); rest=sys.stdin.buffer.read(); sys.stdout.buffer.write(rest); sys.stdout.flush()"
    with pytest.raises(ProcessInputLimit, match="10-byte input limit"):
        run_bounded([sys.executable, "-c", child], timeout=5, input_messages=messages, max_input_bytes=10, on_chunk=receive, stdout_journal=tmp_path / "stdout.log", stderr_journal=tmp_path / "stderr.log")
    assert stdin_writes[0] == [b"first\n"]
    assert (tmp_path / "stdout.log").read_bytes() == b"first\n"


@pytest.mark.contract
def test_output_limit_still_stops_duplex_process(tmp_path: Path):
    with pytest.raises(ProcessOutputLimit):
        run_bounded([sys.executable, "-c", "import os,sys; os.write(1,b'x'*8192); sys.stdin.read()"], timeout=5, input_messages=queue.Queue(), max_output_bytes=16, stdout_journal=tmp_path / "stdout.log", stderr_journal=tmp_path / "stderr.log")
    assert (tmp_path / "stdout.log").read_bytes() == b"x" * 16


@pytest.mark.contract
def test_input_modes_cannot_be_mixed_before_spawn():
    with pytest.raises(ValueError, match="mutually exclusive"):
        run_bounded(["not-a-real-executable"], timeout=5, input_text="one-shot", input_messages=queue.Queue())


@pytest.mark.contract
def test_invalid_queued_input_is_visible():
    messages = queue.Queue()
    messages.put("not bytes")
    with pytest.raises(TypeError, match="bytes or None"):
        run_bounded([sys.executable, "-c", "import sys; sys.stdin.read()"], timeout=5, input_messages=messages)


@pytest.mark.contract
def test_one_shot_input_bound_uses_encoded_bytes_before_spawn():
    with pytest.raises(ProcessInputLimit):
        run_bounded(["not-a-real-executable"], timeout=5, input_text="é", max_input_bytes=1)


@pytest.mark.contract
def test_already_cancelled_request_never_reaches_spawn(monkeypatch):
    cancel = threading.Event()
    cancel.set()

    def forbidden_spawn(*args, **kwargs):
        raise AssertionError("already-cancelled execution reached process spawn")

    monkeypatch.setattr(subprocess, "Popen", forbidden_spawn)
    with pytest.raises(ProcessCancelled):
        run_bounded([sys.executable, "-c", "raise SystemExit(0)"], timeout=5, input_messages=queue.Queue(), cancel_event=cancel)


@pytest.mark.contract
def test_binary_burst_fifo_preserves_order_at_exact_input_cap(tmp_path: Path):
    messages = queue.Queue()
    frames = [bytes([n, 255 - n, 0, 10]) for n in range(256)]
    for frame in frames:
        messages.put(frame)
    messages.put(None)
    expected = b"".join(frames)
    result = run_bounded([sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"], timeout=5, input_messages=messages, max_input_bytes=len(expected), stdout_journal=tmp_path / "stdout.log", stderr_journal=tmp_path / "stderr.log")
    assert result.returncode == 0
    assert (tmp_path / "stdout.log").read_bytes() == expected
    assert result.stdout == expected.decode("utf-8", errors="replace")


@pytest.mark.contract
@pytest.mark.parametrize("ending", ["cancel", "timeout"])
def test_inflight_input_writer_does_not_prevent_shutdown(stdin_writes, ending):
    messages = queue.Queue()
    messages.put(b"x" * (2 * 1024 * 1024))
    cancel = threading.Event()
    ready = threading.Event()
    before = set(threading.enumerate())

    def observe(channel, chunk):
        if channel == "stdout" and b"ready" in chunk:
            assert stdin_writes[1].wait(1), "writer must have entered the real pipe write"
            assert not stdin_writes[2].is_set(), "oversized input write must still be in flight"
            ready.set()
            if ending == "cancel":
                cancel.set()

    error = ProcessCancelled if ending == "cancel" else subprocess.TimeoutExpired
    with pytest.raises(error):
        run_bounded([sys.executable, "-c", "import os,time; assert os.read(0,1)==b'x'; print('ready',flush=True); time.sleep(30)"], timeout=1, input_messages=messages, cancel_event=cancel, on_chunk=observe)
    assert ready.is_set()
    assert stdin_writes[1].is_set()
    assert not [thread for thread in threading.enumerate() if thread not in before and thread.name == "process-stdin-writer"]
