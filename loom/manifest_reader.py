"""Independent reconstruction and verification of sealed invocation context."""

from __future__ import annotations

from typing import Any

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
    generated_sources = {item["ref"]: item for item in manifest["generated_object"]["sources"]}
    output: list[bytes] = []
    ordinals: list[int] = []
    for segment in manifest["segments"]:
        ordinals.append(segment["ordinal"])
        block = store.get_block(segment["block_id"], include_content=False)
        content = store.blobs.get(block["content_sha256"])
        if sha256_bytes(content) != segment["sha256"] or block["content_sha256"] != segment["sha256"]:
            raise ManifestVerificationError(f"context block changed: {segment['block_id']}")
        source = generated_sources.get(f"loom:block/{segment['block_id']}")
        if not source or source["sha256"] != segment["sha256"]:
            raise ManifestVerificationError(f"context source is not exactly bound: {segment['block_id']}")
        output.append(_independent_segment_bytes(segment, content))
    if ordinals != sorted(ordinals) or len(ordinals) != len(set(ordinals)):
        raise ManifestVerificationError("context segment ordering is ambiguous")
    reconstructed = b"".join(output)
    if sha256_bytes(reconstructed) != manifest["compilation"]["compiled_prompt_sha256"]:
        raise ManifestVerificationError("compiled prompt does not reconstruct from declared context")
    return reconstructed
