"""Oracle and corruption mutation for restored-continuation qualification."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from lattice import Lattice, LatticeConfig, LatticeStore
from lattice.canonical import canonical_bytes, generated_object
from lattice.contracts import DirectActorBinding, DraftCreate
from lattice.contracts import InvocationBatchPreviewV1
from lattice.diagnostics import assess_data_root
from lattice.snapshot import create_snapshot, restore_snapshot


QUALIFIER_PATH = Path(__file__).parents[1] / "ops" / "qualify_restore.py"
SPEC = importlib.util.spec_from_file_location("qualify_restore", QUALIFIER_PATH)
assert SPEC and SPEC.loader
qualifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qualifier)


@pytest.mark.regression
def test_restored_server_does_not_inherit_axisvi_outport_identity() -> None:
    assert qualifier._standalone_environment({"AXISVI_ROOT": "/repo", "PATH": "/bin"}) == {"PATH": "/bin"}


@pytest.mark.regression
def test_restore_continuation_stays_inside_the_browser_project(lattice_env) -> None:
    request = qualifier._project_batch_request(lattice_env["project"]["project_id"], "restore-operation", "RESTORE-MARKER", "invocation_parent")
    validated = InvocationBatchPreviewV1.model_validate(request)

    assembly = validated.specifications[0].assembly
    assert assembly.project_id == lattice_env["project"]["project_id"]
    assert assembly.parent_invocation_ids == ["invocation_parent"]
    assert "external_project_key" not in request


def _complete(store: LatticeStore, lattice: Lattice, project_id: str, route, prompt: str, response: str, parents: list[str] | None = None) -> dict:
    draft = store.create_draft(DraftCreate(project_id=project_id, actor_binding=DirectActorBinding(), prompt=prompt, route=route, parent_invocation_ids=parents or []))
    invocation = lattice.seal(draft["draft_id"])
    attempt = store.create_attempt(invocation["invocation_id"], f"restore-{invocation['invocation_id']}", "restore-fixture", 9_999_999_999)
    return store.finish_successful_attempt(attempt["attempt_id"], response=response, artifacts=[], journal_path="fixture:restore", runtime_receipt={"fixture": "restore"})


def _evidence(lattice_env, tmp_path: Path) -> dict:
    parent = _complete(lattice_env["store"], lattice_env["lattice"], lattice_env["project"]["project_id"], lattice_env["route"], "dormant", "dormant response")
    canonical = _complete(lattice_env["store"], lattice_env["lattice"], lattice_env["project"]["project_id"], lattice_env["route"], "after restart", "canonical response", [parent["invocation_id"]])
    runtime = {
        "generated_object": generated_object(kind="lattice-installed-runtime", schema_name="lattice-installed-runtime", subject="lattice:runtime:fixture", role="canonical", sources=[{"ref": "fixture:runtime-source", "revision": "fixture", "sha256": "0" * 64}]),
        "contract_version": "LatticeInstalledRuntime/4.0",
        "runtime_id": "0.3.1-fixture",
    }
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_bytes(canonical_bytes(runtime))
    browser = {
        "generated_object": generated_object(kind="lattice-browser-acceptance-receipt", schema_name="lattice-browser-acceptance-receipt", subject="fixture:browser", role="snapshot", sources=[{"ref": "fixture:browser-source", "revision": "fixture", "sha256": "1" * 64}]),
        "contract_version": "LatticeBrowserAcceptanceReceipt/1.0",
        "actions": [{"action": "dormant-branch-return", "invocation_ids": [parent["invocation_id"]]}],
    }
    browser_path = tmp_path / "browser.json"
    browser_path.write_bytes(canonical_bytes(browser))
    source = assess_data_root(lattice_env["config"].data_root)
    snapshot = create_snapshot(lattice_env["config"].data_root, backup_root=tmp_path / "backups", reason="restore-qualification-oracle", source_revision=source["state_digest"], schema_version=source["schema_version"], state_digest=source["state_digest"])
    restored_root = tmp_path / "restored"
    restore_snapshot(snapshot, restored_root)
    restored_pre = assess_data_root(restored_root)["state_digest"]
    restored_store = LatticeStore(LatticeConfig(data_root=restored_root, port=19010, axisvi_root=None))
    child = _complete(restored_store, Lattice(restored_store), lattice_env["project"]["project_id"], lattice_env["route"], "restored child", "restored response", [canonical["invocation_id"]])
    return {"runtime_path": runtime_path, "browser_path": browser_path, "snapshot": snapshot, "restored_root": restored_root, "restored_pre": restored_pre, "parent": parent, "canonical": canonical, "child": child, "restored_store": restored_store}


@pytest.mark.oracle
def test_restore_receipt_binds_snapshot_identity_and_new_restored_ancestry(lattice_env, tmp_path: Path) -> None:
    evidence = _evidence(lattice_env, tmp_path)

    receipt = qualifier.build_receipt(
        runtime_manifest=evidence["runtime_path"],
        browser_receipt=evidence["browser_path"],
        snapshot_root=evidence["snapshot"],
        restored_root=evidence["restored_root"],
        pre_restart_boot_id="boot-before",
        post_restart_boot_id="boot-after",
        browser_parent_invocation_id=evidence["parent"]["invocation_id"],
        canonical_continuation={"state": "succeeded", "invocation_id": evidence["canonical"]["invocation_id"], "manifest_sha256": evidence["canonical"]["manifest_sha256"], "parent_invocation_id": evidence["parent"]["invocation_id"]},
        restored_child={"state": "succeeded", "invocation_id": evidence["child"]["invocation_id"], "manifest_sha256": evidence["child"]["manifest_sha256"], "parent_invocation_id": evidence["canonical"]["invocation_id"]},
        restored_pre_state_digest=evidence["restored_pre"],
    )

    assert receipt["contract_version"] == "LatticeRestoreContinuationReceipt/1.0"
    assert receipt["source_state_digest"] == receipt["restored_pre_continuation_state_digest"]
    assert receipt["restored_post_continuation_state_digest"] != receipt["restored_pre_continuation_state_digest"]
    assert receipt["parent_invocation_id"] == evidence["canonical"]["invocation_id"]
    assert receipt["child_invocation_id"] == evidence["child"]["invocation_id"]


@pytest.mark.regression
def test_negative_control_restore_receipt_rejects_a_missing_restored_blob(lattice_env, tmp_path: Path) -> None:
    evidence = _evidence(lattice_env, tmp_path)
    digest = evidence["child"]["output_block"]["content_sha256"]
    (evidence["restored_root"] / "blobs" / digest[:2] / digest).unlink()

    with pytest.raises(OSError, match="missing blob"):
        qualifier.build_receipt(
            runtime_manifest=evidence["runtime_path"],
            browser_receipt=evidence["browser_path"],
            snapshot_root=evidence["snapshot"],
            restored_root=evidence["restored_root"],
            pre_restart_boot_id="boot-before",
            post_restart_boot_id="boot-after",
            browser_parent_invocation_id=evidence["parent"]["invocation_id"],
            canonical_continuation={"state": "succeeded", "invocation_id": evidence["canonical"]["invocation_id"], "manifest_sha256": evidence["canonical"]["manifest_sha256"], "parent_invocation_id": evidence["parent"]["invocation_id"]},
            restored_child={"state": "succeeded", "invocation_id": evidence["child"]["invocation_id"], "manifest_sha256": evidence["child"]["manifest_sha256"], "parent_invocation_id": evidence["canonical"]["invocation_id"]},
            restored_pre_state_digest=evidence["restored_pre"],
        )
