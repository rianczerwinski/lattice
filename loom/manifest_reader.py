"""Independent reconstruction and verification of sealed invocation context."""

from __future__ import annotations

from typing import Any

from axis_model_runner import DEFAULT_ADAPTERS, SubscriptionRouteUnavailable, validate_execution_policy

from .canonical import sha256_bytes, sha256_json
from .store import LoomStore


class ManifestVerificationError(ValueError):
    pass


def _independent_segment_bytes(segment: dict[str, Any], content: bytes) -> bytes:
    prefix = "<<<LOOM {plane} {label} {block_id} {sha256}>>>\n".format(**segment).encode("utf-8")
    return prefix + content + b"\n<<<END>>>\n"


def verify_and_reconstruct(store: LoomStore, manifest: dict[str, Any]) -> bytes:
    claimed_manifest_sha = manifest.get("manifest_sha256")
    actual_manifest_sha = sha256_json({key: value for key, value in manifest.items() if key != "manifest_sha256"})
    if claimed_manifest_sha != actual_manifest_sha:
        raise ManifestVerificationError("manifest bytes do not match its declared identity")
    generated_source_values = manifest["generated_object"]["sources"]
    generated_sources = {item["ref"]: item for item in generated_source_values}
    if len(generated_sources) != len(generated_source_values):
        raise ManifestVerificationError("manifest contains ambiguous duplicate source references")
    session = store.get_role_session(manifest["role_session_id"])
    if session["project_id"] != manifest["project_id"]:
        raise ManifestVerificationError("manifest role session belongs to another project")
    route = manifest["execution"]
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
            output.append(_independent_segment_bytes(segment, content))
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
