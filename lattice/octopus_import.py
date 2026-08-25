"""Import exact Octopus conversation projections without inventing hidden context."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

import httpx

from .contracts import (
    BlockDisposition,
    ContextSelection,
    DraftCreate,
    ProviderRoute,
)
from .operations import Lattice
from .store import LatticeStore
from .canonical import strict_json_loads
from .http_boundary import HTTPBoundaryError, bounded_http_get, canonical_origin, require_opaque_segment


TURN_RE = re.compile(r"^### ([UA])\n(.*?)(?=^### [UA]\n|\Z)", re.MULTILINE | re.DOTALL)
REFERENCE_RE = re.compile(r"^[ICHTSPX][1-9][0-9]*(?:\.(?:[1-9][0-9]*|c|r))*$")
ABSENT_RESPONSE_SHA256 = hashlib.sha256(b"lattice:absent-historical-response-v1").hexdigest()


def _metadata(transcript: str) -> dict[str, str]:
    first = transcript.splitlines()[0] if transcript else ""
    if not first.startswith("### META "):
        raise ValueError("Octopus transcript is not a bound format-3 projection")
    fields: dict[str, str] = {}
    for token in first.removeprefix("### META ").split():
        if "=" not in token:
            raise ValueError("Octopus transcript has an ambiguous metadata token")
        key, value = token.split("=", 1)
        if key in fields:
            raise ValueError(f"Octopus transcript repeats metadata field: {key}")
        fields[key] = value
    if fields.get("fmt") != "3" or not fields.get("thread") or not fields.get("source") or not fields.get("date") or not fields.get("refs") or not fields.get("rounds", "").isdigit() or not re.fullmatch(r"[0-9a-f]{64}", fields.get("map", "")) or not re.fullmatch(r"[0-9a-f]{64}", fields.get("source_sha256", "")):
        raise ValueError("Octopus transcript is not a bound format-3 projection")
    return fields


def _octopus_bytes(value: Any) -> bytes:
    if isinstance(value, str):
        return value.encode("utf-8")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_reference_catalogue(refs: bytes, expected_map: str) -> dict[str, dict[str, Any]]:
    if hashlib.sha256(refs).hexdigest() != expected_map:
        raise ValueError("Octopus reference catalogue does not match transcript binding")
    lines = refs.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    entries: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(lines, start=1):
        try:
            entry = strict_json_loads(line.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"Octopus reference catalogue has invalid JSON at line {line_number}") from error
        required = {"ref", "kind", "label", "value", "children", "bytes", "sha256", "provider_type", "origins", "pointer", "disposition"}
        if not isinstance(entry, dict) or not required.issubset(entry):
            raise ValueError(f"Octopus reference catalogue entry {line_number} is incomplete")
        ref = entry["ref"]
        if not isinstance(ref, str) or REFERENCE_RE.fullmatch(ref) is None or ref in entries:
            raise ValueError(f"Octopus reference catalogue entry {line_number} has an invalid or duplicate ref")
        children = entry["children"]
        if not isinstance(children, list) or any(not isinstance(child, str) for child in children):
            raise ValueError(f"Octopus reference catalogue entry {ref} has invalid children")
        payload = children if children else entry["value"]
        exact = _octopus_bytes(payload)
        if entry["bytes"] != len(exact) or entry["sha256"] != hashlib.sha256(exact).hexdigest():
            raise ValueError(f"Octopus reference catalogue entry {ref} fails its byte binding")
        if not isinstance(entry["kind"], str) or not isinstance(entry["label"], str) or not isinstance(entry["bytes"], int) or isinstance(entry["bytes"], bool) or entry["bytes"] < 0 or not isinstance(entry["provider_type"], str) or not isinstance(entry["pointer"], str) or not isinstance(entry["disposition"], str):
            raise ValueError(f"Octopus reference catalogue entry {ref} has invalid metadata")
        if not isinstance(entry["origins"], list) or any(not isinstance(origin, dict) or not isinstance(origin.get("ref"), str) or not isinstance(origin.get("pointer"), str) or re.fullmatch(r"[0-9a-f]{64}", origin.get("sha256", "")) is None for origin in entry["origins"]):
            raise ValueError(f"Octopus reference catalogue entry {ref} has invalid origins")
        entries[ref] = entry
    dangling = sorted({child for entry in entries.values() for child in entry["children"] if child not in entries})
    if dangling:
        raise ValueError(f"Octopus reference catalogue has dangling children: {dangling}")
    non_nested = sorted(child for ref, entry in entries.items() for child in entry["children"] if not child.startswith(ref + "."))
    if non_nested:
        raise ValueError(f"Octopus reference catalogue has non-nested children: {non_nested}")
    return entries


class OctopusImporter:
    MAX_PROJECTION_BYTES = 64 * 1024 * 1024

    def __init__(self, store: LatticeStore, lattice: Lattice, base_url: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self.store = store
        self.lattice = lattice
        self.base_url = base_url.rstrip("/")
        try:
            self.origin = canonical_origin(self.base_url, origin_only=True)
        except HTTPBoundaryError as error:
            raise ValueError(f"invalid Octopus origin: {error}") from error
        self.transport = transport
        self.boot_id = f"octopus-importer-{uuid.uuid4().hex}"

    def fetch(self, conversation_id: str) -> tuple[str, bytes]:
        try:
            conversation_id = require_opaque_segment(conversation_id, label="Octopus conversation ID")
        except HTTPBoundaryError as error:
            raise ValueError(str(error)) from error
        try:
            response = bounded_http_get(f"{self.base_url}/api/conversations/{conversation_id}/transcript.txt", allowed_origins={self.origin}, max_bytes=self.MAX_PROJECTION_BYTES, timeout=60, transport=self.transport)
            refs = bounded_http_get(f"{self.base_url}/api/conversations/{conversation_id}/transcript.refs.jsonl", allowed_origins={self.origin}, max_bytes=self.MAX_PROJECTION_BYTES, timeout=60, transport=self.transport)
        except HTTPBoundaryError as error:
            raise ValueError(f"Octopus projection fetch failed: {error}") from error
        try:
            transcript = response.content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ValueError("Octopus transcript is not exact UTF-8") from error
        return transcript, refs.content

    def import_conversation(self, *, project_id: str, conversation_id: str, provider: str, transcript: str | None = None, refs: bytes | None = None) -> dict[str, Any]:
        try:
            require_opaque_segment(conversation_id, label="Octopus conversation ID")
        except HTTPBoundaryError as error:
            raise ValueError(str(error)) from error
        if transcript is None or refs is None:
            transcript, refs = self.fetch(conversation_id)
        transcript_sha = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
        refs_sha = hashlib.sha256(refs).hexdigest()
        metadata = _metadata(transcript)
        map_sha = metadata["map"]
        source_sha = metadata["source_sha256"]
        if metadata.get("thread") != conversation_id:
            raise ValueError("Octopus transcript identity does not match the requested conversation")
        verify_reference_catalogue(refs, map_sha)
        # The projection renderer contributes exactly one separator newline after each
        # visible block. Preserve all other projected bytes; `.strip()` would silently
        # rewrite leading whitespace and therefore falsify the imported transcript.
        turns = [(role, body[:-1] if body.endswith("\n") else body) for role, body in TURN_RE.findall(transcript)]
        visible_user_turns = sum(kind == "U" for kind, _body in turns)
        if visible_user_turns != int(metadata["rounds"]):
            raise ValueError(f"Octopus transcript declares {metadata['rounds']} user rounds but projects {visible_user_turns}")
        route_identities = {
            "codex": ("openai", "codex"),
            "chatgpt": ("openai", "codex"),
            "openai": ("openai", "codex"),
            "claude": ("anthropic", "claude"),
            "claude-code": ("anthropic", "claude"),
            "anthropic": ("anthropic", "claude"),
        }
        route_identity = route_identities.get(provider.casefold())
        bound_provider = metadata.get("source", "").casefold()
        source_identity = route_identities.get(bound_provider)
        if route_identity is None:
            raise ValueError(f"unsupported historical provider identity: {provider}")
        if source_identity is None or source_identity != route_identity:
            raise ValueError("requested historical provider does not match the bound Octopus source")
        transcript_blob = self.store.blobs.put(transcript.encode("utf-8"))
        refs_blob = self.store.blobs.put(refs)
        claim = self.store.claim_conversation_import(project_id=project_id, conversation_id=conversation_id, provider=bound_provider, transcript_sha256=transcript_sha, refs_sha256=refs_sha, boot_id=self.boot_id)
        if not claim["claimed"]:
            return self.store.get_conversation_import(claim["import_id"], include_result=True)["result"]
        try:
            role_name = f"Imported {bound_provider}"
            import_provenance = {"kind": "octopus-format-3-import-v1", "conversation_id": conversation_id, "declared_archive_source_sha256": source_sha, "archive_source_hash_state": "declared-by-projection-not-recomputed", "transcript_ref": f"lattice:blob/{transcript_blob}", "transcript_sha256": transcript_sha, "reference_catalogue_ref": f"lattice:blob/{refs_blob}", "reference_catalogue_sha256": refs_sha, "map_sha256": map_sha}
            setup = self.store.ensure_conversation_import_setup(claim["import_id"], role_name=role_name, session_label=f"{bound_provider}:{conversation_id}", proof_limit=f"The historical provider and harness context for this invocation is unavailable. Visible transcript ancestry does not prove what the model received. The exact transcript is lattice:blob/{transcript_blob}; its verified format-3 reference catalogue is lattice:blob/{refs_blob}. The archive source hash was declared by that projection and was not independently recomputed by Lattice.", provenance={**import_provenance, "kind": "octopus-provider-proof-limit-v1"})
            route = ProviderRoute(route_kind="historical-import", provider=route_identity[0], model=f"historical-{bound_provider}", harness=route_identity[1], effort="high")
            invocations: list[str] = []
            pending_prompts: list[str] = []
            previous: str | None = None
            for kind, body in turns:
                if kind == "U":
                    pending_prompts.append(body)
                    continue
                prompt = "\n\n".join(pending_prompts) if pending_prompts else "[No visible user prompt preceded this imported response.]"
                pending_prompts.clear()
                ordinal = len(invocations)
                turn = self.store.claim_conversation_import_turn(claim["import_id"], ordinal=ordinal, prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(), response_sha256=hashlib.sha256(body.encode()).hexdigest())
                if turn["draft_id"] is None:
                    draft = self.store.create_draft(DraftCreate(project_id=project_id, role_session_id=setup["role_session_id"], prompt=prompt, route=route, selections=[ContextSelection(block_id=setup["proof_limit_block_id"], disposition=BlockDisposition.provider_opaque, plane="state", label="historical-context-proof-limit")]), import_turn=(claim["import_id"], ordinal))
                    turn = self.store.get_conversation_import_turn(claim["import_id"], ordinal)
                else:
                    draft = self.store.get_draft(turn["draft_id"])
                if turn["invocation_id"] is None:
                    invocation = self.lattice.seal(draft["draft_id"], import_turn=(claim["import_id"], ordinal))
                    turn = self.store.get_conversation_import_turn(claim["import_id"], ordinal)
                else:
                    invocation = self.store.get_invocation(turn["invocation_id"])
                if turn["state"] != "completed":
                    invocation = self.store.attach_imported_turn_output(claim["import_id"], ordinal=ordinal, response=body, provenance={**import_provenance, "kind": "octopus-visible-response-import-v1", "visible_turn_ordinal": ordinal}, previous_invocation_id=previous)
                previous = invocation["invocation_id"]
                invocations.append(previous)
            pending_drafts: list[str] = []
            for prompt in pending_prompts:
                ordinal = len(invocations) + len(pending_drafts)
                turn = self.store.claim_conversation_import_turn(claim["import_id"], ordinal=ordinal, prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(), response_sha256=ABSENT_RESPONSE_SHA256, response_state="absent")
                if turn["draft_id"] is None:
                    draft = self.store.create_draft(DraftCreate(project_id=project_id, role_session_id=setup["role_session_id"], prompt=prompt, route=route, selections=[ContextSelection(block_id=setup["proof_limit_block_id"], disposition=BlockDisposition.provider_opaque, plane="state", label="historical-context-proof-limit")], parent_invocation_ids=[previous] if previous else []), import_turn=(claim["import_id"], ordinal))
                else:
                    draft = self.store.get_draft(turn["draft_id"])
                if draft["state"] != "editable":
                    raise ValueError("unanswered imported prompt is no longer an editable draft")
                pending_drafts.append(draft["draft_id"])
            result = {"import_id": claim["import_id"], "conversation_id": conversation_id, "provider": bound_provider, "map_sha256": map_sha, "source_sha256": source_sha, "source_sha256_state": "declared-by-projection-not-recomputed", "transcript_sha256": transcript_sha, "transcript_ref": f"lattice:blob/{transcript_blob}", "refs_ref": f"lattice:blob/{refs_blob}", "role_session_id": setup["role_session_id"], "invocation_ids": invocations, "pending_draft_ids": pending_drafts}
            self.store.complete_conversation_import(claim["import_id"], result=result)
            return result
        except BaseException as error:
            self.store.fail_conversation_import(claim["import_id"], error_text=f"{type(error).__name__}: {error}")
            raise
