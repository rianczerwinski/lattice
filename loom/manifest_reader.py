"""Independent reconstruction and verification of sealed invocation context."""

from __future__ import annotations

import re
from typing import Any

from axis_model_runner import DEFAULT_ADAPTERS, SubscriptionRouteUnavailable, validate_execution_policy

from .canonical import canonical_bytes, sha256_bytes, sha256_json
from .store import LoomStore


class ManifestVerificationError(ValueError):
    pass


ARTIFACT_ID = re.compile(r"artifact:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
TRACKING_TIMESTAMP = re.compile(r"[0-9]{8}T[0-9]{6}(?:Z[+-][0-9]{2})?")
SEMVER = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
SHA256 = re.compile(r"[0-9a-f]{64}")


def _verify_generated_object(manifest: dict[str, Any]) -> None:
    envelope = manifest.get("generated_object")
    required = {"contract_version", "role", "kind", "schema", "artifact_id", "generated_at", "producer", "subject", "sources"}
    if not isinstance(envelope, dict) or set(envelope) != required:
        raise ManifestVerificationError("manifest generated-object envelope is incomplete or carries undeclared fields")
    if envelope["contract_version"] != 1 or envelope["role"] != "canonical" or envelope["kind"] != "loom-context-manifest":
        raise ManifestVerificationError("manifest generated-object identity is invalid")
    schema = envelope["schema"]
    if not isinstance(schema, dict) or set(schema) != {"name", "version"} or schema["name"] != "loom-context-manifest" or not isinstance(schema["version"], int) or isinstance(schema["version"], bool) or schema["version"] < 1:
        raise ManifestVerificationError("manifest generated-object schema is invalid")
    producer = envelope["producer"]
    if not isinstance(producer, dict) or set(producer) != {"name", "version"} or producer["name"] != "axis-loom" or not isinstance(producer["version"], str) or SEMVER.fullmatch(producer["version"]) is None:
        raise ManifestVerificationError("manifest generated-object producer is invalid")
    if not isinstance(envelope["artifact_id"], str) or ARTIFACT_ID.fullmatch(envelope["artifact_id"]) is None or not isinstance(envelope["generated_at"], str) or TRACKING_TIMESTAMP.fullmatch(envelope["generated_at"]) is None:
        raise ManifestVerificationError("manifest generated-object generation identity is invalid")
    if envelope["subject"] != f"loom-invocation:{manifest.get('invocation_id')}":
        raise ManifestVerificationError("manifest generated-object subject does not match its invocation")
    sources = envelope["sources"]
    if not isinstance(sources, list) or not sources:
        raise ManifestVerificationError("manifest generated-object requires exact sources")
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"ref", "revision", "sha256"} or not isinstance(source["ref"], str) or not source["ref"]:
            raise ManifestVerificationError("manifest generated-object source is malformed")
        revision, digest = source["revision"], source["sha256"]
        if revision is not None and (not isinstance(revision, str) or not revision):
            raise ManifestVerificationError("manifest generated-object source revision is malformed")
        if digest is not None and (not isinstance(digest, str) or SHA256.fullmatch(digest) is None):
            raise ManifestVerificationError("manifest generated-object source digest is malformed")
        if revision is None and digest is None:
            raise ManifestVerificationError("manifest generated-object source lacks immutable state")


def _independent_segment_bytes(segment: dict[str, Any], content: bytes, compilation_format: str) -> bytes:
    if compilation_format == "loom-segment-stream-v1":
        prefix = "<<<LOOM {plane} {label} {block_id} {sha256}>>>\n".format(**segment).encode("utf-8")
        return prefix + content + b"\n<<<END>>>\n"
    if compilation_format not in {"loom-context-jsonl-v2", "loom-context-jsonl-v3"}:
        raise ManifestVerificationError(f"unsupported compiled context format: {compilation_format}")
    try:
        decoded = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ManifestVerificationError("context segment is not exact UTF-8") from error
    record = {
        "kind": "loom-context-segment-v3" if compilation_format == "loom-context-jsonl-v3" else "loom-context-segment-v2",
        "ordinal": segment["ordinal"],
        "plane": segment["plane"],
        "label": segment["label"],
        "block_id": segment["block_id"],
        "content_sha256": segment["sha256"],
        "semantic_type": segment["semantic_type"],
        "status": segment["status"],
        "disposition": segment["disposition"],
        "derived_from_block_ids": segment.get("derived_from_block_ids", []),
        "reason": segment.get("reason"),
        "content": decoded,
    }
    if compilation_format == "loom-context-jsonl-v3":
        record = {
            **{key: value for key, value in record.items() if key not in {"disposition", "derived_from_block_ids", "reason", "content"}},
            "authority": segment["authority"],
            "evidence_level": segment["evidence_level"],
            "sensitivity": segment["sensitivity"],
            "source_ref": segment["source_ref"],
            "source_sha256": segment["source_sha256"],
            "producing_invocation_id": segment["producing_invocation_id"],
            "transformation": segment["transformation"],
            "disposition": segment["disposition"],
            "derived_from_block_ids": segment.get("derived_from_block_ids", []),
            "reason": segment.get("reason"),
            "content": decoded,
        }
    return canonical_bytes(record) + b"\n"


def verify_and_reconstruct(store: LoomStore, manifest: dict[str, Any]) -> bytes:
    _verify_generated_object(manifest)
    claimed_manifest_sha = manifest.get("manifest_sha256")
    actual_manifest_sha = sha256_json({key: value for key, value in manifest.items() if key != "manifest_sha256"})
    if claimed_manifest_sha != actual_manifest_sha:
        raise ManifestVerificationError("manifest bytes do not match its declared identity")
    generated_source_values = manifest["generated_object"]["sources"]
    compilation_format = manifest["compilation"].get("format")
    if compilation_format == "loom-context-jsonl-v3":
        if manifest.get("source_bindings") != generated_source_values:
            raise ManifestVerificationError("manifest preflight source bindings disagree with its canonical source envelope")
        preflight_keys = {
            "invocation_id",
            "project_id",
            "role_session_id",
            "draft_id",
            "draft_revision",
            "context_mode",
            "ancestry",
            "segments",
            "omitted",
            "source_bindings",
            "capabilities",
            "execution",
            "response_contract",
            "compilation",
            "provider_boundary",
        }
        preflight_material = {"contract_version": "LoomContextPreflight/1.0", **{key: manifest[key] for key in preflight_keys}}
        if manifest.get("preflight_sha256") != sha256_json(preflight_material):
            raise ManifestVerificationError("manifest does not preserve the exact inspected preflight context")
    generated_sources = {item["ref"]: item for item in generated_source_values}
    if len(generated_sources) != len(generated_source_values):
        raise ManifestVerificationError("manifest contains ambiguous duplicate source references")
    session = store.get_role_session(manifest["role_session_id"])
    if session["project_id"] != manifest["project_id"]:
        raise ManifestVerificationError("manifest role session belongs to another project")
    draft = store.get_draft(manifest["draft_id"])
    ancestry_ids = [item["invocation_id"] for item in manifest["ancestry"]]
    if draft["project_id"] != manifest["project_id"] or draft["role_session_id"] != manifest["role_session_id"] or draft["revision"] != manifest["draft_revision"] or draft["context_mode"] != manifest["context_mode"] or draft["parents"] != ancestry_ids:
        raise ManifestVerificationError("manifest does not match its sealed draft identity and ancestry")
    route = manifest["execution"]
    if route != draft["route"]:
        raise ManifestVerificationError("manifest execution route does not match its sealed draft")
    adapter = next((item for item in DEFAULT_ADAPTERS if item.provider_code == route["provider"] and item.cli == route["harness"]), None)
    if adapter is None:
        raise ManifestVerificationError("manifest execution route is not qualified")
    try:
        effective_policy = validate_execution_policy(
            adapter,
            session_policy=route["session_policy"],
            tools=route["tools"],
            permissions=route["permissions"],
            workspace=route["workspace"],
            network_policy=route["network_policy"],
        )
    except SubscriptionRouteUnavailable as error:
        raise ManifestVerificationError(f"manifest execution policy is not enforceable: {error}") from error
    if manifest["capabilities"] != effective_policy:
        raise ManifestVerificationError("manifest capabilities do not match the enforced provider policy")
    for parent in manifest["ancestry"]:
        invocation = store.get_invocation(parent["invocation_id"])
        if invocation["project_id"] != manifest["project_id"] or invocation["manifest_sha256"] != parent["manifest_sha256"]:
            raise ManifestVerificationError(f"manifest ancestry does not resolve exactly: {parent['invocation_id']}")
    output: list[bytes] = []
    expected_contract = {"loom-segment-stream-v1": ("LoomContextManifest/1.0", 1), "loom-context-jsonl-v2": ("LoomContextManifest/2.0", 2), "loom-context-jsonl-v3": ("LoomContextManifest/3.0", 3)}.get(compilation_format)
    if expected_contract is None or manifest.get("contract_version") != expected_contract[0] or manifest["generated_object"]["schema"].get("version") != expected_contract[1]:
        raise ManifestVerificationError("context format, manifest contract, and generated-object schema disagree")
    if compilation_format in {"loom-context-jsonl-v2", "loom-context-jsonl-v3"} and manifest.get("response_contract") != draft["response_schema"]:
        raise ManifestVerificationError("manifest response contract does not match its sealed draft")
    all_entries = manifest["segments"] + manifest["omitted"]
    ordinals = [entry["ordinal"] for entry in all_entries]
    if sorted(ordinals) != list(range(len(all_entries))):
        raise ManifestVerificationError("context segment ordering is ambiguous")
    expected_source_refs: set[str] = set()
    for segment in all_entries:
        block = store.get_block(segment["block_id"], include_content=False)
        content = store.blobs.get(block["content_sha256"])
        if block["project_id"] != manifest["project_id"]:
            raise ManifestVerificationError(f"context block belongs to another project: {segment['block_id']}")
        if block["semantic_type"] != segment["semantic_type"]:
            raise ManifestVerificationError(f"context semantic type is not ledger-bound: {segment['block_id']}")
        if compilation_format == "loom-context-jsonl-v3":
            transformation = __import__("json").loads(block["transformation_json"]) if block["transformation_json"] else None
            provenance = {"authority": block["authority"], "evidence_level": block["evidence_level"], "sensitivity": block["sensitivity"], "source_ref": block["source_ref"], "source_sha256": block["source_sha256"], "producing_invocation_id": block["producing_invocation_id"], "transformation": transformation}
            if any(segment.get(key) != value for key, value in provenance.items()):
                raise ManifestVerificationError(f"context provenance is not ledger-bound: {segment['block_id']}")
        try:
            historical_status = store.get_block_status_revision(segment["block_id"], segment["status_revision"])
        except (KeyError, TypeError) as error:
            raise ManifestVerificationError(f"context status revision does not resolve: {segment['block_id']}") from error
        if historical_status != segment["status"]:
            raise ManifestVerificationError(f"context status is not ledger-bound: {segment['block_id']}")
        if sha256_bytes(content) != segment["sha256"] or block["content_sha256"] != segment["sha256"]:
            raise ManifestVerificationError(f"context block changed: {segment['block_id']}")
        source = generated_sources.get(f"loom:block/{segment['block_id']}")
        if not source or source["sha256"] != segment["sha256"] or source["revision"] != str(segment["status_revision"]):
            raise ManifestVerificationError(f"context source is not exactly bound: {segment['block_id']}")
        expected_source_refs.add(source["ref"])
        derived = segment.get("derived_from_block_ids", [])
        for source_block_id in derived:
            source_block = store.get_block(source_block_id, include_content=False)
            if source_block["project_id"] != manifest["project_id"]:
                raise ManifestVerificationError(f"derived context source belongs to another project: {source_block_id}")
            derived_source = generated_sources.get(f"loom:block/{source_block_id}")
            if not derived_source or derived_source["sha256"] != source_block["content_sha256"] or not str(derived_source["revision"]).isdigit():
                raise ManifestVerificationError(f"derived context source is not exactly bound: {source_block_id}")
            expected_source_refs.add(derived_source["ref"])
        if segment["disposition"] == "summarized":
            import json
            transformation = json.loads(block["transformation_json"] or "null")
            if not derived or not isinstance(transformation, dict) or transformation.get("kind") != "summary" or transformation.get("source_block_ids") != derived:
                raise ManifestVerificationError(f"summary lineage is invalid: {segment['block_id']}")
        if segment in manifest["segments"]:
            if segment["disposition"] not in {"selected", "summarized"}:
                raise ManifestVerificationError("non-selected context was included in the provider prompt")
            output.append(_independent_segment_bytes(segment, content, compilation_format))
        elif segment["disposition"] in {"selected", "summarized"}:
            raise ManifestVerificationError("selected context was mislabeled as omitted")
    if set(generated_sources) != expected_source_refs:
        raise ManifestVerificationError("generated-object sources do not exactly match manifest context lineage")
    reconstructed = b"".join(output)
    if sha256_bytes(reconstructed) != manifest["compilation"]["compiled_prompt_sha256"]:
        raise ManifestVerificationError("compiled prompt does not reconstruct from declared context")
    prompt_ref = manifest["compilation"]["compiled_prompt_ref"]
    if not prompt_ref.startswith("loom:blob/") or store.blobs.get(prompt_ref.removeprefix("loom:blob/")) != reconstructed:
        raise ManifestVerificationError("compiled prompt artifact does not resolve to reconstructed bytes")
    return reconstructed
