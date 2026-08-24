"""Contract and property tests for conversational lattice operations."""

from __future__ import annotations

import json

import pytest

from lattice.canonical import canonical_bytes, sha256_bytes
from lattice.contracts import BlockDisposition, ContextBlockCreate, ContextSelection, DirectActorBinding, DraftCreate, DraftPatch, EdgeRelation, ProjectCreate, RoleCreate, RolePinCreate, RoleSessionCreate, SliceCreate, TagRequest

from conftest import completed_invocation


@pytest.mark.contract
def test_contract_fork_join_diff_replay_and_tag(lattice_env) -> None:
    left = completed_invocation(lattice_env, "P1", "left")
    right = completed_invocation(lattice_env, "P2", "right")
    fork = lattice_env["lattice"].fork(left["invocation_id"], role_session_id=lattice_env["session"]["role_session_id"], prompt="challenge", route=lattice_env["route"])
    joined = lattice_env["lattice"].join([left["invocation_id"], right["invocation_id"]], role_session_id=lattice_env["session"]["role_session_id"], prompt="join", route=lattice_env["route"])
    sealed_fork = lattice_env["lattice"].seal(fork["draft_id"])
    sealed_join = lattice_env["lattice"].seal(joined["draft_id"])
    difference = lattice_env["lattice"].diff(sealed_fork["invocation_id"], sealed_join["invocation_id"])
    replay = lattice_env["lattice"].replay(sealed_fork["invocation_id"], prompt="fresh replay")
    tagged = lattice_env["lattice"].tag(left["invocation_id"], TagRequest(concern_ref="slate:canon-quaternary-german", label="Canon > Quaternary > German"))

    assert set(joined["parents"]) == {left["invocation_id"], right["invocation_id"]}
    assert difference["only_right"]
    assert replay["invocation_id"] is None
    assert tagged["slice"]["items"][0]["invocation_id"] == left["invocation_id"]

    repeated = lattice_env["lattice"].tag(left["invocation_id"], TagRequest(concern_ref="slate:canon-quaternary-german", label="Canon > Quaternary > German"))
    assert repeated["edge_id"] == tagged["edge_id"]
    with pytest.raises(Exception, match="label disagrees"):
        lattice_env["lattice"].tag(left["invocation_id"], TagRequest(concern_ref="slate:canon-quaternary-german", label="Different topic"))


@pytest.mark.property
def test_property_replay_never_mutates_original(lattice_env) -> None:
    original = completed_invocation(lattice_env, "original prompt", "original result")
    before = lattice_env["lattice"].inspect(original["invocation_id"])
    replay = lattice_env["lattice"].replay(original["invocation_id"], prompt="changed prompt")
    lattice_env["lattice"].seal(replay["draft_id"])
    after = lattice_env["lattice"].inspect(original["invocation_id"])

    assert before["manifest_sha256"] == after["manifest_sha256"]
    assert before["output_block"]["content"] == "original result"


@pytest.mark.regression
def test_replay_with_new_prompt_does_not_precreate_an_unattached_context_block(monkeypatch, lattice_env) -> None:
    original = completed_invocation(lattice_env, "original prompt", "original result")

    def forbidden_precreation(*_args, **_kwargs):
        raise AssertionError("replay must create its prompt block inside draft creation")

    monkeypatch.setattr(lattice_env["store"], "create_context_block", forbidden_precreation)
    replay = lattice_env["lattice"].replay(original["invocation_id"], prompt="replacement prompt")

    assert lattice_env["store"].get_block(replay["prompt_block_id"])["content"] == "replacement prompt"
    assert next(item for item in replay["selections"] if item["plane"] == "current_prompt")["block_id"] == replay["prompt_block_id"]


@pytest.mark.oracle
def test_editing_an_explicit_replay_retargets_the_exact_prompt_context(lattice_env) -> None:
    original = completed_invocation(lattice_env, "old visible prompt", "original result")
    replay = lattice_env["lattice"].replay(original["invocation_id"])

    revised = lattice_env["store"].patch_draft(replay["draft_id"], DraftPatch(expected_revision=1, prompt="new visible prompt"))
    sealed = lattice_env["lattice"].seal(revised["draft_id"])
    prompt_segment = next(item for item in sealed["manifest"]["segments"] if item["plane"] == "current_prompt")
    inspected = lattice_env["lattice"].inspect(sealed["invocation_id"])

    assert prompt_segment["block_id"] == revised["prompt_block_id"]
    assert lattice_env["store"].get_block(prompt_segment["block_id"])["content"] == "new visible prompt"
    assert "new visible prompt" in inspected["compiled_prompt"]
    assert "old visible prompt" not in inspected["compiled_prompt"]


@pytest.mark.contract
def test_draft_parent_replacement_rejects_duplicates_and_cross_project_ancestry(lattice_env) -> None:
    original = completed_invocation(lattice_env, "parent", "result")
    draft = lattice_env["lattice"].fork(original["invocation_id"], role_session_id=lattice_env["session"]["role_session_id"], prompt="child", route=lattice_env["route"])
    with pytest.raises(ValueError, match="must be unique"):
        DraftPatch(expected_revision=1, parent_invocation_ids=[original["invocation_id"], original["invocation_id"]])

    other_project = lattice_env["store"].create_project(ProjectCreate(title="Other ancestry", purpose="negative control"))
    other_role = lattice_env["store"].create_role(other_project["project_id"], RoleCreate(name="other", instructions="other"))
    other_session = lattice_env["store"].create_role_session(RoleSessionCreate(project_id=other_project["project_id"], role_id=other_role["role_id"], label="other"))
    other_draft = lattice_env["store"].create_draft(DraftCreate(project_id=other_project["project_id"], role_session_id=other_session["role_session_id"], prompt="other", route=lattice_env["route"]))
    other_invocation = lattice_env["lattice"].seal(other_draft["draft_id"])
    with pytest.raises(Exception, match="does not belong to project"):
        lattice_env["store"].patch_draft(draft["draft_id"], DraftPatch(expected_revision=1, parent_invocation_ids=[other_invocation["invocation_id"]]))


@pytest.mark.concurrency
def test_tag_edge_and_global_slice_roll_back_together(monkeypatch, lattice_env) -> None:
    invocation = completed_invocation(lattice_env, "tag atomically", "result")
    concern = "slate:atomic-tag-negative-control"

    def fail_edge(*_args, **_kwargs):
        raise OSError("planted edge failure")

    monkeypatch.setattr(lattice_env["store"], "_insert_edge", fail_edge)
    with pytest.raises(OSError, match="planted edge failure"):
        lattice_env["lattice"].tag(invocation["invocation_id"], TagRequest(concern_ref=concern, label="Atomic"))

    assert lattice_env["store"].get_slice_by_external_ref(concern) is None
    with lattice_env["store"]._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM edges WHERE target_id=?", (concern,)).fetchone()[0] == 0


@pytest.mark.oracle
def test_oracle_replay_preserves_exact_context_and_uses_a_provenance_edge(lattice_env) -> None:
    store = lattice_env["store"]
    omitted = store.create_context_block(ContextBlockCreate(project_id=lattice_env["project"]["project_id"], semantic_type="known-unknown", content="not supplied"))
    draft = store.create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], role_session_id=lattice_env["session"]["role_session_id"], prompt="repeat exactly", route=lattice_env["route"], selections=[ContextSelection(block_id=omitted["block_id"], disposition=BlockDisposition.omitted, plane="knowledge", label="deliberately absent", reason="negative control")]))
    original = lattice_env["lattice"].seal(draft["draft_id"])

    replay_draft = lattice_env["lattice"].replay(original["invocation_id"])
    replayed = lattice_env["lattice"].seal(replay_draft["draft_id"])

    assert replayed["compiled_prompt_sha256"] == original["compiled_prompt_sha256"]
    assert replayed["manifest"]["segments"] == original["manifest"]["segments"]
    assert replayed["manifest"]["omitted"] == original["manifest"]["omitted"]
    assert any(edge["relation"] == "derived_from" and edge["target_id"] == original["invocation_id"] for edge in replayed["edges"])
    assert not any(edge["relation"] == "inherited_context_from" for edge in replayed["edges"])


@pytest.mark.oracle
def test_negative_control_replay_with_changed_prompt_changes_only_prompt_context(lattice_env) -> None:
    original = completed_invocation(lattice_env, "original prompt", "response")
    replay = lattice_env["lattice"].replay(original["invocation_id"], prompt="changed prompt")
    sealed = lattice_env["lattice"].seal(replay["draft_id"])
    difference = lattice_env["lattice"].diff(original["invocation_id"], sealed["invocation_id"])

    assert sealed["compiled_prompt_sha256"] != original["compiled_prompt_sha256"]
    assert {item["plane"] for item in difference["only_left"] + difference["only_right"]} == {"current_prompt"}


@pytest.mark.contract
def test_slate_slice_can_render_authoritative_blackboard_without_copying_it(lattice_env) -> None:
    store = lattice_env["store"]
    project_id = lattice_env["project"]["project_id"]
    block = store.create_context_block(ContextBlockCreate(project_id=project_id, semantic_type="workflow-blackboard-snapshot", content='{"state":"calibrating"}', authority="quaternary", evidence_level="authoritative-read"))
    concern = "slate:canon-quaternary-german"
    store.add_edge(project_id=project_id, relation=EdgeRelation.concerns, source_type="context-block", source_id=block["block_id"], source_revision=block["content_sha256"], target_type="external-concern", target_id=concern, target_revision=sha256_bytes(concern.encode()), metadata={})
    slice_value = store.create_slice(SliceCreate(project_id=project_id, scope="global", name="German", external_ref=concern, query={"concern_ref": concern, "object_types": ["context-block"]}))
    rendered = store.get_slice(slice_value["slice_id"])
    assert rendered["items"][0]["object_id"] == block["block_id"]
    assert rendered["items"][0]["response"] == '{"state":"calibrating"}'
    assert rendered["external_ref"] == concern


@pytest.mark.regression
def test_negative_control_external_slice_cannot_be_project_local(lattice_env) -> None:
    with pytest.raises(ValueError, match="must be global"):
        SliceCreate(project_id=lattice_env["project"]["project_id"], scope="project", name="Broken shared topic", external_ref="slate:shared-but-hidden", query={"concern_ref": "slate:shared-but-hidden"})


@pytest.mark.contract
def test_slice_admission_rejects_unexecutable_or_identity_divergent_queries(lattice_env) -> None:
    project_id = lattice_env["project"]["project_id"]
    with pytest.raises(ValueError):
        SliceCreate(project_id=project_id, name="Unknown grammar", query={"sql": "SELECT *"})
    with pytest.raises(ValueError, match="must equal"):
        SliceCreate(project_id=project_id, scope="global", name="Split identity", external_ref="slate:a", query={"concern_ref": "slate:b"})
    with pytest.raises(ValueError, match="exact concern"):
        SliceCreate(project_id=project_id, scope="global", name="Unbounded global", query={"object_types": ["invocation"]})
    with pytest.raises(ValueError, match="must be unique"):
        SliceCreate(project_id=project_id, name="Duplicate types", query={"object_types": ["invocation", "invocation"]})


@pytest.mark.oracle
def test_oracle_slate_concern_slice_spans_projects_without_copying_invocations(lattice_env) -> None:
    store = lattice_env["store"]
    concern = "slate:shared-topic"
    first = completed_invocation(lattice_env, "first project", "first result")
    tagged = lattice_env["lattice"].tag(first["invocation_id"], TagRequest(concern_ref=concern, label="Shared topic"))
    other_project = store.create_project(ProjectCreate(title="Other", purpose="cross-project slice oracle"))
    other_role = store.create_role(other_project["project_id"], RoleCreate(name="operator", instructions="Use supplied context."))
    other_session = store.create_role_session(RoleSessionCreate(project_id=other_project["project_id"], role_id=other_role["role_id"], label="other"))
    other_env = {**lattice_env, "project": other_project, "role": other_role, "session": other_session}
    second = completed_invocation(other_env, "second project", "second result")
    store.add_edge(project_id=other_project["project_id"], relation=EdgeRelation.concerns, source_type="invocation", source_id=second["invocation_id"], source_revision=second["manifest_sha256"], target_type="external-concern", target_id=concern, target_revision=sha256_bytes(concern.encode()), metadata={})

    tagged["slice"] = store.get_slice(tagged["slice"]["slice_id"])

    assert tagged["slice"]["scope"] == "global"
    assert {item["invocation_id"] for item in tagged["slice"]["items"]} == {first["invocation_id"], second["invocation_id"]}
    current_first = store.get_invocation(first["invocation_id"])
    assert {edge["target_revision"] for edge in current_first["edges"] if edge["target_id"] == concern} == {sha256_bytes(concern.encode())}


@pytest.mark.oracle
def test_oracle_slice_pagination_is_complete_stable_and_resumable(lattice_env) -> None:
    concern = "slate:bounded-topic"
    expected = set()
    slice_id = None
    for index in range(3):
        invocation = completed_invocation(lattice_env, f"prompt {index}", f"result {index}")
        expected.add(invocation["invocation_id"])
        tagged = lattice_env["lattice"].tag(invocation["invocation_id"], TagRequest(concern_ref=concern, label="Bounded topic"))
        slice_id = tagged["slice"]["slice_id"]

    first = lattice_env["store"].get_slice(slice_id, limit=2)
    continuation = first["coverage"]["enumeration"]["continuation"]
    late = completed_invocation(lattice_env, "late prompt", "late result")
    lattice_env["lattice"].tag(late["invocation_id"], TagRequest(concern_ref=concern, label="Bounded topic"))
    second = lattice_env["store"].get_slice(slice_id, limit=2, **continuation)
    fresh = lattice_env["store"].get_slice(slice_id, limit=10)

    assert first["coverage"]["state"] == "partial"
    assert first["coverage"]["enumeration"]["has_more"] is True
    assert second["coverage"]["state"] == "complete"
    assert second["coverage"]["enumeration"]["has_more"] is False
    assert {item["object_id"] for item in first["items"] + second["items"]} == expected
    assert not ({item["object_id"] for item in first["items"]} & {item["object_id"] for item in second["items"]})
    assert late["invocation_id"] not in {item["object_id"] for item in first["items"] + second["items"]}
    assert {item["object_id"] for item in fresh["items"]} == expected | {late["invocation_id"]}


@pytest.mark.regression
def test_negative_control_slice_never_labels_clipped_response_content_complete(lattice_env) -> None:
    store = lattice_env["store"]
    store.SLICE_INLINE_RESPONSE_BYTES = 4
    project_id = lattice_env["project"]["project_id"]
    concern = "slate:large-response"
    block = store.create_context_block(ContextBlockCreate(project_id=project_id, semantic_type="evidence", content="too large"))
    store.add_edge(project_id=project_id, relation=EdgeRelation.concerns, source_type="context-block", source_id=block["block_id"], source_revision=block["content_sha256"], target_type="external-concern", target_id=concern, target_revision=sha256_bytes(concern.encode()), metadata={})
    live_slice = store.create_slice(SliceCreate(project_id=project_id, scope="global", name="Large response", external_ref=concern, query={"concern_ref": concern, "object_types": ["context-block"]}))

    item = live_slice["items"][0]
    assert "response" not in item
    assert item["response_state"] == "omitted-byte-budget"
    assert item["response_sha256"] == block["content_sha256"]
    assert item["response_byte_count"] == len("too large")
    assert live_slice["coverage"]["state"] == "partial"
    assert live_slice["coverage"]["content"]["state"] == "partial"
    assert live_slice["coverage"]["content"]["omitted_responses"] == 1


@pytest.mark.regression
def test_negative_control_slice_rejects_partial_or_forged_continuations(lattice_env) -> None:
    store = lattice_env["store"]
    slice_value = store.create_slice(SliceCreate(project_id=lattice_env["project"]["project_id"], name="Local", query={"object_types": ["invocation"]}))
    with pytest.raises(Exception, match="requires created_at, object_type, and object_id"):
        store.get_slice(slice_value["slice_id"], after_created_at="20260821T000000")
    with pytest.raises(Exception, match="unsupported object type"):
        store.get_slice(slice_value["slice_id"], after_created_at="20260821T000000", after_object_type="artifact", after_object_id="artifact-fake")


@pytest.mark.regression
def test_negative_control_external_concern_edge_rejects_an_unbound_revision(lattice_env) -> None:
    invocation = completed_invocation(lattice_env, "edge", "result")
    with pytest.raises(Exception, match="bind its exact stable identity"):
        lattice_env["store"].add_edge(project_id=lattice_env["project"]["project_id"], relation=EdgeRelation.concerns, source_type="invocation", source_id=invocation["invocation_id"], source_revision=invocation["manifest_sha256"], target_type="external-concern", target_id="slate:truth", target_revision="slate:truth", metadata={})


@pytest.mark.contract
def test_contract_diff_exposes_omission_semantics_for_the_same_block(lattice_env) -> None:
    store = lattice_env["store"]
    block = store.create_context_block(ContextBlockCreate(project_id=lattice_env["project"]["project_id"], semantic_type="evidence", content="same bytes"))
    selected = store.create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], role_session_id=lattice_env["session"]["role_session_id"], prompt="left", route=lattice_env["route"], selections=[ContextSelection(block_id=block["block_id"], plane="knowledge", label="evidence")]))
    omitted = store.create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], role_session_id=lattice_env["session"]["role_session_id"], prompt="right", route=lattice_env["route"], selections=[ContextSelection(block_id=block["block_id"], disposition=BlockDisposition.omitted, plane="knowledge", label="evidence", reason="withheld")]))
    left = lattice_env["lattice"].seal(selected["draft_id"])
    right = lattice_env["lattice"].seal(omitted["draft_id"])

    difference = lattice_env["lattice"].diff(left["invocation_id"], right["invocation_id"])

    assert difference["changed_semantics"][0]["block_id"] == block["block_id"]
    assert difference["changed_semantics"][0]["right"][0]["disposition"] == "omitted"


@pytest.mark.regression
def test_diff_orders_null_and_text_reasons_without_crashing(lattice_env) -> None:
    store = lattice_env["store"]
    block = store.create_context_block(ContextBlockCreate(project_id=lattice_env["project"]["project_id"], semantic_type="evidence", content="same bytes"))
    left_draft = store.create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], role_session_id=lattice_env["session"]["role_session_id"], prompt="left", route=lattice_env["route"], selections=[ContextSelection(block_id=block["block_id"], disposition=BlockDisposition.omitted, plane="knowledge", label="same")]))
    right_draft = store.create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], role_session_id=lattice_env["session"]["role_session_id"], prompt="right", route=lattice_env["route"], selections=[ContextSelection(block_id=block["block_id"], disposition=BlockDisposition.omitted, plane="knowledge", label="same", reason="withheld")]))
    left = lattice_env["lattice"].seal(left_draft["draft_id"])
    right = lattice_env["lattice"].seal(right_draft["draft_id"])

    difference = lattice_env["lattice"].diff(left["invocation_id"], right["invocation_id"])

    assert difference["changed_semantics"][0]["block_id"] == block["block_id"]


@pytest.mark.oracle
def test_diff_preserves_artifact_name_and_media_semantics(lattice_env) -> None:
    def complete_with_artifact(prompt: str, name: str, media_type: str):
        draft = lattice_env["store"].create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], role_session_id=lattice_env["session"]["role_session_id"], prompt=prompt, route=lattice_env["route"]))
        invocation = lattice_env["lattice"].seal(draft["draft_id"])
        attempt = lattice_env["store"].create_attempt(invocation["invocation_id"], f"artifact-{name}", "test-boot", 9_999_999_999)
        return lattice_env["store"].finish_successful_attempt(attempt["attempt_id"], response="done", artifacts=[{"name": name, "media_type": media_type, "content": "same bytes"}], journal_path="fixture:journal", runtime_receipt={"fixture": True})

    left = complete_with_artifact("left artifact", "report.csv", "text/csv")
    right = complete_with_artifact("right artifact", "evidence.txt", "text/plain")
    difference = lattice_env["lattice"].diff(left["invocation_id"], right["invocation_id"])

    assert difference["output_delta"]["left_artifacts"][0]["name"] == "report.csv"
    assert difference["output_delta"]["right_artifacts"][0]["media_type"] == "text/plain"
    assert difference["output_delta"]["left_artifacts"] != difference["output_delta"]["right_artifacts"]


@pytest.mark.oracle
def test_oracle_extracted_workflow_artifact_resolves_to_its_exact_bytes(lattice_env) -> None:
    invocation = completed_invocation(lattice_env, "extract me", "result")
    artifact = lattice_env["lattice"].extract(lattice_env["project"]["project_id"], [invocation["invocation_id"]], "Reusable path")

    stored = lattice_env["store"].get_artifact(artifact["generated_object"]["artifact_id"])

    assert stored["generated"] == artifact
    assert stored["content"]


@pytest.mark.oracle
def test_direct_conversation_extracts_and_materializes_without_inventing_a_role(lattice_env) -> None:
    store = lattice_env["store"]
    source_draft = store.create_draft(DraftCreate(
        project_id=lattice_env["project"]["project_id"],
        actor_binding=DirectActorBinding(),
        prompt="direct reusable source",
        route=lattice_env["route"],
    ))
    source = lattice_env["lattice"].seal(source_draft["draft_id"])
    attempt = store.create_attempt(source["invocation_id"], "direct-template-source", "test-boot", 9_999_999_999)
    source = store.finish_successful_attempt(attempt["attempt_id"], response="direct result", artifacts=[], journal_path="fixture:direct-template", runtime_receipt={"fixture": True})

    template = lattice_env["lattice"].extract(lattice_env["project"]["project_id"], [source["invocation_id"]], "Direct path")
    contracts = {item["parameter_id"]: item for item in template["parameters"] if item["kind"] == "context-block"}
    bindings = {
        parameter_id: store.create_context_block(ContextBlockCreate(
            project_id=lattice_env["project"]["project_id"],
            semantic_type=contract["semantic_type"],
            content=f"replacement {parameter_id}",
            status=contract["status"],
            authority=contract["authority"],
            evidence_level=contract["evidence_level"],
            sensitivity=contract["sensitivity"],
        ))["block_id"]
        for parameter_id, contract in contracts.items()
    }
    artifact_id = template["generated_object"]["artifact_id"]
    template_sha = store.get_artifact(artifact_id)["content_sha256"]
    target = lattice_env["lattice"].materialize_template_step(
        artifact_id,
        "step-001",
        expected_template_sha256=template_sha,
        project_id=lattice_env["project"]["project_id"],
        role_session_id=None,
        context_bindings=bindings,
        invocation_bindings={},
    )

    assert template["contract_version"] == "LatticeWorkflowTemplate/3.0"
    assert template["roles"] == []
    assert template["steps"][0]["actor_binding"] == {"kind": "direct"}
    assert target["actor_binding"] == {"kind": "direct"}
    with pytest.raises(Exception, match="cannot acquire a role session"):
        lattice_env["lattice"].materialize_template_step(
            artifact_id,
            "step-001",
            expected_template_sha256=template_sha,
            project_id=lattice_env["project"]["project_id"],
            role_session_id=lattice_env["session"]["role_session_id"],
            context_bindings=bindings,
            invocation_bindings={},
        )


@pytest.mark.oracle
def test_pin_as_role_is_future_facing_and_preserves_direct_history(lattice_env) -> None:
    store = lattice_env["store"]
    source_draft = store.create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], actor_binding=DirectActorBinding(), prompt="demonstrate the function", route=lattice_env["route"]))
    source = lattice_env["lattice"].seal(source_draft["draft_id"])
    attempt = store.create_attempt(source["invocation_id"], "pin-role-source", "test-boot", 9_999_999_999)
    source = store.finish_successful_attempt(attempt["attempt_id"], response="demonstrated output", artifacts=[], journal_path="fixture:pin-role", runtime_receipt={"fixture": True})
    original_manifest = canonical_bytes(source["manifest"])

    pinned = store.pin_invocation_as_role(source["invocation_id"], RolePinCreate(name="Critic", instructions="Find falsifiers.", session_label="Critic spine"))
    unchanged = store.get_invocation(source["invocation_id"])
    next_draft = store.create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], role_session_id=pinned["session"]["role_session_id"], prompt="apply the demonstrated function", route=lattice_env["route"]))

    assert unchanged["actor_binding"] == {"kind": "direct"}
    assert canonical_bytes(unchanged["manifest"]) == original_manifest
    assert next_draft["actor_binding"] == {"kind": "role-session", "role_session_id": pinned["session"]["role_session_id"]}
    with store._connect() as conn:
        lineage = conn.execute("SELECT relation,metadata_json FROM edges WHERE source_id=? AND target_id=?", (pinned["role"]["instructions_block_id"], source["invocation_id"])).fetchone()
    assert lineage is not None and lineage["relation"] == "derived_from" and "role-demonstration" in lineage["metadata_json"]


@pytest.mark.regression
def test_direct_template_reader_rejects_a_planted_role_actor(lattice_env) -> None:
    store = lattice_env["store"]
    source_draft = store.create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], actor_binding=DirectActorBinding(), prompt="direct source", route=lattice_env["route"]))
    source = lattice_env["lattice"].seal(source_draft["draft_id"])
    output = store.create_context_block(ContextBlockCreate(project_id=lattice_env["project"]["project_id"], semantic_type="model-response", content="result", producing_invocation_id=source["invocation_id"]))
    source = store.attach_imported_output(source["invocation_id"], output["block_id"])
    template = lattice_env["lattice"].extract(lattice_env["project"]["project_id"], [source["invocation_id"]], "Direct source")
    artifact_id = template["generated_object"]["artifact_id"]
    mutated = json.loads(json.dumps(template))
    mutated["steps"][0]["actor_binding"] = {"kind": "role-parameter", "parameter_id": "role-001"}
    raw = canonical_bytes(mutated)
    digest = store.blobs.put(raw)
    with store.transaction() as conn:
        conn.execute("UPDATE artifacts SET generated_json=?,content_sha256=? WHERE artifact_id=?", (raw.decode(), digest, artifact_id))

    with pytest.raises(Exception, match="changed the direct source actor"):
        lattice_env["lattice"].materialize_template_step(artifact_id, "step-001", expected_template_sha256=digest, project_id=lattice_env["project"]["project_id"], role_session_id=None, context_bindings={}, invocation_bindings={})


@pytest.mark.contract
def test_v2_role_template_remains_readable_after_v3_extraction_upgrade(lattice_env) -> None:
    source = completed_invocation(lattice_env, "legacy role path", "result")
    template = lattice_env["lattice"].extract(lattice_env["project"]["project_id"], [source["invocation_id"]], "Legacy role path")
    artifact_id = template["generated_object"]["artifact_id"]
    legacy = json.loads(json.dumps(template))
    legacy["contract_version"] = "LatticeWorkflowTemplate/2.0"
    legacy["generated_object"]["schema"]["version"] = 2
    for step in legacy["steps"]:
        step["role_parameter_id"] = step.pop("actor_binding")["parameter_id"]
    raw = canonical_bytes(legacy)
    digest = lattice_env["store"].blobs.put(raw)
    with lattice_env["store"].transaction() as conn:
        conn.execute("UPDATE artifacts SET generated_json=?,content_sha256=? WHERE artifact_id=?", (raw.decode(), digest, artifact_id))

    parameters = {item["parameter_id"]: item for item in legacy["parameters"] if item["kind"] == "context-block"}
    bindings = {
        parameter_id: lattice_env["store"].create_context_block(ContextBlockCreate(project_id=lattice_env["project"]["project_id"], semantic_type=contract["semantic_type"], content=f"legacy {parameter_id}", status=contract["status"], authority=contract["authority"], evidence_level=contract["evidence_level"], sensitivity=contract["sensitivity"]))["block_id"]
        for parameter_id, contract in parameters.items()
    }
    draft = lattice_env["lattice"].materialize_template_step(artifact_id, "step-001", expected_template_sha256=digest, project_id=lattice_env["project"]["project_id"], role_session_id=lattice_env["session"]["role_session_id"], context_bindings=bindings, invocation_bindings={})

    assert draft["actor_kind"] == "role-session"


@pytest.mark.oracle
def test_extracted_workflow_materializes_parameterized_edges_without_copying_project_evidence(lattice_env) -> None:
    first = completed_invocation(lattice_env, "SOURCE PROMPT MUST NOT COPY", "SOURCE OUTPUT MUST NOT COPY")
    second_draft = lattice_env["lattice"].fork(first["invocation_id"], role_session_id=lattice_env["session"]["role_session_id"], prompt="SECOND SOURCE PROMPT MUST NOT COPY", route=lattice_env["route"])
    second_sealed = lattice_env["lattice"].seal(second_draft["draft_id"])
    second_attempt = lattice_env["store"].create_attempt(second_sealed["invocation_id"], "template-source-second", "test-boot", 9_999_999_999)
    second = lattice_env["store"].finish_successful_attempt(second_attempt["attempt_id"], response="SECOND SOURCE OUTPUT MUST NOT COPY", artifacts=[], journal_path="fixture:template", runtime_receipt={"fixture": True})
    template = lattice_env["lattice"].extract(lattice_env["project"]["project_id"], [second["invocation_id"], first["invocation_id"]], "Executable path")
    template_bytes = canonical_bytes(template)

    assert template["contract_version"] == "LatticeWorkflowTemplate/3.0"
    assert [step["step_id"] for step in template["steps"]] == ["step-001", "step-002"]
    assert b"SOURCE PROMPT MUST NOT COPY" not in template_bytes
    assert b"SOURCE OUTPUT MUST NOT COPY" not in template_bytes

    parameter_contracts = {item["parameter_id"]: item for item in template["parameters"] if item["kind"] == "context-block"}
    target_blocks = {}
    for parameter_id, contract in parameter_contracts.items():
        target_blocks[parameter_id] = lattice_env["store"].create_context_block(ContextBlockCreate(
            project_id=lattice_env["project"]["project_id"],
            semantic_type=contract["semantic_type"],
            content=f"replacement bytes for {parameter_id}",
            status=contract["status"],
            authority=contract["authority"],
            evidence_level=contract["evidence_level"],
            sensitivity=contract["sensitivity"],
        ))

    def step_context(step: dict) -> dict[str, str]:
        identifiers = {
            binding["parameter_id"]
            for item in step["context"]
            for binding in [item["binding"], *item["derived_bindings"]]
            if binding["kind"] == "context-parameter"
        }
        return {identifier: target_blocks[identifier]["block_id"] for identifier in identifiers}

    artifact_id = template["generated_object"]["artifact_id"]
    template_sha = lattice_env["store"].get_artifact(artifact_id)["content_sha256"]
    first_draft = lattice_env["lattice"].materialize_template_step(artifact_id, "step-001", expected_template_sha256=template_sha, project_id=lattice_env["project"]["project_id"], role_session_id=lattice_env["session"]["role_session_id"], context_bindings=step_context(template["steps"][0]), invocation_bindings={})
    first_target = lattice_env["lattice"].seal(first_draft["draft_id"])
    first_target_attempt = lattice_env["store"].create_attempt(first_target["invocation_id"], "template-target-first", "test-boot", 9_999_999_999)
    first_target = lattice_env["store"].finish_successful_attempt(first_target_attempt["attempt_id"], response="new first output", artifacts=[], journal_path="fixture:template", runtime_receipt={"fixture": True})
    second_target_draft = lattice_env["lattice"].materialize_template_step(artifact_id, "step-002", expected_template_sha256=template_sha, project_id=lattice_env["project"]["project_id"], role_session_id=lattice_env["session"]["role_session_id"], context_bindings=step_context(template["steps"][1]), invocation_bindings={"step-001": first_target["invocation_id"]})

    assert second_target_draft["parents"] == [first_target["invocation_id"]]
    assert lattice_env["store"].get_block(second_target_draft["prompt_block_id"])["content"].startswith("replacement bytes")
    with lattice_env["store"]._connect() as conn:
        lineage = conn.execute("SELECT metadata_json FROM edges WHERE source_type='draft' AND source_id=? AND target_id=?", (second_target_draft["draft_id"], artifact_id)).fetchone()
    assert lineage is not None and "step-002" in lineage["metadata_json"]


@pytest.mark.regression
def test_workflow_template_rejects_semantically_incompatible_context_binding(lattice_env) -> None:
    source = completed_invocation(lattice_env, "source prompt", "source output")
    template = lattice_env["lattice"].extract(lattice_env["project"]["project_id"], [source["invocation_id"]], "Bound template")
    artifact_id = template["generated_object"]["artifact_id"]
    template_sha = lattice_env["store"].get_artifact(artifact_id)["content_sha256"]
    parameter_id = next(item["parameter_id"] for item in template["parameters"] if item["kind"] == "context-block")
    wrong = lattice_env["store"].create_context_block(ContextBlockCreate(project_id=lattice_env["project"]["project_id"], semantic_type="fabricated-evidence", content="wrong"))

    with pytest.raises(Exception, match="violates parameter contract"):
        lattice_env["lattice"].materialize_template_step(artifact_id, "step-001", expected_template_sha256=template_sha, project_id=lattice_env["project"]["project_id"], role_session_id=lattice_env["session"]["role_session_id"], context_bindings={parameter_id: wrong["block_id"]}, invocation_bindings={})


@pytest.mark.oracle
def test_independent_template_reader_rejects_source_semantic_mutation(lattice_env) -> None:
    source = completed_invocation(lattice_env, "source prompt", "source output")
    template = lattice_env["lattice"].extract(lattice_env["project"]["project_id"], [source["invocation_id"]], "Bound template")
    artifact_id = template["generated_object"]["artifact_id"]
    mutated = json.loads(json.dumps(template))
    mutated["steps"][0]["relation"] = "challenges"
    raw = canonical_bytes(mutated)
    digest = lattice_env["store"].blobs.put(raw)
    with lattice_env["store"].transaction() as conn:
        conn.execute("UPDATE artifacts SET generated_json=?,content_sha256=? WHERE artifact_id=?", (raw.decode(), digest, artifact_id))

    with pytest.raises(Exception, match="changed source execution semantics"):
        lattice_env["lattice"].materialize_template_step(artifact_id, "step-001", expected_template_sha256=digest, project_id=lattice_env["project"]["project_id"], role_session_id=lattice_env["session"]["role_session_id"], context_bindings={}, invocation_bindings={})


@pytest.mark.oracle
def test_workflow_extraction_orders_consumed_outputs_even_without_parent_edges(lattice_env) -> None:
    first = completed_invocation(lattice_env, "first", "first output")
    draft = lattice_env["store"].create_draft(DraftCreate(
        project_id=lattice_env["project"]["project_id"],
        role_session_id=lattice_env["session"]["role_session_id"],
        prompt="consume evidence without ancestry",
        route=lattice_env["route"],
        selections=[ContextSelection(block_id=first["output_block_id"], plane="knowledge", label="selected earlier output")],
    ))
    sealed = lattice_env["lattice"].seal(draft["draft_id"])
    output = lattice_env["store"].create_context_block(ContextBlockCreate(project_id=lattice_env["project"]["project_id"], semantic_type="model-response", content="second output", producing_invocation_id=sealed["invocation_id"]))
    second = lattice_env["store"].attach_imported_output(sealed["invocation_id"], output["block_id"])

    template = lattice_env["lattice"].extract(lattice_env["project"]["project_id"], [second["invocation_id"], first["invocation_id"]], "Data dependency")

    assert [step["source_invocation_id"] for step in template["steps"]] == [first["invocation_id"], second["invocation_id"]]
    assert any(item["binding"] == {"kind": "step-output", "step_id": "step-001"} for item in template["steps"][1]["context"])
    contracts = {item["parameter_id"]: item for item in template["parameters"] if item["kind"] == "context-block"}
    replacement_blocks = {
        parameter_id: lattice_env["store"].create_context_block(ContextBlockCreate(project_id=lattice_env["project"]["project_id"], semantic_type=contract["semantic_type"], content=f"target {parameter_id}", status=contract["status"], authority=contract["authority"], evidence_level=contract["evidence_level"], sensitivity=contract["sensitivity"]))["block_id"]
        for parameter_id, contract in contracts.items()
    }

    def bindings_for(step: dict) -> dict[str, str]:
        return {
            binding["parameter_id"]: replacement_blocks[binding["parameter_id"]]
            for item in step["context"]
            for binding in [item["binding"], *item["derived_bindings"]]
            if binding["kind"] == "context-parameter"
        }

    artifact_id = template["generated_object"]["artifact_id"]
    template_sha = lattice_env["store"].get_artifact(artifact_id)["content_sha256"]
    target_first_draft = lattice_env["lattice"].materialize_template_step(artifact_id, "step-001", expected_template_sha256=template_sha, project_id=lattice_env["project"]["project_id"], role_session_id=lattice_env["session"]["role_session_id"], context_bindings=bindings_for(template["steps"][0]), invocation_bindings={})
    target_first = lattice_env["lattice"].seal(target_first_draft["draft_id"])
    target_output = lattice_env["store"].create_context_block(ContextBlockCreate(project_id=lattice_env["project"]["project_id"], semantic_type="model-response", content="target first output", producing_invocation_id=target_first["invocation_id"]))
    target_first = lattice_env["store"].attach_imported_output(target_first["invocation_id"], target_output["block_id"])
    target_second = lattice_env["lattice"].materialize_template_step(artifact_id, "step-002", expected_template_sha256=template_sha, project_id=lattice_env["project"]["project_id"], role_session_id=lattice_env["session"]["role_session_id"], context_bindings=bindings_for(template["steps"][1]), invocation_bindings={"step-001": target_first["invocation_id"]})
    assert target_second["parents"] == []


@pytest.mark.contract
def test_contract_graph_neighborhood_returns_every_typed_edge_endpoint(lattice_env) -> None:
    invocation = completed_invocation(lattice_env, "graph", "answer")
    concern = "slate:typed-neighborhood"
    lattice_env["lattice"].tag(invocation["invocation_id"], TagRequest(concern_ref=concern, label="Typed neighborhood"))

    neighborhood = lattice_env["store"].graph_neighborhood(invocation["invocation_id"], depth=1)
    identities = {(node["object_type"], node["object_id"]) for node in neighborhood["nodes"]}

    assert ("invocation", invocation["invocation_id"]) in identities
    assert ("context-block", invocation["output_block_id"]) in identities
    assert ("external-concern", concern) in identities
    assert all((edge["source_type"], edge["source_id"]) in identities and (edge["target_type"], edge["target_id"]) in identities for edge in neighborhood["edges"])
    assert neighborhood["coverage"]["state"] == "complete"


@pytest.mark.regression
def test_negative_control_graph_limit_is_visible_and_never_emits_dangling_edges(lattice_env) -> None:
    invocation = completed_invocation(lattice_env, "bounded graph", "answer")
    for index in range(3):
        concern = f"slate:bounded-{index}"
        lattice_env["store"].add_edge(
            project_id=lattice_env["project"]["project_id"],
            relation=EdgeRelation.concerns,
            source_type="invocation",
            source_id=invocation["invocation_id"],
            source_revision=invocation["manifest_sha256"],
            target_type="external-concern",
            target_id=concern,
            target_revision=sha256_bytes(concern.encode()),
            metadata={},
        )

    neighborhood = lattice_env["store"].graph_neighborhood(invocation["invocation_id"], depth=1, max_nodes=2, max_edges=10)
    identities = {(node["object_type"], node["object_id"]) for node in neighborhood["nodes"]}

    assert neighborhood["coverage"]["state"] == "partial"
    assert neighborhood["coverage"]["completed_depth"] == 0
    assert len(neighborhood["nodes"]) == 2
    assert all((edge["source_type"], edge["source_id"]) in identities and (edge["target_type"], edge["target_id"]) in identities for edge in neighborhood["edges"])
