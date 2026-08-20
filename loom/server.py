"""Single-origin Loom API and response-first cockpit host."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .config import AXIS_ROOT, WEB_DIST, WORKFLOW_PACK_ROOT, LoomConfig
from .canonical import canonical_bytes, sha256_bytes
from .contracts import (
    ContextBlockCreate,
    ContextSelection,
    ContextModuleDefinition,
    DraftCreate,
    DraftPatch,
    ExecuteRequest,
    ImportConversationRequest,
    ProjectCreate,
    ProviderRoute,
    RoleCreate,
    RoleSessionCreate,
    SliceCreate,
    TagRequest,
    WorkflowTransitionRequest,
    EdgeRelation,
)
from .octopus_import import OctopusImporter
from .context_modules import ContextModuleError, ContextModuleRunner
from .operations import Loom
from .quaternary import QuaternaryAdapter, QuaternaryBoundaryError
from .platform_foundry import PlatformFoundryAdapter
from .security import OperatorBoundaryMiddleware, OperatorSessions, ensure_cli_token
from .store import LoomConflict, LoomNotFound, LoomStore
from .supervisor import ExecutionSupervisor
from .workflows import WorkflowRegistry


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ForkRequest(StrictRequest):
    role_session_id: str
    prompt: str = Field(min_length=1)
    route: ProviderRoute


class JoinRequest(ForkRequest):
    invocation_ids: list[str] = Field(min_length=2, max_length=32)


class ReplayRequest(StrictRequest):
    prompt: str | None = None
    route: ProviderRoute | None = None


class ExtractRequest(StrictRequest):
    project_id: str
    invocation_ids: list[str] = Field(min_length=1)
    title: str = Field(min_length=1)


class BootstrapProjectRequest(StrictRequest):
    title: str = Field(min_length=1, max_length=300)
    purpose: str = ""
    role_name: str = "Operator"
    role_instructions: str = "Answer the current request using only the context Loom explicitly supplies. Name material uncertainty and do not invent hidden state."


class WorkflowInspectRequest(StrictRequest):
    selector: dict[str, Any] = Field(default_factory=dict)


class WorkflowCommandRequest(StrictRequest):
    project_id: str
    operation: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=200)


class WorkflowProjectRequest(StrictRequest):
    title: str = Field(min_length=1, max_length=300)
    purpose: str = ""
    selector: dict[str, Any] = Field(default_factory=dict)
    slice_external_ref: str | None = None


class ResolveContextModuleRequest(StrictRequest):
    project_id: str
    definition: ContextModuleDefinition


def create_app(config: LoomConfig | None = None) -> FastAPI:
    config = config or LoomConfig.from_environment()
    store = LoomStore(config)
    loom = Loom(store)
    supervisor = ExecutionSupervisor(store)
    sessions = OperatorSessions()
    cli_token = ensure_cli_token(config.operator_token)
    importer = OctopusImporter(store, loom, config.octopus_base_url)
    context_modules = ContextModuleRunner(allowed_file_roots=(AXIS_ROOT,), octopus_base_url=config.octopus_base_url)
    workflows = WorkflowRegistry(WORKFLOW_PACK_ROOT)
    workflows.register("quaternary", QuaternaryAdapter(config.quaternary_root / "quaternary.sqlite3", config.quaternary_root / "tools" / "gatherctl.py"))
    workflows.register("platform-foundry", PlatformFoundryAdapter(config.octopus_base_url))
    store.reconcile_workflow_instances({pack_id: workflows.load(pack_id)["definition"]["allowed_transitions"][0]["from"] for pack_id in workflows.adapters})

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await supervisor.start()
        try:
            yield
        finally:
            await supervisor.stop()

    app = FastAPI(title="Loom", version="0.1.0", lifespan=lifespan)
    app.state.config = config
    app.state.store = store
    app.state.loom = loom
    app.state.supervisor = supervisor
    app.add_middleware(OperatorBoundaryMiddleware, cli_token=cli_token, sessions=sessions, port=config.port)

    @app.exception_handler(LoomNotFound)
    async def missing(_request: Request, error: LoomNotFound):
        return Response(json.dumps({"detail": f"not found: {error.args[0]}"}), status_code=404, media_type="application/json")

    @app.exception_handler(LoomConflict)
    async def conflict(_request: Request, error: LoomConflict):
        return Response(json.dumps({"detail": str(error)}), status_code=409, media_type="application/json")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "service": "loom", "boot_id": supervisor.boot_id}

    @app.get("/api/session")
    async def create_session(response: Response) -> dict[str, str]:
        session, csrf = sessions.issue()
        response.set_cookie("loom_operator", session, httponly=True, samesite="strict", secure=False, path="/")
        return {"csrf": csrf}

    @app.get("/api/v1/routes")
    async def routes() -> list[dict[str, Any]]:
        from axis_model_runner import installed_adapter_status
        return installed_adapter_status()

    @app.post("/api/v1/projects")
    async def create_project(value: ProjectCreate):
        return store.create_project(value)

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
        project["invocations"] = store.list_invocations(project_id)
        return project

    @app.post("/api/v1/projects/{project_id}/roles")
    async def create_role(project_id: str, value: RoleCreate):
        return store.create_role(project_id, value)

    @app.post("/api/v1/role-sessions")
    async def create_role_session(value: RoleSessionCreate):
        return store.create_role_session(value)

    @app.post("/api/v1/context-blocks")
    async def create_context_block(value: ContextBlockCreate):
        return store.create_context_block(value)

    @app.get("/api/v1/context-blocks/{block_id}")
    async def get_context_block(block_id: str):
        return store.get_block(block_id)

    @app.post("/api/v1/drafts")
    async def create_draft(value: DraftCreate):
        return loom.invoke(value)

    @app.get("/api/v1/drafts/{draft_id}")
    async def get_draft(draft_id: str):
        return store.get_draft(draft_id)

    @app.get("/api/v1/drafts/{draft_id}/preview")
    async def preview_draft(draft_id: str):
        draft = store.get_draft(draft_id)
        manifest, prompt = loom.compositor.compile(draft)
        return {"draft_id": draft_id, "revision": draft["revision"], "manifest": manifest, "compiled_prompt_preview": prompt.decode("utf-8")}

    @app.patch("/api/v1/drafts/{draft_id}")
    async def patch_draft(draft_id: str, value: DraftPatch):
        return store.patch_draft(draft_id, value)

    @app.post("/api/v1/drafts/{draft_id}/seal")
    async def seal_draft(draft_id: str):
        return loom.seal(draft_id)

    @app.post("/api/v1/invocations/{invocation_id}/forks")
    async def fork(invocation_id: str, value: ForkRequest):
        return loom.fork(invocation_id, role_session_id=value.role_session_id, prompt=value.prompt, route=value.route)

    @app.post("/api/v1/joins")
    async def join(value: JoinRequest):
        return loom.join(value.invocation_ids, role_session_id=value.role_session_id, prompt=value.prompt, route=value.route)

    @app.post("/api/v1/invocations/{invocation_id}/replays")
    async def replay(invocation_id: str, value: ReplayRequest):
        return loom.replay(invocation_id, prompt=value.prompt, route=value.route)

    @app.post("/api/v1/invocations/{invocation_id}/execute", status_code=202)
    async def execute(invocation_id: str, value: ExecuteRequest):
        return await supervisor.execute(invocation_id, value)

    @app.post("/api/v1/attempts/{attempt_id}/cancel", status_code=202)
    async def cancel(attempt_id: str):
        return await supervisor.cancel(attempt_id)

    @app.get("/api/v1/invocations/{invocation_id}")
    async def inspect(invocation_id: str):
        return loom.inspect(invocation_id)

    @app.get("/api/v1/invocations/{invocation_id}/neighborhood")
    async def neighborhood(invocation_id: str, depth: int = 2):
        return store.graph_neighborhood(invocation_id, depth)

    @app.get("/api/v1/diff")
    async def diff(left: str, right: str):
        return loom.diff(left, right)

    @app.post("/api/v1/invocations/{invocation_id}/tags")
    async def tag(invocation_id: str, value: TagRequest):
        return loom.tag(invocation_id, value)

    @app.post("/api/v1/slices")
    async def create_slice(value: SliceCreate):
        return store.create_slice(value)

    @app.get("/api/v1/slices/{slice_id}")
    async def get_slice(slice_id: str):
        return store.get_slice(slice_id)

    @app.get("/api/v1/slices/by-external-ref/{external_ref:path}")
    async def slice_by_external_ref(external_ref: str):
        value = store.get_slice_by_external_ref(external_ref)
        if value is None:
            raise HTTPException(404, "slice not found")
        return value

    @app.post("/api/v1/workflows/extract")
    async def extract(value: ExtractRequest):
        return loom.extract(value.project_id, value.invocation_ids, value.title)

    @app.get("/api/v1/workflow-packs/{pack_id}")
    async def workflow_pack(pack_id: str):
        return workflows.load(pack_id)

    @app.post("/api/v1/workflow-packs/{pack_id}/projects")
    async def create_workflow_project(pack_id: str, value: WorkflowProjectRequest):
        loaded = workflows.load(pack_id)
        definition = loaded["definition"]
        state = await asyncio.to_thread(workflows.inspect, pack_id, value.selector)
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
        transitions = definition["allowed_transitions"]
        if not transitions:
            raise HTTPException(422, "workflow pack has no initial state")
        return store.create_workflow_project_atomic(title=value.title, purpose=value.purpose, workflow_pack=pack_id, initial_state=transitions[0]["from"], role_specs=role_specs, blackboard_content=state_bytes, blackboard_source_ref=f"{pack_id}:state:{value.selector}", slice_external_ref=value.slice_external_ref, context_modules=resolved_modules)

    @app.post("/api/v1/workflow-packs/{pack_id}/inspect")
    async def inspect_workflow(pack_id: str, value: WorkflowInspectRequest):
        try:
            return await asyncio.to_thread(workflows.inspect, pack_id, value.selector)
        except (ValueError, FileNotFoundError, sqlite3.Error) as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/v1/workflow-packs/{pack_id}/commands")
    async def command_workflow(pack_id: str, value: WorkflowCommandRequest):
        command = store.claim_workflow_command(project_id=value.project_id, pack_id=pack_id, operation=value.operation, arguments=value.arguments, idempotency_key=value.idempotency_key, boot_id=supervisor.boot_id)
        if command["state"] != "running" or command["owner_boot_id"] != supervisor.boot_id:
            return store.get_workflow_command(command["command_id"], include_result=True)
        try:
            result = await asyncio.to_thread(workflows.command, pack_id, value.operation, value.arguments)
            return store.complete_workflow_command(command["command_id"], result=result)
        except (ValueError, QuaternaryBoundaryError) as error:
            store.fail_workflow_command(command["command_id"], error_text=str(error))
            raise HTTPException(422, str(error)) from error
        except BaseException as error:
            store.fail_workflow_command(command["command_id"], error_text=f"{type(error).__name__}: {error}", outcome_unknown=True)
            raise

    @app.get("/api/v1/workflow-commands/{command_id}")
    async def inspect_workflow_command(command_id: str):
        return store.get_workflow_command(command_id, include_result=True)

    @app.get("/api/v1/workflow-projects/{project_id}")
    async def inspect_workflow_project(project_id: str):
        return store.get_workflow_instance(project_id)

    @app.post("/api/v1/workflow-packs/{pack_id}/transitions")
    async def transition_workflow(pack_id: str, value: WorkflowTransitionRequest):
        try:
            declared = workflows.validate_transition(pack_id, value.from_state, value.to_state)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        if value.to_state == "closure-candidate":
            candidate_block_id = value.payload.get("closure_candidate_block_id")
            if not isinstance(candidate_block_id, str):
                raise HTTPException(422, "closure transition requires a closure_candidate_block_id")
            candidate = store.get_block(candidate_block_id, include_content=False)
            if candidate["project_id"] != value.project_id:
                raise HTTPException(409, "closure candidate belongs to another project")
            if candidate_block_id in value.evidence_block_ids:
                raise HTTPException(422, "blinded challenger evidence cannot include the proposer conclusion")
        receipt = store.advance_workflow(project_id=value.project_id, pack_id=pack_id, expected_revision=value.expected_revision, from_state=value.from_state, to_state=value.to_state, evidence_block_ids=value.evidence_block_ids, payload=value.payload, trigger=declared["trigger"])
        trigger = receipt.get("trigger")
        if trigger and trigger["trigger_name"] == "closure-proposed":
            project = store.get_project(value.project_id)
            verifier = next((role for role in project["roles"] if role["name"] == "Blinded closure challenger"), None)
            if verifier is None:
                raise LoomConflict("workflow has no blinded challenger role")
            session = next(item for item in project["sessions"] if item["role_id"] == verifier["role_id"])
            route_status = next((item for item in __import__("axis_model_runner", fromlist=["DEFAULT_ADAPTERS"]).DEFAULT_ADAPTERS if item.provider_code == "openai"), None)
            if route_status is None:
                raise LoomConflict("Codex challenger route is unavailable")
            selections = [ContextSelection(block_id=block_id, plane="knowledge", label=f"independent-evidence:{index}") for index, block_id in enumerate(value.evidence_block_ids)]
            draft = store.create_draft(DraftCreate(project_id=value.project_id, role_session_id=session["role_session_id"], prompt="Independently recompute whether bounded closure is supported by the supplied base evidence. Do not infer or reproduce the proposer’s conclusion. Return falsifiers, remaining gaps, and the narrowest closure statement justified.", route=ProviderRoute(provider="openai", model=route_status.model_code, harness="codex", effort="high"), selections=selections, relation=EdgeRelation.triggered_by))
            invocation = loom.seal(draft["draft_id"])
            store.complete_workflow_trigger(trigger["trigger_id"], result_ref=invocation["invocation_id"])
            store.add_edge(project_id=value.project_id, relation=EdgeRelation.triggered_by, source_type="invocation", source_id=invocation["invocation_id"], source_revision=invocation["manifest_sha256"], target_type="workflow-trigger", target_id=trigger["trigger_id"], target_revision=trigger["source_transition_id"], metadata={"blinded": True})
            receipt["challenger_invocation"] = invocation
        return receipt

    @app.post("/api/v1/import/octopus")
    async def import_octopus(value: ImportConversationRequest):
        return await asyncio.to_thread(importer.import_conversation, project_id=value.project_id, conversation_id=value.conversation_id, provider=value.provider)

    @app.post("/api/v1/context-modules/resolve")
    async def resolve_context_module(value: ResolveContextModuleRequest):
        try:
            result = await asyncio.to_thread(context_modules.resolve, value.definition)
        except ContextModuleError as error:
            raise HTTPException(422, str(error)) from error
        block = store.create_context_block(ContextBlockCreate(project_id=value.project_id, semantic_type=f"context-module:{value.definition.module_id}", content=result.content.decode("utf-8"), source_ref=result.receipt["source_ref"], source_sha256=result.receipt["content_sha256"], authority=value.definition.module_id, evidence_level="module-receipt", sensitivity="private", transformation=result.receipt))
        return {"block": block, "receipt": result.receipt}

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
        index = dist / "index.html"
        if index.is_file():
            return FileResponse(index)
        return HTMLResponse("<main><h1>Loom API is running</h1><p>Build <code>web/</code> to install the cockpit.</p></main>")

    return app
