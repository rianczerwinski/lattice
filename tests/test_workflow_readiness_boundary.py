"""Workflow failures remain visible without obstructing unrelated runtime recovery."""
import json
from pathlib import Path

import pytest

from lattice.outport import OutportService
from ops import install as deployment


@pytest.mark.regression
@pytest.mark.parametrize("failure", [
    {"conversation_ready": False},
    {"durable_run_ready": False},
    {"startup_recovery_failures": ["unfinished recovery"]},
    {"runtime_id": "wrong-runtime"},
])
def test_workflow_degradation_never_masks_core_failure(monkeypatch, failure):
    payload = {"status": "degraded", "conversation_ready": True, "durable_run_ready": True,
               "port": 9010, "data_root": str(deployment.DEFAULT_DATA_ROOT),
               "runtime_id": "candidate", "version": "0.3.1",
               "workflow_pack_failures": [{"pack_id": "optional", "detail": "invalid definition"}],
               "startup_recovery_failures": [], **failure}
    service = OutportService(project="axisvi", instance="main", name="lattice", port=9010, hostname="lattice.axisvi.test", url="https://lattice.axisvi.test", up=True)
    monkeypatch.setattr("lattice.outport.resolve_lattice_service", lambda _root: service)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self):
            return json.dumps(payload).encode()

    monkeypatch.setattr(deployment.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    ticks = iter((0.0, 0.5, 1.0))
    monkeypatch.setattr(deployment.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(deployment.time, "sleep", lambda _seconds: None)
    with pytest.raises(RuntimeError, match="health identity mismatch"):
        deployment.verify_live_service("candidate", "0.3.1", timeout_seconds=1)


@pytest.mark.regression
def test_candidate_validation_checks_only_shipped_packs(tmp_path, monkeypatch):
    import lattice.config
    from lattice.config import WORKFLOW_PACK_ROOT

    target = tmp_path / "candidate"
    commands = []

    def run(command, **kwargs):
        assert command[:2] == [str(target / ".venv/bin/python"), "-c"]
        assert kwargs["cwd"] == target
        commands.append(command)
        exec(command[2], {})

    monkeypatch.setattr(deployment, "run", run)
    monkeypatch.setattr(deployment, "verify_candidate_backup", lambda *_args: None)
    monkeypatch.setattr(deployment, "qualify_candidate_migration", lambda *_args: None)
    assert WORKFLOW_PACK_ROOT.is_dir()
    deployment.default_candidate_verification(target, None)
    assert len(commands) == 1

    broken = tmp_path / "broken-bundle"
    pack = broken / "invalid"
    pack.mkdir(parents=True)
    (pack / "pack.json").write_text("{}")
    monkeypatch.setattr(lattice.config, "WORKFLOW_PACK_ROOT", broken)
    with pytest.raises(RuntimeError, match="invalid bundled workflow packs"):
        deployment.default_candidate_verification(target, None)

    # External content is absent from this preflight's inputs. It is evaluated
    # and exposed by the running workflow catalogue, not by core publication.
    monkeypatch.setattr(lattice.config, "WORKFLOW_PACK_ROOT", WORKFLOW_PACK_ROOT)
    deployment.default_candidate_verification(target, None)


@pytest.mark.regression
@pytest.mark.parametrize("duplicate", [False, True])
def test_external_pack_error_does_not_block_real_service_publication(tmp_path, monkeypatch, duplicate):
    import io
    from fastapi.testclient import TestClient
    from lattice import LatticeConfig
    from lattice.server import create_app
    from lattice.security import ensure_cli_token

    roots = []
    for ordinal in range(2 if duplicate else 1):
        root = tmp_path / f"external-{ordinal}"
        pack = root / "optional"
        pack.mkdir(parents=True)
        (pack / "pack.json").write_text("{}")
        roots.append(root)
    config = LatticeConfig(data_root=tmp_path / "state", axisvi_root=None, port=9010, workflow_pack_roots_extra=tuple(roots))
    with TestClient(create_app(config)) as client:
        token = ensure_cli_token(config.operator_token)
        client.get("/api/session", headers={"authorization": f"Bearer {token}"})
        health = client.get("/api/health").json()
        assert health["conversation_ready"] is True
        assert health["workflow_ready"] is False
        assert any(item["pack_id"] == "optional" for item in health["workflow_pack_failures"])
        assert client.get("/api/v1/workflow-packs/optional").status_code == 422
        assert client.get("/api/v1/workflow-packs/platform-foundry").status_code == 200
        service = OutportService(project="axisvi", instance="main", name="lattice", port=9010, hostname="lattice.axisvi.test", url="https://lattice.axisvi.test", up=True)
        monkeypatch.setattr("lattice.outport.resolve_lattice_service", lambda _root: service)
        monkeypatch.setattr(deployment, "DEFAULT_DATA_ROOT", config.data_root)
        monkeypatch.setattr(deployment.urllib.request, "urlopen", lambda *_args, **_kwargs: io.BytesIO(json.dumps(health).encode()))
        deployment.verify_live_service(health["runtime_id"], health["version"], timeout_seconds=1)
