"""Conversational graph operations over Lattice's storage and context compositor."""

from __future__ import annotations

import json
import uuid
from collections import Counter
from typing import Any

from .canonical import canonical_bytes, generated_object, opaque_id, sha256_bytes, sha256_json, stamp
from .compositor import ContextCompositor
from .contracts import (
    ActorBindingV1,
    ContextSelection,
    ContextAssemblyV1,
    DirectActorBinding,
    DraftCreate,
    EdgeRelation,
    ExecutionSubmissionV1,
    InvocationBatchCreateV1,
    InvocationBatchPreviewV1,
    InvocationSpecV1,
    ProviderRoute,
    TagRequest,
)
from .manifest_reader import verify_and_reconstruct
from .store import LatticeConflict, LatticeStore
from .workflow_template_reader import verify_workflow_template


class Lattice:
    def __init__(self, store: LatticeStore) -> None:
        self.store = store
        self.compositor = ContextCompositor(store)

    def invoke(self, value: DraftCreate) -> dict[str, Any]:
        return self.store.create_draft(value)

    def seal_execution_submission(self, value: ExecutionSubmissionV1) -> dict[str, Any]:
        """Resolve a caller identity and seal its singleton graph before dispatch."""

        request = value.model_dump(mode="json")
        request_sha = sha256_json(request)
        operation = self.store.reserve_external_submission(request, request_sha)
        if operation.get("invocation_id") is not None:
            return operation
        selections = [
            ContextSelection(
                block_id=block_id,
                disposition="selected",
                plane="knowledge",
                label=f"external-context-{ordinal}",
            )
            for ordinal, block_id in enumerate(value.context_block_ids)
        ]
        assembly = ContextAssemblyV1(
            project_id=operation["project_id"],
            actor_binding=DirectActorBinding(),
            prompt=value.prompt,
            selections=selections,
            parent_invocation_ids=value.parent_invocation_ids,
            response_schema=value.output_contract.model_dump(mode="json"),
        )
        batch_identity = sha256_json({"external_project_key": value.external_project_key, "operation_key": value.operation_key, "request_sha256": request_sha})
        batch_id = f"batch_{batch_identity[:32]}"
        preview_request = InvocationBatchPreviewV1(
            batch_id=batch_id,
            specifications=[InvocationSpecV1(spec_id="direct", assembly=assembly, route=value.route)],
        )
        preview = self.preview_invocation_batch(preview_request)
        receipt = self.create_invocation_batch(InvocationBatchCreateV1(
            batch_id=batch_id,
            specifications=preview_request.specifications,
            idempotency_key=f"external:{batch_identity}",
            expected_preview_sha256=preview["preview_sha256"],
            confirmed_reason_sha256s=[item["reason_sha256"] for item in preview["confirmation_reasons"]],
        ))
        invocation_id = receipt["invocation_ids"][0]
        return self.store.bind_external_submission(value.external_project_key, value.operation_key, invocation_id)

    @staticmethod
    def _batch_prompt_block(assembly: ContextAssemblyV1) -> tuple[str, dict[str, Any], bytes]:
        content = assembly.prompt.encode("utf-8")
        digest = sha256_bytes(content)
        identity = sha256_bytes(canonical_bytes({"project_id": assembly.project_id, "prompt_sha256": digest}))
        block_id = f"block_{identity[:32]}"
        return block_id, {
            "block_id": block_id,
            "project_id": assembly.project_id,
            "semantic_type": "user-prompt",
            "content_sha256": digest,
            "byte_count": len(content),
            "initial_status": "active",
            "producing_invocation_id": None,
            "source_ref": None,
            "source_sha256": None,
            "authority": "operator",
            "evidence_level": "operator-intent",
            "issuer_kind": "operator",
            "issuer_ref": None,
            "sensitivity": "private",
            "transformation_json": None,
            "created_at": None,
            "status": "active",
            "status_revision": 1,
            "content": assembly.prompt,
        }, content

    @staticmethod
    def _confirmation_reasons(preflight: dict[str, Any], parents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        capabilities = preflight["capabilities"]
        previous = [parent["manifest"].get("capabilities", {}) for parent in parents]
        baseline_tools = set().union(*(set(item.get("tools", [])) for item in previous)) if previous else set()
        baseline_permissions = set().union(*(set(item.get("permissions", [])) for item in previous)) if previous else set()
        baseline_workspaces = {item.get("workspace") for item in previous}
        baseline_network = {item.get("network_policy", "disabled") for item in previous}
        baseline_sandboxes = {item.get("sandbox", "read-only") for item in previous}
        baseline_approvals = {item.get("approval_policy", "never") for item in previous}
        reasons: list[dict[str, Any]] = []

        def add(code: str, detail: dict[str, Any]) -> None:
            material = {"code": code, "detail": detail}
            reasons.append({**material, "reason_sha256": sha256_json(material)})

        tools = set(capabilities.get("tools", []))
        permissions = set(capabilities.get("permissions", []))
        workspace = capabilities.get("workspace")
        network = capabilities.get("network_policy", "disabled")
        sandbox = capabilities.get("sandbox", "read-only")
        approval = capabilities.get("approval_policy", "never")
        if tools - baseline_tools:
            add("authority.tools-expanded", {"added": sorted(tools - baseline_tools)})
        if permissions - baseline_permissions:
            add("authority.permissions-expanded", {"added": sorted(permissions - baseline_permissions)})
        if workspace is not None and workspace not in baseline_workspaces:
            add("authority.workspace-expanded", {"workspace": workspace})
        if network != "disabled" and network not in baseline_network:
            add("authority.network-expanded", {"network_policy": network})
        if sandbox == "workspace-write" and sandbox not in baseline_sandboxes:
            add("authority.sandbox-expanded", {"sandbox": sandbox})
        if approval != "never" and approval not in baseline_approvals:
            add("authority.approval-expanded", {"approval_policy": approval})
        for item in preflight.get("omitted", []):
            if str(item.get("reason") or "").startswith("automatic:"):
                add("context.automatic-omission", {"block_id": item["block_id"], "reason": item["reason"]})
        for item in preflight.get("segments", []):
            transformation = item.get("transformation")
            if isinstance(transformation, dict) and transformation.get("initiated_by") not in {None, "operator"}:
                add("context.automatic-transformation", {"block_id": item["block_id"], "transformation": transformation})
        previous_profiles = {item.get("execution_profile", "compiled-fresh") for item in previous}
        current_profile = capabilities.get("execution_profile", "compiled-fresh")
        if previous_profiles and current_profile not in previous_profiles and current_profile == "workspace-agent":
            add("execution.isolation-changed", {"before": sorted(previous_profiles), "after": current_profile})
        return reasons

    @staticmethod
    def _assert_shared_assembly_context(members: list[dict[str, Any]]) -> None:
        """One route-neutral assembly must compile to one exact semantic byte stream."""

        compiled_by_assembly: dict[str, str] = {}
        for member in members:
            assembly_sha = member["assembly_sha256"]
            compiled_sha = member.get("compiled_prompt_sha256") or member["preflight"]["compilation"]["compiled_prompt_sha256"]
            existing = compiled_by_assembly.setdefault(assembly_sha, compiled_sha)
            if existing != compiled_sha:
                raise LatticeConflict("one shared context assembly compiled to different semantic bytes")

    def preview_invocation_batch(self, value: InvocationBatchPreviewV1) -> dict[str, Any]:
        """Compile a batch without persisting drafts, invocations, or events."""

        members: list[dict[str, Any]] = []
        all_reasons: dict[str, dict[str, Any]] = {}
        for spec in value.specifications:
            assembly = spec.assembly
            resolved_route = self.store.validate_invocation_intent(assembly, spec.route, route_scope_refs=spec.route_scope_refs)
            assembly_sha = sha256_json(assembly.model_dump(mode="json"))
            prompt_block_id, prompt_block, _prompt_bytes = self._batch_prompt_block(assembly)
            draft_identity = sha256_bytes(canonical_bytes({"batch_id": value.batch_id, "spec_id": spec.spec_id}))
            actor = assembly.actor_binding.model_dump(mode="json")
            selections = [item.model_dump(mode="json") for item in assembly.selections]
            if assembly.context_mode == "explicit":
                selections = [{**item, "block_id": prompt_block_id} if item["plane"] == "current_prompt" else item for item in selections]
            draft = {
                "draft_id": f"draft_{draft_identity[:32]}",
                "project_id": assembly.project_id,
                "actor_kind": actor["kind"],
                "actor_binding": actor,
                "role_session_id": actor.get("role_session_id"),
                "prompt_block_id": prompt_block_id,
                "route": resolved_route,
                "selections": selections,
                "parents": assembly.parent_invocation_ids,
                "relation": assembly.relation.value,
                "context_mode": assembly.context_mode,
                "response_schema": assembly.response_schema,
                "route_scope_refs": spec.route_scope_refs,
                "revision": 1,
                "state": "editable",
                "created_at": None,
                "updated_at": None,
            }
            preflight, compiled = self.compositor.compile(draft, preview=True, virtual_blocks={prompt_block_id: prompt_block})
            parents = [self.store.get_invocation(parent_id) for parent_id in assembly.parent_invocation_ids]
            reasons = self._confirmation_reasons(preflight, parents)
            for reason in reasons:
                all_reasons[reason["reason_sha256"]] = reason
            parent_deltas = [
                {"invocation_id": parent["invocation_id"], "manifest_sha256": parent["manifest_sha256"], **self.context_delta(parent["manifest"], preflight)}
                for parent in parents
            ]
            members.append({
                "spec_id": spec.spec_id,
                "assembly_sha256": assembly_sha,
                "draft": draft,
                "preflight": preflight,
                "compiled_prompt_preview": compiled.decode("utf-8"),
                "parent_deltas": parent_deltas,
                "route_scope_refs": spec.route_scope_refs,
            })
        self._assert_shared_assembly_context(members)
        material = {
            "contract_version": "InvocationBatchPreviewV1",
            "batch_id": value.batch_id,
            "project_id": value.specifications[0].assembly.project_id,
            "request_sha256": sha256_json(value.model_dump(mode="json")),
            "members": members,
            "confirmation_reasons": [all_reasons[key] for key in sorted(all_reasons)],
        }
        return {**material, "preview_sha256": sha256_json(material)}

    def create_invocation_batch(self, value: InvocationBatchCreateV1) -> dict[str, Any]:
        request_material = value.model_dump(mode="json")
        create_request_sha = sha256_json({key: item for key, item in request_material.items() if key != "idempotency_key"})
        existing = self.store.get_invocation_batch_by_idempotency(value.idempotency_key)
        if existing is not None:
            if existing["create_request_sha256"] != create_request_sha:
                raise LatticeConflict("batch idempotency key was already used for different bytes")
            return existing
        preview_input = InvocationBatchPreviewV1(batch_id=value.batch_id, specifications=value.specifications)
        preview = self.preview_invocation_batch(preview_input)
        if preview["preview_sha256"] != value.expected_preview_sha256:
            raise LatticeConflict("batch preview changed; nothing was created")
        required = {item["reason_sha256"] for item in preview["confirmation_reasons"]}
        confirmed = set(value.confirmed_reason_sha256s)
        if confirmed != required:
            raise LatticeConflict("batch confirmations do not exactly match its deterministic reasons")

        now = stamp()
        prepared: list[dict[str, Any]] = []
        for spec, member in zip(value.specifications, preview["members"], strict=True):
            assembly = spec.assembly
            prompt_block_id, prompt_block, prompt_bytes = self._batch_prompt_block(assembly)
            draft = {**member["draft"], "created_at": now, "updated_at": now}
            self.store.blobs.put(prompt_bytes)
            compiled = member["compiled_prompt_preview"].encode("utf-8")
            manifest = self.compositor.manifest_from_preflight(member["preflight"], draft)
            self.store.blobs.put(compiled)
            reconstructed = verify_and_reconstruct(
                self.store,
                manifest,
                draft_override=draft,
                block_overrides={prompt_block_id: prompt_block},
            )
            if reconstructed != compiled:
                raise LatticeConflict("independent manifest reader disagrees with batch compositor")
            prepared.append({
                "spec_id": spec.spec_id,
                "concern_refs": spec.concern_refs,
                "route_scope_refs": spec.route_scope_refs,
                "assembly_sha256": member["assembly_sha256"],
                "prompt_block": prompt_block,
                "draft": draft,
                "manifest": manifest,
                "compiled_prompt_sha256": manifest["compilation"]["compiled_prompt_sha256"],
            })
        self._assert_shared_assembly_context(prepared)

        sources = [
            {"ref": f"lattice:invocation/{item['manifest']['invocation_id']}", "revision": None, "sha256": item["manifest"]["manifest_sha256"]}
            for item in prepared
        ]
        receipt: dict[str, Any] = {
            "batch_id": value.batch_id,
            "project_id": preview["project_id"],
            "request_sha256": preview["request_sha256"],
            "create_request_sha256": create_request_sha,
            "preview_sha256": preview["preview_sha256"],
            "invocation_ids": [item["manifest"]["invocation_id"] for item in prepared],
            "manifest_sha256s": [item["manifest"]["manifest_sha256"] for item in prepared],
            "assembly_sha256s": [item["assembly_sha256"] for item in prepared],
            "confirmation_reason_sha256s": sorted(required),
            "generated_object": generated_object(
                kind="lattice-invocation-batch-receipt",
                schema_name="lattice-invocation-batch-receipt",
                subject=f"lattice-invocation-batch:{value.batch_id}",
                sources=sources,
                artifact_id=f"artifact:{uuid.uuid5(uuid.NAMESPACE_URL, f'lattice-batch:{value.batch_id}')}",
                generated_at=now,
            ),
        }
        receipt["receipt_sha256"] = sha256_json(receipt)
        return self.store.insert_invocation_batch(
            idempotency_key=value.idempotency_key,
            create_request_sha256=create_request_sha,
            receipt=receipt,
            prepared=prepared,
        )

    @staticmethod
    def context_delta(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        """Describe the exact observable context change without ordinal noise."""

        def entries(manifest: dict[str, Any]) -> list[dict[str, Any]]:
            values = []
            for surface, items in (("supplied", manifest.get("segments", [])), ("omitted", manifest.get("omitted", []))):
                for item in items:
                    values.append({**{key: value for key, value in item.items() if key != "ordinal"}, "surface_state": surface})
            return values

        before = entries(previous)
        after = entries(current)
        before_counts = Counter(canonical_bytes(item) for item in before)
        after_counts = Counter(canonical_bytes(item) for item in after)

        def difference(values: list[dict[str, Any]], other: Counter[bytes]) -> list[dict[str, Any]]:
            available = other.copy()
            result = []
            for item in values:
                identity = canonical_bytes(item)
                if available[identity]:
                    available[identity] -= 1
                else:
                    result.append(item)
            return result

        newly_observed = difference(after, before_counts)
        removed = difference(before, after_counts)
        provider_opaque = [item for item in newly_observed if item.get("disposition") == "provider_opaque"]
        omitted = [item for item in newly_observed if item["surface_state"] == "omitted" and item not in provider_opaque]
        transformed = [item for item in newly_observed if item["surface_state"] == "supplied" and item not in provider_opaque and (item.get("transformation") is not None or item.get("derived_from_block_ids"))]
        added = [item for item in newly_observed if item not in provider_opaque and item not in omitted and item not in transformed]
        return {
            "added": added,
            "removed": removed,
            "transformed": transformed,
            "omitted": omitted,
            "provider_opaque": provider_opaque,
            "unchanged_count": len(after) - len(newly_observed),
        }

    def fork(
        self,
        invocation_id: str,
        *,
        role_session_id: str | None = None,
        actor_binding: ActorBindingV1 | None = None,
        prompt: str,
        route: ProviderRoute,
        selections: list[ContextSelection] | None = None,
    ) -> dict[str, Any]:
        parent = self.store.get_invocation(invocation_id)
        binding = actor_binding or (None if role_session_id is not None else DirectActorBinding())
        return self.store.create_draft(DraftCreate(project_id=parent["project_id"], actor_binding=binding, role_session_id=role_session_id, prompt=prompt, route=route, selections=selections or [], parent_invocation_ids=[invocation_id], relation=EdgeRelation.inherited_context_from))

    def join(
        self,
        invocation_ids: list[str],
        *,
        role_session_id: str | None = None,
        actor_binding: ActorBindingV1 | None = None,
        prompt: str,
        route: ProviderRoute,
        selections: list[ContextSelection] | None = None,
    ) -> dict[str, Any]:
        if len(invocation_ids) < 2:
            raise LatticeConflict("join requires at least two parent invocations")
        parents = [self.store.get_invocation(item) for item in invocation_ids]
        if len({parent["project_id"] for parent in parents}) != 1:
            raise LatticeConflict("join parents must belong to one project")
        binding = actor_binding or (None if role_session_id is not None else DirectActorBinding())
        return self.store.create_draft(DraftCreate(project_id=parents[0]["project_id"], actor_binding=binding, role_session_id=role_session_id, prompt=prompt, route=route, selections=selections or [], parent_invocation_ids=invocation_ids, relation=EdgeRelation.inherited_context_from))

    def seal(self, draft_id: str, *, trigger_id: str | None = None, import_turn: tuple[str, int] | None = None, expected_revision: int | None = None, expected_preflight_sha256: str | None = None) -> dict[str, Any]:
        if (expected_revision is None) != (expected_preflight_sha256 is None):
            raise ValueError("seal preflight requires both draft revision and preflight identity")
        draft = self.store.get_draft(draft_id)
        if expected_revision is not None and draft["revision"] != expected_revision:
            raise LatticeConflict("draft changed after context inspection")
        manifest, compiled_prompt = self.compositor.compile(draft)
        if expected_preflight_sha256 is not None and manifest["preflight_sha256"] != expected_preflight_sha256:
            raise LatticeConflict("invocation context changed after inspection; inspect the exact context again")
        if self.store.blobs.put(compiled_prompt) != manifest["compilation"]["compiled_prompt_sha256"]:
            raise OSError("compiled context changed while entering the Lattice blob store")
        independently_reconstructed = verify_and_reconstruct(self.store, manifest)
        if independently_reconstructed != compiled_prompt:
            raise LatticeConflict("independent manifest reader disagrees with compositor")
        manifest_sha = manifest["manifest_sha256"]
        return self.store.insert_invocation(draft, manifest, manifest_sha, manifest["compilation"]["compiled_prompt_sha256"], trigger_id=trigger_id, import_turn=import_turn)

    def inspect(self, invocation_id: str) -> dict[str, Any]:
        invocation = self.store.get_invocation(invocation_id)
        reconstructed = verify_and_reconstruct(self.store, invocation["manifest"])
        invocation["manifest_verification"] = {
            "valid": True,
            "compiled_prompt_sha256": sha256_bytes(reconstructed),
            "byte_count": len(reconstructed),
        }
        invocation["compiled_prompt"] = reconstructed.decode("utf-8")
        return invocation

    def diff(self, invocation_a: str, invocation_b: str) -> dict[str, Any]:
        left = self.store.get_invocation(invocation_a)
        right = self.store.get_invocation(invocation_b)

        def semantic_identity(item: dict[str, Any]) -> tuple[Any, ...]:
            return (
                item["ordinal"],
                item["block_id"],
                item["plane"],
                item["disposition"],
                item["label"],
                tuple(item.get("derived_from_block_ids", [])),
                item.get("reason"),
            )

        left_context = left["manifest"]["segments"] + left["manifest"]["omitted"]
        right_context = right["manifest"]["segments"] + right["manifest"]["omitted"]
        left_segments = {semantic_identity(item): item for item in left_context}
        right_segments = {semantic_identity(item): item for item in right_context}
        left_ids, right_ids = set(left_segments), set(right_segments)
        stable_sort = lambda identity: canonical_bytes(list(identity))
        left_by_block: dict[str, list[dict[str, Any]]] = {}
        right_by_block: dict[str, list[dict[str, Any]]] = {}
        for item in left_context:
            left_by_block.setdefault(item["block_id"], []).append(item)
        for item in right_context:
            right_by_block.setdefault(item["block_id"], []).append(item)
        def artifact_view(item: dict[str, Any]) -> dict[str, Any]:
            generated = json.loads(item["generated_json"])
            return {"artifact_id": item["artifact_id"], "name": generated.get("name"), "media_type": generated.get("media_type"), "content_sha256": item["content_sha256"]}

        left_artifacts = sorted((artifact_view(item) for item in left["artifacts"]), key=lambda item: canonical_bytes(item))
        right_artifacts = sorted((artifact_view(item) for item in right["artifacts"]), key=lambda item: canonical_bytes(item))
        changed = [
            {"block_id": block_id, "left": left_by_block[block_id], "right": right_by_block[block_id]}
            for block_id in sorted(set(left_by_block) & set(right_by_block))
            if {semantic_identity(item) for item in left_by_block[block_id]}
            != {semantic_identity(item) for item in right_by_block[block_id]}
        ]
        return {
            "left": invocation_a,
            "right": invocation_b,
            "only_left": [left_segments[item] for item in sorted(left_ids - right_ids, key=stable_sort)],
            "only_right": [right_segments[item] for item in sorted(right_ids - left_ids, key=stable_sort)],
            "shared": [left_segments[item] for item in sorted(left_ids & right_ids, key=stable_sort)],
            "changed_semantics": changed,
            "execution_delta": {key: {"left": left["route"].get(key), "right": right["route"].get(key)} for key in sorted(set(left["route"]) | set(right["route"])) if left["route"].get(key) != right["route"].get(key)},
            "output_delta": {
                "left_sha256": left.get("output_block", {}).get("content_sha256"),
                "right_sha256": right.get("output_block", {}).get("content_sha256"),
                "left_state": left["state"],
                "right_state": right["state"],
                "left_artifacts": left_artifacts,
                "right_artifacts": right_artifacts,
            },
        }

    def replay(self, invocation_id: str, *, prompt: str | None = None, route: ProviderRoute | None = None) -> dict[str, Any]:
        original = self.store.get_invocation(invocation_id)
        manifest = original["manifest"]
        prompt_segment = next(item for item in manifest["segments"] if item["plane"] == "current_prompt")
        original_prompt = self.store.get_block(prompt_segment["block_id"])["content"]
        explicit = [
            ContextSelection.model_validate(
                {key: item[key] for key in ("block_id", "disposition", "plane", "label", "derived_from_block_ids", "reason")}
            )
            for item in manifest["segments"] + manifest["omitted"]
        ]
        replay_prompt = original_prompt
        prompt_block_id = prompt_segment["block_id"]
        if prompt is not None:
            replay_prompt = prompt
            prompt_block_id = None
        draft = self.store.create_draft(
            DraftCreate(
                project_id=original["project_id"],
                actor_binding=original["actor_binding"],
                prompt=replay_prompt,
                route=route or ProviderRoute.model_validate(original["route"]),
                selections=explicit,
                parent_invocation_ids=[invocation_id],
                relation=EdgeRelation.derived_from,
                context_mode="explicit",
                response_schema=original["response_schema"],
            ),
            prompt_block_id=prompt_block_id,
            retarget_current_prompt_selections=prompt is not None,
        )
        return draft

    def tag(self, invocation_id: str, value: TagRequest) -> dict[str, Any]:
        return self.store.tag_invocation(invocation_id, value)

    def extract(self, project_id: str, invocation_ids: list[str], title: str) -> dict[str, Any]:
        if len(invocation_ids) != len(set(invocation_ids)):
            raise LatticeConflict("workflow extraction invocation IDs must be unique")
        invocations = [self.store.get_invocation(item) for item in invocation_ids]
        if not invocations or any(item["project_id"] != project_id for item in invocations):
            raise LatticeConflict("workflow extraction requires project-local invocations")
        if any(item["state"] not in {"succeeded", "imported"} for item in invocations):
            raise LatticeConflict("workflow extraction requires successful or imported source invocations")

        by_id = {item["invocation_id"]: item for item in invocations}
        output_owner = {item["output_block_id"]: item_id for item_id, item in by_id.items() if item.get("output_block_id")}
        dependencies: dict[str, set[str]] = {}
        for item_id, item in by_id.items():
            direct = {parent["invocation_id"] for parent in item["manifest"]["ancestry"] if parent["invocation_id"] in by_id}
            consumed_blocks = {
                block_id
                for entry in item["manifest"]["segments"] + item["manifest"]["omitted"]
                for block_id in [entry["block_id"], *entry.get("derived_from_block_ids", [])]
            }
            direct.update(owner for block_id in consumed_blocks if (owner := output_owner.get(block_id)) is not None and owner != item_id)
            dependencies[item_id] = direct
        ordered: list[dict[str, Any]] = []
        remaining = set(by_id)
        while remaining:
            ready = sorted((item_id for item_id in remaining if not dependencies[item_id] & remaining), key=lambda item_id: (by_id[item_id]["created_at"], item_id))
            if not ready:
                raise LatticeConflict("workflow extraction source graph contains a cycle")
            for item_id in ready:
                ordered.append(by_id[item_id])
                remaining.remove(item_id)

        step_by_invocation = {item["invocation_id"]: f"step-{index:03d}" for index, item in enumerate(ordered, start=1)}
        output_to_step = {item["output_block_id"]: step_by_invocation[item["invocation_id"]] for item in ordered if item.get("output_block_id")}
        context_parameters: dict[str, dict[str, Any]] = {}
        context_parameter_by_block: dict[str, str] = {}
        invocation_parameters: dict[str, dict[str, Any]] = {}
        invocation_parameter_by_source: dict[str, str] = {}
        role_parameters: dict[str, dict[str, Any]] = {}
        role_parameter_by_source_contract: dict[tuple[str, str | None], str] = {}

        def context_parameter(block_id: str) -> str:
            existing = context_parameter_by_block.get(block_id)
            if existing:
                return existing
            block = self.store.get_block(block_id)
            parameter_id = f"context-{len(context_parameters) + 1:03d}"
            context_parameter_by_block[block_id] = parameter_id
            context_parameters[parameter_id] = {
                "parameter_id": parameter_id,
                "kind": "context-block",
                "semantic_type": block["semantic_type"],
                "status": block["status"],
                "authority": block["authority"],
                "evidence_level": block["evidence_level"],
                "sensitivity": block["sensitivity"],
                "source_content_sha256": block["content_sha256"],
            }
            return parameter_id

        def invocation_parameter(invocation_id: str) -> str:
            existing = invocation_parameter_by_source.get(invocation_id)
            if existing:
                return existing
            parameter_id = f"invocation-{len(invocation_parameters) + 1:03d}"
            invocation_parameter_by_source[invocation_id] = parameter_id
            invocation_parameters[parameter_id] = {"parameter_id": parameter_id, "kind": "invocation", "source_invocation_id": invocation_id}
            return parameter_id

        def block_binding(block_id: str, *, role_instruction_block_id: str | None) -> dict[str, str]:
            if role_instruction_block_id is not None and block_id == role_instruction_block_id:
                return {"kind": "role-instruction"}
            if block_id in output_to_step:
                return {"kind": "step-output", "step_id": output_to_step[block_id]}
            return {"kind": "context-parameter", "parameter_id": context_parameter(block_id)}

        steps: list[dict[str, Any]] = []
        for item in ordered:
            entries = sorted(item["manifest"]["segments"] + item["manifest"]["omitted"], key=lambda entry: entry["ordinal"])
            source_role_entry = next((entry for entry in entries if entry["plane"] == "instruction" and entry["semantic_type"] == "role-instruction" and entry["label"].startswith("role:")), None)
            source_role_block = self.store.get_block(source_role_entry["block_id"]) if source_role_entry else None
            if item["actor_kind"] == "direct":
                if source_role_block is not None or item["role_session_id"] is not None:
                    raise LatticeConflict("direct source invocation carries hidden role context")
                actor_binding = {"kind": "direct"}
            else:
                session = self.store.get_role_session(item["role_session_id"])
                role = self.store.get_role(session["role_id"])
                role_source_key = (role["role_id"], source_role_block["content_sha256"] if source_role_block else None)
                role_parameter_id = role_parameter_by_source_contract.get(role_source_key)
                if role_parameter_id is None:
                    role_parameter_id = f"role-{len(role_parameters) + 1:03d}"
                    role_parameter_by_source_contract[role_source_key] = role_parameter_id
                    role_parameters[role_parameter_id] = {
                        "parameter_id": role_parameter_id,
                        "kind": "role-session",
                        "source_role_label": source_role_entry["label"] if source_role_entry else None,
                        "source_role_contract": source_role_block["content"] if source_role_block else None,
                        "source_role_contract_sha256": source_role_block["content_sha256"] if source_role_block else None,
                        "proof_limit": None if source_role_block else "the source invocation did not include an observable role-instruction block",
                    }
                actor_binding = {"kind": "role-parameter", "parameter_id": role_parameter_id}
            bindings = []
            for entry in entries:
                role_instruction_block_id = source_role_block["block_id"] if source_role_block else None
                binding = block_binding(entry["block_id"], role_instruction_block_id=role_instruction_block_id)
                derived_bindings = [block_binding(block_id, role_instruction_block_id=role_instruction_block_id) for block_id in entry.get("derived_from_block_ids", [])]
                bindings.append({
                    "selection": {key: entry.get(key) for key in ("disposition", "plane", "label", "reason")},
                    "binding": binding,
                    "derived_bindings": derived_bindings,
                })
            parents = []
            for parent in item["manifest"]["ancestry"]:
                parent_id = parent["invocation_id"]
                parents.append({"kind": "step", "step_id": step_by_invocation[parent_id]} if parent_id in step_by_invocation else {"kind": "invocation-parameter", "parameter_id": invocation_parameter(parent_id)})
            prompt_bindings = [binding for binding in bindings if binding["selection"]["plane"] == "current_prompt"]
            if len(prompt_bindings) != 1 or prompt_bindings[0]["binding"]["kind"] != "context-parameter":
                raise LatticeConflict("workflow extraction requires one parameterizable current prompt per invocation")
            steps.append({
                "step_id": step_by_invocation[item["invocation_id"]],
                "source_invocation_id": item["invocation_id"],
                "actor_binding": actor_binding,
                "route_default": item["route"],
                "relation": self.store.get_draft(item["draft_id"])["relation"],
                "response_contract": item["response_schema"],
                "parents": parents,
                "context": bindings,
            })

        sources = [{"ref": f"lattice:invocation/{item['invocation_id']}", "revision": item["manifest_sha256"], "sha256": item["manifest_sha256"]} for item in ordered]
        artifact = {
            "generated_object": generated_object(kind="lattice-workflow-template", schema_name="lattice-workflow-template", schema_version=3, subject=f"lattice-workflow:{opaque_id('workflow')}", sources=sources),
            "contract_version": "LatticeWorkflowTemplate/3.0",
            "title": title,
            "roles": list(role_parameters.values()),
            "parameters": [*context_parameters.values(), *invocation_parameters.values()],
            "steps": steps,
            "materialization": {"mode": "one-edge-at-a-time", "endpoint": "/api/v1/workflow-templates/{artifact_id}/steps/{step_id}/drafts"},
            "evidence_policy": "prompt and project evidence bytes are required at materialization and are not copied into this template",
        }
        raw = canonical_bytes(artifact)
        content_sha = self.store.blobs.put(raw)
        artifact_id = artifact["generated_object"]["artifact_id"]
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO artifacts(artifact_id,project_id,artifact_kind,generated_json,content_sha256,created_at) VALUES(?,?,?,?,?,?)", (artifact_id, project_id, "workflow-template", raw.decode(), content_sha, __import__("time").strftime("%Y%m%dT%H%M%S")))
            self.store._event(conn, project_id=project_id, event_type="workflow.extracted", subject_type="artifact", subject_id=artifact_id, payload={"title": title, "invocations": invocation_ids})
        return artifact

    def materialize_template_step(
        self,
        artifact_id: str,
        step_id: str,
        *,
        expected_template_sha256: str,
        project_id: str,
        role_session_id: str | None,
        context_bindings: dict[str, str],
        invocation_bindings: dict[str, str],
        route: ProviderRoute | None = None,
    ) -> dict[str, Any]:
        stored = self.store.get_artifact(artifact_id)
        if stored["artifact_kind"] != "workflow-template" or stored["content_sha256"] != expected_template_sha256:
            raise LatticeConflict("workflow template identity changed before materialization")
        template = verify_workflow_template(self.store, stored)
        step = next((item for item in template.get("steps", []) if item.get("step_id") == step_id), None)
        if step is None:
            raise LatticeConflict("workflow template step does not exist")
        parameters = {item["parameter_id"]: item for item in template.get("parameters", [])}
        expected_context = {parameter_id for parameter_id, value in parameters.items() if value["kind"] == "context-block" and any(binding.get("binding", {}).get("parameter_id") == parameter_id or any(derived.get("parameter_id") == parameter_id for derived in binding.get("derived_bindings", [])) for binding in step["context"])}
        expected_invocations = {parent["parameter_id"] for parent in step["parents"] if parent["kind"] == "invocation-parameter"}
        context_steps = {binding["step_id"] for item in step["context"] for binding in [item["binding"], *item["derived_bindings"]] if binding["kind"] == "step-output"}
        if set(context_bindings) != expected_context or set(invocation_bindings) != ({parent["step_id"] for parent in step["parents"] if parent["kind"] == "step"} | context_steps | expected_invocations):
            raise LatticeConflict("workflow template bindings do not exactly match this step")
        actor_declaration = step.get("actor_binding")
        if actor_declaration is None:
            actor_declaration = {"kind": "role-parameter", "parameter_id": step["role_parameter_id"]}
        if actor_declaration["kind"] == "direct":
            if role_session_id is not None:
                raise LatticeConflict("direct workflow template steps cannot acquire a role session")
            actor_binding: ActorBindingV1 = DirectActorBinding()
            role = None
        else:
            if role_session_id is None:
                raise LatticeConflict("role-bound workflow template steps require a role session")
            session = self.store.get_role_session(role_session_id)
            if session["project_id"] != project_id:
                raise LatticeConflict("workflow template role session belongs to another project")
            role = self.store.get_role(session["role_id"])
            actor_binding = {"kind": "role-session", "role_session_id": role_session_id}

        def resolve_block(binding: dict[str, str]) -> str:
            if binding["kind"] == "role-instruction":
                if role is None:
                    raise LatticeConflict("direct workflow template step cannot resolve a role instruction")
                return role["instructions_block_id"]
            if binding["kind"] == "context-parameter":
                parameter_id = binding["parameter_id"]
                block = self.store.get_block(context_bindings[parameter_id], include_content=False)
                contract = parameters[parameter_id]
                if block["project_id"] != project_id or any(block[key] != contract[key] for key in ("semantic_type", "status", "authority", "evidence_level", "sensitivity")):
                    raise LatticeConflict(f"workflow context binding violates parameter contract: {parameter_id}")
                return block["block_id"]
            if binding["kind"] == "step-output":
                invocation = self.store.get_invocation(invocation_bindings[binding["step_id"]])
                if invocation["project_id"] != project_id or invocation["state"] not in {"succeeded", "imported"} or not invocation.get("output_block_id"):
                    raise LatticeConflict(f"workflow step output is not a successful project-local invocation: {binding['step_id']}")
                return invocation["output_block_id"]
            raise LatticeConflict("workflow template contains an unsupported context binding")

        parents: list[str] = []
        for parent in step["parents"]:
            binding_key = parent["step_id"] if parent["kind"] == "step" else parent["parameter_id"]
            invocation = self.store.get_invocation(invocation_bindings[binding_key])
            if invocation["project_id"] != project_id:
                raise LatticeConflict("workflow parent binding belongs to another project")
            parents.append(invocation["invocation_id"])
        selections = []
        for item in step["context"]:
            block_id = resolve_block(item["binding"])
            derived = [resolve_block(binding) for binding in item["derived_bindings"]]
            selections.append(ContextSelection(block_id=block_id, derived_from_block_ids=derived, **item["selection"]))
        prompt_selection = next(item for item in selections if item.plane == "current_prompt")
        prompt_block = self.store.get_block(prompt_selection.block_id)
        draft = self.store.create_draft(
            DraftCreate(
                project_id=project_id,
                actor_binding=actor_binding,
                prompt=prompt_block["content"],
                route=route or ProviderRoute.model_validate(step["route_default"]),
                selections=selections,
                parent_invocation_ids=parents,
                relation=EdgeRelation(step["relation"]),
                context_mode="explicit",
                response_schema=step["response_contract"],
            ),
            prompt_block_id=prompt_block["block_id"],
            source_artifact=(artifact_id, expected_template_sha256, step_id),
        )
        return draft
