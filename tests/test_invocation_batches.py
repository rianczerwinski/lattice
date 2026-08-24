"""Truth-typed assurance for stateless preflight and atomic conversation batches."""

from __future__ import annotations

import pytest

from lattice.canonical import opaque_id
from lattice.contracts import (
    ContextAssemblyV1,
    ContextBlockCreate,
    ContextSelection,
    DerivedTextBlockCreate,
    DirectActorBinding,
    InvocationBatchCreateV1,
    InvocationBatchPreviewV1,
    InvocationSpecV1,
    ProjectCreate,
    ProviderRoute,
)
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

    assert store.get_block(excerpt["block_id"])["content"] == "🌊"
    assert store.get_block(source["block_id"])["status"] == "disputed"
    assert store.get_block(excerpt["block_id"])["transformation_json"] is not None


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
