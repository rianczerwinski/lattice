"""Truth-typed assurance for Lattice service identity, snapshots, and recovery."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from axis_model_runner import bind_request, create_run_journal
from fastapi.testclient import TestClient

from lattice import cli
from lattice.config import LatticeConfig, user_data_root
from lattice.contracts import DraftCreate, ProjectCreate
from lattice.diagnostics import assess_backup, assess_data_root
from lattice.outport import OutportService, resolve_lattice_service
from lattice.server import create_app
from lattice.snapshot import create_snapshot, restore_snapshot
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
def test_outport_is_the_only_lattice_service_identity(tmp_path) -> None:
    service = resolve_lattice_service(tmp_path, runner=lambda _root: outport_payload())

    assert service.port == 9010
    assert service.url == "https://lattice.axisvi.test"
    assert service.frame_origins == ("https://slate.axisvi.test",)
    assert service.loopback_url == "http://127.0.0.1:9010"


@pytest.mark.regression
def test_negative_control_outport_rejects_hostname_url_disagreement(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="disagrees"):
        resolve_lattice_service(tmp_path, runner=lambda _root: outport_payload(url="https://impostor.axisvi.test"))


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
    assert {item["path"] for item in manifest["inventory"]} >= {"lattice.sqlite3"}


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
    monkeypatch.setattr(cli, "_runtime_status", lambda: {"editable": False})
    monkeypatch.setattr(cli, "_adapter_status", lambda: {"anthropic": {}, "openai": {}})
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
    monkeypatch.setattr(cli, "_health", lambda _service: {"status": "ok", "service": "lattice", "port": 9010, "data_root": str(tmp_path)})
    monkeypatch.setattr(cli, "_launchd_status", lambda: {"loaded": True})
    monkeypatch.setattr(cli, "_runtime_status", lambda: {"editable": False})
    monkeypatch.setattr(cli, "_adapter_status", lambda: {"anthropic": {}, "openai": {}})
    monkeypatch.setattr(cli, "_backup_status", lambda _config: {"fresh": True})
    monkeypatch.setattr(cli, "assess_data_root", lambda _root: {"schema_version": 8, "expected_schema_version": 9})
    monkeypatch.setenv("LATTICE_DATA_DIR", str(tmp_path))

    status = cli.command_doctor(argparse.Namespace(json=True))
    output = json.loads(capsys.readouterr().out)

    assert status == 1
    check = next(item for item in output["checks"] if item["name"] == "database-blobs-and-journals")
    assert check["state"] == "fail"
    assert "schema generation is 8, expected 9" in check["detail"]


@pytest.mark.contract
def test_launchd_and_cli_installation_bind_only_the_current_verified_runtime() -> None:
    service = deployment.plist_payload(deployment.SERVICE_LABEL, backup=False)
    backup = deployment.plist_payload(deployment.BACKUP_LABEL, backup=True)
    shim = deployment.shim_payload().decode("utf-8")

    assert service["ProgramArguments"] == [str(deployment.CURRENT_LINK / ".venv" / "bin" / "lattice"), "serve"]
    assert service["RunAtLoad"] is True and service["KeepAlive"] is True and service["ThrottleInterval"] == 10
    assert backup["ProgramArguments"][-2:] == ["--reason", "scheduled-daily"]
    assert backup["StartCalendarInterval"] == {"Hour": 3, "Minute": 0}
    assert f"export AXISVI_ROOT={deployment.AXISVI_ROOT}" in shim
    assert str(deployment.CURRENT_LINK / ".venv" / "bin" / "lattice") in shim
    assert str(deployment.SOURCE_ROOT / ".venv") not in shim


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
