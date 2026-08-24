"""Truth-typed assurance for Lattice service identity, snapshots, and recovery."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from axis_model_runner import bind_request, create_run_journal
from fastapi.testclient import TestClient

from lattice import cli
from lattice import durability
from lattice import __version__
from lattice.canonical import canonical_bytes, generated_object
from lattice.config import LatticeConfig, user_data_root
from lattice.contracts import DraftCreate, ProjectCreate
from lattice.diagnostics import assess_backup, assess_data_root
from lattice.outport import OutportService, outport_executable, resolve_lattice_service
from lattice.runtime_attestation import mapped_inventory, tree_inventory
from lattice.server import create_app
from lattice import snapshot as snapshot_runtime
from lattice.snapshot import create_snapshot, restore_snapshot, verify_snapshot
from ops import install as deployment

from conftest import completed_invocation


def outport_payload(*, port: int = 9010, url: str = "https://lattice.axisvi.test") -> bytes:
    return json.dumps({
        "ok": True,
        "data": {
            "project": "axisvi",
            "instance": "main",
            "services": {
                "lattice": {"port": port, "hostname": "lattice.axisvi.test", "url": url, "up": True},
                "panels": {"port": 9000, "hostname": "slate.axisvi.test", "url": "https://slate.axisvi.test", "up": True},
            },
        },
    }).encode()


@pytest.mark.contract
def test_release_identity_has_one_package_owned_source() -> None:
    assert __version__ == "0.2.0"
    assert cli._version() == __version__
    assert deployment.release_version() == __version__


@pytest.mark.contract
def test_cli_exposes_the_release_identity(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.parser().parse_args(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out == "lattice 0.2.0\n"


@pytest.mark.regression
def test_darwin_strict_sync_flushes_kernel_and_drive(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(durability.sys, "platform", "darwin")
    monkeypatch.setattr(durability.os, "fsync", lambda descriptor: calls.append(("fsync", descriptor)))
    monkeypatch.setattr(durability.fcntl, "fcntl", lambda descriptor, operation: calls.append(("fcntl", descriptor, operation)))

    durability.sync_file(41)

    assert calls == [("fsync", 41), ("fcntl", 41, durability.fcntl.F_FULLFSYNC)]


@pytest.mark.regression
def test_darwin_ledger_connections_enable_full_drive_sync(lattice_env) -> None:
    if durability.sys.platform != "darwin":
        pytest.skip("SQLite fullfsync is a macOS durability contract")
    with lattice_env["store"]._connect() as connection:
        assert connection.execute("PRAGMA fullfsync").fetchone()[0] == 1
        assert connection.execute("PRAGMA checkpoint_fullfsync").fetchone()[0] == 1


@pytest.mark.regression
def test_store_closes_every_lexically_owned_sqlite_connection(monkeypatch, lattice_env) -> None:
    store = lattice_env["store"]
    opened = []
    real_open = store._open_connection

    def tracked_open():
        connection = real_open()
        opened.append(connection)
        return connection

    monkeypatch.setattr(store, "_open_connection", tracked_open)
    for _ in range(12):
        store.get_project(lattice_env["project"]["project_id"])

    assert len(opened) >= 12
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")


@pytest.mark.contract
def test_outport_is_the_only_lattice_service_identity(tmp_path) -> None:
    service = resolve_lattice_service(tmp_path, runner=lambda _root: outport_payload())

    assert service.port == 9010
    assert service.url == "https://lattice.axisvi.test"
    assert service.frame_origins == ("https://slate.axisvi.test",)
    assert service.loopback_url == "http://127.0.0.1:9010"


@pytest.mark.contract
def test_lattice_remains_an_independent_ignored_nested_repository() -> None:
    root = deployment.AXISVI_ROOT
    assert not (root / ".gitmodules").exists() or "axis/chariots/lattice" not in (root / ".gitmodules").read_text(encoding="utf-8")
    tracked = subprocess.run(["git", "ls-files", "-s", "--", "axis/chariots/lattice"], cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    ignored = subprocess.run(["git", "check-ignore", "-q", "axis/chariots/lattice/README.md"], cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

    assert tracked.stdout == b""
    assert ignored.returncode == 0
    assert (root / "axis" / "chariots" / "lattice" / ".git").is_dir()


@pytest.mark.regression
def test_negative_control_doctor_requires_both_service_and_backup_launchd_jobs(monkeypatch) -> None:
    def fake_run(arguments, **_kwargs):
        target = arguments[-1]
        if target.endswith(cli.LAUNCHD_LABEL):
            return argparse.Namespace(returncode=0, stdout=b"state = running\n", stderr=b"")
        return argparse.Namespace(returncode=3, stdout=b"", stderr=b"Could not find service")

    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="com.axisvi.lattice.backup.*not loaded"):
        cli._launchd_status()


@pytest.mark.regression
def test_negative_control_outport_rejects_hostname_url_disagreement(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="disagrees"):
        resolve_lattice_service(tmp_path, runner=lambda _root: outport_payload(url="https://impostor.axisvi.test"))


@pytest.mark.regression
def test_negative_control_outport_rejects_a_relative_supervisor_binary() -> None:
    with pytest.raises(RuntimeError, match="absolute executable path"):
        outport_executable({"OUTPORT_BIN": "outport"})


@pytest.mark.oracle
def test_oracle_snapshot_restore_reproduces_exact_lattice_state(lattice_env, tmp_path) -> None:
    completed_invocation(lattice_env, "snapshot prompt", "snapshot response")
    source = assess_data_root(lattice_env["config"].data_root)
    snapshot = create_snapshot(
        lattice_env["config"].data_root,
        backup_root=tmp_path / "backups",
        reason="oracle",
        source_revision=source["state_digest"],
        schema_version=source["schema_version"],
        state_digest=source["state_digest"],
    )
    verified = assess_backup(snapshot)
    destination = tmp_path / "restored"
    manifest = restore_snapshot(snapshot, destination)
    restored = assess_data_root(destination)

    assert verified["state_digest"] == source["state_digest"]
    assert restored["state_digest"] == source["state_digest"]
    assert manifest["generated_object"]["role"] == "snapshot"
    assert manifest["generated_object"]["sources"] == [{"ref": f"lattice:data-root:{lattice_env['config'].data_root.resolve()}", "revision": source["state_digest"], "sha256": None}]
    assert {item["path"] for item in manifest["inventory"]} >= {"lattice.sqlite3"}


@pytest.mark.contract
def test_v2_snapshot_reader_preserves_v010_producer_compatibility(lattice_env, tmp_path) -> None:
    source = assess_data_root(lattice_env["config"].data_root)
    snapshot = create_snapshot(lattice_env["config"].data_root, backup_root=tmp_path / "backups", reason="legacy-producer-contract", source_revision=source["state_digest"], schema_version=source["schema_version"], state_digest=source["state_digest"])
    manifest_path = snapshot / snapshot_runtime.SNAPSHOT_MANIFEST
    legacy = json.loads(manifest_path.read_bytes())
    legacy["generated_object"]["producer"]["version"] = "0.1.0"
    manifest_path.write_bytes(canonical_bytes(legacy))

    assert verify_snapshot(snapshot)["generated_object"]["producer"] == {"name": "lattice", "version": "0.1.0"}

    legacy["generated_object"]["producer"]["name"] = "impostor"
    manifest_path.write_bytes(canonical_bytes(legacy))
    with pytest.raises(OSError, match="generated-object envelope is invalid"):
        verify_snapshot(snapshot)


@pytest.mark.regression
def test_snapshot_and_restore_sync_complete_directory_trees(monkeypatch, lattice_env, tmp_path) -> None:
    source = assess_data_root(lattice_env["config"].data_root)
    synced = []
    real_sync = snapshot_runtime._sync_tree_directories

    def recording_sync(root):
        synced.append(root)
        real_sync(root)

    monkeypatch.setattr(snapshot_runtime, "_sync_tree_directories", recording_sync)
    snapshot = create_snapshot(lattice_env["config"].data_root, backup_root=tmp_path / "backups", reason="directory-durability-regression", source_revision=source["state_digest"], schema_version=source["schema_version"], state_digest=source["state_digest"])
    restore_snapshot(snapshot, tmp_path / "restored")

    assert len(synced) == 2
    assert synced[0].name.startswith(".lattice-snapshot-")
    assert synced[1].name.startswith(".restored.restore-")


@pytest.mark.concurrency
def test_snapshot_identity_describes_the_captured_generation_not_a_stale_preflight(lattice_env, tmp_path) -> None:
    before = assess_data_root(lattice_env["config"].data_root)
    completed_invocation(lattice_env, "newer prompt", "newer response")

    snapshot = create_snapshot(
        lattice_env["config"].data_root,
        backup_root=tmp_path / "backups",
        reason="stale-preflight-negative-control",
        source_revision=before["state_digest"],
        schema_version=before["schema_version"],
        state_digest=before["state_digest"],
    )
    captured = assess_backup(snapshot)
    manifest = verify_snapshot(snapshot)

    assert captured["state_digest"] != before["state_digest"]
    assert manifest["state_digest"] == captured["state_digest"]
    assert manifest["generated_object"]["sources"][0]["revision"] == captured["state_digest"]


@pytest.mark.regression
def test_negative_control_snapshot_paths_cannot_corrupt_content_or_snapshot_identity(lattice_env, tmp_path) -> None:
    source = assess_data_root(lattice_env["config"].data_root)

    with pytest.raises(OSError, match="inside snapshot content"):
        create_snapshot(
            lattice_env["config"].data_root,
            backup_root=lattice_env["config"].data_root / "blobs" / "backups",
            reason="recursive-negative-control",
            source_revision=source["state_digest"],
            schema_version=source["schema_version"],
            state_digest=source["state_digest"],
        )

    snapshot = create_snapshot(
        lattice_env["config"].data_root,
        backup_root=tmp_path / "backups",
        reason="restore-containment-negative-control",
        source_revision=source["state_digest"],
        schema_version=source["schema_version"],
        state_digest=source["state_digest"],
    )
    with pytest.raises(OSError, match="inside its immutable snapshot"):
        restore_snapshot(snapshot, snapshot / "restored")
    assert verify_snapshot(snapshot)["state_digest"] == source["state_digest"]


@pytest.mark.regression
def test_negative_control_snapshot_rejects_direct_symlink_sources(lattice_env, tmp_path) -> None:
    source = assess_data_root(lattice_env["config"].data_root)
    token = lattice_env["config"].data_root / "operator-token"
    target = tmp_path / "real-token"
    target.write_text("secret", encoding="utf-8")
    token.symlink_to(target)

    with pytest.raises(OSError, match="source is a symlink"):
        create_snapshot(
            lattice_env["config"].data_root,
            backup_root=tmp_path / "backups",
            reason="symlink-negative-control",
            source_revision=source["state_digest"],
            schema_version=source["schema_version"],
            state_digest=source["state_digest"],
        )


@pytest.mark.regression
def test_negative_control_restore_rejects_missing_blob_even_when_database_is_readable(lattice_env, tmp_path) -> None:
    completed_invocation(lattice_env, "blob prompt", "blob response")
    source = assess_data_root(lattice_env["config"].data_root)
    snapshot = create_snapshot(lattice_env["config"].data_root, backup_root=tmp_path / "backups", reason="missing-blob-negative-control", source_revision=source["state_digest"], schema_version=source["schema_version"], state_digest=source["state_digest"])
    manifest = json.loads((snapshot / "snapshot.json").read_bytes())
    blob = next(snapshot / item["path"] for item in manifest["inventory"] if item["path"].startswith("blobs/"))
    blob.unlink()

    with pytest.raises(OSError, match="inventory"):
        restore_snapshot(snapshot, tmp_path / "must-not-exist")
    assert not (tmp_path / "must-not-exist").exists()


@pytest.mark.regression
def test_legacy_backup_manifest_remains_readable_without_rewriting_history(lattice_env, tmp_path) -> None:
    source = assess_data_root(lattice_env["config"].data_root)
    snapshot = create_snapshot(lattice_env["config"].data_root, backup_root=tmp_path / "backups", reason="legacy-reader", source_revision=source["state_digest"], schema_version=source["schema_version"], state_digest=source["state_digest"])
    path = snapshot / "snapshot.json"
    legacy = json.loads(path.read_bytes())
    legacy["contract_version"] = "LatticeBackup/1.0"
    legacy["generated_object"]["schema"]["version"] = 1
    legacy["generated_object"]["producer"]["version"] = "0.1.0"
    del legacy["generated_object"]["sources"][0]["sha256"]
    path.write_bytes(canonical_bytes(legacy))

    assert verify_snapshot(snapshot)["generated_object"]["artifact_id"] == legacy["generated_object"]["artifact_id"]


@pytest.mark.concurrency
def test_snapshot_refuses_active_durable_work(lattice_env, tmp_path) -> None:
    draft = lattice_env["store"].create_draft(DraftCreate(project_id=lattice_env["project"]["project_id"], role_session_id=lattice_env["session"]["role_session_id"], prompt="still running", route=lattice_env["route"]))
    invocation = lattice_env["lattice"].seal(draft["draft_id"])
    lattice_env["store"].create_attempt(invocation["invocation_id"], "active-snapshot-negative", "test-boot", 9_999_999_999)
    source = assess_data_root(lattice_env["config"].data_root)

    with pytest.raises(RuntimeError, match="durable work is active"):
        create_snapshot(lattice_env["config"].data_root, backup_root=tmp_path / "backups", reason="must-fail", source_revision=source["state_digest"], schema_version=source["schema_version"], state_digest=source["state_digest"])


@pytest.mark.regression
def test_negative_control_diagnostics_reject_tampered_model_run_binding(lattice_env) -> None:
    journal = create_run_journal("fake", subject="diagnostic", model="fake-model", effort="high", timeout=5, runs_dir=lattice_env["config"].runs)
    bind_request(journal, prompt="exact prompt", response_format="markdown", resume_strategy="portable")
    intact = assess_data_root(lattice_env["config"].data_root)
    assert intact["model_runs"]["run_count"] == 1

    journal.prompt.write_text("tampered prompt", encoding="utf-8")
    with pytest.raises(OSError, match="checkpoint binding failed"):
        assess_data_root(lattice_env["config"].data_root)


@pytest.mark.contract
def test_outport_public_origin_and_panels_embedding_are_exact(lattice_env) -> None:
    config = LatticeConfig(
        data_root=lattice_env["config"].data_root,
        port=9010,
        axisvi_root=None,
        workflow_pack_roots_extra=lattice_env["config"].workflow_pack_roots_extra,
        public_url="https://lattice.axisvi.test",
        frame_origins=("https://slate.axisvi.test",),
    )
    app = create_app(config)
    with TestClient(app, base_url="https://lattice.axisvi.test") as client:
        session = client.get("/api/session", headers={"origin": "https://lattice.axisvi.test"})
        rejected = client.get("/api/session", headers={"host": "impostor.axisvi.test"})

    assert session.status_code == 200
    assert "Secure" in session.headers["set-cookie"]
    assert "https://slate.axisvi.test" in session.headers["content-security-policy"]
    assert rejected.status_code == 421


@pytest.mark.regression
def test_negative_control_doctor_rejects_running_port_that_disagrees_with_outport(monkeypatch, tmp_path, capsys) -> None:
    service = OutportService(project="axisvi", instance="main", name="lattice", port=9010, hostname="lattice.axisvi.test", url="https://lattice.axisvi.test", up=True)
    monkeypatch.setattr(cli, "_service", lambda _config: service)
    monkeypatch.setattr(cli, "_health", lambda _service: {"status": "ok", "service": "lattice", "port": 9011, "data_root": str(tmp_path)})
    monkeypatch.setattr(cli, "_launchd_status", lambda: {"loaded": True})
    monkeypatch.setattr(cli, "_runtime_status", lambda: {"editable": False, "runtime_id": "test-runtime"})
    monkeypatch.setattr(cli, "_adapter_status", lambda _service=None, _token=None: {"anthropic": {}, "openai": {}})
    monkeypatch.setattr(cli, "_backup_status", lambda _config: {"fresh": True})
    monkeypatch.setattr(cli, "assess_data_root", lambda _root: {"state_digest": "0" * 64, "schema_version": 9, "expected_schema_version": 9})
    monkeypatch.setenv("LATTICE_DATA_DIR", str(tmp_path))

    status = cli.command_doctor(argparse.Namespace(json=True))
    output = json.loads(capsys.readouterr().out)

    assert status == 1
    assert next(item for item in output["checks"] if item["name"] == "service")["state"] == "fail"
    assert "Outport owns :9010" in next(item for item in output["checks"] if item["name"] == "service")["detail"]


@pytest.mark.regression
def test_negative_control_doctor_rejects_stale_schema_generation(monkeypatch, tmp_path, capsys) -> None:
    service = OutportService(project="axisvi", instance="main", name="lattice", port=9010, hostname="lattice.axisvi.test", url="https://lattice.axisvi.test", up=True)
    monkeypatch.setattr(cli, "_service", lambda _config: service)
    monkeypatch.setattr(cli, "_health", lambda _service: {"status": "ok", "service": "lattice", "port": 9010, "data_root": str(tmp_path), "runtime_id": "test-runtime"})
    monkeypatch.setattr(cli, "_launchd_status", lambda: {"loaded": True})
    monkeypatch.setattr(cli, "_runtime_status", lambda: {"editable": False, "runtime_id": "test-runtime"})
    monkeypatch.setattr(cli, "_adapter_status", lambda _service=None, _token=None: {"anthropic": {}, "openai": {}})
    monkeypatch.setattr(cli, "_backup_status", lambda _config: {"fresh": True})
    monkeypatch.setattr(cli, "assess_data_root", lambda _root: {"schema_version": 8, "expected_schema_version": 9})
    monkeypatch.setenv("LATTICE_DATA_DIR", str(tmp_path))

    status = cli.command_doctor(argparse.Namespace(json=True))
    output = json.loads(capsys.readouterr().out)

    assert status == 1
    check = next(item for item in output["checks"] if item["name"] == "database-blobs-and-journals")
    assert check["state"] == "fail"
    assert "schema generation is 8, expected 9" in check["detail"]


@pytest.mark.regression
def test_negative_control_doctor_rejects_service_from_unselected_runtime(monkeypatch, tmp_path, capsys) -> None:
    service = OutportService(project="axisvi", instance="main", name="lattice", port=9010, hostname="lattice.axisvi.test", url="https://lattice.axisvi.test", up=True)
    monkeypatch.setattr(cli, "_service", lambda _config: service)
    monkeypatch.setattr(cli, "_health", lambda _service: {"status": "ok", "service": "lattice", "port": 9010, "data_root": str(tmp_path), "runtime_id": "prior-runtime"})
    monkeypatch.setattr(cli, "_launchd_status", lambda: {"loaded": True})
    monkeypatch.setattr(cli, "_runtime_status", lambda: {"editable": False, "runtime_id": "candidate-runtime"})
    monkeypatch.setattr(cli, "_adapter_status", lambda _service=None, _token=None: {"anthropic": {}, "openai": {}})
    monkeypatch.setattr(cli, "_backup_status", lambda _config: {"fresh": True})
    monkeypatch.setattr(cli, "assess_data_root", lambda _root: {"state_digest": "0" * 64, "schema_version": 9, "expected_schema_version": 9})
    monkeypatch.setenv("LATTICE_DATA_DIR", str(tmp_path))

    status = cli.command_doctor(argparse.Namespace(json=True))
    output = json.loads(capsys.readouterr().out)

    assert status == 1
    check = next(item for item in output["checks"] if item["name"] == "service")
    assert check["state"] == "fail"
    assert "prior-runtime" in check["detail"] and "candidate-runtime" in check["detail"]


@pytest.mark.regression
def test_negative_control_doctor_rejects_service_version_disagreement(monkeypatch, tmp_path, capsys) -> None:
    service = OutportService(project="axisvi", instance="main", name="lattice", port=9010, hostname="lattice.axisvi.test", url="https://lattice.axisvi.test", up=True)
    monkeypatch.setattr(cli, "_service", lambda _config: service)
    monkeypatch.setattr(cli, "_health", lambda _service: {"status": "ok", "service": "lattice", "port": 9010, "data_root": str(tmp_path), "runtime_id": "test-runtime", "version": "0.1.0"})
    monkeypatch.setattr(cli, "_launchd_status", lambda: {"loaded": True})
    monkeypatch.setattr(cli, "_runtime_status", lambda: {"editable": False, "runtime_id": "test-runtime", "version": "0.2.0"})
    monkeypatch.setattr(cli, "_adapter_status", lambda _service=None, _token=None: {"anthropic": {}, "openai": {}})
    monkeypatch.setattr(cli, "_backup_status", lambda _config: {"fresh": True})
    monkeypatch.setattr(cli, "assess_data_root", lambda _root: {"state_digest": "0" * 64, "schema_version": 9, "expected_schema_version": 9})
    monkeypatch.setenv("LATTICE_DATA_DIR", str(tmp_path))

    status = cli.command_doctor(argparse.Namespace(json=True))
    output = json.loads(capsys.readouterr().out)

    assert status == 1
    check = next(item for item in output["checks"] if item["name"] == "service")
    assert check["state"] == "fail"
    assert "0.1.0" in check["detail"] and "0.2.0" in check["detail"]


@pytest.mark.regression
def test_negative_control_doctor_reports_undispatched_startup_recovery(monkeypatch, tmp_path, capsys) -> None:
    service = OutportService(project="axisvi", instance="main", name="lattice", port=9010, hostname="lattice.axisvi.test", url="https://lattice.axisvi.test", up=True)
    failure = {"invocation_id": "invocation_poisoned", "error": "planted"}
    monkeypatch.setattr(cli, "_service", lambda _config: service)
    monkeypatch.setattr(cli, "_health", lambda _service: {"status": "ok", "service": "lattice", "port": 9010, "data_root": str(tmp_path), "runtime_id": "test-runtime", "startup_recovery_failures": [failure]})
    monkeypatch.setattr(cli, "_launchd_status", lambda: {"loaded": True})
    monkeypatch.setattr(cli, "_runtime_status", lambda: {"editable": False, "runtime_id": "test-runtime"})
    monkeypatch.setattr(cli, "_adapter_status", lambda _service=None, _token=None: {"anthropic": {}, "openai": {}})
    monkeypatch.setattr(cli, "_backup_status", lambda _config: {"fresh": True})
    monkeypatch.setattr(cli, "assess_data_root", lambda _root: {"state_digest": "0" * 64, "schema_version": 9, "expected_schema_version": 9})
    monkeypatch.setenv("LATTICE_DATA_DIR", str(tmp_path))

    status = cli.command_doctor(argparse.Namespace(json=True))
    output = json.loads(capsys.readouterr().out)

    assert status == 1
    check = next(item for item in output["checks"] if item["name"] == "service")
    assert check["state"] == "fail"
    assert "invocation_poisoned" in check["detail"]


@pytest.mark.regression
def test_negative_control_doctor_reports_quarantined_workflow_pack(monkeypatch, tmp_path, capsys) -> None:
    service = OutportService(project="axisvi", instance="main", name="lattice", port=9010, hostname="lattice.axisvi.test", url="https://lattice.axisvi.test", up=True)
    failure = {"pack_id": "platform-foundry", "detail": "planted malformed pack"}
    monkeypatch.setattr(cli, "_service", lambda _config: service)
    monkeypatch.setattr(cli, "_health", lambda _service: {"status": "ok", "service": "lattice", "port": 9010, "data_root": str(tmp_path), "runtime_id": "test-runtime", "workflow_pack_failures": [failure]})
    monkeypatch.setattr(cli, "_launchd_status", lambda: {"loaded": True})
    monkeypatch.setattr(cli, "_runtime_status", lambda: {"editable": False, "runtime_id": "test-runtime"})
    monkeypatch.setattr(cli, "_adapter_status", lambda _service=None, _token=None: {"anthropic": {}, "openai": {}})
    monkeypatch.setattr(cli, "_backup_status", lambda _config: {"fresh": True})
    monkeypatch.setattr(cli, "assess_data_root", lambda _root: {"state_digest": "0" * 64, "schema_version": 9, "expected_schema_version": 9})
    monkeypatch.setenv("LATTICE_DATA_DIR", str(tmp_path))

    status = cli.command_doctor(argparse.Namespace(json=True))
    output = json.loads(capsys.readouterr().out)

    assert status == 1
    check = next(item for item in output["checks"] if item["name"] == "service")
    assert "quarantined invalid workflow packs" in check["detail"]
    assert "platform-foundry" in check["detail"]


@pytest.mark.contract
def test_doctor_qualifies_the_live_daemon_with_operator_authority(monkeypatch, tmp_path, capsys) -> None:
    service = OutportService(project="axisvi", instance="main", name="lattice", port=9010, hostname="lattice.axisvi.test", url="https://lattice.axisvi.test", up=True)
    token_path = tmp_path / "operator-token"
    token_path.write_text("x" * 64 + "\n", encoding="utf-8")
    token_path.chmod(0o600)
    monkeypatch.setattr(cli, "_service", lambda _config: service)
    monkeypatch.setattr(cli, "_health", lambda _service: {"status": "ok", "service": "lattice", "port": 9010, "data_root": str(tmp_path), "runtime_id": "test-runtime", "version": "0.2.0", "startup_recovery_failures": [], "workflow_pack_failures": []})
    monkeypatch.setattr(cli, "_launchd_status", lambda: {"service": {"loaded": True, "running": True}, "backup": {"loaded": True, "running": False}})
    monkeypatch.setattr(cli, "_runtime_status", lambda: {"editable": False, "runtime_id": "test-runtime", "version": "0.2.0"})
    monkeypatch.setattr(cli, "_service_routes", lambda _service, token: [
        {"provider_code": "anthropic", "official_cli": "claude", "model_code": "opus", "qualified": True, "operational": token == "x" * 64, "qualified_profiles": ["compiled-fresh", "workspace-agent", "native-continuation"]},
        {"provider_code": "openai", "official_cli": "codex", "model_code": "gpt-5.6-sol", "qualified": True, "operational": token == "x" * 64, "qualified_profiles": ["compiled-fresh", "workspace-agent", "native-continuation"]},
    ])
    monkeypatch.setattr(cli, "_backup_status", lambda _config: {"fresh": True})
    monkeypatch.setattr(cli, "assess_data_root", lambda _root: {"state_digest": "0" * 64, "schema_version": 9, "expected_schema_version": 9})
    monkeypatch.setenv("LATTICE_DATA_DIR", str(tmp_path))

    status = cli.command_doctor(argparse.Namespace(json=True))
    output = json.loads(capsys.readouterr().out)

    assert status == 0
    assert output["status"] == "pass"
    assert all(check["state"] == "pass" for check in output["checks"])


@pytest.mark.contract
def test_launchd_and_cli_installation_bind_only_the_current_verified_runtime() -> None:
    service = deployment.plist_payload(deployment.SERVICE_LABEL, backup=False, runtime_id="test-runtime")
    backup = deployment.plist_payload(deployment.BACKUP_LABEL, backup=True, runtime_id="test-runtime")
    shim = deployment.shim_payload().decode("utf-8")

    assert service["ProgramArguments"] == [str(deployment.CURRENT_LINK / ".venv" / "bin" / "lattice"), "serve"]
    assert service["RunAtLoad"] is True and service["KeepAlive"] is True and service["ThrottleInterval"] == 10
    assert service["ProcessType"] == "Interactive"
    assert backup["ProgramArguments"][-2:] == ["--reason", "scheduled-daily"]
    assert backup["StartCalendarInterval"] == {"Hour": 3, "Minute": 0}
    assert backup["ProcessType"] == "Background"
    assert service["EnvironmentVariables"]["OUTPORT_BIN"] == str(deployment.required_binary("outport"))
    assert service["EnvironmentVariables"]["LATTICE_RUNTIME_ID"] == "test-runtime"
    assert str(deployment.required_binary("claude").parent) in service["EnvironmentVariables"]["PATH"].split(":")
    assert str(deployment.required_binary("codex").parent) in service["EnvironmentVariables"]["PATH"].split(":")
    assert f"export AXISVI_ROOT={deployment.AXISVI_ROOT}" in shim
    assert f"export OUTPORT_BIN={deployment.required_binary('outport')}" in shim
    assert str(deployment.CURRENT_LINK / ".venv" / "bin" / "lattice") in shim
    assert str(deployment.SOURCE_ROOT / ".venv") not in shim
    assert deployment.managed_shim(deployment.shim_payload()) is True
    prior = deployment.shim_payload().decode("utf-8").replace(str(deployment.required_binary("outport")), str(deployment.required_binary("outport").resolve())).encode("utf-8")
    assert deployment.managed_shim(prior) is True
    assert deployment.managed_shim(b"#!/bin/sh\nexec something-else\n") is False
    planted = deployment.shim_payload().decode("utf-8").replace(str(deployment.required_binary("outport")), "/bin/sh").encode("utf-8")
    assert deployment.managed_shim(planted) is False


@pytest.mark.regression
def test_negative_control_runtime_probe_executes_outside_the_source_checkout(monkeypatch, tmp_path) -> None:
    observed = {}

    def fake_run(arguments, *, cwd, capture):
        observed.update({"arguments": arguments, "cwd": cwd, "capture": capture})
        return argparse.Namespace(stdout=b'{"lattice":"/installed/lattice/__init__.py","model_runner":"/installed/axis_model_runner/__init__.py"}')

    monkeypatch.setattr(deployment, "run", fake_run)
    locations = deployment.probe_runtime(tmp_path)

    assert observed["cwd"] == tmp_path
    assert observed["capture"] is True
    assert locations["lattice"].startswith("/installed/")


@pytest.mark.regression
def test_installer_reexecs_before_application_imports(monkeypatch, tmp_path) -> None:
    source = tmp_path / "source-python"
    source.write_text("", encoding="utf-8")
    observed = {}

    def fake_execv(executable, arguments):
        observed.update({"executable": executable, "arguments": arguments})
        raise RuntimeError("exec boundary")

    monkeypatch.setattr(deployment, "source_python", lambda: source)
    monkeypatch.setattr(deployment.sys, "prefix", str(tmp_path / "system-prefix"))
    monkeypatch.setattr(deployment.os, "execv", fake_execv)

    with pytest.raises(RuntimeError, match="exec boundary"):
        deployment.reexec_in_source_environment()

    assert observed["executable"] == str(source.resolve())
    assert observed["arguments"][0] == str(source.resolve())


@pytest.mark.regression
def test_installer_verification_includes_static_cockpit_diagnostics(monkeypatch) -> None:
    calls = []

    def fake_run(arguments, *, cwd=deployment.SOURCE_ROOT, env=None, capture=False):
        calls.append((arguments, cwd, env, capture))
        return argparse.Namespace(stdout=b"", stderr=b"", returncode=0)

    monkeypatch.setattr(deployment, "run", fake_run)
    deployment.verify_source()

    assert (["npm", "run", "check"], deployment.SOURCE_ROOT / "web", None, False) in calls


@pytest.mark.contract
def test_runtime_sync_forces_both_local_packages_to_rebuild() -> None:
    arguments = deployment.runtime_sync_arguments()

    assert arguments.count("--reinstall-package") == 2
    assert arguments[arguments.index("--reinstall-package") + 1] == "lattice"
    assert arguments[arguments.index("--reinstall-package", arguments.index("--reinstall-package") + 1) + 1] == "axis-model-runner"


@pytest.mark.oracle
def test_runtime_tree_attestation_detects_a_stale_cached_package(tmp_path) -> None:
    source = tmp_path / "source"
    installed = tmp_path / "installed"
    source.mkdir()
    installed.mkdir()
    source.joinpath("cli.py").write_text("from .security import ensure_cli_token\n", encoding="utf-8")
    installed.joinpath("cli.py").write_text("from .security import ensure_cli_token\n", encoding="utf-8")

    expected = mapped_inventory(((source, ""),))
    assert tree_inventory(installed) == expected

    installed.joinpath("cli.py").write_text("# stale cached wheel\n", encoding="utf-8")
    assert tree_inventory(installed) != expected


@pytest.mark.regression
def test_live_publication_probe_never_swallows_operator_interrupt(monkeypatch) -> None:
    service = OutportService(project="axisvi", instance="main", name="lattice", port=9010, hostname="lattice.axisvi.test", url="https://lattice.axisvi.test", up=True)
    monkeypatch.setattr("lattice.outport.resolve_lattice_service", lambda _root: service)
    monkeypatch.setattr(deployment.urllib.request, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        deployment.verify_live_service("candidate-runtime", timeout_seconds=1)


@pytest.mark.contract
def test_runtime_manifest_canonicalization_uses_the_candidate_environment(tmp_path) -> None:
    runtime_python = Path(__import__("sys").executable)
    first = deployment.canonical_runtime_manifest({"z": "last", "a": [1, "é"]}, runtime_python)
    second = deployment.canonical_runtime_manifest({"a": [1, "é"], "z": "last"}, runtime_python)

    assert first == second == b'{"a":[1,"\xc3\xa9"],"z":"last"}'


@pytest.mark.regression
def test_negative_control_generated_objects_reject_partial_source_records() -> None:
    with pytest.raises(ValueError, match="exactly ref, revision, and sha256"):
        generated_object(kind="lattice-test", schema_name="lattice-test", subject="lattice:test", sources=[{"ref": "lattice:test", "revision": "one"}])
    with pytest.raises(ValueError, match="durable identity"):
        generated_object(kind="lattice-test", schema_name="lattice-test", subject="lattice:test", sources=[{"ref": "lattice:test", "revision": "one", "sha256": None}], artifact_id="artifact:traversal")


@pytest.mark.contract
def test_runtime_manifest_binds_complete_sources_and_executing_modules(monkeypatch, tmp_path) -> None:
    target = tmp_path / "runtime"
    python = target / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(Path(__import__("sys").executable))
    executable = target / ".venv" / "bin" / "lattice"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    locations = {"lattice": str(target / "lattice/__init__.py"), "model_runner": str(target / "axis_model_runner/__init__.py")}
    source_trees = {"lattice": [{"path": "cli.py", "sha256": "c" * 64, "byte_count": 1}], "model_runner": [{"path": "process.py", "sha256": "d" * 64, "byte_count": 1}]}
    manifest = deployment.build_runtime_manifest(target=target, runtime_id="test-runtime", lattice_revision="a" * 40, runner_revision="b" * 40, packages=["lattice==0.2.0"], module_locations=locations, source_trees=source_trees)
    raw = canonical_bytes(manifest)
    (target / "runtime.json").write_bytes(raw)
    monkeypatch.setattr(deployment, "probe_runtime", lambda _target: locations)
    monkeypatch.setattr(deployment, "canonical_runtime_manifest", lambda value, _python: canonical_bytes(value))
    monkeypatch.setattr(deployment, "source_tree_inventories", lambda: source_trees)
    monkeypatch.setattr(deployment, "installed_tree_inventories", lambda _locations: source_trees)

    deployment.verify_runtime_target(target, runtime_id="test-runtime", lattice_revision="a" * 40, runner_revision="b" * 40)
    assert all(set(source) == {"ref", "revision", "sha256"} for source in manifest["generated_object"]["sources"])

    (target / "runtime.json").write_bytes(raw + b"\n")
    with pytest.raises(RuntimeError, match="not exact RFC 8785"):
        deployment.verify_runtime_target(target, runtime_id="test-runtime", lattice_revision="a" * 40, runner_revision="b" * 40)


@pytest.mark.regression
def test_negative_control_runtime_manifest_rejects_incomplete_generated_source(tmp_path) -> None:
    target = tmp_path / "runtime"
    python = target / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(Path(__import__("sys").executable))
    source_trees = {"lattice": [{"path": "cli.py", "sha256": "c" * 64, "byte_count": 1}], "model_runner": [{"path": "process.py", "sha256": "d" * 64, "byte_count": 1}]}
    manifest = deployment.build_runtime_manifest(target=target, runtime_id="test-runtime", lattice_revision="a" * 40, runner_revision="b" * 40, packages=[], module_locations={"lattice": "/runtime/lattice", "model_runner": "/runtime/model-runner"}, source_trees=source_trees)
    del manifest["generated_object"]["sources"][0]["sha256"]

    with pytest.raises(RuntimeError, match="provenance"):
        deployment._validate_runtime_manifest(manifest, target=target, runtime_id="test-runtime", lattice_revision="a" * 40, runner_revision="b" * 40)


@pytest.mark.regression
def test_negative_control_runtime_manifest_rejects_missing_package_attestation(tmp_path) -> None:
    target = tmp_path / "runtime"
    python = target / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(Path(__import__("sys").executable))
    source_trees = {"lattice": [{"path": "cli.py", "sha256": "c" * 64, "byte_count": 1}], "model_runner": [{"path": "process.py", "sha256": "d" * 64, "byte_count": 1}]}
    manifest = deployment.build_runtime_manifest(target=target, runtime_id="test-runtime", lattice_revision="a" * 40, runner_revision="b" * 40, packages=[], module_locations={"lattice": "/runtime/lattice", "model_runner": "/runtime/model-runner"}, source_trees=source_trees)
    del manifest["source_trees"]["model_runner"]

    with pytest.raises(RuntimeError, match="source-tree attestation is malformed"):
        deployment._validate_runtime_manifest(manifest, target=target, runtime_id="test-runtime", lattice_revision="a" * 40, runner_revision="b" * 40)


@pytest.mark.regression
def test_negative_control_runtime_manifest_canonicalization_rejects_nonfinite_numbers() -> None:
    with pytest.raises(ValueError, match="JSON compliant"):
        deployment.canonical_runtime_manifest({"unsafe": float("nan")}, Path(__import__("sys").executable))


@pytest.mark.regression
def test_negative_control_live_publication_rejects_the_prior_runtime(monkeypatch) -> None:
    service = OutportService(project="axisvi", instance="main", name="lattice", port=9010, hostname="lattice.axisvi.test", url="https://lattice.axisvi.test", up=True)
    monkeypatch.setattr("lattice.outport.resolve_lattice_service", lambda _root: service)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"status": "ok", "port": 9010, "data_root": str(deployment.DEFAULT_DATA_ROOT), "runtime_id": "prior-runtime"}).encode()

    monkeypatch.setattr(deployment.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(deployment.time, "sleep", lambda _seconds: None)
    ticks = iter((0.0, 0.5, 1.0))
    monkeypatch.setattr(deployment.time, "monotonic", lambda: next(ticks))

    with pytest.raises(RuntimeError, match="prior-runtime"):
        deployment.verify_live_service("candidate-runtime", timeout_seconds=1)


@pytest.mark.regression
def test_negative_control_live_publication_rejects_wrong_package_version(monkeypatch) -> None:
    service = OutportService(project="axisvi", instance="main", name="lattice", port=9010, hostname="lattice.axisvi.test", url="https://lattice.axisvi.test", up=True)
    monkeypatch.setattr("lattice.outport.resolve_lattice_service", lambda _root: service)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"status": "ok", "port": 9010, "data_root": str(deployment.DEFAULT_DATA_ROOT), "runtime_id": "candidate-runtime", "version": "0.1.0"}).encode()

    monkeypatch.setattr(deployment.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(deployment.time, "sleep", lambda _seconds: None)
    ticks = iter((0.0, 0.5, 1.0))
    monkeypatch.setattr(deployment.time, "monotonic", lambda: next(ticks))

    with pytest.raises(RuntimeError, match="0.1.0"):
        deployment.verify_live_service("candidate-runtime", timeout_seconds=1)


@pytest.mark.regression
def test_negative_control_live_publication_rejects_quarantined_workflows(monkeypatch) -> None:
    service = OutportService(project="axisvi", instance="main", name="lattice", port=9010, hostname="lattice.axisvi.test", url="https://lattice.axisvi.test", up=True)
    monkeypatch.setattr("lattice.outport.resolve_lattice_service", lambda _root: service)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"status": "ok", "port": 9010, "data_root": str(deployment.DEFAULT_DATA_ROOT), "runtime_id": "candidate-runtime", "workflow_pack_failures": [{"pack_id": "broken", "detail": "planted"}]}).encode()

    monkeypatch.setattr(deployment.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(deployment.time, "sleep", lambda _seconds: None)
    ticks = iter((0.0, 0.5, 1.0))
    monkeypatch.setattr(deployment.time, "monotonic", lambda: next(ticks))

    with pytest.raises(RuntimeError, match="workflow_pack_failures"):
        deployment.verify_live_service("candidate-runtime", timeout_seconds=1)


@pytest.mark.regression
def test_negative_control_first_install_failure_unpublishes_every_candidate_selector(monkeypatch, tmp_path) -> None:
    runtime_root = tmp_path / "runtime"
    target = runtime_root / "versions" / "candidate"
    target.mkdir(parents=True)
    current = runtime_root / "current"
    previous = runtime_root / "previous"
    shim = tmp_path / "bin" / "lattice"
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    shim.parent.mkdir()
    current.symlink_to(target)
    previous.symlink_to(target)
    shim.write_bytes(b"candidate shim")
    service_path = agents / f"{deployment.SERVICE_LABEL}.plist"
    backup_path = agents / f"{deployment.BACKUP_LABEL}.plist"
    service_path.write_bytes(b"candidate service")
    backup_path.write_bytes(b"candidate backup")
    monkeypatch.setattr(deployment, "CURRENT_LINK", current)
    monkeypatch.setattr(deployment, "PREVIOUS_LINK", previous)
    monkeypatch.setattr(deployment, "SHIM", shim)
    monkeypatch.setattr(deployment, "LAUNCH_AGENTS", agents)
    unloaded = []
    monkeypatch.setattr(deployment, "unload_job", lambda label: unloaded.append(label))

    deployment.restore_publication_state(deployment.PublicationState(None, None, None, None, None, False, False))

    assert not current.exists() and not current.is_symlink()
    assert not previous.exists() and not previous.is_symlink()
    assert not shim.exists() and not service_path.exists() and not backup_path.exists()
    assert unloaded == [deployment.BACKUP_LABEL, deployment.SERVICE_LABEL]


@pytest.mark.regression
def test_publication_rollback_attempts_every_independent_restoration(monkeypatch, tmp_path) -> None:
    current = tmp_path / "current"
    previous = tmp_path / "previous"
    shim = tmp_path / "lattice"
    agents = tmp_path / "LaunchAgents"
    monkeypatch.setattr(deployment, "CURRENT_LINK", current)
    monkeypatch.setattr(deployment, "PREVIOUS_LINK", previous)
    monkeypatch.setattr(deployment, "SHIM", shim)
    monkeypatch.setattr(deployment, "LAUNCH_AGENTS", agents)
    calls = []

    def restore_symlink(path, _target):
        calls.append(("selector", path.name))
        if path == current:
            raise OSError("planted current-selector failure")

    monkeypatch.setattr(deployment, "_restore_symlink", restore_symlink)
    monkeypatch.setattr(deployment, "_restore_file", lambda path, _state: calls.append(("file", path.name)))
    monkeypatch.setattr(deployment, "unload_job", lambda label: calls.append(("job", label)))

    with pytest.raises(BaseExceptionGroup, match="rollback was incomplete"):
        deployment.restore_publication_state(deployment.PublicationState(None, None, None, None, None, False, False))

    assert calls == [
        ("selector", "current"),
        ("selector", "previous"),
        ("file", "lattice"),
        ("file", f"{deployment.SERVICE_LABEL}.plist"),
        ("file", f"{deployment.BACKUP_LABEL}.plist"),
        ("job", deployment.BACKUP_LABEL),
        ("job", deployment.SERVICE_LABEL),
    ]


@pytest.mark.regression
def test_runtime_selector_replacement_syncs_its_parent(monkeypatch, tmp_path) -> None:
    target = tmp_path / "versions" / "candidate"
    target.mkdir(parents=True)
    link = tmp_path / "runtime" / "current"
    synced = []
    monkeypatch.setattr(deployment, "sync_directory", lambda path: synced.append(path))

    deployment.replace_symlink(link, target)

    assert link.resolve() == target
    assert synced == [link.parent]


@pytest.mark.regression
def test_publication_failure_always_invokes_exact_state_rollback(monkeypatch, tmp_path) -> None:
    target = tmp_path / "candidate"
    target.mkdir()
    prior = deployment.PublicationState(None, None, None, None, None, False, False)
    calls = []
    monkeypatch.setattr(deployment, "capture_publication_state", lambda: prior)
    monkeypatch.setattr(deployment, "install_links", lambda value: calls.append(("links", value)))
    monkeypatch.setattr(deployment, "install_launchd", lambda value: (_ for _ in ()).throw(RuntimeError("publication failed")))
    monkeypatch.setattr(deployment, "restore_publication_state", lambda value: calls.append(("restore", value)))

    with pytest.raises(RuntimeError, match="publication failed"):
        deployment.publish_runtime(target)

    assert calls == [("links", target), ("restore", prior)]


@pytest.mark.regression
def test_negative_control_selector_publication_failure_also_rolls_back(monkeypatch, tmp_path) -> None:
    target = tmp_path / "candidate"
    target.mkdir()
    prior = deployment.PublicationState(None, None, None, None, None, False, False)
    calls = []
    monkeypatch.setattr(deployment, "capture_publication_state", lambda: prior)
    monkeypatch.setattr(deployment, "install_links", lambda value: (_ for _ in ()).throw(RuntimeError(f"selector failed: {value}")))
    monkeypatch.setattr(deployment, "restore_publication_state", lambda value: calls.append(("restore", value)))

    with pytest.raises(RuntimeError, match="selector failed"):
        deployment.publish_runtime(target)

    assert calls == [("restore", prior)]


@pytest.mark.regression
def test_negative_control_required_binary_preserves_the_public_entrypoint(monkeypatch, tmp_path) -> None:
    target = tmp_path / "provider.exe"
    target.write_bytes(b"provider")
    target.chmod(0o755)
    entrypoint = tmp_path / "provider"
    entrypoint.symlink_to(target)
    monkeypatch.setattr(deployment.shutil, "which", lambda _name: str(entrypoint))

    assert deployment.required_binary("provider") == entrypoint


@pytest.mark.regression
def test_negative_control_doctor_uses_the_daemon_provider_view(monkeypatch) -> None:
    service = OutportService(project="axisvi", instance="main", name="lattice", port=9010, hostname="lattice.axisvi.test", url="https://lattice.axisvi.test", up=True)
    monkeypatch.setattr(cli, "_service_routes", lambda _service, _token: [
        {"provider_code": "anthropic", "official_cli": "claude", "model_code": "opus", "qualified": False, "operational": False, "qualified_profiles": []},
        {"provider_code": "openai", "official_cli": "codex", "model_code": "gpt-5.6-sol", "qualified": True, "operational": True, "qualified_profiles": ["compiled-fresh", "workspace-agent", "native-continuation"]},
    ])

    with pytest.raises(RuntimeError, match=r"unavailable=\['anthropic'\]"):
        cli._adapter_status(service, "operator-capability")


@pytest.mark.regression
def test_negative_control_doctor_rejects_an_installed_but_hung_provider(monkeypatch) -> None:
    service = OutportService(project="axisvi", instance="main", name="lattice", port=9010, hostname="lattice.axisvi.test", url="https://lattice.axisvi.test", up=True)
    monkeypatch.setattr(cli, "_service_routes", lambda _service, _token: [
        {"provider_code": "anthropic", "official_cli": "claude", "model_code": "opus", "qualified": True, "operational": False, "qualified_profiles": ["compiled-fresh", "workspace-agent", "native-continuation"]},
        {"provider_code": "openai", "official_cli": "codex", "model_code": "gpt-5.6-sol", "qualified": True, "operational": True, "qualified_profiles": ["compiled-fresh", "workspace-agent", "native-continuation"]},
    ])

    with pytest.raises(RuntimeError, match=r"unavailable=\['anthropic'\]"):
        cli._adapter_status(service, "operator-capability")


@pytest.mark.regression
def test_negative_control_doctor_check_does_not_swallow_operator_interrupt() -> None:
    def interrupted():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        cli._check("interruptible", interrupted)


@pytest.mark.regression
def test_negative_control_launchd_reload_waits_out_removal_and_transient_eio(monkeypatch, tmp_path) -> None:
    calls = []
    results = iter((0, 0, 0, 1, 5, 0))

    def fake_run(arguments, **_kwargs):
        calls.append(arguments)
        return argparse.Namespace(returncode=next(results), stdout=b"", stderr=b"")

    monkeypatch.setattr(deployment.subprocess, "run", fake_run)
    monkeypatch.setattr(deployment.time, "sleep", lambda _seconds: None)
    deployment.reload_job("com.axisvi.lattice", tmp_path / "lattice.plist")

    assert [call[1] for call in calls] == ["print", "bootout", "print", "print", "bootstrap", "bootstrap"]


@pytest.mark.property
def test_negative_control_migration_baseline_rejects_invented_source_rows(lattice_env) -> None:
    baseline = deployment.capture_migration_baseline(lattice_env["config"].data_root)
    lattice_env["store"].create_project(ProjectCreate(title="invented during migration", purpose="must fail"))

    with pytest.raises(OSError, match="changed source-schema rows"):
        deployment.verify_migration_baseline(lattice_env["config"].data_root, baseline)


@pytest.mark.contract
def test_macos_default_state_is_outside_the_development_checkout() -> None:
    root = user_data_root({})
    assert root == Path.home() / "Library" / "Application Support" / "Lattice"
