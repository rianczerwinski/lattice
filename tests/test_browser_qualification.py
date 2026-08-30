"""Independent oracle and mutations for the installed-browser receipt builder."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from lattice.canonical import opaque_id
from lattice.contracts import (
    ContextAssemblyV1,
    ContextSelection,
    DerivedTextBlockCreate,
    DirectActorBinding,
    EpisodeCreate,
    InvocationBatchCreateV1,
    InvocationBatchPreviewV1,
    InvocationSpecV1,
    ProviderRoute,
    TagRequest,
)


QUALIFIER_PATH = Path(__file__).parents[1] / "ops" / "qualify_browser.py"
SPEC = importlib.util.spec_from_file_location("qualify_browser", QUALIFIER_PATH)
assert SPEC and SPEC.loader
qualifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qualifier)


def _complete_batch(lattice_env, specifications: list[InvocationSpecV1], responses: list[str]) -> list[dict]:
    batch_id = opaque_id("batch")
    preview_request = InvocationBatchPreviewV1(batch_id=batch_id, specifications=specifications)
    preview = lattice_env["lattice"].preview_invocation_batch(preview_request)
    batch = lattice_env["lattice"].create_invocation_batch(
        InvocationBatchCreateV1(
            batch_id=batch_id,
            specifications=specifications,
            idempotency_key=f"browser-{batch_id}",
            expected_preview_sha256=preview["preview_sha256"],
            confirmed_reason_sha256s=[item["reason_sha256"] for item in preview["confirmation_reasons"]],
        )
    )
    completed = []
    for invocation_id, response in zip(batch["invocation_ids"], responses, strict=True):
        attempt = lattice_env["store"].create_attempt(invocation_id, f"attempt-{invocation_id}", "browser-fixture", 9_999_999_999)
        completed.append(
            lattice_env["store"].finish_successful_attempt(
                attempt["attempt_id"],
                response=response,
                artifacts=[],
                journal_path="fixture:installed-browser",
                runtime_receipt={"fixture": "installed-browser"},
            )
        )
    return completed


def _seed_browser_journey(lattice_env, run_id: str) -> tuple[dict, dict[str, str]]:
    project_id = lattice_env["project"]["project_id"]
    openai = lattice_env["route"]
    anthropic = ProviderRoute(provider="anthropic", model="claude-sonnet-4-6", harness="claude", effort="high")
    markers = {
        "root": f"BROWSER-ROOT-{run_id}",
        "excerpt": f"BROWSER-EXCERPT-{run_id}",
        "side": f"BROWSER-SIDE-{run_id}",
        "fanout": f"BROWSER-FANOUT-{run_id}",
        "crossfeed": f"BROWSER-XFEED-{run_id}",
        "join": f"BROWSER-JOIN-{run_id}",
        "dormant": f"BROWSER-DORMANT-{run_id}",
    }

    def assembly(prompt: str, *, parents: list[str] | None = None, selections: list[ContextSelection] | None = None) -> ContextAssemblyV1:
        return ContextAssemblyV1(project_id=project_id, actor_binding=DirectActorBinding(), prompt=prompt, parent_invocation_ids=parents or [], selections=selections or [])

    root = _complete_batch(
        lattice_env,
        [InvocationSpecV1(spec_id="anthropic", assembly=assembly(f"root prompt {markers['root']}"), route=anthropic)],
        [f"The exact visible answer contains {markers['root']} and {markers['excerpt']} together."],
    )[0]
    root_bytes = root["output_block"]["content"].encode("utf-8")
    excerpt_bytes = markers["excerpt"].encode("utf-8")
    start = root_bytes.index(excerpt_bytes)
    excerpt = lattice_env["store"].create_derived_text_block(
        DerivedTextBlockCreate(project_id=project_id, source_block_id=root["output_block_id"], start_byte=start, end_byte=start + len(excerpt_bytes))
    )
    parent_episode = lattice_env["store"].create_episode(EpisodeCreate(project_id=project_id, name="Main trail", anchor_type="invocation", anchor_id=root["invocation_id"]))
    child_episode = lattice_env["store"].create_episode(EpisodeCreate(project_id=project_id, name="Side quest", parent_episode_id=parent_episode["slice_id"], anchor_type="context-block", anchor_id=excerpt["block_id"]))
    side = _complete_batch(
        lattice_env,
        [InvocationSpecV1(
            spec_id="openai",
            assembly=assembly(
                f"side prompt {markers['side']}",
                parents=[root["invocation_id"]],
                selections=[ContextSelection(block_id=excerpt["block_id"], plane="knowledge", label="exact excerpt", derived_from_block_ids=[root["output_block_id"]])],
            ),
            route=openai,
            concern_refs=[child_episode["concern_ref"]],
        )],
        [markers["side"]],
    )[0]
    shared_fanout = assembly(f"fanout prompt {markers['fanout']}")
    fanout = _complete_batch(
        lattice_env,
        [
            InvocationSpecV1(spec_id="anthropic", assembly=shared_fanout, route=anthropic),
            InvocationSpecV1(spec_id="openai", assembly=shared_fanout, route=openai),
        ],
        [markers["fanout"], markers["fanout"]],
    )
    fanout_by_provider = {item["route"]["provider"]: item for item in fanout}
    crossfeed = _complete_batch(
        lattice_env,
        [
            InvocationSpecV1(spec_id="anthropic", assembly=assembly(f"crossfeed prompt {markers['crossfeed']}", parents=[fanout_by_provider["openai"]["invocation_id"]]), route=anthropic),
            InvocationSpecV1(spec_id="openai", assembly=assembly(f"crossfeed prompt {markers['crossfeed']}", parents=[fanout_by_provider["anthropic"]["invocation_id"]]), route=openai),
        ],
        [markers["crossfeed"], markers["crossfeed"]],
    )
    joined = _complete_batch(
        lattice_env,
        [InvocationSpecV1(spec_id="openai", assembly=assembly(f"join prompt {markers['join']}", parents=[item["invocation_id"] for item in crossfeed]), route=openai)],
        [markers["join"]],
    )[0]
    dormant = _complete_batch(
        lattice_env,
        [InvocationSpecV1(spec_id="anthropic", assembly=assembly(f"dormant prompt {markers['dormant']}", parents=[root["invocation_id"]]), route=anthropic)],
        [markers["dormant"]],
    )[0]
    slate_ref = f"slate:lattice-browser-acceptance-{run_id}"
    lattice_env["store"].tag_invocation(dormant["invocation_id"], TagRequest(concern_ref=slate_ref, label=f"Browser acceptance {run_id}"))
    driver = {"run_id": run_id, "project_title": lattice_env["project"]["title"], "slate_external_ref": slate_ref, "markers": markers}
    return driver, {"excerpt_block_id": excerpt["block_id"], "side_invocation_id": side["invocation_id"], "join_invocation_id": joined["invocation_id"]}


@pytest.mark.oracle
def test_browser_receipt_reconstructs_the_hand_authored_freestyle_topology(lattice_env) -> None:
    driver, identities = _seed_browser_journey(lattice_env, "oracle")

    receipt = qualifier.build_receipt(data_root=lattice_env["config"].data_root, driver=driver, runtime_artifact_id="artifact:installed-runtime")

    assert receipt["contract_version"] == "LatticeBrowserAcceptanceReceipt/1.0"
    assert [item["action"] for item in receipt["actions"]] == list(qualifier.REQUIRED_ACTIONS)
    assert receipt["excerpt"]["block_id"] == identities["excerpt_block_id"]
    assert identities["side_invocation_id"] in receipt["manifest_sha256s"]
    assert receipt["topology"]["join_parent_invocation_ids"]
    assert receipt["slate"]["copied_transcript_bytes"] == 0


@pytest.mark.regression
def test_negative_control_browser_receipt_rejects_a_crossfeed_parent_swap(lattice_env) -> None:
    driver, _identities = _seed_browser_journey(lattice_env, "broken-parent")
    with lattice_env["store"]._connect() as connection:
        connection.create_function("lattice_replaying", 0, lambda: 1)
        row = connection.execute("SELECT d.draft_id,d.parents_json FROM drafts d JOIN context_blocks p ON p.block_id=d.prompt_block_id WHERE p.content_sha256 IN (SELECT content_sha256 FROM context_blocks WHERE semantic_type='user-prompt') AND d.project_id=? ORDER BY d.created_at", (lattice_env["project"]["project_id"],)).fetchall()
        crossfeed_drafts = [item for item in row if "BROWSER-XFEED" in lattice_env["store"].get_block(connection.execute("SELECT prompt_block_id FROM drafts WHERE draft_id=?", (item["draft_id"],)).fetchone()[0])["content"]]
        assert len(crossfeed_drafts) == 2
        first_parents = crossfeed_drafts[0]["parents_json"]
        connection.execute("UPDATE drafts SET parents_json=? WHERE draft_id=?", (first_parents, crossfeed_drafts[1]["draft_id"]))

    with pytest.raises(qualifier.BrowserQualificationFailure, match="opposite provider branch"):
        qualifier.build_receipt(data_root=lattice_env["config"].data_root, driver=driver, runtime_artifact_id="artifact:installed-runtime")


@pytest.mark.regression
def test_negative_control_browser_receipt_rejects_transformation_drift(lattice_env) -> None:
    driver, identities = _seed_browser_journey(lattice_env, "broken-excerpt")
    with lattice_env["store"]._connect() as connection:
        connection.create_function("lattice_replaying", 0, lambda: 1)
        transformation = json.loads(connection.execute("SELECT transformation_json FROM context_blocks WHERE block_id=?", (identities["excerpt_block_id"],)).fetchone()[0])
        transformation["start_byte"] += 1
        connection.execute("UPDATE context_blocks SET transformation_json=? WHERE block_id=?", (json.dumps(transformation, sort_keys=True, separators=(",", ":")), identities["excerpt_block_id"]))

    with pytest.raises(qualifier.BrowserQualificationFailure, match="excerpt bytes"):
        qualifier.build_receipt(data_root=lattice_env["config"].data_root, driver=driver, runtime_artifact_id="artifact:installed-runtime")
