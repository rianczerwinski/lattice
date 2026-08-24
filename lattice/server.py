"""Single-origin Lattice API and response-first cockpit host."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .config import WEB_DIST, LatticeConfig
from .canonical import canonical_bytes, opaque_id, sha256_bytes, strict_json_loads
from .contracts import (
    ActorBindingV1,
    ContextBlockCreate,
    ContextSelection,
    ContextModuleDefinition,
    DerivedTextBlockCreate,
    DraftCreate,
    DraftPatch,
    ExecuteRequest,
    ImportConversationRequest,
    InvocationBatchCreateV1,
    InvocationBatchPreviewV1,
    ProjectCreate,
    ProviderRoute,
    RoleCreate,
    RolePinCreate,
    RoleSessionCreate,
    SliceCreate,
    TagRequest,
    WorkflowTransitionRequest,
    WorkflowAutomationGrantRequest,
    EdgeRelation,
    EpisodeCreate,
    EpisodeMemberCreate,
)
from .octopus_import import OctopusImporter
from .context_modules import ContextModuleError, ContextModuleRunner, read_bounded_local_file
from .operations import Lattice
from .quaternary import QuaternaryAdapter, QuaternaryBoundaryError, QuaternaryOutcomeUnknown
from .platform_foundry import PlatformFoundryAdapter
from .security import OperatorBoundaryMiddleware, OperatorSessions, ServiceInstanceLock, configured_development_origin, ensure_cli_token
from .store import LatticeConflict, LatticeNotFound, LatticeStore
from .supervisor import ExecutionSupervisor
from .workflows import WorkflowRegistry


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ForkRequest(StrictRequest):
    role_session_id: str | None = None
    actor_binding: ActorBindingV1 | None = None
    prompt: str = Field(min_length=1)
    route: ProviderRoute


class JoinRequest(ForkRequest):
    invocation_ids: list[str] = Field(min_length=2, max_length=32)


class ReplayRequest(StrictRequest):
    prompt: str | None = None
    route: ProviderRoute | None = None


class SealDraftRequest(StrictRequest):
    expected_revision: int = Field(ge=1)
    expected_preflight_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExtractRequest(StrictRequest):
    project_id: str
    invocation_ids: list[str] = Field(min_length=1, max_length=1000)
    title: str = Field(min_length=1, max_length=300)


class MaterializeTemplateStepRequest(StrictRequest):
    expected_template_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_id: str
    role_session_id: str | None = None
    context_bindings: dict[str, str] = Field(default_factory=dict, max_length=1000)
    invocation_bindings: dict[str, str] = Field(default_factory=dict, max_length=1000)
    route: ProviderRoute | None = None


class BootstrapProjectRequest(StrictRequest):
    title: str = Field(min_length=1, max_length=300)
    purpose: str = ""
    role_name: str = "Operator"
    role_instructions: str = "Answer the current request using only the context Lattice explicitly supplies. Name material uncertainty and do not invent hidden state."


class ManualProjectRequest(StrictRequest):
    title: str = Field(min_length=1, max_length=300)
    purpose: str = Field(default="", max_length=20_000)


class WorkflowInspectRequest(StrictRequest):
    selector: dict[str, Any] = Field(default_factory=dict)


class WorkflowCommandRequest(StrictRequest):
    project_id: str
    expected_revision: int = Field(ge=1)
    operation: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=200)
    review_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class WorkflowCommandPreflightRequest(StrictRequest):
    project_id: str
    expected_revision: int = Field(ge=1)
    operation: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class WorkflowProjectRequest(StrictRequest):
    title: str = Field(min_length=1, max_length=300)
    purpose: str = ""
    selector: dict[str, Any] = Field(default_factory=dict)
    slice_external_ref: str | None = Field(default=None, min_length=1, max_length=500, pattern=r"^[^\x00-\x1f\x7f]+$")


class ResolveContextModuleRequest(StrictRequest):
    project_id: str
    definition: ContextModuleDefinition


def _initialize_runtime(config: LatticeConfig):
    """Acquire canonical ownership before opening or migrating any durable state."""
    cli_token = ensure_cli_token(config.operator_token)
    service_lock = ServiceInstanceLock(config.data_root / "service.lock")
    service_lock.acquire()
    try:
        store = LatticeStore(config)
        lattice = Lattice(store)
        supervisor = ExecutionSupervisor(store)
        sessions = OperatorSessions()
        importer = OctopusImporter(store, lattice, config.octopus_base_url)
        context_modules = ContextModuleRunner(allowed_file_roots=config.allowed_file_roots, octopus_base_url=config.octopus_base_url)
        pack_roots = list(config.workflow_pack_roots)
        if config.quaternary_root and (config.quaternary_root / "workflows").is_dir():
            pack_roots.append(config.quaternary_root / "workflows")
        workflows = WorkflowRegistry(pack_roots)
        if config.quaternary_root and (config.quaternary_root / "quaternary.sqlite3").is_file() and (config.quaternary_root / "tools" / "gatherctl.py").is_file():
            workflows.register("quaternary", QuaternaryAdapter(config.quaternary_root / "quaternary.sqlite3", config.quaternary_root / "tools" / "gatherctl.py", allowed_input_roots=config.allowed_file_roots, blob_reader=store.blobs.get, blob_writer=store.blobs.put))
        workflows.register("platform-foundry", PlatformFoundryAdapter(config.octopus_base_url))
        loaded_packs: dict[str, dict[str, Any]] = {}
        workflow_pack_failures = workflows.catalogue()["failures"]
        for pack_id in workflows.adapters:
            try:
                loaded_packs[pack_id] = workflows.load(pack_id)
            except (FileNotFoundError, OSError, ValueError) as error:
                if not any(failure["pack_id"] == pack_id for failure in workflow_pack_failures):
                    workflow_pack_failures.append({"pack_id": pack_id, "detail": str(error)})
    except BaseException:
        service_lock.release()
        raise
    return store, lattice, supervisor, sessions, cli_token, importer, context_modules, workflows, loaded_packs, workflow_pack_failures, service_lock


def create_app(config: LatticeConfig | None = None) -> FastAPI:
    config = config or LatticeConfig.from_environment()
    configured_development_origin()
    store, lattice, supervisor, sessions, cli_token, importer, context_modules, workflows, loaded_packs, workflow_pack_failures, service_lock = _initialize_runtime(config)

    def load_workflow_pack(pack_id: str) -> dict[str, Any]:
        try:
            return workflows.load(pack_id)
        except (FileNotFoundError, OSError, ValueError) as error:
            raise HTTPException(422, f"workflow pack is unavailable: {error}") from error

    async def refresh_workflow_state(pack_id: str, project_id: str) -> dict[str, Any]:
        instance, _loaded = require_current_workflow_instance(pack_id, project_id)
        state = await asyncio.to_thread(workflows.inspect, pack_id, instance["selector"])
        raw = canonical_bytes(state)
        block = store.refresh_workflow_blackboard(project_id, content=raw, source_ref=f"{pack_id}:state:{sha256_bytes(raw)}")
        return {"workflow": store.get_workflow_instance(project_id), "blackboard": block}

    def require_current_workflow_instance(pack_id: str, project_id: str, loaded: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        instance = store.get_workflow_instance(project_id)
        if instance["pack_id"] != pack_id:
            raise LatticeConflict("project is not governed by this workflow pack")
        loaded = loaded or load_workflow_pack(pack_id)
        if instance.get("pack_canonical_sha256") != loaded["canonical_sha256"]:
            raise LatticeConflict("workflow pack generation changed; preserve this history and open a current-generation workflow project")
        return instance, loaded

    def prepare_workflow_command(pack_id: str, project_id: str, expected_revision: int, operation: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[tuple[str, bytes]]]:
        instance, loaded = require_current_workflow_instance(pack_id, project_id)
        if instance["revision"] != expected_revision:
            raise LatticeConflict("workflow state changed before command review")
        resolved = workflows.resolve_command(pack_id, operation, arguments)
        command_definition = next(item for item in loaded["definition"]["commands"] if item["operation"] == operation)
        state_basis = None
        if command_definition["authority"] != "read":
            blackboard_id = instance.get("current_blackboard_block_id")
            if not blackboard_id:
                raise LatticeConflict("workflow command lacks an authoritative state snapshot")
            blackboard = store.get_block(blackboard_id)
            try:
                recorded_state = strict_json_loads(blackboard["content"])
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
                raise LatticeConflict("workflow authoritative state snapshot is not exact JSON") from error
            fresh_state = workflows.inspect(pack_id, instance["selector"])
            recorded_revision = recorded_state.get("state_sha256") if isinstance(recorded_state, dict) else None
            fresh_revision = fresh_state.get("state_sha256") if isinstance(fresh_state, dict) else None
            if not isinstance(recorded_revision, str) or not isinstance(fresh_revision, str):
                raise LatticeConflict("workflow adapter does not expose a stable state revision for controlled commands")
            if recorded_revision != fresh_revision:
                raise LatticeConflict("authoritative workflow state changed; refresh it before reviewing this command")
            state_basis = {"blackboard_block_id": blackboard_id, "blackboard_sha256": blackboard["content_sha256"], "adapter_state_sha256": fresh_revision}
        prepared = dict(resolved)
        file_receipts = []
        file_blobs: list[tuple[str, bytes]] = []
        for argument in command_definition["arguments"]:
            if argument["value_type"] != "file" or argument["key"] not in resolved:
                continue
            source, content = read_bounded_local_file(str(resolved[argument["key"]]), allowed_roots=config.allowed_file_roots, max_bytes=argument["max_bytes"] or 64 * 1024 * 1024)
            digest = sha256_bytes(content)
            prepared[argument["key"]] = f"lattice:blob/{digest}"
            file_receipts.append({"argument": argument["key"], "source_ref": f"file:{source}", "content_sha256": digest, "byte_count": len(content)})
            file_blobs.append((digest, content))
        review = {"kind": "lattice-workflow-command-review-v1", "project_id": project_id, "workflow_revision": expected_revision, "pack_id": pack_id, "pack_canonical_sha256": loaded["canonical_sha256"], "operation": operation, "authority": command_definition["authority"], "state_basis": state_basis, "requested_arguments_sha256": sha256_bytes(canonical_bytes(resolved)), "prepared_arguments_sha256": sha256_bytes(canonical_bytes(prepared)), "file_receipts": file_receipts}
        review["review_sha256"] = sha256_bytes(canonical_bytes(review))
        return prepared, review, command_definition, file_blobs

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            await asyncio.to_thread(store.reconcile_conversation_imports, importer.boot_id)
            await asyncio.to_thread(store.reconcile_workflow_instances, {pack_id: loaded["definition"]["initial_state"] for pack_id, loaded in loaded_packs.items()})
            await asyncio.to_thread(store.reconcile_workflow_role_policies, {pack_id: {"canonical_sha256": loaded["canonical_sha256"], "roles": loaded["definition"]["roles"]} for pack_id, loaded in loaded_packs.items()})
            await supervisor.start()
            try:
                yield
            finally:
                await supervisor.stop()
        finally:
            service_lock.release()

    app = FastAPI(title="Lattice", version="0.1.0", lifespan=lifespan)
    app.state.config = config
    app.state.store = store
    app.state.lattice = lattice
    app.state.supervisor = supervisor
    app.state.workflows = workflows
    app.add_middleware(OperatorBoundaryMiddleware, cli_token=cli_token, sessions=sessions, port=config.port, public_url=config.public_url, frame_origins=config.frame_origins)

    @app.exception_handler(LatticeNotFound)
    async def missing(_request: Request, error: LatticeNotFound):
        return Response(json.dumps({"detail": f"not found: {error.args[0]}"}), status_code=404, media_type="application/json")

    @app.exception_handler(LatticeConflict)
    async def conflict(_request: Request, error: LatticeConflict):
        return Response(json.dumps({"detail": str(error)}), status_code=409, media_type="application/json")

    @app.exception_handler(QuaternaryBoundaryError)
    async def quaternary_boundary(_request: Request, error: QuaternaryBoundaryError):
        return Response(json.dumps({"detail": str(error)}), status_code=422, media_type="application/json")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        import importlib.metadata
        try:
            version = importlib.metadata.version("lattice")
        except importlib.metadata.PackageNotFoundError:
            version = "0.1.0+source"
        return {"status": "ok", "service": "lattice", "boot_id": supervisor.boot_id, "port": config.port, "data_root": str(config.data_root.resolve()), "version": version, "runtime_id": config.runtime_id, "startup_recovery_failures": supervisor.startup_recovery_failures, "workflow_pack_failures": workflow_pack_failures}

    @app.get("/api/session")
    async def create_session(response: Response) -> dict[str, str]:
        session, csrf = sessions.issue()
        response.set_cookie("lattice_operator", session, httponly=True, samesite="strict", secure=bool(config.public_url and config.public_url.startswith("https://")), path="/", max_age=sessions.ttl_seconds)
        return {"csrf": csrf}

    @app.get("/api/v1/routes")
    async def routes(probe: bool = Query(default=False)) -> list[dict[str, Any]]:
        from axis_model_runner import installed_adapter_status
        if probe:
            raise HTTPException(status_code=405, detail="provider probes require an authenticated POST")
        return await asyncio.to_thread(installed_adapter_status, probe_versions=False)

    @app.post("/api/v1/routes/probe")
    async def probe_routes() -> list[dict[str, Any]]:
        from axis_model_runner import installed_adapter_status
        return await asyncio.to_thread(installed_adapter_status, probe_versions=True)

    async def resolve_trigger_route(project_id: str, action: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        project = store.get_project(project_id)
        role = next((item for item in project["roles"] if item["workflow_role_key"] == action["role_id"]), None)
        if role is None:
            raise LatticeConflict(f"workflow trigger role is unavailable: {action['role_id']}")
        session = next(item for item in project["sessions"] if item["role_id"] == role["role_id"])
        policy = role["route_policy"]
        if policy["kind"] != "model":
            raise LatticeConflict("automatic invocation trigger requires a model-bound role")
        from axis_model_runner import installed_adapter_status
        statuses = await asyncio.to_thread(installed_adapter_status, probe_versions=False)
        route_status = next((item for item in statuses if item["qualified"] and item["provider_code"] == policy["provider"] and item["official_cli"] == policy["harness"] and (not policy["allowed_models"] or item["model_code"] in policy["allowed_models"])), None)
        if route_status is None:
            raise LatticeConflict(f"qualified {policy['provider']} trigger route is unavailable")
        return session, policy, route_status

    def materialize_trigger_invocation(*, project_id: str, trigger: dict[str, Any], action: dict[str, Any], evidence_block_ids: list[str], trigger_plan: tuple[dict[str, Any], dict[str, Any], dict[str, Any]]) -> dict[str, Any]:
        session, policy, route_status = trigger_plan
        selections = [ContextSelection(block_id=block_id, plane="knowledge", label=f"independent-evidence:{index}") for index, block_id in enumerate(evidence_block_ids)]
        draft = store.create_draft(DraftCreate(project_id=project_id, role_session_id=session["role_session_id"], prompt=action["prompt"], route=ProviderRoute(provider=policy["provider"], model=route_status["model_code"], harness=policy["harness"], effort="high"), selections=selections, relation=EdgeRelation.triggered_by))
        return lattice.seal(draft["draft_id"], trigger_id=trigger["trigger_id"])

    async def dispatch_trigger_invocation(invocation_id: str, trigger_id: str, *, retry: bool = False) -> dict[str, Any]:
        idempotency_key = opaque_id("trigger_retry") if retry else f"workflow-trigger:{trigger_id}"
        return await supervisor.execute(invocation_id, ExecuteRequest(idempotency_key=idempotency_key))

    @app.post("/api/v1/projects")
    async def create_project(value: ManualProjectRequest):
        return store.create_project(ProjectCreate(title=value.title, purpose=value.purpose))

    @app.post("/api/v1/projects/bootstrap")
    async def bootstrap_project(value: BootstrapProjectRequest):
        project = store.create_project(ProjectCreate(title=value.title, purpose=value.purpose))
        role = store.create_role(project["project_id"], RoleCreate(name=value.role_name, instructions=value.role_instructions))
        session = store.create_role_session(RoleSessionCreate(project_id=project["project_id"], role_id=role["role_id"], label=f"{value.role_name} spine"))
        return {"project": project, "role": role, "session": session}

    @app.get("/api/v1/projects")
    async def list_projects():
        return store.list_projects()

    @app.get("/api/v1/projects/{project_id}")
    async def get_project(project_id: str):
        project = store.get_project(project_id)
        page = store.page_invocations(project_id, limit=250)
        project["invocations"] = page["items"]
        project["invocation_page"] = {key: page[key] for key in ("has_older", "next_before")}
        return project

    @app.get("/api/v1/projects/{project_id}/invocations")
    async def project_invocations(
        project_id: str,
        limit: int = Query(default=250, ge=1, le=500),
        before_created_at: str | None = Query(default=None, min_length=15, max_length=15, pattern=r"^\d{8}T\d{6}$"),
        before_invocation_id: str | None = Query(default=None, min_length=1, max_length=96, pattern=r"^[A-Za-z0-9_:-]+$"),
    ):
        return store.page_invocations(project_id, limit=limit, before_created_at=before_created_at, before_invocation_id=before_invocation_id)

    @app.get("/api/v1/projects/{project_id}/trailheads")
    async def project_trailheads(
        project_id: str,
        limit: int = Query(default=50, ge=1, le=500),
        before_created_at: str | None = Query(default=None, min_length=15, max_length=15, pattern=r"^\d{8}T\d{6}$"),
        before_invocation_id: str | None = Query(default=None, min_length=1, max_length=96, pattern=r"^[A-Za-z0-9_:-]+$"),
    ):
        return store.page_trailheads(project_id, limit=limit, before_created_at=before_created_at, before_invocation_id=before_invocation_id)

    @app.get("/api/v1/projects/{project_id}/axial-projection")
    async def axial_projection(project_id: str):
        return store.project_enduring_graph(project_id)

    @app.post("/api/v1/projects/{project_id}/roles")
    async def create_role(project_id: str, value: RoleCreate):
        return store.create_role(project_id, value)

    @app.post("/api/v1/invocations/{invocation_id}/roles")
    async def pin_invocation_as_role(invocation_id: str, value: RolePinCreate):
        return store.pin_invocation_as_role(invocation_id, value)

    @app.post("/api/v1/role-sessions")
    async def create_role_session(value: RoleSessionCreate):
        return store.create_role_session(value)

    @app.post("/api/v1/context-blocks")
    async def create_context_block(value: ContextBlockCreate):
        return store.create_context_block(value)

    @app.post("/api/v1/context-blocks/excerpts")
    async def create_context_excerpt(value: DerivedTextBlockCreate):
        return store.create_derived_text_block(value)

    @app.get("/api/v1/context-blocks/{block_id}")
    async def get_context_block(block_id: str):
        return store.get_block(block_id)

    @app.post("/api/v1/drafts")
    async def create_draft(value: DraftCreate):
        return lattice.invoke(value)

    @app.get("/api/v1/drafts/{draft_id}")
    async def get_draft(draft_id: str):
        return store.get_draft(draft_id)

    @app.get("/api/v1/drafts/{draft_id}/preview")
    async def preview_draft(draft_id: str):
        draft = store.get_draft(draft_id)
        preflight, prompt = lattice.compositor.compile(draft, preview=True)
        parent_deltas = []
        for parent_id in draft["parents"]:
            parent = store.get_invocation(parent_id)
            parent_deltas.append({"invocation_id": parent_id, "manifest_sha256": parent["manifest_sha256"], **lattice.context_delta(parent["manifest"], preflight)})
        return {"draft_id": draft_id, "revision": draft["revision"], "preflight": preflight, "parent_deltas": parent_deltas, "compiled_prompt_preview": prompt.decode("utf-8")}

    @app.patch("/api/v1/drafts/{draft_id}")
    async def patch_draft(draft_id: str, value: DraftPatch):
        return store.patch_draft(draft_id, value)

    @app.post("/api/v1/drafts/{draft_id}/seal")
    async def seal_draft(draft_id: str, value: SealDraftRequest):
        return lattice.seal(draft_id, expected_revision=value.expected_revision, expected_preflight_sha256=value.expected_preflight_sha256)

    @app.post("/api/v1/invocation-batches/preview")
    async def preview_invocation_batch(value: InvocationBatchPreviewV1):
        return lattice.preview_invocation_batch(value)

    @app.post("/api/v1/invocation-batches", status_code=202)
    async def create_invocation_batch(value: InvocationBatchCreateV1):
        batch = lattice.create_invocation_batch(value)
        attempts = []
        for member in batch["members"]:
            attempts.append(await supervisor.execute(member["invocation_id"], ExecuteRequest(idempotency_key=f"batch:{value.batch_id}:{member['spec_id']}")))
        return {**store.get_invocation_batch(value.batch_id), "execution_attempts": attempts}

    @app.get("/api/v1/invocation-batches/{batch_id}")
    async def get_invocation_batch(batch_id: str):
        return store.get_invocation_batch(batch_id)

    @app.post("/api/v1/invocations/{invocation_id}/forks")
    async def fork(invocation_id: str, value: ForkRequest):
        return lattice.fork(invocation_id, role_session_id=value.role_session_id, actor_binding=value.actor_binding, prompt=value.prompt, route=value.route)

    @app.post("/api/v1/joins")
    async def join(value: JoinRequest):
        return lattice.join(value.invocation_ids, role_session_id=value.role_session_id, actor_binding=value.actor_binding, prompt=value.prompt, route=value.route)

    @app.post("/api/v1/invocations/{invocation_id}/replays")
    async def replay(invocation_id: str, value: ReplayRequest):
        return lattice.replay(invocation_id, prompt=value.prompt, route=value.route)

    @app.post("/api/v1/invocations/{invocation_id}/execute", status_code=202)
    async def execute(invocation_id: str, value: ExecuteRequest):
        return await supervisor.execute(invocation_id, value)

    @app.post("/api/v1/attempts/{attempt_id}/cancel", status_code=202)
    async def cancel(attempt_id: str):
        return await supervisor.cancel(attempt_id)

    @app.get("/api/v1/invocations/{invocation_id}")
    async def inspect(invocation_id: str):
        return lattice.inspect(invocation_id)

    @app.get("/api/v1/artifacts/{artifact_id}")
    async def inspect_artifact(artifact_id: str):
        return store.get_artifact(artifact_id)

    @app.post("/api/v1/artifacts/{artifact_id}/context-blocks")
    async def graft_artifact(artifact_id: str):
        return store.create_artifact_context_block(artifact_id)

    @app.get("/api/v1/invocations/{invocation_id}/neighborhood")
    async def neighborhood(
        invocation_id: str,
        depth: int = Query(default=2, ge=0, le=5),
        max_nodes: int = Query(default=LatticeStore.NEIGHBORHOOD_DEFAULT_MAX_NODES, ge=1, le=LatticeStore.NEIGHBORHOOD_MAX_NODES),
        max_edges: int = Query(default=LatticeStore.NEIGHBORHOOD_DEFAULT_MAX_EDGES, ge=1, le=LatticeStore.NEIGHBORHOOD_MAX_EDGES),
    ):
        return store.graph_neighborhood(invocation_id, depth, max_nodes=max_nodes, max_edges=max_edges)

    @app.get("/api/v1/diff")
    async def diff(left: str, right: str):
        return lattice.diff(left, right)

    @app.post("/api/v1/invocations/{invocation_id}/tags")
    async def tag(invocation_id: str, value: TagRequest):
        return lattice.tag(invocation_id, value)

    @app.post("/api/v1/context-blocks/{block_id}/tags")
    async def tag_context_block(block_id: str, value: TagRequest):
        return store.tag_context_block(block_id, value)

    @app.post("/api/v1/slices")
    async def create_slice(value: SliceCreate):
        return store.create_slice(value)

    @app.post("/api/v1/episodes")
    async def create_episode(value: EpisodeCreate):
        return store.create_episode(value)

    @app.get("/api/v1/projects/{project_id}/episodes")
    async def project_episodes(project_id: str):
        return store.list_project_episodes(project_id)

    @app.get("/api/v1/invocations/{invocation_id}/episodes")
    async def invocation_episodes(invocation_id: str):
        return store.list_invocation_episodes(invocation_id)

    @app.get("/api/v1/invocations/{invocation_id}/navigation")
    async def invocation_navigation(invocation_id: str):
        return store.invocation_navigation(invocation_id)

    @app.post("/api/v1/episodes/{episode_id}/members")
    async def add_episode_member(episode_id: str, value: EpisodeMemberCreate):
        return store.add_episode_member(episode_id, value)

    @app.get("/api/v1/slices/{slice_id}")
    async def get_slice(
        slice_id: str,
        limit: int = Query(default=LatticeStore.SLICE_DEFAULT_LIMIT, ge=1, le=LatticeStore.SLICE_MAX_LIMIT),
        after_created_at: str | None = Query(default=None, min_length=1, max_length=100),
        after_object_type: str | None = Query(default=None, min_length=1, max_length=30),
        after_object_id: str | None = Query(default=None, min_length=1, max_length=200),
        snapshot_event_seq: int | None = Query(default=None, ge=1),
    ):
        return store.get_slice(
            slice_id,
            limit=limit,
            after_created_at=after_created_at,
            after_object_type=after_object_type,
            after_object_id=after_object_id,
            snapshot_event_seq=snapshot_event_seq,
        )

    @app.get("/api/v1/slices/by-external-ref/{external_ref:path}")
    async def slice_by_external_ref(
        external_ref: str,
        limit: int = Query(default=LatticeStore.SLICE_DEFAULT_LIMIT, ge=1, le=LatticeStore.SLICE_MAX_LIMIT),
        after_created_at: str | None = Query(default=None, min_length=1, max_length=100),
        after_object_type: str | None = Query(default=None, min_length=1, max_length=30),
        after_object_id: str | None = Query(default=None, min_length=1, max_length=200),
        snapshot_event_seq: int | None = Query(default=None, ge=1),
    ):
        value = store.get_slice_by_external_ref(
            external_ref,
            limit=limit,
            after_created_at=after_created_at,
            after_object_type=after_object_type,
            after_object_id=after_object_id,
            snapshot_event_seq=snapshot_event_seq,
        )
        if value is None:
            raise HTTPException(404, "slice not found")
        return value

    @app.post("/api/v1/workflows/extract")
    async def extract(value: ExtractRequest):
        return lattice.extract(value.project_id, value.invocation_ids, value.title)

    @app.post("/api/v1/workflow-templates/{artifact_id}/steps/{step_id}/drafts")
    async def materialize_template_step(artifact_id: str, step_id: str, value: MaterializeTemplateStepRequest):
        return lattice.materialize_template_step(artifact_id, step_id, expected_template_sha256=value.expected_template_sha256, project_id=value.project_id, role_session_id=value.role_session_id, context_bindings=value.context_bindings, invocation_bindings=value.invocation_bindings, route=value.route)

    @app.get("/api/v1/workflow-packs/{pack_id}")
    async def workflow_pack(pack_id: str):
        return load_workflow_pack(pack_id)

    @app.get("/api/v1/workflow-packs")
    async def workflow_packs():
        return workflows.catalogue()

    @app.post("/api/v1/workflow-packs/{pack_id}/projects")
    async def create_workflow_project(pack_id: str, value: WorkflowProjectRequest):
        loaded = load_workflow_pack(pack_id)
        definition = loaded["definition"]
        try:
            selector = workflows.resolve_selector(pack_id, value.selector)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        pack_definition_sha256 = store.blobs.put(canonical_bytes(definition))
        if pack_definition_sha256 != loaded["canonical_sha256"]:
            raise OSError("workflow pack canonical identity changed during installation")
        try:
            state = await asyncio.to_thread(workflows.inspect, pack_id, selector)
        except (ValueError, QuaternaryBoundaryError, FileNotFoundError, sqlite3.Error) as error:
            raise HTTPException(422, str(error)) from error
        state_bytes = canonical_bytes(state)
        resolved_modules = []
        for module_value in definition["context_modules"]:
            definition_model = ContextModuleDefinition.model_validate(module_value)
            if definition_model.mode.value == "manual":
                continue
            try:
                resolved = await asyncio.to_thread(context_modules.resolve, definition_model)
            except ContextModuleError as error:
                raise HTTPException(422, f"required workflow context failed: {definition_model.module_id}: {error}") from error
            resolved_modules.append({"definition": definition_model.model_dump(mode="json"), "content": resolved.content, "receipt": resolved.receipt})
        role_specs = []
        for role_spec in definition["roles"]:
            instructions = role_spec.get("instructions") or f"Act only as {role_spec['name']} inside the {definition['title']} workflow. Your authority is {role_spec['authority']}. Treat model outputs as proposals unless the authoritative workflow explicitly accepts them."
            role_specs.append({**role_spec, "instructions": instructions})
        return store.create_workflow_project_atomic(title=value.title, purpose=value.purpose, workflow_pack=pack_id, initial_state=definition["initial_state"], role_specs=role_specs, blackboard_content=state_bytes, blackboard_source_ref=f"{pack_id}:state:{sha256_bytes(state_bytes)}", slice_external_ref=value.slice_external_ref, selector=selector, context_modules=resolved_modules, pack_canonical_sha256=loaded["canonical_sha256"], pack_definition_sha256=pack_definition_sha256)

    @app.post("/api/v1/workflow-packs/{pack_id}/inspect")
    async def inspect_workflow(pack_id: str, value: WorkflowInspectRequest):
        try:
            return await asyncio.to_thread(workflows.inspect, pack_id, value.selector)
        except (ValueError, QuaternaryBoundaryError, FileNotFoundError, sqlite3.Error) as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/v1/workflow-packs/{pack_id}/commands")
    async def command_workflow(pack_id: str, value: WorkflowCommandRequest):
        prior = store.get_workflow_command_by_idempotency(value.idempotency_key)
        if prior is not None:
            try:
                retried_arguments = workflows.resolve_command(pack_id, value.operation, value.arguments)
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
            expected_request_sha = sha256_bytes(canonical_bytes(retried_arguments))
            if prior["project_id"] != value.project_id or prior["pack_id"] != pack_id or prior["operation"] != value.operation or prior["expected_workflow_revision"] != value.expected_revision or prior["review"].get("requested_arguments_sha256") != expected_request_sha or (value.review_sha256 is not None and prior["review_sha256"] != value.review_sha256):
                raise LatticeConflict("workflow idempotency key was reused for another command")
            return {"command": store.get_workflow_command(prior["command_id"], include_result=True), "blackboard_state": "not-refreshed-idempotent-replay"}
        try:
            prepared_arguments, review, command_definition, file_blobs = await asyncio.to_thread(prepare_workflow_command, pack_id, value.project_id, value.expected_revision, value.operation, value.arguments)
        except (ValueError, ContextModuleError) as error:
            raise HTTPException(422, str(error)) from error
        if command_definition["authority"] != "read" and value.review_sha256 != review["review_sha256"]:
            raise LatticeConflict("workflow command bytes or authority changed after review")
        for digest, content in file_blobs:
            if store.blobs.put(content) != digest:
                raise OSError("workflow command file identity changed during binding")
        command = store.claim_workflow_command(project_id=value.project_id, pack_id=pack_id, expected_workflow_revision=value.expected_revision, pack_canonical_sha256=review["pack_canonical_sha256"], operation=value.operation, arguments=prepared_arguments, review=review, idempotency_key=value.idempotency_key, boot_id=supervisor.boot_id)
        if command["state"] != "running" or command["owner_boot_id"] != supervisor.boot_id:
            return {"command": store.get_workflow_command(command["command_id"], include_result=True), "blackboard_state": "not-refreshed-idempotent-replay"}
        try:
            result = await asyncio.to_thread(workflows.command, pack_id, value.operation, prepared_arguments)
            completed = store.complete_workflow_command(command["command_id"], result=result)
            try:
                refreshed = await refresh_workflow_state(pack_id, value.project_id)
            except BaseException as refresh_error:
                store.record_event(project_id=value.project_id, event_type="workflow.blackboard-refresh-failed", subject_type="workflow-command", subject_id=command["command_id"], payload={"error": f"{type(refresh_error).__name__}: {refresh_error}"})
                return {"command": completed, "blackboard_state": "request_failure", "blackboard_error": f"{type(refresh_error).__name__}: {refresh_error}"}
            return {"command": completed, "blackboard_state": "data", **refreshed}
        except QuaternaryOutcomeUnknown as error:
            store.fail_workflow_command(command["command_id"], error_text=str(error), outcome_unknown=True)
            raise HTTPException(409, str(error)) from error
        except (ValueError, QuaternaryBoundaryError) as error:
            store.fail_workflow_command(command["command_id"], error_text=str(error))
            raise HTTPException(422, str(error)) from error
        except BaseException as error:
            store.fail_workflow_command(command["command_id"], error_text=f"{type(error).__name__}: {error}", outcome_unknown=True)
            raise

    @app.post("/api/v1/workflow-packs/{pack_id}/commands/preflight")
    async def preflight_workflow_command(pack_id: str, value: WorkflowCommandPreflightRequest):
        try:
            _prepared, review, _definition, _file_blobs = await asyncio.to_thread(prepare_workflow_command, pack_id, value.project_id, value.expected_revision, value.operation, value.arguments)
            return review
        except (ValueError, ContextModuleError) as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/api/v1/workflow-commands/{command_id}")
    async def inspect_workflow_command(command_id: str):
        return store.get_workflow_command(command_id, include_result=True)

    @app.get("/api/v1/workflow-projects/{project_id}")
    async def inspect_workflow_project(project_id: str):
        return store.get_workflow_instance(project_id)

    @app.post("/api/v1/workflow-packs/{pack_id}/projects/{project_id}/refresh")
    async def refresh_workflow_project(pack_id: str, project_id: str):
        try:
            return await refresh_workflow_state(pack_id, project_id)
        except (ValueError, QuaternaryBoundaryError, FileNotFoundError, sqlite3.Error) as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/v1/workflow-packs/{pack_id}/transitions")
    async def transition_workflow(pack_id: str, value: WorkflowTransitionRequest):
        try:
            loaded = load_workflow_pack(pack_id)
            require_current_workflow_instance(pack_id, value.project_id, loaded)
            declared = workflows.validate_transition(pack_id, value.from_state, value.to_state)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        trigger_definition = declared["trigger_definition"]
        if len(set(value.evidence_block_ids)) != len(value.evidence_block_ids):
            raise HTTPException(422, "workflow evidence block IDs must be unique")
        if len(value.evidence_block_ids) < declared["minimum_evidence_blocks"]:
            raise HTTPException(422, f"workflow transition requires at least {declared['minimum_evidence_blocks']} evidence block(s)")
        if declared["maximum_evidence_blocks"] is not None and len(value.evidence_block_ids) > declared["maximum_evidence_blocks"]:
            raise HTTPException(422, f"workflow transition permits at most {declared['maximum_evidence_blocks']} evidence block(s)")
        action = trigger_definition["action"] if trigger_definition else None
        excluded_block_id = None
        if action and action["type"] == "invoke_role" and action.get("exclude_payload_block_field"):
            field = action["exclude_payload_block_field"]
            excluded_block_id = value.payload.get(field)
            if not isinstance(excluded_block_id, str):
                raise HTTPException(422, f"workflow trigger requires block payload field {field}")
            excluded = store.get_block(excluded_block_id, include_content=False)
            if excluded["project_id"] != value.project_id:
                raise HTTPException(409, "excluded trigger context belongs to another project")
            if excluded_block_id in value.evidence_block_ids:
                raise HTTPException(422, "trigger evidence cannot include its explicitly excluded block")
        trigger_plan = None
        workflow = store.get_workflow_instance(value.project_id)
        automatic_grant = None
        if trigger_definition:
            action_sha = sha256_bytes(canonical_bytes(trigger_definition["action"]))
            automatic_grant = next((grant for grant in workflow["automatic_grants"] if isinstance(grant, dict) and grant.get("trigger") == trigger_definition["trigger"] and grant.get("pack_canonical_sha256") == loaded["canonical_sha256"] and grant.get("action_sha256") == action_sha), None)
        if trigger_definition and trigger_definition["automatic"] and automatic_grant and action["type"] == "invoke_role":
            trigger_plan = await resolve_trigger_route(value.project_id, action)
        receipt = store.advance_workflow(project_id=value.project_id, pack_id=pack_id, expected_revision=value.expected_revision, from_state=value.from_state, to_state=value.to_state, evidence_block_ids=value.evidence_block_ids, payload=value.payload, trigger=trigger_definition, requires_completed_trigger=declared.get("requires_completed_trigger"), trigger_pack_sha256=loaded["canonical_sha256"] if trigger_definition else None, minimum_evidence_blocks=declared["minimum_evidence_blocks"], maximum_evidence_blocks=declared["maximum_evidence_blocks"], minimum_terminal_invocation_outputs=declared["minimum_terminal_invocation_outputs"], terminal_outputs_after_current_state_entry=declared["terminal_outputs_after_current_state_entry"], terminal_output_requirements=declared["terminal_output_requirements"], evidence_requirements=declared["evidence_requirements"], expected_pack_canonical_sha256=loaded["canonical_sha256"])
        trigger = receipt.get("trigger")
        if trigger and trigger_definition and trigger_definition["automatic"] and not automatic_grant and action["type"] == "invoke_role":
            receipt["activation_required"] = True
        if trigger and trigger_plan:
            try:
                invocation = materialize_trigger_invocation(project_id=value.project_id, trigger=trigger, action=action, evidence_block_ids=value.evidence_block_ids, trigger_plan=trigger_plan)
                receipt["challenger_invocation"] = invocation
            except BaseException as error:
                trigger_error = f"{type(error).__name__}: {error}"
                store.fail_workflow_trigger_planning(trigger["trigger_id"], error_text=trigger_error)
                receipt["trigger"] = store.get_workflow_trigger(trigger["trigger_id"])
                receipt["trigger_error"] = trigger_error
            else:
                try:
                    receipt["challenger_attempt"] = await dispatch_trigger_invocation(invocation["invocation_id"], trigger["trigger_id"])
                except BaseException as error:
                    trigger_error = f"{type(error).__name__}: {error}"
                    store.record_workflow_trigger_dispatch_failure(trigger["trigger_id"], error_text=trigger_error)
                    receipt["trigger"] = store.get_workflow_trigger(trigger["trigger_id"])
                    receipt["trigger_error"] = trigger_error
        return receipt

    @app.put("/api/v1/workflow-packs/{pack_id}/projects/{project_id}/automatic-grants/{trigger_name}")
    async def set_automatic_grant(pack_id: str, project_id: str, trigger_name: str, value: WorkflowAutomationGrantRequest):
        loaded = load_workflow_pack(pack_id)
        require_current_workflow_instance(pack_id, project_id, loaded)
        if value.pack_canonical_sha256 != loaded["canonical_sha256"]:
            raise LatticeConflict("workflow pack changed before automation consent")
        trigger = next((item for item in loaded["definition"]["triggers"] if item["trigger"] == trigger_name and item["automatic"]), None)
        if trigger is None:
            raise HTTPException(422, "workflow trigger is not declared automatic")
        grant = None
        if value.enabled:
            grant = {"trigger": trigger_name, "pack_canonical_sha256": loaded["canonical_sha256"], "action_sha256": sha256_bytes(canonical_bytes(trigger["action"]))}
        return store.set_workflow_automatic_grant(project_id, expected_revision=value.expected_revision, trigger_name=trigger_name, grant=grant)

    @app.post("/api/v1/workflow-triggers/{trigger_id}/retry")
    async def retry_workflow_trigger(trigger_id: str):
        trigger = store.get_workflow_trigger(trigger_id)
        if trigger["action"] is None or trigger["action"]["type"] != "invoke_role":
            raise LatticeConflict("workflow trigger has no retryable invocation")
        current_pack = load_workflow_pack(trigger["pack_id"])
        instance = store.get_workflow_instance(trigger["project_id"])
        if trigger["pack_canonical_sha256"] != current_pack["canonical_sha256"] or instance["pack_canonical_sha256"] != trigger["pack_canonical_sha256"]:
            raise LatticeConflict("workflow pack changed after this trigger was created; remodel the transition instead of reinterpreting it")
        if instance["state"] != trigger["to_state"]:
            raise LatticeConflict("workflow has moved beyond this trigger's causal state; remodel instead of dispatching stale work")
        if trigger["state"] == "pending":
            try:
                trigger_plan = await resolve_trigger_route(trigger["project_id"], trigger["action"])
                invocation = materialize_trigger_invocation(project_id=trigger["project_id"], trigger=trigger, action=trigger["action"], evidence_block_ids=trigger["evidence_block_ids"], trigger_plan=trigger_plan)
            except BaseException as error:
                trigger_error = f"{type(error).__name__}: {error}"
                store.fail_workflow_trigger_planning(trigger_id, error_text=trigger_error)
                raise HTTPException(409, trigger_error) from error
        elif trigger["state"] in {"planned", "execution_failed"} and trigger.get("result_ref"):
            invocation = store.get_invocation(trigger["result_ref"])
        else:
            raise LatticeConflict("workflow trigger is not retryable")
        try:
            attempt = await dispatch_trigger_invocation(invocation["invocation_id"], trigger_id, retry=True)
            return {"trigger": store.get_workflow_trigger(trigger_id), "challenger_invocation": invocation, "challenger_attempt": attempt}
        except BaseException as error:
            trigger_error = f"{type(error).__name__}: {error}"
            store.record_workflow_trigger_dispatch_failure(trigger_id, error_text=trigger_error)
            raise HTTPException(409, trigger_error) from error

    @app.post("/api/v1/import/octopus")
    async def import_octopus(value: ImportConversationRequest):
        return await asyncio.to_thread(importer.import_conversation, project_id=value.project_id, conversation_id=value.conversation_id, provider=value.provider)

    @app.get("/api/v1/context-modules")
    async def context_module_catalogue():
        return context_modules.catalogue()

    @app.post("/api/v1/context-modules/resolve")
    async def resolve_context_module(value: ResolveContextModuleRequest):
        if value.definition.mode.value in {"role_default", "workflow_required"}:
            raise HTTPException(422, f"{value.definition.mode.value} is a governed workflow-pack mode and cannot be installed through the manual context surface")
        try:
            result = await asyncio.to_thread(context_modules.resolve, value.definition)
        except ContextModuleError as error:
            receipt = context_modules.failure_receipt(value.definition, error)
            store.record_event(project_id=value.project_id, event_type="context-module.failed", subject_type="context-module", subject_id=value.definition.module_id, payload={"receipt": receipt})
            raise HTTPException(422, str(error)) from error
        block = store.create_context_block(ContextBlockCreate(project_id=value.project_id, semantic_type=f"context-module:{value.definition.module_id}", content=result.content.decode("utf-8"), source_ref=result.receipt["source_ref"], source_sha256=result.receipt["content_sha256"], authority=value.definition.module_id, evidence_level="module-receipt", sensitivity="private", transformation=result.receipt))
        default = None
        if value.definition.mode.value in {"project_default", "workflow_required"}:
            default = store.set_project_context_default(value.project_id, block["block_id"], module_id=value.definition.module_id, mode=value.definition.mode.value)
        return {"block": block, "receipt": result.receipt, "project_default": default}

    @app.delete("/api/v1/projects/{project_id}/context-defaults/{module_id}", status_code=204)
    async def remove_project_context_default(project_id: str, module_id: str):
        store.remove_project_context_default(project_id, module_id)
        return Response(status_code=204)

    @app.delete("/api/v1/roles/{role_id}/context-defaults/{block_id}", status_code=204)
    async def remove_context_default(role_id: str, block_id: str):
        store.remove_role_context_default(role_id, block_id)
        return Response(status_code=204)

    @app.get("/api/v1/projects/{project_id}/events")
    async def events(project_id: str, after: int = 0):
        return store.events_after(project_id, after)

    @app.get("/api/v1/projects/{project_id}/events/stream")
    async def event_stream(project_id: str, after: int = 0):
        async def generate():
            cursor = after
            while True:
                batch = store.events_after(project_id, cursor)
                if batch:
                    for event in batch:
                        cursor = event["seq"]
                        yield f"id: {cursor}\nevent: {event['event_type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                else:
                    yield ": keepalive\n\n"
                await asyncio.sleep(1)
        return StreamingResponse(generate(), media_type="text/event-stream")

    dist = WEB_DIST
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}")
    async def cockpit(path: str):
        if path == "api" or path.startswith("api/"):
            return Response(json.dumps({"detail": "API route not found"}), status_code=404, media_type="application/json")
        index = dist / "index.html"
        if index.is_file():
            return FileResponse(index)
        return HTMLResponse("<main><h1>Lattice API is running</h1><p>Build <code>web/</code> to install the cockpit.</p></main>")

    return app
    DerivedTextBlockCreate,
    InvocationBatchCreateV1,
    InvocationBatchPreviewV1,
