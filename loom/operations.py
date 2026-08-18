"""Conversational graph operations over Loom's storage and context compositor."""

from __future__ import annotations

import json
from typing import Any

from .canonical import canonical_bytes, generated_object, opaque_id, sha256_bytes, sha256_json
from .compositor import ContextCompositor
from .contracts import (
    ContextSelection,
    DraftCreate,
    EdgeRelation,
    ProviderRoute,
    SliceCreate,
    TagRequest,
)
from .manifest_reader import verify_and_reconstruct
from .store import LoomConflict, LoomStore


class Loom:
    def __init__(self, store: LoomStore) -> None:
        self.store = store
        self.compositor = ContextCompositor(store)

    def invoke(self, value: DraftCreate) -> dict[str, Any]:
        return self.store.create_draft(value)

    def fork(
        self,
        invocation_id: str,
        *,
        role_session_id: str,
        prompt: str,
        route: ProviderRoute,
        selections: list[ContextSelection] | None = None,
    ) -> dict[str, Any]:
        parent = self.store.get_invocation(invocation_id)
        return self.store.create_draft(DraftCreate(project_id=parent["project_id"], role_session_id=role_session_id, prompt=prompt, route=route, selections=selections or [], parent_invocation_ids=[invocation_id], relation=EdgeRelation.inherited_context_from))

    def join(
        self,
        invocation_ids: list[str],
        *,
        role_session_id: str,
        prompt: str,
        route: ProviderRoute,
        selections: list[ContextSelection] | None = None,
    ) -> dict[str, Any]:
        if len(invocation_ids) < 2:
            raise LoomConflict("join requires at least two parent invocations")
        parents = [self.store.get_invocation(item) for item in invocation_ids]
        if len({parent["project_id"] for parent in parents}) != 1:
            raise LoomConflict("join parents must belong to one project")
        return self.store.create_draft(DraftCreate(project_id=parents[0]["project_id"], role_session_id=role_session_id, prompt=prompt, route=route, selections=selections or [], parent_invocation_ids=invocation_ids, relation=EdgeRelation.inherited_context_from))

    def seal(self, draft_id: str) -> dict[str, Any]:
        draft = self.store.get_draft(draft_id)
        manifest, compiled_prompt = self.compositor.compile(draft)
        independently_reconstructed = verify_and_reconstruct(self.store, manifest)
        if independently_reconstructed != compiled_prompt:
            raise LoomConflict("independent manifest reader disagrees with compositor")
        manifest_sha = manifest["manifest_sha256"]
        return self.store.insert_invocation(draft, manifest, manifest_sha, manifest["compilation"]["compiled_prompt_sha256"])

    def inspect(self, invocation_id: str) -> dict[str, Any]:
        invocation = self.store.get_invocation(invocation_id)
        reconstructed = verify_and_reconstruct(self.store, invocation["manifest"])
        invocation["manifest_verification"] = {
            "valid": True,
            "compiled_prompt_sha256": sha256_bytes(reconstructed),
            "byte_count": len(reconstructed),
        }
        return invocation

    def diff(self, invocation_a: str, invocation_b: str) -> dict[str, Any]:
        left = self.store.get_invocation(invocation_a)
        right = self.store.get_invocation(invocation_b)
        left_segments = {item["block_id"]: item for item in left["manifest"]["segments"]}
        right_segments = {item["block_id"]: item for item in right["manifest"]["segments"]}
        left_ids, right_ids = set(left_segments), set(right_segments)
        return {
            "left": invocation_a,
            "right": invocation_b,
            "only_left": [left_segments[item] for item in sorted(left_ids - right_ids)],
            "only_right": [right_segments[item] for item in sorted(right_ids - left_ids)],
            "shared": [left_segments[item] for item in sorted(left_ids & right_ids)],
            "execution_delta": {key: {"left": left["route"].get(key), "right": right["route"].get(key)} for key in sorted(set(left["route"]) | set(right["route"])) if left["route"].get(key) != right["route"].get(key)},
            "output_delta": {
                "left_sha256": left.get("output_block", {}).get("content_sha256"),
                "right_sha256": right.get("output_block", {}).get("content_sha256"),
            },
        }

    def replay(self, invocation_id: str, *, prompt: str | None = None, route: ProviderRoute | None = None) -> dict[str, Any]:
        original = self.store.get_invocation(invocation_id)
        manifest = original["manifest"]
        prompt_segment = next(item for item in manifest["segments"] if item["plane"] == "current_prompt")
        original_prompt = self.store.get_block(prompt_segment["block_id"])["content"]
        explicit = [ContextSelection.model_validate({key: item[key] for key in ("block_id", "disposition", "plane", "label", "derived_from_block_ids", "reason")}) for item in manifest["segments"] if item["plane"] not in {"instruction", "ancestry", "current_prompt"}]
        draft = self.store.create_draft(DraftCreate(project_id=original["project_id"], role_session_id=original["role_session_id"], prompt=prompt or original_prompt, route=route or ProviderRoute.model_validate(original["route"]), selections=explicit, parent_invocation_ids=[invocation_id], relation=EdgeRelation.derived_from, response_schema=original["response_schema"]))
        return draft

    def tag(self, invocation_id: str, value: TagRequest) -> dict[str, Any]:
        invocation = self.store.get_invocation(invocation_id)
        target_revision = sha256_bytes(value.concern_ref.encode("utf-8"))
        edge_id = self.store.add_edge(project_id=invocation["project_id"], relation=EdgeRelation.concerns, source_type="invocation", source_id=invocation_id, source_revision=invocation["manifest_sha256"], target_type="external-concern", target_id=value.concern_ref, target_revision=target_revision, metadata={"label": value.label})
        existing = self.store.get_slice_by_external_ref(value.concern_ref)
        if existing is None:
            existing = self.store.create_slice(SliceCreate(project_id=invocation["project_id"], name=value.label, external_ref=value.concern_ref, query={"concern_ref": value.concern_ref}))
        return {"edge_id": edge_id, "slice": existing}

    def extract(self, project_id: str, invocation_ids: list[str], title: str) -> dict[str, Any]:
        invocations = [self.store.get_invocation(item) for item in invocation_ids]
        if not invocations or any(item["project_id"] != project_id for item in invocations):
            raise LoomConflict("workflow extraction requires project-local invocations")
        sources = [{"ref": f"loom:invocation/{item['invocation_id']}", "revision": item["manifest_sha256"], "sha256": item["manifest_sha256"]} for item in invocations]
        artifact = {
            "generated_object": generated_object(kind="loom-workflow-template", schema_name="loom-workflow-template", subject=f"loom-workflow:{opaque_id('workflow')}", sources=sources),
            "contract_version": "LoomWorkflowTemplate/1.0",
            "title": title,
            "roles": sorted({item["role_session_id"] for item in invocations}),
            "invocations": [{"source_invocation_id": item["invocation_id"], "route": item["route"], "context_parameters": [segment["semantic_type"] for segment in item["manifest"]["segments"] if segment["plane"] != "current_prompt"]} for item in invocations],
            "evidence_policy": "project evidence remains a parameter and is not copied",
        }
        raw = canonical_bytes(artifact)
        artifact_id = artifact["generated_object"]["artifact_id"]
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO artifacts(artifact_id,project_id,artifact_kind,generated_json,content_sha256,created_at) VALUES(?,?,?,?,?,?)", (artifact_id, project_id, "workflow-template", raw.decode(), sha256_bytes(raw), __import__("time").strftime("%Y%m%dT%H%M%S")))
            self.store._event(conn, project_id=project_id, event_type="workflow.extracted", subject_type="artifact", subject_id=artifact_id, payload={"title": title, "invocations": invocation_ids})
        return artifact
