"""Owned real process groups must stop even without inherited output pipes."""

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import lattice.execution.process as process


@pytest.fixture
def owned_processes(monkeypatch):
    """Retain exact fixture-created session leaders for failure-safe cleanup."""
    real_popen = subprocess.Popen
    owned = []

    def spawn(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        assert kwargs.get("start_new_session") is True
        owned.append(child)
        return child

    monkeypatch.setattr(process.subprocess, "Popen", spawn)
    try:
        yield owned
    finally:
        for child in owned:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=3)


@pytest.mark.regression
@pytest.mark.parametrize("ending", ["cancel", "timeout", "leader_exit"])
def test_redirected_term_resistant_descendant_cannot_continue_after_return(tmp_path: Path, owned_processes, ending):
    ready, trigger, survived = (tmp_path / name for name in ("ready", "trigger", "survived"))
    descendant = "\n".join([
        "import signal,sys,time",
        "from pathlib import Path",
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
        "Path(sys.argv[1]).touch()",
        "while not Path(sys.argv[2]).exists(): time.sleep(0.005)",
        "Path(sys.argv[3]).touch()",
    ])
    leader = "\n".join([
        "import subprocess,sys,time",
        "from pathlib import Path",
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], *sys.argv[2:5]], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)",
        "while not Path(sys.argv[2]).exists(): time.sleep(0.005)",
        "print('descendant-ready', flush=True)",
        "if sys.argv[5] != 'leader_exit': time.sleep(30)",
    ])
    cancel = threading.Event()

    def observe(channel, chunk):
        if ending == "cancel" and channel == "stdout" and b"descendant-ready" in chunk:
            cancel.set()

    command = [sys.executable, "-c", leader, descendant, str(ready), str(trigger), str(survived), ending]
    if ending == "leader_exit":
        assert process.run_bounded(command, timeout=3, on_chunk=observe).returncode == 0
    else:
        error = process.ProcessCancelled if ending == "cancel" else subprocess.TimeoutExpired
        with pytest.raises(error):
            process.run_bounded(command, timeout=1, cancel_event=cancel, on_chunk=observe)
    assert ready.exists(), "the resistant descendant must be ready before the terminal condition"
    trigger.touch()
    deadline = time.monotonic() + 0.3
    while not survived.exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not survived.exists(), "an owned descendant performed work after run_bounded returned"
    assert all(child.poll() is not None for child in owned_processes)


@pytest.mark.regression
@pytest.mark.parametrize("fault", ["read", "projection"])
def test_sidecar_failure_stops_closed_pipe_provider_and_watchdog(tmp_path: Path, owned_processes, monkeypatch, fault):
    sidecar = tmp_path / "native-sidecar"
    if fault == "projection":
        def fail_projection(*args):
            raise OSError("planted sidecar projection failure")

        monkeypatch.setattr(process, "observe_sidecar", fail_projection)
    child = "\n".join([
        "import os,sys,time",
        "from pathlib import Path",
        "os.close(1); os.close(2)",
        "path=Path(sys.argv[1])",
        "path.mkdir() if sys.argv[2]=='read' else path.write_bytes(b'evidence')",
        "time.sleep(30)",
    ])
    expected = IsADirectoryError if fault == "read" else OSError
    with pytest.raises(expected):
        process.run_bounded([sys.executable, "-c", child, str(sidecar), fault], timeout=3, stdout_journal=tmp_path / "stdout.log", stderr_journal=tmp_path / "stderr.log", observed_files={"native": sidecar})
    assert sidecar.exists(), "the error must arise after the provider closes its output pipes"
    assert len(owned_processes) == 2
    assert all(child.poll() is not None for child in owned_processes), "sidecar failure left the provider or watchdog running"
