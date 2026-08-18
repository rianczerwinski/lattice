"""Contract and property tests for conversational lattice operations."""

from __future__ import annotations

import pytest

from loom.contracts import ContextBlockCreate, EdgeRelation, SliceCreate, TagRequest

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
