"""Exact-action ledger contracts; no provider process or native approval claim."""

from concurrent.futures import ThreadPoolExecutor
import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from lattice import LatticeStore
from lattice import native_approvals as approvals
from lattice.contracts import AttemptState, DraftCreate, NativeApprovalDecisionV1, NativeToolActionV1, ProviderRoute
from lattice.execution import DEFAULT_ADAPTERS, SubscriptionRouteUnavailable, validate_execution_policy
from lattice.security import ensure_cli_token
from lattice.server import create_app
from lattice.store import LatticeConflict, LatticeNotFound
from lattice.supervisor import ExecutionSupervisor


@pytest.fixture
def approval_env(lattice_env, monkeypatch, tmp_path):
    # Exercise the ledger beneath the still-closed production capability gate.
    # All other route checks remain real. No execution seam is invoked here.
    def admit_fixture(adapter, **policy):
        requested = policy["approval_policy"]
        result = validate_execution_policy(adapter, **{**policy, "approval_policy": "never"})
        return {**result, "approval_policy": requested}

    for module in ("store", "compositor", "manifest_reader"):
        monkeypatch.setattr(f"lattice.{module}.validate_execution_policy", admit_fixture)
    env = lattice_env
    env["route"] = ProviderRoute(provider="anthropic", harness="claude", model="claude-opus-5", execution_profile="workspace-agent", tools=["Read"], workspace=str(tmp_path), sandbox="provider-permissions", approval_policy="on-request")
    return env


def start(env, *, owner="test-boot", policy=None):
    draft = env["store"].create_draft(DraftCreate(project_id=env["project"]["project_id"], actor_binding={"kind": "direct"}, prompt="Read the planted fixture only", route=policy or env["route"]))
    invocation = env["lattice"].seal(draft["draft_id"])
    attempt = env["store"].create_attempt(invocation["invocation_id"], invocation["invocation_id"], owner, int(time.time()) + 3600)
    env["store"].mark_attempt_running(attempt["attempt_id"])
    return attempt["attempt_id"]


def ask(env, attempt, *, tool_id="tool_1", path="/planted/source.txt", owner="test-boot"):
    return approvals.request(env["store"], attempt, owner_boot_id=owner, action=NativeToolActionV1(tool_use_id=tool_id, tool_name="Read", tool_input={"file_path": path}, context={"description": "planted read", "suggestions": [{"type": "allowEverything"}]}))


def decide(env, attempt, item, decision="allow", *, owner="test-boot"):
    return approvals.decide(env["store"], attempt, item["request_id"], NativeApprovalDecisionV1(request_sha256=item["request_sha256"], decision=decision), owner_boot_id=owner)


def take(env, attempt, item, *, owner="test-boot"):
    return approvals.take_decision(env["store"], attempt, item["request_id"], owner_boot_id=owner, request_sha256=item["request_sha256"])


@pytest.mark.contract
@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_exact_decision_is_persistent_and_released_once(approval_env, decision):
    env = approval_env
    attempt = start(env)
    item = ask(env, attempt)
    assert item["state"] == "awaiting-operator"
    assert item["decision"] is None  # Native descriptions/suggestions cannot grant.
    assert take(env, attempt, item) is None
    assert ask(env, attempt) == item
    recorded = decide(env, attempt, item, decision)
    assert recorded["state"] == "decided"
    assert recorded["release"] is None
    assert recorded["decision"]["decision"] == decision
    assert recorded["decision"]["authority"] == "operator"
    reopened = LatticeStore(env["config"])
    assert approvals.inspect(reopened, attempt)["requests"] == [recorded]
    env["store"] = reopened
    assert decide(env, attempt, item, decision) == recorded
    released = take(env, attempt, item)
    assert released["state"] == "released-to-adapter"
    assert released["release"]["decision_event_id"] == recorded["decision"]["event_id"]
    assert released["release"]["decision"] == decision
    assert released["binding"] == item["binding"]
    with pytest.raises(LatticeConflict, match="not available for delivery"):
        take(env, attempt, item)
    with pytest.raises(LatticeConflict, match="immutable"):
        decide(env, attempt, item, "deny" if decision == "allow" else "allow")


@pytest.mark.contract
def test_action_or_sibling_identity_cannot_reuse_a_grant(approval_env):
    env = approval_env
    attempt = start(env)
    item = ask(env, attempt)
    decide(env, attempt, item)
    with pytest.raises(LatticeConflict, match="different action"):
        ask(env, attempt, path="/planted/another.txt")
    second = ask(env, attempt, tool_id="tool_2")
    with pytest.raises(LatticeConflict, match="exact requested action"):
        approvals.take_decision(env["store"], attempt, second["request_id"], owner_boot_id="test-boot", request_sha256=item["request_sha256"])
    other_attempt = start(env)
    with pytest.raises(LatticeNotFound):
        take(env, other_attempt, item)
    assert take(env, attempt, second) is None
    assert take(env, attempt, item)["decision"]["decision"] == "allow"


@pytest.mark.contract
def test_wrong_supervisor_and_undeclared_tool_are_rejected(approval_env):
    env = approval_env
    attempt = start(env)
    with pytest.raises(LatticeConflict, match="another supervisor"):
        ask(env, attempt, owner="unrelated-boot")
    item = ask(env, attempt)
    for operation in (decide, take):
        with pytest.raises(LatticeConflict, match="another supervisor"):
            operation(env, attempt, item, owner="unrelated-boot")
    with pytest.raises(LatticeConflict, match="sealed invocation tool surface"):
        approvals.request(env["store"], attempt, owner_boot_id="test-boot", action=NativeToolActionV1(tool_use_id="shell_1", tool_name="Bash", tool_input={"command": "echo forbidden"}))
    assert approvals.inspect(env["store"], attempt)["pending_count"] == 1


@pytest.mark.contract
@pytest.mark.parametrize("terminal", list(AttemptState)[2:])
@pytest.mark.parametrize("decided", [False, True])
def test_terminal_attempts_expire_unreleased_decisions(approval_env, terminal, decided):
    env = approval_env
    attempt = start(env)
    item = ask(env, attempt)
    if decided:
        decide(env, attempt, item)
    env["store"].finish_attempt(attempt, state=terminal)
    view = approvals.inspect(env["store"], attempt)
    assert view["pending_count"] == 0
    assert view["closed_reason"] == f"attempt {terminal.value}"
    assert view["requests"][0]["state"] == "expired"
    with pytest.raises(LatticeConflict, match="attempt is"):
        take(env, attempt, item)
    if decided:
        assert decide(env, attempt, item)["state"] == "expired"  # retry observes, not renews
    else:
        with pytest.raises(LatticeConflict, match="attempt is"):
            decide(env, attempt, item)


@pytest.mark.contract
def test_success_result_and_orphan_recovery_close_pending_requests(approval_env):
    env = approval_env
    first = start(env)
    second = start(env)
    first_item = ask(env, first)
    second_item = ask(env, second)
    decide(env, second, second_item)
    env["store"].finish_successful_attempt(first, response="planted completion", artifacts=[], journal_path="/planted/journal", runtime_receipt={"fixture": True})
    assert approvals.inspect(env["store"], first)["closed_reason"] == "attempt completed"
    reopened = LatticeStore(env["config"])
    assert reopened.reconcile_orphans("next-boot") == 1
    env["store"] = reopened
    for attempt, item in ((first, first_item), (second, second_item)):
        with pytest.raises(LatticeConflict, match="attempt is"):
            take(env, attempt, item)
    destination = env["config"].data_root.parent / "rebuilt" / "lattice.sqlite3"
    assert reopened.rebuild_database(destination) == reopened.state_digest()


@pytest.mark.contract
def test_fixed_approval_deadline_cannot_authorize_or_deliver(approval_env, monkeypatch):
    env = approval_env
    attempt = start(env)
    item = ask(env, attempt)
    monkeypatch.setattr(approvals, "monotonic", lambda: item["binding"]["deadline_monotonic"] + 1)
    assert approvals.inspect(env["store"], attempt)["requests"][0]["state"] == "expired"
    for operation in (decide, take):
        with pytest.raises(LatticeConflict, match="approval deadline expired"):
            operation(env, attempt, item)


@pytest.mark.contract
def test_withdrawal_is_action_scoped_and_closure_prevents_late_requests(approval_env):
    env = approval_env
    attempt = start(env)
    item = ask(env, attempt)
    sibling = ask(env, attempt, tool_id="tool_2")
    decide(env, attempt, item)
    approvals.withdraw(env["store"], attempt, item["request_id"], owner_boot_id="test-boot", reason="native request cancelled")
    with pytest.raises(LatticeConflict, match="not available for delivery"):
        take(env, attempt, item)
    assert decide(env, attempt, sibling)["state"] == "decided"
    approvals.close(env["store"], attempt, owner_boot_id="test-boot", reason="operator cancelled")
    assert approvals.inspect(env["store"], attempt)["requests"][1]["state"] == "expired"
    with pytest.raises(LatticeConflict, match="closed"):
        ask(env, attempt, tool_id="late")
    empty = start(env)
    approvals.close(env["store"], empty, owner_boot_id="test-boot", reason="cancel before callback")
    with pytest.raises(LatticeConflict, match="closed"):
        ask(env, empty)


@pytest.mark.concurrency
def test_concurrent_delivery_claims_release_only_once(approval_env):
    env = approval_env
    attempt = start(env)
    item = ask(env, attempt)
    decide(env, attempt, item)
    def consume():
        try:
            return take(env, attempt, item)["state"]
        except LatticeConflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: consume(), range(2)))
    assert sorted(results) == ["conflict", "released-to-adapter"]


@pytest.mark.contract
def test_request_bound_and_decision_shape_do_not_allow_rule_widening(approval_env):
    env = approval_env
    attempt = start(env)
    with pytest.raises(LatticeConflict, match="exceeds"):
        ask(env, attempt, path="x" * (approvals.MAX_REQUEST_BYTES + 1))
    with pytest.raises(ValidationError):
        NativeApprovalDecisionV1(request_sha256="a" * 64, decision="allow", updatedPermissions=[{"type": "addRules"}])
    with pytest.raises(ValidationError):
        NativeApprovalDecisionV1(request_sha256="a" * 64, decision="allow", tool_input={"changed": True})
    assert approvals.inspect(env["store"], attempt)["requests"] == []


@pytest.mark.contract
def test_noninteractive_invocation_cannot_request_native_approval(approval_env):
    env = approval_env
    attempt = start(env, policy=env["route"].model_copy(update={"approval_policy": "never"}))
    with pytest.raises(LatticeConflict, match="no native interactive"):
        ask(env, attempt)


@pytest.mark.contract
def test_production_native_approval_admits_only_ungranted_claude_bash(approval_env, tmp_path):
    adapter = next(profile for profile in DEFAULT_ADAPTERS if profile.provider_code == "anthropic")
    policy = dict(execution_profile="workspace-agent", tools=["Bash"], workspace=tmp_path,
                  sandbox="provider-permissions", approval_policy="on-request", network_policy="provider_managed")
    admitted = validate_execution_policy(adapter, **policy)
    assert admitted["approval_policy"] == "on-request"
    assert admitted["tools"] == ["Bash"] and admitted["permissions"] == []
    with pytest.raises(SubscriptionRouteUnavailable, match="pre-granted"):
        validate_execution_policy(adapter, **{**policy, "permissions": ["Bash"]})
    with pytest.raises(SubscriptionRouteUnavailable, match="exact Bash"):
        validate_execution_policy(adapter, **{**policy, "tools": ["Read"]})
    with pytest.raises(SubscriptionRouteUnavailable, match="Claude workspace"):
        validate_execution_policy(adapter, **{**policy, "execution_profile": "native-continuation", "continuation_mode": "resume", "native_session_id": "0a0a0a0a-0a0a-4a0a-8a0a-0a0a0a0a0a0a"})


@pytest.mark.contract
def test_api_requires_operator_auth_and_returns_exact_decision(approval_env):
    env = approval_env
    app = create_app(env["config"])
    token = ensure_cli_token(env["config"].operator_token)
    with TestClient(app, headers={"Host": "127.0.0.1:9010"}) as client:
        attempt = start(env, owner=app.state.supervisor.boot_id)
        item = ask(env, attempt, owner=app.state.supervisor.boot_id)
        path = f"/api/v1/attempts/{attempt}/native-approvals/{item['request_id']}/decision"
        body = {"request_sha256": item["request_sha256"], "decision": "allow"}
        assert client.post(path, json=body).status_code == 403
        assert approvals.inspect(env["store"], attempt)["pending_count"] == 1
        headers = {"Authorization": f"Bearer {token}"}
        wrong = client.post(path, json={**body, "request_sha256": "0" * 64}, headers=headers)
        assert wrong.status_code == 409
        response = client.post(path, json=body, headers=headers)
        assert response.status_code == 200
        assert response.json()["state"] == "decided"
        assert response.json()["release"] is None
        assert client.get(f"/api/v1/attempts/{attempt}/native-approvals", headers=headers).json()["pending_count"] == 0
        env["store"].finish_attempt(attempt, state=AttemptState.interrupted)


@pytest.mark.regression
@pytest.mark.parametrize("decided", [False, True])
def test_expired_approval_cannot_revive_through_late_heartbeat(approval_env, monkeypatch, decided):
    env = approval_env
    attempt = start(env)
    item = ask(env, attempt)
    if decided:
        decide(env, attempt, item)
    original_clock = time.time()
    monkeypatch.setattr(approvals.time, "time", lambda: 9_999_999_999)
    assert approvals.inspect(env["store"], attempt)["requests"][0]["state"] == ("decided" if decided else "awaiting-operator")
    env["store"].heartbeat_attempt(attempt, 10_000_000_999)
    assert approvals.inspect(env["store"], attempt)["requests"][0]["state"] == "expired"
    with pytest.raises(LatticeConflict, match="not available for delivery"):
        take(env, attempt, item)
    monkeypatch.setattr(approvals.time, "time", lambda: original_clock)
    assert approvals.inspect(env["store"], attempt)["requests"][0]["state"] == "expired"
    with pytest.raises(LatticeConflict, match="not available for delivery"):
        take(env, attempt, item)


@pytest.mark.asyncio
@pytest.mark.regression
@pytest.mark.parametrize("failed_boundary", ["closure", "attempt-read"])
async def test_cancel_signals_owned_process_even_when_persistence_fails(approval_env, monkeypatch, failed_boundary):
    env = approval_env
    supervisor = ExecutionSupervisor(env["store"])
    attempt = start(env, owner=supervisor.boot_id)
    signal = threading.Event()
    supervisor._cancellations[attempt] = signal

    def fail(*_args, **_kwargs):
        raise OSError("planted control-store failure")

    if failed_boundary == "closure":
        monkeypatch.setattr(approvals, "close", fail)
    else:
        monkeypatch.setattr(env["store"], "get_attempt", fail)
    with pytest.raises(OSError, match="planted control-store failure"):
        await supervisor.cancel(attempt)
    assert signal.is_set()


@pytest.mark.asyncio
@pytest.mark.regression
async def test_shutdown_signals_and_joins_all_owned_tasks_after_closure_failure(approval_env, monkeypatch):
    env = approval_env
    supervisor = ExecutionSupervisor(env["store"])
    attempts = [start(env, owner=supervisor.boot_id), start(env, owner=supervisor.boot_id)]
    finished = []
    release_fixture = asyncio.Event()

    async def worker(attempt):
        while not supervisor._cancellations[attempt].is_set() and not release_fixture.is_set():
            await asyncio.sleep(0.001)
        finished.append(attempt)

    for attempt in attempts:
        supervisor._cancellations[attempt] = threading.Event()
        supervisor._tasks[attempt] = asyncio.create_task(worker(attempt))
    def fail(*_args, **_kwargs):
        raise OSError("planted closure failure")
    monkeypatch.setattr(approvals, "close", fail)
    try:
        with pytest.raises((OSError, ExceptionGroup)) as raised:
            await supervisor.stop()
        assert "planted closure failure" in str(raised.value) or any("planted closure failure" in str(item) for item in getattr(raised.value, "exceptions", []))
        assert all(signal.is_set() for signal in supervisor._cancellations.values())
        assert sorted(finished) == sorted(attempts)
        assert all(task.done() for task in supervisor._tasks.values())
    finally:
        release_fixture.set()
        await asyncio.gather(*supervisor._tasks.values())


@pytest.mark.concurrency
@pytest.mark.parametrize("invalidation", ["close", "withdraw"])
def test_release_and_invalidation_serialize_across_store_instances(approval_env, invalidation):
    env = approval_env
    attempt = start(env)
    item = ask(env, attempt)
    decide(env, attempt, item)
    second_store = LatticeStore(env["config"])
    ready = threading.Barrier(2, timeout=5)

    def consume():
        ready.wait()
        try:
            return take(env, attempt, item)["state"]
        except LatticeConflict:
            return "conflict"

    def invalidate():
        ready.wait()
        if invalidation == "close":
            approvals.close(second_store, attempt, owner_boot_id="test-boot", reason="racing cancel")
        else:
            approvals.withdraw(second_store, attempt, item["request_id"], owner_boot_id="test-boot", reason="racing native cancel")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(consume)
        second = pool.submit(invalidate)
        outcome = first.result(timeout=10)
        second.result(timeout=10)
    with env["store"]._connect() as conn:
        events = [(row["seq"], row["event_type"]) for row in conn.execute("SELECT seq,event_type FROM events WHERE subject_id=? ORDER BY seq", (attempt,))]
    releases = [seq for seq, kind in events if kind == "native-approval.released"]
    endings = [seq for seq, kind in events if kind in {"native-approval.closed", "native-approval.withdrawn"}]
    assert len(releases) == (1 if outcome == "released-to-adapter" else 0)
    assert not releases or not endings or releases[0] < endings[0]
    with pytest.raises(LatticeConflict):
        take(env, attempt, item)


@pytest.mark.concurrency
def test_invocation_and_approval_views_share_a_read_snapshot(approval_env, monkeypatch):
    env = approval_env
    attempt = start(env)
    ask(env, attempt)
    invocation_id = env["store"].get_attempt(attempt)["invocation_id"]
    original = approvals.state
    changed = False

    def interleave(conn, attempt_id):
        nonlocal changed
        if not changed:
            changed = True
            env["store"].finish_attempt(attempt_id, state=AttemptState.interrupted)
        return original(conn, attempt_id)

    monkeypatch.setattr(approvals, "state", interleave)
    observed = env["store"].get_invocation(invocation_id)
    assert changed
    assert observed["state"] == "running"
    assert observed["attempts"][0]["state"] == "running"
    assert observed["attempts"][0]["native_approvals"]["requests"][0]["state"] == "awaiting-operator"
    assert env["store"].get_invocation(invocation_id)["state"] == "interrupted"


@pytest.mark.regression
@pytest.mark.parametrize("decided", [False, True])
def test_approval_lifetime_uses_monotonic_time_not_clock_corrections(approval_env, monkeypatch, decided):
    env = approval_env
    wall_clock = time.time()
    monkeypatch.setattr(approvals, "monotonic", lambda: 100.0, raising=False)
    attempt = start(env)
    item = ask(env, attempt)
    if decided:
        decide(env, attempt, item)
    monkeypatch.setattr(approvals.time, "time", lambda: 9_999_999_999)
    # A wall-clock correction alone neither expires nor renews this capability.
    assert approvals.inspect(env["store"], attempt)["requests"][0]["state"] == ("decided" if decided else "awaiting-operator")
    deadline = item["binding"]["deadline_monotonic"]
    assert 100.0 < deadline <= 100.0 + env["config"].lease_seconds
    monkeypatch.setattr(approvals, "monotonic", lambda: deadline + 1)
    assert approvals.inspect(env["store"], attempt)["requests"][0]["state"] == "expired"
    approvals.withdraw(env["store"], attempt, item["request_id"], owner_boot_id="test-boot", reason="cancel expired native callback")
    monkeypatch.setattr(approvals.time, "time", lambda: wall_clock)
    assert approvals.inspect(env["store"], attempt)["requests"][0]["state"] == "expired"
    assert ask(env, attempt)["binding"]["deadline_monotonic"] == deadline
    with pytest.raises(LatticeConflict, match="approval deadline expired"):
        take(env, attempt, item)
