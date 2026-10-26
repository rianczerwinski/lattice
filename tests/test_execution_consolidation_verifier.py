"""Adversarial checks for independent execution-consolidation closure."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import sqlite3
from pathlib import Path

import pytest

from lattice.canonical import canonical_bytes, generated_object, sha256_bytes
from lattice.contracts import AttemptState, ExecutionSubmissionV1, OutputContractV2, ProviderRoute
from lattice.diagnostics import assess_data_root
from lattice.snapshot import create_snapshot


# LATTICE_SOURCE_ROOT lets an isolated copy of this file exercise the live
# verifier and fixtures read-only; inside the checkout it is unset and inert.
REPOSITORY_ROOT = Path(os.environ.get("LATTICE_SOURCE_ROOT") or Path(__file__).parents[1])
VERIFIER_PATH = REPOSITORY_ROOT / "ops" / "verify_execution_consolidation.py"
SPEC = importlib.util.spec_from_file_location("verify_execution_consolidation", VERIFIER_PATH)
assert SPEC and SPEC.loader
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


def _load_source_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.regression
def test_independent_verifier_accepts_v4_backup_and_rejects_inventory_mutation(lattice_env, tmp_path) -> None:
    data_root = lattice_env["config"].data_root
    report = assess_data_root(data_root)
    snapshot = create_snapshot(data_root, backup_root=tmp_path / "backups", reason="verifier-v4", source_revision=report["state_digest"], schema_version=report["schema_version"], state_digest=report["state_digest"])

    checked = verifier._verify_snapshot(snapshot)
    assert checked["contract_version"] == "LatticeBackup/4.0"
    assert checked["inventory"]

    inventory = snapshot / "inventory.jsonl"
    inventory.write_bytes(inventory.read_bytes() + b" ")
    with pytest.raises(verifier.ConsolidationVerificationError, match="inventory stream disagrees"):
        verifier._verify_snapshot(snapshot)


@pytest.mark.contract
def test_independent_verifier_recognizes_the_qualified_meta_muse_profiles() -> None:
    assert verifier.PROVIDER_HARNESSES["meta"] == "muse"
    assert {profile for provider, profile in verifier.REQUIRED_PROVIDER_PROFILES if provider == "meta"} == {
        "compiled-fresh",
        "research-fresh",
    }


@pytest.mark.regression
@pytest.mark.parametrize(
    "relative_path",
    (
        "assurance/execution-consolidation/consumer-registry.json",
        "assurance/execution-consolidation/findings.json",
    ),
)
def test_committed_closure_inputs_are_exact_canonical_json(relative_path: str) -> None:
    path = REPOSITORY_ROOT / relative_path
    raw = path.read_bytes()

    assert canonical_bytes(verifier.parse_json(raw, label=relative_path)) == raw


def _blob(store, value: dict) -> dict:
    content = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    digest = store.blobs.put(content)
    return {"ref": f"lattice:blob/{digest}", "sha256": digest, "byte_count": len(content)}


def _completed_external(lattice_env, *, route=None, response: str = "exact output") -> str:
    request = ExecutionSubmissionV1(
        external_project_key="fixture.consumer",
        operation_key="verified-operation",
        actor="fixture",
        workflow_role="fixture:worker",
        prompt="Return exact output.",
        route=route or lattice_env["route"],
        output_contract=OutputContractV2(mode="text"),
        idempotency_key="fixture:verified-operation",
    )
    operation = lattice_env["lattice"].seal_execution_submission(request)
    attempt = lattice_env["store"].create_attempt(operation["invocation_id"], "verified-attempt", "fixture-boot", 9_999_999_999)
    lattice_env["store"].mark_attempt_running(attempt["attempt_id"])
    request_file = _blob(lattice_env["store"], {"driver": "codex", "model": "gpt-5.6-sol"})
    checkpoint_file = _blob(lattice_env["store"], {"request_sha256": request_file["sha256"]})
    terminal_file = _blob(lattice_env["store"], {"status": "success"})
    receipt = {
        "kind": "lattice-model-runtime-receipt",
        "journal_path": "fixture:journal",
        "request_sha256": request_file["sha256"],
        "checkpoint_sha256": checkpoint_file["sha256"],
        "terminal_sha256": terminal_file["sha256"],
        "driver": "codex",
        "model": "gpt-5.6-sol",
        "cli_version": "fixture",
        "binary": "/fixture/codex",
        "isolation": {},
        "native_session": None,
        "observed": {},
        "terminal": {"status": "success"},
        "files": {"request.json": request_file, "checkpoint.json": checkpoint_file, "terminal.json": terminal_file},
    }
    lattice_env["store"].finish_successful_attempt(attempt["attempt_id"], response=response, artifacts=[{"name": "result.txt", "media_type": "text/plain", "content": response}], journal_path="fixture:journal", runtime_receipt=receipt)
    return operation["invocation_id"]


@pytest.mark.oracle
def test_independent_verifier_reconciles_external_execution_from_base_records(lattice_env) -> None:
    _completed_external(lattice_env)

    result = verifier.verify_database(lattice_env["config"].data_root)

    assert result["external_operations"] == 1
    assert result["referenced_blobs"] >= 5


@pytest.mark.oracle
@pytest.mark.parametrize("unsupported_version", [15, 17])
def test_independent_verifier_rejects_unsupported_schema_boundaries(lattice_env, unsupported_version) -> None:
    database = lattice_env['config'].database
    with sqlite3.connect(database) as connection:
        connection.execute('DROP TRIGGER schema_migrations_no_delete')
        connection.execute('DELETE FROM schema_migrations WHERE version=16')
        if unsupported_version == 17:
            connection.execute("INSERT INTO schema_migrations VALUES(17, '20261014T000000')")
    with pytest.raises(verifier.ConsolidationVerificationError, match=f'database schema is {unsupported_version}, expected 16'):
        verifier.verify_database(lattice_env['config'].data_root)


@pytest.mark.contract
def test_pre_profile_manifest_is_reported_without_rewriting_history(lattice_env) -> None:
    route = lattice_env["route"].model_dump(mode="json")
    route.pop("provider_profile_id")
    with lattice_env["store"]._connect() as connection:
        profiles = verifier._verify_provider_profiles(connection)

    result = verifier._verify_route(route, profiles, label="legacy fixture", allow_legacy_profileless=True)

    assert result == "legacy-profileless-matched"


@pytest.mark.regression
def test_negative_control_current_manifest_cannot_drop_its_provider_profile(lattice_env) -> None:
    invocation_id = _completed_external(lattice_env)
    with lattice_env["store"]._connect() as connection:
        connection.create_function("lattice_replaying", 0, lambda: 1)
        row = connection.execute("SELECT route_json,manifest_json FROM invocations WHERE invocation_id=?", (invocation_id,)).fetchone()
        route = json.loads(row["route_json"])
        manifest = json.loads(row["manifest_json"])
        route.pop("provider_profile_id")
        manifest["execution"] = route
        manifest_without_hash = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
        manifest["manifest_sha256"] = sha256_bytes(canonical_bytes(manifest_without_hash))
        connection.execute(
            "UPDATE invocations SET route_json=?,manifest_json=?,manifest_sha256=? WHERE invocation_id=?",
            (canonical_bytes(route).decode(), canonical_bytes(manifest).decode(), manifest["manifest_sha256"], invocation_id),
        )

    with pytest.raises(verifier.ConsolidationVerificationError, match="no immutable provider profile"):
        verifier.verify_database(lattice_env["config"].data_root)


@pytest.mark.regression
def test_negative_control_successful_legacy_invocation_cannot_claim_an_unqualified_model(lattice_env) -> None:
    invocation_id = _completed_external(lattice_env)
    with lattice_env["store"]._connect() as connection:
        connection.create_function("lattice_replaying", 0, lambda: 1)
        row = connection.execute("SELECT route_json,manifest_json FROM invocations WHERE invocation_id=?", (invocation_id,)).fetchone()
        route = json.loads(row["route_json"])
        manifest = json.loads(row["manifest_json"])
        route.pop("provider_profile_id")
        route["model"] = "unqualified-legacy-model"
        manifest["contract_version"] = "LatticeContextManifest/9.0"
        manifest["execution"] = route
        manifest_without_hash = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
        manifest["manifest_sha256"] = sha256_bytes(canonical_bytes(manifest_without_hash))
        connection.execute(
            "UPDATE invocations SET route_json=?,manifest_json=?,manifest_sha256=?,created_at=? WHERE invocation_id=?",
            (canonical_bytes(route).decode(), canonical_bytes(manifest).decode(), manifest["manifest_sha256"], "20260824T000000", invocation_id),
        )

    with pytest.raises(verifier.ConsolidationVerificationError, match="used an unqualified legacy route successfully"):
        verifier.verify_database(lattice_env["config"].data_root)


@pytest.mark.regression
def test_negative_control_verifier_rejects_changed_runtime_receipt_blob(lattice_env) -> None:
    invocation_id = _completed_external(lattice_env)
    with sqlite3.connect(lattice_env["config"].database) as connection:
        receipt = json.loads(connection.execute("SELECT runtime_receipt_json FROM attempts WHERE invocation_id=?", (invocation_id,)).fetchone()[0])
    digest = receipt["files"]["terminal.json"]["sha256"]
    path = lattice_env["config"].blobs / digest[:2] / digest
    path.write_bytes(b'{"status":"failed"}\n')

    with pytest.raises(verifier.ConsolidationVerificationError, match="changed or disagrees"):
        verifier.verify_database(lattice_env["config"].data_root)


@pytest.mark.regression
def test_negative_control_verifier_rejects_external_operation_without_lattice_receipt(lattice_env) -> None:
    invocation_id = _completed_external(lattice_env)
    with lattice_env["store"]._connect() as connection:
        connection.create_function("lattice_replaying", 0, lambda: 1)
        connection.execute("UPDATE attempts SET runtime_receipt_json=? WHERE invocation_id=?", (canonical_bytes({"kind": "project-local-result"}).decode(), invocation_id))

    with pytest.raises(verifier.ConsolidationVerificationError, match="full Lattice runtime receipt"):
        verifier.verify_database(lattice_env["config"].data_root)


@pytest.mark.regression
def test_negative_control_consumer_census_rejects_direct_provider_launcher(tmp_path) -> None:
    canon = tmp_path / "sui/mission/projects/canon/dev/quaternary-exhaustion"
    survey = tmp_path / "sui/mission/typology/abduction/field-survey/research-pipeline"
    lattice = tmp_path / "axis/chariots/lattice"
    canon.mkdir(parents=True)
    survey.mkdir(parents=True)
    lattice.mkdir(parents=True)
    (lattice / "pyproject.toml").write_text('[project]\nname="lattice"\n', encoding="utf-8")
    (canon / "direct.py").write_text('import subprocess\nsubprocess.run(["claude", "-p", "prompt"])\n', encoding="utf-8")

    consumers = [
        {"consumer_id": "canon", "source_root": "sui/mission/projects/canon/dev/quaternary-exhaustion", "client_boundary": "sui/mission/projects/canon/dev/quaternary-exhaustion/direct.py", "forbidden_direct_authorities": ["provider-cli", "provider-sdk", "developer-api"]},
        {"consumer_id": "survey", "source_root": "sui/mission/typology/abduction/field-survey/research-pipeline", "client_boundary": "sui/mission/typology/abduction/field-survey/research-pipeline/client.py", "forbidden_direct_authorities": ["provider-cli", "provider-sdk", "developer-api"]},
    ]

    with pytest.raises(verifier.ConsolidationVerificationError, match="direct provider executable"):
        verifier.verify_consumer_census(tmp_path, consumers, require_retired_model_runner=False)


@pytest.mark.regression
def test_negative_control_consumer_census_rejects_legacy_gemini_launcher(tmp_path) -> None:
    path = tmp_path / "legacy_gemini.py"
    path.write_text(
        'import subprocess\nsubprocess.run(["gemini", "-p", "prompt"])\n',
        encoding="utf-8",
    )

    assert verifier._provider_launches(path) == [
        f"{path}:2: direct provider executable gemini"
    ]


@pytest.mark.regression
def test_negative_control_consumer_census_rejects_indirect_provider_launcher(tmp_path) -> None:
    lattice = tmp_path / "axis/chariots/lattice"
    lattice.mkdir(parents=True)
    (lattice / "pyproject.toml").write_text('[project]\nname="lattice"\n', encoding="utf-8")
    live = tmp_path / "sui/mission/projects/live"
    live.mkdir(parents=True)
    (live / "indirect.py").write_text(
        'import shutil, subprocess\nbinary = shutil.which("claude")\ncommand = [binary, "--print"]\nsubprocess.Popen(command)\n',
        encoding="utf-8",
    )

    with pytest.raises(verifier.ConsolidationVerificationError, match="direct provider executable claude"):
        verifier.verify_consumer_census(tmp_path, [], require_retired_model_runner=False)


@pytest.mark.regression
def test_negative_control_consumer_census_rejects_helper_mediated_provider_launcher(tmp_path) -> None:
    lattice = tmp_path / "axis/chariots/lattice"
    lattice.mkdir(parents=True)
    (lattice / "pyproject.toml").write_text('[project]\nname="lattice"\n', encoding="utf-8")
    live = tmp_path / "sui/mission/projects/live"
    live.mkdir(parents=True)
    (live / "helper.py").write_text(
        'import subprocess\ndef launch(command):\n    subprocess.run(command)\nbinary = "codex"\ncommand = [binary, "exec"]\nlaunch(command)\n',
        encoding="utf-8",
    )

    with pytest.raises(verifier.ConsolidationVerificationError, match="helper-mediated provider executable codex"):
        verifier.verify_consumer_census(tmp_path, [], require_retired_model_runner=False)


@pytest.mark.regression
def test_negative_control_consumer_census_rejects_starred_async_provider_command(tmp_path) -> None:
    path = tmp_path / "starred.py"
    path.write_text(
        'import asyncio\nCLAUDE = "/opt/homebrew/bin/claude"\nasync def launch():\n    command = [CLAUDE, "-p"]\n    await asyncio.create_subprocess_exec(*command)\n',
        encoding="utf-8",
    )

    assert verifier._provider_launches(path) == [
        f"{path}:5: direct provider executable claude"
    ]


@pytest.mark.regression
def test_consumer_census_does_not_confuse_asyncio_run_with_subprocess_run(tmp_path) -> None:
    path = tmp_path / "asyncio_only.py"
    path.write_text(
        'import asyncio\nCLAUDE = "claude"\ncommand = [CLAUDE, "-p"]\nasync def harmless():\n    return command\nasyncio.run(harmless())\n',
        encoding="utf-8",
    )

    assert verifier._provider_launches(path) == []


@pytest.mark.regression
def test_negative_control_consumer_census_rejects_class_method_provider_command(tmp_path) -> None:
    path = tmp_path / "method.py"
    path.write_text(
        'import shutil, subprocess\nclass Launcher:\n    def run(self):\n        binary = shutil.which("codex")\n        subprocess.Popen([binary, "exec"])\n',
        encoding="utf-8",
    )

    assert verifier._provider_launches(path) == [
        f"{path}:5: direct provider executable codex"
    ]


@pytest.mark.regression
def test_negative_control_consumer_census_rejects_provider_command_builder(tmp_path) -> None:
    path = tmp_path / "builder.py"
    path.write_text(
        'import subprocess\ndef command():\n    return ["claude", "-p"]\ndef launch():\n    argv = command()\n    subprocess.run(argv)\n',
        encoding="utf-8",
    )

    assert verifier._provider_launches(path) == [
        f"{path}:6: direct provider executable claude"
    ]


@pytest.mark.regression
def test_consumer_census_prunes_ephemera_and_dependency_caches(tmp_path) -> None:
    lattice = tmp_path / "axis/chariots/lattice"
    lattice.mkdir(parents=True)
    (lattice / "pyproject.toml").write_text('[project]\nname="lattice"\n', encoding="utf-8")
    live = tmp_path / "sui/mission/projects/live"
    live.mkdir(parents=True)
    (live / "safe.py").write_text("value = 1\n", encoding="utf-8")
    ignored = tmp_path / "sui/flux/ephemera/uv-cache/site-packages/package"
    ignored.mkdir(parents=True)
    (ignored / "broken.py").write_text('subprocess.run(["claude"\n', encoding="utf-8")

    result = verifier.verify_consumer_census(tmp_path, [], require_retired_model_runner=False)

    assert result["repository_source_files_scanned"] == 1
    assert result["violations"] == 0


@pytest.mark.regression
def test_consumer_source_inventory_prunes_before_descent(tmp_path, monkeypatch) -> None:
    (tmp_path / "client.py").write_text("value = 1\n", encoding="utf-8")
    cache = tmp_path / "runtime/provider/journals"
    cache.mkdir(parents=True)
    (cache / "private.bin").write_bytes(b"ignored")

    def reject_rglob(*_args, **_kwargs):
        raise AssertionError("source inventory must not descend with Path.rglob")

    monkeypatch.setattr(Path, "rglob", reject_rglob)

    assert verifier._source_inventory(tmp_path) == [
        {"path": "client.py", "sha256": verifier.sha256_file(tmp_path / "client.py"), "byte_count": 10}
    ]


@pytest.mark.regression
def test_false_mutation_reintroduced_model_runner_tree_is_refused_by_final_closure_and_tolerated_only_while_inert(tmp_path) -> None:
    # Finding 627260b7: the retired sibling execution tree. The final closure
    # path (no --retirement-readiness) must refuse the tree's mere existence;
    # the readiness path tolerates it only while nothing references it.
    lattice = tmp_path / "axis/chariots/lattice"
    lattice.mkdir(parents=True)
    (lattice / "pyproject.toml").write_text('[project]\nname="lattice"\n', encoding="utf-8")
    live = tmp_path / "sui/mission/projects/live"
    live.mkdir(parents=True)
    (live / "safe.py").write_text("value = 1\n", encoding="utf-8")

    retired = verifier.verify_consumer_census(tmp_path, [], require_retired_model_runner=True)
    assert retired["model_runner_retired"] is True
    assert retired["violations"] == 0

    runner = tmp_path / "axis/tools/model-runner/axis_model_runner"
    runner.mkdir(parents=True)
    (runner / "__init__.py").write_text('import subprocess\n\ndef launch():\n    subprocess.run(["claude", "-p", "prompt"])\n', encoding="utf-8")

    with pytest.raises(verifier.ConsolidationVerificationError, match=r"retired sibling execution tree remains: .*axis/tools/model-runner"):
        verifier.verify_consumer_census(tmp_path, [], require_retired_model_runner=True)

    tolerated = verifier.verify_consumer_census(tmp_path, [], require_retired_model_runner=False)
    assert tolerated["model_runner_retired"] is False
    assert tolerated["violations"] == 0

    (live / "uses_runner.py").write_text("import axis_model_runner\n", encoding="utf-8")
    with pytest.raises(verifier.ConsolidationVerificationError, match=r"retired sibling execution package remains active: .*uses_runner\.py"):
        verifier.verify_consumer_census(tmp_path, [], require_retired_model_runner=False)


@pytest.mark.regression
def test_negative_control_forbidden_credential_canary_is_detected(tmp_path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    root.joinpath("receipt").write_bytes(b"prefix-planted-secret-suffix")

    with pytest.raises(verifier.ConsolidationVerificationError, match="credential canary"):
        verifier.scan_forbidden_bytes((root,), (b"planted-secret",))


@pytest.mark.regression
def test_negative_control_closure_requires_every_evidence_plane(lattice_env, tmp_path) -> None:
    bundle = {
        "generated_object": generated_object(
            kind="lattice-execution-closure-bundle",
            schema_name="lattice-execution-closure-bundle",
            subject="lattice:execution-consolidation",
            sources=[{"ref": "fixture:missing-evidence", "revision": "fixture", "sha256": None}],
        ),
        "contract_version": "LatticeExecutionClosureBundle/1.0",
        "target_version": "0.3.1",
        "evidence": {},
    }
    path = tmp_path / "bundle.json"
    path.write_bytes(canonical_bytes(bundle))

    with pytest.raises(verifier.ConsolidationVerificationError, match="omits or adds evidence"):
        verifier.verify(bundle_path=path, data_root=lattice_env["config"].data_root, axisvi_root=tmp_path, forbidden_files=[], rerun_suites=False, rerun_doctor=False)


@pytest.mark.regression
def test_negative_control_zero_historical_execution_set_cannot_pass(lattice_env, tmp_path) -> None:
    value = {
        "generated_object": generated_object(
            kind="lattice-historical-execution-set",
            schema_name="lattice-historical-execution-set",
            subject="lattice:historical-execution:field-survey",
            role="snapshot",
            sources=[{"ref": "fixture:history", "revision": "empty", "sha256": None}],
        ),
        "contract_version": "HistoricalExecutionSet/1.0",
        "external_project_key": verifier.HISTORICAL_EXECUTION_KEY,
        "source_root": "history",
        "run_root": ".",
        "runs": [],
    }
    (tmp_path / "history").mkdir()

    with pytest.raises(verifier.ConsolidationVerificationError, match="is empty"):
        verifier.verify_historical_execution_set(value, data_root=lattice_env["config"].data_root, axisvi_root=tmp_path)


@pytest.mark.regression
def test_negative_control_open_s2_prevents_final_attestation() -> None:
    value = {
        "generated_object": generated_object(
            kind="lattice-execution-consolidation-findings",
            schema_name="lattice-execution-consolidation-findings",
            subject="lattice:execution-consolidation-findings",
            sources=[{"ref": "fixture:findings", "revision": "one", "sha256": None}],
        ),
        "contract_version": "LatticeExecutionConsolidationFindings/2.0",
        "findings": [{"finding_id": "fixture-s2", "severity": "S2", "status": "implemented-pending-verification"}],
    }

    with pytest.raises(verifier.ConsolidationVerificationError, match="remain open"):
        verifier.verify_findings(value, allow_pending=False)
    assert verifier.verify_findings(value, allow_pending=True)["blockers"] == 1


@pytest.mark.regression
def test_negative_control_historical_import_must_still_resolve_to_exact_source_bytes(lattice_env, tmp_path) -> None:
    history_root = tmp_path / "history"
    run = history_root / "run-fixture"
    run.mkdir(parents=True)
    source = run / "terminal.json"
    source.write_text('{"status":"success"}\n', encoding="utf-8")
    files = [{"relative_path": "terminal.json", "content_sha256": verifier.sha256_file(source), "byte_count": source.stat().st_size}]
    request = {
        "contract_version": "HistoricalExecutionImport/1.0",
        "external_project_key": "fixture.history",
        "operation_key": "historical:run-fixture",
        "source_ref": "run-fixture",
        "source_census_sha256": sha256_bytes(canonical_bytes(files)),
        "files": files,
        "provider": "openai",
        "model": "historical-codex",
        "original_invocation_id": "fixture-run",
        "terminal_state": "success",
    }
    lattice_env["store"].import_historical_execution(request, sha256_bytes(canonical_bytes(request)))

    verified = verifier.verify_database(lattice_env["config"].data_root, historical_roots={"fixture.history": history_root})
    assert verified["historical_sources_verified"] == 1

    source.write_text('{"status":"rewritten"}\n', encoding="utf-8")
    with pytest.raises(verifier.ConsolidationVerificationError, match="no longer match"):
        verifier.verify_database(lattice_env["config"].data_root, historical_roots={"fixture.history": history_root})


@pytest.mark.contract
def test_superseded_historical_namespace_preserves_claim_without_claiming_live_source_equality(lattice_env, tmp_path) -> None:
    history_root = tmp_path / "history"
    run = history_root / "run-fixture"
    run.mkdir(parents=True)
    source = run / "terminal.json"
    source.write_text('{"status":"success"}\n', encoding="utf-8")
    files = [{"relative_path": "terminal.json", "content_sha256": verifier.sha256_file(source), "byte_count": source.stat().st_size}]
    request = {
        "contract_version": "HistoricalExecutionImport/1.0",
        "external_project_key": verifier.LEGACY_HISTORICAL_EXECUTION_KEY,
        "operation_key": "historical:run-fixture",
        "source_ref": "run-fixture",
        "source_census_sha256": sha256_bytes(canonical_bytes(files)),
        "files": files,
        "provider": "openai",
        "model": "historical-codex",
        "original_invocation_id": "fixture-run",
        "terminal_state": "success",
    }
    lattice_env["store"].import_historical_execution(request, sha256_bytes(canonical_bytes(request)))
    source.write_text('{"status":"changed-after-snapshot"}\n', encoding="utf-8")

    result = verifier.verify_database(lattice_env["config"].data_root, historical_roots={})

    assert result["legacy_historical_sources_unavailable"] == 1
    assert result["historical_sources_verified"] == 0


@pytest.mark.regression
def test_negative_control_consumer_canary_cannot_point_at_a_different_invocation(lattice_env, tmp_path) -> None:
    invocation_id = _completed_external(lattice_env)
    report = lattice_env["config"].data_root / "acceptance" / "consumer.json"
    report.parent.mkdir(parents=True)
    report.write_bytes(b"consumer receipt")
    with sqlite3.connect(lattice_env["config"].database) as connection:
        manifest_sha256 = connection.execute("SELECT manifest_sha256 FROM invocations WHERE invocation_id=?", (invocation_id,)).fetchone()[0]
    value = {
        "generated_object": generated_object(kind="lattice-qualification-receipt", schema_name="lattice-qualification-receipt", subject="fixture", role="snapshot", sources=[{"ref": "fixture:qualification", "revision": "one", "sha256": None}]),
        "contract_version": "LatticeQualificationReceipt/1.0",
        "runtime_artifact_id": "artifact:00000000-0000-4000-8000-000000000000",
        "suites": [{"suite_id": suite_id, "truth_kinds": ["contract"], "command": [["fixture"]], "cwd": str(tmp_path), "exit_code": 0, "stdout_sha256": "1" * 64, "stderr_sha256": "2" * 64} for suite_id in verifier.REQUIRED_SUITES],
        "consumer_source_inventories": {"fixture.consumer": []},
        "consumer_canaries": [{"consumer_id": "fixture.consumer", "external_project_key": "fixture.consumer", "invocation_id": "invocation_wrong", "manifest_sha256": manifest_sha256, "report_path": str(report), "report_sha256": verifier.sha256_file(report)}],
        "provider_canaries": [{"provider": provider, "profile": profile, "state": "succeeded", "invocation_id": invocation_id, "manifest_sha256": manifest_sha256} for provider, profile in verifier.REQUIRED_PROVIDER_PROFILES],
        "interruption": {"pre_restart_state": "running", "post_restart_state": "orphaned", "continuation_invocation_id": invocation_id},
        "credential_canary_sha256s": [],
        "kimi": {"state": "unavailable", "reason": "unqualified"},
    }

    with pytest.raises(verifier.ConsolidationVerificationError, match="absent, nonterminal, or inexact"):
        verifier.verify_qualification_receipt(value, runtime={"runtime_artifact_id": value["runtime_artifact_id"]}, consumer_inventories={"fixture.consumer": []}, data_root=lattice_env["config"].data_root)


@pytest.mark.oracle
def test_octopus_consumer_canary_is_reconstructed_from_base_records(lattice_env) -> None:
    sentinel = "OCTOPUS-LATTICE-CANARY-fixture"
    route = ProviderRoute(provider="anthropic", model="sonnet", harness="claude", effort="high", execution_profile="compiled-fresh")
    invocation_id = _completed_external(lattice_env, route=route, response=sentinel)
    with sqlite3.connect(lattice_env["config"].database) as connection:
        connection.row_factory = sqlite3.Row
        invocation = connection.execute("SELECT manifest_sha256,route_json FROM invocations WHERE invocation_id=?", (invocation_id,)).fetchone()
        attempt = connection.execute("SELECT attempt_id,output_sha256,runtime_receipt_json FROM attempts WHERE invocation_id=?", (invocation_id,)).fetchone()
        artifact = connection.execute("SELECT artifact_id FROM artifacts WHERE content_sha256=?", (attempt["output_sha256"],)).fetchone()
        runtime_receipt = json.loads(attempt["runtime_receipt_json"])
        stored_route = json.loads(invocation["route_json"])
        provider_attestation = {
            "profile_id": stored_route.get("provider_profile_id"),
            "provider": stored_route["provider"],
            "harness": stored_route["harness"],
            "requested_model": stored_route["model"],
            "observed": runtime_receipt.get("observed"),
        }
        report = {
            "generated_object": generated_object(kind="octopus-lattice-consumer-canary", schema_name="octopus-lattice-consumer-canary", subject="fixture", role="snapshot", sources=[{"ref": f"lattice:invocation:{invocation_id}", "revision": invocation_id, "sha256": invocation["manifest_sha256"]}]),
            "contract_version": "OctopusLatticeCanary/1.0",
            "consumer_id": "octopus.model-execution",
            "external_project_key": "fixture.consumer",
            "operation_key": "verified-operation",
            "invocation_id": invocation_id,
            "attempt_id": attempt["attempt_id"],
            "manifest_sha256": invocation["manifest_sha256"],
            "output_sha256": attempt["output_sha256"],
            "output_artifact_id": artifact["artifact_id"],
            "sentinel_sha256": verifier.sha256_bytes(sentinel.encode()),
            "provider_attestation_sha256": verifier.sha256_bytes(verifier.canonical_bytes(provider_attestation)),
            "journal_receipt_sha256": verifier.sha256_bytes(verifier.canonical_bytes(runtime_receipt)),
        }
        report_path = lattice_env["config"].data_root / "acceptance" / "octopus.json"
        report_path.parent.mkdir()
        report_path.write_bytes(canonical_bytes(report))
        canary = {field: report[field] for field in ("consumer_id", "external_project_key", "invocation_id", "manifest_sha256")}
        verifier._verify_octopus_consumer_canary(connection, report_path, canary)


@pytest.mark.regression
def test_negative_control_octopus_consumer_canary_rejects_nonexact_sentinel(lattice_env) -> None:
    route = ProviderRoute(provider="anthropic", model="sonnet", harness="claude", effort="high", execution_profile="compiled-fresh")
    invocation_id = _completed_external(lattice_env, route=route, response="sentinel with extra bytes")
    with sqlite3.connect(lattice_env["config"].database) as connection:
        connection.row_factory = sqlite3.Row
        invocation = connection.execute("SELECT manifest_sha256 FROM invocations WHERE invocation_id=?", (invocation_id,)).fetchone()
        attempt = connection.execute("SELECT attempt_id,output_sha256,runtime_receipt_json FROM attempts WHERE invocation_id=?", (invocation_id,)).fetchone()
        artifact = connection.execute("SELECT artifact_id FROM artifacts WHERE content_sha256=?", (attempt["output_sha256"],)).fetchone()
        report = {
            "generated_object": generated_object(kind="octopus-lattice-consumer-canary", schema_name="octopus-lattice-consumer-canary", subject="fixture", role="snapshot", sources=[{"ref": f"lattice:invocation:{invocation_id}", "revision": invocation_id, "sha256": invocation["manifest_sha256"]}]),
            "contract_version": "OctopusLatticeCanary/1.0", "consumer_id": "octopus.model-execution", "external_project_key": "fixture.consumer", "operation_key": "verified-operation", "invocation_id": invocation_id, "attempt_id": attempt["attempt_id"], "manifest_sha256": invocation["manifest_sha256"], "output_sha256": attempt["output_sha256"], "output_artifact_id": artifact["artifact_id"], "sentinel_sha256": "0" * 64, "provider_attestation_sha256": "1" * 64, "journal_receipt_sha256": "2" * 64,
        }
        report_path = lattice_env["config"].data_root / "acceptance" / "octopus-bad.json"
        report_path.parent.mkdir()
        report_path.write_bytes(canonical_bytes(report))
        canary = {field: report[field] for field in ("consumer_id", "external_project_key", "invocation_id", "manifest_sha256")}
        with pytest.raises(verifier.ConsolidationVerificationError, match="exact sentinel"):
            verifier._verify_octopus_consumer_canary(connection, report_path, canary)


QUALIFICATION_PROJECT_KEY = "lattice.release-qualification"
WORKSPACE_SESSION = "11111111-1111-4111-8111-111111111111"
FORK_SESSION = "22222222-2222-4222-8222-222222222222"


def _anthropic_canary(lattice_env, *, profile: str, parent_id: str | None, workspace: Path, session_id: str | None, operation_key: str) -> dict:
    """Seal one Anthropic qualification canary through the real store and finish it with its marker output.

    The route carries exactly the qualifier's shape for that profile; the store adds the provider profile,
    resolves the native session from the parent's receipt, and enforces its own session-head guard. The
    returned dict is the verifier-shaped provider canary receipt, read back from base records.
    """

    native = profile.startswith("native-continuation:")
    workspace_bound = native or profile == "workspace-agent"
    route = ProviderRoute(
        provider="anthropic",
        model="opus",
        harness="claude",
        effort="high",
        execution_profile="native-continuation" if native else "compiled-fresh" if profile == "portable-fallback" else profile,
        session_policy="visible_native_continuation" if native else "fresh",
        continuation_mode=profile.split(":", 1)[1] if native else None,
        native_parent_invocation_id=parent_id if native else None,
        workspace=str(workspace) if workspace_bound else None,
        sandbox="provider-permissions" if workspace_bound else "read-only",
    )
    marker = "QUAL-ANTHROPIC-" + {"workspace-agent": "WORKSPACE", "portable-fallback": "PORTABLE", "native-continuation:resume": "RESUME", "native-continuation:fork": "FORK"}.get(profile, profile.upper())
    request = ExecutionSubmissionV1(
        external_project_key=QUALIFICATION_PROJECT_KEY,
        operation_key=operation_key,
        actor="lattice-release-qualification",
        prompt=f"Reply in one sentence containing exact marker {marker}. This is synthetic qualification evidence.",
        parent_invocation_ids=[parent_id] if parent_id is not None else [],
        route=route,
        output_contract=OutputContractV2(mode="text"),
        idempotency_key=operation_key,
    )
    operation = lattice_env["lattice"].seal_execution_submission(request)
    invocation_id = operation["invocation_id"]
    store = lattice_env["store"]
    attempt = store.create_attempt(invocation_id, "attempt-" + operation_key.replace(":", "-"), "fixture-boot", 9_999_999_999)
    store.mark_attempt_running(attempt["attempt_id"])
    request_file = _blob(store, {"driver": "claude", "model": "opus", "operation_key": operation_key})
    checkpoint_file = _blob(store, {"request_sha256": request_file["sha256"]})
    terminal_file = _blob(store, {"status": "success"})
    receipt = {
        "kind": "lattice-model-runtime-receipt",
        "journal_path": "fixture:journal",
        "request_sha256": request_file["sha256"],
        "checkpoint_sha256": checkpoint_file["sha256"],
        "terminal_sha256": terminal_file["sha256"],
        "driver": "claude",
        "model": "opus",
        "cli_version": "fixture",
        "binary": "/fixture/claude",
        "isolation": {},
        "native_session": {"id": session_id} if session_id is not None else None,
        "observed": {},
        "terminal": {"status": "success"},
        "files": {"request.json": request_file, "checkpoint.json": checkpoint_file, "terminal.json": terminal_file},
    }
    response = f"Synthetic evidence containing {marker}."
    store.finish_successful_attempt(attempt["attempt_id"], response=response, artifacts=[{"name": "result.txt", "media_type": "text/plain", "content": response}], journal_path="fixture:journal", runtime_receipt=receipt)
    with sqlite3.connect(lattice_env["config"].database) as connection:
        connection.row_factory = sqlite3.Row
        invocation = connection.execute("SELECT manifest_sha256,route_json FROM invocations WHERE invocation_id=?", (invocation_id,)).fetchone()
        stored = connection.execute("SELECT output_sha256,runtime_receipt_json FROM attempts WHERE invocation_id=? AND state='succeeded'", (invocation_id,)).fetchone()
    return {
        "provider": "anthropic",
        "profile": profile,
        "state": "succeeded",
        "invocation_id": invocation_id,
        "manifest_sha256": invocation["manifest_sha256"],
        "output_sha256": stored["output_sha256"],
        "provider_profile_id": json.loads(invocation["route_json"]).get("provider_profile_id"),
        "runtime_receipt_sha256": sha256_bytes(canonical_bytes(json.loads(stored["runtime_receipt_json"]))),
    }


def _canary_placeholders() -> list[dict]:
    """Every non-Anthropic required profile, xai:compiled-fresh first, pointing at an invocation that does not exist.

    ``verify_qualification_receipt`` checks canaries in list order and stops at the first refusal, so a
    receipt that ends in these placeholders is accepted exactly as far as its real Anthropic chain and
    then refuses with a distinct, recognisable message for xai:compiled-fresh.
    """

    others = sorted(item for item in verifier.REQUIRED_PROVIDER_PROFILES if item[0] != "anthropic")
    others.remove(("xai", "compiled-fresh"))
    return [
        {"provider": provider, "profile": profile, "state": "succeeded", "invocation_id": "invocation_absent", "manifest_sha256": "0" * 64, "output_sha256": "0" * 64, "provider_profile_id": "provider-profile:fixture", "runtime_receipt_sha256": "0" * 64}
        for provider, profile in [("xai", "compiled-fresh"), *others]
    ]


def _qualification_receipt(tmp_path: Path, canaries: list[dict]) -> dict:
    """A receipt whose only live content is its provider canaries; the interruption block is never reached."""

    return {
        "generated_object": generated_object(kind="lattice-qualification-receipt", schema_name="lattice-qualification-receipt", subject="fixture", role="snapshot", sources=[{"ref": "fixture:qualification", "revision": "one", "sha256": None}]),
        "contract_version": "LatticeQualificationReceipt/1.0",
        "runtime_artifact_id": "artifact:00000000-0000-4000-8000-000000000000",
        "suites": [{"suite_id": suite_id, "truth_kinds": ["contract"], "command": [["fixture"]], "cwd": str(tmp_path), "exit_code": 0, "stdout_sha256": "1" * 64, "stderr_sha256": "2" * 64} for suite_id in verifier.REQUIRED_SUITES],
        "consumer_source_inventories": {},
        "consumer_canaries": [],
        "provider_canaries": canaries,
        "interruption": {},
        "credential_canary_sha256s": [],
        "kimi": {"state": "unavailable", "reason": "unqualified"},
    }


def _verify_canaries(lattice_env, receipt: dict) -> dict:
    return verifier.verify_qualification_receipt(receipt, runtime={"runtime_artifact_id": receipt["runtime_artifact_id"]}, consumer_inventories={}, data_root=lattice_env["config"].data_root)


@pytest.mark.oracle
def test_native_fork_canary_is_accepted_from_the_resumed_session_head(lattice_env, tmp_path) -> None:
    # The corrected qualifier chain: fresh -> workspace agent -> resume (same session, now the head)
    # -> fork from the resume (new session); portable fallback from the workspace agent. Every row is
    # a real store execution; the store itself admitted each native parent as its session head.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fresh = _anthropic_canary(lattice_env, profile="compiled-fresh", parent_id=None, workspace=workspace, session_id=None, operation_key="qualification:fixture:anthropic:compiled-fresh:a1")
    agent = _anthropic_canary(lattice_env, profile="workspace-agent", parent_id=fresh["invocation_id"], workspace=workspace, session_id=WORKSPACE_SESSION, operation_key="qualification:fixture:anthropic:workspace-agent:a1")
    resume = _anthropic_canary(lattice_env, profile="native-continuation:resume", parent_id=agent["invocation_id"], workspace=workspace, session_id=WORKSPACE_SESSION, operation_key="qualification:fixture:anthropic:native-continuation:resume:a1")
    fork = _anthropic_canary(lattice_env, profile="native-continuation:fork", parent_id=resume["invocation_id"], workspace=workspace, session_id=FORK_SESSION, operation_key="qualification:fixture:anthropic:native-continuation:fork:a2")
    portable = _anthropic_canary(lattice_env, profile="portable-fallback", parent_id=agent["invocation_id"], workspace=workspace, session_id=None, operation_key="qualification:fixture:anthropic:portable-fallback:a1")
    receipt = _qualification_receipt(tmp_path, [fresh, agent, resume, fork, portable, *_canary_placeholders()])

    # Reaching the xai placeholder's distinct refusal proves every Anthropic item, the fork's ancestry
    # and native claims included, was accepted. (Falsifier: a verifier expecting the fork to continue the
    # workspace agent refuses here with "ancestry is wrong: anthropic:native-continuation:fork".)
    with pytest.raises(verifier.ConsolidationVerificationError, match=r"does not resolve to exact successful execution: xai:compiled-fresh"):
        _verify_canaries(lattice_env, receipt)


@pytest.mark.regression
def test_an_admission_refused_fork_reservation_is_retained_unsealed_and_blocks_database_closure(lattice_env, tmp_path) -> None:
    # The live failure, reproduced against the real store: after the resume became the session head, a
    # fork naming the workspace agent is refused at admission, and the reservation made before validation
    # (operations.seal_execution_submission) is left behind with no invocation. verify_database currently
    # refuses every such row as unbound, so the retained live fork:a1 reservation is a closure obligation
    # of its own, independent of the qualification receipt. This test documents that obligation; it does
    # not decide whether the store or the verifier is the right place to resolve it.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fresh = _anthropic_canary(lattice_env, profile="compiled-fresh", parent_id=None, workspace=workspace, session_id=None, operation_key="qualification:fixture:anthropic:compiled-fresh:a1")
    agent = _anthropic_canary(lattice_env, profile="workspace-agent", parent_id=fresh["invocation_id"], workspace=workspace, session_id=WORKSPACE_SESSION, operation_key="qualification:fixture:anthropic:workspace-agent:a1")
    _anthropic_canary(lattice_env, profile="native-continuation:resume", parent_id=agent["invocation_id"], workspace=workspace, session_id=WORKSPACE_SESSION, operation_key="qualification:fixture:anthropic:native-continuation:resume:a1")
    stale_key = "qualification:fixture:anthropic:native-continuation:fork:a1"

    with pytest.raises(Exception, match="no longer the provider session head"):
        _anthropic_canary(lattice_env, profile="native-continuation:fork", parent_id=agent["invocation_id"], workspace=workspace, session_id=FORK_SESSION, operation_key=stale_key)

    with sqlite3.connect(lattice_env["config"].database) as connection:
        residue = connection.execute("SELECT state,invocation_id FROM external_operations WHERE external_project_key=? AND operation_key=?", (QUALIFICATION_PROJECT_KEY, stale_key)).fetchone()
    assert residue == ("reserved", None)
    with pytest.raises(verifier.ConsolidationVerificationError, match=re.escape(f"{QUALIFICATION_PROJECT_KEY}:{stale_key} is not bound to exactly one invocation")):
        verifier.verify_database(lattice_env["config"].data_root)


@pytest.mark.regression
def test_negative_control_fork_from_the_stale_workspace_head_is_refused_as_wrong_ancestry(lattice_env, tmp_path) -> None:
    # The store admits this order: the fork is sealed while the workspace agent is still its session
    # head, and the resume follows from the same parent. Every row is a genuine succeeded execution with
    # the right route, profile, native claim and marker, so the only thing the verifier can refuse is the
    # fork's ancestry. (Falsifier: a verifier expecting the fork to continue the workspace agent accepts
    # this chain and stops at the xai placeholder instead.)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fresh = _anthropic_canary(lattice_env, profile="compiled-fresh", parent_id=None, workspace=workspace, session_id=None, operation_key="qualification:fixture:anthropic:compiled-fresh:a1")
    agent = _anthropic_canary(lattice_env, profile="workspace-agent", parent_id=fresh["invocation_id"], workspace=workspace, session_id=WORKSPACE_SESSION, operation_key="qualification:fixture:anthropic:workspace-agent:a1")
    stale_fork = _anthropic_canary(lattice_env, profile="native-continuation:fork", parent_id=agent["invocation_id"], workspace=workspace, session_id=FORK_SESSION, operation_key="qualification:fixture:anthropic:native-continuation:fork:a1")
    resume = _anthropic_canary(lattice_env, profile="native-continuation:resume", parent_id=agent["invocation_id"], workspace=workspace, session_id=WORKSPACE_SESSION, operation_key="qualification:fixture:anthropic:native-continuation:resume:a1")
    portable = _anthropic_canary(lattice_env, profile="portable-fallback", parent_id=agent["invocation_id"], workspace=workspace, session_id=None, operation_key="qualification:fixture:anthropic:portable-fallback:a1")
    with sqlite3.connect(lattice_env["config"].database) as connection:
        parents = connection.execute("SELECT d.parents_json FROM invocations i JOIN drafts d USING(draft_id) WHERE i.invocation_id=?", (stale_fork["invocation_id"],)).fetchone()[0]
    assert json.loads(parents) == [agent["invocation_id"]]

    receipt = _qualification_receipt(tmp_path, [fresh, agent, resume, stale_fork, portable, *_canary_placeholders()])

    with pytest.raises(verifier.ConsolidationVerificationError, match=r"provider canary ancestry is wrong: anthropic:native-continuation:fork"):
        _verify_canaries(lattice_env, receipt)


@pytest.mark.regression
def test_false_mutation_copied_transcript_bytes_in_slate_are_refused_against_a_live_slice_control(lattice_env) -> None:
    # Finding e03e72a6: the browser receipt's Slate claim must be a live
    # concern-backed slice with zero copied transcript bytes. The journey
    # fixture and receipt builder are the installed-browser producer's own;
    # the independent verifier is the oracle under test.
    browser_tests = _load_source_module(REPOSITORY_ROOT / "tests" / "test_browser_qualification.py", "browser_qualification_fixtures")
    driver, _identities = browser_tests._seed_browser_journey(lattice_env, "20261021T000000-0badc0de")
    data_root = lattice_env["config"].data_root
    runtime = {"runtime_artifact_id": "artifact:installed-runtime"}
    receipt = browser_tests.qualifier.build_receipt(data_root=data_root, driver=driver, runtime_artifact_id=runtime["runtime_artifact_id"])

    control = verifier.verify_browser_receipt(receipt, data_root=data_root, runtime=runtime)
    assert control == {"actions": 10, "invocations": 8, "slate_slice": receipt["slate"]["slice_id"]}

    transcript = verifier._blob_content(data_root, receipt["excerpt"]["source_sha256"], label="browser root output")
    copied = copy.deepcopy(receipt)
    copied["slate"]["copied_transcript_bytes"] = len(transcript)
    with pytest.raises(verifier.ConsolidationVerificationError, match="not a live concern-backed slice"):
        verifier.verify_browser_receipt(copied, data_root=data_root, runtime=runtime)

    detached = copy.deepcopy(receipt)
    detached["slate"]["external_ref"] = "slate:copied-transcript"
    with pytest.raises(verifier.ConsolidationVerificationError, match="not a live concern-backed slice"):
        verifier.verify_browser_receipt(detached, data_root=data_root, runtime=runtime)


@pytest.mark.contract
def test_producer_and_verifier_share_fixed_suite_commands(tmp_path) -> None:
    from ops import qualify_release

    axisvi_root = tmp_path / "axisvi"
    lattice = axisvi_root / "axis" / "chariots" / "lattice"
    lattice.mkdir(parents=True)
    (lattice / ".venv" / "bin").mkdir(parents=True)
    (lattice / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    producer = qualify_release._suite_commands(axisvi_root)
    independent = verifier._fixed_suite_commands(axisvi_root)
    assert {suite_id: commands for suite_id, (_cwd, commands, _kinds) in producer.items()} == {
        suite_id: commands for suite_id, (_cwd, commands) in independent.items()
    }
