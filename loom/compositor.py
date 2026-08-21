"""Provider-neutral compilation of selected context into an immutable manifest."""

from __future__ import annotations

from typing import Any

from axis_model_runner import DEFAULT_ADAPTERS, validate_execution_policy

from .canonical import canonical_bytes, generated_object, opaque_id, sha256_bytes, sha256_json
from .contracts import BlockDisposition, ContextSelection
from .store import LoomConflict, LoomStore


PLANE_ORDER = {"instruction": 0, "knowledge": 1, "state": 2, "ancestry": 3, "current_prompt": 4}


def _segment_bytes(segment: dict[str, Any], content: str) -> bytes:
    header = f"<<<LOOM {segment['plane']} {segment['label']} {segment['block_id']} {segment['sha256']}>>>\n"
    return (header + content + "\n<<<END>>>\n").encode("utf-8")


class ContextCompositor:
    def __init__(self, store: LoomStore) -> None:
        self.store = store

    def _implicit_selections(self, draft: dict[str, Any]) -> list[ContextSelection]:
        session = self.store.get_role_session(draft["role_session_id"])
        role = self.store.get_role(session["role_id"])
        selections = [ContextSelection(block_id=role["instructions_block_id"], disposition=BlockDisposition.selected, plane="instruction", label=f"role:{role['name']}")]
        for default in self.store.list_role_context_defaults(role["role_id"]):
            selections.append(ContextSelection(block_id=default["block_id"], disposition=BlockDisposition.selected, plane="state", label=f"module:{default['module_id']}"))
        for parent_id in draft["parents"]:
            parent = self.store.get_invocation(parent_id)
            if not parent.get("output_block_id"):
                raise LoomConflict(f"parent {parent_id} has no consumable output")
            selections.append(ContextSelection(block_id=parent["output_block_id"], disposition=BlockDisposition.selected, plane="ancestry", label=f"parent:{parent_id}"))
        selections.append(ContextSelection(block_id=draft["prompt_block_id"], disposition=BlockDisposition.selected, plane="current_prompt", label="user-prompt"))
        return selections

    def compile(self, draft: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
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
                raise LoomConflict("context block belongs to another project")
            derived = [self.store.get_block(block_id, include_content=False) for block_id in selection.derived_from_block_ids]
            if any(parent["project_id"] != draft["project_id"] for parent in derived):
                raise LoomConflict("derived context source belongs to another project")
            if selection.disposition == BlockDisposition.summarized:
                transformation = __import__("json").loads(block["transformation_json"] or "null")
                if not selection.derived_from_block_ids or not isinstance(transformation, dict) or transformation.get("kind") != "summary" or transformation.get("source_block_ids") != selection.derived_from_block_ids:
                    raise LoomConflict("summarized context requires a summary transformation bound to exact source blocks")
            entries.append((selection, block))
        entries.sort(key=lambda pair: (PLANE_ORDER[pair[0].plane], pair[0].label, pair[0].block_id))

        segments: list[dict[str, Any]] = []
        omitted: list[dict[str, Any]] = []
        prompt_parts: list[bytes] = []
        sources: list[dict[str, str | None]] = []
        source_identities: set[tuple[str, str, str]] = set()
        for ordinal, (selection, block) in enumerate(entries):
            source = {"ref": f"loom:block/{block['block_id']}", "revision": str(block["status_revision"]), "sha256": block["content_sha256"]}
            source_identity = (source["ref"], source["revision"], source["sha256"])
            if source_identity not in source_identities:
                source_identities.add(source_identity)
                sources.append(source)
            for derived_block_id in selection.derived_from_block_ids:
                derived = self.store.get_block(derived_block_id, include_content=False)
                derived_source = {"ref": f"loom:block/{derived_block_id}", "revision": str(derived["status_revision"]), "sha256": derived["content_sha256"]}
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
                "derived_from_block_ids": selection.derived_from_block_ids,
                "reason": selection.reason,
            }
            if selection.disposition in {BlockDisposition.selected, BlockDisposition.summarized}:
                segments.append(item)
                prompt_parts.append(_segment_bytes(item, block["content"]))
            else:
                omitted.append(item)

        compiled_prompt = b"".join(prompt_parts)
        prompt_sha = self.store.blobs.put(compiled_prompt)
        route = draft["route"]
        adapter = next((item for item in DEFAULT_ADAPTERS if item.provider_code == route["provider"]), None)
        proof_limits = list(adapter.proof_limits) if adapter else ["provider route has not been qualified"]
        effective_policy = validate_execution_policy(
            adapter,
            session_policy=route["session_policy"],
            tools=route["tools"],
            permissions=route["permissions"],
            workspace=route["workspace"],
            network_policy=route["network_policy"],
        ) if adapter else None
        invocation_id = opaque_id("invocation")
        manifest: dict[str, Any] = {
            "generated_object": generated_object(kind="loom-context-manifest", schema_name="loom-context-manifest", subject=f"loom-invocation:{invocation_id}", sources=sources),
            "contract_version": "LoomContextManifest/1.0",
            "invocation_id": invocation_id,
            "project_id": draft["project_id"],
            "role_session_id": draft["role_session_id"],
            "draft_id": draft["draft_id"],
            "draft_revision": draft["revision"],
            "context_mode": draft.get("context_mode", "standard"),
            "ancestry": [{"invocation_id": parent, "manifest_sha256": self.store.get_invocation(parent)["manifest_sha256"]} for parent in draft["parents"]],
            "segments": segments,
            "omitted": omitted,
            "capabilities": {
                **effective_policy,
            },
            "execution": route,
            "compilation": {
                "format": "loom-segment-stream-v1",
                "compiled_prompt_ref": f"loom:blob/{prompt_sha}",
                "compiled_prompt_sha256": prompt_sha,
                "byte_count": len(compiled_prompt),
                "truncation": None,
                "compaction": None,
            },
            "provider_boundary": {
                "observable_request": "compiled prompt over stdin plus declared CLI flags",
                "proof_limits": proof_limits,
            },
            "result": {"state": "pending", "attempts_ref": f"loom:invocation/{invocation_id}/attempts"},
        }
        manifest["manifest_sha256"] = sha256_json({key: value for key, value in manifest.items() if key != "manifest_sha256"})
        return manifest, compiled_prompt
