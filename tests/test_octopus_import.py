"""Independent captured-projection contract for Octopus imports."""

from __future__ import annotations

import hashlib
import json

import pytest
import httpx

from lattice.octopus_import import OctopusImporter


def _catalogue(value: str) -> bytes:
    exact = value.encode()
    entry = {
        "ref": "I1",
        "kind": "instructions",
        "label": "session context",
        "value": value,
        "children": [],
        "bytes": len(exact),
        "sha256": hashlib.sha256(exact).hexdigest(),
        "provider_type": "fixture",
        "origins": [{"ref": "fixture:source", "pointer": "/instructions", "sha256": "f" * 64}],
        "pointer": "/groups/I1/items/1",
        "disposition": "referenced",
    }
    return (json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


@pytest.mark.contract
def test_contract_import_preserves_turns_and_names_hidden_context(lattice_env) -> None:
    refs = _catalogue("hidden")
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
    importer = OctopusImporter(lattice_env["store"], lattice_env["lattice"], "http://unused")

    result = importer.import_conversation(project_id=lattice_env["project"]["project_id"], conversation_id="abc", provider="codex", transcript=transcript, refs=refs)

    assert len(result["invocation_ids"]) == 2
    second = lattice_env["lattice"].inspect(result["invocation_ids"][1])
    assert second["state"] == "imported"
    assert second["output_block"]["content"] == "A2"
    assert second["output_block"]["source_ref"] == f"lattice:blob/{second['output_block']['content_sha256']}"
    assert second["output_block"]["source_sha256"] == second["output_block"]["content_sha256"]
    output_provenance = json.loads(second["output_block"]["transformation_json"])
    assert output_provenance["kind"] == "octopus-visible-response-import-v1"
    assert output_provenance["declared_archive_source_sha256"] == source_sha
    assert result["source_sha256_state"] == "declared-by-projection-not-recomputed"
    assert second["manifest"]["omitted"][0]["disposition"] == "provider_opaque"
    proof_limit = lattice_env["store"].get_block(second["manifest"]["omitted"][0]["block_id"])
    assert proof_limit["source_ref"] == f"lattice:blob/{proof_limit['content_sha256']}"
    assert json.loads(proof_limit["transformation_json"])["transcript_sha256"] == result["transcript_sha256"]
    assert any(edge["relation"] == "responds_to" and edge["metadata_json"].find("inheritance_unproven") >= 0 for edge in second["edges"])


@pytest.mark.oracle
def test_import_preserves_visible_projection_whitespace(lattice_env) -> None:
    refs = _catalogue("bound")
    map_sha = hashlib.sha256(refs).hexdigest()
    transcript = f"""### META thread=spacing source=codex date=2026-08-18 rounds=1 fmt=3 refs=spacing.refs.jsonl map={map_sha} source_sha256={'a' * 64} leaf=spacing
### U
  indented prompt
### A
  indented answer
"""
    importer = OctopusImporter(lattice_env["store"], lattice_env["lattice"], "http://unused")

    result = importer.import_conversation(project_id=lattice_env["project"]["project_id"], conversation_id="spacing", provider="codex", transcript=transcript, refs=refs)
    invocation = lattice_env["store"].get_invocation(result["invocation_ids"][0])
    prompt_segment = next(item for item in invocation["manifest"]["segments"] if item["plane"] == "current_prompt")

    assert lattice_env["store"].get_block(prompt_segment["block_id"])["content"] == "  indented prompt"
    assert invocation["output_block"]["content"] == "  indented answer"


@pytest.mark.oracle
def test_import_preserves_unanswered_visible_turn_as_editable_draft(lattice_env) -> None:
    refs = _catalogue("bound")
    map_sha = hashlib.sha256(refs).hexdigest()
    transcript = f"""### META thread=pending source=codex date=2026-08-18 rounds=2 fmt=3 refs=pending.refs.jsonl map={map_sha} source_sha256={'a' * 64} leaf=pending
### U
answered
### A
response
### U
still waiting
"""
    importer = OctopusImporter(lattice_env["store"], lattice_env["lattice"], "http://unused")

    result = importer.import_conversation(project_id=lattice_env["project"]["project_id"], conversation_id="pending", provider="codex", transcript=transcript, refs=refs)
    draft = lattice_env["store"].get_draft(result["pending_draft_ids"][0])
    imported = lattice_env["store"].get_invocation(result["invocation_ids"][0])
    record = lattice_env["store"].get_conversation_import(result["import_id"])

    assert draft["state"] == "editable"
    assert lattice_env["store"].get_block(draft["prompt_block_id"])["content"] == "still waiting"
    assert draft["parents"] == [imported["invocation_id"]]
    assert record["turns"][1]["response_state"] == "absent"


@pytest.mark.regression
def test_negative_control_import_rejects_declared_round_count_mismatch(lattice_env) -> None:
    refs = _catalogue("bound")
    map_sha = hashlib.sha256(refs).hexdigest()
    transcript = f"""### META thread=round-mismatch source=codex date=2026-08-18 rounds=2 fmt=3 refs=round-mismatch.refs.jsonl map={map_sha} source_sha256={'a' * 64} leaf=round-mismatch
### U
only one
### A
response
"""
    importer = OctopusImporter(lattice_env["store"], lattice_env["lattice"], "http://unused")

    with pytest.raises(ValueError, match="declares 2 user rounds but projects 1"):
        importer.import_conversation(project_id=lattice_env["project"]["project_id"], conversation_id="round-mismatch", provider="codex", transcript=transcript, refs=refs)


@pytest.mark.regression
def test_negative_control_import_rejects_ambiguous_metadata(lattice_env) -> None:
    refs = _catalogue("bound")
    map_sha = hashlib.sha256(refs).hexdigest()
    transcript = f"### META thread=ambiguous source=codex source=claude date=2026-08-18 rounds=0 fmt=3 refs=ambiguous.refs.jsonl map={map_sha} source_sha256={'a' * 64} leaf=ambiguous\n"
    importer = OctopusImporter(lattice_env["store"], lattice_env["lattice"], "http://unused")

    with pytest.raises(ValueError, match="repeats metadata field"):
        importer.import_conversation(project_id=lattice_env["project"]["project_id"], conversation_id="ambiguous", provider="codex", transcript=transcript, refs=refs)


@pytest.mark.oracle
def test_oracle_repeated_exact_import_returns_the_original_objects(lattice_env) -> None:
    refs = _catalogue("bound")
    map_sha = hashlib.sha256(refs).hexdigest()
    transcript = f"""### META thread=abc source=codex date=2026-08-18 rounds=1 fmt=3 refs=abc.refs.jsonl map={map_sha} source_sha256={'b' * 64} leaf=abc
### U
P1
### A
A1
"""
    importer = OctopusImporter(lattice_env["store"], lattice_env["lattice"], "http://unused")

    first = importer.import_conversation(project_id=lattice_env["project"]["project_id"], conversation_id="abc", provider="codex", transcript=transcript, refs=refs)
    second = importer.import_conversation(project_id=lattice_env["project"]["project_id"], conversation_id="abc", provider="codex", transcript=transcript, refs=refs)

    assert second == first
    assert len(lattice_env["store"].list_invocations(lattice_env["project"]["project_id"])) == 1
    assert len(lattice_env["store"].get_project(lattice_env["project"]["project_id"])["sessions"]) == 2


@pytest.mark.contract
def test_negative_control_unknown_provider_is_not_silently_relabelled_as_claude(lattice_env) -> None:
    refs = _catalogue("unknown")
    map_sha = hashlib.sha256(refs).hexdigest()
    transcript = f"### META thread=x source=unknown date=2026-08-18 rounds=0 fmt=3 refs=x.refs.jsonl map={map_sha} source_sha256={'c' * 64} leaf=x\n"
    importer = OctopusImporter(lattice_env["store"], lattice_env["lattice"], "http://unused")

    with pytest.raises(ValueError, match="unsupported historical provider"):
        importer.import_conversation(project_id=lattice_env["project"]["project_id"], conversation_id="x", provider="mystery", transcript=transcript, refs=refs)


@pytest.mark.oracle
def test_negative_control_reference_entry_with_false_hash_is_rejected(lattice_env) -> None:
    entry = json.loads(_catalogue("hidden"))
    entry["sha256"] = "0" * 64
    refs = (json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n").encode()
    map_sha = hashlib.sha256(refs).hexdigest()
    transcript = f"### META thread=bad source=codex date=2026-08-18 rounds=0 fmt=3 refs=bad.refs.jsonl map={map_sha} source_sha256={'d' * 64} leaf=bad\n"
    importer = OctopusImporter(lattice_env["store"], lattice_env["lattice"], "http://unused")

    with pytest.raises(ValueError, match="fails its byte binding"):
        importer.import_conversation(project_id=lattice_env["project"]["project_id"], conversation_id="bad", provider="codex", transcript=transcript, refs=refs)


@pytest.mark.regression
def test_negative_control_reference_catalogue_rejects_duplicate_json_keys(lattice_env) -> None:
    refs = _catalogue("hidden").replace(b'"ref":"I1"', b'"ref":"I1","ref":"I2"')
    map_sha = hashlib.sha256(refs).hexdigest()
    transcript = f"### META thread=duplicate source=codex date=2026-08-18 rounds=0 fmt=3 refs=duplicate.refs.jsonl map={map_sha} source_sha256={'d' * 64} leaf=duplicate\n"
    importer = OctopusImporter(lattice_env["store"], lattice_env["lattice"], "http://unused")

    with pytest.raises(ValueError, match="duplicate JSON object key"):
        importer.import_conversation(project_id=lattice_env["project"]["project_id"], conversation_id="duplicate", provider="codex", transcript=transcript, refs=refs)


@pytest.mark.contract
def test_negative_control_reference_catalogue_rejects_fabricated_origin_lineage(lattice_env) -> None:
    entry = json.loads(_catalogue("hidden"))
    entry["origins"][0]["sha256"] = "not-a-source-hash"
    refs = (json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n").encode()
    map_sha = hashlib.sha256(refs).hexdigest()
    transcript = f"### META thread=origin source=codex date=2026-08-18 rounds=0 fmt=3 refs=origin.refs.jsonl map={map_sha} source_sha256={'d' * 64} leaf=origin\n"
    importer = OctopusImporter(lattice_env["store"], lattice_env["lattice"], "http://unused")

    with pytest.raises(ValueError, match="invalid origins"):
        importer.import_conversation(project_id=lattice_env["project"]["project_id"], conversation_id="origin", provider="codex", transcript=transcript, refs=refs)


@pytest.mark.concurrency
def test_interrupted_import_resumes_completed_turns_without_duplicates(monkeypatch, lattice_env) -> None:
    refs = _catalogue("bound")
    map_sha = hashlib.sha256(refs).hexdigest()
    transcript = f"""### META thread=resume source=codex date=2026-08-18 rounds=2 fmt=3 refs=resume.refs.jsonl map={map_sha} source_sha256={'e' * 64} leaf=resume
### U
P1
### A
A1
### U
P2
### A
A2
"""
    importer = OctopusImporter(lattice_env["store"], lattice_env["lattice"], "http://unused")
    real_attach = lattice_env["store"].attach_imported_turn_output
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("planted import interruption")
        return real_attach(*args, **kwargs)

    monkeypatch.setattr(lattice_env["store"], "attach_imported_turn_output", fail_second)
    with pytest.raises(OSError, match="planted import interruption"):
        importer.import_conversation(project_id=lattice_env["project"]["project_id"], conversation_id="resume", provider="codex", transcript=transcript, refs=refs)
    monkeypatch.setattr(lattice_env["store"], "attach_imported_turn_output", real_attach)

    result = importer.import_conversation(project_id=lattice_env["project"]["project_id"], conversation_id="resume", provider="openai", transcript=transcript, refs=refs)

    assert len(result["invocation_ids"]) == 2
    assert len(lattice_env["store"].list_invocations(lattice_env["project"]["project_id"])) == 2
    assert lattice_env["store"].blobs.get(result["refs_ref"].removeprefix("lattice:blob/")) == refs


@pytest.mark.regression
def test_import_rejects_path_shaped_conversation_identity(lattice_env) -> None:
    importer = OctopusImporter(lattice_env["store"], lattice_env["lattice"], "http://127.0.0.1:8000")
    with pytest.raises(ValueError, match="safe opaque"):
        importer.import_conversation(project_id=lattice_env["project"]["project_id"], conversation_id="../operator-token", provider="codex", transcript="", refs=b"")


@pytest.mark.contract
def test_octopus_fetch_is_stream_bounded_and_does_not_follow_redirects(lattice_env) -> None:
    mode = {"value": "oversize"}

    def handler(request: httpx.Request) -> httpx.Response:
        if mode["value"] == "redirect":
            return httpx.Response(302, headers={"location": "http://127.0.0.1:9000/operator-token"}, request=request)
        return httpx.Response(200, content=b"12345", request=request)

    importer = OctopusImporter(lattice_env["store"], lattice_env["lattice"], "http://127.0.0.1:8000", transport=httpx.MockTransport(handler))
    importer.MAX_PROJECTION_BYTES = 4
    with pytest.raises(ValueError, match="4-byte limit"):
        importer.fetch("bounded")
    mode["value"] = "redirect"
    with pytest.raises(ValueError, match="HTTP 302"):
        importer.fetch("bounded")
