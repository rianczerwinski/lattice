"""Provider updates affect their own capabilities, not service availability."""

import pytest
import sys
from fastapi.testclient import TestClient

from lattice import cli
from lattice.canonical import canonical_bytes
from lattice.execution import adapters
from lattice.runtime_attestation import tree_inventory
from lattice.security import ensure_cli_token
from lattice.server import create_app
from ops import install


@pytest.mark.regression
def test_installation_verifies_application_without_freezing_optional_providers(monkeypatch, tmp_path):
    target = tmp_path / "runtime"
    binary = target / ".venv/bin/python"
    binary.parent.mkdir(parents=True)
    binary.symlink_to(sys.executable)
    binary.with_name("lattice").write_text("fixture")
    package = target / "package"
    package.mkdir()
    package.joinpath("__init__.py").write_text("original application")
    locations = {"lattice": str(package / "__init__.py")}
    trees = {"lattice": tree_inventory(package)}
    attestations = [{**a.definition(), "model_code": a.model_code, "binary": str(tmp_path / "updated-provider"),
                    "binary_sha256": "a" * 64, "installed": True, "qualified": True,
                    "operational": True, "version": "historical", "control_surface_qualified": True,
                    "control_surface_detail": "historical"} for a in adapters.DEFAULT_ADAPTERS]
    manifest = install.build_runtime_manifest(target=target, runtime_id="test-runtime", lattice_revision="a" * 40,
                                             packages=[], module_locations=locations, source_trees=trees,
                                             provider_attestations=attestations)
    (target / "runtime.json").write_bytes(canonical_bytes(manifest))
    monkeypatch.setattr(install, "probe_runtime", lambda _target: locations)
    monkeypatch.setattr(install, "canonical_runtime_manifest", lambda value, _python: canonical_bytes(value))
    monkeypatch.setattr(install, "source_tree_inventories", lambda: trees)
    monkeypatch.setattr("lattice.runtime_attestation.verify_provider_attestation_freshness",
                        lambda _value: pytest.fail("installation must not freeze optional provider binaries"))
    install.verify_runtime_target(target, runtime_id="test-runtime", lattice_revision="a" * 40)
    package.joinpath("__init__.py").write_text("changed application")
    with pytest.raises(RuntimeError, match="application bytes disagree"):
        install.verify_runtime_target(target, runtime_id="test-runtime", lattice_revision="a" * 40)


@pytest.mark.regression
def test_runtime_identity_does_not_require_unchanged_provider_bytes(monkeypatch, tmp_path):
    target = tmp_path / "versions" / "fixture-runtime"
    package = target / ".venv/lib/site-packages/lattice"
    package.mkdir(parents=True)
    package.joinpath("__init__.py").write_text("")
    package.joinpath("cli.py").write_text("# retained installed application\n")
    executable = target / ".venv/bin/python"
    executable.parent.mkdir(parents=True)
    executable.write_text("fixture")
    provider = tmp_path / "provider"
    provider.write_text("updated executable")
    attestations = []
    for adapter in adapters.DEFAULT_ADAPTERS:
        attestations.append({**adapter.definition(), "model_code": adapter.model_code,
            "binary": str(provider), "binary_sha256": "a" * 64, "installed": True,
            "qualified": True, "operational": True, "version": "prior version",
            "control_surface_qualified": True, "control_surface_detail": "prior observation"})
    manifest = install.build_runtime_manifest(target=target, runtime_id=target.name,
        lattice_revision="a" * 40, packages=[],
        module_locations={"lattice": str(package / "__init__.py")},
        source_trees={"lattice": tree_inventory(package)}, provider_attestations=attestations)
    target.joinpath("runtime.json").write_bytes(canonical_bytes(manifest))
    monkeypatch.setattr(cli, "__file__", str(package / "cli.py"))
    monkeypatch.setattr(cli.lattice_package, "__file__", str(package / "__init__.py"))
    monkeypatch.setattr(cli.sys, "executable", str(executable))
    monkeypatch.setattr("lattice.runtime_attestation.shutil.which", lambda _: str(provider))
    assert cli._runtime_status()["runtime_id"] == target.name
    # Application tampering is a different claim; its existing check remains.
    package.joinpath("cli.py").write_text("changed application")
    with pytest.raises(RuntimeError, match="application bytes disagree"):
        cli._runtime_status()


@pytest.mark.regression
@pytest.mark.parametrize("passed", [True, False])
def test_catalogue_retains_actual_current_capability_result(monkeypatch, lattice_env, passed):
    adapter = adapters.DEFAULT_ADAPTERS[0]
    status = {**adapter.definition(), "binary_sha256": "a" * 64,
        "installed": True, "qualified": True, "operational": None}
    monkeypatch.setattr("lattice.execution.installed_adapter_status", lambda **_: [status.copy()])
    lattice_env["store"].record_provider_qualification(adapter.profile_id, "a" * 64,
        {"control_surface_qualified": passed, "control_surface_detail": "controls present" if passed else "required output flag missing"},
        state="passed" if passed else "failed")
    with TestClient(create_app(lattice_env["config"])) as client:
        token = ensure_cli_token(lattice_env["config"].operator_token)
        result = client.get("/api/v1/routes", headers={"authorization": f"Bearer {token}"}).json()[0]
    assert result["qualified"] is passed
    assert result["operational"] is passed
    assert result["qualification_current"] is True
    if not passed:
        assert result["qualification_blocker"] == "required output flag missing"


@pytest.mark.regression
def test_dispatch_rechecks_changed_selected_provider_only(monkeypatch, tmp_path):
    selected = adapters.DEFAULT_ADAPTERS[0]
    executable = tmp_path / selected.executable
    executable.write_text("initial")
    monkeypatch.setattr(adapters.shutil, "which", lambda name: str(executable) if name == selected.executable else pytest.fail("unrelated provider inspected"))
    calls = []
    compatible = True
    def probe(chosen, *, probe_versions):
        assert chosen == (selected,)
        assert probe_versions is True
        calls.append(executable.read_text())
        return [{"qualified": True, "operational": compatible,
            "control_surface_qualified": compatible, "control_surface_detail": "required control missing"}]
    monkeypatch.setattr(adapters, "installed_adapter_status", probe)
    monkeypatch.setattr(adapters, "_RUNTIME_QUALIFICATION_CACHE", {})
    adapters.require_current_adapter_qualification(selected)
    adapters.require_current_adapter_qualification(selected)
    assert calls == ["initial"]
    executable.write_text("compatible update")
    adapters.require_current_adapter_qualification(selected)
    assert calls == ["initial", "compatible update"]
    compatible = False
    executable.write_text("incompatible update now")
    with pytest.raises(adapters.SubscriptionRouteUnavailable, match="required control missing"):
        adapters.require_current_adapter_qualification(selected)
