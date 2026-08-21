"""Independent captured-projection contract for Octopus imports."""

from __future__ import annotations

import hashlib

import pytest

from loom.octopus_import import OctopusImporter


@pytest.mark.contract
def test_contract_import_preserves_turns_and_names_hidden_context(loom_env) -> None:
    refs = b'{"reference":"I1","value":"hidden"}\n'
    map_sha = hashlib.sha256(refs).hexdigest()
    source_sha = "a" * 64
    transcript = f"""### META thread=abc source=codex date=2026-08-18 rounds=2 fmt=3 refs=abc.refs.jsonl map={map_sha} source_sha256={source_sha} leaf=abc
### U
P1
### A
A1
### U
P2
### A
A2
"""
    importer = OctopusImporter(loom_env["store"], loom_env["loom"], "http://unused")

    result = importer.import_conversation(project_id=loom_env["project"]["project_id"], conversation_id="abc", provider="codex", transcript=transcript, refs=refs)

    assert len(result["invocation_ids"]) == 2
    second = loom_env["loom"].inspect(result["invocation_ids"][1])
    assert second["state"] == "imported"
    assert second["output_block"]["content"] == "A2"
    assert second["manifest"]["omitted"][0]["disposition"] == "provider_opaque"
    assert any(edge["relation"] == "responds_to" and edge["metadata_json"].find("inheritance_unproven") >= 0 for edge in second["edges"])


@pytest.mark.oracle
def test_oracle_repeated_exact_import_returns_the_original_objects(loom_env) -> None:
    refs = b'{"reference":"I1","value":"bound"}\n'
    map_sha = hashlib.sha256(refs).hexdigest()
    transcript = f"""### META thread=abc source=codex date=2026-08-18 rounds=1 fmt=3 refs=abc.refs.jsonl map={map_sha} source_sha256={'b' * 64} leaf=abc
### U
P1
### A
A1
"""
    importer = OctopusImporter(loom_env["store"], loom_env["loom"], "http://unused")

    first = importer.import_conversation(project_id=loom_env["project"]["project_id"], conversation_id="abc", provider="codex", transcript=transcript, refs=refs)
    second = importer.import_conversation(project_id=loom_env["project"]["project_id"], conversation_id="abc", provider="codex", transcript=transcript, refs=refs)

    assert second == first
    assert len(loom_env["store"].list_invocations(loom_env["project"]["project_id"])) == 1
    assert len(loom_env["store"].get_project(loom_env["project"]["project_id"])["sessions"]) == 2


@pytest.mark.contract
def test_negative_control_unknown_provider_is_not_silently_relabelled_as_claude(loom_env) -> None:
    refs = b'{}\n'
    map_sha = hashlib.sha256(refs).hexdigest()
    transcript = f"### META thread=x source=unknown date=2026-08-18 rounds=0 fmt=3 refs=x.refs.jsonl map={map_sha} source_sha256={'c' * 64} leaf=x\n"
    importer = OctopusImporter(loom_env["store"], loom_env["loom"], "http://unused")

    with pytest.raises(ValueError, match="unsupported historical provider"):
        importer.import_conversation(project_id=loom_env["project"]["project_id"], conversation_id="x", provider="mystery", transcript=transcript, refs=refs)
