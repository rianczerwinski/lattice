"""Import exact Octopus conversation projections without inventing hidden context."""

from __future__ import annotations

import hashlib
import re
from typing import Any

import httpx

from .contracts import (
    BlockDisposition,
    ContextBlockCreate,
    ContextSelection,
    DraftCreate,
    EdgeRelation,
    ProviderRoute,
    RoleCreate,
    RoleSessionCreate,
)
from .operations import Loom
from .store import LoomStore


META_RE = re.compile(r"^### META .*?map=([0-9a-f]{64}).*?source_sha256=([0-9a-f]{64})", re.MULTILINE)
TURN_RE = re.compile(r"^### ([UA])\n(.*?)(?=^### [UA]\n|\Z)", re.MULTILINE | re.DOTALL)


class OctopusImporter:
    def __init__(self, store: LoomStore, loom: Loom, base_url: str) -> None:
        self.store = store
        self.loom = loom
        self.base_url = base_url.rstrip("/")

    def fetch(self, conversation_id: str) -> tuple[str, bytes]:
        response = httpx.get(f"{self.base_url}/api/conversations/{conversation_id}/transcript.txt", timeout=60)
        response.raise_for_status()
        refs = httpx.get(f"{self.base_url}/api/conversations/{conversation_id}/transcript.refs.jsonl", timeout=60)
        refs.raise_for_status()
        return response.text, refs.content

    def import_conversation(self, *, project_id: str, conversation_id: str, provider: str, transcript: str | None = None, refs: bytes | None = None) -> dict[str, Any]:
        if transcript is None or refs is None:
            transcript, refs = self.fetch(conversation_id)
        metadata = META_RE.search(transcript)
        if not metadata:
            raise ValueError("Octopus transcript is not a bound format-3 projection")
        map_sha, source_sha = metadata.groups()
        if hashlib.sha256(refs).hexdigest() != map_sha:
            raise ValueError("Octopus reference catalogue does not match transcript binding")
        turns = [(role, body.strip()) for role, body in TURN_RE.findall(transcript) if body.strip()]
        role_name = f"Imported {provider}"
        role = self.store.find_role_by_name(project_id, role_name)
        if role is None:
            role = self.store.create_role(project_id, RoleCreate(name=role_name, instructions="Historical provider session. Loom did not observe its original instruction stack; never infer missing context.", incentives=[], authority_limits=["read-only historical import"]))
        session = self.store.create_role_session(RoleSessionCreate(project_id=project_id, role_id=role["role_id"], label=f"{provider}:{conversation_id}"))
        opaque = self.store.create_context_block(ContextBlockCreate(project_id=project_id, semantic_type="provider-proof-limit", content="The historical provider and harness context for this invocation is unavailable. Visible transcript ancestry does not prove what the model received.", status="unknown", source_ref=f"octopus:conversation/{conversation_id}", source_sha256=source_sha, authority="octopus-projection", evidence_level="explicit-proof-limit"))
        route = ProviderRoute(provider="openai" if provider in {"codex", "chatgpt"} else "anthropic", model=f"historical-{provider}", harness="codex" if provider in {"codex", "chatgpt"} else "claude", effort="high")
        invocations: list[str] = []
        pending_prompts: list[str] = []
        previous: str | None = None
        for kind, body in turns:
            if kind == "U":
                pending_prompts.append(body)
                continue
            prompt = "\n\n".join(pending_prompts) or "[No visible user prompt preceded this imported response.]"
            pending_prompts.clear()
            draft = self.store.create_draft(DraftCreate(project_id=project_id, role_session_id=session["role_session_id"], prompt=prompt, route=route, selections=[ContextSelection(block_id=opaque["block_id"], disposition=BlockDisposition.provider_opaque, plane="state", label="historical-context-proof-limit")]))
            invocation = self.loom.seal(draft["draft_id"])
            output = self.store.create_context_block(ContextBlockCreate(project_id=project_id, semantic_type="imported-model-response", content=body, producing_invocation_id=invocation["invocation_id"], source_ref=f"octopus:conversation/{conversation_id}#map={map_sha}", source_sha256=source_sha, authority="octopus-projection", evidence_level="captured-provider-output"))
            invocation = self.store.attach_imported_output(invocation["invocation_id"], output["block_id"])
            if previous is not None:
                self.store.add_edge(project_id=project_id, relation=EdgeRelation.responds_to, source_type="invocation", source_id=invocation["invocation_id"], source_revision=invocation["manifest_sha256"], target_type="invocation", target_id=previous, target_revision=self.store.get_invocation(previous)["manifest_sha256"], metadata={"historical_visible_order": True, "context_inheritance_unproven": True})
            previous = invocation["invocation_id"]
            invocations.append(previous)
        return {"conversation_id": conversation_id, "provider": provider, "map_sha256": map_sha, "source_sha256": source_sha, "role_session_id": session["role_session_id"], "invocation_ids": invocations}
