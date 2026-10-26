"""Independent emitted-byte and transaction oracles for durable-run admission."""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from lattice.run_supervisor import DurableRunSupervisor
from lattice.run_supervisor import _LocalExecution
from lattice.run_journal import DurableRunJournal
from lattice.contracts import RunUnitResultV1, RunAttemptFinishV1, RunArtifactCreateV1, RunProcessIdentityV2
from lattice.store import LatticeConflict
from test_durable_runs import _declaration, _start
from test_run_supervisor import _bound_fixture


def _log_endpoint(lattice_env, monkeypatch):
    """Registered production route, without lifespan, providers or sockets."""
    import lattice.server as server
    store = lattice_env["store"]
    supervisor = DurableRunSupervisor(store, journal_root=store.config.data_root / "log-fixture")
    monkeypatch.setattr(server, "_initialize_runtime", lambda config: (store, lattice_env["lattice"], None, supervisor, None, "fixture-token", None, None, None, {}, [], None))
    app = server.create_app(lattice_env["config"])
    log = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/api/v1/runs/{run_id}/attempts/{attempt_id}/logs/{stream}")
    inspect = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/api/v1/runs/{run_id}" and "GET" in route.methods)
    return supervisor, log, inspect


def _plant_log(store, run, attempt, chunks, channel="stdout", *, offset=0):
    """Plant exact independent receipts, without the admission producer oracle."""
    refs = [(store.blobs.put(raw), len(raw)) for raw in dict.fromkeys(chunks)]
    identities = {raw: ref for raw, ref in zip(dict.fromkeys(chunks), refs)}
    with store.transaction() as conn:
        project = conn.execute("SELECT project_id FROM runs WHERE run_id=?", (run,)).fetchone()[0]
        for raw in chunks:
            digest, size = identities[raw]
            store._event(conn, project_id=project, event_type=f"durable-run-attempt.{channel}-admitted", subject_type="durable-run-attempt", subject_id=attempt, payload={"contract_version": "RunStreamAdmission/1.0", "attempt_id": attempt, "channel": channel, "offset": offset, "byte_count": size, "content_ref": f"lattice:blob/{digest}"})
            offset += size


@pytest.mark.asyncio
async def test_log_endpoint_cas_io_is_off_loop(lattice_env, monkeypatch):
    store, run, attempt = _attempt(lattice_env)
    raw = bytes(range(256)) * 512
    _plant_log(store, run, attempt, [raw[:65536], raw[65536:]])
    _, endpoint, _ = _log_endpoint(lattice_env, monkeypatch)
    loop_thread = threading.get_ident()
    original = store.blobs.path_for
    def require_thread(digest):
        assert threading.get_ident() != loop_thread, "CAS I/O on HTTP event loop"
        return original(digest)
    monkeypatch.setattr(store.blobs, "path_for", require_thread)
    response = await endpoint(run, attempt, "stdout")
    assert response.media_type == "text/plain"
    assert response.headers["content-length"] == str(len(raw))
    assert b"".join([chunk async for chunk in response.body_iterator]) == raw


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["stdout", "stderr"])
async def test_log_endpoint_pins_pages_without_raw_tail_or_read_transaction(lattice_env, monkeypatch, channel):
    store, run, attempt = _attempt(lattice_env)
    chunks = [b"\x00\xfffirst", b"\r\nsecond\x80"] * 65
    _plant_log(store, run, attempt, chunks, channel)
    supervisor, endpoint, _ = _log_endpoint(lattice_env, monkeypatch)
    root = supervisor.journal_root / run / attempt
    root.mkdir(parents=True)
    (root / f"{channel}.log").write_bytes(b"unadmitted diagnostic bytes")
    def forbidden(*args, **kwargs):
        raise AssertionError("production log path materialized receipts or bytes")
    monkeypatch.setattr(store, "run_log_evidence", forbidden)
    monkeypatch.setattr(store.blobs, "get", forbidden)
    monkeypatch.setattr(supervisor, "open_attempt_log", forbidden)
    original_page, connect = store.run_log_receipt_page, store._connect
    pages, active = [], []
    @contextmanager
    def tracked_connect():
        with connect() as conn:
            active.append(conn)
            try:
                yield conn
            finally:
                active.remove(conn)
    def page(*args):
        value = original_page(*args)
        pages.append(len(value))
        assert not active
        return value
    monkeypatch.setattr(store, "_connect", tracked_connect)
    monkeypatch.setattr(store, "run_log_receipt_page", page)
    response = await endpoint(run, attempt, channel)
    late = b"late append is outside the immutable response extent"
    # This lands after the boundary but before pages two and three are fetched.
    _plant_log(store, run, attempt, [late], "stderr" if channel == "stdout" else "stdout")
    _plant_log(store, run, attempt, [late], channel, offset=sum(map(len, chunks)))
    result = []
    async for raw in response.body_iterator:
        assert not active, "a source SQLite read transaction survived into client output"
        result.append(raw)
        await asyncio.sleep(0)
    assert b"".join(result) == b"".join(chunks)
    assert response.headers["content-length"] == str(sum(map(len, chunks)))
    assert pages == [64, 64, 2, 0]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing", "corrupt", "oversized"])
@pytest.mark.parametrize("late", [False, True])
async def test_log_endpoint_required_cas_failure_never_finishes_successfully(lattice_env, tmp_path, monkeypatch, fault, late):
    store, run, attempt = _attempt(lattice_env)
    good, bad = b"verified prefix\x00\xff", b"required binary\x80\r\n"
    _plant_log(store, run, attempt, [good, bad] if late else [bad])
    _, endpoint, _ = _log_endpoint(lattice_env, monkeypatch)
    digest = hashlib.sha256(bad).hexdigest()
    original = store.blobs.path_for
    path = original(digest)
    if fault == "missing":
        monkeypatch.setattr(store.blobs, "path_for", lambda value: tmp_path / "absent-cas" if value == digest else original(value))
    else:
        path.read_bytes()
        path.write_bytes(b"x" * (1024 * 1024 if fault == "oversized" else len(bad)))
    if not late:
        with pytest.raises(LatticeConflict, match="CAS"):
            await endpoint(run, attempt, "stdout")
        return
    response = await endpoint(run, attempt, "stdout")
    messages = []
    async def send(message):
        messages.append(message)
    async def receive():
        raise AssertionError("ASGI 2.4 should use send-side disconnects")
    with pytest.raises(LatticeConflict, match="CAS"):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert messages[0]["status"] == 200
    assert response.headers["content-length"] == str(len(good) + len(bad))
    assert [m["body"] for m in messages if m["type"] == "http.response.body"] == [good]
    assert not any(m.get("more_body") is False for m in messages), "late CAS failure was presented as successful EOF"


@pytest.mark.asyncio
async def test_log_endpoint_distinguishes_empty_legacy_and_wrong_owner(lattice_env, monkeypatch):
    from lattice.store import LatticeNotFound
    store, run, attempt = _attempt(lattice_env)
    supervisor, endpoint, _ = _log_endpoint(lattice_env, monkeypatch)
    with pytest.raises(LatticeConflict, match="another run"):
        await endpoint("wrong-run", attempt, "stdout")
    with pytest.raises(LatticeNotFound):
        await endpoint(run, attempt, "stdout")
    root = supervisor.journal_root / run / attempt
    root.mkdir(parents=True)
    raw = bytes(range(256)) * 400
    path = root / "stdout.log"
    path.write_bytes(raw)
    response = await endpoint(run, attempt, "stdout")
    path.read_bytes()
    path.write_bytes(raw + b"not part of pinned legacy extent")
    assert b"".join([chunk async for chunk in response.body_iterator]) == raw
    assert response.headers["content-length"] == str(len(raw))
    # An explicitly admitted empty prefix must never borrow those diagnostic bytes.
    with store.transaction() as conn:
        store._event(conn, project_id=lattice_env["project"]["project_id"], event_type="durable-run-attempt.terminal-evidence", subject_type="durable-run-attempt", subject_id=attempt, payload={"contract_version": "RunTerminalEvidence/1.0", "stream_prefixes": {"stdout": 0, "stderr": 0}})
    empty = await endpoint(run, attempt, "stdout")
    assert empty.headers["content-length"] == "0"
    assert [chunk async for chunk in empty.body_iterator] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("late", [False, True])
async def test_log_endpoint_rejects_receipt_gaps_instead_of_truncation(lattice_env, monkeypatch, late):
    store, run, attempt = _attempt(lattice_env)
    prefix = [b"\x00\xffprefix"] * 65 if late else []
    if prefix:
        _plant_log(store, run, attempt, prefix)
    _plant_log(store, run, attempt, [b"gap"], offset=sum(map(len, prefix)) + 1)
    _, endpoint, _ = _log_endpoint(lattice_env, monkeypatch)
    if not late:
        with pytest.raises(LatticeConflict, match="gap"):
            await endpoint(run, attempt, "stdout")
        return
    response = await endpoint(run, attempt, "stdout")
    received = []
    with pytest.raises(LatticeConflict, match="gap"):
        async for raw in response.body_iterator:
            received.append(raw)
    assert received == prefix
    assert sum(map(len, received)) < int(response.headers["content-length"])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["disconnect", "cancel"])
async def test_log_endpoint_slow_cas_responsive_and_cleanup(lattice_env, monkeypatch, mode):
    import httpx
    import lattice.server as server
    store, run, attempt = _attempt(lattice_env)
    _plant_log(store, run, attempt, [bytes(range(256)) * 256] * 32)
    _, endpoint, _ = _log_endpoint(lattice_env, monkeypatch)
    app = server.create_app(lattice_env["config"])
    entered, release, settled = threading.Event(), threading.Event(), threading.Event()
    disconnect = asyncio.Event()
    read, calls = store.read_run_log_chunk, []
    def slow(receipt):
        calls.append(receipt["offset"])
        if len(calls) == 2:
            entered.set()
            assert release.wait(3), "test did not release its synthetic CAS blocker"
            try:
                return read(receipt)
            finally:
                settled.set()
        return read(receipt)
    monkeypatch.setattr(store, "read_run_log_chunk", slow)
    response = await endpoint(run, attempt, "stdout")
    sent = []
    async def send(message):
        sent.append(message)
    async def receive():
        await disconnect.wait()
        return {"type": "http.disconnect"}
    scope = {"type": "http", "asgi": {"spec_version": "2.0" if mode == "disconnect" else "2.4"}}
    serving = asyncio.create_task(response(scope, receive, send))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        # The real inspection endpoint completes while the log's CAS operation
        # is deliberately still blocked. This is causal responsiveness evidence,
        # rather than an unqualified elapsed-time comparison.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"authorization": "Bearer fixture-token"}) as client:
            started = time.monotonic()
            status = await asyncio.wait_for(client.get(f"/api/v1/runs/{run}"), 1)
            elapsed = time.monotonic() - started
        assert status.status_code == 200 and status.json()["run_id"] == run
        assert not serving.done() and not release.is_set()
        print("LOG_FOREGROUND_SAMPLE " + json.dumps({"mode": mode, "log_bytes": 2097152, "receipts": 32, "foreground_seconds": elapsed, "cas_still_blocked": True}), flush=True)
        if mode == "disconnect":
            disconnect.set()
        else:
            serving.cancel()
            await asyncio.sleep(0)
            serving.cancel()
        await asyncio.sleep(.02)
        assert not serving.done(), "cancellation detached an unsettled read"
        release.set()
        if mode == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(serving, 1)
        else:
            await asyncio.wait_for(serving, 1)
        assert settled.is_set()
        assert len(calls) == 2, "reader continued after disconnect/cancellation"
        assert not any(m.get("more_body") is False for m in sent)
    finally:
        release.set()
        disconnect.set()
        await asyncio.gather(serving, return_exceptions=True)


@pytest.mark.asyncio
async def test_log_endpoint_asgi_download_and_early_error_contract(lattice_env, tmp_path, monkeypatch):
    import httpx
    import lattice.server as server
    store, run, attempt = _attempt(lattice_env)
    raw = bytes(range(256)) * 512
    _plant_log(store, run, attempt, [raw[:65536], raw[65536:]])
    _log_endpoint(lattice_env, monkeypatch)
    app = server.create_app(lattice_env["config"])
    path = f"/api/v1/runs/{run}/attempts/{attempt}/logs/stdout"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"authorization": "Bearer fixture-token"}) as client:
        response = await client.get(path)
        assert response.status_code == 200 and response.content == raw
        assert response.headers["content-type"] == "text/plain; charset=utf-8"
        assert response.headers["content-length"] == str(len(raw))
        assert response.headers["content-disposition"].endswith(f"{attempt}-stdout.log")
        assert (await client.get(path.replace("stdout", "combined"))).status_code == 422
        assert (await client.get(path.replace(run, "wrong-run"))).status_code == 409
        assert (await client.get(path.replace(attempt, "missing-attempt"))).status_code == 404
        monkeypatch.setattr(store.blobs, "path_for", lambda digest: tmp_path / "missing-required-cas")
        failed = await client.get(path)
        assert failed.status_code == 409 and "CAS unavailable" in failed.json()["detail"]


@pytest.mark.asyncio
async def test_log_endpoint_memory_stays_bounded_as_receipts_grow(lattice_env, monkeypatch):
    import tracemalloc
    store = lattice_env["store"]
    samples = []
    for count in (16, 1024):
        declaration = _declaration(lattice_env, operation_key=f"memory-{count}")
        run = store.declare_run(declaration)["run_id"]
        store.admit_run(run)
        attempt = store.start_run_attempt(run, _start(f"memory-{count}"))["attempt_id"]
        raw = bytes(range(256)) * (4096 // count)
        _plant_log(store, run, attempt, [raw] * count)
        _, endpoint, _ = _log_endpoint(lattice_env, monkeypatch)
        page, sizes = store.run_log_receipt_page, []
        def observed(*args):
            value = page(*args)
            sizes.append(len(value))
            return value
        with monkeypatch.context() as patch:
            patch.setattr(store, "run_log_receipt_page", observed)
            tracemalloc.start()
            try:
                response = await endpoint(run, attempt, "stdout")
                digest, total, max_chunk = hashlib.sha256(), 0, 0
                async for chunk in response.body_iterator:
                    digest.update(chunk)
                    total += len(chunk)
                    max_chunk = max(max_chunk, len(chunk))
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
        assert total == 1024 * 1024
        assert digest.hexdigest() == hashlib.sha256(bytes(range(256)) * 4096).hexdigest()
        assert max(sizes) <= 64 and max_chunk <= 65536
        samples.append({"receipt_count": count, "bytes": total, "peak_traced_bytes": peak, "max_page": max(sizes), "max_chunk": max_chunk})
    print("LOG_MEMORY_SAMPLES " + json.dumps(samples), flush=True)
    assert samples[1]["peak_traced_bytes"] < samples[0]["peak_traced_bytes"] + 256 * 1024


def _lifecycle_run(lattice_env, tmp_path, body):
    """Bind the exact independent fixture bytes, including their changed closure."""
    from lattice.contracts import LocalFileBindingV1, LocalFileClosureV1
    declaration, work, _ = _bound_fixture(lattice_env, tmp_path, operation_key="lifecycle", behavior="normal")
    worker = work / "worker.py"
    worker.read_bytes()
    worker.write_text("import os, time\nevent=int(os.environ['LATTICE_RUN_EVENT_FD'])\n" + body)
    raw = worker.read_bytes()
    closure = LocalFileClosureV1(bindings=[LocalFileBindingV1(path=str(worker), content_sha256=hashlib.sha256(raw).hexdigest(), byte_count=len(raw))])
    declaration = declaration.model_copy(update={"code_closure": closure, "code_revision": closure.identity_sha256()})
    store = lattice_env["store"]
    run = store.declare_run(declaration)
    store.admit_run(run["run_id"])
    return run


def _completed_worker():
    from lattice.canonical import canonical_bytes
    events = []
    for key in ("one", "two", "three"):
        events.extend([{"event_type": "unit_started", "unit_key": key}, {"event_type": "unit_completed", "unit_key": key}])
    events.append({"event_type": "run_completed"})
    return "".join(f"os.write(event, {canonical_bytes(dict(event, contract_version='DurableRunWorkerEvent/1.0', sequence=n, monotonic_ns=n)) + bytes([10])!r})\n" for n, event in enumerate(events, 1))


@pytest.mark.asyncio
@pytest.mark.parametrize("sink", ["event_journal", "stream_commit"])
async def test_post_exit_admission_backlog_is_not_a_stuck_peer(lattice_env, tmp_path, monkeypatch, sink):
    """Exited worker + held real persistence, independently emitted frames/bytes."""
    import lattice.run_supervisor as runtime
    store = lattice_env["store"]
    raw = b"post-exit\x00\xff\n"
    run = _lifecycle_run(lattice_env, tmp_path, f"os.write(1, {raw!r})\n" + _completed_worker())
    monkeypatch.setattr(runtime, "WORKER_DRAIN_SECONDS", .08)
    entered, release = threading.Event(), threading.Event()
    target = DurableRunJournal if sink == "event_journal" else store
    name = "append_event" if sink == "event_journal" else "record_run_stream_chunk"
    original = getattr(target, name)
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5), "fixture persistence was not released"
        return original(*args, **kwargs)
    monkeypatch.setattr(target, name, held)
    supervisor = DurableRunSupervisor(store, journal_root=tmp_path / "journals")
    attempt = await supervisor.execute(run["run_id"], "post-exit-backlog")
    execution = supervisor._executions[attempt["attempt_id"]]
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        assert await asyncio.to_thread(execution.process.wait, timeout=3) == 0
        # Hold admission past the peer grace: cancellation must not lose the
        # next frames after its in-flight write settles.
        await asyncio.sleep(.2)
        assert not execution.task.done()
    finally:
        release.set()
        terminal = await asyncio.wait_for(supervisor.wait(attempt["attempt_id"]), 10)
    print("POST_EXIT_BACKLOG " + json.dumps({"sink": sink, "terminal": terminal}, sort_keys=True), flush=True)
    assert terminal["state"] == "completed", terminal
    assert [unit["state"] for unit in store.get_run(run["run_id"])["units"]] == ["completed"] * 3
    evidence = store.run_attempt_evidence_boundary(attempt["attempt_id"])
    assert evidence["worker_event_count"] == 7 and evidence["terminal_event_ref"]
    assert evidence["stream_prefixes"] == {"stdout": len(raw), "stderr": 0}
    assert b"".join(store._evidence_bytes(item) for item in store.run_log_evidence(attempt["attempt_id"], "stdout")["chunks"]) == raw
    diagnostics = json.loads((execution.journal.root / "terminal.json").read_bytes())["lifecycle_diagnostics"]
    assert diagnostics["returncode"] == 0 and diagnostics["last_worker_sequence"] == 7
    assert not diagnostics["worker_event_in_flight"]
    assert all(state["phase"] == "eof" for state in diagnostics["pipes"].values())
    print("POST_EXIT_DIAGNOSTICS " + json.dumps(diagnostics, sort_keys=True), flush=True)


@pytest.mark.asyncio
async def test_post_exit_peer_wait_budget_does_not_reset_on_small_reads(tmp_path, monkeypatch):
    import lattice.run_supervisor as runtime
    monkeypatch.setattr(runtime, "WORKER_DRAIN_SECONDS", .12)
    execution = _LocalExecution(run_id="fixture", attempt_id="fixture", bound=None, process=SimpleNamespace(returncode=0), event_read_fd=-1, control_write_fd=-1, journal=None)
    supervisor = DurableRunSupervisor(SimpleNamespace(config=SimpleNamespace(data_root=tmp_path)))
    supervisor._mark_process_exited(execution)
    read_fd, write_fd = os.pipe()
    received = []
    async def emit():
        while True:
            await asyncio.sleep(.025)
            os.write(write_fd, b"x")
    async def consume():
        while True:
            received.append(await supervisor._read_channel(execution, "stdout", read_fd, 1))
    writer = asyncio.create_task(emit())
    try:
        with pytest.raises(runtime.DurableRunProtocolError, match="drain deadline.*stdout"):
            await asyncio.wait_for(consume(), 1)
        state = execution.pipe_drains["stdout"]
        assert 1 < len(received) < 10
        assert b"".join(received) == b"x" * len(received)
        assert state.phase == "timed_out" and state.wait_seconds >= .12
        print("PEER_WAIT_BUDGET " + json.dumps({"reads": state.reads, "wait_seconds": state.wait_seconds}), flush=True)
    finally:
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["terminal", "event_eof", "open_drain"])
async def test_lifecycle_bounds_terminal_eof_and_open_drain(lattice_env, tmp_path, monkeypatch, mode):
    import lattice.run_supervisor as runtime
    monkeypatch.setattr(runtime, "WORKER_EXIT_SECONDS", .15, raising=False)
    monkeypatch.setattr(runtime, "WORKER_DRAIN_SECONDS", .15, raising=False)
    body = _completed_worker() if mode != "event_eof" else "os.close(event)\n"
    if mode != "open_drain":
        body += "time.sleep(60)\n"
    else:
        acknowledged = tmp_path / "terminal-admitted"
        body += f"while not os.path.exists({str(acknowledged)!r}): time.sleep(.002)\n"
        admit = lattice_env["store"].record_run_terminal_event
        def acknowledge(*args, **kwargs):
            result = admit(*args, **kwargs)
            acknowledged.write_bytes(b"admitted")
            return result
        monkeypatch.setattr(lattice_env["store"], "record_run_terminal_event", acknowledge)
    run = _lifecycle_run(lattice_env, tmp_path, body)
    supervisor = DurableRunSupervisor(lattice_env["store"], journal_root=tmp_path / "journals")
    held = None
    original = runtime.spawn_local_run
    if mode == "open_drain":
        read_fd, held = os.pipe()
        def spawn(*args, **kwargs):
            result = original(*args, **kwargs)
            result.process.stdout.close()
            result.process.stdout = os.fdopen(read_fd, "rb", buffering=0)
            return result
        monkeypatch.setattr(runtime, "spawn_local_run", spawn)
    attempt = await supervisor.execute(run["run_id"], "lifecycle-bounded")
    execution = supervisor._executions[attempt["attempt_id"]]
    try:
        terminal = await asyncio.wait_for(asyncio.shield(supervisor.wait(attempt["attempt_id"])), 3)
        assert execution.process.poll() is not None
        assert terminal["state"] == "failed"
        assert terminal["failure_kind"] == "worker_protocol"
        assert ("drain" if mode == "open_drain" else "exit") in terminal["error_text"]
        assert not [task for task in asyncio.all_tasks() if task.get_name().startswith(f"run-child:{attempt['attempt_id']}")]
        if mode != "event_eof":
            assert all(unit["state"] == "completed" for unit in lattice_env["store"].get_run(run["run_id"])["units"])
    finally:
        if held is not None:
            os.close(held)
        if execution.process.poll() is None:
            await supervisor._terminate(execution)
        await asyncio.gather(execution.task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["terminal_store", "projection"])
async def test_public_lifecycle_failure_is_visible_and_reconcilable(lattice_env, tmp_path, monkeypatch, fault):
    store = lattice_env["store"]
    run = _lifecycle_run(lattice_env, tmp_path, _completed_worker())
    finish = store.finish_run_attempt
    projection = DurableRunJournal.finish
    reached = asyncio.Event()
    def fail(*args, **kwargs):
        reached.set()
        raise OSError(f"planted {fault} failure")
    monkeypatch.setattr(store if fault == "terminal_store" else DurableRunJournal, "finish_run_attempt" if fault == "terminal_store" else "finish", fail)
    supervisor = DurableRunSupervisor(store, journal_root=tmp_path / "journals")
    attempt = await supervisor.execute(run["run_id"], "public-failure")
    await asyncio.wait_for(reached.wait(), 3)
    # Let the task and its callback finish without keeping their private handle.
    await asyncio.sleep(.05)
    status = await supervisor.wait(attempt["attempt_id"])
    assert status["supervision_failure"]["error_text"] == f"OSError: planted {fault} failure"
    assert status["state"] == ("running" if fault == "terminal_store" else "completed")
    assert supervisor.inspect_run(run["run_id"])["attempts"][-1]["supervision_failure"] == status["supervision_failure"]
    # Exercise the actual registered inspection endpoint without starting a
    # service, entering lifespan, binding a socket, or touching provider state.
    import lattice.server as server
    monkeypatch.setattr(server, "_initialize_runtime", lambda config: (store, lattice_env["lattice"], None, supervisor, None, "fixture-token", None, None, None, {}, [], None))
    app = server.create_app(lattice_env["config"])
    route = next(route for route in app.routes if getattr(route, "path", None) == "/api/v1/runs/{run_id}" and "GET" in route.methods)
    assert (await route.endpoint(run["run_id"]))["attempts"][-1]["supervision_failure"] == status["supervision_failure"]
    duplicate = await supervisor.execute(run["run_id"], "public-failure")
    assert duplicate["supervision_failure"] == status["supervision_failure"]
    stopped = await supervisor.request_stop(run["run_id"])
    assert stopped["attempts"][-1]["supervision_failure"]["error_text"] == status["supervision_failure"]["error_text"]
    monkeypatch.setattr(store, "finish_run_attempt", finish)
    monkeypatch.setattr(DurableRunJournal, "finish", projection)
    reconciled = await supervisor.reconcile_attempt(attempt["attempt_id"])
    assert reconciled["state"] == ("failed" if fault == "terminal_store" else "completed")
    assert all(unit["state"] == "completed" for unit in store.get_run(run["run_id"])["units"])
    assert reconciled["supervision_failure"]["reconciliation"] == "resolved"
    if fault == "projection":
        assert reconciled["supervision_failure"]["projection_status"] == "repaired"
        receipt = json.loads((supervisor.journal_root / run["run_id"] / attempt["attempt_id"] / "terminal.json").read_bytes())
        assert receipt["state"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("held_drain", [False, True])
async def test_setup_failure_uses_guardian_ownership_after_gate_closes(lattice_env, tmp_path, monkeypatch, held_drain):
    import lattice.run_supervisor as runtime
    store = lattice_env["store"]
    run = _lifecycle_run(lattice_env, tmp_path, "raise AssertionError('gate must remain closed')\n")
    original = runtime.spawn_local_run
    spawned = []
    held = None
    if held_drain:
        read_fd, held = os.pipe()
        monkeypatch.setattr(runtime, "WORKER_DRAIN_SECONDS", .1)
    def spawn(*args, **kwargs):
        result = original(*args, **kwargs)
        if held_drain:
            result.process.stdout.close()
            result.process.stdout = os.fdopen(read_fd, "rb", buffering=0)
        spawned.append(result)
        return result
    monkeypatch.setattr(runtime, "spawn_local_run", spawn)
    def fail(*args, **kwargs):
        raise OSError("planted controls failure")
    monkeypatch.setattr(store, "record_run_process", fail)
    prove = runtime.terminate_and_prove_process_dead
    def after_exit(identity):
        spawned[0].process.wait(timeout=2)
        return prove(identity)
    monkeypatch.setattr(runtime, "terminate_and_prove_process_dead", after_exit)
    def forbidden(*args, **kwargs):
        raise AssertionError("numeric group signal after setup gate closure")
    monkeypatch.setattr(runtime, "terminate_process_group", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    supervisor = DurableRunSupervisor(store, journal_root=tmp_path / "journals")
    try:
        result = await asyncio.wait_for(supervisor.execute(run["run_id"], "setup-ownership"), 3)
        assert result["state"] == "failed" and "planted controls failure" in result["error_text"]
        if held_drain:
            assert "setup drain deadline" in result["error_text"]
    finally:
        if held is not None:
            os.close(held)
        if spawned:
            spawned[0].process.wait(timeout=2)


@pytest.mark.asyncio
async def test_drain_cancellation_settles_an_inflight_admission(lattice_env, tmp_path, monkeypatch):
    store, run, attempt = _attempt(lattice_env)
    journal = DurableRunJournal(tmp_path / "journal", {})
    supervisor = DurableRunSupervisor(store)
    execution = _LocalExecution(run_id=run, attempt_id=attempt, bound=None, process=None, event_read_fd=-1, control_write_fd=-1, journal=journal)
    entered, release = threading.Event(), threading.Event()
    append = store.record_run_stream_chunk
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(3), "fixture admission was never released"
        return append(*args, **kwargs)
    monkeypatch.setattr(store, "record_run_stream_chunk", blocked)
    read_fd, write_fd = os.pipe()
    raw = b"committing\x00\xff"
    os.write(write_fd, raw)
    os.close(write_fd)
    pump = asyncio.create_task(supervisor._pump_stream(execution, "stdout", os.fdopen(read_fd, "rb", buffering=0), journal.write_stdout))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        pump.cancel()
        await asyncio.sleep(.03)
        assert not pump.done(), "drain returned while its admission could still commit"
        pump.cancel()
        await asyncio.sleep(.03)
        assert not pump.done(), "repeated cancellation detached an admission"
    finally:
        release.set()
        await asyncio.gather(pump, return_exceptions=True)
        journal.close()
    assert store.run_attempt_evidence_boundary(attempt)["stream_prefixes"]["stdout"] == len(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize("sink", ["cas", "commit", "raw", "lost_ack", "terminal_store"])
async def test_evidence_failure_bounds_an_indefinitely_writing_owned_worker(lattice_env, tmp_path, monkeypatch, sink):
    from lattice.contracts import LocalFileBindingV1, LocalFileClosureV1

    store = lattice_env["store"]
    declaration, work, _ = _bound_fixture(lattice_env, tmp_path, operation_key="sink-failure", behavior="normal")
    worker = work / "worker.py"
    worker.read_bytes()  # The fixture copy is owned by this test.
    worker.write_text("import os, signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                      "event=int(os.environ['LATTICE_RUN_EVENT_FD'])\n"
                      "os.write(event, b'{\"contract_version\":\"DurableRunWorkerEvent/1.0\",\"event_type\":\"unit_started\",\"monotonic_ns\":1,\"sequence\":1,\"unit_key\":\"one\"}\\n')\n"
                      "os.write(event, b'{\"contract_version\":\"DurableRunWorkerEvent/1.0\",\"event_type\":\"unit_completed\",\"monotonic_ns\":2,\"sequence\":2,\"unit_key\":\"one\"}\\n')\n"
                      f"while not os.path.exists({str(work / 'one-admitted')!r}): time.sleep(.002)\n"
                      "while True:\n os.write(1, b'\\x00\\xff'+b'x'*609)\n os.write(2, b'diagnostic\\x80')\n"
                      f" if os.path.exists({str(work / 'sink-failed')!r}):\n"
                      "  os.write(event, b'{\"contract_version\":\"DurableRunWorkerEvent/1.0\",\"event_type\":\"progress\",\"message\":\"after-failure\",\"monotonic_ns\":3,\"sequence\":3}\\n')\n"
                      " time.sleep(.002)\n")
    raw = worker.read_bytes()
    closure = LocalFileClosureV1(bindings=[LocalFileBindingV1(path=str(worker), content_sha256=hashlib.sha256(raw).hexdigest(), byte_count=len(raw))])
    declaration = declaration.model_copy(update={"code_closure": closure, "code_revision": closure.identity_sha256()})
    run = store.declare_run(declaration)
    store.admit_run(run["run_id"])
    supervisor = DurableRunSupervisor(store, journal_root=tmp_path / "journals")
    await supervisor.start()
    reached = asyncio.Event()
    loop = asyncio.get_running_loop()
    calls = []
    put, append, raw_write = store.blobs.put, store.record_run_stream_chunk, DurableRunJournal.write_stdout
    finish_unit = store.finish_run_unit

    def admit_unit(*args, **kwargs):
        result = finish_unit(*args, **kwargs)
        (work / "one-admitted").write_bytes(b"go")
        return result

    def fail():
        (work / "sink-failed").write_bytes(b"failed")
        loop.call_soon_threadsafe(reached.set)
        raise OSError("planted evidence sink failure")

    def publish(value):
        if sink == "cas" and value.startswith(b"\x00\xff"):
            calls.append("cas")
            fail()
        return put(value)

    def commit(attempt, channel, offset, evidence):
        if sink in {"commit", "lost_ack", "terminal_store"} and channel == "stdout":
            calls.append("commit")
            if sink == "lost_ack":
                append(attempt, channel, offset, evidence)
            fail()
        return append(attempt, channel, offset, evidence)

    def write(journal, value):
        if sink == "raw":
            calls.append("raw")
            fail()
        return raw_write(journal, value)

    monkeypatch.setattr(store.blobs, "put", publish)
    monkeypatch.setattr(store, "record_run_stream_chunk", commit)
    monkeypatch.setattr(DurableRunJournal, "write_stdout", write)
    monkeypatch.setattr(store, "finish_run_unit", admit_unit)
    if sink == "terminal_store":
        def unavailable(*args, **kwargs):
            raise OSError("terminal store remains unavailable")
        monkeypatch.setattr(store, "finish_run_attempt", unavailable)
    attempt = await supervisor.execute(run["run_id"], "indefinite-worker")
    execution = supervisor._executions[attempt["attempt_id"]]
    try:
        await asyncio.wait_for(reached.wait(), 5)
        assert execution.process.poll() is None  # Failure reached a live, unbounded worker.
        if sink == "terminal_store":
            with pytest.raises(OSError, match="terminal store remains unavailable"):
                await asyncio.wait_for(asyncio.shield(execution.task), 6)
            assert execution.process.poll() is not None
            assert store.get_run_attempt(attempt["attempt_id"])["state"] == "running"
            root = supervisor.journal_root / run["run_id"] / attempt["attempt_id"]
            assert not (root / "terminal.json").exists()
            assert (root / "stdout.log").stat().st_size > 0
            return
        terminal = await asyncio.wait_for(asyncio.shield(execution.task), 6)
        assert execution.process.poll() is not None
        assert terminal["state"] == "failed" and terminal["failure_kind"] == "worker_protocol"
        assert "planted evidence sink failure" in terminal["error_text"]
        assert calls == [sink if sink in {"cas", "raw"} else "commit"]
        assert store.get_run_unit(run["run_id"], "one")["state"] == "completed"
        assert len(_events(store, attempt["attempt_id"], "durable-run-attempt.worker-admitted")) == 2
        assert not _events(store, attempt["attempt_id"], "durable-run.progress")
        boundary = store.run_attempt_evidence_boundary(attempt["attempt_id"])
        stdout = boundary["stream_prefixes"]["stdout"]
        assert (stdout > 0) == (sink == "lost_ack")
        root = supervisor.journal_root / run["run_id"] / attempt["attempt_id"]
        if sink != "raw":
            assert (root / "stdout.log").stat().st_size > stdout
        assert (root / "stderr.log").stat().st_size > 0
        receipt = json.loads((root / "terminal.json").read_bytes())
        assert receipt["stdout_bytes"] == stdout
        assert b'"after-failure"' in (root / "events.jsonl").read_bytes()
        if sink == "raw":
            assert receipt["unretained_stdout_bytes"] > 0
        else:
            assert receipt["unretained_stdout_bytes"] == 0
    finally:
        if execution.process.poll() is None:
            await supervisor._terminate(execution)
        await asyncio.gather(execution.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_failure_termination_never_signals_a_reused_guardian_group(lattice_env, tmp_path, monkeypatch):
    import lattice.run_supervisor as runtime

    lock = tmp_path / "free-process.lock"
    lock.write_bytes(b"nonce\n")
    identity = SimpleNamespace(lock_path=str(lock), nonce="nonce", process_group_id=424242)

    def forbidden(*args):
        raise AssertionError("stale numeric process identity received a signal")

    def wait(*, timeout):
        assert timeout == 2
        raise subprocess.TimeoutExpired("unproven-worker", timeout)

    monkeypatch.setattr(os, "killpg", forbidden)
    monkeypatch.setattr(runtime, "terminate_process_group", forbidden)
    execution = _LocalExecution(run_id="run", attempt_id="attempt", bound=None, process=SimpleNamespace(wait=wait), event_read_fd=-1, control_write_fd=-1, journal=None, process_identity=identity)
    with pytest.raises(subprocess.TimeoutExpired):
        await DurableRunSupervisor(lattice_env["store"])._terminate(execution)


@pytest.mark.asyncio
async def test_noisy_stream_cost_sample(lattice_env, tmp_path, monkeypatch):
    """One finite sample: real CAS barriers/SQLite plus competing foreground writes."""
    import lattice.blob_store as blobs

    store, run, attempt = _attempt(lattice_env)
    supervisor = DurableRunSupervisor(store, journal_root=tmp_path / "journals")
    execution = _LocalExecution(run_id=run, attempt_id=attempt, bound=None, process=None, event_read_fd=-1, control_write_fd=-1, journal=None)
    records = [index.to_bytes(4, "big") + bytes(range(256)) * 2 + b"\xff" * 95 for index in range(96)]
    expected = b"".join(records)
    counts = {"file_syncs": 0, "directory_syncs": 0, "transactions": 0}
    original_file, original_directory, transaction = blobs.sync_file, blobs.sync_directory, store.transaction
    lock = threading.Lock()

    def file_sync(descriptor):
        with lock:
            counts["file_syncs"] += 1
        return original_file(descriptor)

    def directory_sync(path):
        with lock:
            counts["directory_syncs"] += 1
        return original_directory(path)

    @contextmanager
    def counted_transaction():
        with transaction() as conn:
            with lock:
                counts["transactions"] += 1
            yield conn

    monkeypatch.setattr(blobs, "sync_file", file_sync)
    monkeypatch.setattr(blobs, "sync_directory", directory_sync)
    monkeypatch.setattr(store, "transaction", counted_transaction)
    before = len(list(store.blobs.root.glob("*/*")))
    read_fd, write_fd = os.pipe()
    received, foreground = [], []
    original_read = os.read
    reads = []

    def small_read(descriptor, size):
        value = original_read(descriptor, min(size, 611) if descriptor == read_fd else size)
        if descriptor == read_fd and value:
            reads.append(len(value))
        return value

    monkeypatch.setattr(os, "read", small_read)

    def emit():
        try:
            for record in records:
                os.write(write_fd, record)
                time.sleep(.001)
        finally:
            os.close(write_fd)

    async def compete():
        for index in range(12):
            started = time.monotonic()
            await asyncio.to_thread(store.record_run_progress_event, attempt, kind="progress", payload={"sample": index})
            foreground.append(time.monotonic() - started)
            await asyncio.sleep(.004)

    started = time.monotonic()
    await asyncio.gather(asyncio.to_thread(emit), supervisor._pump_stream(execution, "stdout", os.fdopen(read_fd, "rb", buffering=0), received.append), compete())
    elapsed = time.monotonic() - started
    receipts = _events(store, attempt, "durable-run-attempt.stdout-admitted")
    assert b"".join(received) == expected
    assert b"".join(store.blobs.get(item["content_ref"].split("/")[-1]) for item in receipts) == expected
    assert [item["offset"] for item in receipts] == [sum(item["byte_count"] for item in receipts[:index]) for index in range(len(receipts))]
    result = {**counts, "pipe_reads": len(reads), "read_cap_bytes": 611, "stream_transactions": len(receipts), "objects_added": len(list(store.blobs.root.glob("*/*"))) - before, "input_records": len(records), "record_bytes": len(records[0]), "bytes": len(expected), "elapsed_seconds": elapsed, "foreground_writes": len(foreground), "foreground_median_seconds": sorted(foreground)[len(foreground)//2], "foreground_max_seconds": max(foreground)}
    print("STREAM_COST_SAMPLE " + json.dumps(result, sort_keys=True))
    # This budget rejects per-read admission on the planted tiny-read source.
    # It is deliberately loose about scheduler/disk timing, never a latency SLA.
    assert len(receipts) <= 12
    assert counts["transactions"] == len(receipts) + 12
    assert counts["file_syncs"] == len(receipts)
    assert counts["directory_syncs"] <= 3 * len(receipts)
    assert result["objects_added"] == len(receipts)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["semantic", "stop", "eof"])
async def test_stream_batches_flush_before_boundary_reaction(lattice_env, tmp_path, monkeypatch, boundary):
    import lattice.run_supervisor as runtime

    store, run, attempt = _attempt(lattice_env)
    # Only a boundary can flush this sample: neither size nor timer is reached.
    monkeypatch.setattr(runtime, "STREAM_BATCH_SECONDS", 60)
    journal = DurableRunJournal(tmp_path / "attempt-journal", {})
    event_read, event_write = os.pipe()
    read_fd, write_fd = os.pipe()
    execution = _LocalExecution(run_id=run, attempt_id=attempt, bound=None, process=SimpleNamespace(poll=lambda: None), event_read_fd=event_read, control_write_fd=-1, journal=journal)
    supervisor = DurableRunSupervisor(store, journal_root=tmp_path / "journals")
    supervisor._executions[attempt] = execution
    raw = b"\x00\xffpending-at-boundary\x80"
    reaction = []

    async def await_buffer():
        while not execution.stream_buffers["stdout"].pending:
            await asyncio.sleep(.001)

    def check_reaction(*args, **kwargs):
        admitted = store.run_log_evidence(attempt, "stdout")
        assert admitted["admitted_bytes"] == len(raw)
        assert store.blobs.get(admitted["chunks"][0]["content_ref"].split("/")[-1]) == raw
        reaction.append(boundary)

    async def terminate(_execution):
        check_reaction()

    pump = asyncio.create_task(supervisor._pump_stream(execution, "stdout", os.fdopen(read_fd, "rb", buffering=0), journal.write_stdout))
    try:
        os.write(write_fd, raw)
        await asyncio.wait_for(await_buffer(), 3)
        assert store.run_log_evidence(attempt, "stdout")["admitted_bytes"] == 0
        if boundary == "semantic":
            append = store.record_run_progress_event

            def progress(*args, **kwargs):
                check_reaction()
                return append(*args, **kwargs)

            monkeypatch.setattr(store, "record_run_progress_event", progress)
            event = json.dumps({"contract_version": "DurableRunWorkerEvent/1.0", "event_type": "progress", "sequence": 1, "monotonic_ns": 1, "message": "boundary"}, sort_keys=True, separators=(",", ":")).encode() + b"\n"
            os.write(event_write, event)
            os.close(event_write)
            event_write = -1
            await asyncio.wait_for(supervisor._read_worker_events(execution), 3)
            event_read = -1
        elif boundary == "stop":
            monkeypatch.setattr(supervisor, "_signal_stop", check_reaction)
            monkeypatch.setattr(supervisor, "_terminate", terminate)
            await asyncio.wait_for(supervisor.request_stop(run), 3)
        else:
            os.close(write_fd)
            write_fd = -1
            await asyncio.wait_for(asyncio.shield(pump), 3)
            check_reaction()
        assert reaction
        assert len(_events(store, attempt, "durable-run-attempt.stdout-admitted")) == 1
    finally:
        if write_fd != -1:
            os.close(write_fd)
        if event_write != -1:
            os.close(event_write)
        if event_read != -1:
            os.close(event_read)
        await pump
        journal.close()


@pytest.mark.asyncio
async def test_batch_timer_flushes_open_pipe_and_status_never_promotes_pending_bytes(lattice_env, tmp_path, monkeypatch):
    store, run, attempt = _attempt(lattice_env)
    root = tmp_path / "journals"
    journal = DurableRunJournal(root / run / attempt, {})
    execution = _LocalExecution(run_id=run, attempt_id=attempt, bound=None, process=None, event_read_fd=-1, control_write_fd=-1, journal=journal)
    supervisor = DurableRunSupervisor(store, journal_root=root)
    supervisor._executions[attempt] = execution
    read_fd, write_fd = os.pipe()
    entered, release = threading.Event(), threading.Event()
    commit = store.record_run_stream_chunk
    raw = b"\xff\x00timer-only"
    started = time.monotonic()
    elapsed = []

    def pause(*args):
        elapsed.append(time.monotonic() - started)
        entered.set()
        if not release.wait(3):
            raise TimeoutError("test admission gate expired")
        return commit(*args)

    monkeypatch.setattr(store, "record_run_stream_chunk", pause)
    pump = asyncio.create_task(supervisor._pump_stream(execution, "stdout", os.fdopen(read_fd, "rb", buffering=0), journal.write_stdout))
    try:
        os.write(write_fd, raw)
        # The writer deliberately leaves its descriptor open. EOF cannot help.
        assert await asyncio.to_thread(entered.wait, 3)
        assert elapsed[0] >= .04
        status = supervisor.attempt_log_status(run, attempt, "stdout")
        assert status["admitted_bytes"] == 0 and status["buffered_bytes"] == len(raw)
        assert status["raw_bytes"] == len(raw)
        release.set()
        os.close(write_fd)
        write_fd = -1
        await asyncio.wait_for(asyncio.shield(pump), 3)
        status = supervisor.attempt_log_status(run, attempt, "stdout")
        assert status["admitted_bytes"] == len(raw) and status["buffered_bytes"] == 0
        assert execution.stream_prefixes["stdout"] == len(raw)
    finally:
        release.set()
        if write_fd != -1:
            os.close(write_fd)
        await pump
        journal.close()


def _events(store, attempt_id, kind):
    with store._connect() as conn:
        return [json.loads(row[0]) for row in conn.execute(
            "SELECT payload_json FROM events WHERE subject_type='durable-run-attempt' AND subject_id=? AND event_type=? ORDER BY seq",
            (attempt_id, kind),
        )]


@pytest.mark.asyncio
async def test_exact_frames_and_initial_controls_are_admitted_before_dispatch(lattice_env, tmp_path, monkeypatch):
    import lattice.run_supervisor as runtime

    store = lattice_env["store"]
    declaration, _, release = _bound_fixture(lattice_env, tmp_path, operation_key="evidence-oracle", behavior="normal")
    run = store.declare_run(declaration)
    store.admit_run(run["run_id"])
    release.write_bytes(b"go")
    original_release = runtime.release_local_run

    def check_gate(spawned):
        bound = _events(store, spawned.process_identity.attempt_id, "durable-run-attempt.process-bound")
        assert {item["name"] for item in bound[0]["controls"]} == {
            "launch.json", "unit-plan.json", "target-environment.json", "process.json",
        }
        original_release(spawned)

    monkeypatch.setattr(runtime, "release_local_run", check_gate)
    supervisor = DurableRunSupervisor(store, journal_root=tmp_path / "journals")
    await supervisor.start()
    attempt = await supervisor.execute(run["run_id"], "evidence-oracle-attempt")
    terminal = await supervisor.wait(attempt["attempt_id"])
    assert terminal["state"] == "completed", terminal
    root = supervisor.journal_root / run["run_id"] / attempt["attempt_id"]
    frames = (root / "events.jsonl").read_bytes().splitlines(keepends=True)
    receipts = _events(store, attempt["attempt_id"], "durable-run-attempt.worker-admitted")
    assert len(receipts) == len(frames) > 0
    offset = 0
    for ordinal, (receipt, frame) in enumerate(zip(receipts, frames), 1):
        assert receipt["worker_sequence"] == ordinal
        assert receipt["raw_offset"] == offset
        assert receipt["byte_count"] == len(frame)
        assert store.blobs.get(receipt["content_ref"].split("/")[-1]) == frame
        offset += len(frame)
    for channel in ("stdout", "stderr"):
        chunks = _events(store, attempt["attempt_id"], f"durable-run-attempt.{channel}-admitted")
        assert b"".join(store.blobs.get(item["content_ref"].split("/")[-1]) for item in chunks) == (root / f"{channel}.log").read_bytes()
    binding = _events(store, attempt["attempt_id"], "durable-run-attempt.process-bound")[0]
    for item in binding["controls"]:
        raw = (root / item["name"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == item["content_ref"].split("/")[-1]
        assert len(raw) == item["byte_count"]
    store.record_run_process(attempt["attempt_id"], RunProcessIdentityV2.model_validate(binding["process_identity"]), controls=binding["controls"])
    assert len(_events(store, attempt["attempt_id"], "durable-run-attempt.process-bound")) == 1
    referenced = store.referenced_blob_digests()
    assert all(item["content_ref"].split("/")[-1] in referenced for item in receipts + binding["controls"])
    original = (root / "stdout.log").read_bytes()
    tail = b"\xffunaccepted-tail\x00"
    with (root / "stdout.log").open("ab") as handle:
        handle.write(tail)
    status = supervisor.attempt_log_status(run["run_id"], attempt["attempt_id"], "stdout")
    assert status["diagnostic_offset"] == len(original)
    assert status["raw_bytes"] == len(original) + len(tail)
    descriptor, size = supervisor.open_attempt_log(run["run_id"], attempt["attempt_id"], "stdout")
    with os.fdopen(descriptor, "rb") as handle:
        assert handle.read() == original and size == len(original)
    descriptor, size = supervisor.open_attempt_log(run["run_id"], attempt["attempt_id"], "stdout", diagnostic=True)
    with os.fdopen(descriptor, "rb") as handle:
        assert handle.read(size) == original + tail
    missing_projection = DurableRunSupervisor(store, journal_root=tmp_path / "missing-projections")
    descriptor, size = missing_projection.open_attempt_log(run["run_id"], attempt["attempt_id"], "stdout")
    with os.fdopen(descriptor, "rb") as handle:
        assert handle.read() == original and size == len(original)


def _attempt(env):
    store = env["store"]
    run = store.declare_run(_declaration(env))
    store.admit_run(run["run_id"])
    attempt = store.start_run_attempt(run["run_id"], _start("exact-frame-test"))
    return store, run["run_id"], attempt["attempt_id"]


def _frame(store, attempt_id, sequence, event_type, **fields):
    # Independent emitter: do not generate expected bytes from admission code.
    raw = json.dumps({"contract_version": "DurableRunWorkerEvent/1.0", "sequence": sequence, "monotonic_ns": 123, "event_type": event_type, **fields}, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    digest = hashlib.sha256(raw).hexdigest()
    assert store.blobs.put(raw) == digest
    return {"attempt_id": attempt_id, "worker_sequence": sequence, "raw_offset": (sequence - 1) * (1024 * 1024 + 2), "content_ref": f"lattice:blob/{digest}", "byte_count": len(raw)}


def test_semantic_commit_and_frame_are_atomic_and_retry_returns_original(lattice_env, monkeypatch):
    store, run, attempt = _attempt(lattice_env)
    evidence = _frame(store, attempt, 1, "unit_started", unit_key="one")
    assert _events(store, attempt, "durable-run-attempt.worker-admitted") == []
    assert store.get_run_unit(run, "one")["state"] == "eligible"
    original = store._event

    def fail_before_commit(conn, **kwargs):
        result = original(conn, **kwargs)
        if kwargs["event_type"] == "durable-run-attempt.worker-admitted":
            raise RuntimeError("interrupted after evidence insert before commit")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(store, "_event", fail_before_commit)
        with pytest.raises(RuntimeError, match="interrupted"):
            store.start_run_unit(run, attempt, "one", evidence=evidence)
    assert store.get_run_unit(run, "one")["state"] == "eligible"
    assert not _events(store, attempt, "durable-run-attempt.worker-admitted")
    transaction = store.transaction

    @contextmanager
    def fail_after_commit():
        with transaction() as conn:
            yield conn
        raise RuntimeError("interrupted after commit before reply")

    with monkeypatch.context() as patch:
        patch.setattr(store, "transaction", fail_after_commit)
        with pytest.raises(RuntimeError, match="after commit"):
            store.start_run_unit(run, attempt, "one", evidence=evidence)
    receipts = _events(store, attempt, "durable-run-attempt.worker-admitted")
    assert len(receipts) == 1
    first = receipts[0]["result"]
    complete = _frame(store, attempt, 2, "unit_completed", unit_key="one", output_sha256=None)
    store.finish_run_unit(run, attempt, "one", RunUnitResultV1(state="completed"), evidence=complete)
    assert store.start_run_unit(run, attempt, "one", evidence=evidence) == first
    assert store.get_run_unit(run, "one")["state"] == "completed"
    assert store.get_run_unit(run, "two")["state"] == "eligible"
    with pytest.raises(LatticeConflict, match="another run"):
        store.start_run_unit("different-run", attempt, "one", evidence=evidence)
    conflict = _frame(store, attempt, 1, "unit_started", unit_key="one")
    raw = store.blobs.get(conflict["content_ref"].split("/")[-1]).replace(b'123', b'124')
    conflict.update(content_ref=f"lattice:blob/{store.blobs.put(raw)}")
    with pytest.raises(LatticeConflict, match="conflicting bytes"):
        store.start_run_unit(run, attempt, "one", evidence=conflict)
    gap = _frame(store, attempt, 4, "unit_started", unit_key="two")
    with pytest.raises(LatticeConflict, match="skip sequences"):
        store.start_run_unit(run, attempt, "two", evidence=gap)


@pytest.mark.parametrize("fault", ["missing", "corrupt", "length", "semantic", "delimiter"])
def test_unresolvable_or_mismatched_frames_cannot_change_units(lattice_env, fault):
    store, run, attempt = _attempt(lattice_env)
    evidence = _frame(store, attempt, 1, "unit_started", unit_key="one")
    if fault == "missing":
        evidence["content_ref"] = "lattice:blob/" + "0" * 64
    elif fault == "corrupt":
        store.blobs.path_for(evidence["content_ref"].split("/")[-1]).write_bytes(b"changed")
    elif fault == "length":
        evidence["byte_count"] += 1
    elif fault == "semantic":
        evidence = _frame(store, attempt, 1, "unit_started", unit_key="two")
    else:
        raw = store.blobs.get(evidence["content_ref"].split("/")[-1])[:-1]
        evidence.update(content_ref=f"lattice:blob/{store.blobs.put(raw)}", byte_count=len(raw))
    with pytest.raises((LatticeConflict, OSError)):
        store.start_run_unit(run, attempt, "one", evidence=evidence)
    assert store.get_run_unit(run, "one")["state"] == "eligible"
    assert not _events(store, attempt, "durable-run-attempt.worker-admitted")


def test_semantic_noops_still_admit_each_new_sequence(lattice_env):
    store, run, attempt = _attempt(lattice_env)
    for sequence in (1, 2):
        store.start_run_unit(run, attempt, "one", evidence=_frame(store, attempt, sequence, "unit_started", unit_key="one"))
    for sequence in (3, 4):
        store.finish_run_unit(run, attempt, "one", RunUnitResultV1(state="completed"), evidence=_frame(store, attempt, sequence, "unit_completed", unit_key="one", output_sha256=None))
    for sequence in (5, 6):
        store.set_run_waiting(attempt, "operator", evidence=_frame(store, attempt, sequence, "waiting", reason="operator"))
    for sequence in (7, 8):
        store.continue_run(attempt, evidence=_frame(store, attempt, sequence, "continued"))
    receipts = _events(store, attempt, "durable-run-attempt.worker-admitted")
    assert [item["worker_sequence"] for item in receipts] == list(range(1, 9))
    assert store.get_run_unit(run, "one")["state"] == "completed"


def test_checkpoint_noop_retains_original_file_frame_and_normalized_result(lattice_env):
    store, run, attempt = _attempt(lattice_env)
    content = b"checkpoint bytes"
    digest = store.blobs.put(content)
    original = RunArtifactCreateV1(artifact_key="checkpoint", name="Checkpoint", artifact_class="checkpoint", media_type="application/octet-stream", content_sha256=digest, byte_count=len(content), storage_locator="file:/historical/checkpoint")
    captured = original.model_copy(update={"storage_locator": f"lattice:blob/{digest}"})
    receipts = []
    for sequence in (1, 2):
        evidence = _frame(store, attempt, sequence, "checkpoint", unit_key=None, artifact=original.model_dump(mode="json"))
        receipts.append(evidence)
        result = store.record_run_artifact(run, attempt, None, captured, evidence=evidence)
        assert result["storage_locator"] == f"lattice:blob/{digest}"
        assert store.get_run(run)["latest_checkpoint_artifact_id"] == result["artifact_id"]
    assert len(_events(store, attempt, "durable-run-attempt.worker-admitted")) == 2
    assert store.record_run_artifact(run, attempt, None, captured, evidence=receipts[0])["artifact_id"] == result["artifact_id"]
    changed = captured.model_copy(update={"name": "invented"})
    evidence = _frame(store, attempt, 3, "checkpoint", unit_key=None, artifact=original.model_dump(mode="json"))
    with pytest.raises(LatticeConflict, match="semantic transition"):
        store.record_run_artifact(run, attempt, None, changed, evidence=evidence)
    assert len(_events(store, attempt, "durable-run-attempt.worker-admitted")) == 2


def test_terminal_evidence_rejects_false_success_and_changed_prefixes(lattice_env):
    store, run, attempt = _attempt(lattice_env)
    evidence = _frame(store, attempt, 1, "run_completed")
    store.record_run_terminal_event(attempt, evidence)
    assert store.get_run_attempt(attempt)["state"] == "running"
    for unit in ("one", "two", "three"):
        store.start_run_unit(run, attempt, unit)
        store.finish_run_unit(run, attempt, unit, RunUnitResultV1(state="completed"))
    valid = {"stream_prefixes": {"stdout": 0, "stderr": 0}, "returncode": 0, "terminal_event_ref": evidence["content_ref"]}
    for bad in ({**valid, "returncode": 7}, {**valid, "returncode": False}, {**valid, "terminal_event_ref": None}, {**valid, "stream_prefixes": {"stdout": 1, "stderr": 0}}):
        with pytest.raises(LatticeConflict):
            store.finish_run_attempt(attempt, RunAttemptFinishV1(state="completed"), evidence=bad)
        assert store.get_run_attempt(attempt)["state"] == "running"
    terminal = store.finish_run_attempt(attempt, RunAttemptFinishV1(state="completed"), evidence=valid)
    assert terminal["state"] == "completed"
    assert store.record_run_terminal_event(attempt, evidence)["event_type"] == "run_completed"
    assert len(_events(store, attempt, "durable-run-attempt.worker-admitted")) == 1
    with pytest.raises(LatticeConflict):
        store.finish_run_attempt(attempt, RunAttemptFinishV1(state="completed"), evidence={**valid, "returncode": 8})


def test_stream_commit_interruption_does_not_admit_raw_tail(lattice_env, monkeypatch):
    store, run, attempt = _attempt(lattice_env)
    raw = b"\x00\xffadmitted only after commit"
    evidence = {"content_ref": f"lattice:blob/{store.blobs.put(raw)}", "byte_count": len(raw)}
    original = store._event

    def interrupt(conn, **kwargs):
        result = original(conn, **kwargs)
        if kwargs["event_type"] == "durable-run-attempt.stdout-admitted":
            raise RuntimeError("stream commit interrupted")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(store, "_event", interrupt)
        with pytest.raises(RuntimeError, match="interrupted"):
            store.record_run_stream_chunk(attempt, "stdout", 0, evidence)
    assert store.run_log_evidence(attempt, "stdout")["admitted_bytes"] == 0
    store.record_run_stream_chunk(attempt, "stdout", 0, evidence)
    assert store.run_log_evidence(attempt, "stdout")["admitted_bytes"] == len(raw)


def test_binary_stream_admission_rejects_gaps_changes_and_missing_bytes(lattice_env):
    store, run, attempt = _attempt(lattice_env)
    raw = b"\x00\xff\n\r\x80"
    evidence = {"content_ref": f"lattice:blob/{store.blobs.put(raw)}", "byte_count": len(raw)}
    first = store.record_run_stream_chunk(attempt, "stdout", 0, evidence)
    assert store.record_run_stream_chunk(attempt, "stdout", 0, evidence) == first
    store.record_run_stream_chunk(attempt, "stderr", 0, evidence)
    for offset in (1, 6):
        with pytest.raises(LatticeConflict):
            store.record_run_stream_chunk(attempt, "stdout", offset, evidence)
    changed = {"content_ref": f"lattice:blob/{store.blobs.put(b'other')}", "byte_count": 5}
    with pytest.raises(LatticeConflict, match="changes|overlaps"):
        store.record_run_stream_chunk(attempt, "stdout", 0, changed)
    missing = {"content_ref": "lattice:blob/" + "0" * 64, "byte_count": 1}
    with pytest.raises(OSError):
        store.record_run_stream_chunk(attempt, "stdout", 5, missing)
    oversized = b"x" * (64 * 1024 + 1)
    with pytest.raises(LatticeConflict, match="bounded"):
        store.record_run_stream_chunk(attempt, "stdout", 5, {"content_ref": f"lattice:blob/{store.blobs.put(oversized)}", "byte_count": len(oversized)})
    supervisor = DurableRunSupervisor(store, journal_root=store.config.data_root / "absent-journals")
    descriptor, size = supervisor.open_attempt_log(run, attempt, "stdout")
    try:
        assert size == len(raw) and os.read(descriptor, size + 1) == raw
    finally:
        os.close(descriptor)
    assert supervisor.attempt_log_status(run, attempt, "stdout")["qualified"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["stdout", "stderr"])
async def test_actual_pump_uses_bounded_chunks_and_keeps_binary_bytes(lattice_env, tmp_path, channel):
    store, run, attempt = _attempt(lattice_env)
    raw = bytes(range(256)) * 600
    source = tmp_path / "binary-source"
    source.write_bytes(raw)
    execution = _LocalExecution(run_id=run, attempt_id=attempt, bound=None, process=None, event_read_fd=-1, control_write_fd=-1, journal=None)
    supervisor = DurableRunSupervisor(store, journal_root=tmp_path / "journals")
    received = []
    await supervisor._pump_stream(execution, channel, source.open("rb"), received.append)
    receipts = _events(store, attempt, f"durable-run-attempt.{channel}-admitted")
    assert b"".join(received) == raw
    assert len(receipts) == 3
    assert [item["offset"] for item in receipts] == [0, 65536, 131072]
    assert b"".join(store.blobs.get(item["content_ref"].split("/")[-1]) for item in receipts) == raw


def _execution_for_frames(store, run, attempt, tmp_path, frames):
    read_fd, write_fd = os.pipe()
    os.write(write_fd, frames)
    os.close(write_fd)
    journal = DurableRunJournal(tmp_path / "attempt-journal", {})
    execution = _LocalExecution(run_id=run, attempt_id=attempt, bound=SimpleNamespace(artifact_roots=()), process=None, event_read_fd=read_fd, control_write_fd=-1, journal=journal)
    return execution


@pytest.mark.asyncio
async def test_retries_and_partial_diagnostic_frames_do_not_reapply_semantics(lattice_env, tmp_path):
    store, run, attempt = _attempt(lattice_env)
    receipt = _frame(store, attempt, 1, "unit_started", unit_key="one")
    raw = store.blobs.get(receipt["content_ref"].split("/")[-1])
    execution = _execution_for_frames(store, run, attempt, tmp_path, raw + raw + b'{"partial":')
    supervisor = DurableRunSupervisor(store, journal_root=tmp_path / "journals")
    try:
        with pytest.raises(RuntimeError, match="unterminated"):
            await supervisor._read_worker_events(execution)
        assert execution.last_sequence == 1
        assert execution.active_units == {"one"}
        assert execution.worker_event_in_flight is False
        assert len(_events(store, attempt, "durable-run-attempt.worker-admitted")) == 1
        assert execution.journal.events_path.read_bytes() == raw + raw + b'{"partial":'
    finally:
        execution.journal.close()


@pytest.mark.asyncio
async def test_failed_admission_cannot_update_safe_stop_memory(lattice_env, tmp_path, monkeypatch):
    store, run, attempt = _attempt(lattice_env)
    receipt = _frame(store, attempt, 1, "progress", message="safe", completed=None, total=None, safe_to_stop=True)
    raw = store.blobs.get(receipt["content_ref"].split("/")[-1])
    execution = _execution_for_frames(store, run, attempt, tmp_path, raw)
    supervisor = DurableRunSupervisor(store, journal_root=tmp_path / "journals")
    original = store._event

    def interrupt(conn, **kwargs):
        result = original(conn, **kwargs)
        if kwargs["event_type"] == "durable-run-attempt.worker-admitted":
            raise RuntimeError("admission interrupted")
        return result

    monkeypatch.setattr(store, "_event", interrupt)
    try:
        with pytest.raises(RuntimeError, match="interrupted"):
            await supervisor._read_worker_events(execution)
        assert execution.last_sequence == 0
        assert execution.safe_to_stop is False
        assert not _events(store, attempt, "durable-run.progress")
        assert execution.journal.events_path.read_bytes() == raw
    finally:
        execution.journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["journal", "semantics"])
async def test_previous_safe_boundary_cannot_overtake_inflight_unit_admission(lattice_env, tmp_path, monkeypatch, stage):
    store, run, attempt = _attempt(lattice_env)
    receipt = _frame(store, attempt, 1, "unit_started", unit_key="one")
    raw = store.blobs.get(receipt["content_ref"].split("/")[-1])
    execution = _execution_for_frames(store, run, attempt, tmp_path, raw)
    execution.process = SimpleNamespace(poll=lambda: None)
    execution.safe_to_stop = True
    supervisor = DurableRunSupervisor(store, journal_root=tmp_path / "journals")
    supervisor._executions[attempt] = execution
    reached, release = asyncio.Event(), asyncio.Event()
    journal_release = threading.Event()
    loop = asyncio.get_running_loop()
    terminated = []
    apply = supervisor._apply_event

    async def delayed(execution, event):
        reached.set()
        await release.wait()
        await apply(execution, event)

    async def terminate(execution):
        terminated.append(True)

    if stage == "journal":
        append = execution.journal.append_event

        def delayed_journal(*args, **kwargs):
            loop.call_soon_threadsafe(reached.set)
            if not journal_release.wait(5):
                raise TimeoutError("test journal gate expired")
            return append(*args, **kwargs)

        monkeypatch.setattr(execution.journal, "append_event", delayed_journal)
    else:
        monkeypatch.setattr(supervisor, "_apply_event", delayed)
    monkeypatch.setattr(supervisor, "_terminate", terminate)
    monkeypatch.setattr(supervisor, "_signal_stop", lambda execution: None)
    reader = asyncio.create_task(supervisor._read_worker_events(execution))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        assert (await supervisor.request_stop(run))["state"] == "stopping"
        assert not terminated
        release.set()
        journal_release.set()
        await reader
        assert not terminated
        assert store.get_run_unit(run, "one")["state"] == "running"
        assert execution.safe_to_stop is False
    finally:
        release.set()
        journal_release.set()
        await reader
        execution.journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["launch.json", "unit-plan.json", "target-environment.json", "process.json", "omit"])
async def test_control_tampering_or_omission_prevents_gate_release(lattice_env, tmp_path, monkeypatch, fault):
    import lattice.run_supervisor as runtime

    store = lattice_env["store"]
    declaration, _, _ = _bound_fixture(lattice_env, tmp_path, operation_key="control-tamper", behavior="normal")
    run = store.declare_run(declaration)
    store.admit_run(run["run_id"])
    original = store.record_run_process
    released = []
    monkeypatch.setattr(runtime, "release_local_run", lambda spawned: released.append(True))

    def tamper(attempt, identity, *, controls):
        if fault == "omit":
            controls = controls[:-1]
        else:
            item = next(item for item in controls if item["name"] == fault)
            raw = store.blobs.get(item["content_ref"].split("/")[-1])
            value = json.loads(raw)
            value["tampered"] = True
            changed = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            item.update(content_ref=f"lattice:blob/{store.blobs.put(changed)}", byte_count=len(changed))
        return original(attempt, identity, controls=controls)

    monkeypatch.setattr(store, "record_run_process", tamper)
    supervisor = DurableRunSupervisor(store, journal_root=tmp_path / "journals")
    await supervisor.start()
    result = await supervisor.execute(run["run_id"], "control-tamper-attempt")
    assert result["state"] == "failed"
    assert result["failure_kind"] == "executor_setup"
    assert not released
    assert result["process_identity"] is None
    assert not _events(store, result["attempt_id"], "durable-run-attempt.process-bound")
    assert _events(store, result["attempt_id"], "durable-run-attempt.terminal-evidence")[0]["returncode"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(("returncode", "lost_ack"), [(0, False), (7, False), (0, True)])
async def test_terminal_waits_for_binary_drains_and_actual_exit(lattice_env, tmp_path, monkeypatch, returncode, lost_ack):
    store, run, attempt = _attempt(lattice_env)
    for unit in ("one", "two", "three"):
        store.start_run_unit(run, attempt, unit)
        store.finish_run_unit(run, attempt, unit, RunUnitResultV1(state="completed"))
    receipt = _frame(store, attempt, 1, "run_completed")
    frame = store.blobs.get(receipt["content_ref"].split("/")[-1])
    read_fd, write_fd = os.pipe()
    control_read, control_write = os.pipe()
    stdout = b"committed" if lost_ack else bytes(range(256)) * 600
    stderr = b"\xff\x00error\n" * 10000
    # Generate large payloads in the child so the fixture respects ARG_MAX.
    stdout_expression = "b'committed'" if lost_ack else "bytes(range(256))*600"
    gate_read, gate_write = os.pipe()
    # Make the lost-ack ordering explicit: this terminal frame is committed
    # before output starts. Separate pipes cannot establish that order alone.
    gate = f"assert os.read({gate_read}, 1) == b'G'; " if lost_ack else ""
    program = f"import os; os.write({write_fd}, {frame!r}); os.close({write_fd}); {gate}os.write(1, {stdout_expression}); os.write(2, b'\\xff\\x00error\\n'*10000); raise SystemExit({returncode})"
    process = subprocess.Popen([sys.executable, "-c", program], pass_fds=(write_fd, gate_read), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    os.close(write_fd)
    os.close(gate_read)
    journal = DurableRunJournal(tmp_path / "attempt-journal", {})
    execution = _LocalExecution(run_id=run, attempt_id=attempt, bound=SimpleNamespace(artifact_roots=()), process=process, event_read_fd=read_fd, control_write_fd=control_write, journal=journal)
    supervisor = DurableRunSupervisor(store, journal_root=tmp_path / "journals")
    if lost_ack:
        append = store.record_run_stream_chunk
        admit_terminal = store.record_run_terminal_event

        def release_after_terminal(*args, **kwargs):
            result = admit_terminal(*args, **kwargs)
            os.write(gate_write, b"G")
            return result

        monkeypatch.setattr(store, "record_run_terminal_event", release_after_terminal)

        def lose_reply(attempt_id, channel, offset, evidence):
            result = append(attempt_id, channel, offset, evidence)
            if channel == "stdout":
                # Isolate lost acknowledgement from early worker termination;
                # the separate indefinite-worker case exercises that lifecycle.
                process.wait(timeout=3)
                raise RuntimeError("lost stream commit acknowledgement")
            return result

        monkeypatch.setattr(store, "record_run_stream_chunk", lose_reply)
    finish = journal.finish

    def project_only_after_commit(receipt):
        assert store.get_run_attempt(attempt)["state"] != "running"
        assert _events(store, attempt, "durable-run-attempt.terminal-evidence")
        finish(receipt)

    monkeypatch.setattr(journal, "finish", project_only_after_commit)
    try:
        terminal = await supervisor._monitor(execution)
    finally:
        os.close(control_read)
        os.close(gate_write)
    assert terminal["state"] == ("completed" if returncode == 0 and not lost_ack else "failed")
    if lost_ack:
        assert terminal["failure_kind"] == "worker_protocol"
        assert execution.stream_prefixes["stdout"] == 0
    if returncode:
        assert terminal["failure_kind"] == "process_exit"
    evidence = _events(store, attempt, "durable-run-attempt.terminal-evidence")[0]
    assert evidence["returncode"] == returncode
    assert evidence["terminal_event_ref"] == receipt["content_ref"]
    if lost_ack:
        assert evidence["stream_prefixes"]["stdout"] == len(stdout)
        assert 0 <= evidence["stream_prefixes"]["stderr"] <= len(stderr)
    else:
        assert evidence["stream_prefixes"] == {"stdout": len(stdout), "stderr": len(stderr)}
    assert [unit["state"] for unit in store.get_run(run)["units"]] == ["completed"] * 3
    for channel, expected in (("stdout", stdout), ("stderr", stderr)):
        descriptor, size = supervisor.open_attempt_log(run, attempt, channel)
        with os.fdopen(descriptor, "rb") as handle:
            assert size == evidence["stream_prefixes"][channel]
            assert handle.read() == expected[:size]
            if not lost_ack:
                assert size == len(expected)
        assert (journal.root / f"{channel}.log").read_bytes() == expected
    with store._connect() as conn:
        admitted = conn.execute("SELECT MAX(seq) FROM events WHERE subject_id=? AND event_type IN ('durable-run-attempt.stdout-admitted','durable-run-attempt.stderr-admitted','durable-run-attempt.worker-admitted')", (attempt,)).fetchone()[0]
        final = conn.execute("SELECT seq FROM events WHERE subject_id=? AND event_type='durable-run-attempt.terminal-evidence'", (attempt,)).fetchone()[0]
    assert admitted < final
