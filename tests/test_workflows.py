from __future__ import annotations

import json
import gzip
import sqlite3
import subprocess
from pathlib import Path

import pytest
import httpx

from lattice.context_modules import ContextModuleError, ContextModuleRunner
from lattice.canonical import canonical_bytes, sha256_bytes
from lattice.contracts import AttemptState, ContextBlockCreate, ContextModuleDefinition, ContextModuleMode, DraftCreate, DraftPatch, ProjectCreate, ProviderRoute, RoleCreate, RoleSessionCreate, WorkflowPackDefinition
from lattice.quaternary import QuaternaryAdapter, QuaternaryBoundaryError, QuaternaryOutcomeUnknown
from lattice.platform_foundry import PlatformFoundryAdapter
from lattice.workflows import WorkflowRegistry


def _command_project(store):
    pack_sha = WorkflowRegistry(Path(__file__).parents[1] / "workflow_packs").load("quaternary")["canonical_sha256"]
    created = store.create_workflow_project_atomic(title="workflow", purpose="", workflow_pack="quaternary", initial_state="semantic-intent", role_specs=[{"name": "Operator", "authority": "read", "instructions": "Inspect."}], blackboard_content=b"{}", blackboard_source_ref="fixture:state", slice_external_ref=None, pack_canonical_sha256=pack_sha)
    return created["project"], pack_sha


def _command_review(project_id: str, pack_sha: str, operation: str, arguments: dict):
    body = {"kind": "lattice-workflow-command-review-v1", "project_id": project_id, "workflow_revision": 1, "pack_id": "quaternary", "pack_canonical_sha256": pack_sha, "operation": operation, "authority": "fixture", "requested_arguments_sha256": sha256_bytes(canonical_bytes(arguments)), "prepared_arguments_sha256": sha256_bytes(canonical_bytes(arguments)), "file_receipts": []}
    return {**body, "review_sha256": sha256_bytes(canonical_bytes(body))}


@pytest.mark.contract
def test_quaternary_adapter_reads_authoritative_state_without_writing(tmp_path: Path):
    database = tmp_path / "quaternary.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.executescript(
            """
            CREATE TABLE qe_language_preparations(preparation_id TEXT, language_code TEXT, generation INTEGER, state TEXT);
            CREATE TABLE qe_preparation_actions(action_id TEXT, preparation_id TEXT, action_kind TEXT, role TEXT, status TEXT, request_sha256 TEXT, created_at TEXT, completed_at TEXT);
            CREATE TABLE qe_language_readiness(preparation_id TEXT, state TEXT, dependency_set_sha256 TEXT, proof_limits_json TEXT, verified_at TEXT, verifier TEXT);
            INSERT INTO qe_language_preparations VALUES('prep-de-1','de',1,'calibrating');
            INSERT INTO qe_preparation_actions VALUES('action-1','prep-de-1','native_consultation','native_partner','pending','abc','now',NULL);
            """
        )
    before = database.read_bytes()
    adapter = QuaternaryAdapter(database, tmp_path / "gatherctl.py")
    result = adapter.inspect({"language": "de"})
    assert result["preparations"][0]["actions"][0]["role"] == "native_partner"
    assert result["preparations"][0]["profile_counts"] is None
    assert result["state_sha256"]
    assert "qe_preflight_receipts" in result["schema_missing"]
    assert database.read_bytes() == before


@pytest.mark.oracle
def test_quaternary_language_state_selects_latest_and_binds_omitted_action_rows(tmp_path: Path):
    database = tmp_path / "quaternary.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.executescript(
            """
            CREATE TABLE qe_language_preparations(preparation_id TEXT, language_code TEXT, generation INTEGER, state TEXT);
            CREATE TABLE qe_preparation_actions(action_id TEXT, preparation_id TEXT, action_kind TEXT, role TEXT, status TEXT, request_sha256 TEXT, created_at TEXT, completed_at TEXT);
            CREATE TABLE qe_authority_receipts(receipt_sha256 TEXT, preparation_id TEXT, body_zlib BLOB);
            INSERT INTO qe_language_preparations VALUES('qep_de_1','de',1,'retired');
            INSERT INTO qe_language_preparations VALUES('qep_de_2','de',2,'calibrating');
            INSERT INTO qe_authority_receipts VALUES('receipt-1','qep_de_2',X'00010203');
            """
        )
        conn.executemany(
            "INSERT INTO qe_preparation_actions VALUES(?,?,?,?,?,?,?,NULL)",
            [(f"action-{index:03d}", "qep_de_2", "native_consultation", "native_partner", "pending", f"sha-{index:03d}", f"time-{index:03d}") for index in range(101)],
        )
    adapter = QuaternaryAdapter(database, tmp_path / "gatherctl.py")

    first = adapter.inspect({"language": "de"})

    assert first["resolved_preparation_id"] == "qep_de_2"
    assert first["preparation_coverage"] == {"state": "partial", "matching": 2, "returned": 1, "selection": "latest-generation"}
    assert len(first["preparations"]) == 1
    assert len(first["preparations"][0]["actions"]) == 100
    assert first["preparations"][0]["action_coverage"]["row_count"] == 101
    assert first["preparations"][0]["action_coverage"]["state"] == "partial"

    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE qe_preparation_actions SET role='mutated-omitted-role' WHERE action_id='action-000'")
    second = adapter.inspect({"language": "de"})

    assert [item["action_id"] for item in second["preparations"][0]["actions"]] == [item["action_id"] for item in first["preparations"][0]["actions"]]
    assert second["state_sha256"] != first["state_sha256"]

    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE qe_authority_receipts SET body_zlib=X'00010204' WHERE receipt_sha256='receipt-1'")
    third = adapter.inspect({"language": "de"})
    assert third["preparations"][0]["rowset_receipts"]["authority_receipts"]["row_count"] == 1
    assert third["state_sha256"] != second["state_sha256"]


@pytest.mark.regression
def test_quaternary_boundary_rejects_arbitrary_commands_and_arguments(tmp_path: Path):
    adapter = QuaternaryAdapter(tmp_path / "db", tmp_path / "gatherctl.py")
    with pytest.raises(QuaternaryBoundaryError, match="unsupported"):
        adapter.command("execute-arbitrary-python", {})

    def planted_runner(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"ok": True}), stderr="")

    gatherctl = tmp_path / "gatherctl.py"
    gatherctl.write_text("# fixture", encoding="utf-8")
    adapter = QuaternaryAdapter(tmp_path / "db", gatherctl, runner=planted_runner)
    with pytest.raises(QuaternaryBoundaryError, match="undeclared argument"):
        adapter.command("language-status", {"shell": "rm"})

    private_input_reads = 0
    private_body = b"private authority body"
    private_digest = sha256_bytes(private_body)

    def private_blob_reader(_digest):
        nonlocal private_input_reads
        private_input_reads += 1
        return private_body

    adapter = QuaternaryAdapter(tmp_path / "db", gatherctl, runner=planted_runner, blob_reader=private_blob_reader)
    with pytest.raises(QuaternaryBoundaryError, match="undeclared argument"):
        adapter.command("record-language-authority", {"body_path": f"lattice:blob/{private_digest}", "shell": "rm"})
    assert private_input_reads == 0

    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    approved = tmp_path / "approved"
    approved.mkdir()
    adapter = QuaternaryAdapter(tmp_path / "db", tmp_path / "gatherctl.py", runner=planted_runner, allowed_input_roots=(approved,))
    with pytest.raises(QuaternaryBoundaryError, match="escaped"):
        adapter.command("freeze-language-profile", {"preparation_id": "qep_fixture", "input": str(outside)})
    with pytest.raises(QuaternaryBoundaryError, match="body_path escaped"):
        adapter.command("record-language-authority", {"preparation_id": "qep_fixture", "language_code": "de", "locator": "https://example.invalid", "body_path": str(outside)})


@pytest.mark.regression
def test_quaternary_subprocess_timeout_is_a_typed_visible_failure(tmp_path: Path):
    gatherctl = tmp_path / "gatherctl.py"
    gatherctl.write_text("# fixture", encoding="utf-8")

    def timeout_runner(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    adapter = QuaternaryAdapter(tmp_path / "db", gatherctl, runner=timeout_runner)
    with pytest.raises(QuaternaryBoundaryError, match='"kind": "quaternary-command-timeout"'):
        adapter.command("next-language", {})


@pytest.mark.regression
def test_quaternary_default_runner_bounds_output_and_write_failures_are_outcome_unknown(tmp_path: Path):
    gatherctl = tmp_path / "gatherctl.py"
    gatherctl.write_text("print('x' * 4096)", encoding="utf-8")
    adapter = QuaternaryAdapter(tmp_path / "db", gatherctl)
    adapter.MAX_CAPTURE_BYTES = 128

    with pytest.raises(QuaternaryBoundaryError, match="quaternary-command-output-limit") as read_failure:
        adapter.command("next-language", {})
    assert not isinstance(read_failure.value, QuaternaryOutcomeUnknown)

    with pytest.raises(QuaternaryOutcomeUnknown, match="quaternary-command-output-limit"):
        adapter.command("prepare-language", {"language": "de"})

    def nonzero_runner(command, **_kwargs):
        return subprocess.CompletedProcess(command, 7, stdout="", stderr="write may have committed")

    injected = QuaternaryAdapter(tmp_path / "db", gatherctl, runner=nonzero_runner)
    with pytest.raises(QuaternaryOutcomeUnknown, match="write may have committed"):
        injected.command("prepare-language", {"language": "de"})


@pytest.mark.regression
def test_quaternary_driver_drift_makes_dispatched_outcome_unknown(tmp_path: Path):
    gatherctl = tmp_path / "gatherctl.py"
    gatherctl.write_text("# before", encoding="utf-8")

    def drifting_runner(command, **_kwargs):
        gatherctl.write_text("# after", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"ok": True}), stderr="")

    adapter = QuaternaryAdapter(tmp_path / "db", gatherctl, runner=drifting_runner)
    with pytest.raises(QuaternaryOutcomeUnknown, match="quaternary-command-driver-drift"):
        adapter.command("next-language", {})


@pytest.mark.regression
def test_quaternary_post_dispatch_attestation_failure_preserves_unknown_write_outcome(monkeypatch, tmp_path: Path):
    gatherctl = tmp_path / "gatherctl.py"
    gatherctl.write_text("# fixture", encoding="utf-8")

    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, stdout='{"ok":true}', stderr="")

    adapter = QuaternaryAdapter(tmp_path / "db", gatherctl, runner=runner)
    before = adapter._driver_manifest()
    calls = iter((before, QuaternaryBoundaryError("planted postflight failure")))

    def attestation():
        value = next(calls)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(adapter, "_driver_manifest", attestation)
    with pytest.raises(QuaternaryOutcomeUnknown, match="quaternary-command-postflight-unavailable"):
        adapter.command("prepare-language", {"language": "de"})


@pytest.mark.oracle
def test_quaternary_adapter_materializes_exact_bound_blob_only_for_subprocess_lifetime(tmp_path: Path):
    gatherctl = tmp_path / "gatherctl.py"
    gatherctl.write_text("# fixture", encoding="utf-8")
    content = b'{"profile":"exact"}'
    digest = sha256_bytes(content)
    observed = {}

    def runner(command, **_kwargs):
        input_path = Path(command[command.index("--input") + 1])
        observed["path"] = input_path
        observed["content"] = input_path.read_bytes()
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"ok": True}), stderr="")

    adapter = QuaternaryAdapter(tmp_path / "db", gatherctl, runner=runner, blob_reader=lambda requested: content if requested == digest else b"")
    result = adapter.command("freeze-language-profile", {"preparation_id": "qep_fixture", "input": f"lattice:blob/{digest}"})

    assert observed["content"] == content
    assert not observed["path"].exists()
    assert result["receipt"]["input_receipts"][0]["content_sha256"] == digest


@pytest.mark.regression
def test_quaternary_adapter_removes_bound_input_when_driver_attestation_fails(monkeypatch, tmp_path: Path):
    gatherctl = tmp_path / "gatherctl.py"
    gatherctl.write_text("# fixture", encoding="utf-8")
    content = b'{"profile":"private"}'
    digest = sha256_bytes(content)
    created_snapshots = []
    real_mkstemp = __import__("tempfile").mkstemp

    def recording_mkstemp(*args, **kwargs):
        descriptor, name = real_mkstemp(*args, dir=tmp_path, **kwargs)
        created_snapshots.append(Path(name))
        return descriptor, name

    adapter = QuaternaryAdapter(tmp_path / "db", gatherctl, blob_reader=lambda _digest: content)
    monkeypatch.setattr("lattice.quaternary.tempfile.mkstemp", recording_mkstemp)
    monkeypatch.setattr(adapter, "_driver_manifest", lambda: (_ for _ in ()).throw(QuaternaryBoundaryError("planted attestation failure")))

    with pytest.raises(QuaternaryBoundaryError, match="planted attestation failure"):
        adapter.command("freeze-language-profile", {"preparation_id": "qep_fixture", "input": f"lattice:blob/{digest}"})

    assert created_snapshots
    assert all(not path.exists() for path in created_snapshots)


@pytest.mark.contract
def test_negative_control_registry_rejects_adapter_operations_absent_from_the_portable_pack(tmp_path: Path):
    pack_root = tmp_path / "packs"
    pack_root.mkdir()
    source = Path(__file__).parents[1] / "workflow_packs" / "quaternary" / "pack.json"
    target = pack_root / "quaternary"
    target.mkdir()
    value = json.loads(source.read_text())
    value["commands"] = [item for item in value["commands"] if item["operation"] != "create-gather"]
    (target / "pack.json").write_text(json.dumps(value), encoding="utf-8")
    registry = WorkflowRegistry(pack_root)
    registry.register("quaternary", QuaternaryAdapter(tmp_path / "db", tmp_path / "gatherctl.py"))

    with pytest.raises(ValueError, match="not declared"):
        registry.command("quaternary", "create-gather", {"preparation_id": "qep_fixture"})


@pytest.mark.contract
def test_workflow_project_selectors_are_declared_defaulted_and_closed_to_extras():
    registry = WorkflowRegistry(Path(__file__).parents[1] / "workflow_packs")

    assert registry.resolve_selector("quaternary", {}) == {"language": "de"}
    assert registry.resolve_selector("platform-foundry", {"platform_id": "pinterest-com"}) == {"platform_id": "pinterest-com"}
    with pytest.raises(ValueError, match="lacks required fields"):
        registry.resolve_selector("platform-foundry", {})
    with pytest.raises(ValueError, match="undeclared fields"):
        registry.resolve_selector("platform-foundry", {"platform_id": "pinterest-com", "authority": "write"})


@pytest.mark.regression
def test_negative_control_workflow_pack_rejects_duplicate_json_keys(tmp_path: Path):
    pack_root = tmp_path / "packs"
    target = pack_root / "quaternary"
    target.mkdir(parents=True)
    source = (Path(__file__).parents[1] / "workflow_packs" / "quaternary" / "pack.json").read_text()
    (target / "pack.json").write_text(source.replace('"version": 2,', '"version": 2, "version": 3,', 1), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate JSON object key"):
        WorkflowRegistry(pack_root).load("quaternary")


@pytest.mark.regression
def test_negative_control_workflow_pack_is_bounded_before_json_parsing(tmp_path: Path):
    pack_root = tmp_path / "packs"
    target = pack_root / "oversized"
    target.mkdir(parents=True)
    registry = WorkflowRegistry(pack_root)
    registry.MAX_PACK_BYTES = 32
    (target / "pack.json").write_bytes(b"{" + (b" " * 64) + b"}")

    with pytest.raises(ValueError, match="exceeds 32 bytes"):
        registry.load("oversized")


@pytest.mark.oracle
def test_workflow_pack_has_blinded_challenger_and_deterministic_authority():
    root = Path(__file__).parents[1] / "workflow_packs"
    registry = WorkflowRegistry(root)
    pack = registry.load("quaternary")["definition"]
    assert any(role["role_id"] == "verifier" and role["authority"] == "challenge-only" for role in pack["roles"])
    assert {trigger["trigger"] for trigger in pack["triggers"]} == {"closure-proposed", "query-accepted"}
    assert pack["closure_contract"]["self_certification_forbidden"] is True
    assert {command["operation"] for command in pack["commands"]} == QuaternaryAdapter.READ_OPERATIONS | QuaternaryAdapter.WRITE_OPERATIONS


@pytest.mark.contract
def test_file_context_module_is_root_bounded_and_receipted(tmp_path: Path):
    root = tmp_path / "allowed"
    root.mkdir()
    source = root / "SURFACE.json"
    source.write_text('{"status":"current"}', encoding="utf-8")
    runner = ContextModuleRunner(allowed_file_roots=(root,), octopus_base_url="http://127.0.0.1:8000")
    definition = ContextModuleDefinition(module_id="surface.current", version=1, mode=ContextModuleMode.manual, source_kind="surface", capabilities=["read-local-file"], configuration={"path": str(source)})
    result = runner.resolve(definition)
    assert result.content == source.read_bytes()
    assert result.receipt["content_sha256"]
    assert result.receipt["definition"] == definition.model_dump(mode="json")
    assert result.receipt["definition_sha256"] == sha256_bytes(canonical_bytes(definition.model_dump(mode="json")))

    outside = tmp_path / "secret"
    outside.write_text("no", encoding="utf-8")
    planted_escape = definition.model_copy(update={"configuration": {"path": str(outside)}})
    with pytest.raises(ContextModuleError, match="outside"):
        runner.resolve(planted_escape)


@pytest.mark.oracle
def test_http_context_module_enforces_exact_origin_bounded_utf8_and_declared_capability(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "http://127.0.0.1:9000/private"})
        if request.url.path == "/binary":
            return httpx.Response(200, content=b"\xff")
        if request.url.path == "/compressed":
            return httpx.Response(200, content=gzip.compress(b"rain" * 1_000_000), headers={"content-encoding": "gzip"})
        return httpx.Response(200, content=b"rain", headers={"content-type": "text/plain; charset=utf-8"})

    runner = ContextModuleRunner(allowed_file_roots=(tmp_path,), octopus_base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(handler))
    valid = ContextModuleDefinition(module_id="weather.local", version=1, mode=ContextModuleMode.manual, source_kind="http_readonly", capabilities=["read-http"], configuration={"url": "https://weather.example/current", "allowed_origins": ["https://weather.example"], "max_bytes": 4})
    result = runner.resolve(valid)
    assert result.content == b"rain"
    assert result.receipt["http"]["authorized_origin"] == "https://weather.example"

    for mutation, message in (
        (valid.model_copy(update={"capabilities": []}), "exactly the read-http"),
        (valid.model_copy(update={"configuration": {**valid.configuration, "url": "https://weather.example.evil/current"}}), "exact origin"),
        (valid.model_copy(update={"configuration": {**valid.configuration, "url": "https://weather.example/redirect"}}), "HTTP 302"),
        (valid.model_copy(update={"configuration": {**valid.configuration, "url": "https://weather.example/binary"}}), "UTF-8"),
        (valid.model_copy(update={"configuration": {**valid.configuration, "url": "https://weather.example/compressed"}}), "rejected before decompression"),
        (valid.model_copy(update={"configuration": {**valid.configuration, "max_bytes": 3}}), "3-byte limit"),
    ):
        with pytest.raises(ContextModuleError, match=message):
            runner.resolve(mutation)


@pytest.mark.regression
def test_workflow_project_bootstrap_is_atomic(lattice_env):
    store = lattice_env["store"]
    before = len(store.list_projects())
    duplicated_roles = [
        {"name": "same", "authority": "proposal", "instructions": "one"},
        {"name": "same", "authority": "proposal", "instructions": "two"},
    ]
    with pytest.raises(sqlite3.IntegrityError):
        store.create_workflow_project_atomic(title="must roll back", purpose="negative control", workflow_pack="quaternary", initial_state="semantic-intent", role_specs=duplicated_roles, blackboard_content=b"{}", blackboard_source_ref="fixture:state", slice_external_ref="slate:atomic-negative")
    assert len(store.list_projects()) == before


@pytest.mark.contract
def test_workflow_command_is_idempotent_and_preserves_exact_result(lattice_env):
    store = lattice_env["store"]
    project, pack_sha = _command_project(store)
    arguments = {"language": "de"}
    review = _command_review(project["project_id"], pack_sha, "language-status", arguments)
    first = store.claim_workflow_command(project_id=project["project_id"], pack_id="quaternary", expected_workflow_revision=1, pack_canonical_sha256=pack_sha, operation="language-status", arguments=arguments, review=review, idempotency_key="workflow-idempotency-1", boot_id="boot-a")
    same = store.claim_workflow_command(project_id=project["project_id"], pack_id="quaternary", expected_workflow_revision=1, pack_canonical_sha256=pack_sha, operation="language-status", arguments=arguments, review=review, idempotency_key="workflow-idempotency-1", boot_id="boot-a")
    assert same["command_id"] == first["command_id"]

    completed = store.complete_workflow_command(first["command_id"], result={"receipt": {"exit_code": 0}, "output": {"state": "calibrating"}, "stdout": "exact bytes", "stderr": ""})
    assert completed["result"]["stdout"] == "exact bytes"
    with pytest.raises(Exception, match="terminal"):
        store.complete_workflow_command(first["command_id"], result={"receipt": {}, "output": {}})


@pytest.mark.concurrency
def test_negative_control_workflow_command_cannot_cross_reviewed_revision(lattice_env):
    store = lattice_env["store"]
    project, pack_sha = _command_project(store)
    store.advance_workflow(project_id=project["project_id"], pack_id="quaternary", expected_revision=1, from_state="semantic-intent", to_state="native-query-proposal", evidence_block_ids=[], payload={}, trigger=None)
    arguments = {"language": "de"}

    with pytest.raises(Exception, match="state changed after command review"):
        store.claim_workflow_command(project_id=project["project_id"], pack_id="quaternary", expected_workflow_revision=1, pack_canonical_sha256=pack_sha, operation="language-status", arguments=arguments, review=_command_review(project["project_id"], pack_sha, "language-status", arguments), idempotency_key="stale-workflow-command", boot_id="boot-a")


@pytest.mark.oracle
def test_workflow_command_event_binds_private_arguments_without_copying_them(lattice_env):
    store = lattice_env["store"]
    project, pack_sha = _command_project(store)
    secret = "PRIVATE_AUTHORITY_EVIDENCE"
    arguments = {"body": secret}
    command = store.claim_workflow_command(project_id=project["project_id"], pack_id="quaternary", expected_workflow_revision=1, pack_canonical_sha256=pack_sha, operation="record-language-authority", arguments=arguments, review=_command_review(project["project_id"], pack_sha, "record-language-authority", arguments), idempotency_key="workflow-private-arguments", boot_id="boot-a")

    event = next(item for item in store.events_after(project["project_id"]) if item["event_type"] == "workflow.command-started")

    assert secret not in json.dumps(event)
    assert event["payload"]["arguments_sha256"]
    assert store.get_workflow_command(command["command_id"])["arguments"]["body"] == secret


@pytest.mark.contract
def test_workflow_registry_rejects_integer_arguments_outside_declared_bounds(tmp_path: Path):
    registry = WorkflowRegistry(Path(__file__).parents[1] / "workflow_packs")
    registry.register("quaternary", QuaternaryAdapter(tmp_path / "db", tmp_path / "gatherctl.py"))

    with pytest.raises(ValueError, match="below its minimum"):
        registry.command("quaternary", "qualify-language", {"preparation_id": "qep_fixture", "timeout": 0})


@pytest.mark.contract
def test_workflow_command_defaults_are_resolved_before_execution_and_receipting():
    class RecordingAdapter:
        def __init__(self):
            self.arguments = None

        def inspect(self, _selector):
            return {}

        def command(self, _operation, arguments):
            self.arguments = arguments
            return {"arguments": arguments}

    adapter = RecordingAdapter()
    registry = WorkflowRegistry(Path(__file__).parents[1] / "workflow_packs")
    registry.register("quaternary", adapter)

    resolved = registry.resolve_command("quaternary", "language-status", {})
    result = registry.command("quaternary", "language-status", {})

    assert resolved == {"language": "de"}
    assert result["arguments"] == resolved
    assert adapter.arguments == resolved


@pytest.mark.concurrency
def test_restart_marks_dispatched_workflow_outcome_unknown_instead_of_retrying(lattice_env):
    store = lattice_env["store"]
    project, pack_sha = _command_project(store)
    arguments = {"language": "de"}
    command = store.claim_workflow_command(project_id=project["project_id"], pack_id="quaternary", expected_workflow_revision=1, pack_canonical_sha256=pack_sha, operation="prepare-language", arguments=arguments, review=_command_review(project["project_id"], pack_sha, "prepare-language", arguments), idempotency_key="workflow-restart-1", boot_id="dead-boot")

    assert store.reconcile_workflow_commands("new-boot") == 1
    recovered = store.get_workflow_command(command["command_id"])
    assert recovered["state"] == "outcome_unknown"
    assert "must be inspected" in recovered["error_text"]


@pytest.mark.oracle
def test_query_transition_freezes_exact_bytes_and_rejects_undeclared_jump(lattice_env):
    store = lattice_env["store"]
    registry = WorkflowRegistry(Path(__file__).parents[1] / "workflow_packs")
    created = store.create_workflow_project_atomic(title="query workflow", purpose="", workflow_pack="quaternary", initial_state="native-query-proposal", role_specs=[{"name": "worker", "authority": "proposal", "instructions": "work"}], blackboard_content=b"{}", blackboard_source_ref="fixture:state", slice_external_ref=None)
    project_id = created["project"]["project_id"]
    declared = registry.validate_transition("quaternary", "native-query-proposal", "frozen-query")
    receipt = store.advance_workflow(project_id=project_id, pack_id="quaternary", expected_revision=1, from_state="native-query-proposal", to_state="frozen-query", evidence_block_ids=[], payload={"query_bytes": "exakte deutsche Anfrage"}, trigger=declared["trigger_definition"])

    assert receipt["workflow"]["state"] == "frozen-query"
    frozen_ref = receipt["trigger"]["result_ref"]
    assert store.blobs.get(frozen_ref.removeprefix("lattice:blob/")) == "exakte deutsche Anfrage".encode()
    with pytest.raises(ValueError, match="not declared"):
        registry.validate_transition("quaternary", "frozen-query", "closure-candidate")


@pytest.mark.oracle
def test_negative_control_workflow_rejects_wrong_or_pre_state_evidence(lattice_env):
    store = lattice_env["store"]
    created = store.create_workflow_project_atomic(title="causal evidence", purpose="", workflow_pack="quaternary", initial_state="native-query-proposal", role_specs=[{"name": "worker", "authority": "proposal", "instructions": "work"}], blackboard_content=b'{"stage":"before"}', blackboard_source_ref="fixture:before", slice_external_ref=None)
    project_id = created["project"]["project_id"]
    store.advance_workflow(project_id=project_id, pack_id="quaternary", expected_revision=1, from_state="native-query-proposal", to_state="frozen-query", evidence_block_ids=[], payload={"query_bytes": "exact"}, trigger=None)
    operator_note = store.create_context_block(ContextBlockCreate(project_id=project_id, semantic_type="workflow-blackboard-snapshot", content="operator claim", authority="quaternary", evidence_level="authoritative-read"))
    requirement = [{"semantic_type": "workflow-blackboard-snapshot", "authority": "quaternary", "status": "active", "provenance": "current_workflow_blackboard", "minimum_count": 1, "after_current_state_entry": True}]

    with pytest.raises(Exception, match="typed evidence requirement"):
        store.advance_workflow(project_id=project_id, pack_id="quaternary", expected_revision=2, from_state="frozen-query", to_state="search-receipts", evidence_block_ids=[created["blackboard"]["block_id"]], payload={}, trigger=None, minimum_evidence_blocks=1, evidence_requirements=requirement)
    with pytest.raises(Exception, match="typed evidence requirement"):
        store.advance_workflow(project_id=project_id, pack_id="quaternary", expected_revision=2, from_state="frozen-query", to_state="search-receipts", evidence_block_ids=[operator_note["block_id"]], payload={}, trigger=None, minimum_evidence_blocks=1, evidence_requirements=requirement)

    current = store.refresh_workflow_blackboard(project_id, content=b'{"stage":"after"}', source_ref="fixture:after")
    accepted = store.advance_workflow(project_id=project_id, pack_id="quaternary", expected_revision=2, from_state="frozen-query", to_state="search-receipts", evidence_block_ids=[current["block_id"]], payload={}, trigger=None, minimum_evidence_blocks=1, evidence_requirements=requirement)
    assert accepted["workflow"]["state"] == "search-receipts"


@pytest.mark.concurrency
def test_workflow_consent_revision_cannot_reset_state_entry_freshness(lattice_env):
    store = lattice_env["store"]
    created = store.create_workflow_project_atomic(title="freshness", purpose="", workflow_pack="quaternary", initial_state="before", role_specs=[{"role_id": "sol", "name": "Orchestrator", "provider": "openai", "harness": "codex", "authority": "proposal", "instructions": "propose"}], blackboard_content=b"{}", blackboard_source_ref="fixture:state", slice_external_ref=None)
    project = created["project"]
    session = project["sessions"][0]
    draft = store.create_draft(DraftCreate(project_id=project["project_id"], role_session_id=session["role_session_id"], prompt="old evidence", route=lattice_env["route"]))
    invocation = lattice_env["lattice"].seal(draft["draft_id"])
    attempt = store.create_attempt(invocation["invocation_id"], "pre-state-output", "test-boot", 9_999_999_999)
    old_output = store.finish_successful_attempt(attempt["attempt_id"], response="old", artifacts=[], journal_path="fixture:journal", runtime_receipt={"fixture": True})["output_block"]
    store.advance_workflow(project_id=project["project_id"], pack_id="quaternary", expected_revision=1, from_state="before", to_state="after", evidence_block_ids=[], payload={}, trigger=None)
    store.set_workflow_automatic_grant(project["project_id"], expected_revision=2, trigger_name="fixture", grant={"trigger": "fixture"})

    with pytest.raises(Exception, match="role-bound terminal output"):
        store.advance_workflow(project_id=project["project_id"], pack_id="quaternary", expected_revision=3, from_state="after", to_state="next", evidence_block_ids=[old_output["block_id"]], payload={}, trigger=None, minimum_evidence_blocks=1, terminal_output_requirements=[{"workflow_role_key": "sol", "invocation_states": ["succeeded"], "minimum_count": 1, "after_current_state_entry": True}])


@pytest.mark.property
def test_context_module_modes_are_executable_and_required_context_is_not_removable(lattice_env):
    store = lattice_env["store"]
    modules = [
        {"definition": {"module_id": "project.weather", "version": 1, "mode": "role_default", "capabilities": [], "source_kind": "file", "configuration": {}}, "content": b"rain", "receipt": {"source_ref": "fixture:weather", "content_sha256": __import__("hashlib").sha256(b"rain").hexdigest()}},
        {"definition": {"module_id": "project.rules", "version": 1, "mode": "workflow_required", "capabilities": [], "source_kind": "file", "configuration": {}}, "content": b"hard boundary", "receipt": {"source_ref": "fixture:rules", "content_sha256": __import__("hashlib").sha256(b"hard boundary").hexdigest()}},
    ]
    created = store.create_workflow_project_atomic(title="modules", purpose="", workflow_pack="quaternary", initial_state="semantic-intent", role_specs=[{"name": "worker", "authority": "proposal", "instructions": "work"}], blackboard_content=b"{}", blackboard_source_ref="fixture:state", slice_external_ref=None, context_modules=modules)
    project = created["project"]
    role = project["roles"][0]
    session = project["sessions"][0]
    defaults = store.list_role_context_defaults(role["role_id"])
    draft = store.create_draft(DraftCreate(project_id=project["project_id"], role_session_id=session["role_session_id"], prompt="go", route=lattice_env["route"]))
    sealed = lattice_env["lattice"].seal(draft["draft_id"])
    labels = {segment["label"] for segment in sealed["manifest"]["segments"]}
    assert {"module:project.weather", "module:project.rules"} <= labels

    removable = next(item for item in defaults if item["mode"] == "role_default")
    required = next(item for item in defaults if item["mode"] == "workflow_required")
    store.remove_role_context_default(role["role_id"], removable["block_id"])
    with pytest.raises(Exception, match="cannot be removed"):
        store.remove_role_context_default(role["role_id"], required["block_id"])
    assert {item["module_id"] for item in store.list_role_context_defaults(role["role_id"])} == {"project.rules"}


@pytest.mark.contract
def test_platform_foundry_pack_reuses_workflow_kernel_without_gaining_octopus_write_authority():
    def handler(request: httpx.Request):
        return httpx.Response(200, json={"platform_id": "pinterest-com", "state": "shadow"}, request=request)

    adapter = PlatformFoundryAdapter("http://127.0.0.1:8000", transport=httpx.MockTransport(handler))
    receipt = adapter.inspect({"platform_id": "pinterest-com"})
    assert receipt["state"]["state"] == "shadow"
    assert receipt["content_sha256"]
    with pytest.raises(ValueError, match="remain owned by Octopus"):
        adapter.command("promote", {"candidate_id": "candidate"})
    with pytest.raises(ValueError, match="safe opaque"):
        adapter.inspect({"platform_id": "../operator-token"})

    registry = WorkflowRegistry(Path(__file__).parents[1] / "workflow_packs")
    assert registry.validate_transition("platform-foundry", "validating", "shadow")["to"] == "shadow"
    with pytest.raises(ValueError, match="not declared"):
        registry.validate_transition("platform-foundry", "observing", "promotable")


@pytest.mark.regression
def test_legacy_workflow_projects_receive_runtime_state_once_without_rewriting_domain_state(lattice_env):
    store = lattice_env["store"]
    project = store.create_project(ProjectCreate(title="legacy workflow", workflow_pack="quaternary"))

    assert store.reconcile_workflow_instances({"quaternary": "semantic-intent"}) == 1
    assert store.reconcile_workflow_instances({"quaternary": "semantic-intent"}) == 0
    workflow = store.get_workflow_instance(project["project_id"])
    assert workflow["state"] == "semantic-intent"
    assert workflow["revision"] == 1


@pytest.mark.regression
def test_legacy_workflow_roles_cannot_gain_model_authority_from_a_name_match(lattice_env):
    store = lattice_env["store"]
    project = store.create_project(ProjectCreate(title="legacy workflow", workflow_pack="quaternary"))
    role = store.create_role(project["project_id"], RoleCreate(name="Orchestrator", instructions="orchestrate"))
    loaded = WorkflowRegistry(Path(__file__).parents[1] / "workflow_packs").load("quaternary")
    store.reconcile_workflow_instances({"quaternary": loaded["definition"]["initial_state"]})
    contracts = {"quaternary": {"canonical_sha256": loaded["canonical_sha256"], "roles": loaded["definition"]["roles"]}}

    assert store.reconcile_workflow_role_policies(contracts) == 1
    assert store.reconcile_workflow_role_policies(contracts) == 0
    repaired = store.get_role(role["role_id"])
    assert repaired["workflow_role_key"] is None
    assert repaired["route_policy"] == {"kind": "non-model", "provider": "legacy-unbound"}
    with pytest.raises(Exception, match="not a model-execution role"):
        store.create_draft(DraftCreate(project_id=project["project_id"], role_session_id=store.create_role_session(RoleSessionCreate(project_id=project["project_id"], role_id=role["role_id"], label="legacy"))["role_session_id"], prompt="must not execute", route=lattice_env["route"]))


@pytest.mark.regression
def test_workflow_role_route_is_enforced_instead_of_treated_as_cockpit_decoration(lattice_env):
    store = lattice_env["store"]
    created = store.create_workflow_project_atomic(
        title="route-bound",
        purpose="",
        workflow_pack="quaternary",
        initial_state="semantic-intent",
        role_specs=[{"role_id": "native-partner", "name": "German native-language partner", "provider": "google", "harness": "gemini", "allowed_models": [], "authority": "language-proposal-only", "instructions": "Interpret German."}],
        blackboard_content=b"{}",
        blackboard_source_ref="fixture:state",
        slice_external_ref=None,
    )
    session = created["project"]["sessions"][0]
    with pytest.raises(Exception, match="bound to google through gemini"):
        store.create_draft(DraftCreate(project_id=created["project"]["project_id"], role_session_id=session["role_session_id"], prompt="pretend to be native", route=ProviderRoute(provider="openai", model="gpt-5.6-sol", harness="codex")))


@pytest.mark.regression
def test_workflow_role_route_cannot_be_changed_after_draft_creation_or_sealed_from_legacy_state(lattice_env):
    store = lattice_env["store"]
    created = store.create_workflow_project_atomic(
        title="route-bound",
        purpose="",
        workflow_pack="quaternary",
        initial_state="semantic-intent",
        role_specs=[{"role_id": "sol", "name": "Orchestrator", "provider": "openai", "harness": "codex", "allowed_models": [], "authority": "orchestration", "instructions": "Orchestrate."}],
        blackboard_content=b"{}",
        blackboard_source_ref="fixture:state",
        slice_external_ref=None,
    )
    project = created["project"]
    session = project["sessions"][0]
    draft = store.create_draft(DraftCreate(project_id=project["project_id"], role_session_id=session["role_session_id"], prompt="work", route=ProviderRoute(provider="openai", model="gpt-5.6-sol", harness="codex")))
    forbidden = ProviderRoute(provider="anthropic", model="claude-opus-4-1", harness="claude")

    with pytest.raises(Exception, match="bound to openai through codex"):
        store.patch_draft(draft["draft_id"], DraftPatch(expected_revision=1, route=forbidden))

    with store.transaction() as conn:
        conn.execute("UPDATE drafts SET route_json=? WHERE draft_id=?", (json.dumps(forbidden.model_dump(mode="json")), draft["draft_id"]))
    with pytest.raises(Exception, match="bound to openai through codex"):
        lattice_env["lattice"].seal(draft["draft_id"])


@pytest.mark.contract
def test_workflow_initial_state_and_trigger_binding_survive_transition_reordering():
    raw = json.loads((Path(__file__).parents[1] / "workflow_packs" / "quaternary" / "pack.json").read_text())
    raw["allowed_transitions"] = list(reversed(raw["allowed_transitions"]))
    definition = WorkflowPackDefinition.model_validate(raw)
    assert definition.initial_state == "semantic-intent"
    closure = next(item for item in definition.allowed_transitions if item.to_state == "closure-candidate")
    assert closure.trigger == "closure-proposed"

    broken = json.loads(json.dumps(raw))
    broken["allowed_transitions"].append({"from": "unreachable", "to": "island"})
    with pytest.raises(ValueError, match="reachable"):
        WorkflowPackDefinition.model_validate(broken)

    noncausal = json.loads(json.dumps(raw))
    gated = next(item for item in noncausal["allowed_transitions"] if item["to"] == "blinded-challenge")
    gated["requires_completed_trigger"] = "query-accepted"
    with pytest.raises(ValueError, match="transition that enters the gated state"):
        WorkflowPackDefinition.model_validate(noncausal)


@pytest.mark.regression
def test_terminal_workflow_output_sealed_before_state_entry_is_not_fresh(lattice_env):
    store = lattice_env["store"]
    registry = WorkflowRegistry(Path(__file__).parents[1] / "workflow_packs")
    pack_sha = registry.load("quaternary")["canonical_sha256"]
    roles = [
        {"role_id": "native-partner", "name": "Native", "provider": "openai", "harness": "codex", "authority": "proposal", "instructions": "Propose."},
        {"role_id": "sol", "name": "Sol", "provider": "openai", "harness": "codex", "authority": "proposal", "instructions": "Propose."},
    ]
    created = store.create_workflow_project_atomic(title="fresh cognition", purpose="", workflow_pack="quaternary", initial_state="terminology-amendment", role_specs=roles, blackboard_content=b"{}", blackboard_source_ref="fixture:state", slice_external_ref=None, pack_canonical_sha256=pack_sha)
    project = store.get_project(created["project"]["project_id"])
    sessions = {next(role["workflow_role_key"] for role in project["roles"] if role["role_id"] == session["role_id"]): session for session in project["sessions"]}

    stale_sol_draft = store.create_draft(DraftCreate(project_id=project["project_id"], role_session_id=sessions["sol"]["role_session_id"], prompt="prepared before countersearch", route=lattice_env["route"]))
    stale_sol = lattice_env["lattice"].seal(stale_sol_draft["draft_id"])
    native_draft = store.create_draft(DraftCreate(project_id=project["project_id"], role_session_id=sessions["native-partner"]["role_session_id"], prompt="amend terminology", route=lattice_env["route"]))
    native = lattice_env["lattice"].seal(native_draft["draft_id"])
    native_attempt = store.create_attempt(native["invocation_id"], "native-freshness", "test-boot", 9_999_999_999)
    store.mark_attempt_running(native_attempt["attempt_id"])
    native_done = store.finish_successful_attempt(native_attempt["attempt_id"], response="native amendment", artifacts=[], journal_path="fixture:journal", runtime_receipt={"fixture": True})
    first = registry.validate_transition("quaternary", "terminology-amendment", "countersearch")
    store.advance_workflow(project_id=project["project_id"], pack_id="quaternary", expected_revision=1, from_state="terminology-amendment", to_state="countersearch", evidence_block_ids=[native_done["output_block_id"]], payload={}, trigger=None, minimum_evidence_blocks=first["minimum_evidence_blocks"], minimum_terminal_invocation_outputs=first["minimum_terminal_invocation_outputs"], terminal_outputs_after_current_state_entry=first["terminal_outputs_after_current_state_entry"], terminal_output_requirements=first["terminal_output_requirements"], evidence_requirements=first["evidence_requirements"], expected_pack_canonical_sha256=pack_sha)

    stale_attempt = store.create_attempt(stale_sol["invocation_id"], "sol-stale-context", "test-boot", 9_999_999_999)
    store.mark_attempt_running(stale_attempt["attempt_id"])
    stale_done = store.finish_successful_attempt(stale_attempt["attempt_id"], response="closure proposal from old context", artifacts=[], journal_path="fixture:journal", runtime_receipt={"fixture": True})
    blackboard = store.refresh_workflow_blackboard(project["project_id"], content=b'{"state":"countersearch"}', source_ref="fixture:countersearch")
    second = registry.validate_transition("quaternary", "countersearch", "closure-candidate")

    with pytest.raises(Exception, match="exact terminal invocation output"):
        store.advance_workflow(project_id=project["project_id"], pack_id="quaternary", expected_revision=2, from_state="countersearch", to_state="closure-candidate", evidence_block_ids=[blackboard["block_id"], stale_done["output_block_id"]], payload={"closure_candidate_block_id": stale_done["output_block_id"]}, trigger=second["trigger_definition"], trigger_pack_sha256=pack_sha, minimum_evidence_blocks=second["minimum_evidence_blocks"], minimum_terminal_invocation_outputs=second["minimum_terminal_invocation_outputs"], terminal_outputs_after_current_state_entry=second["terminal_outputs_after_current_state_entry"], terminal_output_requirements=second["terminal_output_requirements"], evidence_requirements=second["evidence_requirements"], expected_pack_canonical_sha256=pack_sha)


def _planned_trigger_fixture(lattice_env):
    store = lattice_env["store"]
    registry = WorkflowRegistry(Path(__file__).parents[1] / "workflow_packs")
    pack_sha = registry.load("quaternary")["canonical_sha256"]
    created = store.create_workflow_project_atomic(title="trigger fixture", purpose="", workflow_pack="quaternary", initial_state="countersearch", role_specs=[{"role_id": "verifier", "name": "Verifier", "provider": "openai", "harness": "codex", "authority": "challenge-only", "instructions": "Challenge."}], blackboard_content=b"{}", blackboard_source_ref="fixture:state", slice_external_ref=None, pack_canonical_sha256=pack_sha)
    project = created["project"]
    declared = registry.validate_transition("quaternary", "countersearch", "closure-candidate")
    transition = store.advance_workflow(project_id=project["project_id"], pack_id="quaternary", expected_revision=1, from_state="countersearch", to_state="closure-candidate", evidence_block_ids=[], payload={}, trigger=declared["trigger_definition"], trigger_pack_sha256=pack_sha, expected_pack_canonical_sha256=pack_sha)
    session = project["sessions"][0]
    draft = store.create_draft(DraftCreate(project_id=project["project_id"], role_session_id=session["role_session_id"], prompt="challenge", route=lattice_env["route"]))
    invocation = lattice_env["lattice"].seal(draft["draft_id"])
    return transition["trigger"], invocation


@pytest.mark.regression
def test_trigger_rejects_invocation_from_wrong_workflow_role(lattice_env):
    store = lattice_env["store"]
    trigger, _unused = _planned_trigger_fixture(lattice_env)
    wrong_role = store.create_role(trigger["project_id"], RoleCreate(name="Wrong role", instructions="Do not verify."))
    wrong_session = store.create_role_session(RoleSessionCreate(project_id=trigger["project_id"], role_id=wrong_role["role_id"], label="wrong"))
    draft = store.create_draft(DraftCreate(project_id=trigger["project_id"], role_session_id=wrong_session["role_session_id"], prompt="claim verification", route=lattice_env["route"]))
    invocation = lattice_env["lattice"].seal(draft["draft_id"])

    with pytest.raises(Exception, match="requires role verifier"):
        store.plan_workflow_trigger(trigger["trigger_id"], result_ref=invocation["invocation_id"])
    assert store.get_workflow_trigger(trigger["trigger_id"])["state"] == "pending"


@pytest.mark.regression
def test_completed_trigger_is_bound_to_exact_current_state_entry(lattice_env):
    store = lattice_env["store"]
    trigger, invocation = _planned_trigger_fixture(lattice_env)
    store.plan_workflow_trigger(trigger["trigger_id"], result_ref=invocation["invocation_id"])
    attempt = store.create_attempt(invocation["invocation_id"], "trigger-entry-attempt", "test-boot", 9_999_999_999)
    store.mark_attempt_running(attempt["attempt_id"])
    finished = store.finish_successful_attempt(attempt["attempt_id"], response="independent result", artifacts=[], journal_path="fixture:journal", runtime_receipt={"fixture": True})
    pack_sha = WorkflowRegistry(Path(__file__).parents[1] / "workflow_packs").load("quaternary")["canonical_sha256"]

    with store.transaction() as conn:
        replacement_transition = "workflow_transition_reentry_fixture"
        conn.execute("INSERT INTO workflow_transitions(transition_id,project_id,pack_id,from_state,to_state,revision,evidence_block_ids_json,payload_sha256,pack_canonical_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (replacement_transition, trigger["project_id"], "quaternary", "countersearch", "closure-candidate", 99, "[]", "0" * 64, pack_sha, "fixture"))
        event_id = store._event(conn, project_id=trigger["project_id"], event_type="workflow.transitioned", subject_type="workflow-transition", subject_id=replacement_transition, payload={"from": "countersearch", "to": "closure-candidate", "revision": 99})
        state_entry_seq = conn.execute("SELECT seq FROM events WHERE event_id=?", (event_id,)).fetchone()[0]
        conn.execute("UPDATE workflow_instances SET revision=99,state_entry_seq=? WHERE project_id=?", (state_entry_seq, trigger["project_id"]))

    with pytest.raises(Exception, match="current state entry's trigger"):
        store.advance_workflow(project_id=trigger["project_id"], pack_id="quaternary", expected_revision=99, from_state="closure-candidate", to_state="blinded-challenge", evidence_block_ids=[finished["output_block_id"]], payload={}, trigger=None, requires_completed_trigger="closure-proposed", minimum_evidence_blocks=1, minimum_terminal_invocation_outputs=1, terminal_output_requirements=[{"workflow_role_key": "verifier", "invocation_states": ["succeeded"], "minimum_count": 1, "after_current_state_entry": False}], expected_pack_canonical_sha256=pack_sha)


@pytest.mark.concurrency
def test_negative_control_trigger_plan_and_lineage_edge_commit_atomically(monkeypatch, lattice_env):
    store = lattice_env["store"]
    trigger, invocation = _planned_trigger_fixture(lattice_env)

    def fail_edge(*_args, **_kwargs):
        raise OSError("planted lineage failure")

    monkeypatch.setattr(store, "_insert_edge", fail_edge)
    with pytest.raises(OSError, match="planted lineage failure"):
        store.plan_workflow_trigger(trigger["trigger_id"], result_ref=invocation["invocation_id"])

    recovered = store.get_workflow_trigger(trigger["trigger_id"])
    assert recovered["state"] == "pending"
    assert recovered["result_ref"] is None


@pytest.mark.concurrency
def test_negative_control_invocation_and_trigger_plan_are_one_transaction(monkeypatch, lattice_env):
    store = lattice_env["store"]
    trigger, _unused = _planned_trigger_fixture(lattice_env)
    session = store.get_project(trigger["project_id"])["sessions"][0]
    draft = store.create_draft(DraftCreate(project_id=trigger["project_id"], role_session_id=session["role_session_id"], prompt="atomic challenge", route=lattice_env["route"]))
    real_insert = store._insert_edge

    def fail_trigger_edge(conn, **kwargs):
        if kwargs.get("target_type") == "workflow-trigger":
            raise OSError("planted trigger lineage failure")
        return real_insert(conn, **kwargs)

    monkeypatch.setattr(store, "_insert_edge", fail_trigger_edge)
    with pytest.raises(OSError, match="planted trigger lineage failure"):
        lattice_env["lattice"].seal(draft["draft_id"], trigger_id=trigger["trigger_id"])

    recovered_draft = store.get_draft(draft["draft_id"])
    assert recovered_draft["state"] == "editable"
    assert recovered_draft["invocation_id"] is None
    assert store.get_workflow_trigger(trigger["trigger_id"])["state"] == "pending"


@pytest.mark.oracle
def test_oracle_failed_trigger_invocation_can_retry_to_completion(lattice_env):
    store = lattice_env["store"]
    trigger, invocation = _planned_trigger_fixture(lattice_env)
    store.plan_workflow_trigger(trigger["trigger_id"], result_ref=invocation["invocation_id"])
    first = store.create_attempt(invocation["invocation_id"], "trigger-failure-attempt", "test-boot", 9_999_999_999)
    store.mark_attempt_running(first["attempt_id"])
    store.finish_attempt(first["attempt_id"], state=AttemptState.failed, failure_kind="outage", error_text="planted provider outage")

    failed = store.get_workflow_trigger(trigger["trigger_id"])
    assert failed["state"] == "execution_failed"
    second = store.create_attempt(invocation["invocation_id"], "trigger-success-attempt", "test-boot", 9_999_999_999)
    store.mark_attempt_running(second["attempt_id"])
    store.finish_successful_attempt(second["attempt_id"], response="independent result", artifacts=[], journal_path="fixture:journal", runtime_receipt={"fixture": True})

    completed = store.get_workflow_trigger(trigger["trigger_id"])
    assert completed["state"] == "completed"
    assert completed["error_text"] is None
