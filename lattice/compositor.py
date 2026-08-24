"""Provider-neutral compilation of selected context into an immutable manifest."""

from __future__ import annotations

import uuid
from typing import Any

from axis_model_runner import DEFAULT_ADAPTERS, validate_execution_policy

from .canonical import canonical_bytes, generated_object, sha256_bytes, sha256_json
from .contracts import BlockDisposition, ContextSelection
from .store import LatticeConflict, LatticeStore


PLANE_ORDER = {"instruction": 0, "knowledge": 1, "state": 2, "ancestry": 3, "current_prompt": 4}
MAX_COMPILED_CONTEXT_BYTES = 64 * 1024 * 1024


def _segment_bytes(segment: dict[str, Any], content: str) -> bytes:
    record = {
        "kind": "lattice-context-segment-v4",
        "ordinal": segment["ordinal"],
        "plane": segment["plane"],
        "label": segment["label"],
        "block_id": segment["block_id"],
        "content_sha256": segment["sha256"],
        "semantic_type": segment["semantic_type"],
        "status": segment["status"],
        "authority": segment["authority"],
        "evidence_level": segment["evidence_level"],
        "sensitivity": segment["sensitivity"],
        "source_ref": segment["source_ref"],
        "source_sha256": segment["source_sha256"],
        "producing_invocation_id": segment["producing_invocation_id"],
        "transformation": segment["transformation"],
        "disposition": segment["disposition"],
        "derived_from_block_ids": segment["derived_from_block_ids"],
        "reason": segment["reason"],
        "content": content,
    }
    return canonical_bytes(record) + b"\n"


class ContextCompositor:
    def __init__(self, store: LatticeStore) -> None:
        self.store = store

    def _implicit_selections(self, draft: dict[str, Any]) -> list[ContextSelection]:
        selections: list[ContextSelection] = []
        actor_binding = draft["actor_binding"]
        if actor_binding["kind"] == "role-session":
            session = self.store.get_role_session(actor_binding["role_session_id"])
            role = self.store.get_role(session["role_id"])
            selections.append(ContextSelection(block_id=role["instructions_block_id"], disposition=BlockDisposition.selected, plane="instruction", label=f"role:{role['name']}"))
            for default in self.store.list_role_context_defaults(role["role_id"]):
                selections.append(ContextSelection(block_id=default["block_id"], disposition=BlockDisposition.selected, plane="state", label=f"module:{default['module_id']}"))
        for parent_id in draft["parents"]:
            parent = self.store.get_invocation(parent_id)
            if not parent.get("output_block_id"):
                raise LatticeConflict(f"parent {parent_id} has no consumable output")
            selections.append(ContextSelection(block_id=parent["output_block_id"], disposition=BlockDisposition.selected, plane="ancestry", label=f"parent:{parent_id}"))
        selections.append(ContextSelection(block_id=draft["prompt_block_id"], disposition=BlockDisposition.selected, plane="current_prompt", label="user-prompt"))
        return selections

    def compile(self, draft: dict[str, Any], *, preview: bool = False) -> tuple[dict[str, Any], bytes]:
        explicit = [ContextSelection.model_validate(item) for item in draft["selections"]]
        combined = explicit if draft.get("context_mode", "standard") == "explicit" else self._implicit_selections(draft) + explicit
        identities: set[tuple[str, str, str, str, tuple[str, ...], str | None]] = set()
        entries: list[tuple[ContextSelection, dict[str, Any]]] = []
        for selection in combined:
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
            block = self.store.get_block(selection.block_id)
            if block["project_id"] != draft["project_id"]:
                raise LatticeConflict("context block belongs to another project")
            derived = [self.store.get_block(block_id, include_content=False) for block_id in selection.derived_from_block_ids]
            if any(parent["project_id"] != draft["project_id"] for parent in derived):
                raise LatticeConflict("derived context source belongs to another project")
            if selection.disposition == BlockDisposition.summarized:
                transformation = __import__("json").loads(block["transformation_json"] or "null")
                if not selection.derived_from_block_ids or not isinstance(transformation, dict) or transformation.get("kind") != "summary" or transformation.get("source_block_ids") != selection.derived_from_block_ids:
                    raise LatticeConflict("summarized context requires a summary transformation bound to exact source blocks")
            entries.append((selection, block))
        entries.sort(key=lambda pair: (PLANE_ORDER[pair[0].plane], pair[0].label, pair[0].block_id))

        segments: list[dict[str, Any]] = []
        omitted: list[dict[str, Any]] = []
        prompt_parts: list[bytes] = []
        prompt_byte_count = 0
        sources: list[dict[str, str | None]] = []
        source_identities: set[tuple[str, str, str]] = set()
        for ordinal, (selection, block) in enumerate(entries):
            source = {"ref": f"lattice:block/{block['block_id']}", "revision": str(block["status_revision"]), "sha256": block["content_sha256"]}
            source_identity = (source["ref"], source["revision"], source["sha256"])
            if source_identity not in source_identities:
                source_identities.add(source_identity)
                sources.append(source)
            for derived_block_id in selection.derived_from_block_ids:
                derived = self.store.get_block(derived_block_id, include_content=False)
                derived_source = {"ref": f"lattice:block/{derived_block_id}", "revision": str(derived["status_revision"]), "sha256": derived["content_sha256"]}
                derived_identity = (derived_source["ref"], derived_source["revision"], derived_source["sha256"])
                if derived_identity not in source_identities:
                    source_identities.add(derived_identity)
                    sources.append(derived_source)
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
                "transformation": __import__("json").loads(block["transformation_json"]) if block["transformation_json"] else None,
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
        effective_policy = validate_execution_policy(
            adapter,
            session_policy=route["session_policy"],
            tools=route["tools"],
            permissions=route["permissions"],
            workspace=route["workspace"],
            network_policy=route["network_policy"],
        ) if adapter else None
        invocation_identity = sha256_bytes(f"{draft['draft_id']}:{draft['revision']}".encode("utf-8"))
        invocation_id = f"invocation_{invocation_identity[:32]}"
        manifest_artifact_id = f"artifact:{uuid.uuid5(uuid.NAMESPACE_URL, f'lattice-manifest:{invocation_identity}')}"
        preflight: dict[str, Any] = {
            "contract_version": "LatticeContextPreflight/2.0",
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
                "format": "lattice-context-jsonl-v4",
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
            "generated_object": generated_object(kind="lattice-context-manifest", schema_name="lattice-context-manifest", schema_version=4, subject=f"lattice-invocation:{invocation_id}", sources=sources, artifact_id=manifest_artifact_id, generated_at=draft["updated_at"]),
            "contract_version": "LatticeContextManifest/4.0",
            "result": {"state": "pending", "attempts_ref": f"lattice:invocation/{invocation_id}/attempts"},
        }
        manifest["manifest_sha256"] = sha256_json({key: value for key, value in manifest.items() if key != "manifest_sha256"})
        return manifest, compiled_prompt
