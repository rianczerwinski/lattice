"""Truth-typed assurance for stateless preflight and atomic conversation batches."""

from __future__ import annotations

import json

import pytest

from lattice.canonical import canonical_bytes, opaque_id, sha256_bytes, sha256_json
from lattice.contracts import (
    ContextAssemblyV1,
    ContextBlockCreate,
    ContextSelection,
    DerivedTextBlockCreate,
    DirectActorBinding,
    DraftCreate,
    EpisodeCreate,
    EpisodeMemberCreate,
    InvocationBatchCreateV1,
    InvocationBatchPreviewV1,
    InvocationSpecV1,
    ProjectCreate,
    ProviderRoute,
    TagRequest,
)
from lattice.manifest_reader import ManifestVerificationError, verify_and_reconstruct
from lattice.store import LatticeConflict

from conftest import completed_invocation


def _preview_request(lattice_env, *, parents: list[str] | None = None, prompt: str = "Synthesize this.") -> InvocationBatchPreviewV1:
    project_id = lattice_env["project"]["project_id"]
    assembly = ContextAssemblyV1(project_id=project_id, actor_binding=DirectActorBinding(), prompt=prompt, parent_invocation_ids=parents or [])
    return InvocationBatchPreviewV1(
        batch_id=opaque_id("batch"),
        specifications=[
            InvocationSpecV1(spec_id="codex", assembly=assembly, route=lattice_env["route"]),
            InvocationSpecV1(spec_id="claude", assembly=assembly, route=ProviderRoute(provider="anthropic", model="claude-sonnet-4-6", harness="claude")),
        ],
    )


@pytest.mark.oracle
def test_stateless_fanout_preview_shares_semantic_context_without_persistence(lattice_env) -> None:
    store = lattice_env["store"]
    request = _preview_request(lattice_env)
    with store._connect() as conn:
        before = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("events", "drafts", "invocations", "invocation_batches")}

    preview = lattice_env["lattice"].preview_invocation_batch(request)

    with store._connect() as conn:
        after = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before}
    assert after == before
    assert len({item["assembly_sha256"] for item in preview["members"]}) == 1
    assert len({item["preflight"]["compilation"]["compiled_prompt_sha256"] for item in preview["members"]}) == 1
    assert {item["preflight"]["execution"]["provider"] for item in preview["members"]} == {"openai", "anthropic"}
    assert all("capabilities" in item["preflight"] for item in preview["members"])


@pytest.mark.regression
def test_negative_control_fanout_rejects_route_specific_semantic_context(monkeypatch, lattice_env) -> None:
    compositor = lattice_env["lattice"].compositor
    real_compile = compositor.compile

    def divergent_compile(draft, **kwargs):
        manifest, compiled = real_compile(draft, **kwargs)
        if draft["route"]["provider"] == "anthropic":
            compiled += b"\nroute-specific semantic mutation"
            manifest["compilation"]["compiled_prompt_sha256"] = sha256_bytes(compiled)
        return manifest, compiled

    monkeypatch.setattr(compositor, "compile", divergent_compile)

    with pytest.raises(LatticeConflict, match="shared context assembly"):
        lattice_env["lattice"].preview_invocation_batch(_preview_request(lattice_env))


@pytest.mark.property
def test_atomic_batch_sealing_and_duplicate_submission_are_idempotent(lattice_env) -> None:
    request = _preview_request(lattice_env)
    preview = lattice_env["lattice"].preview_invocation_batch(request)
    create = InvocationBatchCreateV1(
        **request.model_dump(mode="json"),
        idempotency_key="batch-idempotency-0001",
        expected_preview_sha256=preview["preview_sha256"],
        confirmed_reason_sha256s=[item["reason_sha256"] for item in preview["confirmation_reasons"]],
    )

    first = lattice_env["lattice"].create_invocation_batch(create)
    second = lattice_env["lattice"].create_invocation_batch(create)

    assert first["receipt_sha256"] == second["receipt_sha256"]
    assert first["invocation_ids"] == second["invocation_ids"]
    with lattice_env["store"]._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM invocation_batches WHERE batch_id=?", (request.batch_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM invocation_batch_members WHERE batch_id=?", (request.batch_id,)).fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM invocations WHERE invocation_id IN (?,?)", first["invocation_ids"]).fetchone()[0] == 2


@pytest.mark.regression
def test_stale_preview_creates_no_partial_batch_objects(lattice_env) -> None:
    request = _preview_request(lattice_env)
    preview = lattice_env["lattice"].preview_invocation_batch(request)
    mutated = request.model_copy(deep=True)
    mutated.specifications[0].assembly.prompt = "Changed after confirmation"
    create = InvocationBatchCreateV1(
        **mutated.model_dump(mode="json"),
        idempotency_key="batch-idempotency-stale",
        expected_preview_sha256=preview["preview_sha256"],
    )
    with lattice_env["store"]._connect() as conn:
        before = tuple(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("drafts", "invocations", "invocation_batches"))

    with pytest.raises(LatticeConflict, match="preview changed"):
        lattice_env["lattice"].create_invocation_batch(create)

    with lattice_env["store"]._connect() as conn:
        after = tuple(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("drafts", "invocations", "invocation_batches"))
    assert after == before


@pytest.mark.regression
def test_nonterminal_crossfeed_source_is_rejected_before_preview(lattice_env) -> None:
    request = _preview_request(lattice_env)
    preview = lattice_env["lattice"].preview_invocation_batch(request)
    create = InvocationBatchCreateV1(
        **request.model_dump(mode="json"),
        idempotency_key="batch-idempotency-source",
        expected_preview_sha256=preview["preview_sha256"],
        confirmed_reason_sha256s=[item["reason_sha256"] for item in preview["confirmation_reasons"]],
    )
    sealed = lattice_env["lattice"].create_invocation_batch(create)

    child = _preview_request(lattice_env, parents=[sealed["invocation_ids"][0]])
    with pytest.raises(LatticeConflict, match="no terminal output"):
        lattice_env["lattice"].preview_invocation_batch(child)


@pytest.mark.oracle
def test_exact_utf8_excerpt_is_independently_bound_and_source_remains_unchanged(lattice_env) -> None:
    store = lattice_env["store"]
    source = store.create_context_block(ContextBlockCreate(project_id=lattice_env["project"]["project_id"], semantic_type="evidence", content="alpha 🌊 omega", status="disputed"))
    raw = "alpha 🌊 omega".encode("utf-8")
    start = raw.index("🌊".encode("utf-8"))
    end = start + len("🌊".encode("utf-8"))

    excerpt = store.create_derived_text_block(DerivedTextBlockCreate(project_id=lattice_env["project"]["project_id"], source_block_id=source["block_id"], start_byte=start, end_byte=end))
    draft = store.create_draft(DraftCreate(
        project_id=lattice_env["project"]["project_id"],
        actor_binding=DirectActorBinding(),
        prompt="Use the exact wave excerpt.",
        route=lattice_env["route"],
        selections=[ContextSelection(block_id=excerpt["block_id"], plane="knowledge", label="wave")],
    ))
    invocation = lattice_env["lattice"].seal(draft["draft_id"])
    segment = next(item for item in invocation["manifest"]["segments"] if item["block_id"] == excerpt["block_id"])

    assert store.get_block(excerpt["block_id"])["content"] == "🌊"
    assert store.get_block(source["block_id"])["status"] == "disputed"
    assert store.get_block(excerpt["block_id"])["transformation_json"] is not None
    assert segment["derived_from_block_ids"] == [source["block_id"]]
    assert f"lattice:block/{source['block_id']}" in {item["ref"] for item in invocation["manifest"]["generated_object"]["sources"]}
    assert verify_and_reconstruct(store, invocation["manifest"]) == store.blobs.get(invocation["compiled_prompt_sha256"])


@pytest.mark.regression
def test_excerpt_rejects_contradictory_caller_lineage(lattice_env) -> None:
    store = lattice_env["store"]
    source = store.create_context_block(ContextBlockCreate(project_id=lattice_env["project"]["project_id"], semantic_type="evidence", content="source"))
    other = store.create_context_block(ContextBlockCreate(project_id=lattice_env["project"]["project_id"], semantic_type="evidence", content="other"))
    excerpt = store.create_derived_text_block(DerivedTextBlockCreate(project_id=lattice_env["project"]["project_id"], source_block_id=source["block_id"], start_byte=0, end_byte=6))
    draft = store.create_draft(DraftCreate(
        project_id=lattice_env["project"]["project_id"],
        actor_binding=DirectActorBinding(),
        prompt="smuggle lineage",
        route=lattice_env["route"],
        selections=[ContextSelection(block_id=excerpt["block_id"], plane="knowledge", label="excerpt", derived_from_block_ids=[other["block_id"]])],
    ))

    with pytest.raises(LatticeConflict, match="contradicts"):
        lattice_env["lattice"].seal(draft["draft_id"])


@pytest.mark.regression
@pytest.mark.parametrize("mutation", ["offset", "source-hash"])
def test_independent_reader_rejects_forged_excerpt_receipts(lattice_env, mutation) -> None:
    store = lattice_env["store"]
    source = store.create_context_block(ContextBlockCreate(project_id=lattice_env["project"]["project_id"], semantic_type="evidence", content="alpha 🌊 omega"))
    raw = "alpha 🌊 omega".encode("utf-8")
    start = raw.index("🌊".encode("utf-8"))
    excerpt = store.create_derived_text_block(DerivedTextBlockCreate(project_id=lattice_env["project"]["project_id"], source_block_id=source["block_id"], start_byte=start, end_byte=start + 4))
    draft = store.create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], actor_binding=DirectActorBinding(), prompt="inspect", route=lattice_env["route"], selections=[ContextSelection(block_id=excerpt["block_id"], plane="knowledge", label="excerpt")]))
    invocation = lattice_env["lattice"].seal(draft["draft_id"])
    forged = json.loads(json.dumps(invocation["manifest"]))
    segment = next(item for item in forged["segments"] if item["block_id"] == excerpt["block_id"])
    if mutation == "offset":
        segment["transformation"]["start_byte"] = 0
        segment["transformation"]["end_byte"] = 4
    else:
        segment["transformation"]["source_sha256"] = "0" * 64
    overridden = store.get_block(excerpt["block_id"])
    overridden["transformation_json"] = canonical_bytes(segment["transformation"]).decode("utf-8")
    preflight_keys = {"invocation_id", "project_id", "actor_binding", "role_session_id", "draft_id", "draft_revision", "context_mode", "ancestry", "segments", "omitted", "source_bindings", "capabilities", "execution", "response_contract", "compilation", "provider_boundary"}
    forged["preflight_sha256"] = sha256_json({"contract_version": "LatticeContextPreflight/4.0", **{key: forged[key] for key in preflight_keys}})
    forged["manifest_sha256"] = sha256_json({key: value for key, value in forged.items() if key != "manifest_sha256"})

    with pytest.raises(ManifestVerificationError, match="exact excerpt bytes"):
        verify_and_reconstruct(store, forged, block_overrides={excerpt["block_id"]: overridden})


@pytest.mark.regression
def test_excerpt_rejects_split_utf8_and_cross_project_sources(lattice_env) -> None:
    store = lattice_env["store"]
    source = store.create_context_block(ContextBlockCreate(project_id=lattice_env["project"]["project_id"], semantic_type="evidence", content="🌊"))
    with pytest.raises(LatticeConflict, match="UTF-8"):
        store.create_derived_text_block(DerivedTextBlockCreate(project_id=lattice_env["project"]["project_id"], source_block_id=source["block_id"], start_byte=1, end_byte=4))

    other = store.create_project(ProjectCreate(title="other"))
    with pytest.raises(LatticeConflict, match="another project"):
        store.create_derived_text_block(DerivedTextBlockCreate(project_id=other["project_id"], source_block_id=source["block_id"], start_byte=0, end_byte=4))


@pytest.mark.oracle
def test_project_default_is_direct_context_and_muting_is_draft_local(lattice_env) -> None:
    store = lattice_env["store"]
    block = store.create_context_block(ContextBlockCreate(project_id=lattice_env["project"]["project_id"], semantic_type="context-module:local.weather", content="rain"))
    store.set_project_context_default(lattice_env["project"]["project_id"], block["block_id"], module_id="local.weather", mode="project_default")
    base = ContextAssemblyV1(project_id=lattice_env["project"]["project_id"], prompt="forecast", actor_binding=DirectActorBinding())
    active_request = InvocationBatchPreviewV1(batch_id=opaque_id("batch"), specifications=[InvocationSpecV1(spec_id="active", assembly=base, route=lattice_env["route"])])
    active = lattice_env["lattice"].preview_invocation_batch(active_request)

    muted_assembly = base.model_copy(deep=True)
    muted_assembly.selections = [ContextSelection(block_id=block["block_id"], disposition="omitted", plane="state", label="module:local.weather", reason="operator-muted")]
    muted_request = InvocationBatchPreviewV1(batch_id=opaque_id("batch"), specifications=[InvocationSpecV1(spec_id="muted", assembly=muted_assembly, route=lattice_env["route"])])
    muted = lattice_env["lattice"].preview_invocation_batch(muted_request)

    assert any(item["block_id"] == block["block_id"] for item in active["members"][0]["preflight"]["segments"])
    assert all(item["block_id"] != block["block_id"] for item in muted["members"][0]["preflight"]["segments"])
    assert any(item["block_id"] == block["block_id"] for item in muted["members"][0]["preflight"]["omitted"])
    assert store.get_block(block["block_id"])["status"] == "active"


@pytest.mark.property
def test_only_knowledge_plane_obeys_operator_selection_order(lattice_env) -> None:
    store = lattice_env["store"]
    first = store.create_context_block(ContextBlockCreate(project_id=lattice_env["project"]["project_id"], semantic_type="evidence", content="first"))
    second = store.create_context_block(ContextBlockCreate(project_id=lattice_env["project"]["project_id"], semantic_type="evidence", content="second"))
    assembly = ContextAssemblyV1(
        project_id=lattice_env["project"]["project_id"],
        prompt="order",
        selections=[
            ContextSelection(block_id=second["block_id"], plane="knowledge", label="z-second"),
            ContextSelection(block_id=first["block_id"], plane="knowledge", label="a-first"),
        ],
    )
    request = InvocationBatchPreviewV1(batch_id=opaque_id("batch"), specifications=[InvocationSpecV1(spec_id="ordered", assembly=assembly, route=lattice_env["route"])])
    preview = lattice_env["lattice"].preview_invocation_batch(request)
    knowledge = [item["block_id"] for item in preview["members"][0]["preflight"]["segments"] if item["plane"] == "knowledge"]

    assert knowledge == [second["block_id"], first["block_id"]]


@pytest.mark.oracle
def test_episodes_are_overlapping_live_slices_and_returned_work_is_new_history(lattice_env) -> None:
    root = completed_invocation(lattice_env, "root", "root answer")
    parent_episode = lattice_env["store"].create_episode(EpisodeCreate(project_id=lattice_env["project"]["project_id"], name="Main line", anchor_type="invocation", anchor_id=root["invocation_id"]))
    child_episode = lattice_env["store"].create_episode(EpisodeCreate(project_id=lattice_env["project"]["project_id"], name="Side quest", parent_episode_id=parent_episode["slice_id"], anchor_type="invocation", anchor_id=root["invocation_id"]))

    memberships = lattice_env["store"].list_invocation_episodes(root["invocation_id"])
    assert {item["slice_id"] for item in memberships} == {parent_episode["slice_id"], child_episode["slice_id"]}
    assert child_episode["parent_episode_id"] == parent_episode["slice_id"]

    request = InvocationBatchPreviewV1(
        batch_id=opaque_id("batch"),
        specifications=[InvocationSpecV1(
            spec_id="return",
            assembly=ContextAssemblyV1(project_id=lattice_env["project"]["project_id"], prompt="Return the useful result", parent_invocation_ids=[root["invocation_id"]]),
            route=lattice_env["route"],
            concern_refs=[parent_episode["concern_ref"]],
        )],
    )
    preview = lattice_env["lattice"].preview_invocation_batch(request)
    returned = lattice_env["lattice"].create_invocation_batch(InvocationBatchCreateV1(**request.model_dump(mode="json"), idempotency_key="episode-return-0001", expected_preview_sha256=preview["preview_sha256"], confirmed_reason_sha256s=[]))

    assert returned["invocation_ids"] != [root["invocation_id"]]
    parent_live = lattice_env["store"].get_episode(parent_episode["slice_id"])
    assert {item["object_id"] for item in parent_live["items"]} == {root["invocation_id"], returned["invocation_ids"][0]}
    with lattice_env["store"]._connect() as conn:
        query_bytes = conn.execute("SELECT query_json FROM slices WHERE slice_id=?", (parent_episode["slice_id"],)).fetchone()[0]
    assert "root answer" not in query_bytes


@pytest.mark.oracle
def test_artifact_episode_members_remain_live_references_without_binary_text_coercion(lattice_env) -> None:
    store = lattice_env["store"]
    draft = store.create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], role_session_id=lattice_env["session"]["role_session_id"], prompt="Produce evidence artifacts", route=lattice_env["route"]))
    invocation = lattice_env["lattice"].seal(draft["draft_id"])
    attempt = store.create_attempt(invocation["invocation_id"], "artifact-episode-attempt", "test-boot", 9_999_999_999)
    completed = store.finish_successful_attempt(
        attempt["attempt_id"],
        response="Artifacts produced",
        artifacts=[
            {"name": "finding.txt", "media_type": "text/plain; charset=utf-8", "content": "exact textual finding"},
            {"name": "diagram.png", "media_type": "image/png", "content": "not conversational text"},
        ],
        journal_path="fixture:artifact-episode",
        runtime_receipt={"fixture": True},
    )
    artifacts = {store.get_artifact(item["artifact_id"], include_content=False)["generated"]["name"]: item for item in completed["artifacts"]}
    text_artifact = artifacts["finding.txt"]
    binary_artifact = artifacts["diagram.png"]

    episode = store.create_episode(EpisodeCreate(project_id=lattice_env["project"]["project_id"], name="Artifact side quest", anchor_type="artifact", anchor_id=text_artifact["artifact_id"]))
    episode = store.add_episode_member(episode["slice_id"], EpisodeMemberCreate(object_type="artifact", object_id=binary_artifact["artifact_id"]))["episode"]
    members = {item["artifact_id"]: item for item in episode["items"]}

    assert set(members) == {text_artifact["artifact_id"], binary_artifact["artifact_id"]}
    assert members[text_artifact["artifact_id"]]["response"] == "exact textual finding"
    assert members[text_artifact["artifact_id"]]["response_state"] == "included"
    assert members[text_artifact["artifact_id"]]["content_sha256"] == text_artifact["content_sha256"]
    assert members[binary_artifact["artifact_id"]]["response_state"] == "binary-artifact"
    assert "response" not in members[binary_artifact["artifact_id"]]
    assert members[binary_artifact["artifact_id"]]["content_sha256"] == binary_artifact["content_sha256"]
    with store._connect() as conn:
        query_bytes = conn.execute("SELECT query_json FROM slices WHERE slice_id=?", (episode["slice_id"],)).fetchone()[0]
    assert "exact textual finding" not in query_bytes and "not conversational text" not in query_bytes


@pytest.mark.oracle
def test_invocation_and_response_block_share_one_live_slate_slice_without_copying(lattice_env) -> None:
    invocation = completed_invocation(lattice_env, "private prompt", "source-bound response")
    tag = TagRequest(concern_ref="slate:topic-live", label="Topic live")
    invocation_result = lattice_env["store"].tag_invocation(invocation["invocation_id"], tag)
    block_result = lattice_env["store"].tag_context_block(invocation["output_block_id"], tag)

    assert invocation_result["slice"]["slice_id"] == block_result["slice"]["slice_id"]
    assert {(item["object_type"], item["object_id"]) for item in block_result["slice"]["items"]} == {
        ("invocation", invocation["invocation_id"]),
        ("context-block", invocation["output_block_id"]),
    }
    with lattice_env["store"]._connect() as conn:
        stored = conn.execute("SELECT query_json FROM slices WHERE slice_id=?", (block_result["slice"]["slice_id"],)).fetchone()[0]
    assert "private prompt" not in stored and "source-bound response" not in stored
