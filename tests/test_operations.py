"""Contract and property tests for conversational lattice operations."""

from __future__ import annotations

import pytest

from loom.contracts import BlockDisposition, ContextBlockCreate, ContextSelection, DraftCreate, EdgeRelation, ProjectCreate, RoleCreate, RoleSessionCreate, SliceCreate, TagRequest

from conftest import completed_invocation


@pytest.mark.contract
def test_contract_fork_join_diff_replay_and_tag(loom_env) -> None:
    left = completed_invocation(loom_env, "P1", "left")
    right = completed_invocation(loom_env, "P2", "right")
    fork = loom_env["loom"].fork(left["invocation_id"], role_session_id=loom_env["session"]["role_session_id"], prompt="challenge", route=loom_env["route"])
    joined = loom_env["loom"].join([left["invocation_id"], right["invocation_id"]], role_session_id=loom_env["session"]["role_session_id"], prompt="join", route=loom_env["route"])
    sealed_fork = loom_env["loom"].seal(fork["draft_id"])
    sealed_join = loom_env["loom"].seal(joined["draft_id"])
    difference = loom_env["loom"].diff(sealed_fork["invocation_id"], sealed_join["invocation_id"])
    replay = loom_env["loom"].replay(sealed_fork["invocation_id"], prompt="fresh replay")
    tagged = loom_env["loom"].tag(left["invocation_id"], TagRequest(concern_ref="slate:canon-quaternary-german", label="Canon > Quaternary > German"))

    assert set(joined["parents"]) == {left["invocation_id"], right["invocation_id"]}
    assert difference["only_right"]
    assert replay["invocation_id"] is None
    assert tagged["slice"]["items"][0]["invocation_id"] == left["invocation_id"]


@pytest.mark.property
def test_property_replay_never_mutates_original(loom_env) -> None:
    original = completed_invocation(loom_env, "original prompt", "original result")
    before = loom_env["loom"].inspect(original["invocation_id"])
    replay = loom_env["loom"].replay(original["invocation_id"], prompt="changed prompt")
    loom_env["loom"].seal(replay["draft_id"])
    after = loom_env["loom"].inspect(original["invocation_id"])

    assert before["manifest_sha256"] == after["manifest_sha256"]
    assert before["output_block"]["content"] == "original result"


@pytest.mark.oracle
def test_oracle_replay_preserves_exact_context_and_uses_a_provenance_edge(loom_env) -> None:
    store = loom_env["store"]
    omitted = store.create_context_block(ContextBlockCreate(project_id=loom_env["project"]["project_id"], semantic_type="known-unknown", content="not supplied"))
    draft = store.create_draft(DraftCreate(project_id=loom_env["project"]["project_id"], role_session_id=loom_env["session"]["role_session_id"], prompt="repeat exactly", route=loom_env["route"], selections=[ContextSelection(block_id=omitted["block_id"], disposition=BlockDisposition.omitted, plane="knowledge", label="deliberately absent", reason="negative control")]))
    original = loom_env["loom"].seal(draft["draft_id"])

    replay_draft = loom_env["loom"].replay(original["invocation_id"])
    replayed = loom_env["loom"].seal(replay_draft["draft_id"])

    assert replayed["compiled_prompt_sha256"] == original["compiled_prompt_sha256"]
    assert replayed["manifest"]["segments"] == original["manifest"]["segments"]
    assert replayed["manifest"]["omitted"] == original["manifest"]["omitted"]
    assert any(edge["relation"] == "derived_from" and edge["target_id"] == original["invocation_id"] for edge in replayed["edges"])
    assert not any(edge["relation"] == "inherited_context_from" for edge in replayed["edges"])


@pytest.mark.oracle
def test_negative_control_replay_with_changed_prompt_changes_only_prompt_context(loom_env) -> None:
    original = completed_invocation(loom_env, "original prompt", "response")
    replay = loom_env["loom"].replay(original["invocation_id"], prompt="changed prompt")
    sealed = loom_env["loom"].seal(replay["draft_id"])
    difference = loom_env["loom"].diff(original["invocation_id"], sealed["invocation_id"])

    assert sealed["compiled_prompt_sha256"] != original["compiled_prompt_sha256"]
    assert {item["plane"] for item in difference["only_left"] + difference["only_right"]} == {"current_prompt"}


@pytest.mark.contract
def test_slate_slice_can_render_authoritative_blackboard_without_copying_it(loom_env) -> None:
    store = loom_env["store"]
    project_id = loom_env["project"]["project_id"]
    block = store.create_context_block(ContextBlockCreate(project_id=project_id, semantic_type="workflow-blackboard-snapshot", content='{"state":"calibrating"}', authority="quaternary", evidence_level="authoritative-read"))
    concern = "slate:canon-quaternary-german"
    store.add_edge(project_id=project_id, relation=EdgeRelation.concerns, source_type="context-block", source_id=block["block_id"], source_revision=block["content_sha256"], target_type="external-concern", target_id=concern, target_revision=concern, metadata={})
    slice_value = store.create_slice(SliceCreate(project_id=project_id, name="German", external_ref=concern, query={"concern_ref": concern, "object_types": ["context-block"]}))
    rendered = store.get_slice(slice_value["slice_id"])
    assert rendered["items"][0]["object_id"] == block["block_id"]
    assert rendered["items"][0]["response"] == '{"state":"calibrating"}'
    assert rendered["external_ref"] == concern


@pytest.mark.oracle
def test_oracle_slate_concern_slice_spans_projects_without_copying_invocations(loom_env) -> None:
    store = loom_env["store"]
    concern = "slate:shared-topic"
    first = completed_invocation(loom_env, "first project", "first result")
    tagged = loom_env["loom"].tag(first["invocation_id"], TagRequest(concern_ref=concern, label="Shared topic"))
    other_project = store.create_project(ProjectCreate(title="Other", purpose="cross-project slice oracle"))
    other_role = store.create_role(other_project["project_id"], RoleCreate(name="operator", instructions="Use supplied context."))
    other_session = store.create_role_session(RoleSessionCreate(project_id=other_project["project_id"], role_id=other_role["role_id"], label="other"))
    other_env = {**loom_env, "project": other_project, "role": other_role, "session": other_session}
    second = completed_invocation(other_env, "second project", "second result")
    store.add_edge(project_id=other_project["project_id"], relation=EdgeRelation.concerns, source_type="invocation", source_id=second["invocation_id"], source_revision=second["manifest_sha256"], target_type="external-concern", target_id=concern, target_revision=concern, metadata={})

    tagged["slice"] = store.get_slice(tagged["slice"]["slice_id"])

    assert tagged["slice"]["scope"] == "global"
    assert {item["invocation_id"] for item in tagged["slice"]["items"]} == {first["invocation_id"], second["invocation_id"]}


@pytest.mark.contract
def test_contract_diff_exposes_omission_semantics_for_the_same_block(loom_env) -> None:
    store = loom_env["store"]
    block = store.create_context_block(ContextBlockCreate(project_id=loom_env["project"]["project_id"], semantic_type="evidence", content="same bytes"))
    selected = store.create_draft(DraftCreate(project_id=loom_env["project"]["project_id"], role_session_id=loom_env["session"]["role_session_id"], prompt="left", route=loom_env["route"], selections=[ContextSelection(block_id=block["block_id"], plane="knowledge", label="evidence")]))
    omitted = store.create_draft(DraftCreate(project_id=loom_env["project"]["project_id"], role_session_id=loom_env["session"]["role_session_id"], prompt="right", route=loom_env["route"], selections=[ContextSelection(block_id=block["block_id"], disposition=BlockDisposition.omitted, plane="knowledge", label="evidence", reason="withheld")]))
    left = loom_env["loom"].seal(selected["draft_id"])
    right = loom_env["loom"].seal(omitted["draft_id"])

    difference = loom_env["loom"].diff(left["invocation_id"], right["invocation_id"])

    assert difference["changed_semantics"][0]["block_id"] == block["block_id"]
    assert difference["changed_semantics"][0]["right"][0]["disposition"] == "omitted"


@pytest.mark.oracle
def test_oracle_extracted_workflow_artifact_resolves_to_its_exact_bytes(loom_env) -> None:
    invocation = completed_invocation(loom_env, "extract me", "result")
    artifact = loom_env["loom"].extract(loom_env["project"]["project_id"], [invocation["invocation_id"]], "Reusable path")

    stored = loom_env["store"].get_artifact(artifact["generated_object"]["artifact_id"])

    assert stored["generated"] == artifact
    assert stored["content"]


@pytest.mark.contract
def test_contract_graph_neighborhood_returns_every_typed_edge_endpoint(loom_env) -> None:
    invocation = completed_invocation(loom_env, "graph", "answer")
    concern = "slate:typed-neighborhood"
    loom_env["loom"].tag(invocation["invocation_id"], TagRequest(concern_ref=concern, label="Typed neighborhood"))

    neighborhood = loom_env["store"].graph_neighborhood(invocation["invocation_id"], depth=1)
    identities = {(node["object_type"], node["object_id"]) for node in neighborhood["nodes"]}

    assert ("invocation", invocation["invocation_id"]) in identities
    assert ("context-block", invocation["output_block_id"]) in identities
    assert ("external-concern", concern) in identities
    assert all((edge["source_type"], edge["source_id"]) in identities and (edge["target_type"], edge["target_id"]) in identities for edge in neighborhood["edges"])
