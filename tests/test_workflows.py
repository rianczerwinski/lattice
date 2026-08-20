from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
import httpx

from loom.context_modules import ContextModuleError, ContextModuleRunner
from loom.contracts import ContextModuleDefinition, ContextModuleMode, DraftCreate, ProjectCreate
from loom.quaternary import QuaternaryAdapter, QuaternaryBoundaryError
from loom.platform_foundry import PlatformFoundryAdapter
from loom.workflows import WorkflowRegistry


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
    assert database.read_bytes() == before


@pytest.mark.regression
def test_quaternary_boundary_rejects_arbitrary_commands_and_arguments(tmp_path: Path):
    adapter = QuaternaryAdapter(tmp_path / "db", tmp_path / "gatherctl.py")
    with pytest.raises(QuaternaryBoundaryError, match="unsupported"):
        adapter.command("execute-arbitrary-python", {})

    def planted_runner(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"ok": True}), stderr="")

    adapter = QuaternaryAdapter(tmp_path / "db", tmp_path / "gatherctl.py", runner=planted_runner)
    with pytest.raises(QuaternaryBoundaryError, match="undeclared argument"):
        adapter.command("language-status", {"shell": "rm"})


@pytest.mark.oracle
def test_workflow_pack_has_blinded_challenger_and_deterministic_authority():
    root = Path(__file__).parents[1] / "workflow_packs"
    registry = WorkflowRegistry(root)
    pack = registry.load("quaternary")["definition"]
    assert any(role["role_id"] == "verifier" and role["authority"] == "challenge-only" for role in pack["roles"])
    assert {trigger["trigger"] for trigger in pack["triggers"]} == {"closure-proposed", "query-accepted"}
    assert pack["closure_contract"]["self_certification_forbidden"] is True


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

    outside = tmp_path / "secret"
    outside.write_text("no", encoding="utf-8")
    planted_escape = definition.model_copy(update={"configuration": {"path": str(outside)}})
    with pytest.raises(ContextModuleError, match="outside"):
        runner.resolve(planted_escape)


@pytest.mark.regression
def test_workflow_project_bootstrap_is_atomic(loom_env):
    store = loom_env["store"]
    before = len(store.list_projects())
    duplicated_roles = [
        {"name": "same", "authority": "proposal", "instructions": "one"},
        {"name": "same", "authority": "proposal", "instructions": "two"},
    ]
    with pytest.raises(sqlite3.IntegrityError):
        store.create_workflow_project_atomic(title="must roll back", purpose="negative control", workflow_pack="quaternary", initial_state="semantic-intent", role_specs=duplicated_roles, blackboard_content=b"{}", blackboard_source_ref="fixture:state", slice_external_ref="slate:atomic-negative")
    assert len(store.list_projects()) == before


@pytest.mark.contract
def test_workflow_command_is_idempotent_and_preserves_exact_result(loom_env):
    store = loom_env["store"]
    project = store.create_project(ProjectCreate(title="workflow", workflow_pack="quaternary"))
    first = store.claim_workflow_command(project_id=project["project_id"], pack_id="quaternary", operation="language-status", arguments={"language": "de"}, idempotency_key="workflow-idempotency-1", boot_id="boot-a")
    same = store.claim_workflow_command(project_id=project["project_id"], pack_id="quaternary", operation="language-status", arguments={"language": "de"}, idempotency_key="workflow-idempotency-1", boot_id="boot-a")
    assert same["command_id"] == first["command_id"]

    completed = store.complete_workflow_command(first["command_id"], result={"receipt": {"exit_code": 0}, "output": {"state": "calibrating"}, "stdout": "exact bytes", "stderr": ""})
    assert completed["result"]["stdout"] == "exact bytes"
    with pytest.raises(Exception, match="terminal"):
        store.complete_workflow_command(first["command_id"], result={"receipt": {}, "output": {}})


@pytest.mark.concurrency
def test_restart_marks_dispatched_workflow_outcome_unknown_instead_of_retrying(loom_env):
    store = loom_env["store"]
    project = store.create_project(ProjectCreate(title="workflow", workflow_pack="quaternary"))
    command = store.claim_workflow_command(project_id=project["project_id"], pack_id="quaternary", operation="prepare-language", arguments={"language": "de"}, idempotency_key="workflow-restart-1", boot_id="dead-boot")

    assert store.reconcile_workflow_commands("new-boot") == 1
    recovered = store.get_workflow_command(command["command_id"])
    assert recovered["state"] == "outcome_unknown"
    assert "must be inspected" in recovered["error_text"]


@pytest.mark.oracle
def test_query_transition_freezes_exact_bytes_and_rejects_undeclared_jump(loom_env):
    store = loom_env["store"]
    registry = WorkflowRegistry(Path(__file__).parents[1] / "workflow_packs")
    created = store.create_workflow_project_atomic(title="query workflow", purpose="", workflow_pack="quaternary", initial_state="native-query-proposal", role_specs=[{"name": "worker", "authority": "proposal", "instructions": "work"}], blackboard_content=b"{}", blackboard_source_ref="fixture:state", slice_external_ref=None)
    project_id = created["project"]["project_id"]
    declared = registry.validate_transition("quaternary", "native-query-proposal", "frozen-query")
    receipt = store.advance_workflow(project_id=project_id, pack_id="quaternary", expected_revision=1, from_state="native-query-proposal", to_state="frozen-query", evidence_block_ids=[], payload={"query_bytes": "exakte deutsche Anfrage"}, trigger=declared["trigger"])

    assert receipt["workflow"]["state"] == "frozen-query"
    frozen_ref = receipt["trigger"]["result_ref"]
    assert store.blobs.get(frozen_ref.removeprefix("loom:blob/")) == "exakte deutsche Anfrage".encode()
    with pytest.raises(ValueError, match="not declared"):
        registry.validate_transition("quaternary", "frozen-query", "closure-candidate")


@pytest.mark.property
def test_context_module_modes_are_executable_and_required_context_is_not_removable(loom_env):
    store = loom_env["store"]
    modules = [
        {"definition": {"module_id": "project.weather", "version": 1, "mode": "role_default", "capabilities": [], "source_kind": "file", "configuration": {}}, "content": b"rain", "receipt": {"source_ref": "fixture:weather", "content_sha256": __import__("hashlib").sha256(b"rain").hexdigest()}},
        {"definition": {"module_id": "project.rules", "version": 1, "mode": "workflow_required", "capabilities": [], "source_kind": "file", "configuration": {}}, "content": b"hard boundary", "receipt": {"source_ref": "fixture:rules", "content_sha256": __import__("hashlib").sha256(b"hard boundary").hexdigest()}},
    ]
    created = store.create_workflow_project_atomic(title="modules", purpose="", workflow_pack="quaternary", initial_state="semantic-intent", role_specs=[{"name": "worker", "authority": "proposal", "instructions": "work"}], blackboard_content=b"{}", blackboard_source_ref="fixture:state", slice_external_ref=None, context_modules=modules)
    project = created["project"]
    role = project["roles"][0]
    session = project["sessions"][0]
    defaults = store.list_role_context_defaults(role["role_id"])
    draft = store.create_draft(DraftCreate(project_id=project["project_id"], role_session_id=session["role_session_id"], prompt="go", route=loom_env["route"]))
    sealed = loom_env["loom"].seal(draft["draft_id"])
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
    def getter(url: str, **_kwargs):
        return httpx.Response(200, json={"platform_id": "pinterest-com", "state": "shadow"}, request=httpx.Request("GET", url))

    adapter = PlatformFoundryAdapter("http://127.0.0.1:8000", getter=getter)
    receipt = adapter.inspect({"platform_id": "pinterest-com"})
    assert receipt["state"]["state"] == "shadow"
    assert receipt["content_sha256"]
    with pytest.raises(ValueError, match="remain owned by Octopus"):
        adapter.command("promote", {"candidate_id": "candidate"})

    registry = WorkflowRegistry(Path(__file__).parents[1] / "workflow_packs")
    assert registry.validate_transition("platform-foundry", "validating", "shadow")["to"] == "shadow"
    with pytest.raises(ValueError, match="not declared"):
        registry.validate_transition("platform-foundry", "observing", "promotable")


@pytest.mark.regression
def test_legacy_workflow_projects_receive_runtime_state_once_without_rewriting_domain_state(loom_env):
    store = loom_env["store"]
    project = store.create_project(ProjectCreate(title="legacy workflow", workflow_pack="quaternary"))

    assert store.reconcile_workflow_instances({"quaternary": "semantic-intent"}) == 1
    assert store.reconcile_workflow_instances({"quaternary": "semantic-intent"}) == 0
    workflow = store.get_workflow_instance(project["project_id"])
    assert workflow["state"] == "semantic-intent"
    assert workflow["revision"] == 1
