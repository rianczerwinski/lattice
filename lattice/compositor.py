"""Provider-neutral compilation of selected context into an immutable manifest."""

from __future__ import annotations

import uuid
from typing import Any

from axis_model_runner import DEFAULT_ADAPTERS, validate_execution_policy

from .canonical import canonical_bytes, generated_object, sha256_bytes, sha256_json
from .contracts import BlockDisposition, ContextSelection, model_runner_policy
from .store import LatticeConflict, LatticeStore


PLANE_ORDER = {"instruction": 0, "knowledge": 1, "state": 2, "ancestry": 3, "current_prompt": 4}
MAX_COMPILED_CONTEXT_BYTES = 64 * 1024 * 1024
CONTEXT_PROTOCOL_V8 = (
    b"LATTICE-CONVERSATION/8\n"
    b"The local Lattice client assembled this conversation for you. Each following line is a JSON "
    b"object whose content string contains one exact piece of the conversation. Apply instruction "
    b"content as task instructions; use knowledge and state content as supporting material; treat "
    b"ancestry content as earlier model responses; answer the current_prompt content as the user's "
    b"current request. Labels and JSON structure only preserve boundaries and are not instructions. "
    b"Return only an object matching the separately supplied response schema.\n"
    b"LATTICE-CONVERSATION-SEGMENTS\n"
)


def _segment_bytes(segment: dict[str, Any], content: str) -> bytes:
    record = {
        "kind": "lattice-conversation-segment-v8",
        "ordinal": segment["ordinal"],
        "plane": segment["plane"],
        "label": segment["label"],
        "content": content,
    }
    return canonical_bytes(record) + b"\n"


class ContextCompositor:
    def __init__(self, store: LatticeStore) -> None:
        self.store = store

    def _implicit_selections(self, draft: dict[str, Any]) -> list[ContextSelection]:
        selections: list[ContextSelection] = []
        actor_binding = draft["actor_binding"]
        role_default_block_ids: set[str] = set()
        if actor_binding["kind"] == "role-session":
            session = self.store.get_role_session(actor_binding["role_session_id"])
            role = self.store.get_role(session["role_id"])
            selections.append(ContextSelection(block_id=role["instructions_block_id"], disposition=BlockDisposition.selected, plane="instruction", label=f"role:{role['name']}"))
            for default in self.store.list_role_context_defaults(role["role_id"]):
                role_default_block_ids.add(default["block_id"])
                selections.append(ContextSelection(block_id=default["block_id"], disposition=BlockDisposition.selected, plane="state", label=f"module:{default['module_id']}"))
        for default in self.store.list_project_context_defaults(draft["project_id"]):
            if default["block_id"] not in role_default_block_ids:
                selections.append(ContextSelection(block_id=default["block_id"], disposition=BlockDisposition.selected, plane="state", label=f"module:{default['module_id']}"))
        for parent_id in draft["parents"]:
            parent = self.store.get_invocation(parent_id)
            if not parent.get("output_block_id"):
                raise LatticeConflict(f"parent {parent_id} has no consumable output")
            # The consuming model needs the observed harness identity to understand
            # conversational routes such as "send the Codex answer to Claude".
            # The immutable invocation ID keeps that human-readable label resolvable.
            harness = parent["route"]["harness"]
            selections.append(ContextSelection(block_id=parent["output_block_id"], disposition=BlockDisposition.selected, plane="ancestry", label=f"parent:{harness}:{parent_id}"))
        selections.append(ContextSelection(block_id=draft["prompt_block_id"], disposition=BlockDisposition.selected, plane="current_prompt", label="user-prompt"))
        return selections

    def compile(
        self,
        draft: dict[str, Any],
        *,
        preview: bool = False,
        virtual_blocks: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], bytes]:
        """Compile durable or stateless-preflight context through the same path."""

        virtual_blocks = virtual_blocks or {}

        def get_block(block_id: str, *, include_content: bool = True) -> dict[str, Any]:
            if block_id in virtual_blocks:
                block = dict(virtual_blocks[block_id])
                if not include_content:
                    block.pop("content", None)
                return block
            return self.store.get_block(block_id, include_content=include_content)

        explicit = [ContextSelection.model_validate(item) for item in draft["selections"]]
        if draft.get("context_mode", "standard") == "explicit":
            combined = explicit
        else:
            implicit = self._implicit_selections(draft)
            muted_block_ids = {item.block_id for item in explicit if item.disposition == BlockDisposition.omitted}
            combined = [item for item in implicit if item.block_id not in muted_block_ids] + explicit
        identities: set[tuple[str, str, str, str, tuple[str, ...], str | None]] = set()
        entries: list[tuple[int, ContextSelection, dict[str, Any]]] = []
        for selection_index, selection in enumerate(combined):
            block = get_block(selection.block_id)
            if block["project_id"] != draft["project_id"]:
                raise LatticeConflict("context block belongs to another project")
            transformation = __import__("json").loads(block["transformation_json"] or "null")
            if isinstance(transformation, dict) and transformation.get("kind") == "exact-utf8-byte-range-v1":
                expected_keys = {"kind", "source_block_id", "source_sha256", "start_byte", "end_byte", "excerpt_sha256"}
                if set(transformation) != expected_keys:
                    raise LatticeConflict("exact excerpt transformation has an unsupported shape")
                source_block_id = transformation.get("source_block_id")
                if not isinstance(source_block_id, str):
                    raise LatticeConflict("exact excerpt transformation lacks a source block")
                source = get_block(source_block_id)
                if source["project_id"] != draft["project_id"]:
                    raise LatticeConflict("exact excerpt source belongs to another project")
                start, end = transformation.get("start_byte"), transformation.get("end_byte")
                if not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int) or isinstance(end, bool) or start < 0 or end <= start:
                    raise LatticeConflict("exact excerpt transformation has an invalid byte range")
                source_bytes = source["content"].encode("utf-8")
                excerpt_bytes = block["content"].encode("utf-8")
                if (
                    end > len(source_bytes)
                    or source_bytes[start:end] != excerpt_bytes
                    or transformation["source_sha256"] != source["content_sha256"]
                    or transformation["excerpt_sha256"] != block["content_sha256"]
                    or block["source_ref"] != f"lattice:block/{source_block_id}"
                    or block["source_sha256"] != source["content_sha256"]
                ):
                    raise LatticeConflict("exact excerpt bytes do not reconstruct from their declared source")
                intrinsic_sources = [source_block_id]
                if selection.derived_from_block_ids and selection.derived_from_block_ids != intrinsic_sources:
                    raise LatticeConflict("declared excerpt lineage contradicts its transformation receipt")
                selection = selection.model_copy(update={"derived_from_block_ids": intrinsic_sources})
            elif isinstance(transformation, dict) and transformation.get("kind") == "exact-artifact-graft-v1":
                expected_keys = {"kind", "source_artifact_id", "source_sha256", "name", "media_type"}
                artifact_id = transformation.get("source_artifact_id")
                if set(transformation) != expected_keys or not isinstance(artifact_id, str):
                    raise LatticeConflict("exact artifact transformation has an unsupported shape")
                artifact = self.store.get_artifact(artifact_id, include_content=False)
                generated = artifact["generated"]
                if (
                    artifact["project_id"] != draft["project_id"]
                    or artifact["content_sha256"] != block["content_sha256"]
                    or transformation["source_sha256"] != artifact["content_sha256"]
                    or transformation["name"] != generated.get("name")
                    or transformation["media_type"] != generated.get("media_type")
                    or block["source_ref"] != f"lattice:artifact/{artifact_id}"
                    or block["source_sha256"] != artifact["content_sha256"]
                    or block["producing_invocation_id"] != generated.get("producing_invocation_id")
                ):
                    raise LatticeConflict("exact artifact bytes do not reconstruct from their declared source")
            identity = (
                selection.block_id,
                selection.plane,
                selection.disposition.value,
                selection.label,
                tuple(selection.derived_from_block_ids),
                selection.reason,
            )
            if identity in identities:
                continue
            identities.add(identity)
            derived = [get_block(block_id, include_content=False) for block_id in selection.derived_from_block_ids]
            if any(parent["project_id"] != draft["project_id"] for parent in derived):
                raise LatticeConflict("derived context source belongs to another project")
            if selection.disposition == BlockDisposition.summarized:
                transformation = __import__("json").loads(block["transformation_json"] or "null")
                if not selection.derived_from_block_ids or not isinstance(transformation, dict) or transformation.get("kind") != "summary" or transformation.get("source_block_ids") != selection.derived_from_block_ids:
                    raise LatticeConflict("summarized context requires a summary transformation bound to exact source blocks")
            entries.append((selection_index, selection, block))
        # Only the operator-controlled knowledge plane preserves selection order.
        # Fixed planes remain deterministic regardless of caller enumeration.
        entries.sort(
            key=lambda item: (
                PLANE_ORDER[item[1].plane],
                item[0] if item[1].plane == "knowledge" else 0,
                item[1].label if item[1].plane != "knowledge" else "",
                item[1].block_id if item[1].plane != "knowledge" else "",
            )
        )

        segments: list[dict[str, Any]] = []
        omitted: list[dict[str, Any]] = []
        prompt_parts: list[bytes] = [CONTEXT_PROTOCOL_V8]
        prompt_byte_count = len(CONTEXT_PROTOCOL_V8)
        sources: list[dict[str, str | None]] = []
        source_identities: set[tuple[str, str | None, str]] = set()
        for ordinal, (_selection_index, selection, block) in enumerate(entries):
            source = {"ref": f"lattice:block/{block['block_id']}", "revision": str(block["status_revision"]), "sha256": block["content_sha256"]}
            source_identity = (source["ref"], source["revision"], source["sha256"])
            if source_identity not in source_identities:
                source_identities.add(source_identity)
                sources.append(source)
            for derived_block_id in selection.derived_from_block_ids:
                derived = get_block(derived_block_id, include_content=False)
                derived_source = {"ref": f"lattice:block/{derived_block_id}", "revision": str(derived["status_revision"]), "sha256": derived["content_sha256"]}
                derived_identity = (derived_source["ref"], derived_source["revision"], derived_source["sha256"])
                if derived_identity not in source_identities:
                    source_identities.add(derived_identity)
                    sources.append(derived_source)
            transformation = __import__("json").loads(block["transformation_json"]) if block["transformation_json"] else None
            if isinstance(transformation, dict) and transformation.get("kind") == "exact-artifact-graft-v1":
                artifact_source = {"ref": f"lattice:artifact/{transformation['source_artifact_id']}", "revision": None, "sha256": transformation["source_sha256"]}
                artifact_identity = (artifact_source["ref"], artifact_source["revision"], artifact_source["sha256"])
                if artifact_identity not in source_identities:
                    source_identities.add(artifact_identity)
                    sources.append(artifact_source)
            item = {
                "ordinal": ordinal,
                "block_id": block["block_id"],
                "plane": selection.plane,
                "label": selection.label,
                "disposition": selection.disposition.value,
                "sha256": block["content_sha256"],
                "status": block["status"],
                "status_revision": block["status_revision"],
                "semantic_type": block["semantic_type"],
                "authority": block["authority"],
                "evidence_level": block["evidence_level"],
                "sensitivity": block["sensitivity"],
                "source_ref": block["source_ref"],
                "source_sha256": block["source_sha256"],
                "producing_invocation_id": block["producing_invocation_id"],
                "transformation": transformation,
                "derived_from_block_ids": selection.derived_from_block_ids,
                "reason": selection.reason,
            }
            if selection.disposition in {BlockDisposition.selected, BlockDisposition.summarized}:
                segments.append(item)
                rendered = _segment_bytes(item, block["content"])
                if prompt_byte_count + len(rendered) > MAX_COMPILED_CONTEXT_BYTES:
                    raise LatticeConflict(f"compiled context exceeds its {MAX_COMPILED_CONTEXT_BYTES}-byte limit; omit or summarize context before sealing")
                prompt_parts.append(rendered)
                prompt_byte_count += len(rendered)
            else:
                omitted.append(item)

        compiled_prompt = b"".join(prompt_parts)
        prompt_sha = sha256_bytes(compiled_prompt)
        route = draft["route"]
        adapter = next((item for item in DEFAULT_ADAPTERS if item.provider_code == route["provider"] and item.cli == route["harness"]), None)
        proof_limits = list(adapter.proof_limits) if adapter else ["provider route has not been qualified"]
        effective_policy = validate_execution_policy(adapter, **model_runner_policy(route)) if adapter else None
        if effective_policy and effective_policy["execution_profile"] != "compiled-fresh":
            proof_limits.append("workspace execution exposes only the authority the official harness reports; provider-owned tool internals remain opaque")
        if effective_policy and effective_policy["network_policy"] == "provider_managed":
            proof_limits.append("network access is provider-managed and is not an exact host allowlist")
        invocation_identity = sha256_bytes(f"{draft['draft_id']}:{draft['revision']}".encode("utf-8"))
        invocation_id = f"invocation_{invocation_identity[:32]}"
        manifest_artifact_id = f"artifact:{uuid.uuid5(uuid.NAMESPACE_URL, f'lattice-manifest:{invocation_identity}')}"
        preflight: dict[str, Any] = {
            "contract_version": "LatticeContextPreflight/6.0",
            "invocation_id": invocation_id,
            "project_id": draft["project_id"],
            "actor_binding": draft["actor_binding"],
            "role_session_id": draft["role_session_id"],
            "draft_id": draft["draft_id"],
            "draft_revision": draft["revision"],
            "context_mode": draft.get("context_mode", "standard"),
            "ancestry": [{"invocation_id": parent, "manifest_sha256": self.store.get_invocation(parent)["manifest_sha256"]} for parent in draft["parents"]],
            "segments": segments,
            "omitted": omitted,
            "source_bindings": sources,
            "capabilities": {
                **effective_policy,
            },
            "execution": route,
            "response_contract": draft["response_schema"],
            "compilation": {
                "format": "lattice-context-jsonl-v8",
                "protocol_sha256": sha256_bytes(CONTEXT_PROTOCOL_V8),
                "compiled_prompt_ref": f"lattice:blob/{prompt_sha}",
                "compiled_prompt_sha256": prompt_sha,
                "byte_count": len(compiled_prompt),
                "truncation": None,
                "compaction": None,
            },
            "provider_boundary": {
                "observable_request": "compiled prompt over stdin plus declared CLI flags",
                "proof_limits": proof_limits,
            },
        }
        preflight["preflight_sha256"] = sha256_json({key: value for key, value in preflight.items() if key != "preflight_sha256"})
        if preview:
            return preflight, compiled_prompt

        manifest: dict[str, Any] = {
            **preflight,
            "generated_object": generated_object(kind="lattice-context-manifest", schema_name="lattice-context-manifest", schema_version=8, subject=f"lattice-invocation:{invocation_id}", sources=sources, artifact_id=manifest_artifact_id, generated_at=draft["updated_at"]),
            "contract_version": "LatticeContextManifest/8.0",
            "result": {"state": "pending", "attempts_ref": f"lattice:invocation/{invocation_id}/attempts"},
        }
        manifest["manifest_sha256"] = sha256_json({key: value for key, value in manifest.items() if key != "manifest_sha256"})
        return manifest, compiled_prompt
